"""DAQ lists for a measurement plan: layout, capacity check and the receive side.

One DAQ list per ECU event. Signals reading the same (extension, address,
type) share one ODT entry. The receive callback runs on the library's receive
thread and only counts and enqueues; decoding happens on the session thread.
"""

from __future__ import annotations

import dataclasses
import logging
import queue
import time
from typing import Any

from zelos_extension_xcp.a2l import Group, Signal
from zelos_extension_xcp.compat import (
    DAQ_TIMESTAMP_SIZE,
    DAQ_TIMESTAMP_UNIT_TO_NS,
    DaqList,
    DaqPolicy,
    FrameCategory,
    daq_header_size,
    make_continuous_blocks,
    pack_odts,
)

logger = logging.getLogger(__name__)

#: Rows held for the session thread before the receive side drops and counts.
QUEUE_LIMIT = 200_000

#: Highest ODT count a list (relative numbering) or a slave (absolute) can hold:
#: PIDs 0xFC-0xFF are responses.
MAX_ODTS = 0xFC
#: The same when the slave flags overload in the PID's MSB.
MAX_ODTS_MSB = 0x80

#: Frames that carry the slave's packet counter.
_SLAVE_FRAMES = frozenset(
    {FrameCategory.RESPONSE, FrameCategory.EVENT, FrameCategory.SERV, FrameCategory.DAQ}
)


class CapacityError(RuntimeError):
    """The ECU cannot hold the selection; nothing was trimmed."""


def entries(group: Group) -> list[tuple[int, int, str]]:
    """Unique (extension, address, type code) of a group, in signal order."""
    return list(dict.fromkeys((s.ext, s.address, s.code) for s in group.signals))


def entry_name(key: tuple[int, int, str]) -> str:
    ext, address, code = key
    return f"{ext}:{address:X}:{code}"


def daq_lists(groups: list[Group], timestamps: bool) -> list[DaqList]:
    """One DAQ list per group, entries named by `entry_name`."""
    return [
        DaqList(
            g.event,
            g.channel,
            False,
            timestamps,
            [(entry_name(k), k[1], k[0], k[2]) for k in entries(g)],
        )
        for g in groups
    ]


def oversized(info: dict[str, Any], max_dto: int, groups: list[Group], hint: str) -> dict[str, str]:
    """Signals larger than one ODT entry of this slave, by name, with the reason.

    The library never splits a value across packets, so these cannot be sampled.
    """
    id_size = daq_header_size(str(info["processor"]["keyByte"]["identificationField"]))
    limit = min(info["resolution"]["maxOdtEntrySizeDaq"], max_dto - id_size)
    return {
        s.name: f"{s.size} bytes do not fit one DAQ packet of this ECU ({limit} data bytes); {hint}"
        for g in groups
        for s in g.signals
        if s.size > limit
    }


def without(groups: list[Group], skipped: dict[str, str]) -> list[Group]:
    """`groups` less the `skipped` signals; groups left empty are dropped."""
    out = []
    for g in groups:
        kept = [s for s in g.signals if s.name not in skipped]
        if kept:
            out.append(dataclasses.replace(g, signals=kept))
    return out


@dataclasses.dataclass
class Layout:
    """How the lists fit the slave: per list ODT count, entry count, packet sizes."""

    odts: list[int]
    odt_entries: list[int]
    odt_bytes: list[list[int]]
    id_size: int
    ts_size: int
    tick_ns: float
    timestamps: bool
    one_ext_per_odt: bool = False
    overload_msb: bool = False


