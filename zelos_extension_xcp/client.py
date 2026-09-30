"""XCP connection lifecycle: one ECU, one master, one session thread.

`start` loads the A2L, resolves the selection and declares the trace events.
`run` drives the session on its own thread until `stop`: connect, EPK check,
DAQ and polled groups, watchdog, reconnect with backoff after a loss. Every
command goes through the guard in `guard.py`; nothing here writes to the ECU.
"""

from __future__ import annotations

import asyncio
import logging
import queue
import threading
import time
from enum import StrEnum
from pathlib import Path
from typing import Any

import zelos_sdk

from zelos_extension_xcp import a2l, can_link, daq, guard
from zelos_extension_xcp.compat import (
    Event,
    XcpResponseError,
    XcpTimeoutError,
    close,
    make_can_master,
    make_master,
    synch,
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


class State(StrEnum):
    STOPPED = "stopped"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    ERROR = "error"


class Refused(Exception):
    """The ECU cannot be measured as configured; no reconnect."""


class SessionLost(Exception):
    """The session ended on the ECU's side or went silent; reconnect."""


class _Event:
    """Counters of one trace event."""

    def __init__(self, group: a2l.Group) -> None:
        self.group = group
        self.rows = 0
        self.rate_hz = 0.0
        self.stalled = False
        self.last_row = 0.0  # monotonic
        self._rows_at = 0
        self._t_at = 0.0
        self.reset()

    def reset(self) -> None:
        """Start of a DAQ session: the rows-against-cycle count starts over."""
        self.session_rows = 0
        self.first_t: int | None = None
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
    def missing_rows(self) -> int | None:
        expected = self.expected_rows
        return None if expected is None else max(0, expected - self.session_rows)

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

    def update_rate(self, now: float) -> None:
        if self._t_at:
            self.rate_hz = (self.rows - self._rows_at) / (now - self._t_at)
        self._rows_at, self._t_at = self.rows, now


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

        self.state = State.STOPPED
        self.last_error = ""
        self.errors = 0
        self.reconnects = 0
        self.catalog: dict[str, Any] | None = None
        self.plan: a2l.Plan | None = None
        self.a2l_warnings: list[str] = []
        self.daq_skipped: dict[str, str] = {}  # too large for this ECU's DAQ packets
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
        self._demo: Any = None
        self._last_probe = 0.0
        self._unsynched = False
        self._terminated = ""
        self._stale = 0  # stale responses of closed sessions
        self._overloads = 0  # by event, and by PID MSB of closed sessions
        self._overloads_warned = (0, 0.0)  # count, monotonic time
        self._logged_lost = 0
        self._daq_running = False
        self._sessions = 0
        self._source: zelos_sdk.TraceSource | None = None
        self._event_prefix: str | None = None
        self._paths: dict[str, str] = {}
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

    def _load(self) -> None:
        path = str(Path(self.a2l_file).expanduser())
        try:
            self.catalog = a2l.load(path)
            self.plan = a2l.resolve(self.catalog, self.measurements)
        except a2l.A2lUnavailable as e:
            self._refuse(str(e))
            return
        except (ValueError, OSError) as e:  # A2lError is a ValueError
            self._refuse(f"A2L {path}: {e}")
            return
        if self.transport == DemoTransport.CAN:
            try:
                self._can_ids = can_link.ids(self.link, self.catalog)
                self._can_fd = can_link.fd(self.link, self.catalog)
            except ValueError as e:
                self._refuse(str(e))
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
            logger.warning(
                "[%s] A2L has no EPK: the A2L cannot be checked against the ECU", self.name
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
        for group in plan.groups:
            path = f"{self._event_prefix}/{group.event}" if self._event_prefix else group.event
            fields = [
                zelos_sdk.TraceEventFieldMetadata(s.field, s.dtype, s.unit) for s in group.signals
            ]
            self._source.add_event(path, fields)
            for s in group.signals:
                if s.table:
                    self._source.add_value_table(path, s.field, s.table)
            self._paths[group.event] = path
            self.events[group.event] = _Event(group)

    # ─── Session thread ────────────────────────────────────────────────────

    def _worker(self) -> None:
        backoff = RECONNECT_INITIAL
        while not self._stop.is_set():
            measured = False
            try:
                measured = self._session()
            except Refused as e:
                self._refuse(str(e))
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
            master = make_can_master(self._bus, tx, rx, self._can_fd, self.timeout, receiver)
        else:
            host, port = self.link.get("host", ""), int(self.link.get("port", 5555))
            master = make_master(host, port, str(self.transport), self.timeout, receiver)
        guard.install(master)
        self._unsynched = False
        self._terminated = ""
        master.transport.on_event = self._on_event
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
        self._check_epk()
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
        for attempt in range(attempts):
            try:
                with self._lock:
                    if self._unsynched and resync:
                        synch(self._master)
                        self._unsynched = False
                    return fn(*args)
            except XcpTimeoutError:
                self._unsynched = resync
                if attempt + 1 >= attempts or self._stop.is_set():
                    raise
        raise AssertionError("unreachable")

    def _on_event(self, code: int, packet: bytes) -> None:
        """An event packet from the ECU, on the receive thread."""
        if code == Event.EV_SESSION_TERMINATED:
            self._terminated = "the ECU ended the session (EV_SESSION_TERMINATED)"
        elif code == Event.EV_DAQ_OVERLOAD:
            self._overloads += 1
            receiver = self._receiver
            if receiver is not None:
                receiver.drop_in_progress()
        else:
            try:
                name = Event(code).name
            except ValueError:
                name = "unknown"
            logger.info("[%s] ECU event 0x%02X %s: %s", self.name, code, name, packet.hex(" "))

    def _upload(self, address: int, ext: int, size: int) -> bytes:
        """`size` bytes of ECU memory, read in one command sequence."""
        master = self._master
        if master is None:
            raise RuntimeError(f"ECU '{self.name}' is not connected ({self.state})")
        chunk = master.slaveProperties.maxCto - 1

        def read() -> bytes:
            if size <= chunk:
                data = master.shortUpload(size, address, ext)
            else:
                master.setMta(address, ext)
                # On CAN the library returns padded frames: keep the requested bytes.
                data = b"".join(
                    master.upload(n)[:n]
                    for n in (min(chunk, size - o) for o in range(0, size, chunk))
                )
            if len(data) != size:
                raise SessionLost(f"read {len(data)} of {size} bytes at 0x{address:X}")
            return bytes(data)

        return self._request(read)

    def _read_signal(self, s: a2l.Signal) -> int | float:
        """Physical value of `s`, read in one command. Torn multi-command reads are refused."""
        master = self._master
        if master is not None and s.size > master.slaveProperties.maxCto - 1:
            raise ValueError(f"{s.size} bytes do not fit one SHORT_UPLOAD")
        return s.physical(s.unpack(self._upload(s.address, s.ext, s.size)))

    def _check_epk(self) -> None:
        epk = (self.catalog or {}).get("epk")
        if not epk:
            self.epk = {"result": "absent"}
            return
        expected = epk["string"]
        self.epk = {"a2l": expected, "ecu": None, "result": None}
        try:
            data = self._upload(epk["address"], 0, len(expected.encode("latin-1")))
        except XcpResponseError as e:
            self.epk["result"] = "unreadable"
            reason = f"cannot read the EPK at 0x{epk['address']:X}: {e}"
            if self.epk_check == "strict":
                raise Refused(f"{reason}; not measuring (epk_check: strict)") from e
            self._warn_once("epk", "[%s] %s; measuring anyway (epk_check: warn)", self.name, reason)
            return
        ecu = data.decode("latin-1")
        self.epk["ecu"] = ecu
        if ecu == expected:
            self.epk["result"] = "match"
            return
        self.epk["result"] = "mismatch"
        reason = f"EPK mismatch: A2L {expected!r}, ECU {ecu!r}"
        if self.epk_check == "strict":
            raise Refused(
                f"{reason}; the A2L is not from this build, not measuring (epk_check: strict)"
            )
        self._warn_once("epk", "[%s] %s; measuring anyway (epk_check: warn)", self.name, reason)

    def _layout(self) -> tuple[daq.Layout, list[a2l.Group], list]:
        """The DAQ groups that fit, their lists and checked layout: slave limits, then CAN
        bus load.

        Signals larger than one DAQ packet are left out, reasons in `daq_skipped`.
        Raises Refused with the reason when the rest does not fit.
        """
        master = self._master
        info = self._request(master.getDaqInfo, False)
        if not all(info.get("valid", {}).get(k, True) for k in ("processor", "resolution")):
            raise Refused("the ECU does not report its DAQ processor and resolution")
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
        return layout, groups, lists

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
        a2l_ceiling = (can_link.a2l_can(self.catalog, self.link) or {}).get("max_bus_load")
        ceiling = a2l_ceiling or self.max_bus_load  # None: reported, never refused
        source = "A2L MAX_BUS_LOAD" if a2l_ceiling else "max_bus_load"
        return {
            "events": events,
            "total_pct": total,
            "bitrate": rates[0] if rates else None,
            "bitrate_source": ("interface" if self.link.get("bitrate") else "A2L")
            if rates
            else None,
            "ceiling_pct": ceiling,
            "ceiling_source": source if ceiling is not None else None,
        }

    def _start_daq(self) -> None:
        master, receiver = self._master, self._receiver
        layout, groups, lists = self._layout()
        if not groups:
            self._receiver = None  # every DAQ signal skipped
            return
        receiver.daq_lists, receiver.is_predefined = lists, [False] * len(lists)
        self._daq_groups = groups
        try:
            receiver.one_ext_per_odt = layout.one_ext_per_odt
            with self._lock:
                receiver.setup()
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
        self._decoders = [daq.decoder(g, p, little) for g, p in zip(groups, positions, strict=True)]
        receiver.arm(layout, little, receiver.first_pids())
        self._clock = None
        receive = "adapter" if self.transport == DemoTransport.CAN else "host"
        self.timestamps = {"source": receive}
        if self.timestamp_mode == "auto" and layout.timestamps:
            self._clock = EcuClock(layout.ts_size, layout.tick_ns)
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
                    self._poll(self.events[event])
                    rate = self.events[event].group.rate_ms / 1000
                    polls[event] = max(due + rate, time.monotonic())
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
        row: dict[str, Any] = {}
        for s in ev.group.signals:
            try:
                row[s.field] = self._read_signal(s)
            except (XcpResponseError, ValueError) as e:
                self._warn_once(f"poll:{s.name}", "[%s] cannot read %s: %s", self.name, s.name, e)
        if row:
            t = time.time_ns()
            self._source.log_many([(t, self._paths[ev.group.event], row)])
            ev.observe(t)
            ev.last_row = time.monotonic()

    def _check(self, now: float) -> None:
        """Once a second: rates, watchdog, rows against the cycle, liveness, counters."""
        for ev in self.events.values():
            ev.update_rate(now)
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
            self.counters.update(
                incomplete_rows=receiver.incomplete,
                rejected_packets=receiver.rejected,
                malformed_datagrams=getattr(framer, "malformed", 0),
                queue_overflow=receiver.overflow,
            )
            if not receiver.can:
                self.counters["lost_packets"] = receiver.lost
                if receiver.lost > self._logged_lost:
                    logger.warning(
                        "[%s] %d packets lost (%d in all)",
                        self.name,
                        receiver.lost - self._logged_lost,
                        receiver.lost,
                    )
                    self._logged_lost = receiver.lost
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
        if getattr(master.transport, "use_tcp", False) and master.transport.status == 0:
            raise SessionLost("the ECU closed the connection")

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
        missing = ev.missing_rows
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
        if master is not None:
            transport = master.transport
            transport.abort.clear()
            transport.timeout = int(STOP_TIMEOUT * 1e9)
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
        value = self._read_signal(s)
        result = {"name": name, "value": value, "unit": s.unit, "datatype": s.datatype}
        if s.table is not None:
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
                layout, groups, _ = self._layout()
            except Refused as e:
                result.update(fits=False, reason=str(e))
            else:
                result["skipped"].update(self.daq_skipped)
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
                }
                for name, ev in self.events.items()
            },
            "watchdog": "stalled"
            if self._running and any(ev.silent(now) for ev in self.events.values())
            else "ok",
            **self.counters,
            "bus_load": dict(self.bus_load),
            "timestamps": dict(self.timestamps),
            "a2l_warnings": list(self.a2l_warnings),
            "unknown": list(plan.unknown) if plan else [],
            "skipped": {**plan.skipped, **self.daq_skipped} if plan else {},
            "polled_no_default_event": dict(plan.polled_no_default_event) if plan else {},
        }


def _describe(e: Exception) -> str:
    if isinstance(e, XcpTimeoutError):
        return "no response from the ECU"
    if isinstance(e, XcpResponseError):
        return f"the ECU refused a command: {e}"
    return f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
