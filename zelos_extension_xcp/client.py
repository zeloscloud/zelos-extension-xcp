"""XCP connection lifecycle: one ECU, one master, one session thread.

`start` loads the A2L, resolves the selection and declares the trace events.
`run` drives the session on its own thread until `stop`: connect, EPK check,
DAQ and polled groups, watchdog, reconnect with backoff after a loss. Every
command goes through the guard in `guard.py`; nothing here writes to the ECU.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import queue
import threading
import time
from enum import StrEnum
from pathlib import Path
from typing import Any

import zelos_sdk

from zelos_extension_xcp import a2l, can_link, daq, guard, poll
from zelos_extension_xcp.compat import (
    Event,
    XcpResponseError,
    XcpTimeoutError,
    close,
    daq_info,
    daq_list_mode,
    make_can_master,
    make_master,
    synch,
    upload,
)
from zelos_extension_xcp.constants import CAN_INTERFACES, DemoTransport, Interface, trace_layout
from zelos_extension_xcp.timestamps import EcuClock

logger = logging.getLogger(__name__)

#: Seconds between checks for a stop while idle.
IDLE_TICK = 0.1

#: Reconnect backoff: doubles per failed attempt up to the cap; a session that
#: reached measurement resets it.
RECONNECT_INITIAL = 3.0
RECONNECT_MAX = 60.0

#: Shutdown path: one attempt per command, this timeout, whatever the settings.
STOP_TIMEOUT = 0.3
#: Stopping an ECU completes within this, in every state.
STOP_BOUND = 3.0

#: An event is stalled after this many cycles without a row, at least WATCHDOG_MIN.
WATCHDOG_CYCLES = 10
WATCHDOG_MIN = 1.0

#: With DAQ running and nothing received for this long, GET_STATUS probes the ECU.
LIVENESS = 1.0

#: Rows missing against the event cycle before a warning: this share of the
#: expected rows, at least MISSING_MIN.
MISSING_SHARE = 0.01
MISSING_MIN = 2

#: Session loop wake-up and status refresh.
TICK = 0.02
STATS_PERIOD = 1.0

#: Seconds between DAQ overload warnings after the first.
OVERLOAD_WARN_PERIOD = 60.0

#: Rows written to the trace per call.
BATCH = 5000

#: A2L warnings logged one by one; the rest are only counted in the log.
LOGGED_WARNINGS = 20

#: ERR_CMD_BUSY: the command was not executed; repeated after this wait (the
#: standard's WAIT_T7 pre-action) until the command's timeout has passed.
BUSY_WAIT = 0.01

#: A poll group missing more than this share of its cycles in a status period is
#: warned about, once.
POLL_MISSED_SHARE = 0.1

#: Trace event, beside the ECU's events, recording each session's provenance.
SESSION_EVENT = "session"
SESSION_FIELDS = (
    ("ecu_epk", zelos_sdk.DataType.String),
    ("a2l_epk", zelos_sdk.DataType.String),
    ("a2l_path", zelos_sdk.DataType.String),
    ("a2l_sha256", zelos_sdk.DataType.String),
    ("epk_result", zelos_sdk.DataType.String),
    ("protocol_version", zelos_sdk.DataType.UInt8),
    ("transport_version", zelos_sdk.DataType.UInt8),
)

#: Event packets logged at warning or by their own handling; others once at info, then debug.
QUIET_EVENTS = frozenset({Event.EV_SESSION_TERMINATED, Event.EV_DAQ_OVERLOAD})


class State(StrEnum):
    STOPPED = "stopped"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    ERROR = "error"


class Refused(Exception):
    """The ECU cannot be measured as configured; no reconnect."""


class SessionLost(Exception):
    """The session ended on the ECU's side or went silent; reconnect."""


class Locked(SessionLost):
    """A resource the selection needs is protected by seed and key; retried at the longest
    reconnect interval."""


class _Event:
    """Counters of one trace event."""

    def __init__(self, group: a2l.Group) -> None:
        self.group = group
        self.rows = 0
        self.rate_hz = 0.0
        self.stalled = False
        self.last_row = 0.0  # monotonic
        self.missed_cycles = 0  # polls skipped to keep the schedule
        self.poll_window_ms: float | None = None  # last poll's read time
        self.frames_per_cycle: int | None = None  # packets per poll
        self._missed_at = 0
        self._rows_at = 0
        self._t_at = 0.0
        self.missing_before = 0  # rows missing in earlier sessions
        self.session_rows = 0
        self.first_t: int | None = None
        self.reset()

    def reset(self) -> None:
        """Start of a DAQ session: the rows-against-cycle count starts over."""
        self.missing_before += self.session_missing or 0
        self.session_rows = 0
        self.first_t = None
        self.last_t = 0
        self.logged_missing = 0

    @property
    def expected_hz(self) -> float | None:
        g = self.group
        if g.rate_ms:
            return 1000.0 / g.rate_ms
        return 1e9 / g.cycle_ns if g.cycle_ns else None

    @property
    def watchdog(self) -> float | None:
        """Seconds without a row before the event counts as stalled; None: no expectation."""
        if not self.group.cycle_ns:
            return None
        return max(WATCHDOG_MIN, WATCHDOG_CYCLES * self.group.cycle_ns / 1e9)

    @property
    def expected_rows(self) -> int | None:
        """Rows the event cycle predicts between the first and last row of this session."""
        if self.group.polled or not self.group.cycle_ns or self.first_t is None:
            return None
        return round((self.last_t - self.first_t) / self.group.cycle_ns) + 1

    @property
    def session_missing(self) -> int | None:
        expected = self.expected_rows
        return None if expected is None else max(0, expected - self.session_rows)

    @property
    def missing_rows(self) -> int | None:
        """Rows missing against the cycle, every session so far."""
        missing = self.session_missing
        return (
            None
            if missing is None and not self.missing_before
            else self.missing_before + (missing or 0)
        )

    def observe(self, t: int) -> None:
        self.rows += 1
        self.session_rows += 1
        if self.first_t is None:
            self.first_t = t
        self.last_t = t

    def silent(self, now: float) -> bool:
        """No row for longer than the watchdog allows, since measurement first started."""
        limit = self.watchdog
        return bool(limit and self.last_row and now - self.last_row > limit)

    def update_rate(self, now: float) -> int:
        """Rate over the last period; returns the poll cycles missed in it."""
        if self._t_at:
            self.rate_hz = (self.rows - self._rows_at) / (now - self._t_at)
        missed = self.missed_cycles - self._missed_at
        self._rows_at, self._t_at, self._missed_at = self.rows, now, self.missed_cycles
        return missed


