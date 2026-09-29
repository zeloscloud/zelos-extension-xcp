"""Demo ECU against a raw XCP master, on CAN, UDP and TCP.

CAN runs on the in-process zelos-can virtual bus. On Linux, set
XCP_DEMO_CAN_INTERFACE=socketcan and XCP_DEMO_CAN_CHANNEL=vcan0 to run the
CAN cases through python-can on an existing interface instead.
"""

import os
import re
import socket
import struct
import threading
import time
import uuid

import pytest
from conftest import wait_until

from zelos_extension_xcp.demo import DemoEcu, model
from zelos_extension_xcp.demo.a2l import render

CONNECT, DISCONNECT, GET_STATUS, GET_ID = 0xFF, 0xFE, 0xFD, 0xFA
SET_MTA, UPLOAD, SHORT_UPLOAD = 0xF6, 0xF5, 0xF4
SET_DAQ_PTR, WRITE_DAQ, SET_DAQ_LIST_MODE, GET_DAQ_LIST_MODE = 0xE2, 0xE1, 0xE0, 0xDF
START_STOP_DAQ_LIST, START_STOP_SYNCH = 0xDE, 0xDD
FREE_DAQ, ALLOC_DAQ, ALLOC_ODT, ALLOC_ODT_ENTRY = 0xD6, 0xD5, 0xD4, 0xD3
OK = b"\xff"
CAN_INTERFACE = os.environ.get("XCP_DEMO_CAN_INTERFACE")
CAN_CHANNEL = os.environ.get("XCP_DEMO_CAN_CHANNEL")


def u16(v):
    return tuple(struct.pack("<H", v))


def u32(v):
    return tuple(struct.pack("<I", v))


def where(name):
    """(ext, addr, size, struct format) of a measurement or array element."""
    m = re.fullmatch(r"(.+)\[(\d+)\]", name)
    leaf = model.LEAVES.get(name) or model.LEAVES[m.group(1)]
    fmt = model.DTYPES[leaf.dtype]
    size = struct.calcsize(fmt)
    idx = int(m.group(2)) if leaf.dims else 0
    return leaf.ext, leaf.addr + idx * size, size, fmt


def demo_ecu(transport, **kw):
    """A DemoEcu; CAN on its own virtual channel, or on XCP_DEMO_CAN_* when set."""
    if transport == "can":
        if CAN_INTERFACE:
            kw.update(interface=CAN_INTERFACE, channel=CAN_CHANNEL)
        else:
            pytest.importorskip("zelos_can")
            kw.setdefault("channel", f"t-{uuid.uuid4()}")
    return DemoEcu(transport=transport, **kw)


