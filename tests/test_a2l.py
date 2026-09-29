"""A2L measurements to physical values, skip reasons and event resolution."""

import math
import struct

import pytest
import zelos_sdk

from zelos_extension_xcp.a2l import resolve, signal


def meas(name="m", datatype="UWORD", conversion=None, **kw):
    return {
        "name": name,
        "address": 0x1000,
        "address_extension": 0,
        "datatype": datatype,
        "byte_order": "little",
        "bit_mask": None,
        "dims": [],
        "conversion": conversion or {"kind": "IDENTICAL"},
        "unit": "V",
        "events": {"default": [0], "fixed": [], "available": []},
        **kw,
    }


def test_conversions():
    ident = signal(meas(datatype="SWORD"))
    assert ident.physical(-5) == -5 and ident.dtype == zelos_sdk.DataType.Int16
    linear = signal(meas(conversion={"kind": "LINEAR", "coeffs": [0.1, -40.0]}))
    assert linear.physical(400) == pytest.approx(0.0)
    assert linear.dtype == zelos_sdk.DataType.Float64
    # raw = (2p + 1) / 1  ->  p = (raw - 1) / 2
    rat = signal(meas(conversion={"kind": "RAT_FUNC", "coeffs": [0, 2, 1, 0, 0, 1]}))
    assert rat.physical(7) == 3.0
    div0 = signal(meas(conversion={"kind": "RAT_FUNC", "coeffs": [0, 0, 1, 0, 1, 0]}))
    assert math.isnan(div0.physical(0))
    verb = signal(
        meas(
            conversion={
                "kind": "TAB_VERB",
                "table": [(0.0, 0.0, "OFF"), (1.0, 1.0, "ON"), (5.0, 7.0, "HIGH")],
                "default": None,
            }
        )
    )
    assert verb.physical(1) == 1 and verb.dtype == zelos_sdk.DataType.UInt16
    assert verb.table == {0: "OFF", 1: "ON", 5: "HIGH", 6: "HIGH", 7: "HIGH"}


def test_bit_mask_and_byte_order():
    flag = signal(meas(datatype="UBYTE", bit_mask=0x30))
    assert flag.physical(0b1110_0101) == 0b10
    big = signal(meas(datatype="ULONG", byte_order="big"))
    assert big.unpack(bytes([0x12, 0x34, 0x56, 0x78])) == 0x12345678
    # decoded by the library in the ECU's (little) order, re-read big endian
    as_little = struct.unpack("<I", bytes([0x12, 0x34, 0x56, 0x78]))[0]
    assert big.reorder(as_little, ecu_little=True) == 0x12345678


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"conversion": {"kind": "FORM", "formula": "X1*2", "formula_inv": None}}, "FORM"),
        ({"conversion": {"kind": "TAB_INTP", "table": [], "default": None}}, "TAB_INTP"),
        ({"conversion": {"kind": "TAB_NOINTP", "table": [], "default": None}}, "TAB_NOINTP"),
        ({"conversion": {"kind": "RAT_FUNC", "coeffs": [1, 0, 0, 0, 0, 1]}}, "quadratic"),
        ({"dims": [4]}, "array"),
        ({"byte_order": None}, "byte order"),
        ({"datatype": "FLOAT16_IEEE"}, "FLOAT16_IEEE"),
        ({"datatype": "SWORD", "bit_mask": 0xFF}, "bit mask"),
        ({"address": None}, "ECU_ADDRESS"),
    ],
)
def test_skip_reasons(changes, reason):
    with pytest.raises(ValueError, match=reason):
        signal(meas(**changes))


CATALOG = {
    "events": [
        {"name": "10ms", "channel": 0, "cycle": 10, "cycle_unit": 6},
        {"name": "100ms", "channel": 1, "cycle": 100, "cycle_unit": 6},
    ],
    "measurements": [
        meas("a"),
        meas("b", events={"default": [1], "fixed": [], "available": []}),
        meas("orphan", events={"default": [], "fixed": [], "available": []}),
        meas("pinned", events={"default": [], "fixed": [1], "available": []}),
        meas("x.y"),
        meas("x_y"),
    ],
}


def test_resolve_events_poll_and_reasons():
    plan = resolve(
        CATALOG,
        [
            {"event": "default", "signals": ["a", "b", "orphan", "missing"]},
            {"event": "10ms", "signals": ["pinned"]},
            {"event": "poll", "rate_ms": 50, "signals": ["a"]},
        ],
    )
    got = {g.event: ([s.name for s in g.signals], g.channel, g.cycle_ns) for g in plan.groups}
    assert got == {
        "10ms": (["a"], 0, 10_000_000),
        "100ms": (["b"], 1, 100_000_000),
        "poll_50": (["a"], None, None),
    }
    assert plan.unknown == ["missing"]
    assert "no default event" in plan.skipped["orphan"]
    assert "fixed to event 100ms" in plan.skipped["pinned"]


def test_resolve_refuses_field_collision():
    with pytest.raises(ValueError, match="both map to field"):
        resolve(CATALOG, [{"event": "default", "signals": ["x.y", "x_y"]}])
