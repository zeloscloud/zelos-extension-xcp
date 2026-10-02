"""Demo XCP slave: a measurement-only ECU in pure Python.

One thread serves commands and sends DAQ, over XCP on CAN or XCP on
Ethernet (`transport.py`). Ethernet runs on the standard library alone;
CAN needs `zelos_can` (the default virtual bus) or python-can.

Implemented: CONNECT, DISCONNECT, GET_STATUS, SYNCH, GET_COMM_MODE_INFO,
GET_ID (0-5; 4 uploads the A2L), SET_MTA, UPLOAD, SHORT_UPLOAD, dynamic
DAQ (FREE_DAQ, ALLOC_*, SET_DAQ_PTR, WRITE_DAQ, SET/GET_DAQ_LIST_MODE,
START_STOP_DAQ_LIST, START_STOP_SYNCH), GET_DAQ_CLOCK and the
GET_DAQ_*_INFO commands. DAQ uses absolute ODT PIDs; the timestamp, when
enabled, is fixed and sits in the first ODT of each list.

Measurement only: anything that writes memory, switches pages, stores,
programs, unlocks, or asks for a STIM list is answered with an error and
counted in `stats["refused"]`, never executed.

One master at a time. UDP: a CONNECT from another address ends the
current session and starts a new one (the old master is not told);
other commands from a non-owner are ignored. TCP: one connection is
served; a second one stays in the listen backlog, unanswered, until
the first closes. CAN has no sender address: every CONNECT on the
master id starts a new session.
"""

from __future__ import annotations

import collections
import logging
import struct
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from zelos_extension_xcp.demo import model
from zelos_extension_xcp.demo.transport import CanTransport, EthTransport

logger = logging.getLogger(__name__)

A2L_PATH = Path(__file__).with_name("demo.a2l")

# Commands
CONNECT, DISCONNECT, GET_STATUS, SYNCH, GET_COMM_MODE_INFO, GET_ID = (
    0xFF, 0xFE, 0xFD, 0xFC, 0xFB, 0xFA,
)  # fmt: skip
SET_MTA, UPLOAD, SHORT_UPLOAD = 0xF6, 0xF5, 0xF4
SET_DAQ_PTR, WRITE_DAQ, SET_DAQ_LIST_MODE, GET_DAQ_LIST_MODE = 0xE2, 0xE1, 0xE0, 0xDF
START_STOP_DAQ_LIST, START_STOP_SYNCH, GET_DAQ_CLOCK = 0xDE, 0xDD, 0xDC
GET_DAQ_PROCESSOR_INFO, GET_DAQ_RESOLUTION_INFO, GET_DAQ_EVENT_INFO = 0xDA, 0xD9, 0xD7
FREE_DAQ, ALLOC_DAQ, ALLOC_ODT, ALLOC_ODT_ENTRY = 0xD6, 0xD5, 0xD4, 0xD3

#: Write, page, store, flash, unlock and user commands: refused.
REFUSED = frozenset(
    {
        0xF9,  # SET_REQUEST
        0xF8,  # GET_SEED
        0xF7,  # UNLOCK
        0xF1,  # USER_CMD
        0xF0,  # DOWNLOAD
        0xEF,  # DOWNLOAD_NEXT
        0xEE,  # DOWNLOAD_MAX
        0xED,  # SHORT_DOWNLOAD
        0xEC,  # MODIFY_BITS
        0xEB,  # SET_CAL_PAGE
        0xE4,  # COPY_CAL_PAGE
        *range(0xC8, 0xD3),  # PROGRAM_*
    }
)

# Errors
ERR_CMD_SYNCH = 0x00
ERR_CMD_BUSY = 0x10
ERR_DAQ_ACTIVE = 0x11
ERR_CMD_UNKNOWN = 0x20
ERR_CMD_SYNTAX = 0x21
ERR_OUT_OF_RANGE = 0x22
ERR_ACCESS_DENIED = 0x24
ERR_ACCESS_LOCKED = 0x25
ERR_MODE_NOT_VALID = 0x27
ERR_SEQUENCE = 0x29
ERR_DAQ_CONFIG = 0x2A
ERR_MEMORY_OVERFLOW = 0x30

EV_CMD_PENDING = 0x05

#: Resources in GET_STATUS's protection byte, and the commands each one guards here.
RESOURCES = {"calpag": 0x01, "daq": 0x04}
GUARDED = {"calpag": frozenset({0xF5, 0xF4}), "daq": frozenset(range(0xD3, 0xE3))}

