"""A2L catalog to measurement plan: physical conversion, skip reasons, events.

The catalog comes from `zelos_can.a2l`. A measurement this module cannot read
exactly is skipped with a named reason, never measured approximately.
"""

from __future__ import annotations

import dataclasses
import math
import re
import struct
from collections.abc import Iterable
from typing import Any

import zelos_sdk

from zelos_extension_xcp.constants import field_names
from zelos_extension_xcp.selection import DEFAULT_EVENT, POLL_EVENT, group_names

#: A2L datatype: (library type code, bytes, struct code, trace type).
DATATYPES: dict[str, tuple[str, int, str, zelos_sdk.DataType]] = {
    "UBYTE": ("U8", 1, "B", zelos_sdk.DataType.UInt8),
    "SBYTE": ("I8", 1, "b", zelos_sdk.DataType.Int8),
    "UWORD": ("U16", 2, "H", zelos_sdk.DataType.UInt16),
    "SWORD": ("I16", 2, "h", zelos_sdk.DataType.Int16),
    "ULONG": ("U32", 4, "I", zelos_sdk.DataType.UInt32),
    "SLONG": ("I32", 4, "i", zelos_sdk.DataType.Int32),
    "A_UINT64": ("U64", 8, "Q", zelos_sdk.DataType.UInt64),
    "A_INT64": ("I64", 8, "q", zelos_sdk.DataType.Int64),
    # DAQ reads the raw half as U16; `reorder` turns it into the float.
    "FLOAT16_IEEE": ("U16", 2, "e", zelos_sdk.DataType.Float32),
    "FLOAT32_IEEE": ("F32", 4, "f", zelos_sdk.DataType.Float32),
    "FLOAT64_IEEE": ("F64", 8, "d", zelos_sdk.DataType.Float64),
}

SIGNED = frozenset({"SBYTE", "SWORD", "SLONG", "A_INT64"})

#: Largest value table built from COMPU_VTAB_RANGE rows.
MAX_TABLE_ENTRIES = 1024

#: XCP event channel time unit code to nanoseconds.
CYCLE_UNIT_NS = {
    12: 0.1,
    11: 0.01,
    10: 0.001,
    0: 1,
    1: 10,
    2: 100,
    3: 1_000,
    4: 10_000,
    5: 100_000,
    6: 1_000_000,
    7: 10_000_000,
    8: 100_000_000,
    9: 1_000_000_000,
}


NO_BYTE_ORDER = "multi-byte value with no byte order in the A2L"

#: The reader's warning on a deprecated byte order keyword, `<where>: <name> BYTE_ORDER <kw> ...`.
_DEPRECATED_ORDER = re.compile(r": (\S+) BYTE_ORDER (\w+) is deprecated")


class A2lUnavailable(RuntimeError):
    """The installed zelos-can has no A2L reader."""


def load(path: str) -> dict[str, Any]:
    """The A2L catalog, non-strict. Raises `A2lError` (with file and line) or `A2lUnavailable`."""
    try:
        from zelos_can import a2l
    except ImportError as e:
        raise A2lUnavailable(
            "the installed zelos-can has no A2L reader (zelos_can.a2l); update zelos-can"
        ) from e
    return a2l.load(path, strict=False)


def through(catalog: dict[str, Any], transport: dict[str, Any] | None) -> dict[str, Any]:
    """`catalog` as seen through its `transport` entry: the transport's own DAQ block
    and events, when it has them, overrule the module's."""
    if not transport:
        return catalog
    out = dict(catalog)
    if transport.get("daq") is not None:
        out["daq"] = transport["daq"]
    if transport.get("events"):
        out["events"] = transport["events"]
    return out