class Master:
    """Raw XCP master. DAQ packets seen while waiting for a response go to `dto`."""

    def __init__(self, ecu):
        self.dto, self.max_cto = [], 8
        self.can = ecu.transport == "can"
        if self.can:
            self.fd = ecu._link.fd
            if CAN_INTERFACE:
                import can

                self.bus = can.Bus(interface=CAN_INTERFACE, channel=CAN_CHANNEL, fd=self.fd)
            else:
                import zelos_can

                self.bus = zelos_can.VirtualBus(channel=ecu._link.channel)
        elif ecu.transport == "udp":
            self.s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.s.connect(("127.0.0.1", ecu.port))
        else:
            self.s = socket.create_connection(("127.0.0.1", ecu.port))
        self.buf, self.ctr = b"", 0

    def close(self):
        if not self.can:
            self.s.close()
        elif CAN_INTERFACE:
            self.bus.shutdown()

    def send(self, *b, can_id=model.CAN_ID_MASTER):
        if not self.can:
            self.s.sendall(struct.pack("<HH", len(b), self.ctr) + bytes(b))
            self.ctr += 1
        elif CAN_INTERFACE:
            import can

            self.bus.send(can.Message(arbitration_id=can_id, data=bytes(b), is_extended_id=False))
        else:
            self.bus.send(id=can_id, data=bytes(b), is_extended=False, is_fd=self.fd)

    def packet(self, timeout):
        """Next (ctr, packet) from the ECU, or None on timeout or close. CAN has no CTR: 0."""
        end = time.monotonic() + timeout
        if self.can:
            while (left := end - time.monotonic()) > 0:
                msg = self.bus.recv(timeout=left)
                if msg is not None and msg.arbitration_id == model.CAN_ID_SLAVE:
                    return 0, bytes(msg.data)
            return None
        while len(self.buf) < 4 or len(self.buf) < 4 + struct.unpack_from("<H", self.buf)[0]:
            self.s.settimeout(max(end - time.monotonic(), 0.001))
            try:
                data = self.s.recv(65535)
            except TimeoutError:
                return None
            if not data:
                return None
            self.buf += data
        ln, ctr = struct.unpack_from("<HH", self.buf)
        pkt, self.buf = self.buf[4 : 4 + ln], self.buf[4 + ln :]
        return ctr, pkt

    def reply(self, timeout=5.0):
        end = time.monotonic() + timeout
        while (p := self.packet(end - time.monotonic())) is not None:
            if p[1][0] >= 0xFC:
                return p[1]
            self.dto.append(p)
        return None

    def cmd(self, *b, timeout=5.0):
        self.send(*b)
        return self.reply(timeout)

    def connect(self):
        res = self.cmd(CONNECT, 0)
        assert res[0] == 0xFF, res
        self.max_cto = res[3]
        return res

    def collect(self, pred, timeout=30.0):
        """DAQ packets, as (ctr, packet), read until `pred(packets)` holds."""

        def more():
            assert self.reply(0.02) is None, "unexpected response"
            return pred(self.dto)

        assert wait_until(more, timeout=timeout, interval=0), "DAQ condition not met"
        out, self.dto = self.dto, []
        return out

    def upload(self, ext, addr, n):
        assert self.cmd(SET_MTA, 0, 0, ext, *u32(addr)) == OK
        out = b""
        while len(out) < n:
            k = min(n - len(out), self.max_cto - 1)
            out += self.cmd(UPLOAD, k)[1 : 1 + k]
        return out

    def read(self, name):
        ext, addr, size, fmt = where(name)
        res = self.cmd(SHORT_UPLOAD, size, 0, ext, *u32(addr))
        assert res[0] == 0xFF, res.hex()
        return struct.unpack("<" + fmt, res[1 : 1 + size])[0]

    def daq(self, lists, ts=True):
        """Configure and select DAQ lists [(event, [[entry, ...] per ODT])].

        An entry is a name, or (ext, addr, size) raw. Returns the first PIDs.
        """
        assert self.cmd(FREE_DAQ) == OK
        assert self.cmd(ALLOC_DAQ, 0, *u16(len(lists))) == OK
        for n, (_, odts) in enumerate(lists):
            assert self.cmd(ALLOC_ODT, 0, *u16(n), len(odts)) == OK
        for n, (_, odts) in enumerate(lists):
            for i, entries in enumerate(odts):
                assert self.cmd(ALLOC_ODT_ENTRY, 0, *u16(n), i, len(entries)) == OK
        pids = []
        for n, (event, odts) in enumerate(lists):
            for i, entries in enumerate(odts):
                assert self.cmd(SET_DAQ_PTR, 0, *u16(n), i, 0) == OK
                for e in entries:
                    ext, addr, size = where(e)[:3] if isinstance(e, str) else e
                    assert self.cmd(WRITE_DAQ, 0xFF, size, ext, *u32(addr)) == OK
            mode = 0x10 if ts else 0
            assert self.cmd(SET_DAQ_LIST_MODE, mode, *u16(n), *u16(event), 1, 0) == OK
            res = self.cmd(START_STOP_DAQ_LIST, 2, *u16(n))
            assert res[0] == 0xFF
            pids.append(res[1])
        return pids


@pytest.fixture(params=["can", "udp", "tcp"])
def ecu(request):
    with demo_ecu(request.param) as e:
        yield e


@pytest.fixture
def master(ecu):
    m = Master(ecu)
    yield m
    m.close()


def test_session_epk_and_polled_read(ecu, master):
    res = master.connect()
    size = 8 if ecu.transport == "can" else 255
    assert res[:4] == bytes([0xFF, 0x04, 0x80, size])  # DAQ only; little endian, bytes
    assert master.upload(0, model.EPK_ADDRESS, len(ecu.epk)).decode() == ecu.epk
    assert master.cmd(GET_ID, 1)[4:8] == bytes(u32(4))  # "demo"

    def consistent():
        """The 10 ms task's run count, unchanged across the reads, and the values."""
        k = master.read("sys.tick_10ms")
        torque, cell = master.read("motor.torque"), master.read("bms.cell_voltage[3]")
        return (k, torque, cell) if master.read("sys.tick_10ms") == k else None

    k, torque, cell = wait_until(consistent, timeout=30, interval=0)
    assert torque == ecu.expected("motor.torque", k / 100)
    assert cell == ecu.expected("bms.cell_voltage[3]", k / 100)
    assert master.cmd(DISCONNECT) == OK
    assert master.cmd(GET_STATUS, timeout=0.2) is None


