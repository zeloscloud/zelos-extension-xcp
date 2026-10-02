"""Demo ECU model: a small traction inverter, as one table.

The table drives both the simulator's memory and the shipped A2L
(`a2l.py`). Every value is a deterministic function of ECU time, so a test
can compute exactly what the ECU sent.

Each variable belongs to a task (its default event). Its memory holds the
value from that task's last run: at ECU time `t` a variable of a task with
cycle `c` reads `f(floor(t / c) * c)`, whichever event samples it.

Physics, with `s` seconds since precharge ended and drive-cycle phase
`th = 2*pi*s/T`:

- speed `1500 * (1 - cos th)` rpm, torque `150 * sin th` Nm, so
  `torque = J * d(omega)/dt` holds for the inertia `J` below
- DC power = mechanical power + losses; DC current = power / DC voltage
- energy is the closed-form integral of DC power
- phase currents come from `id`/`iq` at the electrical angle; they sum to 0
- cell voltages sum to the pack voltage; the DC bus precharges to it
"""

from __future__ import annotations

import math
import re
import struct
from collections.abc import Callable
from dataclasses import dataclass, field

EPK = "ZELOS_XCP_DEMO_V1"
EPK_ADDRESS = 0x8000_0000

#: XCP on CAN defaults: master-to-slave id, slave-to-master id, nominal bitrate.
CAN_ID_MASTER, CAN_ID_SLAVE, CAN_BAUDRATE = 0x7F0, 0x7F1, 500_000
CAN_MAX_BUS_LOAD = 50  # %, what the A2L grants XCP on the bus

#: Event channels: (name, cycle in ms). The channel is the index.
EVENTS = (("10ms", 10), ("100ms", 100))
EV_10MS, EV_100MS = 0, 1

DTYPES = {
    "UBYTE": "B",
    "SBYTE": "b",
    "UWORD": "H",
    "SWORD": "h",
    "ULONG": "I",
    "SLONG": "i",
    "A_UINT64": "Q",
    "A_INT64": "q",
    "FLOAT32_IEEE": "f",
    "FLOAT64_IEEE": "d",
}

T_PRE = 1.0  # s of precharge after power-up
T_CYCLE = 20.0  # s, drive cycle
TQ_PK = 150.0  # Nm
W_MAX = 3000 * 2 * math.pi / 60  # rad/s
J = TQ_PK * T_CYCLE / (W_MAX * math.pi)  # kg m^2
P_IDLE = 50.0  # W, switching loss while running
K_CU = 0.02  # W/Nm^2, copper loss
KT = 0.5  # Nm/A
POLE_PAIRS = 4
N_CELLS = 96
STATES = ((0, "OFF"), (1, "PRECHARGE"), (2, "READY"), (3, "RUN"), (4, "FAULT"))


