"""Polled reads: a group's signals merged into as few upload commands as fit.

Signals of one address extension whose ranges touch or overlap are read
together, never across a gap, so nothing outside the A2L's measurements is
read. Each run is one command: `SHORT_UPLOAD` when it fits one response, else
`SET_MTA` and one `UPLOAD` the slave answers in several packets (slave block
mode). A value never spans two commands, so it is never torn between reads.
"""

from __future__ import annotations

import dataclasses
import math

from zelos_extension_xcp.a2l import Signal

#: UPLOAD's length is one byte.
MAX_UPLOAD = 255


@dataclasses.dataclass
class Run:
    """One read: `size` bytes at (`ext`, `address`), each signal at its offset."""

    ext: int
    address: int
    size: int
    signals: list[tuple[Signal, int]]


def limit(max_cto: int, block_mode: bool) -> int:
    """Largest read in one command: one response packet, or UPLOAD's limit in block mode."""
    return MAX_UPLOAD if block_mode else max_cto - 1


def runs(signals: list[Signal], most: int) -> tuple[list[Run], dict[str, str]]:
    """`signals` merged into runs of at most `most` bytes, and those that fit none, with
    the reason."""
    out: list[Run] = []
    skipped: dict[str, str] = {}
    for s in sorted(signals, key=lambda s: (s.ext, s.address, s.size)):
        if s.size > most:
            skipped[s.name] = (
                f"{s.size} bytes do not fit one upload of this ECU ({most} bytes, no block mode)"
            )
            continue
        run = out[-1] if out else None
        end = s.address + s.size
        if (
            run is not None
            and run.ext == s.ext
            and s.address <= run.address + run.size
            and end - run.address <= most
        ):
            run.size = max(run.size, end - run.address)
            run.signals.append((s, s.address - run.address))
        else:
            out.append(Run(s.ext, s.address, s.size, [(s, 0)]))
    return out, skipped


def split(run: Run) -> list[Run]:
    """One run per signal of `run`."""
    return [Run(s.ext, s.address, s.size, [(s, 0)]) for s, _ in run.signals]


def frames(group_runs: list[Run], max_cto: int) -> int:
    """Command and response packets per cycle: two for a `SHORT_UPLOAD`; `SET_MTA`, its
    answer, `UPLOAD` and one packet per `max_cto - 1` bytes for a longer run."""
    short = max_cto - 1
    return sum(2 if r.size <= short else 3 + math.ceil(r.size / short) for r in group_runs)