@dataclasses.dataclass(frozen=True)
class Signal:
    """One measurement as read from the ECU and written to the trace."""

    name: str
    field: str
    address: int
    ext: int
    datatype: str
    little: bool
    mask: int | None
    kind: str
    coeffs: tuple[float, ...]
    table: dict[int, str] | None
    unit: str
    #: Raw ranges (lo, hi, text) that are status, not physical values: traced as null.
    status: tuple[tuple[float, float, str], ...] = ()

    @property
    def code(self) -> str:
        return DATATYPES[self.datatype][0]

    @property
    def size(self) -> int:
        return DATATYPES[self.datatype][1]

    @property
    def dtype(self) -> zelos_sdk.DataType:
        if self.kind in ("LINEAR", "RAT_FUNC"):
            return zelos_sdk.DataType.Float64
        return DATATYPES[self.datatype][3]

    @property
    def is_float(self) -> bool:
        return self.datatype.startswith("FLOAT")

    def unpack(self, data: bytes) -> int | float:
        """Raw value from ECU memory, in this measurement's byte order."""
        order = "<" if self.little else ">"
        return struct.unpack(order + DATATYPES[self.datatype][2], data)[0]

    def reorder(self, value: int | float, ecu_little: bool) -> int | float:
        """`value` as decoded by the library in the ECU's byte order, re-read in this
        measurement's."""
        half = self.datatype == "FLOAT16_IEEE"
        if not half and (self.size == 1 or ecu_little == self.little):
            return value
        code = "H" if half else DATATYPES[self.datatype][2]
        data = struct.pack(("<" if ecu_little else ">") + code, value)
        return self.unpack(data)

    def status_text(self, raw: int | float) -> str | None:
        """The status string `raw` (bit mask applied) falls in, or None."""
        return next((text for lo, hi, text in self.status if lo <= raw <= hi), None)

    def masked(self, raw: int | float) -> int | float:
        """`raw` with the bit mask applied."""
        return raw if self.mask is None else (int(raw) & self.mask) >> _shift(self.mask)

    def physical(self, raw: int | float) -> int | float | None:
        """Physical value: bit mask, then the conversion. None for a status value."""
        raw = self.masked(raw)
        if self.status_text(raw) is not None:
            return None
        if self.kind == "LINEAR":
            a, b = self.coeffs
            return a * raw + b
        if self.kind == "RAT_FUNC":
            # raw = (b*p + c) / (e*p + f), quadratic terms refused at load
            _, b, c, _, e, f = self.coeffs
            den = e * raw - b
            return (c - f * raw) / den if den else math.nan
        return raw


def _shift(mask: int) -> int:
    return (mask & -mask).bit_length() - 1


def signal(m: dict[str, Any], field: str = "") -> Signal:
    """A `Signal` for catalog measurement `m`. ValueError names why it cannot be read exactly."""
    datatype = m["datatype"]
    if m.get("address") is None:
        raise ValueError("no ECU_ADDRESS in the A2L")
    if m.get("dims"):
        raise ValueError(f"array ({'x'.join(map(str, m['dims']))}) is not supported yet")
    if datatype not in DATATYPES:
        raise ValueError(f"datatype {datatype} is not supported")
    size = DATATYPES[datatype][1]
    if size > 1 and m.get("byte_order") is None:
        raise ValueError(NO_BYTE_ORDER)
    mask = m.get("bit_mask")
    if mask is not None:
        if datatype.startswith("FLOAT") or datatype in SIGNED:
            raise ValueError(f"bit mask on {datatype}")
        if not 0 < mask < 1 << (8 * size):
            raise ValueError(f"bit mask 0x{mask:X} does not fit {datatype}")
    conversion = m.get("conversion") or {}
    kind = conversion.get("kind")
    coeffs: tuple[float, ...] = ()
    table = None
    if kind == "LINEAR":
        coeffs = tuple(float(c) for c in conversion["coeffs"])
    elif kind == "RAT_FUNC":
        coeffs = tuple(float(c) for c in conversion["coeffs"])
        a, b, _, d, e, _ = coeffs
        if a or d:
            raise ValueError("RAT_FUNC with quadratic terms is not supported")
        if not (b or e):
            raise ValueError("RAT_FUNC with no dependence on the value")
    elif kind == "TAB_VERB":
        if datatype.startswith("FLOAT"):
            raise ValueError("value table on a float")
        table = _value_table(conversion["table"])
    elif kind != "IDENTICAL":
        raise ValueError(f"conversion {kind} is not supported")
    return Signal(
        name=m["name"],
        field=field,
        address=int(m["address"]),
        ext=int(m.get("address_extension") or 0),
        datatype=datatype,
        little=m.get("byte_order") != "big",
        mask=mask,
        kind=kind,
        coeffs=coeffs,
        table=table,
        unit=m.get("unit") or "",
        status=tuple((float(lo), float(hi), t) for lo, hi, t in m.get("status_strings") or ()),
    )


def _value_table(rows: Iterable[tuple[float, float, str]]) -> dict[int, str]:
    table: dict[int, str] = {}
    for lo, hi, text in rows:
        if lo != int(lo) or hi != int(hi):
            raise ValueError("value table with non-integer keys")
        if hi - lo + len(table) >= MAX_TABLE_ENTRIES:
            raise ValueError(f"value table over {MAX_TABLE_ENTRIES} entries")
        for key in range(int(lo), int(hi) + 1):
            table.setdefault(key, text)
    return table