REFUSED = [
    (0xF0, 1, 0x00),  # DOWNLOAD
    (0xED, 1, 0, 1, *u32(0x10008), 0),  # SHORT_DOWNLOAD over diag.signature
    (0xEC, 0, 0xFF, 0xFF, 0, 0),  # MODIFY_BITS
    (0xEB, 0x83, 0, 0),  # SET_CAL_PAGE
    (0xE4, 0, 0, 0, 1),  # COPY_CAL_PAGE
    (0xF9, 0x01, 0, 0),  # SET_REQUEST
    (0xD2,),  # PROGRAM_START
    (0xD1, 0, 0, 0, *u32(0x100)),  # PROGRAM_CLEAR
    (0xF8, 0, 0x01),  # GET_SEED
    (0xF7, 2, 0, 0),  # UNLOCK
    (0xF1, 0),  # USER_CMD
]


def test_refused_commands_are_not_executed(ecu, master):
    master.connect()
    assert master.cmd(SET_MTA, 0, 0, 1, *u32(0x10008)) == OK  # diag.signature
    for req in REFUSED:
        assert master.cmd(*req) == bytes([0xFE, 0x20]), hex(req[0])
    # STIM direction
    assert master.cmd(FREE_DAQ) == OK
    assert master.cmd(ALLOC_DAQ, 0, *u16(1)) == OK
    assert master.cmd(SET_DAQ_LIST_MODE, 0x12, *u16(0), *u16(0), 1, 0) == bytes([0xFE, 0x27])
    assert master.cmd(GET_DAQ_LIST_MODE, 0, *u16(0))[4:6] == b"\xff\xff"  # no event set
    assert master.read("diag.signature") == 0x5A5A_1234
    refused = ecu.stats["refused"]
    assert refused == {**{r[0]: 1 for r in REFUSED}, SET_DAQ_LIST_MODE: 1}


def ticks(pkts):
    return [struct.unpack("<I", p[1:])[0] for _, p in pkts if len(p) == 5]


def steps(values):
    return [b - a for a, b in zip(values, values[1:], strict=False)]


@pytest.mark.parametrize("transport", ["can", "udp", "tcp"])
def test_fault_injection(transport):
    """Each hook applies from the next DAQ packet on: waits on the effect, no fixed window."""
    with demo_ecu(transport, timestamp_size=0) as ecu:
        master = Master(ecu)
        master.connect()
        master.daq([(0, [["sys.tick_10ms"]])], ts=False)  # PID + 4 bytes, no timestamp
        assert master.cmd(START_STOP_SYNCH, 1) == OK
        last = master.collect(lambda d: len(d) >= 3)[-1:]

        def after(event):
            """Packets until `event` has shown in the tick steps, then one more step."""

            def done(d):
                s = steps(ticks(last + d))
                return event in s and s[-1] != event

            return last + master.collect(done)

        ecu.drop_dto(2)
        got = after(3)
        assert steps(ticks(got)).count(3) == 1 and set(steps(ticks(got))) <= {1, 3}
        if transport != "can":  # a dropped packet still used its CTR
            assert steps([c for c, _ in got]).count(3) == 1
        last = got[-1:]

        ecu.truncate_dto(1)
        got = after(2)
        assert [len(p) for _, p in got].count(2) == 1  # 5 bytes cut to half
        assert steps(ticks(got)).count(2) == 1 and set(steps(ticks(got))) <= {1, 2}
        last = got[-1:]

        ecu.swap_dto()
        got = after(-1)
        assert steps(ticks(got)).count(-1) == 1  # one reversed pair
        assert steps(ticks(got)).count(2) == 2 and set(steps(ticks(got))) <= {-1, 1, 2}

        ecu.stall(2.0)
        t0, frozen = time.monotonic(), ecu.stats["dto_sent"]
        master.send(GET_STATUS)  # discarded, never answered
        assert master.reply(1.5 - (time.monotonic() - t0)) is None
        assert ecu.stats["dto_sent"] == frozen
        assert wait_until(lambda: ecu.stats["dto_sent"] > frozen, timeout=30)
        assert master.cmd(GET_STATUS)[0] == 0xFF

        ecu.go_silent()
        frozen = ecu.stats["dto_sent"]
        master.send(GET_STATUS)
        assert master.reply(0.5) is None
        assert ecu.stats["dto_sent"] == frozen
        master.close()


