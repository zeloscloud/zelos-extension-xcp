"""ECU timestamp to host time: own unwrap, re-anchor after a gap, drift."""

from zelos_extension_xcp.timestamps import EcuClock

MS = 1_000_000


def test_unwrap_across_wraps():
    clock = EcuClock(ts_bytes=2, tick_ns=1000)  # 65.536 ms wrap
    clock.anchor(host_ns=10**18, ecu_ticks=65_000)
    times = []
    for i in range(300):  # 1 ms rows, several wraps
        raw = (65_000 + i * 1000) % 65_536
        host = 10**18 + i * MS + 3 * MS  # constant 3 ms receive lag
        assert not clock.needs_anchor(0, host)
        times.append(clock.stamp(0, raw, host))
    assert [b - a for a, b in zip(times, times[1:], strict=False)] == [MS] * 299
    assert times[0] == 10**18


def test_gap_longer_than_half_wrap_needs_a_fresh_anchor():
    clock = EcuClock(ts_bytes=4, tick_ns=1)  # 4.295 s wrap
    clock.anchor(host_ns=0, ecu_ticks=4_000_000_000)
    assert clock.stamp(0, 4_000_000_000 + 1_000_000, 1_000_000) == 1_000_000
    # 5 s of silence: more than a wrap, the tick difference alone would lose one.
    later = 5_001_000_000
    raw = (4_000_000_000 + later) % 2**32
    assert clock.needs_anchor(0, later)
    clock.anchor(host_ns=later + 2 * MS, ecu_ticks=raw + 2 * MS)
    assert not clock.needs_anchor(0, later)
    assert clock.stamp(0, raw, later) == later
    assert clock.anchors == 1 + clock.gaps == 2


def test_sample_held_through_a_gap_is_refused_not_misplaced():
    clock = EcuClock(ts_bytes=4, tick_ns=1)
    clock.anchor(host_ns=0, ecu_ticks=0)
    assert clock.stamp(0, 1_000_000, 1_000_000) == 1_000_000
    # The ECU froze for 5.3 s holding one sample taken at 11 ms; it arrives on resume.
    resume = 5_300_000_000
    clock.anchor(host_ns=resume, ecu_ticks=resume % 2**32)
    assert clock.stamp(0, 11_000_000, resume) is None
    assert clock.stamp(0, resume % 2**32, resume) == resume


def test_drift_is_measured_not_corrected():
    clock = EcuClock(ts_bytes=4, tick_ns=1)
    clock.anchor(host_ns=0, ecu_ticks=0)
    for i in range(1, 700):  # ECU 100 ppm slow over 7 s
        host = i * 10 * MS
        raw = round(host * (1 - 100e-6)) % 2**32
        t = clock.stamp(0, raw, host)
        assert t == round(host * (1 - 100e-6))
    assert 95 < clock.drift_ppm < 105


def test_short_wrap_keeps_one_offset_across_slow_rows():
    # 2 bytes of 1 us wrap in 65.536 ms; a 100 ms event is further apart than a wrap.
    clock = EcuClock(ts_bytes=2, tick_ns=1000, cycles_ns={0: 100 * MS})
    clock.anchor(host_ns=10**18, ecu_ticks=0)
    for i in range(1, 60):  # 5.9 s, receive lag jittering by 20 ms
        host = 10**18 + i * 100 * MS + (i % 3) * 10 * MS
        assert not clock.needs_anchor(0, host)
        assert clock.stamp(0, (i * 100_000) % 65_536, host) == 10**18 + i * 100 * MS
    assert clock.anchors == 1 and clock.drift_ppm is not None
    # Silence over two cycles and half a wrap: the next row needs a fresh anchor.
    assert clock.needs_anchor(0, 10**18 + 59 * 100 * MS + 20 * MS + 201 * MS)
