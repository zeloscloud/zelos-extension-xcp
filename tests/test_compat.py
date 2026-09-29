"""Workarounds for pyxcp defects that can be checked without a slave."""

import struct
import sys
import threading
import time

import pytest

from zelos_extension_xcp import compat
from zelos_extension_xcp.compat import DaqList, DaqPolicy, DatagramFramer, make_master


def test_import_keeps_global_hooks():
    assert sys.excepthook is sys.__excepthook__
    assert sys.displayhook is sys.__displayhook__


def test_udp_reads_whole_datagrams():
    assert compat._eth.Eth.MAX_DATAGRAM_SIZE == 65535


@pytest.mark.skipif(not hasattr(time, "tzset"), reason="tzset is POSIX only")
def test_master_creation_in_abbreviated_timezone(monkeypatch):
    monkeypatch.setenv("TZ", "America/Los_Angeles")
    time.tzset()
    try:
        assert time.tzname[0] == "PST"
        master = make_master("127.0.0.1", 9, "UDP", 0.1)
        master.transport.close_connection()
    finally:
        monkeypatch.undo()
        time.tzset()


def test_command_times_out_while_daq_flows():
    master = make_master("127.0.0.1", 9, "UDP", 0.2)
    transport = master.transport
    flowing = threading.Event()

    def daq():  # the library restarts a waiting command's timeout on each DAQ packet
        while not flowing.is_set():
            transport.timer_restart_event.set()
            time.sleep(0.01)

    threading.Thread(target=daq).start()
    t0 = time.monotonic()
    try:
        with pytest.raises(compat._transport_base.EmptyFrameError):
            transport.get()
    finally:
        flowing.set()
    assert time.monotonic() - t0 < 0.4
    transport.close_connection()


def test_stop_interrupts_a_waiting_command():
    master = make_master("127.0.0.1", 9, "UDP", 30.0)
    transport = master.transport
    threading.Timer(0.1, transport.abort.set).start()
    t0 = time.monotonic()
    with pytest.raises(compat._transport_base.EmptyFrameError):
        transport.get()
    assert time.monotonic() - t0 < 0.5
    transport.close_connection()


def packet(ctr, payload):
    return struct.pack("<HH", len(payload), ctr) + payload


def test_malformed_datagram_drops_only_itself():
    got = []
    framer = DatagramFramer(lambda p, n, ctr, ts: got.append((ctr, p)))
    framer.feed_frame(packet(1, b"\xff\x00") + struct.pack("<HH", 40, 2) + b"\x00" * 3, 0)
    framer.feed_frame(packet(3, b"\x00\x01\x02\x03"), 0)
    assert got == [(1, b"\xff\x00"), (3, b"\x00\x01\x02\x03")]
    assert framer.malformed == 1


def test_short_daq_packet_never_reaches_the_decoder():
    policy = DaqPolicy([DaqList("e", 0, False, True, [("a", 0x100, 0, "U32")])])
    assert policy.pid_off is False
    policy.id_size, policy.min_length = 4, {(0, 0): 12}
    policy.feed(compat.FrameCategory.DAQ, 0, 0, b"\x00\x00\x00\x00\x01\x02")  # short
    policy.feed(compat.FrameCategory.DAQ, 1, 0, b"\x05\x00\x00\x00" + b"\x00" * 8)  # no ODT 5
    assert policy.rejected == 2