MAX_PID = 0xFB  # absolute PIDs 0x00..0xFB carry DAQ
MODE_TIMESTAMP = 0x10
MODE_STIM = 0x02

# Request lengths, PID included
MIN_LEN = {
    CONNECT: 2, GET_ID: 2, SET_MTA: 8, UPLOAD: 2, SHORT_UPLOAD: 8, SET_DAQ_PTR: 6,
    WRITE_DAQ: 8, SET_DAQ_LIST_MODE: 8, GET_DAQ_LIST_MODE: 4, START_STOP_DAQ_LIST: 4,
    START_STOP_SYNCH: 2, GET_DAQ_EVENT_INFO: 4, ALLOC_DAQ: 4, ALLOC_ODT: 5,
    ALLOC_ODT_ENTRY: 6,
}  # fmt: skip


class XcpError(Exception):
    """Answer the current command with this error code."""

    def __init__(self, code: int):
        super().__init__(code)
        self.code = code


@dataclass
class DaqList:
    odts: list[list[tuple[int, int, int] | None]] = field(default_factory=list)
    event: int | None = None
    mode: int | None = None
    first_pid: int = 0
    selected: bool = False
    running: bool = False


class DemoEcu:
    """A measurement-only XCP slave on CAN (default), UDP or TCP.

    Args:
        host: Ethernet: bind address.
        port: Ethernet: bind port, 0 for an ephemeral one (see `port` after `start()`).
        transport: "can", "udp" or "tcp".
        channel: CAN: channel, of the `zelos_can.VirtualBus` or of `interface`.
        interface: CAN: python-can interface, e.g. "socketcan". None: the
            in-process `zelos_can.VirtualBus`.
        bitrate: CAN: nominal bitrate for `interface`, when it takes one.
        can_id_master: CAN: master-to-slave id.
        can_id_slave: CAN: slave-to-master id.
        can_extended: CAN: 29-bit ids.
        can_fd: CAN: CAN FD frames, MAX_CTO and MAX_DTO 64 instead of 8.
        timestamp_size: DAQ timestamp bytes, 1, 2 or 4; 0 sends none.
        timestamp_unit_ns: Timestamp tick, a power of ten from 1 ns to 1 s.
        max_cto, max_dto: Packet limits reported on CONNECT. Default: the transport's.
        max_daq: DAQ lists that ALLOC_DAQ may allocate.
        max_odt: ODTs per DAQ list.
        max_odt_entries: Entries per ODT.
        address_extension: DAQ key byte address extension rule: 0 any per
            entry, 1 one per ODT, 3 one per DAQ list. With 1 or 3 the ECU
            reads every entry with the extension of the ODT's (or list's)
            first entry, as such a slave does.
        eth_align: Ethernet: fill each packet to a multiple of this many bytes.
        eth_pack: UDP: packets per datagram, at most; a datagram also goes
            out at the end of each serve loop pass.
        can_padding: Classic CAN: fill byte padding every frame to DLC 8;
            None sends DLC = length.
        overload: How `overload()` is signalled: "msb" (PID bit 7 of the
            next DAQ packet; PIDs then end at 0x7F), "event" (EV_DAQ_OVERLOAD)
            or "none".
        block_mode: Slave block mode: an UPLOAD of up to 255 bytes is answered in
            as many response packets as it takes.
        can_max_dlc_required: Classic CAN: command frames shorter than DLC 8 are
            ignored, as by a slave with MAX_DLC_REQUIRED.
        protected: Resources locked by seed and key, of "calpag" (uploads) and
            "daq": flagged in GET_STATUS, their commands answered ERR_ACCESS_LOCKED.
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 0,
        transport: str = "can",
        *,
        channel: str = "xcp-demo",
        interface: str | None = None,
        bitrate: int | None = None,
        can_id_master: int = model.CAN_ID_MASTER,
        can_id_slave: int = model.CAN_ID_SLAVE,
        can_extended: bool = False,
        can_fd: bool = False,
        timestamp_size: int = 4,
        timestamp_unit_ns: int = 1000,
        max_cto: int | None = None,
        max_dto: int | None = None,
        max_daq: int = 16,
        max_odt: int = 64,
        max_odt_entries: int = 255,
        address_extension: int = 0,
        eth_align: int = 1,
        eth_pack: int = 1,
        can_padding: int | None = None,
        overload: str = "none",
        block_mode: bool = True,
        can_max_dlc_required: bool = False,
        protected: tuple[str, ...] = (),
    ):
        if transport == "can":
            link = CanTransport(
                channel,
                can_id_master,
                can_id_slave,
                can_extended,
                can_fd,
                interface,
                bitrate,
                can_padding,
                can_max_dlc_required,
            )
        elif transport in ("udp", "tcp"):
            link = EthTransport(host, port, transport == "tcp", eth_align, eth_pack)
        else:
            raise ValueError(f"transport must be udp, tcp or can, not {transport!r}")
        max_cto = max_cto or link.max_cto
        max_dto = max_dto or link.max_dto
        if overload not in ("msb", "event", "none"):
            raise ValueError("overload must be msb, event or none")
        if address_extension not in (0, 1, 3):
            raise ValueError("address_extension must be 0, 1 or 3")
        if not set(protected) <= set(RESOURCES):
            raise ValueError(f"protected must name {', '.join(RESOURCES)}")
        if timestamp_size not in (0, 1, 2, 4):
            raise ValueError("timestamp_size must be 0, 1, 2 or 4")
        unit_code = [10**i for i in range(10)]
        if timestamp_unit_ns not in unit_code:
            raise ValueError("timestamp_unit_ns must be a power of ten from 1 to 10**9")
        if not 8 <= max_cto <= link.max_cto or not 8 <= max_dto <= link.dto_limit:
            raise ValueError(f"max_cto must be 8..{link.max_cto}, max_dto 8..{link.dto_limit}")
        self.host = host
        self.port = port
        self.transport = transport
        self._link = link
        self.a2l_path = A2L_PATH
        self.epk = model.EPK
        self._ts_size = timestamp_size
        self._ts_unit = timestamp_unit_ns
        self._ts_code = unit_code.index(timestamp_unit_ns)
        self._max_cto = max_cto
        self._max_dto = max_dto
        self._max_daq = max_daq
        self._max_odt = max_odt
        self._max_entries = max_odt_entries
        self._max_entry_size = min(255, max_dto - 1)
        self._ae = address_extension
        self._overload = overload
        self._overload_due = False
        self._block = block_mode
        self._protected = tuple(protected)
        self._errors: list[list] = []  # [commands left, command or None, error code]
        self._pending_every: float | None = None
        self._pending_at = 0.0
        # Held from the mute check to the send, so a hook applies from the next packet on
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._t0 = 0
        self._stats = {
            "commands": collections.Counter(),
            "refused": collections.Counter(),
            "dto_sent": 0,
            "sessions": 0,
        }
        self._drop = self._truncate = 0
        self._drop_from: int | None = None
        self._late = (0, 0.0, None)  # responses left to delay, seconds, command or None
        self._outbox: collections.deque = collections.deque()  # (due, packet) in order
        self._swap = False
        self._held: bytes | None = None
        self._stall_until = 0.0
        self._silent = False
        self._reset_session()
        self._owner = None

    # Lifecycle

    def start(self) -> DemoEcu:
        """Bind and serve. Returns once listening."""
        self._link.open()
        self.port = self._link.port
        self._t0 = time.monotonic_ns()
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="xcp-demo-ecu", daemon=True)
        self._thread.start()
        logger.info("Demo ECU on %s, EPK %s", self._link, self.epk)
        return self

    def stop(self) -> None:
        """Stop serving and close every socket. Returns within 1 s."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=0.9)
            self._thread = None
        self._link.close()
        logger.info("Demo ECU stopped")

    def __enter__(self) -> DemoEcu:
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()

    # Model and counters

    def expected(self, name: str, ecu_time_s: float) -> float | int:
        """Value of measurement `name` at ECU time `ecu_time_s`, as the ECU stores it.

        Datatype decoded and BIT_MASK applied, before the A2L conversion.
        Array elements as `name[i]`. ECU time counts from `start()`; a DAQ
        timestamp of `n` ticks is `n * timestamp_unit_ns / 1e9` s, unwrapped.
        """
        return model.expected(name, round(ecu_time_s * 1e9))

    def physical(self, name: str, ecu_time_s: float) -> float | int:
        """What a correct master writes to the trace for `name` at `ecu_time_s`.

        LINEAR and RAT_FUNC: a float. No conversion, TAB_VERB (see `labels`)
        and BIT_MASK flags: the integer of `expected()`.
        """
        return model.physical(name, round(ecu_time_s * 1e9))

    def labels(self, name: str) -> dict[int, str]:
        """The value table of a TAB_VERB measurement, else {}."""
        conv = model.conversion(name)
        return dict(conv.table) if conv and conv.kind == "TAB_VERB" else {}

    def unit(self, name: str) -> str:
        """The measurement's A2L unit, '' when it has none."""
        return model.unit(name)

    @property
    def stats(self) -> dict:
        """Snapshot: commands and refused by command code, DAQ packets sent, sessions."""
        with self._lock:
            return {
                "commands": dict(self._stats["commands"]),
                "refused": dict(self._stats["refused"]),
                "dto_sent": self._stats["dto_sent"],
                "sessions": self._stats["sessions"],
            }

    # Fault injection

    def drop_dto(self, n: int = 1, from_odt: int | None = None) -> None:
        """Silently drop the next `n` DAQ packets. Their CTR values are skipped.

        `from_odt`: start at the next packet of that ODT of a list, e.g. the
        last, so the loss spans two samples.
        """
        with self._lock:
            self._drop += n
            self._drop_from = from_odt

    def overload(self, n: int = 1, from_odt: int | None = None) -> None:
        """Drop `n` DAQ packets as a full transmit queue does (see `drop_dto`), then
        signal overload the configured way."""
        with self._lock:
            self.drop_dto(n, from_odt)
            self._overload_due = True

    def respond_late(
        self,
        seconds: float,
        n: int = 1,
        command: int | None = None,
        pending_every: float | None = None,
    ) -> None:
        """Answer the next `n` commands (of code `command` only, when given) `seconds` late.

        Responses stay in order, as from a slave that serves one command at a
        time: whatever is answered meanwhile, SYNCH included, follows them.
        `pending_every`: meanwhile send EV_CMD_PENDING at this interval.
        """
        with self._lock:
            self._late = (n, seconds, command)
            self._pending_every = pending_every

    def fail(self, n: int = 1, command: int | None = None, code: int = ERR_CMD_BUSY) -> None:
        """Answer the next `n` commands (of code `command` only) with error `code`, not
        executed. Default ERR_CMD_BUSY."""
        with self._lock:
            self._errors.append([n, command, code])

    def emit_event(self, code: int) -> None:
        """Send event packet `code` now. EV_SESSION_TERMINATED (0x07) also ends the session."""
        with self._lock:
            if not self._connected:
                return
            self._send(bytes([0xFD, code]))
            self._link.flush()
            if code == 0x07:
                self._reset_session()

    def truncate_dto(self, n: int = 1) -> None:
        """Send the next `n` DAQ packets cut to half their length, LEN matching."""
        with self._lock:
            self._truncate += n

    def swap_dto(self) -> None:
        """Send the next two DAQ packets in reverse order, CTR as generated."""
        with self._lock:
            self._swap = True

    def stall(self, seconds: float) -> None:
        """Answer nothing and send nothing for `seconds`, then resume.

        Requests that arrive meanwhile are discarded; DAQ cycles are skipped.
        """
        with self._lock:
            self._stall_until = time.monotonic() + seconds

    def go_silent(self) -> None:
        """Stop answering and sending for good. Sockets stay open."""
        with self._lock:
            self._silent = True

    # Serve loop

    def _now_ns(self) -> int:
        return time.monotonic_ns() - self._t0

    def _muted(self) -> bool:
        return self._silent or time.monotonic() < self._stall_until

    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                for pkt, addr in self._link.poll(self._wait()):
                    with self._lock:
                        if pkt is None:
                            self._hang_up()
                        elif not self._muted():
                            self._command(pkt, addr)
                with self._lock:
                    self._daq_tick()
                    if not self._muted():
                        self._release()
                    self._link.flush()
        except Exception:
            logger.exception("Demo ECU failed")

    def _wait(self) -> float:
        due = self._next_due()
        wait = 0.05 if due is None else min(max((due - self._now_ns()) / 1e9, 0.0), 0.05)
        if self._outbox:
            wait = min(wait, max(self._outbox[0][0] - time.monotonic(), 0.0))
            if self._pending_every:
                wait = min(wait, self._pending_every)
        return wait

    def _release(self) -> None:
        """Send the delayed responses that are due; EV_CMD_PENDING while one waits."""
        now = time.monotonic()
        while self._outbox and self._outbox[0][0] <= now:
            self._send(self._outbox.popleft()[1])
        every = self._pending_every
        if self._outbox and every and now - self._pending_at >= every:
            self._pending_at = now
            self._send(bytes([0xFD, EV_CMD_PENDING]))

    def _respond(self, pid: int, res: bytes | list[bytes]) -> None:
        """Answer one command: a packet, or a block of them (slave block mode)."""
        n, seconds, command = self._late
        late = n > 0 and command in (None, pid)
        if late:
            self._late = (n - 1, seconds, command)
            self._pending_at = time.monotonic()
        for packet in res if isinstance(res, list) else [res]:
            if not late and not self._outbox:
                self._send(packet)
                continue
            due = time.monotonic() + (seconds if late else 0.0)
            if self._outbox:
                due = max(due, self._outbox[-1][0])
            self._outbox.append((due, packet))

    def _hang_up(self) -> None:
        if self._connected:
            logger.info("Master closed the connection")
        self._reset_session()

    def _send(self, pkt: bytes) -> None:
        self._put(self._link.frame(pkt))

    def _put(self, frame: bytes) -> None:
        if not self._link.send(frame, self._owner):
            self._hang_up()

    # Commands

    def _command(self, req: bytes, addr) -> None:
        pid = req[0]
        with self._lock:
            self._stats["commands"][pid] += 1
        if pid == CONNECT:
            if self._connected and addr != self._owner:
                logger.info("CONNECT from %s takes the session", addr)
            self._reset_session()
            self._connected, self._owner = True, addr
            with self._lock:
                self._stats["sessions"] += 1
        elif not self._connected or addr != self._owner:
            return  # a disconnected slave answers only CONNECT
        try:
            if len(req) < MIN_LEN.get(pid, 1):
                raise XcpError(ERR_CMD_SYNTAX)
            if pid in REFUSED:
                self._refuse(pid)
            fault = next((f for f in self._errors if f[0] and f[1] in (None, pid)), None)
            if fault is not None:
                fault[0] -= 1
                raise XcpError(fault[2])
            if any(pid in GUARDED[r] for r in self._protected):
                raise XcpError(ERR_ACCESS_LOCKED)
            handler = self._handlers().get(pid)
            if handler is None:
                raise XcpError(ERR_CMD_UNKNOWN)
            res = handler(req)
        except XcpError as e:
            res = bytes([0xFE, e.code])
        if res is not None:
            self._respond(pid, res)
        if pid == DISCONNECT:
            self._reset_session()

    def _refuse(self, pid: int, code: int = ERR_CMD_UNKNOWN) -> None:
        with self._lock:
            self._stats["refused"][pid] += 1
        logger.warning("Refused command 0x%02X: measurement only", pid)
        raise XcpError(code)

    def _handlers(self) -> dict:
        return {
            CONNECT: self._connect,
            DISCONNECT: lambda r: b"\xff",
            GET_STATUS: self._get_status,
            SYNCH: lambda r: bytes([0xFE, ERR_CMD_SYNCH]),
            GET_COMM_MODE_INFO: lambda r: bytes([0xFF, 0, 0, 0, 0, 0, 0, 0x10]),
            GET_ID: self._get_id,
            SET_MTA: self._set_mta,
            UPLOAD: self._upload,
            SHORT_UPLOAD: self._short_upload,
            GET_DAQ_CLOCK: self._get_daq_clock,
            GET_DAQ_PROCESSOR_INFO: self._get_daq_processor_info,
            GET_DAQ_RESOLUTION_INFO: self._get_daq_resolution_info,
            GET_DAQ_EVENT_INFO: self._get_daq_event_info,
            FREE_DAQ: self._free_daq,
            ALLOC_DAQ: self._alloc_daq,
            ALLOC_ODT: self._alloc_odt,
            ALLOC_ODT_ENTRY: self._alloc_odt_entry,
            SET_DAQ_PTR: self._set_daq_ptr,
            WRITE_DAQ: self._write_daq,
            SET_DAQ_LIST_MODE: self._set_daq_list_mode,
            GET_DAQ_LIST_MODE: self._get_daq_list_mode,
            START_STOP_DAQ_LIST: self._start_stop_daq_list,
            START_STOP_SYNCH: self._start_stop_synch,
        }

    def _reset_session(self) -> None:
        self._connected = False
        self._mta = (0, 0)
        self._blob: bytes | None = None
        self._free()

    def _connect(self, req: bytes) -> bytes:
        # RESOURCE: DAQ only. COMM_MODE_BASIC: little endian, byte granularity, slave
        # block mode when on, optional info.
        mode = 0x80 | (0x40 if self._block else 0)
        return struct.pack("<BBBBHBB", 0xFF, 0x04, mode, self._max_cto, self._max_dto, 1, 1)

    def _get_status(self, req: bytes) -> bytes:
        running = any(d.running for d in self._daq)
        protection = sum(RESOURCES[r] for r in self._protected)
        return struct.pack("<BBBBH", 0xFF, 0x40 if running else 0, protection, 0, 0)

    def _get_id(self, req: bytes) -> bytes:
        # 0 ASCII, 1 A2L name, 2 A2L file, 3 URL, 4 A2L content, 5 EPK
        kind = req[1]
        if kind > 5:
            raise XcpError(ERR_OUT_OF_RANGE)
        if kind == 4:
            data = self.a2l_path.read_bytes()
        else:
            stem, name = self.a2l_path.stem, self.a2l_path.name
            data = {0: stem, 1: stem, 2: name, 3: "", 5: self.epk}[kind].encode()
        head = struct.pack("<BBHI", 0xFF, 1, 0, len(data))
        if kind != 4 and len(head) + len(data) <= self._max_cto:
            return head + data
        self._blob, self._mta = data, (0, 0)
        return struct.pack("<BBHI", 0xFF, 0, 0, len(data))

    # Memory

    def _read(self, ext: int, addr: int, n: int, most: int | None = None) -> bytes:
        if n > (most or self._max_cto - 1):
            raise XcpError(ERR_OUT_OF_RANGE)
        data = self._peek(ext, addr, n, self._now_ns())
        if data is None:
            raise XcpError(ERR_ACCESS_DENIED)
        return data

    @staticmethod
    def _region(ext: int, addr: int, n: int):
        if (
            ext == 0
            and addr >= model.EPK_ADDRESS
            and addr + n <= model.EPK_ADDRESS + len(model.EPK)
        ):
            return "epk"
        for b in model.BLOCKS:
            if b.ext == ext and b.base <= addr and addr + n <= b.base + b.size:
                return b
        return None

    def _peek(self, ext: int, addr: int, n: int, t_ns: int, images=None) -> bytes | None:
        region = self._region(ext, addr, n)
        if region is None:
            return None
        if region == "epk":
            off = addr - model.EPK_ADDRESS
            return model.EPK.encode()[off : off + n]
        images = {} if images is None else images
        if id(region) not in images:
            images[id(region)] = model.image(region, t_ns)
        off = addr - region.base
        return images[id(region)][off : off + n]

    def _set_mta(self, req: bytes) -> bytes:
        ext, addr = struct.unpack_from("<BI", req, 3)
        self._mta, self._blob = (ext, addr), None
        return b"\xff"

    def _upload(self, req: bytes) -> bytes | list[bytes]:
        n = req[1]
        ext, addr = self._mta
        most = 255 if self._block else self._max_cto - 1
        if self._blob is not None:
            if n > most:
                raise XcpError(ERR_OUT_OF_RANGE)
            data = self._blob[addr : addr + n]
            if len(data) < n:
                raise XcpError(ERR_ACCESS_DENIED)
        else:
            data = self._read(ext, addr, n, most)
        self._mta = (ext, addr + n)
        step = self._max_cto - 1
        return [b"\xff" + data[i : i + step] for i in range(0, n, step)] or b"\xff"

    def _short_upload(self, req: bytes) -> bytes:
        n = req[1]
        ext, addr = struct.unpack_from("<BI", req, 3)
        data = self._read(ext, addr, n)
        self._mta, self._blob = (ext, addr + n), None
        return b"\xff" + data

    # DAQ info

    def _get_daq_clock(self, req: bytes) -> bytes:
        ticks = self._now_ns() // self._ts_unit & 0xFFFF_FFFF
        return struct.pack("<BBBBI", 0xFF, 0, 0, 1, ticks)

    def _get_daq_processor_info(self, req: bytes) -> bytes:
        props = 0x01 | (0x10 if self._ts_size else 0)  # dynamic, timestamps
        props |= {"msb": 0x40, "event": 0x80, "none": 0}[self._overload]
        key = self._ae << 4  # absolute ODT numbers, address extension rule
        return struct.pack("<BBHHBB", 0xFF, props, self._max_daq, len(model.EVENTS), 0, key)

    def _get_daq_resolution_info(self, req: bytes) -> bytes:
        mode = self._ts_size | 0x08 | self._ts_code << 4 if self._ts_size else 0
        ticks = 1 if self._ts_size else 0
        return struct.pack("<BBBBBBH", 0xFF, 1, self._max_entry_size, 1, 0, mode, ticks)

    def _get_daq_event_info(self, req: bytes) -> bytes:
        ev = struct.unpack_from("<H", req, 2)[0]
        if ev >= len(model.EVENTS):
            raise XcpError(ERR_OUT_OF_RANGE)
        name, ms = model.EVENTS[ev]
        self._blob, self._mta = name.encode(), (0, 0)
        # DAQ, consistency EVENT; cycle in 1 ms units (code 6)
        return struct.pack("<BBBBBBB", 0xFF, 0x84, 0xFF, len(name), ms, 6, 0)

    # DAQ configuration

    def _free(self) -> None:
        self._daq: list[DaqList] = []
        self._alloc = 0  # 0 free, 1 lists, 2 ODTs, 3 entries
        self._ptr: tuple[int, int, int] | None = None
        self._next_k: dict[int, int] = {}
        self._held = None

    def _free_daq(self, req: bytes) -> bytes:
        self._free()
        return b"\xff"

    def _list(self, n: int) -> DaqList:
        if n >= len(self._daq):
            raise XcpError(ERR_OUT_OF_RANGE)
        return self._daq[n]

    def _alloc_daq(self, req: bytes) -> bytes:
        count = struct.unpack_from("<H", req, 2)[0]
        if self._alloc != 0:
            raise XcpError(ERR_SEQUENCE)
        if count > self._max_daq:
            raise XcpError(ERR_MEMORY_OVERFLOW)
        self._daq = [DaqList() for _ in range(count)]
        self._alloc = 1
        return b"\xff"

    def _alloc_odt(self, req: bytes) -> bytes:
        n, count = struct.unpack_from("<HB", req, 2)
        if self._alloc not in (1, 2):
            raise XcpError(ERR_SEQUENCE)
        d = self._list(n)
        if d.odts:
            raise XcpError(ERR_SEQUENCE)
        total = sum(len(x.odts) for x in self._daq) + count
        pids = 0x80 if self._overload == "msb" else MAX_PID + 1
        if count > self._max_odt or total > pids:
            raise XcpError(ERR_MEMORY_OVERFLOW)
        d.odts = [[] for _ in range(count)]
        self._alloc = 2
        pid = 0
        for x in self._daq:
            x.first_pid, pid = pid, pid + len(x.odts)
        return b"\xff"

    def _alloc_odt_entry(self, req: bytes) -> bytes:
        n, odt, count = struct.unpack_from("<HBB", req, 2)
        if self._alloc not in (2, 3):
            raise XcpError(ERR_SEQUENCE)
        d = self._list(n)
        if odt >= len(d.odts):
            raise XcpError(ERR_OUT_OF_RANGE)
        if d.odts[odt]:
            raise XcpError(ERR_SEQUENCE)
        if count > self._max_entries:
            raise XcpError(ERR_MEMORY_OVERFLOW)
        d.odts[odt] = [None] * count
        self._alloc = 3
        return b"\xff"

    def _set_daq_ptr(self, req: bytes) -> bytes:
        n, odt, entry = struct.unpack_from("<HBB", req, 2)
        d = self._list(n)
        if odt >= len(d.odts) or entry >= len(d.odts[odt]):
            raise XcpError(ERR_OUT_OF_RANGE)
        if d.running:
            raise XcpError(ERR_DAQ_ACTIVE)
        self._ptr = (n, odt, entry)
        return b"\xff"

    def _write_daq(self, req: bytes) -> bytes:
        bit, size, ext, addr = struct.unpack_from("<BBBI", req, 1)
        if self._ptr is None:
            raise XcpError(ERR_SEQUENCE)
        n, odt, entry = self._ptr
        d = self._daq[n]
        if d.running:
            raise XcpError(ERR_DAQ_ACTIVE)
        if entry >= len(d.odts[odt]) or bit != 0xFF or not 0 < size <= self._max_entry_size:
            raise XcpError(ERR_OUT_OF_RANGE)
        if self._region(ext, addr, size) is None:
            raise XcpError(ERR_ACCESS_DENIED)
        d.odts[odt][entry] = (ext, addr, size)
        self._ptr = (n, odt, entry + 1)
        return b"\xff"

    def _set_daq_list_mode(self, req: bytes) -> bytes:
        mode, n, ev, prescaler, _ = struct.unpack_from("<BHHBB", req, 1)
        if mode & MODE_STIM:
            self._refuse(SET_DAQ_LIST_MODE, ERR_MODE_NOT_VALID)
        d = self._list(n)
        if mode != (MODE_TIMESTAMP if self._ts_size else 0):
            raise XcpError(ERR_MODE_NOT_VALID)
        if ev >= len(model.EVENTS) or prescaler != 1:
            raise XcpError(ERR_OUT_OF_RANGE)
        if d.running:
            raise XcpError(ERR_DAQ_ACTIVE)
        d.mode, d.event = mode, ev
        return b"\xff"

    def _get_daq_list_mode(self, req: bytes) -> bytes:
        d = self._list(struct.unpack_from("<H", req, 2)[0])
        mode = (d.mode or 0) | d.selected | d.running << 6
        ev = 0xFFFF if d.event is None else d.event
        return struct.pack("<BBHHBB", 0xFF, mode, 0, ev, 1, 0)

    def _check(self, d: DaqList) -> None:
        """A list must be complete and each ODT must fit one DTO to start."""
        if d.event is None or not d.odts:
            raise XcpError(ERR_DAQ_CONFIG)
        for i, odt in enumerate(d.odts):
            if any(e is None for e in odt):
                raise XcpError(ERR_DAQ_CONFIG)
            size = 1 + (self._ts_size if i == 0 else 0) + sum(e[2] for e in odt)
            if size > self._max_dto:
                raise XcpError(ERR_DAQ_CONFIG)

    def _start_stop_daq_list(self, req: bytes) -> bytes:
        mode, n = struct.unpack_from("<BH", req, 1)
        d = self._list(n)
        if mode == 0:
            d.running = d.selected = False
        elif mode in (1, 2):
            self._check(d)
            if mode == 1:
                self._start(d)
            else:
                d.selected = True
        else:
            raise XcpError(ERR_OUT_OF_RANGE)
        return bytes([0xFF, d.first_pid])

    def _start_stop_synch(self, req: bytes) -> bytes:
        mode = req[1]
        if mode > 2:
            raise XcpError(ERR_OUT_OF_RANGE)
        for d in self._daq:
            if mode == 0:
                d.running = False
            elif d.selected:
                if mode == 1:
                    self._start(d)
                else:
                    d.running = False
            d.selected = False
        return b"\xff"

    def _start(self, d: DaqList) -> None:
        d.running = True
        if d.event not in self._next_k:
            # first sample at the event's next cycle
            self._next_k[d.event] = self._now_ns() // model.CYCLE_NS[d.event] + 1

    # DAQ sampling

    def _next_due(self) -> int | None:
        due = [
            k * model.CYCLE_NS[ev]
            for ev, k in self._next_k.items()
            if any(d.running and d.event == ev for d in self._daq)
        ]
        return min(due) if due else None

    def _daq_tick(self) -> None:
        now = self._now_ns()
        while True:
            due = [
                (k * model.CYCLE_NS[ev], ev)
                for ev, k in self._next_k.items()
                if any(d.running and d.event == ev for d in self._daq)
            ]
            if not due:
                return
            t, ev = min(due)
            if t > now:
                return
            self._next_k[ev] += 1
            if self._muted():
                continue
            self._sample(ev, t)

    def _sample(self, ev: int, t_ns: int) -> None:
        images: dict = {}
        ts = struct.pack("<Q", t_ns // self._ts_unit)[: self._ts_size]
        for d in self._daq:
            if not self._connected:
                return  # the master hung up mid-cycle
            if not (d.running and d.event == ev):
                continue
            for i, odt in enumerate(d.odts):
                data = b"".join(self._entry(d, odt, e, t_ns, images) for e in odt)
                self._send_dto(bytes([d.first_pid + i]) + (ts if i == 0 else b"") + data, i)

    def _entry(self, d: DaqList, odt: list, e: tuple, t_ns: int, images: dict) -> bytes:
        """An ODT entry's bytes, read with the extension the key byte rule gives it."""
        ext, addr, size = e
        if self._ae:
            ext = (odt if self._ae == 1 else d.odts[0])[0][0]
        return self._peek(ext, addr, size, t_ns, images) or bytes(size)

    def _send_dto(self, pkt: bytes, odt: int = 0) -> None:
        with self._lock:
            drop = self._drop > 0 and self._drop_from in (None, odt)
            if drop:
                self._drop, self._drop_from = self._drop - 1, None
            cut = not drop and self._truncate > 0
            self._truncate -= cut
            swap, self._swap = self._swap, False
            signal = not drop and not self._drop and self._overload_due
            if signal:
                self._overload_due = False
        if signal and self._overload == "msb":
            pkt = bytes([pkt[0] | 0x80]) + pkt[1:]
        elif signal and self._overload == "event":
            self._send(bytes([0xFD, 0x06]))
        if cut:
            pkt = pkt[: max(1, len(pkt) // 2)]
        frame = self._link.frame(pkt)
        if drop:
            return
        if swap:
            self._held = frame
            return
        self._put(frame)
        sent = 1
        if self._held is not None:
            self._put(self._held)
            self._held = None
            sent += 1
        with self._lock:
            self._stats["dto_sent"] += sent
