"""Demo ECU transports: XCP on Ethernet (UDP, TCP) and XCP on CAN.

A transport moves whole XCP packets. `poll()` returns received packets
with the sender's address, or `(None, None)` when a TCP master hangs up.
`frame()` wraps one packet for the wire and `send()` puts a frame on it;
they are separate so a dropped or held packet still consumes its CTR.
"""

from __future__ import annotations

import logging
import select
import socket
import struct

logger = logging.getLogger(__name__)

FD_LENGTHS = (0, 1, 2, 3, 4, 5, 6, 7, 8, 12, 16, 20, 24, 32, 48, 64)


class EthTransport:
    """XCP on Ethernet: LEN + CTR header per packet, then 0x00 fill to `align` bytes.

    The CTR counts every packet the slave sends. UDP puts up to `pack`
    packets in one datagram, sent when full or on `flush()`. TCP serves one
    connection; others wait in the listen backlog.
    """

    max_cto, max_dto = 255, 256  # defaults
    dto_limit = 0xFFFF

    def __init__(self, host: str, port: int, tcp: bool, align: int = 1, pack: int = 1):
        self.host, self.port, self.tcp = host, port, tcp
        self.align, self.pack = align, pack
        self._batch: list[bytes] = []
        self._batch_addr = None
        self._sock: socket.socket | None = None
        self._client: socket.socket | None = None
        self._rx = b""
        self._ctr = 0

    def open(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM if self.tcp else socket.SOCK_DGRAM)
        if self.tcp:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self.host, self.port))
        if self.tcp:
            sock.listen(1)
        self._sock = sock
        self.port = sock.getsockname()[1]

    def close(self) -> None:
        for s in (self._client, self._sock):
            if s is not None:
                s.close()
        self._client = self._sock = None

    def __str__(self) -> str:
        return f"{'tcp' if self.tcp else 'udp'} {self.host}:{self.port}"

    def poll(self, timeout: float) -> list:
        sock = self._client or self._sock
        if not select.select([sock], [], [], timeout)[0]:
            return []
        if not self.tcp:
            try:
                data, addr = sock.recvfrom(65535)
            except OSError:
                return []
            return [(p, addr) for p in self._split(data)]
        if sock is self._sock:
            self._client, _ = sock.accept()
            self._client.settimeout(0.5)
            self._rx = b""
            return []
        try:
            data = sock.recv(65535)
        except OSError:
            data = b""
        self._rx += data
        pkts = self._split(self._rx) if data else None
        if pkts is None:
            return self._hang_up()
        return [(p, None) for p in pkts]

    def _split(self, buf: bytes) -> list[bytes] | None:
        """Whole packets from `buf`; a TCP remainder stays buffered. None: bad framing."""
        out = []
        while len(buf) >= 4:
            ln = struct.unpack_from("<H", buf)[0]
            if ln == 0 or ln > self.max_cto:
                logger.warning("Bad XCP frame length %d", ln)
                return None if self.tcp else out
            if len(buf) < 4 + ln:
                break
            out.append(buf[4 : 4 + ln])
            buf = buf[4 + ln :]
        if self.tcp:
            self._rx = buf
        return out

    def _hang_up(self) -> list:
        if self._client is not None:
            self._client.close()
        self._client = None
        return [(None, None)]

    def frame(self, pkt: bytes) -> bytes:
        frame = struct.pack("<HH", len(pkt), self._ctr & 0xFFFF) + pkt
        self._ctr += 1
        return frame + bytes(-len(frame) % self.align)

    def send(self, frame: bytes, addr) -> bool:
        """False when the TCP master is gone."""
        if self.pack > 1 and not self.tcp:
            if self._batch and addr != self._batch_addr:
                self.flush()
            self._batch.append(frame)
            self._batch_addr = addr
            if len(self._batch) >= self.pack:
                self.flush()
            return True
        return self._send(frame, addr)

    def flush(self) -> None:
        """Send the packets held for one datagram."""
        if self._batch:
            batch, self._batch = b"".join(self._batch), []
            self._send(batch, self._batch_addr)

    def _send(self, frame: bytes, addr) -> bool:
        try:
            if not self.tcp:
                self._sock.sendto(frame, addr)
            elif self._client is not None:
                self._client.sendall(frame)
        except OSError as e:
            logger.warning("Send failed: %s", e)
            if self.tcp:
                self._hang_up()
                return False
        return True