def plan_layout(info: dict[str, Any], max_dto: int, lists: list[DaqList], groups: list[Group]):
    """The ODT layout pyxcp's setup will build, checked against the slave's limits.

    Raises `CapacityError` with the reason when it cannot fit.
    """
    processor, resolution = info["processor"], info["resolution"]
    props = processor["properties"]
    if str(props["configType"]) != "DYNAMIC":
        raise CapacityError("the ECU has static DAQ lists only; dynamic DAQ is required")
    if processor["minDaq"]:
        raise CapacityError(
            f"the ECU has {processor['minDaq']} predefined DAQ lists; not supported"
        )
    max_daq = processor["maxDaq"]
    if max_daq and len(lists) > max_daq:
        raise CapacityError(
            f"{len(lists)} events need {len(lists)} DAQ lists, the ECU has {max_daq}"
        )
    id_size = daq_header_size(str(processor["keyByte"]["identificationField"]))
    # DAQ key byte: address extensions may differ within an ODT, or must be one per
    # ODT, or one per DAQ list (the reserved value is taken as the strictest).
    ae = str(processor["keyByte"]["addressExtension"])
    one_ext_per_odt = ae == "AE_SAME_FOR_ALL_ODT"
    one_ext_per_list = ae not in ("AE_DIFFERENT_WITHIN_ODT", "AE_SAME_FOR_ALL_ODT")
    overload_msb = bool(props["overloadMsb"])
    max_odts = MAX_ODTS_MSB if overload_msb else MAX_ODTS
    ts_size = 0
    tick_ns = 0.0
    fixed = False
    if props["timestampSupported"]:
        mode = resolution["timestampMode"]
        ts_size = DAQ_TIMESTAMP_SIZE.get(str(mode["size"]), 0)
        fixed = bool(mode["fixed"])
        tick_ns = DAQ_TIMESTAMP_UNIT_TO_NS[str(mode["unit"])] * resolution["timestampTicks"]
    container = max_dto - id_size
    max_entry = min(resolution["maxOdtEntrySizeDaq"], container)
    odts, counts, sizes = [], [], []
    total = 0
    for dl, group in zip(lists, groups, strict=True):
        with_ts = bool(ts_size) and (fixed or dl.enable_timestamps)
        first = container - (ts_size if with_ts else 0)
        too_big = [s.name for s in group.signals if s.size > max_entry]
        if too_big:
            raise CapacityError(
                f"event {group.event!r}: {', '.join(too_big)} larger than the ECU's "
                f"{max_entry}-byte ODT entry"
            )
        exts = sorted({s.ext for s in group.signals})
        if one_ext_per_list and len(exts) > 1:
            raise CapacityError(
                f"event {group.event!r} mixes address extensions {', '.join(map(str, exts))}; "
                "the ECU takes one per DAQ list"
            )
        blocks = make_continuous_blocks(dl.measurements, max_entry, max_entry)
        bins = pack_odts(blocks, container, first, one_ext_per_odt)
        odts.append(len(bins))
        counts.append(sum(len(b.entries) for b in bins))
        sizes.append(
            [
                id_size + (ts_size if i == 0 and with_ts else 0) + sum(e.length for e in b.entries)
                for i, b in enumerate(bins)
            ]
        )
        total += len(bins)
        if len(bins) > max_odts:
            raise CapacityError(
                f"event {group.event!r} needs {len(bins)} ODTs, a DAQ list holds {max_odts}"
            )
    if id_size == 1 and total > max_odts:
        raise CapacityError(f"{total} ODTs in all, the ECU numbers at most {max_odts}")
    timestamps = bool(ts_size) and (fixed or any(dl.enable_timestamps for dl in lists))
    return Layout(
        odts, counts, sizes, id_size, ts_size, tick_ns, timestamps, one_ext_per_odt, overload_msb
    )


