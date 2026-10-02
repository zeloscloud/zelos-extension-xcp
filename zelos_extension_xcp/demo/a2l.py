"""Render the demo ECU's A2L from the model table.

`demo.a2l` beside this file is this module's output for the default ECU
settings; a test keeps the two identical. Regenerate with:

    uv run python -m zelos_extension_xcp.demo.a2l > zelos_extension_xcp/demo/demo.a2l
"""

from __future__ import annotations

import sys
from functools import partial

from zelos_extension_xcp.demo import model
from zelos_extension_xcp.demo.model import BLOCKS, EVENTS, Conv, Flag, Instance, Struct, Var
from zelos_extension_xcp.demo.transport import EthTransport

#: Commands the demo ECU implements beyond the mandatory ones.
OPTIONAL_CMDS = (
    "GET_COMM_MODE_INFO",
    "GET_ID",
    "SET_MTA",
    "UPLOAD",
    "SHORT_UPLOAD",
    "GET_DAQ_CLOCK",
    "GET_DAQ_PROCESSOR_INFO",
    "GET_DAQ_RESOLUTION_INFO",
    "GET_DAQ_EVENT_INFO",
    "GET_DAQ_LIST_MODE",
    "SET_DAQ_PTR",
    "WRITE_DAQ",
    "SET_DAQ_LIST_MODE",
    "START_STOP_DAQ_LIST",
    "START_STOP_SYNCH",
    "FREE_DAQ",
    "ALLOC_DAQ",
    "ALLOC_ODT",
    "ALLOC_ODT_ENTRY",
)
TS_SIZE = {1: "SIZE_BYTE", 2: "SIZE_WORD", 4: "SIZE_DWORD"}
TS_UNIT = {
    10**i: f"UNIT_{u}"
    for i, u in enumerate(
        ("1NS", "10NS", "100NS", "1US", "10US", "100US", "1MS", "10MS", "100MS", "1S")
    )
}


def _num(x: float) -> str:
    return str(int(x)) if float(x).is_integer() and abs(x) < 1e15 else repr(float(x))


def _events(event: int) -> str:
    return (
        " /begin IF_DATA XCP /begin DAQ_EVENT VARIABLE /begin DEFAULT_EVENT_LIST"
        f" EVENT 0x{event:X} /end DEFAULT_EVENT_LIST /end DAQ_EVENT /end IF_DATA"
    )


def _conv_ref(v: Var) -> str:
    return v.conv.name if v.conv else "NO_COMPU_METHOD"


def _unit(v: Var) -> str:
    return f' PHYS_UNIT "{v.unit}"' if v.unit else ""


def _compu(c: Conv) -> list[str]:
    head = f'/begin COMPU_METHOD {c.name} "" {c.kind} "%.3" "{c.unit}"'
    coeffs = " ".join(_num(x) for x in c.coeffs)
    if c.kind == "LINEAR":
        return [f"{head} COEFFS_LINEAR {coeffs} /end COMPU_METHOD"]
    if c.kind == "RAT_FUNC":
        return [f"{head} COEFFS {coeffs} /end COMPU_METHOD"]
    rows = " ".join(f'{k} "{t}"' for k, t in c.table)
    return [
        f"{head} COMPU_TAB_REF {c.name}.table /end COMPU_METHOD",
        f'/begin COMPU_VTAB {c.name}.table "" TAB_VERB {len(c.table)} {rows}'
        ' DEFAULT_VALUE "unknown" /end COMPU_VTAB',
    ]


def _walk_structs(s: Struct, out: dict) -> None:
    for m in s.members:
        if not isinstance(m, Var):
            _walk_structs(m[1], out)
    out.setdefault(s.name, s)


def _typedef(s: Struct) -> list[str]:
    lines, comps = [], []
    layout, size, _ = model.struct_layout(s)
    for offset, name, item in layout:
        if isinstance(item, Var):
            tname = f"{s.name}.{name}"
            lines.append(
                f'/begin TYPEDEF_MEASUREMENT {tname} "{item.desc}" {item.dtype} {_conv_ref(item)}'
                f" 0 0 {_num(item.lower)} {_num(item.upper)}{_unit(item)} /end TYPEDEF_MEASUREMENT"
            )
        else:
            tname = item.name
        comps.append(
            f"  /begin STRUCTURE_COMPONENT {name} {tname} {offset} /end STRUCTURE_COMPONENT"
        )
    return [
        *lines,
        f'/begin TYPEDEF_STRUCTURE {s.name} "" {size}',
        *comps,
        "/end TYPEDEF_STRUCTURE",
    ]


def _protocol(max_cto: int, max_dto: int, cmds: bool = False) -> list[str]:
    return [
        "/begin PROTOCOL_LAYER",
        f"0x0104 1000 2000 0 0 0 0 0 {max_cto} {max_dto} BYTE_ORDER_MSB_LAST"
        " ADDRESS_GRANULARITY_BYTE",
        *(f"OPTIONAL_CMD {c}" for c in OPTIONAL_CMDS if cmds),
        "/end PROTOCOL_LAYER",
    ]


def _daq(max_daq: int, max_dto: int, ts_size: int, ts_unit_ns: int) -> list[str]:
    ts = ts_size and (
        f"/begin TIMESTAMP_SUPPORTED 0x1 {TS_SIZE[ts_size]} {TS_UNIT[ts_unit_ns]}"
        " TIMESTAMP_FIXED /end TIMESTAMP_SUPPORTED"
    )
    return [
        "/begin DAQ",
        f"DYNAMIC {max_daq} {len(EVENTS)} 0 OPTIMISATION_TYPE_DEFAULT ADDRESS_EXTENSION_FREE"
        " IDENTIFICATION_FIELD_TYPE_ABSOLUTE GRANULARITY_ODT_ENTRY_SIZE_DAQ_BYTE"
        f" {min(255, max_dto - 1)} NO_OVERLOAD_INDICATION",
        *([ts] if ts_size else []),
        *(
            f'/begin EVENT "{name}" "{name}" 0x{ch:X} DAQ 0xFF {ms} 6 0 CONSISTENCY EVENT'
            " /end EVENT"
            for ch, (name, ms) in enumerate(EVENTS)
        ),
        "/end DAQ",
    ]


