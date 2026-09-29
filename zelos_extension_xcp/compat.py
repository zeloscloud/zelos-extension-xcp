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
    "FrameCategory",
    "Master",
    "XcpResponseError",
    "XcpTimeoutError",
    "first_fit_decreasing",
    "make_continuous_blocks",
    "close",
    "make_can_master",
    "make_master",
]

Command = types.Command
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
    """

    def __init__(self, deliver: Callable[[bytes, int, int, int], None]) -> None:
        self._deliver = deliver
        self.malformed = 0

    def feed_frame(self, datagram: bytes, timestamp: int) -> None:
        offset, end = 0, len(datagram)
        while offset < end:
            if end - offset < ETH_HEADER.size:
                self.malformed += 1
                return
            length, counter = ETH_HEADER.unpack_from(datagram, offset)
            start = offset + ETH_HEADER.size
            if length == 0 or start + length > end:
                self.malformed += 1
                return
            self._deliver(datagram[start : start + length], length, counter, timestamp)
            offset = start + length


def _bounded_get(transport: Any) -> Any:
    """`BaseTransport.get` with a hard deadline, ending early once `transport.abort` is set.

    The library restarts a command's timeout on every DAQ packet, so a lost
    response waits forever while DAQ flows; and it reads its timeout once per
    call, so a stop would wait out a long user timeout.
    """
    start = time.monotonic()
    with transport.resQueue_condition:
        while not transport.resQueue:
            if transport.abort.is_set():
                raise _transport_base.EmptyFrameError
            remaining = transport.timeout / 1e9 - (time.monotonic() - start)
            if remaining <= 0:
                raise _transport_base.EmptyFrameError
            transport.resQueue_condition.wait(timeout=min(remaining, 0.05))
        return transport.resQueue.popleft()


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
    return master


def make_master(host: str, port: int, protocol: str, timeout: float, policy: Any = None) -> Master:
    """An XCP on Ethernet master without config file, command line or retry handler.

    Commands raise on the first timeout or negative response; retries are the
    caller's. `master.transport.connect()` opens the socket.
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
    reaches the decoder.
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
        where = self.locate(payload)
        if where is None:
            self.rejected += 1
            return
        self.on_packet(counter, timestamp, payload, where)
        super().feed(cat, counter, timestamp, payload)

    def on_packet(self, counter: int, timestamp: int, payload: bytes, where: tuple[int, int]):
        """A DAQ packet, checked, about to be decoded."""

    def on_frame(self, cat: int, counter: int) -> None:
        """Every frame, DAQ included, before any check."""