class Receiver(DaqPolicy):
    """Counts packets and hands complete rows to the session thread.

    Rows are `(list, receive ns, ECU timestamp or None, values)`, values in the
    library's order (see `positions`). Receive time is the host clock on
    Ethernet. On CAN it is the adapter's frame timestamp plus one offset to the
    host clock fixed at the first packet, or the host clock when the adapter
    stamps nothing. `lost` counts gaps in the XCP on Ethernet packet counter;
    CAN has none. A gap while a list is mid-sample may have taken that sample's
    last ODTs and the next one's first: the row is dropped and counted in
    `incomplete`, as the ODT sequence alone would accept it. The same for an
    overload the slave reports (`overloads`: by PID MSB here, by event through
    `drop_in_progress`).
    """

    def __init__(self, lists: list[DaqList], can: bool = False) -> None:
        super().__init__(lists)
        self.can = can
        self.rows: queue.SimpleQueue = queue.SimpleQueue()
        self.lost = 0
        self.incomplete = 0
        self.overflow = 0
        self.last_frame = 0.0  # monotonic
        self.frame_offset: int | None = None
        self.odt_counts: list[int] = []
        self.ts_offset = 0
        self.ts_size = 0
        self._ctr: int | None = None
        self._next: dict[int, int | None] = {}
        self._raw_ts: dict[int, int | None] = {}
        self._torn: set[int] = set()
        self.overloads = 0
        self._rx = 0

    def arm(self, layout: Layout, little: bool, first_pids: list[int]) -> None:
        """Set the packet guard and counters from the layout pyxcp built."""
        self.id_size = layout.id_size
        self.byteorder = "little" if little else "big"
        self.overload_msb = layout.overload_msb
        self.ts_size = layout.ts_size if layout.timestamps else 0
        self.ts_offset = layout.id_size
        self.odt_counts = []
        for d, dl in enumerate(self.daq_lists):
            with_ts = self.ts_size and (self.ts_fixed or dl.enable_timestamps)
            odts = dl.measurements_opt
            self.odt_counts.append(len(odts))
            for o, odt in enumerate(odts):
                data = sum(e.length for e in odt.entries)
                self.length[(d, o)] = (
                    layout.id_size + (self.ts_size if o == 0 and with_ts else 0) + data
                )
                if layout.id_size == 1:
                    self.pid_map[first_pids[d] + o] = (d, o)
            self._next[d] = None

    def _count(self, counter: int) -> None:
        if self.can:
            return
        if self._ctr is not None:
            gap = (counter - self._ctr - 1) & 0xFFFF
            if gap < 0x8000:
                self.lost += gap
                if gap:
                    self.drop_in_progress()
        self._ctr = counter

    def drop_in_progress(self) -> None:
        """Packets were lost: the sample each list is part way through is not emitted."""
        self._torn.update(d for d, odt in self._next.items() if odt)

    def on_overload(self, where: tuple[int, int]) -> None:
        # The first packet after the slave lost some: its list's sample in progress
        # is unreliable, unless this packet starts a new one.
        self.overloads += 1
        self._torn.add(where[0])

    def on_frame(self, cat: int, counter: int) -> None:
        if cat in _SLAVE_FRAMES:
            self._count(counter)

    def on_packet(self, counter: int, timestamp: int, payload: bytes, where: tuple[int, int]):
        self.last_frame = time.monotonic()
        if self.can:
            if not timestamp:
                timestamp = time.time_ns()
            else:
                if self.frame_offset is None:
                    self.frame_offset = time.time_ns() - timestamp
                timestamp += self.frame_offset
        self._rx = timestamp
        daq, odt = where
        expect = self._next.get(daq)
        if odt == 0:
            self._torn.discard(daq)
            if expect:
                self.incomplete += 1
            if self.ts_size:
                start = self.ts_offset
                raw = payload[start : start + self.ts_size]
                self._raw_ts[daq] = int.from_bytes(raw, self.byteorder)
            self._next[daq] = 1 if self.odt_counts[daq] > 1 else None
        elif expect == odt:
            self._next[daq] = odt + 1 if odt + 1 < self.odt_counts[daq] else None
        else:
            if expect != 0:
                self.incomplete += 1
            self._next[daq] = 0  # orphan: counted once, until the next ODT 0

    def on_daq_list(self, daq_list: int, ts0: int, ts1: int, payload: list) -> None:
        if daq_list in self._torn:
            self._torn.discard(daq_list)
            self.incomplete += 1
            return
        if self.rows.qsize() >= QUEUE_LIMIT:
            self.overflow += 1
            return
        self.rows.put((daq_list, self._rx, self._raw_ts.get(daq_list), payload))


def positions(dl: DaqList, group: Group) -> list[int]:
    """Index in the library's row of each signal of `group`, matched by entry name.

    Raises CapacityError if the library dropped or duplicated an entry: the row
    could not be mapped safely.
    """
    headers = [h[0] for h in dl.headers]
    index = {name: i for i, name in enumerate(headers)}
    if len(index) != len(headers) or len(headers) != len(entries(group)):
        raise CapacityError(f"event {group.event!r}: DAQ layout does not match the selection")
    return [index[entry_name((s.ext, s.address, s.code))] for s in group.signals]


def decoder(group: Group, pos: list[int], ecu_little: bool, status: dict[str, int]):
    """Function turning one library row into `{field: physical value}`. A status value
    is left out (the trace writes null) and counted by signal name in `status`."""
    plain = [(s.field, i) for s, i in zip(group.signals, pos, strict=True) if _plain(s, ecu_little)]
    converted = [
        (s, i) for s, i in zip(group.signals, pos, strict=True) if not _plain(s, ecu_little)
    ]

    def decode(values: list) -> dict[str, Any]:
        row = {f: values[i] for f, i in plain}
        for s, i in converted:
            value = s.physical(s.reorder(values[i], ecu_little))
            if value is None:
                status[s.name] = status.get(s.name, 0) + 1
            else:
                row[s.field] = value
        return row

    return decode


def _plain(s: Signal, ecu_little: bool) -> bool:
    return (
        s.kind in ("IDENTICAL", "TAB_VERB")
        and s.mask is None
        and not s.status
        and s.datatype != "FLOAT16_IEEE"
        and (s.size == 1 or s.little == ecu_little)
    )
