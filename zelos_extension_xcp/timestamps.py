"""ECU DAQ timestamps to host time: one constant offset, our own unwrap.

An anchor pairs the ECU clock (GET_DAQ_CLOCK) with host time. A row's time is
the anchor's host time plus the ECU ticks since the anchor. Consecutive rows of
one DAQ list unwrap from the tick difference, the wrap count taken from the
host time between them: a short counter (2 bytes of 1 us wrap in 65.5 ms) then
still keeps one offset across rows further apart than a wrap. After a host-side
gap longer than half the wrap and longer than one event cycle, the ECU may have
held or dropped samples, so the next row is placed by host time against a
fresh anchor instead, or refused when that is ambiguous too. Drift is measured,
never corrected.
"""

from __future__ import annotations

#: Width of the drift window: the lowest receive lag in each is one sample.
DRIFT_WINDOW_NS = 2_000_000_000

#: After a gap, a row received later than this after its sample time, capped at
#: a quarter wrap, cannot be placed exactly.
MAX_LATENCY_NS = 500_000_000

#: Clock rate difference assumed when placing a list's first row against an
#: anchor: the anchor serves while this drift stays within an eighth of a wrap.
ANCHOR_PPM = 200


class EcuClock:
    """Timestamps for one DAQ session.

    Args:
        ts_bytes: DAQ timestamp width, 1, 2 or 4 bytes.
        tick_ns: Nanoseconds per timestamp tick.
        cycles_ns: Event cycle per DAQ list key, None or absent when sporadic.
    """

    def __init__(
        self, ts_bytes: int, tick_ns: float, cycles_ns: dict[int, int | None] | None = None
    ) -> None:
        self.wrap = 1 << (8 * ts_bytes)
        self.tick_ns = tick_ns
        self.wrap_ns = self.wrap * tick_ns
        self.max_latency_ns = min(MAX_LATENCY_NS, self.wrap_ns / 4)
        self.anchor_life_ns = self.wrap_ns / 8 / (ANCHOR_PPM * 1e-6)
        self.cycles_ns = cycles_ns or {}
        self.anchor_host = 0
        self.anchor_ticks = 0
        self.anchor_clock = 0
        self.anchors = 0
        self._last: dict[int, tuple[int, int, int]] = {}  # list: (host rx, ticks, raw)
        self._baseline: tuple[int, int] | None = None  # (window start, min lag)
        self._window: tuple[int, int] | None = None
        self._latest: tuple[int, int] | None = None

    def anchor(self, host_ns: int, ecu_ticks: int) -> None:
        """Pair host time with the ECU clock (read at the same instant)."""
        self.anchor_host = host_ns
        self.anchor_clock = ecu_ticks
        self.anchor_ticks = ecu_ticks % self.wrap
        self.anchors += 1
        self._last.clear()
        self._baseline = self._window = self._latest = None

    def _continuous(self, key: int, host_rx: int) -> tuple[int, int, int] | None:
        """The list's last row when this one follows it without a gap, else None."""
        last = self._last.get(key)
        if last is None:
            return None
        gap = host_rx - last[0]
        cycle = self.cycles_ns.get(key) or 0
        return last if gap <= self.wrap_ns / 2 or gap <= 2 * cycle else None

    def needs_anchor(self, key: int, host_rx: int) -> bool:
        """True when this row cannot be placed exactly without a fresh anchor: after a gap,
        or the list's first row once the anchor is too old for the assumed drift."""
        if not self.anchors:
            return True
        if self._continuous(key, host_rx):
            return False
        return key in self._last or abs(host_rx - self.anchor_host) > self.anchor_life_ns

    def stamp(self, key: int, raw: int, host_rx: int) -> int | None:
        """Host time in ns of the row of DAQ list `key` with ECU timestamp `raw`.

        Call only when `needs_anchor` is False. None when the row cannot be
        placed exactly: after a gap, a row whose nearest placement is further
        from its receive time than `max_latency_ns` (a sample the ECU held
        through a stall reads the same as one a wrap later).
        """
        last = self._continuous(key, host_rx)
        if last:
            # The wrap count nearest the host time between the rows; never backwards.
            diff = (raw - last[2]) % self.wrap
            elapsed = (host_rx - last[0]) / self.tick_ns
            diff += max(0, round((elapsed - diff) / self.wrap)) * self.wrap
            ticks = last[1] + diff
        else:
            estimate = self.anchor_ticks + (host_rx - self.anchor_host) / self.tick_ns
            ticks = raw + round((estimate - raw) / self.wrap) * self.wrap
            if abs(estimate - ticks) * self.tick_ns > self.max_latency_ns:
                return None
        self._last[key] = (host_rx, ticks, raw)
        t = self.anchor_host + round((ticks - self.anchor_ticks) * self.tick_ns)
        self._drift(host_rx, host_rx - t)
        return t

    def _drift(self, host_rx: int, lag: int) -> None:
        window = self._window
        if window is None or host_rx - window[0] >= DRIFT_WINDOW_NS:
            if window is not None:
                if self._baseline is None:
                    self._baseline = window
                else:
                    self._latest = window
            self._window = (host_rx, lag)
        elif lag < window[1]:
            self._window = (window[0], lag)

    @property
    def drift_ppm(self) -> float | None:
        """ECU clock rate against the host, ppm (positive: the ECU runs slow)."""
        if self._baseline is None or self._latest is None:
            return None
        span = self._latest[0] - self._baseline[0]
        return (self._latest[1] - self._baseline[1]) / span * 1e6 if span else None

    @property
    def offset_ns(self) -> int:
        """Host time minus the ECU clock (GET_DAQ_CLOCK) at the anchor."""
        return self.anchor_host - round(self.anchor_clock * self.tick_ns)