def cycle_ns(event: dict[str, Any]) -> int | None:
    """An event's cycle in ns (at least 1), or None when it has none (sporadic) or an
    unknown unit."""
    unit = CYCLE_UNIT_NS.get(event.get("cycle_unit"))
    cycle = event.get("cycle") or 0
    return max(1, round(cycle * unit)) if cycle and unit else None


def event_segment(name: str) -> str:
    """Trace event name for an ECU event."""
    return zelos_sdk.sanitize_name(name, kind="field")


def poll_segment(rate_ms: int) -> str:
    return f"poll_{rate_ms}"


@dataclasses.dataclass
class Group:
    """One trace event: an ECU event channel (DAQ) or a poll rate."""

    event: str
    signals: list[Signal]
    channel: int | None = None
    cycle_ns: int | None = None
    rate_ms: int | None = None

    @property
    def polled(self) -> bool:
        return self.channel is None


@dataclasses.dataclass
class Plan:
    """What an ECU measures, and what it skips and why."""

    groups: list[Group]
    skipped: dict[str, str]
    unknown: list[str]
    #: `default` group names with no default event in the A2L, polled: {name: rate ms}.
    polled_no_default_event: dict[str, int] = dataclasses.field(default_factory=dict)

    @property
    def daq(self) -> list[Group]:
        return [g for g in self.groups if not g.polled]

    @property
    def polls(self) -> list[Group]:
        return [g for g in self.groups if g.polled]


def resolve(catalog: dict[str, Any], measurements: list[dict[str, Any]]) -> Plan:
    """The measurement plan for an ECU's configured groups.

    Unknown and skipped names are reported, the rest measured. Raises
    ValueError on a trace name collision, which refuses the ECU.
    """
    by_name = {m["name"]: m for m in catalog.get("measurements", [])}
    events = {e["name"]: e for e in catalog.get("events", [])}
    by_channel = {e["channel"]: e for e in catalog.get("events", [])}
    skipped: dict[str, str] = {}
    unknown: list[str] = []
    polled: dict[str, int] = {}
    picks: dict[tuple[str, int], list[dict[str, Any]]] = {}
    deprecated = dict(
        m.groups() for w in catalog.get("warnings") or () if (m := _DEPRECATED_ORDER.search(w))
    )

    for group in measurements:
        event = (group.get("event") or DEFAULT_EVENT).strip()
        names = group_names(group)
        explicit = events.get(event)
        for name in names:
            m = by_name.get(name)
            if m is None:
                if name not in unknown:
                    unknown.append(name)
                continue
            if event == POLL_EVENT:
                key = ("poll", int(group.get("rate_ms") or 100))
            elif event == DEFAULT_EVENT:
                ev = m["events"]
                choice = (ev.get("fixed") or ev.get("default") or [None])[0]
                if choice is None or choice not in by_channel:
                    key = ("poll", int(group.get("rate_ms") or 100))
                    polled.setdefault(name, key[1])
                else:
                    key = ("daq", choice)
            elif explicit is None:
                skipped.setdefault(name, f"event {event!r} is not in the A2L")
                continue
            else:
                fixed = m["events"].get("fixed") or []
                if fixed and explicit["channel"] not in fixed:
                    allowed = ", ".join(by_channel[c]["name"] for c in fixed if c in by_channel)
                    skipped.setdefault(name, f"fixed to event {allowed or fixed}")
                    continue
                key = ("daq", explicit["channel"])
            try:
                signal(m)
            except ValueError as e:
                keyword = deprecated.get(name) or deprecated.get("MOD_COMMON")
                reason = str(e)
                if reason == NO_BYTE_ORDER and keyword:
                    reason += f": BYTE_ORDER {keyword} is deprecated and ambiguous"
                skipped.setdefault(name, reason)
                continue
            entries = picks.setdefault(key, [])
            if m not in entries:
                entries.append(m)

    selected = [m["name"] for entries in picks.values() for m in entries]
    fields = field_names(dict.fromkeys(selected))
    groups: list[Group] = []
    owners: dict[str, str] = {}
    for (kind, value), entries in picks.items():
        if kind == "poll":
            group = Group(poll_segment(value), [], rate_ms=value)
            label = f"poll {value} ms"
        else:
            event = by_channel[value]
            group = Group(event_segment(event["name"]), [], channel=value, cycle_ns=cycle_ns(event))
            label = f"event {event['name']!r}"
        if group.event in owners:
            raise ValueError(
                f"{owners[group.event]} and {label} both map to trace event {group.event!r}"
            )
        owners[group.event] = label
        group.signals = [signal(m, fields[m["name"]]) for m in entries]
        groups.append(group)
    polled = {n: r for n, r in polled.items() if n not in skipped}
    return Plan(groups=groups, skipped=skipped, unknown=unknown, polled_no_default_event=polled)
