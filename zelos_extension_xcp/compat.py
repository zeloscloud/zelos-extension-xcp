"""Workarounds for defects in pyxcp 0.29.18, the pinned XCP stack.

Every pyxcp import in the extension goes through this module, so each patch is
in place before the library is used. A pin bump re-checks every item here.
"""

from __future__ import annotations

import logging
import struct
import sys
import threading
import time
from collections.abc import Callable
from functools import partial
from types import SimpleNamespace
from typing import Any

# `import pyxcp` replaces both global hooks (rich tracebacks with locals, rich
# pretty-printing); they are restored below.
_hooks = (sys.excepthook, sys.displayhook)

import pyxcp.daq_stim as _daq_stim  # noqa: E402
import pyxcp.transport.base as _transport_base  # noqa: E402
import pyxcp.transport.eth as _eth  # noqa: E402
from pyxcp import types  # noqa: E402
from pyxcp.config import General, Transport  # noqa: E402
from pyxcp.cpp_ext.cpp_ext import DaqList  # noqa: E402
from pyxcp.daq_stim import DAQ_TIMESTAMP_SIZE, DaqOnlinePolicy  # noqa: E402
from pyxcp.daq_stim.optimize import make_continuous_blocks  # noqa: E402
from pyxcp.daq_stim.optimize.binpacking import first_fit_decreasing  # noqa: E402
from pyxcp.master import Master  # noqa: E402
from pyxcp.transport.transport_ext import FrameCategory  # noqa: E402
from pyxcp.utils import CurrentDatetime, TimestampInfo  # noqa: E402
from traitlets.config import Config  # noqa: E402

sys.excepthook, sys.displayhook = _hooks

__all__ = [
    "DAQ_TIMESTAMP_SIZE",
    "DAQ_TIMESTAMP_UNIT_TO_NS",
    "Command",
    "DaqList",
    "DaqPolicy",
    "Event",
    "FrameCategory",
    "Master",
    "XcpResponseError",
    "XcpTimeoutError",
    "first_fit_decreasing",
    "make_continuous_blocks",
    "close",
    "make_can_master",
    "make_master",
    "pack_odts",
    "synch",
]

Command = types.Command
Event = types.Event
DAQ_TIMESTAMP_UNIT_TO_NS = types.DAQ_TIMESTAMP_UNIT_TO_NS
XcpResponseError = types.XcpResponseError
XcpTimeoutError = types.XcpTimeoutError

logger = logging.getLogger(__name__)

#: XCP on Ethernet packet header: LEN, CTR (little endian).
ETH_HEADER = struct.Struct("<HH")


class _UtcFallbackDatetime(CurrentDatetime):
    """Master creation raises on zone abbreviations pytz rejects (PST, CST, JST, IST, AEST)."""

    def __init__(self, timestamp_ns: int) -> None:
        try:
            super().__init__(timestamp_ns)
        except Exception:
            TimestampInfo.__init__(self, timestamp_ns)
            self.utc_offset = 0
            self.dst_offset = 0


_transport_base.CurrentDatetime = _UtcFallbackDatetime
_daq_stim.CurrentDatetime = _UtcFallbackDatetime

# UDP receive reads 512 bytes and truncates larger datagrams, which stalls DAQ.
_eth.Eth.MAX_DATAGRAM_SIZE = 65535


class DatagramFramer:
    """Splits each UDP datagram into XCP packets on its own.

    pyxcp's receiver carries partial packets across datagrams, so one malformed
    datagram breaks framing for everything after it and DAQ stops. Here a bad
    header drops the rest of that datagram only; the next one starts clean.
    A slave may fill each packet to a 2 or 4 byte boundary: the smallest
    alignment that frames the whole datagram is taken.
    """

    def __init__(self, deliver: Callable[[bytes, int, int, int], None]) -> None:
        self._deliver = deliver
        self.malformed = 0

    def feed_frame(self, datagram: bytes, timestamp: int) -> None:
        packets, whole = _split(datagram, 1)
        if not whole:
            for align in (2, 4):
                aligned, whole = _split(datagram, align)
                if whole:
                    packets = aligned
                    break
            else:
                self.malformed += 1
        for start, length, counter in packets:
            self._deliver(datagram[start : start + length], length, counter, timestamp)