def test_daq_capacity_overflow():
    with demo_ecu("can", max_daq=2, max_odt=2, max_odt_entries=3) as e:
        m = Master(e)
        m.connect()
        overflow = bytes([0xFE, 0x30])
        assert m.cmd(ALLOC_DAQ, 0, *u16(3)) == overflow
        assert m.cmd(ALLOC_DAQ, 0, *u16(2)) == OK
        assert m.cmd(ALLOC_ODT, 0, *u16(0), 3) == overflow
        assert m.cmd(ALLOC_ODT, 0, *u16(0), 1) == OK
        assert m.cmd(ALLOC_DAQ, 0, *u16(1)) == bytes([0xFE, 0x29])  # sequence
        assert m.cmd(ALLOC_ODT_ENTRY, 0, *u16(0), 0, 4) == overflow
        assert m.cmd(ALLOC_ODT_ENTRY, 0, *u16(0), 0, 2) == OK
        # PID + 4 byte timestamp + 2 x 2 bytes > MAX_DTO 8
        assert m.cmd(SET_DAQ_PTR, 0, *u16(0), 0, 0) == OK
        for _ in range(2):
            assert m.cmd(WRITE_DAQ, 0xFF, 2, 0, *u32(0x10002)) == OK
        assert m.cmd(WRITE_DAQ, 0xFF, 8, 0, *u32(0x10000)) == bytes([0xFE, 0x22])  # > 7
        assert m.cmd(SET_DAQ_LIST_MODE, 0x10, *u16(0), *u16(0), 1, 0) == OK
        assert m.cmd(START_STOP_DAQ_LIST, 1, *u16(0)) == bytes([0xFE, 0x2A])
        m.close()


@pytest.mark.parametrize(
    ("transport", "ts_size", "ts_unit_ns", "seconds"),
    [("udp", 4, 1000, 0.4), ("tcp", 2, 10_000, 0.9)],  # 2 bytes of 10 us wrap every 655 ms
)
def test_eth_daq_multi_odt_two_events(transport, ts_size, ts_unit_ns, seconds):
    fast = [
        ["sys.uptime_ns", "motor.torque", "inv.status_word"],
        ["motor.phase[1].current", "bms.cell_voltage[95]"],
    ]
    slow = [["diag.heartbeat", "bms.soc"]]
    with DemoEcu(transport=transport, timestamp_size=ts_size, timestamp_unit_ns=ts_unit_ns) as e:
        m = Master(e)
        m.connect()
        assert m.daq([(0, fast), (1, slow)]) == [0, 2]
        assert m.cmd(START_STOP_SYNCH, 1) == OK
        n_fast, n_slow = round(seconds / 0.01), round(seconds / 0.1)
        dtos = m.collect(
            lambda d: (
                sum(p[0] == 1 for _, p in d) >= n_fast and sum(p[0] == 2 for _, p in d) >= n_slow
            )
        )
        assert m.cmd(START_STOP_SYNCH, 0) == OK
        m.close()

    wrap = 1 << (8 * ts_size)
    ts_fmt = {2: "H", 4: "I"}[ts_size]

    def unwrap(stamps):
        out = [stamps[0]]
        for a, b in zip(stamps, stamps[1:], strict=False):
            out.append(out[-1] + (b - a) % wrap)
        return out

    pkts = [p for _, p in dtos]
    odt1 = [p for p in pkts if p[0] == 1]
    rows = [struct.unpack("<B" + ts_fmt + "QfH", p) for p in pkts if p[0] == 0]
    assert len(rows) - len(odt1) in (0, 1)  # the last sample may be half read
    rows = rows[: len(odt1)]
    uptimes = [r[2] for r in rows]
    assert all(b - a == 10_000_000 for a, b in zip(uptimes, uptimes[1:], strict=False))
    stamps = unwrap([r[1] for r in rows])
    assert [(s - stamps[0]) * ts_unit_ns for s in stamps] == [u - uptimes[0] for u in uptimes]
    if ts_size == 2:
        assert stamps[-1] >= wrap  # crossed a wrap
    for (_, _, up, torque, word), p1 in zip(rows, odt1, strict=True):
        t = up / 1e9
        current, cell = struct.unpack("<fH", p1[1:])
        assert torque == e.expected("motor.torque", t)
        assert (word & 0x8000) >> 15 == e.expected("inv.flag.toggle", t)
        assert current == e.expected("motor.phase[1].current", t)
        assert cell == e.expected("bms.cell_voltage[95]", t)

    slow_rows = [struct.unpack("<B" + ts_fmt + "BB", p) for p in pkts if p[0] == 2]
    assert all((b[2] - a[2]) % 256 == 1 for a, b in zip(slow_rows, slow_rows[1:], strict=False))
    for s, (_, _, beat, soc) in zip(unwrap([r[1] for r in slow_rows]), slow_rows, strict=True):
        t = s * ts_unit_ns / 1e9
        assert round(t * 1e9) % 100_000_000 == 0
        assert (beat, soc) == (e.expected("diag.heartbeat", t), e.expected("bms.soc", t))