class XcpConnection:
    """One ECU: XCP session, measurement and trace output.

    Args:
        link: The interface's fields from the config: `host`, `port` (udp,
            tcp); the CAN bus fields plus `tx_id`, `rx_id`, `extended_ids`
            (CAN); `demo_transport` (demo).
    """

    def __init__(
        self,
        name: str,
        interface: str,
        a2l_file: str = "",
        measurements: list[dict[str, Any]] | None = None,
        link: dict[str, Any] | None = None,
        timeout: float = 1.0,
        retries: int = 1,
        timestamp_mode: str = "auto",
        epk_check: str = "strict",
        max_bus_load: float | None = None,
        debug_frames: bool = False,
    ) -> None:
        self.name = name
        self.interface = Interface(interface)
        self.a2l_file = a2l_file
        self.measurements = measurements or []
        self.link = dict(link or {})
        self.timeout = timeout
        self.retries = retries
        self.timestamp_mode = timestamp_mode
        self.epk_check = epk_check
        self.max_bus_load = max_bus_load
        self.debug_frames = debug_frames

        self.state = State.STOPPED
        self.last_error = ""
        self.errors = 0
        self.reconnects = 0
        self.catalog: dict[str, Any] | None = None
        self.plan: a2l.Plan | None = None
        self.a2l_warnings: list[str] = []
        self.daq_skipped: dict[str, str] = {}  # too large for this ECU's DAQ packets
        self.poll_skipped: dict[str, str] = {}  # too large for one upload of this ECU
        self.status_values: dict[str, int] = {}  # raw values in a status string range
        self.locked: list[str] = []  # resources the selection needs, protected
        self.a2l_sha256: str | None = None
        self.epk: dict[str, Any] = {"result": None}
        self.events: dict[str, _Event] = {}
        self.counters: dict[str, Any] = {
            "lost_packets": None if self.transport == DemoTransport.CAN else 0,
            "incomplete_rows": 0,
            "rejected_packets": 0,
            "malformed_datagrams": 0,
            "queue_overflow": 0,
            "unplaced_rows": 0,
            "stale_responses": 0,
            "daq_overloads": 0,
        }
        self.timestamps: dict[str, Any] = {"source": None}
        self.bus_load: dict[str, Any] = {}

        self._running = False
        self._refused = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # One command sequence at a time (SET_MTA + UPLOAD, DAQ setup) across
        # the session thread and actions.
        self._lock = threading.RLock()
        self._master: Any = None
        self._bus: Any = None
        self._receiver: daq.Receiver | None = None
        self._clock: EcuClock | None = None
        self._decoders: list[Any] = []
        self._daq_groups: list[a2l.Group] = []
        self._can_ids: tuple[int, int] = (0, 0)
        self._can_fd = False
        self._max_dlc = False
        self._runs: dict[str, list[poll.Run]] = {}
        self._dtos = 0  # DAQ packets received, counted with debug_frames
        self._dtos_logged = 0
        self._demo: Any = None
        self._last_probe = 0.0
        self._list_mode: tuple[int, int, int] | None = None  # as set on our first DAQ list
        self._unsynched = False
        self._terminated = ""
        self._stale = 0  # stale responses of closed sessions
        self._closed = dict.fromkeys(("incomplete", "rejected", "overflow", "lost"), 0)
        self._overloads = 0  # by event, and by PID MSB of closed sessions
        self._overloads_warned = (0, 0.0)  # count, monotonic time
        self._logged_lost = 0
        self._daq_running = False
        self._sessions = 0
        self._source: zelos_sdk.TraceSource | None = None
        self._event_prefix: str | None = None
        self._paths: dict[str, str] = {}
        self._declared: set[str] = set()
        self._warned: set[str] = set()

    @property
    def transport(self) -> DemoTransport:
        """The XCP transport: `can`, `udp` or `tcp`."""
        if self.interface in CAN_INTERFACES:
            return DemoTransport.CAN
        if self.interface == Interface.DEMO:
            return DemoTransport(self.link.get("demo_transport") or DemoTransport.CAN)
        return DemoTransport(str(self.interface))

    @property
    def endpoint(self) -> str:
        """`udp://host:port`, `tcp://host:port`, `<interface>://<channel>` or `demo/<transport>`."""
        if self.interface == Interface.DEMO:
            return f"demo/{self.transport}"
        if self.transport == DemoTransport.CAN:
            return f"{self.interface}://{self.link.get('channel', '')}"
        return f"{self.transport}://{self.link.get('host', '')}:{self.link.get('port', 5555)}"

    # ─── Lifecycle ─────────────────────────────────────────────────────────

    def start(self, prefix: str, source: zelos_sdk.TraceSource | None = None) -> None:
        """Resolve the trace target, load the A2L and declare the trace events.

        The demo ECU is started here, in-process, so its A2L is known.

        Args:
            prefix: Trace prefix (see `trace_layout`); empty = cleared.
            source: The shared source named `prefix`. Required with a prefix;
                cleared, the connection creates its own source.
        """
        source_name, self._event_prefix = trace_layout(prefix, self.name)
        self._source = source or zelos_sdk.TraceSource(source_name)
        self._running = True
        if self.interface == Interface.DEMO and not self._start_demo():
            return
        self._load()

    def stop(self) -> None:
        """Ask the session to end. Returns at once; `join` waits for it."""
        self._running = False
        self._stop.set()
        master = self._master
        if master is not None:
            master.transport.abort.set()

    def join(self, timeout: float) -> bool:
        """Wait up to `timeout` s for the session to end; True once it has."""
        thread = self._thread
        if thread is not None:
            thread.join(timeout)
            if thread.is_alive():
                return False
        self._stop_demo()
        return True

    async def run(self) -> None:
        """Measure until `stop`. A refused ECU idles in `error` so its state stays visible."""
        if not self._refused:
            self._thread = threading.Thread(
                target=self._worker, name=f"xcp-{self.name}", daemon=True
            )
            self._thread.start()
            while self._thread.is_alive():
                await asyncio.sleep(IDLE_TICK)
        while self._running and not self._stop.is_set():
            await asyncio.sleep(IDLE_TICK)
        self._stop_demo()
        if self.state != State.ERROR:
            self.state = State.STOPPED
        logger.info("[%s] stopped", self.name)

    def _fail(self, reason: str) -> None:
        self.state = State.ERROR
        self.last_error = reason
        self.errors += 1
        logger.error("[%s] %s (%s)", self.name, reason, self.endpoint)

    def _refuse(self, reason: str) -> None:
        self._refused = True
        self._fail(reason)

    def _warn_once(self, key: str, message: str, *args: Any) -> None:
        if key not in self._warned:
            self._warned.add(key)
            logger.warning(message, *args)

    # ─── Demo ECU ──────────────────────────────────────────────────────────

    def _start_demo(self) -> bool:
        """Start the in-process demo ECU and point this connection at it."""
        try:
            from zelos_extension_xcp.demo import DemoEcu
        except ImportError as e:
            self._refuse(f"the demo ECU is not available in this build ({e})")
            return False
        transport = self.transport
        channel = f"xcp-demo-{self.name}-{id(self):x}"
        try:
            demo = DemoEcu(transport=str(transport), host="127.0.0.1", port=0, channel=channel)
            demo.start()
        except Exception as e:
            self._refuse(f"the demo ECU did not start: {e}")
            return False
        self._demo = demo
        self.a2l_file = str(demo.a2l_path)
        if transport == DemoTransport.CAN:
            self.link.update(interface="zelos-virtual", channel=channel)
        else:
            self.link.update(host="127.0.0.1", port=demo.port)
        return True

    def _stop_demo(self) -> None:
        demo, self._demo = self._demo, None
        if demo is not None:
            try:
                demo.stop()
            except Exception as e:
                logger.warning("[%s] demo ECU stop failed: %s", self.name, e)

    # ─── A2L and trace ─────────────────────────────────────────────────────

    def _a2l_transport(self, catalog: dict[str, Any]) -> dict[str, Any] | None:
        """The A2L transport entry this ECU is reached through, or None."""
        if self.transport == DemoTransport.CAN:
            return can_link.a2l_can(catalog, self.link)
        protocol = str(self.transport).upper()
        found = [t for t in catalog.get("transports", []) if t.get("protocol") == protocol]
        if len(found) > 1:
            port = int(self.link.get("port", 5555))
            found = [t for t in found if t.get("port") == port] or found
        return found[0] if found else None

    def _load(self) -> None:
        path = str(Path(self.a2l_file).expanduser())
        try:
            catalog = a2l.load(path)
            self.a2l_sha256 = hashlib.sha256(Path(path).read_bytes()).hexdigest()
        except a2l.A2lUnavailable as e:
            self._refuse(str(e))
            return
        except (ValueError, OSError) as e:  # A2lError is a ValueError
            self._refuse(f"A2L {path}: {e}")
            return
        try:
            section = self._a2l_transport(catalog)
            if self.transport == DemoTransport.CAN:
                self._can_ids = can_link.ids(self.link, catalog)
                self._can_fd = can_link.fd(self.link, catalog)
                self._max_dlc = can_link.max_dlc_required(self.link, catalog)
        except ValueError as e:
            self._refuse(str(e))
            return
        self.catalog = a2l.through(catalog, section)
        try:
            self.plan = a2l.resolve(self.catalog, self.measurements)
        except ValueError as e:
            self._refuse(f"A2L {path}: {e}")
            return
        if any(g.event == SESSION_EVENT for g in self.plan.groups):
            self._refuse(f"ECU event {SESSION_EVENT!r} collides with the session record")
            return
        self.a2l_warnings = list(self.catalog.get("warnings") or [])
        for w in self.a2l_warnings[:LOGGED_WARNINGS]:
            logger.warning("[%s] A2L: %s", self.name, w)
        if len(self.a2l_warnings) > LOGGED_WARNINGS:
            logger.warning(
                "[%s] A2L: %d more warnings, see get_status",
                self.name,
                len(self.a2l_warnings) - LOGGED_WARNINGS,
            )
        if not self.catalog.get("epk"):
            self._warn_once(
                "epk:absent",
                "[%s] A2L has no EPK: the A2L cannot be checked against the ECU",
                self.name,
            )
        plan = self.plan
        if plan.unknown:
            logger.warning(
                "[%s] not in the A2L, not measured: %s", self.name, ", ".join(plan.unknown)
            )
        for name, reason in plan.skipped.items():
            logger.warning("[%s] %s not measured: %s", self.name, name, reason)
        polled: dict[int, list[str]] = {}
        for name, rate in plan.polled_no_default_event.items():
            polled.setdefault(rate, []).append(name)
        if polled:
            logger.warning(
                "[%s] %s",
                self.name,
                "; ".join(
                    f"polled at {rate} ms: no default event in the A2L: {', '.join(names)}"
                    for rate, names in polled.items()
                ),
            )
        if not plan.groups:
            logger.warning("[%s] nothing to measure", self.name)
        session = self._path(SESSION_EVENT)
        self._source.add_event(
            session, [zelos_sdk.TraceEventFieldMetadata(n, t) for n, t in SESSION_FIELDS]
        )
        self._paths[SESSION_EVENT] = session
        for group in plan.groups:
            self._paths[group.event] = self._path(group.event)
            self.events[group.event] = _Event(group)

    def _declare(self, event: str, signals: list[a2l.Signal]) -> None:
        """Declare a trace event once, at its first measurement: a signal skipped for this
        ECU's limits is not a field."""
        if event in self._declared or not signals:
            return
        self._declared.add(event)
        path = self._paths[event]
        fields = [zelos_sdk.TraceEventFieldMetadata(s.field, s.dtype, s.unit) for s in signals]
        self._source.add_event(path, fields)
        for s in signals:
            if s.table:
                self._source.add_value_table(path, s.field, s.table)

    def _path(self, event: str) -> str:
        return f"{self._event_prefix}/{event}" if self._event_prefix else event

    # ─── Session thread ────────────────────────────────────────────────────

    def _worker(self) -> None:
        backoff = RECONNECT_INITIAL
        while not self._stop.is_set():
            measured = False
            try:
                measured = self._session()
            except Refused as e:
                self._refuse(str(e))
            except Locked as e:
                self._fail(str(e))
                backoff = RECONNECT_MAX
            except guard.CommandRefused as e:
                self._refuse(f"blocked a command outside the allowlist: {e}")
            except Exception as e:
                if not self._stop.is_set():
                    self._fail(_describe(e))
            finally:
                self._close()
            if self._stop.is_set() or self._refused:
                break
            if measured:
                backoff = RECONNECT_INITIAL
            self.state = State.CONNECTING
            logger.warning("[%s] reconnecting in %gs", self.name, backoff)
            self._stop.wait(backoff)
            backoff = min(backoff * 2, RECONNECT_MAX)
        if not self._refused and self.state != State.ERROR:
            self.state = State.STOPPED

    def _open(self, receiver: daq.Receiver | None) -> Any:
        """A guarded master for this ECU's transport; the CAN bus is opened here."""
        if self.transport == DemoTransport.CAN:
            link = {"interface": str(self.interface), **self.link}
            self._bus = can_link.open_bus(link, self.name)
            tx, rx = self._can_ids
            master = make_can_master(
                self._bus, tx, rx, self._can_fd, self.timeout, receiver, self._max_dlc
            )
        else:
            host, port = self.link.get("host", ""), int(self.link.get("port", 5555))
            master = make_master(host, port, str(self.transport), self.timeout, receiver)
        guard.install(master)
        self._unsynched = False
        self._terminated = ""
        master.transport.on_event = self._on_event
        if self.transport == DemoTransport.CAN:  # no master identity on CAN
            master.transport.on_unasked = self._on_unasked
        if self.debug_frames:
            master.transport.tap = self._tap
        return master

    def _session(self) -> bool:
        """Connect and measure until stop. True if measurement started."""
        self.state = State.CONNECTING
        plan = self.plan
        lists = daq.daq_lists(plan.daq, self.timestamp_mode == "auto") if plan.daq else []
        can = self.transport == DemoTransport.CAN
        receiver = daq.Receiver(lists, can=can) if lists else None
        self._receiver = receiver
        self._master = self._open(receiver)
        if self._stop.is_set():
            return False
        master = self._master
        master.transport.connect()
        self._request(master.connect, resync=False)
        props = master.slaveProperties
        if str(props.addressGranularity) != "BYTE":
            raise Refused(f"address granularity {props.addressGranularity} is not supported")
        self._check_locked()
        refusal = self._check_epk()
        self._log_session(props)
        if refusal:
            raise Refused(refusal)
        self._plan_polls()
        if plan.daq:
            if not props.supportsDaq:
                raise Refused("the ECU does not support DAQ")
            self._start_daq()
        self.state = State.CONNECTED
        self.last_error = ""
        if self._sessions:
            self.reconnects += 1
        self._sessions += 1
        logger.info("[%s] measuring %d events on %s", self.name, len(plan.groups), self.endpoint)
        self._measure()
        return True

    def _request(self, fn: Any, *args: Any, retries: int | None = None, resync: bool = True) -> Any:
        """One command sequence, retried on timeout up to the `retries` setting.

        Only read-only, repeatable sequences are retried; DAQ configuration is
        not (`retries=0`): a lost response there leaves the slave's state unknown.
        After a timeout no command goes out before SYNCH is answered, so a late
        response is never taken for a later command's. `resync=False`: CONNECT,
        which a slave without a session answers alone.
        """
        attempts = 1 + (self.retries if retries is None else retries)
        busy_until = None
        transport = self._master.transport if self._master is not None else None
        if transport is not None:
            transport.busy += 1
        try:
            while True:
                try:
                    with self._lock:
                        if self._unsynched and resync:
                            synch(self._master)
                            self._unsynched = False
                        return fn(*args)
                except XcpTimeoutError:
                    self._unsynched = resync
                    attempts -= 1
                    if attempts <= 0 or self._stop.is_set():
                        raise
                except XcpResponseError as e:
                    # ERR_CMD_BUSY: not executed, so repeated whatever the command.
                    if "ERR_CMD_BUSY" not in str(e):
                        raise
                    now = time.monotonic()
                    busy_until = busy_until or now + self.timeout
                    if now >= busy_until or self._stop.wait(BUSY_WAIT):
                        raise
        finally:
            if transport is not None:
                transport.busy -= 1

    def _on_unasked(self) -> None:
        """A response to no command of ours, on the receive thread: another master is
        commanding the ECU, so nothing received from here on is decoded."""
        receiver = self._receiver
        if self._daq_running and receiver is not None:
            receiver.closed = True
            self._terminated = "response to a command we did not send: another master?"

    def _on_event(self, code: int, packet: bytes) -> None:
        """An event packet from the ECU, on the receive thread."""
        if code == Event.EV_SESSION_TERMINATED:
            self._terminated = "the ECU ended the session (EV_SESSION_TERMINATED)"
        elif code == Event.EV_DAQ_OVERLOAD:
            self._overloads += 1
            receiver = self._receiver
            if receiver is not None:
                receiver.drop_in_progress()
        if code in QUIET_EVENTS:
            return
        try:
            name = Event(code).name
        except ValueError:
            name = "unknown"
        level = logging.DEBUG if f"event:{code}" in self._warned else logging.INFO
        self._warned.add(f"event:{code}")
        logger.log(level, "[%s] ECU event 0x%02X %s: %s", self.name, code, name, packet.hex(" "))

    def _tap(self, direction: str, packet: bytes) -> None:
        """`debug_frames`: every command, response and event packet; DAQ packets counted."""
        if not packet:
            return
        if direction == "rx" and packet[0] < 0xFC:
            self._dtos += 1
            return
        logger.debug("[%s] %s PID 0x%02X: %s", self.name, direction, packet[0], packet.hex(" "))

    def _limit(self) -> int:
        """Largest read in one command on this ECU."""
        master = self._master
        if master is None:
            raise RuntimeError(f"ECU '{self.name}' is not connected ({self.state})")
        props = master.slaveProperties
        return poll.limit(props.maxCto, bool(props.slaveBlockMode))

    def _upload(self, address: int, ext: int, size: int) -> bytes:
        """`size` bytes of ECU memory, read in one command sequence: `SHORT_UPLOAD` when
        one response holds them, else `SET_MTA` and `UPLOAD`s of up to `_limit` bytes."""
        master = self._master
        if master is None:
            raise RuntimeError(f"ECU '{self.name}' is not connected ({self.state})")
        short, most = master.slaveProperties.maxCto - 1, self._limit()

        def read() -> bytes:
            if size <= short:
                data = master.shortUpload(size, address, ext)
            else:
                master.setMta(address, ext)
                data = b"".join(upload(master, min(most, size - o)) for o in range(0, size, most))
            if len(data) != size:
                raise SessionLost(f"read {len(data)} of {size} bytes at 0x{address:X}")
            return bytes(data)

        return self._request(read)

    def _check_locked(self) -> None:
        """GET_STATUS: a resource the selection needs protected by seed and key is not
        measured; retried at the longest reconnect interval, in case it is unlocked."""
        protection = self._request(self._master.getStatus).resourceProtectionStatus
        plan = self.plan
        needed = [("DAQ", protection.daq and bool(plan.daq))]
        needed.append(("CAL/PAG", protection.calpag and bool(plan.polls)))
        self.locked = [name for name, locked in needed if locked]
        if self.locked:
            reason = f"locked: seed and key is not supported ({', '.join(self.locked)})"
            self._warn_once("locked", "[%s] %s", self.name, reason)
            raise Locked(reason)

    def _check_epk(self) -> str | None:
        """Compare the A2L's EPK with the ECU's; the refusal reason, or None."""
        epk = (self.catalog or {}).get("epk")
        if not epk:
            self.epk = {"result": "absent"}
            return None
        expected = epk["string"]
        self.epk = {"a2l": expected, "ecu": None, "result": None}
        for attempt in range(2):  # a transient error is not a stale A2L
            try:
                data = self._upload(epk["address"], 0, len(expected.encode("latin-1")))
                break
            except XcpResponseError as e:
                if attempt:
                    self.epk["result"] = "unreadable"
                    reason = f"cannot read the EPK at 0x{epk['address']:X}: {e}"
                    if self.epk_check == "strict":
                        return f"{reason}; not measuring (epk_check: strict)"
                    self._warn_once(
                        "epk", "[%s] %s; measuring anyway (epk_check: warn)", self.name, reason
                    )
                    return None
        ecu = data.decode("latin-1")
        self.epk["ecu"] = ecu
        if ecu == expected:
            self.epk["result"] = "match"
            return None
        self.epk["result"] = "mismatch"
        reason = f"EPK mismatch: A2L {expected!r}, ECU {ecu!r}"
        if self.epk_check == "strict":
            return f"{reason}; the A2L is not from this build, not measuring (epk_check: strict)"
        self._warn_once("epk", "[%s] %s; measuring anyway (epk_check: warn)", self.name, reason)
        return None

    def _log_session(self, props: Any) -> None:
        """One `session` row: the build measured and the A2L it is read with."""
        fields = {
            "ecu_epk": self.epk.get("ecu"),
            "a2l_epk": self.epk.get("a2l"),
            "a2l_path": str(Path(self.a2l_file).expanduser()),
            "a2l_sha256": self.a2l_sha256,
            "epk_result": self.epk.get("result"),
            "protocol_version": props.protocolLayerVersion,
            "transport_version": props.transportLayerVersion,
        }
        row = {k: v for k, v in fields.items() if v is not None}
        self._source.log_many([(time.time_ns(), self._paths[SESSION_EVENT], row)])

    def _plan_polls(self) -> None:
        """Each poll group's reads for this ECU's packet size and block mode."""
        self._runs, self.poll_skipped = {}, {}
        max_cto = self._master.slaveProperties.maxCto
        for g in self.plan.polls:
            runs, skipped = poll.runs(g.signals, self._limit())
            self._runs[g.event] = runs
            self.events[g.event].frames_per_cycle = poll.frames(runs, max_cto)
            self.poll_skipped.update(skipped)
            self._declare(g.event, [s for s in g.signals if s.name not in skipped])
        for name, reason in self.poll_skipped.items():
            self._warn_once(f"skip:{name}", "[%s] %s not measured: %s", self.name, name, reason)

    def _layout(self) -> tuple[daq.Layout, list[a2l.Group], list]:
        """The DAQ groups that fit, their lists and checked layout: slave limits, then CAN
        bus load.

        Signals larger than one DAQ packet are left out, reasons in `daq_skipped`.
        Raises Refused with the reason when the rest does not fit.
        """
        master = self._master
        info = self._daq_info()
        max_dto = master.slaveProperties.maxDto
        hint = "use CAN FD or poll it" if self.transport == DemoTransport.CAN else "poll it"
        self.daq_skipped = daq.oversized(info, max_dto, self.plan.daq, hint)
        for name, reason in self.daq_skipped.items():
            self._warn_once(f"skip:{name}", "[%s] %s not measured: %s", self.name, name, reason)
        groups = daq.without(self.plan.daq, self.daq_skipped)
        lists = daq.daq_lists(groups, self.timestamp_mode == "auto")
        try:
            layout = daq.plan_layout(info, max_dto, lists, groups)
        except daq.CapacityError as e:
            raise Refused(f"selection does not fit the ECU: {e}") from e
        if self.transport == DemoTransport.CAN:
            channels = [g.channel for g in groups]
            if foreign := can_link.foreign_ids(
                self.link, self.catalog, len(lists), channels, self._can_ids[1]
            ):
                raise Refused(foreign)
            self.bus_load = self._estimate_load(layout, groups)
            load, ceiling = self.bus_load["total_pct"], self.bus_load["ceiling_pct"]
            if load is None and self.interface != Interface.DEMO:
                # SocketCAN carries no bitrate in the config; without one in
                # the A2L either the ceiling cannot be checked.
                self._warn_once(
                    "bus_load:unknown",
                    "[%s] DAQ bus load not checked: no bitrate on the interface or in the A2L",
                    self.name,
                )
            if load is not None and ceiling is not None and load > ceiling:
                raise Refused(
                    f"selection needs about {load:.1f}% of the CAN bus, over the "
                    f"{ceiling:g}% ceiling ({self.bus_load['ceiling_source']})"
                )
        return layout, groups, lists, info

    def _daq_info(self) -> dict[str, Any]:
        """GET_DAQ_PROCESSOR_INFO and GET_DAQ_RESOLUTION_INFO, asked through `_request`.

        The library's own query swallows a timeout, so a late answer could be read as
        the next command's; here a timeout is retried after SYNCH and, if still not
        answered, ends the session. Only a refusal refuses the ECU.
        """
        master = self._master
        answers = []
        for fn, name in (
            (master.getDaqProcessorInfo, "GET_DAQ_PROCESSOR_INFO"),
            (master.getDaqResolutionInfo, "GET_DAQ_RESOLUTION_INFO"),
        ):
            try:
                answers.append(self._request(fn))
            except XcpTimeoutError:
                raise SessionLost(f"{name} not answered") from None
            except XcpResponseError as e:
                raise Refused(f"the ECU does not support {name}: {e}") from e
        return daq_info(*answers)

    def _estimate_load(self, layout: daq.Layout, groups: list[a2l.Group]) -> dict[str, Any]:
        """DAQ frames per second and bus load per event, from the layout and event cycles."""
        rates = can_link.bitrates(self.link, self.catalog)
        extended = bool(self._can_ids[1] & can_link.EXTENDED)
        events: dict[str, Any] = {}
        total: float | None = 0.0 if rates else None
        for group, sizes in zip(groups, layout.odt_bytes, strict=True):
            hz = 1e9 / group.cycle_ns if group.cycle_ns else None
            frames = len(sizes) * hz if hz else None
            pct = None
            if frames is not None and rates:
                busy = sum(can_link.frame_seconds(n, extended, self._can_fd, rates) for n in sizes)
                pct = 100.0 * busy * hz
                total += pct
            elif hz is None:
                self._warn_once(
                    f"sporadic:{group.event}",
                    "[%s] event %r has no cycle: its bus load is not counted",
                    self.name,
                    group.event,
                )
            events[group.event] = {"odts": len(sizes), "frames_per_s": frames, "bus_load_pct": pct}
        ceiling = self.max_bus_load  # None: reported, never refused
        return {
            "events": events,
            "total_pct": total,
            "bitrate": rates[0] if rates else None,
            "bitrate_source": ("interface" if self.link.get("bitrate") else "A2L")
            if rates
            else None,
            "ceiling_pct": ceiling,
            "ceiling_source": "max_bus_load" if ceiling is not None else None,
        }

    def _start_daq(self) -> None:
        master, receiver = self._master, self._receiver
        layout, groups, lists, info = self._layout()
        if not groups:
            self._receiver = None  # every DAQ signal skipped
            return
        receiver.daq_lists, receiver.is_predefined = lists, [False] * len(lists)
        self._daq_groups = groups
        for g in groups:
            self._declare(g.event, g.signals)
        try:
            receiver.one_ext_per_odt = layout.one_ext_per_odt
            with self._lock:
                receiver.setup(info)
        except XcpResponseError as e:
            code = str(e)
            if "MEMORY_OVERFLOW" in code or "OUT_OF_RANGE" in code:
                self._free_daq()
                raise Refused(
                    f"selection does not fit the ECU: DAQ allocation refused ({code}) for "
                    f"{sum(layout.odt_entries)} entries in {sum(layout.odts)} ODTs"
                ) from e
            raise
        built = [len(dl.measurements_opt) for dl in receiver.daq_lists]
        if built != layout.odts:
            self._free_daq()
            raise Refused(f"DAQ layout {built} differs from the checked plan {layout.odts}")
        try:
            positions = [
                daq.positions(dl, g) for dl, g in zip(receiver.daq_lists, groups, strict=True)
            ]
        except daq.CapacityError as e:
            self._free_daq()
            raise Refused(str(e)) from e
        little = str(master.slaveProperties.byteOrder) == "INTEL"
        self._decoders = [
            daq.decoder(g, p, little, self.status_values)
            for g, p in zip(groups, positions, strict=True)
        ]
        if self.transport == DemoTransport.CAN:  # a slave may fill each frame
            receiver.pad = can_link.FD_LENGTHS if self._can_fd else (8,)
        else:  # or each packet, to a 2 or 4 byte boundary
            receiver.align = (2, 4)
        receiver.arm(layout, little, receiver.first_pids())
        self._clock = None
        receive = "adapter" if self.transport == DemoTransport.CAN else "host"
        self.timestamps = {"source": receive}
        if self.timestamp_mode == "auto" and layout.timestamps:
            cycles = {i: g.cycle_ns for i, g in enumerate(groups)}
            self._clock = EcuClock(layout.ts_size, layout.tick_ns, cycles)
            try:
                self._anchor()
                self.timestamps = {"source": "ecu"}
            except XcpResponseError as e:
                self._clock = None
                self._warn_once(
                    "clock", "[%s] GET_DAQ_CLOCK refused (%s): receive time", self.name, e
                )
        elif self.timestamp_mode == "auto":
            self._warn_once("nots", "[%s] the ECU sends no DAQ timestamps: receive time", self.name)
        now = time.monotonic()
        for g in groups:
            self.events[g.event].last_row = now
            self.events[g.event].reset()
        first = receiver.daq_lists[0]
        self._list_mode = (
            0x10 if layout.timestamps else 0,
            first.event_num,
            first.prescaler,
        )
        self._request(receiver.start, retries=0)
        self._daq_running = True

    def _free_daq(self) -> None:
        try:
            self._request(self._master.freeDaq, retries=0)
        except Exception as e:
            logger.debug("[%s] FREE_DAQ failed: %s", self.name, e)

    def _anchor(self) -> None:
        before = time.time_ns()
        clock = self._request(self._master.getDaqClock)
        after = time.time_ns()
        self._clock.anchor((before + after) // 2, clock.timestamp)

    def _measure(self) -> None:
        """Drain DAQ rows, run due polls and the watchdog until stop."""
        receiver = self._receiver
        polls = {g.event: time.monotonic() for g in self.plan.polls}
        next_stats = time.monotonic()
        self._last_probe = 0.0
        while not self._stop.is_set():
            if self._terminated:
                raise SessionLost(self._terminated)
            now = time.monotonic()
            for event, due in polls.items():
                if now >= due:
                    ev = self.events[event]
                    self._poll(ev)
                    rate, after = ev.group.rate_ms / 1000, time.monotonic()
                    # Late by a cycle or more: start now, the cycles passed are missed.
                    ev.missed_cycles += max(0, int((after - due) / rate) - 1)
                    polls[event] = max(due + rate, after)
            if now >= next_stats:
                self._check(now)
                next_stats = now + STATS_PERIOD
            wait = min([TICK, *(due - time.monotonic() for due in polls.values())])
            if receiver is None:
                self._stop.wait(max(wait, 0.0))
                continue
            try:
                first = receiver.rows.get(timeout=max(wait, 0.0))
            except queue.Empty:
                continue
            self._drain(first)

    def _drain(self, first: tuple) -> None:
        """Decode, time and write the queued rows, `first` included."""
        clock, groups = self._clock, self._daq_groups
        batch: list[tuple[int, str, dict[str, Any]]] = []
        touched: set[_Event] = set()
        item: tuple | None = first
        while item is not None:
            idx, rx, raw, values = item
            event = groups[idx].event
            if clock is not None and raw is not None:
                if clock.needs_anchor(idx, rx):
                    self._anchor()
                t = clock.stamp(idx, raw, rx)
            else:
                t = rx
            if t is None:
                self.counters["unplaced_rows"] += 1
                self._warn_once(
                    "unplaced",
                    "[%s] rows held by the ECU through a gap cannot be timed exactly; dropped "
                    "(see unplaced_rows)",
                    self.name,
                )
                item = self._next_row()
                continue
            batch.append((t, self._paths[event], self._decoders[idx](values)))
            ev = self.events[event]
            ev.observe(t)
            touched.add(ev)
            if len(batch) >= BATCH:
                break
            item = self._next_row()
        self._source.log_many(batch)
        now = time.monotonic()
        for ev in touched:
            ev.last_row = now

    def _next_row(self) -> tuple | None:
        try:
            return self._receiver.rows.get_nowait()
        except queue.Empty:
            return None

    def _poll(self, ev: _Event) -> None:
        """One poll of a group, stamped at the middle of its read window."""
        row: dict[str, Any] = {}
        runs = self._runs[ev.group.event]
        start = time.time_ns()
        for i, run in enumerate(list(runs)):
            try:
                data = self._upload(run.address, run.ext, run.size)
            except XcpResponseError as e:
                names = ", ".join(s.name for s, _ in run.signals)
                if len(run.signals) > 1:
                    # One unreadable signal must not cost its neighbours: read them apart.
                    runs[i : i + 1] = []
                    runs.extend(poll.split(run))
                    ev.frames_per_cycle = poll.frames(runs, self._master.slaveProperties.maxCto)
                    self._warn_once(
                        f"poll:{names}", "[%s] reading %s apart: %s", self.name, names, e
                    )
                else:
                    self._warn_once(f"poll:{names}", "[%s] cannot read %s: %s", self.name, names, e)
                continue
            for s, offset in run.signals:
                value = s.physical(s.unpack(data[offset : offset + s.size]))
                if value is None:
                    self.status_values[s.name] = self.status_values.get(s.name, 0) + 1
                else:
                    row[s.field] = value
        end = time.time_ns()
        ev.poll_window_ms = (end - start) / 1e6
        if row:
            t = (start + end) // 2
            self._source.log_many([(t, self._paths[ev.group.event], row)])
            ev.observe(t)
            ev.last_row = time.monotonic()

    def _check(self, now: float) -> None:
        """Once a second: rates, watchdog, rows against the cycle, liveness, counters."""
        for ev in self.events.values():
            missed = ev.update_rate(now)
            if missed > POLL_MISSED_SHARE * STATS_PERIOD * (ev.expected_hz or 0):
                self._warn_once(
                    f"slow:{ev.group.event}",
                    "[%s] %s cannot keep %d ms: %.1f of %.1f Hz, a poll takes %.1f ms "
                    "(see missed_cycles)",
                    self.name,
                    ev.group.event,
                    ev.group.rate_ms,
                    ev.rate_hz,
                    ev.expected_hz,
                    ev.poll_window_ms or 0.0,
                )
            self._check_missing(ev)
            stalled = ev.silent(now)
            if stalled and not ev.stalled:
                logger.warning(
                    "[%s] no data on event %r for %.1fs",
                    self.name,
                    ev.group.event,
                    now - ev.last_row,
                )
            elif ev.stalled and not stalled:
                logger.info("[%s] data on event %r again", self.name, ev.group.event)
            ev.stalled = stalled
        receiver, master = self._receiver, self._master
        self.counters["stale_responses"] = self._stale + master.transport.stale_responses
        self._count_overloads(now)
        if receiver is not None:
            framer = getattr(master.transport, "_eth_receiver", None)
            total = {k: n + getattr(receiver, k) for k, n in self._closed.items()}
            self.counters.update(
                incomplete_rows=total["incomplete"],
                rejected_packets=total["rejected"],
                malformed_datagrams=getattr(framer, "malformed", 0),
                queue_overflow=total["overflow"],
            )
            if not receiver.can:
                lost = self.counters["lost_packets"] = total["lost"]
                if lost > self._logged_lost:
                    logger.warning(
                        "[%s] %d packets lost (%d in all)",
                        self.name,
                        lost - self._logged_lost,
                        lost,
                    )
                    self._logged_lost = lost
            if self._list_mode is not None:
                self._check_list_mode()
            silent = time.monotonic() - receiver.last_frame
            if silent > LIVENESS and now - self._last_probe > LIVENESS:
                self._last_probe = now
                status = self._request(master.getStatus)
                if not status.sessionStatus.daqRunning:
                    raise SessionLost("the ECU stopped DAQ")
        if self._clock is not None:
            self.timestamps.update(
                anchors=self._clock.anchors,
                offset_s=self._clock.offset_ns / 1e9,
                drift_ppm=self._clock.drift_ppm,
            )
        if self.debug_frames:
            dtos, self._dtos_logged = self._dtos - self._dtos_logged, self._dtos
            logger.debug("[%s] %d DAQ packets in %gs", self.name, dtos, STATS_PERIOD)
        if getattr(master.transport, "use_tcp", False) and master.transport.status == 0:
            raise SessionLost("the ECU closed the connection")

    def _check_list_mode(self) -> None:
        """Our first DAQ list still has the mode, event and prescaler we set. XCP on CAN
        cannot tell masters apart: after another master's setup its DAQ packets would
        decode as ours."""
        want = self._list_mode
        try:
            got = self._request(daq_list_mode, self._master, 0)
        except XcpResponseError as e:
            if "ERR_CMD_UNKNOWN" not in str(e):
                raise SessionLost(
                    f"DAQ configuration changed under us: another master? ({e})"
                ) from e
            self._list_mode = None
            self._warn_once("list_mode", "[%s] no GET_DAQ_LIST_MODE on this ECU", self.name)
            return
        if got != want:
            raise SessionLost(
                "DAQ configuration changed under us: another master? (first DAQ list: "
                f"mode 0x{got[0]:02X}, event {got[1]}, prescaler {got[2]}; set "
                f"0x{want[0]:02X}, {want[1]}, {want[2]})"
            )

    def _count_overloads(self, now: float) -> None:
        receiver = self._receiver
        total = self._overloads + (receiver.overloads if receiver is not None else 0)
        self.counters["daq_overloads"] = total
        warned, at = self._overloads_warned
        if total > warned and (not warned or now - at >= OVERLOAD_WARN_PERIOD):
            logger.warning(
                "[%s] the ECU reports DAQ overload: %d in all, samples in progress dropped "
                "(see daq_overloads)",
                self.name,
                total,
            )
            self._overloads_warned = (total, now)

    def _check_missing(self, ev: _Event) -> None:
        missing = ev.session_missing
        if missing is None:
            return
        allowed = max(MISSING_MIN, MISSING_SHARE * ev.expected_rows)
        if missing - ev.logged_missing > allowed:
            logger.warning(
                "[%s] event %r: %d of %d rows the event cycle predicts are missing",
                self.name,
                ev.group.event,
                missing,
                ev.expected_rows,
            )
            ev.logged_missing = missing

    def _close(self) -> None:
        """Stop DAQ, disconnect, close: one short attempt each, whatever the settings."""
        master, bus, receiver = self._master, self._bus, self._receiver
        self._master = self._receiver = self._bus = None
        if receiver is not None:
            self._overloads += receiver.overloads
            for k in self._closed:
                self._closed[k] += getattr(receiver, k)
        if master is not None:
            transport = master.transport
            transport.abort.clear()
            transport.timeout = int(STOP_TIMEOUT * 1e9)
            transport.pending_max = 0  # EV_CMD_PENDING must not stretch the stop
            if self._daq_running:
                try:
                    master.startStopSynch(0)
                except Exception as e:
                    logger.debug("[%s] DAQ stop failed: %s", self.name, e)
            try:
                master.disconnect()
            except Exception as e:
                logger.debug("[%s] DISCONNECT failed: %s", self.name, e)
            try:
                close(master)
            except Exception as e:
                logger.debug("[%s] close failed: %s", self.name, e)
            self._stale += transport.stale_responses
        if bus is not None:
            try:
                bus.shutdown()
            except Exception as e:
                logger.debug("[%s] bus shutdown failed: %s", self.name, e)
        self._daq_running = False

    # ─── Actions ───────────────────────────────────────────────────────────

    def _catalog(self) -> dict[str, Any]:
        if self.catalog is None:
            raise ValueError(f"ECU '{self.name}' has no A2L loaded: {self.last_error}")
        return self.catalog

    def list_events(self) -> list[dict[str, Any]]:
        """ECU event channels from the A2L."""
        return [
            {
                "name": e["name"],
                "channel": e["channel"],
                "cycle_ms": (c / 1e6 if (c := a2l.cycle_ns(e)) else None),
            }
            for e in self._catalog().get("events", [])
        ]

    def list_measurements(self, search: str, offset: int, limit: int) -> dict[str, Any]:
        """Measurements whose name contains `search` (any case), one page."""
        catalog = self._catalog()
        channels = {e["channel"]: e["name"] for e in catalog.get("events", [])}
        needle = search.lower()
        found = [m for m in catalog.get("measurements", []) if needle in m["name"].lower()]
        rows = []
        for m in found[offset : offset + limit]:
            ev = m["events"]
            default = (ev.get("fixed") or ev.get("default") or [None])[0]
            rows.append(
                {
                    "name": m["name"],
                    "unit": m.get("unit") or "",
                    "datatype": m["datatype"],
                    "default_event": channels.get(default),
                }
            )
        return {"measurements": rows, "total": len(found), "offset": offset}

    def read(self, name: str) -> dict[str, Any]:
        """One-shot read of measurement `name`: physical value and unit."""
        m = next((m for m in self._catalog().get("measurements", []) if m["name"] == name), None)
        if m is None:
            raise ValueError(f"{name!r} is not in the A2L")
        s = a2l.signal(m)
        if self.state != State.CONNECTED:
            raise RuntimeError(f"ECU '{self.name}' is not measuring ({self.state})")
        # One command: a value that needs several could be torn.
        _, skipped = poll.runs([s], self._limit())
        if skipped:
            raise ValueError(skipped[name])
        raw = s.unpack(self._upload(s.address, s.ext, s.size))
        value = s.physical(raw)
        result = {"name": name, "value": value, "unit": s.unit, "datatype": s.datatype}
        if value is None:
            result["status"] = s.status_text(s.masked(raw))
        elif s.table is not None:
            result["label"] = s.table.get(int(value))
        return result

    def check_selection(self) -> dict[str, Any]:
        """The configured selection against the A2L and, when connected, the ECU's limits.

        Reads only the ECU's DAQ limits; measurement is not started or changed.
        """
        self._catalog()
        plan = self.plan
        events = {
            g.event: {"source": "poll" if g.polled else "daq", "signals": len(g.signals)}
            for g in plan.groups
        }
        result: dict[str, Any] = {
            "events": events,
            "skipped": dict(plan.skipped),
            "polled_no_default_event": dict(plan.polled_no_default_event),
            "unknown": list(plan.unknown),
            "fits": None,
            "reason": "",
        }
        if not plan.daq:
            result["fits"] = True
        elif self._master is None or self.state not in (State.CONNECTED, State.CONNECTING):
            result["reason"] = f"not connected ({self.state}): checked against the A2L only"
        else:
            try:
                layout, groups, _, _ = self._layout()
            except (Refused, SessionLost) as e:
                result.update(fits=False, reason=str(e))
            else:
                result["skipped"].update(self.daq_skipped)
                result["skipped"].update(self.poll_skipped)
                for g, odts, count in zip(groups, layout.odts, layout.odt_entries, strict=True):
                    events[g.event].update(odts=odts, odt_entries=count)
                    if self.bus_load:
                        events[g.event].update(self.bus_load["events"][g.event])
                if self.bus_load:
                    result["bus_load"] = {k: v for k, v in self.bus_load.items() if k != "events"}
                result["fits"] = True
                result["reason"] = (
                    "within the ECU's reported limits; DAQ memory is confirmed when "
                    "measurement starts"
                )
        return result

    def status(self) -> dict[str, Any]:
        """State and counters only: nothing is read from the ECU."""
        plan, now = self.plan, time.monotonic()
        return {
            "ecu": self.name,
            "interface": str(self.interface),
            "transport": str(self.transport),
            "endpoint": self.endpoint,
            "a2l_file": self.a2l_file,
            "state": str(self.state),
            "error": self.last_error,
            "errors": self.errors,
            "reconnects": self.reconnects,
            "epk": dict(self.epk),
            "locked": list(self.locked),
            "events": {
                name: {
                    "source": "poll" if ev.group.polled else "daq",
                    "signals": len(ev.group.signals),
                    "rows": ev.rows,
                    "rate_hz": round(ev.rate_hz, 3),
                    "expected_hz": ev.expected_hz,
                    "expected_rows": ev.expected_rows,
                    "missing_rows": ev.missing_rows,
                    "stalled": ev.silent(now),
                    **(
                        {
                            "missed_cycles": ev.missed_cycles,
                            "poll_window_ms": ev.poll_window_ms,
                            "frames_per_cycle": ev.frames_per_cycle,
                        }
                        if ev.group.polled
                        else {}
                    ),
                }
                for name, ev in self.events.items()
            },
            "watchdog": "stalled"
            if self._running and any(ev.silent(now) for ev in self.events.values())
            else "ok",
            **self.counters,
            "status_values": dict(self.status_values),
            "bus_load": dict(self.bus_load),
            "timestamps": dict(self.timestamps),
            "a2l_warnings": list(self.a2l_warnings),
            "unknown": list(plan.unknown) if plan else [],
            "skipped": {**plan.skipped, **self.daq_skipped, **self.poll_skipped} if plan else {},
            "polled_no_default_event": dict(plan.polled_no_default_event) if plan else {},
        }


def _describe(e: Exception) -> str:
    if isinstance(e, XcpTimeoutError):
        return "no response from the ECU"
    if isinstance(e, XcpResponseError):
        return f"the ECU refused a command: {e}"
    return f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