def physics(t_ns: int) -> dict:
    """Physical state at ECU time `t_ns`."""
    t = t_ns / 1e9
    run = t >= T_PRE
    s = max(t - T_PRE, 0.0)
    th = 2 * math.pi * s / T_CYCLE
    speed = 1500 * (1 - math.cos(th))
    w = speed * 2 * math.pi / 60
    torque = TQ_PK * math.sin(th)
    theta_e = POLE_PAIRS * (W_MAX / 2) * (s - T_CYCLE / (2 * math.pi) * math.sin(th))
    v_batt = 400 + 5 * math.sin(2 * math.pi * t / 7)
    v_dc = v_batt if run else v_batt * t / T_PRE  # bus precharges to the pack
    p_loss = P_IDLE + K_CU * torque**2 if run else 0.0
    p_dc = torque * w + p_loss
    i_dc = p_dc / v_dc if v_dc else 0.0
    energy = (
        0.5 * J * w**2
        + P_IDLE * s
        + K_CU * TQ_PK**2 * (s / 2 - T_CYCLE / (8 * math.pi) * math.sin(2 * th))
    )
    iq = torque / KT
    i_d = -0.1 * (speed - 2000) if speed > 2000 else 0.0
    ia = i_d * math.cos(theta_e) - iq * math.sin(theta_e)
    ib = i_d * math.cos(theta_e - 2 * math.pi / 3) - iq * math.sin(theta_e - 2 * math.pi / 3)
    v_amp = 0.45 * v_dc * min(speed / 3000, 1.0)
    coolant = 45 + 5 * math.sin(2 * math.pi * t / 60)
    igbt = [coolant + 0.02 * p_loss + 0.5 * k for k in range(3)]
    state = 3 if run and abs(torque) >= 1 else 2 if run else 1
    return {
        "k10": t_ns // 10_000_000,
        "k100": t_ns // 100_000_000,
        "t_ns": t_ns,
        "t": t,
        "state": state,
        "precharged": run,
        "derate": igbt[0] > 55,
        "speed": speed,
        "w": w,
        "torque": torque,
        "theta_e": theta_e,
        "v_dc": v_dc,
        "i_dc": i_dc,
        "p_dc": p_dc,
        "energy": energy,
        "id": i_d,
        "iq": iq,
        "i_ph": (ia, ib, -(ia + ib)),
        "v_ph": tuple(v_amp * math.cos(theta_e - k * 2 * math.pi / 3 + 0.35) for k in range(3)),
        "igbt": igbt,
        "diode": [coolant + 0.01 * p_loss + 0.5 * k for k in range(3)],
        "coolant": coolant,
        "soc": max(80 - energy / 3.6e6, 0.0),  # 100 kWh pack
        "cells": [
            v_batt / N_CELLS + 0.003 * math.sin(2 * math.pi * i / N_CELLS + t / 10)
            for i in range(N_CELLS)
        ],
        "cpu": 35 + 10 * math.sin(2 * math.pi * t / 13),
    }


@dataclass(frozen=True)
class Conv:
    """A2L COMPU_METHOD. LINEAR phys = a*raw + b; RAT_FUNC raw = (b*phys + c) / f."""

    name: str
    kind: str
    unit: str
    coeffs: tuple[float, ...] = ()
    table: tuple[tuple[int, str], ...] = ()


RPM = Conv("conv.rpm_q2", "LINEAR", "rpm", (0.25, 0.0))
DEG = Conv("conv.angle", "LINEAR", "deg", (360 / 65536, 0.0))
MNM = Conv("conv.milli_nm", "LINEAR", "Nm", (0.001, 0.0))
MJ = Conv("conv.milli_j", "LINEAR", "J", (0.001, 0.0))
MV = Conv("conv.milli_v", "LINEAR", "V", (0.001, 0.0))
DEGC = Conv("conv.deci_degc", "LINEAR", "degC", (0.1, 0.0))
HALF_PCT = Conv("conv.half_pct", "LINEAR", "%", (0.5, 0.0))
ADC_V = Conv("conv.adc_v", "RAT_FUNC", "V", (0.0, 4.095, 100.0, 0.0, 0.0, 1.0))
STATE = Conv("conv.state", "TAB_VERB", "", table=STATES)


def _lin(x: float, conv: Conv) -> int:
    a, b = conv.coeffs
    return round((x - b) / a)


def _adc(v: float) -> int:
    _, b, c, _, _, f = ADC_V.coeffs
    return round((b * v + c) / f)


@dataclass(frozen=True)
class Var:
    """A scalar or array variable. `raw(p, *idx)` gives the stored value."""

    name: str
    dtype: str
    raw: Callable
    desc: str
    lower: float
    upper: float
    conv: Conv | None = None
    unit: str = ""
    dims: int = 0


@dataclass(frozen=True)
class Flag:
    """A bit of a preceding word, as its own BIT_MASK measurement."""

    name: str
    of: str
    mask: int
    desc: str


@dataclass(frozen=True)
class Struct:
    """TYPEDEF_STRUCTURE. Members are Var or (name, Struct)."""

    name: str
    members: tuple


@dataclass(frozen=True)
class Instance:
    """INSTANCE of a Struct, `count` elements (MATRIX_DIM)."""

    name: str
    struct: Struct
    desc: str
    count: int = 0