def test_second_master_udp_takes_over():
    with DemoEcu(transport="udp") as e:
        a, b = Master(e), Master(e)
        a.connect()
        b.connect()  # silently ends a's session
        assert a.cmd(GET_STATUS, timeout=0.2) is None
        assert b.cmd(GET_STATUS)[0] == 0xFF
        assert e.stats["sessions"] == 2
        a.close()
        b.close()


def test_second_master_tcp_waits():
    with DemoEcu(transport="tcp") as e:
        a, b = Master(e), Master(e)
        a.connect()
        assert b.cmd(CONNECT, 0, timeout=0.3) is None  # held in the listen backlog
        assert a.cmd(GET_STATUS)[0] == 0xFF
        a.close()
        assert b.reply()[0] == 0xFF  # served once a has gone
        b.close()


@pytest.mark.parametrize("transport", ["can", "udp", "tcp"])
def test_stop_is_bounded(transport):
    e = demo_ecu(transport).start()
    m = Master(e)
    m.connect()
    m.daq([(0, [["motor.speed"]])])
    assert m.cmd(START_STOP_SYNCH, 1) == OK
    t = time.monotonic()
    e.stop()
    assert time.monotonic() - t < 1.0
    assert not any(th.name == "xcp-demo-ecu" for th in threading.enumerate())
    if transport != "can":
        kind = socket.SOCK_DGRAM if transport == "udp" else socket.SOCK_STREAM
        with socket.socket(socket.AF_INET, kind) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)  # TCP: TIME_WAIT only
            s.bind(("127.0.0.1", e.port))
    m.close()


def test_a2l_is_the_rendered_table():
    assert DemoEcu().a2l_path.read_text() == render()


def test_model_fits_its_datatypes():
    """Power-up, two drive cycles, then hours out: every block packs."""
    hours = (h * 3_600_000_000_000 for h in (1, 10, 100))
    for t in [*range(0, 41_000_000_000, 10_000_000), *hours]:
        for block in model.BLOCKS:
            model.image(block, t)


def test_physical_is_the_a2l_conversion_of_expected(tmp_path):
    """Model, rendered A2L and physical() agree for every measurement."""
    a2l = pytest.importorskip("zelos_can.a2l")
    path = tmp_path / "demo.a2l"
    path.write_text(render())
    ecu, t = DemoEcu(), 12.34
    for m in a2l.load(str(path), strict=True)["measurements"]:
        names = [f"{m['name']}[{i}]" for i in range(m["dims"][0])] if m["dims"] else [m["name"]]
        conv = m["conversion"]
        for name in names:
            raw = ecu.expected(name, t)
            if conv["kind"] == "LINEAR":
                a, b = conv["coeffs"]
                want = a * raw + b
            elif conv["kind"] == "RAT_FUNC":
                _, b, c, _, _, f = conv["coeffs"]
                want = (raw * f - c) / b
            else:
                want = raw
            assert ecu.physical(name, t) == want and type(ecu.physical(name, t)) is type(want)
            assert ecu.unit(name) == (m["unit"] or ""), name
            labels = {int(lo): text for lo, _, text in conv.get("table", [])}
            assert ecu.labels(name) == (labels if conv["kind"] == "TAB_VERB" else {}), name