class CanTransport:
    """XCP on CAN: one packet per frame, no CTR.

    The bus is a `zelos_can.VirtualBus` on `channel` (in-process), or, given
    `interface`, any python-can bus, e.g. `socketcan` on `vcan0`. Acts only
    on frames with the master-to-slave id; its own frames, which a virtual
    bus echoes back, and every other id are ignored. Classic CAN carries the
    packet unpadded (DLC = length), or padded to DLC 8 with `padding`; CAN FD
    pads with 0x00 to the next valid FD length. `max_dlc_required`: classic
    command frames shorter than DLC 8 are ignored.
    """

    def __init__(
        self,
        channel: str,
        id_master: int,
        id_slave: int,
        extended: bool,
        fd: bool,
        interface: str | None = None,
        bitrate: int | None = None,
        padding: int | None = None,
        max_dlc_required: bool = False,
    ):
        if id_master == id_slave:
            raise ValueError("CAN master and slave ids must differ")
        limit = 0x1FFF_FFFF if extended else 0x7FF
        if not (0 <= id_master <= limit and 0 <= id_slave <= limit):
            raise ValueError(f"CAN ids must be 0..0x{limit:X}")
        self.channel, self.id_master, self.id_slave = channel, id_master, id_slave
        self.extended, self.fd = extended, fd
        self.interface, self.bitrate = interface, bitrate
        self.padding = padding
        self.max_dlc_required = max_dlc_required and not fd
        self.max_cto = self.max_dto = self.dto_limit = 64 if fd else 8
        self.port = None
        self._bus = None

    def open(self) -> None:
        # Imported here: Ethernet needs neither
        if self.interface is None:
            from zelos_can import VirtualBus

            self._bus = VirtualBus(channel=self.channel)
            return
        import can

        mask = 0x1FFF_FFFF if self.extended else 0x7FF
        rx = {"can_id": self.id_master, "can_mask": mask, "extended": self.extended}
        opts = {"bitrate": self.bitrate} if self.bitrate else {}
        if self.fd:
            opts["fd"] = True
        self._bus = can.Bus(
            interface=self.interface, channel=self.channel, can_filters=[rx], **opts
        )
        self._msg = can.Message

    def close(self) -> None:
        if self.interface is not None and self._bus is not None:
            self._bus.shutdown()
        self._bus = None

    def __str__(self) -> str:
        bus = self.interface or "zelos-can virtual"
        fd = " fd" if self.fd else ""
        return f"can{fd} {bus} {self.channel} 0x{self.id_master:X}/0x{self.id_slave:X}"

    def poll(self, timeout: float) -> list:
        out = []
        msg = self._bus.recv(timeout=timeout)
        for n in range(1, 257):  # bounded, so a flooded bus cannot starve DAQ
            if msg is None:
                break
            if (
                msg.arbitration_id == self.id_master
                and msg.is_extended_id == self.extended
                and not getattr(msg, "is_error_frame", False)
                and not msg.is_remote_frame
                and msg.dlc > (7 if self.max_dlc_required else 0)
            ):
                out.append((bytes(msg.data), None))
            msg = self._bus.recv(timeout=0) if n < 256 else None
        return out

    def frame(self, pkt: bytes) -> bytes:
        if self.fd:
            pkt += bytes(next(n for n in FD_LENGTHS if n >= len(pkt)) - len(pkt))
        elif self.padding is not None:
            pkt += bytes([self.padding]) * (8 - len(pkt))
        return pkt

    def flush(self) -> None:
        """Nothing held: one packet per frame."""

    def send(self, frame: bytes, addr) -> bool:
        try:
            if self.interface is None:
                self._bus.send(
                    id=self.id_slave, data=frame, is_extended=self.extended, is_fd=self.fd
                )
            else:
                self._bus.send(
                    self._msg(
                        arbitration_id=self.id_slave,
                        data=frame,
                        is_extended_id=self.extended,
                        is_fd=self.fd,
                        bitrate_switch=self.fd,
                    )
                )
        except Exception as e:  # a real adapter can refuse, e.g. TX queue full
            logger.warning("CAN send failed: %s", e)
        return True