def render(
    port: int = 5555,
    max_daq: int = 16,
    timestamp_size: int = 4,
    timestamp_unit_ns: int = 1000,
    can_id_master: int = model.CAN_ID_MASTER,
    can_id_slave: int = model.CAN_ID_SLAVE,
    can_extended: bool = False,
    can_fd: bool = False,
    bitrate: int = model.CAN_BAUDRATE,
) -> str:
    """The A2L text for a demo ECU with these settings."""
    leaves = {leaf.name: leaf for b in BLOCKS for leaf in b.leaves}
    convs, structs, body = {}, {}, []
    for block in BLOCKS:
        for e in block.entries:
            if isinstance(e, Var):
                if e.conv:
                    convs[e.conv.name] = e.conv
                leaf = leaves[e.name]
                ext = f" ECU_ADDRESS_EXTENSION {block.ext}" if block.ext else ""
                dims = f" MATRIX_DIM {e.dims}" if e.dims else ""
                body.append(
                    f'/begin MEASUREMENT {e.name} "{e.desc}" {e.dtype} {_conv_ref(e)} 0 0'
                    f" {_num(e.lower)} {_num(e.upper)} ECU_ADDRESS 0x{leaf.addr:X}{ext}{dims}"
                    f"{_unit(e)}{_events(block.event)} /end MEASUREMENT"
                )
            elif isinstance(e, Flag):
                leaf = leaves[e.name]
                ext = f" ECU_ADDRESS_EXTENSION {block.ext}" if block.ext else ""
                body.append(
                    f'/begin MEASUREMENT {e.name} "{e.desc}" {leaf.dtype} NO_COMPU_METHOD 0 0 0 1'
                    f" ECU_ADDRESS 0x{leaf.addr:X}{ext} BIT_MASK 0x{e.mask:X}"
                    f"{_events(block.event)} /end MEASUREMENT"
                )
            elif isinstance(e, Instance):
                _walk_structs(e.struct, structs)
                addr = min(v.addr for k, v in leaves.items() if k.startswith(e.name + "["))
                ext = f" ECU_ADDRESS_EXTENSION {block.ext}" if block.ext else ""
                dims = f" MATRIX_DIM {e.count}" if e.count else ""
                body.append(
                    f'/begin INSTANCE {e.name} "{e.desc}" {e.struct.name} 0x{addr:X}{ext}{dims}'
                    f"{_events(block.event)} /end INSTANCE"
                )
    for s in structs.values():
        for m in s.members:
            if isinstance(m, Var) and m.conv:
                convs[m.conv.name] = m.conv
    size = 64 if can_fd else 8  # CAN, the primary transport, is the global default
    daq = partial(_daq, max_daq, ts_size=timestamp_size, ts_unit_ns=timestamp_unit_ns)
    ext = 0x8000_0000 if can_extended else 0  # bit 31: extended id
    fd = "/begin CAN_FD MAX_DLC 64 CAN_FD_DATA_TRANSFER_BAUDRATE 2000000 /end CAN_FD"
    eth = [
        line
        for kind in ("UDP", "TCP")
        for line in (
            f'/begin XCP_ON_{kind}_IP 0x0104 {port} ADDRESS "127.0.0.1"',
            *_protocol(EthTransport.max_cto, EthTransport.max_dto),
            *daq(EthTransport.max_dto),
            f"/end XCP_ON_{kind}_IP",
        )
    ]
    lines = [
        "/* Demo ECU of the Zelos XCP extension. Generated by zelos_extension_xcp/demo/a2l.py. */",
        "ASAP2_VERSION 1 71",
        '/begin PROJECT demo "Zelos XCP demo ECU"',
        '/begin HEADER "" VERSION "1" /end HEADER',
        '/begin MODULE demo "Demo traction inverter"',
        '/begin MOD_COMMON ""',
        "BYTE_ORDER MSB_LAST",
        "ALIGNMENT_BYTE 1",
        "ALIGNMENT_WORD 2",
        "ALIGNMENT_LONG 4",
        "ALIGNMENT_INT64 8",
        "ALIGNMENT_FLOAT32_IEEE 4",
        "ALIGNMENT_FLOAT64_IEEE 8",
        "/end MOD_COMMON",
        '/begin MOD_PAR ""',
        f'EPK "{model.EPK}"',
        f"ADDR_EPK 0x{model.EPK_ADDRESS:X}",
        "/end MOD_PAR",
        "/begin IF_DATA XCP",
        *_protocol(size, size, cmds=True),
        *daq(size),
        "/begin XCP_ON_CAN 0x0104",
        f"CAN_ID_MASTER 0x{can_id_master | ext:X}",
        f"CAN_ID_SLAVE 0x{can_id_slave | ext:X}",
        f"BAUDRATE {bitrate}",
        *([fd] if can_fd else []),
        f"MAX_BUS_LOAD {model.CAN_MAX_BUS_LOAD}",
        "MEASUREMENT_SPLIT_ALLOWED",
        *_protocol(size, size),
        "/end XCP_ON_CAN",
        *eth,
        "/end IF_DATA",
        *(line for c in convs.values() for line in _compu(c)),
        *(line for s in structs.values() for line in _typedef(s)),
        *body,
        "/end MODULE",
        "/end PROJECT",
    ]
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    sys.stdout.write(render())
