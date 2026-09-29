"""Demo ECU on XCP on CAN: classic-frame DAQ, CAN FD, ids, the python-can path."""

import struct
import time
import uuid

import pytest
from test_demo_ecu import (
    CAN_INTERFACE,
    CONNECT,
    GET_STATUS,
    OK,
    SHORT_UPLOAD,
    START_STOP_SYNCH,
    Master,
    demo_ecu,
    u32,
    where,
)

from zelos_extension_xcp.demo import DemoEcu, model

virtual_only = pytest.mark.skipif(CAN_INTERFACE is not None, reason="zelos-can virtual bus only")


def pack(names, ts_size=4, size=7):
    """Greedy ODTs of `size` data bytes; 8-byte values split in two 4-byte entries.

    Returns the ODTs as raw (ext, addr, n) entries and, per name, its parts as
    (odt, offset in the ODT payload, n).
    """
    odts, parts, room = [[]], {}, size - ts_size
    for name in names:
        ext, addr, n, _ = where(name)
        for off in range(0, n, 4 if n == 8 else n):
            k = 4 if n == 8 else n
            if k > room:
                odts.append([])
                room = size
            parts.setdefault(name, []).append((len(odts) - 1, 1 + size - room, k))
            odts[-1].append((ext, addr + off, k))
            room -= k
    return odts, parts


def value(name, sample, parts):
    raw = b"".join(sample[odt][o : o + n] for odt, o, n in parts[name])
    return struct.unpack("<" + where(name)[3], raw)[0]


FAST = [
    *(n for n, leaf in model.LEAVES.items() if leaf.event == 0 and leaf.mask is None),
    *(f"bms.cell_voltage[{i}]" for i in range(11)),
]  # 40 signals: every 10 ms scalar, the phase structs, 11 cells from the 100 ms task


def test_forty_signals_on_classic_can_for_five_seconds():
    assert len(FAST) == 40
    odts, parts = pack(FAST)
    with demo_ecu("can") as e:
        m = Master(e)
        m.connect()
        assert m.daq([(0, odts), (1, [["diag.heartbeat", "bms.soc", "inv.state"]])]) == [
            0,
            len(odts),
        ]
        assert m.cmd(START_STOP_SYNCH, 1) == OK
        n, last = len(odts), len(odts) - 1
        t0 = time.monotonic()
        # 5 s of ECU time: 500 samples, however long a loaded machine takes
        pkts = [p for _, p in m.collect(lambda d: sum(p[0] == last for _, p in d) >= 500, 120)]
        dt = time.monotonic() - t0
        assert m.cmd(START_STOP_SYNCH, 0) == OK
        m.close()

    assert max(len(p) for p in pkts) <= 8
    fast = [p for p in pkts if p[0] < n]
    # Complete and ordered: PIDs cycle 0..N-1 with no gap, one frame per ODT
    assert [p[0] for p in fast] == (list(range(n)) * (len(fast) // n + 1))[: len(fast)]
    samples = [fast[i : i + n] for i in range(0, len(fast) - n + 1, n)]
    assert len(samples) >= 500
    uptimes = [value("sys.uptime_ns", s, parts) for s in samples]
    assert all(b - a == 10_000_000 for a, b in zip(uptimes, uptimes[1:], strict=False))
    assert len(pkts) / dt > 270  # nominal 2700 frames/s; a wide band for a busy machine
    for s, up in zip(samples, uptimes, strict=True):
        assert struct.unpack_from("<I", s[0], 1)[0] * 1000 == up  # timestamp, 1 us
        for name in FAST:
            assert value(name, s, parts) == e.expected(name, up / 1e9), name
    beats = [p[5] for p in pkts if p[0] == n]
    assert len(beats) >= 45 and all(
        (b - a) % 256 == 1 for a, b in zip(beats, beats[1:], strict=False)
    )
    print(
        f"\n{len(odts)} ODTs per 10 ms sample, {len(samples)} samples, {len(pkts)} frames"
        f" in {dt:.2f} s: {len(pkts) / dt:.0f} frames/s, lost 0"
    )


@virtual_only
def test_can_fd_pads_to_fd_lengths():
    with demo_ecu("can", can_fd=True) as e:
        m = Master(e)
        res = m.connect()
        assert (res[3], struct.unpack_from("<H", res, 4)[0]) == (64, 64)
        res = m.cmd(SHORT_UPLOAD, len(e.epk), 0, 0, *u32(model.EPK_ADDRESS))
        assert res[1:18].decode() == e.epk and res[18:] == b"\0\0"  # 18 bytes padded to 20
        m.close()


@virtual_only
def test_python_can_bus_path():
    """The ECU on python-can's zelos-virtual interface, the master on the native bus."""
    channel = f"t-{uuid.uuid4()}"
    with DemoEcu(transport="can", interface="zelos-virtual", channel=channel) as e:
        m = Master(e)
        m.connect()
        assert m.upload(0, model.EPK_ADDRESS, len(e.epk)).decode() == e.epk
        m.close()


@virtual_only
def test_extended_ids():
    with demo_ecu("can", can_extended=True, can_id_master=0x18DA00F1) as e:
        m = Master(e)
        m.bus.send(id=0x0DA, data=bytes([CONNECT, 0]), is_extended=False, is_fd=False)
        m.bus.send(id=0x18DA00F1, data=bytes([CONNECT, 0]), is_extended=True, is_fd=False)
        assert m.reply()[0] == 0xFF
        # frames are handled in order, so the 11-bit one came first and was ignored
        assert e.stats["commands"] == {CONNECT: 1}
        m.close()


def test_other_ids_and_own_frames_are_ignored():
    with demo_ecu("can") as e:
        m = Master(e)
        m.send(CONNECT, 0, can_id=0x123)
        m.send(CONNECT, 0, can_id=model.CAN_ID_SLAVE)  # looks like the slave's own frame
        if CAN_INTERFACE is None:  # a virtual bus returns the spoofed frame to this master
            assert m.reply() == bytes([CONNECT, 0])
        m.connect()
        assert m.cmd(GET_STATUS)[0] == 0xFF
        assert m.cmd(GET_STATUS)[0] == 0xFF
        # Handled in order: the junk frames before CONNECT, and the echoes of its own
        # RES (PID 0xFF, like CONNECT) before the second GET_STATUS, were all ignored
        assert e.stats["commands"] == {CONNECT: 1, GET_STATUS: 2}
        assert e.stats["sessions"] == 1
        m.close()