@dataclass
class Block:
    """One contiguous memory region, written by one task."""

    ext: int
    base: int
    event: int
    entries: tuple
    size: int = 0
    leaves: list = field(default_factory=list)


@dataclass(frozen=True)
class Leaf:
    """One flattened measurement: where it lives and how to compute it."""

    name: str
    dtype: str
    ext: int
    addr: int
    event: int
    raw: Callable
    mask: int | None = None
    dims: int = 0
    var: Var | None = None  # its definition; None for a Flag


TEMPS = Struct(
    "temps",
    (
        Var("igbt", "SWORD", lambda p, k: _lin(p["igbt"][k], DEGC), "IGBT", -40, 200, DEGC),
        Var("diode", "SWORD", lambda p, k: _lin(p["diode"][k], DEGC), "Diode", -40, 200, DEGC),
    ),
)
PHASE = Struct(
    "phase",
    (
        Var("current", "FLOAT32_IEEE", lambda p, k: p["i_ph"][k], "Current", -500, 500, unit="A"),
        Var("voltage", "FLOAT32_IEEE", lambda p, k: p["v_ph"][k], "Voltage", -500, 500, unit="V"),
        ("temp", TEMPS),
    ),
)


def _status(p: dict) -> int:
    run = p["state"] == 3
    return p["precharged"] | run << 1 | p["derate"] << 2 | (p["k10"] & 1) << 15


BLOCKS = (
    Block(
        0,
        0x0001_0000,
        EV_10MS,
        (
            Var("inv.state", "UBYTE", lambda p: p["state"], "Inverter state", 0, 4, STATE),
            Var(
                "inv.dc.flow",
                "SBYTE",
                lambda p: (p["p_dc"] > 1) - (p["p_dc"] < -1),
                "DC power flow: 1 out of the battery, -1 into it",
                -1,
                1,
            ),
            Var("inv.status_word", "UWORD", _status, "Status bits", 0, 65535),
            Flag("inv.flag.precharged", "inv.status_word", 0x0001, "Precharge done"),
            Flag("inv.flag.running", "inv.status_word", 0x0002, "State RUN"),
            Flag("inv.flag.derate", "inv.status_word", 0x0004, "IGBT over 55 degC"),
            Flag("inv.flag.toggle", "inv.status_word", 0x8000, "Toggles every 10 ms"),
            Var("motor.speed", "SWORD", lambda p: _lin(p["speed"], RPM), "Speed", -8000, 8000, RPM),
            Var(
                "motor.angle",
                "UWORD",
                lambda p: int(p["theta_e"] % (2 * math.pi) / (2 * math.pi) * 65536) & 0xFFFF,
                "Electrical angle",
                0,
                360,
                DEG,
            ),
            Var(
                "inv.dc.voltage_adc",
                "UWORD",
                lambda p: _adc(p["v_dc"]),
                "DC bus ADC",
                0,
                975,
                ADC_V,
            ),
            Var(
                "motor.torque",
                "FLOAT32_IEEE",
                lambda p: p["torque"],
                "Torque",
                -300,
                300,
                unit="Nm",
            ),
            Var(
                "motor.torque_cmd",
                "SLONG",
                lambda p: _lin(p["torque"], MNM),
                "Torque command",
                -300,
                300,
                MNM,
            ),
            Var(
                "motor.ctrl.id_ref",
                "FLOAT32_IEEE",
                lambda p: p["id"],
                "d current",
                -500,
                500,
                unit="A",
            ),
            Var(
                "motor.ctrl.iq_ref",
                "FLOAT32_IEEE",
                lambda p: p["iq"],
                "q current",
                -500,
                500,
                unit="A",
            ),
            Var("inv.dc.voltage", "FLOAT32_IEEE", lambda p: p["v_dc"], "DC bus", 0, 1000, unit="V"),
            Var(
                "inv.dc.current",
                "FLOAT32_IEEE",
                lambda p: p["i_dc"],
                "DC current",
                -1000,
                1000,
                unit="A",
            ),
            Var(
                "sys.tick_10ms",
                "ULONG",
                lambda p: p["k10"] & 0xFFFF_FFFF,
                "10 ms task runs",
                0,
                4294967295,
            ),
            Var(
                "inv.dc.power",
                "FLOAT64_IEEE",
                lambda p: p["v_dc"] * p["i_dc"],
                "DC power",
                -500e3,
                500e3,
                unit="W",
            ),
            Var(
                "motor.omega", "FLOAT64_IEEE", lambda p: p["w"], "Speed", -1000, 1000, unit="rad/s"
            ),
            Var(
                "sys.uptime_ns",
                "A_UINT64",
                lambda p: p["t_ns"],
                "Time of this task run",
                0,
                1.8e19,
                unit="ns",
            ),
            Var(
                "sys.energy",
                "A_INT64",
                lambda p: _lin(p["energy"], MJ),
                "DC energy",
                -1e12,
                1e12,
                MJ,
            ),
            Instance("motor.phase", PHASE, "Phase U, V, W", 3),
        ),
    ),
    Block(
        0,
        0x0001_1000,
        EV_100MS,
        (
            Var(
                "bms.soc",
                "UBYTE",
                lambda p: _lin(p["soc"], HALF_PCT),
                "State of charge",
                0,
                100,
                HALF_PCT,
            ),
            Var(
                "cooling.coolant_temp",
                "SWORD",
                lambda p: _lin(p["coolant"], DEGC),
                "Coolant",
                -40,
                200,
                DEGC,
            ),
            Var(
                "cooling.pump_speed",
                "UWORD",
                lambda p: round(1500 + 20 * (p["igbt"][0] - p["coolant"])),
                "Pump",
                0,
                6000,
                unit="rpm",
            ),
            Var(
                "bms.cell_voltage",
                "UWORD",
                lambda p: [_lin(v, MV) for v in p["cells"]],
                "Cell voltages",
                0,
                5,
                MV,
                dims=N_CELLS,
            ),
        ),
    ),
    Block(
        1,
        0x0001_0000,
        EV_100MS,
        (
            Var("diag.heartbeat", "UBYTE", lambda p: p["k100"] & 0xFF, "100 ms counter", 0, 255),
            Var("diag.cpu_load", "FLOAT32_IEEE", lambda p: p["cpu"], "CPU load", 0, 100, unit="%"),
            Var("diag.signature", "ULONG", lambda p: 0x5A5A_1234, "Constant", 0, 4294967295),
        ),
    ),
)