def _split(datagram: bytes, align: int) -> tuple[list[tuple[int, int, int]], bool]:
    """(start, LEN, CTR) of each packet, fill to `align` skipped; False when a header is bad."""
    out, offset, end = [], 0, len(datagram)
    while offset < end:
        if end - offset < ETH_HEADER.size:
            return out, False
        length, counter = ETH_HEADER.unpack_from(datagram, offset)
        start = offset + ETH_HEADER.size
        if length == 0 or start + length > end:
            return out, False
        out.append((start, length, counter))
        offset = min(-(-(start + length) // align) * align, end)
    return out, True


def _bounded_get(transport: Any, start: float | None = None) -> Any:
    """`BaseTransport.get` with a hard deadline, ending early once `transport.abort` is set.

    The library restarts a command's timeout on every DAQ packet, so a lost
    response waits forever while DAQ flows; and it reads its timeout once per
    call, so a stop would wait out a long user timeout.
    """
    start = time.monotonic() if start is None else start
    with transport.resQueue_condition:
        while not transport.resQueue:
            if transport.abort.is_set():
                raise _transport_base.EmptyFrameError
            remaining = transport.timeout / 1e9 - (time.monotonic() - start)
            if remaining <= 0:
                raise _transport_base.EmptyFrameError
            transport.resQueue_condition.wait(timeout=min(remaining, 0.05))
        return transport.resQueue.popleft()


def _send_fresh(transport: Any, send: Callable[[Any], None], frame: Any) -> None:
    """Send a command with an empty response queue.

    One command is outstanding at a time, so anything queued now answers a
    command already given up on; the library would hand it to this one.
    """
    with transport.resQueue_condition:
        transport.stale_responses += len(transport.resQueue)
        transport.resQueue.clear()
    send(frame)


#: ERR_CMD_SYNCH, the slave's answer to SYNCH.
SYNCH_ANSWER = b"\xfe\x00"


def synch(master: Master) -> None:
    """SYNCH, dropping every response ahead of its answer.

    The slave answers in order, so once ERR_CMD_SYNCH arrives no response to
    an earlier command is still on its way. Raises XcpTimeoutError when the
    answer does not come within the timeout.
    """
    transport = master.transport
    with transport.command_lock:
        transport.send(transport._prepare_request(Command.SYNCH))
        start = time.monotonic()
        try:
            while _bounded_get(transport, start)[:2] != SYNCH_ANSWER:
                transport.stale_responses += 1
        except _transport_base.EmptyFrameError:
            raise XcpTimeoutError("no answer to SYNCH") from None


def _on_event(transport: Any, packet: bytes) -> None:
    """Replaces the library's event handling, which only logs; see `make_master`."""
    if len(packet) >= 2:
        transport.on_event(packet[1], packet)


def _config(layer: str, timeout: float) -> Config:
    c = Config()
    c.Transport.layer = layer
    c.Transport.timeout = timeout
    c.Transport.create_daq_timestamps = True
    c.General.max_retries = 0
    c.General.connect_retries = 0
    # Off: its retry matrix blows the stop bound, sends commands of its own and
    # is one process-wide state shared by every master.
    c.General.disable_error_handling = True
    c.General.stim_support = False
    return c


def _master(name: str, c: Config, policy: Any, interface: Any = None) -> Master:
    config = SimpleNamespace(general=General(config=c), transport=Transport(config=c))
    master = Master(name, config=config, policy=policy, transport_layer_interface=interface)
    transport = master.transport
    transport.abort = threading.Event()
    transport.get = lambda: _bounded_get(transport)
    transport.stale_responses = 0
    transport.send = partial(_send_fresh, transport, transport.send)
    transport.on_event = lambda code, packet: logger.info("XCP event 0x%02X", code)
    transport.process_event_packet = partial(_on_event, transport)
    return master


def make_master(host: str, port: int, protocol: str, timeout: float, policy: Any = None) -> Master:
    """An XCP on Ethernet master without config file, command line or retry handler.

    Commands raise on the first timeout or negative response; retries are the
    caller's, with `synch` before each. Stale responses are dropped before every
    command and counted in `transport.stale_responses`; event packets (EV) go
    to `transport.on_event(code, packet)`. `master.transport.connect()` opens
    the socket.
    """
    c = _config("ETH", timeout)
    c.Transport.Eth.host = host
    c.Transport.Eth.port = port
    c.Transport.Eth.protocol = protocol.upper()
    master = _master("eth", c, policy)
    if not master.transport.use_tcp:
        master.transport._eth_receiver = DatagramFramer(master.transport.process_response)
    return master


def make_can_master(
    bus: Any, tx_id: int, rx_id: int, fd: bool, timeout: float, policy: Any = None
) -> Master:
    """An XCP on CAN master on an open python-can `bus`; ids carry bit 31 when extended.

    `master.transport.connect()` sets the bus filters and starts the receive loop.
    """
    c = _config("CAN", timeout)
    c.Transport.Can.interface = "virtual"  # validated even when a bus is supplied; unused
    c.Transport.Can.can_id_master = tx_id
    c.Transport.Can.can_id_slave = rx_id
    c.Transport.Can.fd = fd
    master = _master("can", c, policy, bus)
    # The library restores the bus filters on close only when some were set.
    master.transport.filters_before = bus.filters
    return master


def close(master: Master) -> None:
    """Close `master`, wait for its receive thread, put back the bus filters.

    The library's CAN close signals its receive thread but does not wait for it,
    and restores the bus filters only when some were set before.
    """
    transport = master.transport
    try:
        master.close()
    finally:
        transport.listener.join(1.0)
        if hasattr(transport, "filters_before"):
            transport.can_interface.can_interface.set_filters(transport.filters_before)


_packing = threading.local()


def pack_odts(blocks: list, container: int, first: int, one_ext: bool = False) -> list:
    """pyxcp's ODT packing, the first ODT `first` bytes; with `one_ext`, one address
    extension per ODT."""
    if not one_ext:
        return first_fit_decreasing(blocks, container, first)
    bins: list = []
    for ext in sorted({b.ext for b in blocks}):
        group = [b for b in blocks if b.ext == ext]
        bins += first_fit_decreasing(group, container, container if bins else first)
    return bins


# The library packs every list in `setup`; the policy being set up picks the rule.
_daq_stim.first_fit_decreasing = lambda blocks, container, first=None: pack_odts(
    blocks, container, container if first is None else first, getattr(_packing, "one_ext", False)
)


def daq_header_size(identification_field: str) -> int:
    """Bytes of the DAQ packet identification field."""
    return _daq_stim.DAQ_ID_FIELD_SIZE[identification_field]


class DaqPolicy(DaqOnlinePolicy):
    """Online DAQ policy with the library's decoder guarded against bad packets.

    The C++ decoder reads past the end of a short packet and aborts the whole
    process, and places an unknown absolute PID on list 0. Set `id_size`,
    `byteorder`, `min_length` ({(daq, odt): bytes}) and, for absolute ODT
    numbering, `pid_map` ({pid: (daq, odt)}) before DAQ starts: a packet that
    cannot be placed or is too short is counted in `rejected` and never
    reaches the decoder. Set `one_ext_per_odt` before `setup` when the slave
    takes one address extension per ODT, and `overload_msb` when it flags
    overload in the PID's MSB: the flag is stripped before lookup and decode,
    and reported to `on_overload`.
    """

    def __init__(self, daq_lists: list[DaqList]) -> None:
        super().__init__(daq_lists, logger=logging.getLogger("pyxcp.daq"))
        # Left unset by the library when a logger is passed; setup() needs it.
        self.pid_off = False
        self.id_size = 0
        self.byteorder = "little"
        self.min_length: dict[tuple[int, int], int] = {}
        self.pid_map: dict[int, tuple[int, int]] = {}
        self.rejected = 0
        self.one_ext_per_odt = False
        self.overload_msb = False

    def setup(self, *args: Any, **kwargs: Any) -> None:
        _packing.one_ext = self.one_ext_per_odt
        try:
            super().setup(*args, **kwargs)
        finally:
            _packing.one_ext = False

    def locate(self, payload: bytes) -> tuple[int, int] | None:
        """(daq list, odt) of a DAQ packet the decoder can read in full, else None."""
        size, id_size = len(payload), self.id_size
        if size < max(id_size, 1):
            return None
        if id_size == 1:
            where = self.pid_map.get(payload[0])
        elif id_size == 2:
            where = (payload[1], payload[0])
        elif id_size in (3, 4):
            where = (int.from_bytes(payload[id_size - 2 : id_size], self.byteorder), payload[0])
        else:
            return None
        if where is None or size < self.min_length.get(where, 1 << 30):
            return None
        return where

    def feed(self, cat: int, counter: int, timestamp: int, payload: bytes) -> None:
        self.on_frame(cat, counter)
        if cat != FrameCategory.DAQ:
            return
        flagged = self.overload_msb and bool(payload) and payload[0] & 0x80
        if flagged:
            payload = bytes([payload[0] & 0x7F]) + payload[1:]
        where = self.locate(payload)
        if where is None:
            self.rejected += 1
            return
        if flagged:
            self.on_overload(where)
        self.on_packet(counter, timestamp, payload, where)
        super().feed(cat, counter, timestamp, payload)

    def on_packet(self, counter: int, timestamp: int, payload: bytes, where: tuple[int, int]):
        """A DAQ packet, checked, about to be decoded."""

    def on_frame(self, cat: int, counter: int) -> None:
        """Every frame, DAQ included, before any check."""

    def on_overload(self, where: tuple[int, int]) -> None:
        """A DAQ packet flagged overload in its PID's MSB, flag stripped, about to be decoded."""