def _align(offset: int, size: int) -> int:
    return -(-offset // size) * size


def struct_layout(s: Struct) -> tuple[list[tuple[int, str, object]], int, int]:
    """Members as (offset, name, Var | Struct), total size, alignment."""
    out, offset, align = [], 0, 1
    for m in s.members:
        name, item = (m.name, m) if isinstance(m, Var) else m
        if isinstance(item, Var):
            size = a = struct.calcsize(DTYPES[item.dtype])
        else:
            _, size, a = struct_layout(item)
        offset = _align(offset, a)
        out.append((offset, name, item))
        offset += size
        align = max(align, a)
    return out, _align(offset, align), align


def _flatten(prefix: str, s: Struct, addr: int, idx: tuple, block: Block) -> None:
    for offset, name, item in struct_layout(s)[0]:
        path = f"{prefix}.{name}"
        if isinstance(item, Var):
            fn = item.raw
            block.leaves.append(
                Leaf(
                    path,
                    item.dtype,
                    block.ext,
                    addr + offset,
                    block.event,
                    lambda p, f=fn: f(p, *idx),
                    var=item,
                )
            )
        else:
            _flatten(path, item, addr + offset, idx, block)


def _layout(block: Block) -> None:
    """Assign addresses in declaration order, natural alignment."""
    offset, words = 0, {}
    for e in block.entries:
        if isinstance(e, Flag):
            word = words[e.of]
            block.leaves.append(
                Leaf(e.name, word.dtype, block.ext, word.addr, block.event, word.raw, e.mask)
            )
            continue
        if isinstance(e, Var):
            size = struct.calcsize(DTYPES[e.dtype])
            offset = _align(offset, size)
            leaf = Leaf(
                e.name,
                e.dtype,
                block.ext,
                block.base + offset,
                block.event,
                e.raw,
                dims=e.dims,
                var=e,
            )
            block.leaves.append(leaf)
            words[e.name] = leaf
            offset += size * max(e.dims, 1)
            continue
        _, size, a = struct_layout(e.struct)
        offset = _align(offset, a)
        for i in range(max(e.count, 1)):
            path = f"{e.name}[{i}]" if e.count else e.name
            _flatten(path, e.struct, block.base + offset + i * size, (i,), block)
        offset += size * max(e.count, 1)
    block.size = offset


for _b in BLOCKS:
    _layout(_b)

LEAVES = {leaf.name: leaf for b in BLOCKS for leaf in b.leaves}
CYCLE_NS = [ms * 1_000_000 for _, ms in EVENTS]


def task_time(event: int, t_ns: int) -> int:
    """Time of the last run of `event`'s task at or before `t_ns`."""
    c = CYCLE_NS[event]
    return t_ns // c * c


def image(block: Block, t_ns: int) -> bytes:
    """The block's memory at ECU time `t_ns`."""
    p = physics(task_time(block.event, t_ns))
    buf = bytearray(block.size)
    for leaf in block.leaves:
        if leaf.mask is not None:
            continue
        fmt = "<" + DTYPES[leaf.dtype] * max(leaf.dims, 1)
        v = leaf.raw(p)
        struct.pack_into(fmt, buf, leaf.addr - block.base, *(v if leaf.dims else (v,)))
    return bytes(buf)


_ELEMENT = re.compile(r"(.+)\[(\d+)\]$")


def _leaf(name: str) -> tuple[Leaf, int]:
    """The leaf of a measurement or array element `name[i]`, and the index."""
    leaf = LEAVES.get(name)
    if leaf is not None and not leaf.dims:
        return leaf, 0
    m = _ELEMENT.match(name)
    leaf = LEAVES.get(m.group(1)) if m else None
    if leaf is None or not leaf.dims or int(m.group(2)) >= leaf.dims:
        raise KeyError(name)
    return leaf, int(m.group(2))


def conversion(name: str) -> Conv | None:
    leaf = _leaf(name)[0]
    return leaf.var.conv if leaf.var else None


def unit(name: str) -> str:
    """The A2L unit: the conversion's, else PHYS_UNIT, else ''."""
    leaf = _leaf(name)[0]
    conv = conversion(name)
    return conv.unit if conv else leaf.var.unit if leaf.var else ""


def physical(name: str, t_ns: int) -> float | int:
    """What a master writes to the trace: LINEAR and RAT_FUNC applied, else the raw value."""
    raw, conv = expected(name, t_ns), conversion(name)
    if conv is None or conv.kind == "TAB_VERB":
        return raw
    if conv.kind == "LINEAR":
        a, b = conv.coeffs
        return a * raw + b
    a, b, c, d, e, f = conv.coeffs  # raw = (b*p + c) / f, linear only
    assert a == d == e == 0
    return (raw * f - c) / b


def expected(name: str, t_ns: int) -> float | int:
    """A measurement's value at ECU time `t_ns`, as read from memory.

    Datatype decoded and BIT_MASK applied (shifted down), before the A2L
    conversion. Array elements by `name[i]`.
    """
    leaf, idx = _leaf(name)
    block = next(b for b in BLOCKS if leaf in b.leaves)
    size = struct.calcsize(DTYPES[leaf.dtype])
    off = leaf.addr - block.base + idx * size
    (v,) = struct.unpack_from("<" + DTYPES[leaf.dtype], image(block, t_ns), off)
    if leaf.mask is not None:
        v = (v & leaf.mask) >> ((leaf.mask & -leaf.mask).bit_length() - 1)
    return v
