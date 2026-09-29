"""XCP on CAN: the bus, the CAN ids and the DAQ bus-load estimate.

The bus comes from the vendored CAN extension's bus factory; only
`zelos_extension_can.bus` is imported (its actions and CLI register on import).
The extension owns the bus: no DBC decode, no raw frame trace.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import can
from zelos_extension_can.bus import BUS_DEFAULTS, open_python_can_bus, prepare_bus_config

#: Bit 31 of a CAN id in the A2L and in pyxcp marks a 29-bit extended id.
EXTENDED = 0x8000_0000
MAX_STANDARD = 0x7FF
MAX_EXTENDED = 0x1FFF_FFFF

#: CAN FD data lengths.
FD_LENGTHS = (8, 12, 16, 20, 24, 32, 48, 64)


def a2l_can(catalog: dict[str, Any] | None) -> dict[str, Any] | None:
    """The A2L's XCP on CAN section, or None."""
    for t in (catalog or {}).get("transports", []):
        if t.get("protocol") == "CAN":
            return t
    return None


def parse_id(text: str) -> int:
    return int(text, 16)


def ids(link: dict[str, Any], catalog: dict[str, Any] | None) -> tuple[int, int]:
    """(command id, response id), bit 31 set for extended ids.

    From the config when given, else from the A2L. Raises ValueError when
    neither states one: ids are never guessed.
    """
    section = a2l_can(catalog) or {}
    extended = bool(link.get("extended_ids"))
    out = []
    for key, a2l_key, label in (
        ("tx_id", "can_id_master", "Command CAN ID"),
        ("rx_id", "can_id_slave", "Response CAN ID"),
    ):
        text = (link.get(key) or "").strip()
        if text:
            value = parse_id(text)
            limit = MAX_EXTENDED if extended else MAX_STANDARD
            if value > limit:
                kind = "extended" if extended else "standard (turn on Extended IDs)"
                raise ValueError(f"{label} {text} does not fit a {kind} CAN id")
            out.append(value | EXTENDED if extended else value)
        elif section.get(a2l_key) is not None:
            out.append(int(section[a2l_key]))
        else:
            raise ValueError(
                f"no {label}: set it on the ECU or give the A2L an XCP on CAN section "
                f"({a2l_key.upper()})"
            )
    return out[0], out[1]


def id_text(can_id: int) -> str:
    raw = can_id & ~EXTENDED
    return f"0x{raw:08X}x" if can_id & EXTENDED else f"0x{raw:03X}"


def fd(link: dict[str, Any], catalog: dict[str, Any] | None) -> bool:
    """CAN FD in use. An A2L CAN section and the CAN-FD Mode setting must agree."""
    section = a2l_can(catalog)
    configured = bool(link.get("fd_mode"))
    if section is None:
        return configured
    described = section.get("can_fd") is not None
    if described and not configured:
        raise ValueError("the A2L describes CAN FD: turn on CAN-FD Mode")
    if configured and not described:
        raise ValueError("CAN-FD Mode is on but the A2L describes classic CAN")
    return described


def bitrates(link: dict[str, Any], catalog: dict[str, Any] | None) -> tuple[int, int] | None:
    """(nominal, data phase) bitrate in bit/s, or None when unknown."""
    section = a2l_can(catalog) or {}
    nominal = link.get("bitrate") or section.get("bitrate")
    if not nominal:
        return None
    data = (section.get("can_fd") or {}).get("data_bitrate") or nominal
    return int(nominal), int(data)


def open_bus(link: dict[str, Any], name: str) -> can.BusABC:
    """Open the ECU's bus with the vendored factory; own frames are not received."""
    advanced = {**BUS_DEFAULTS, "receive_own_messages": False, "log_raw_frames": False}
    config = prepare_bus_config({**link, "name": name}, Path(), advanced)
    return open_python_can_bus(config, name)


def frame_seconds(payload: int, extended: bool, fd: bool, rates: tuple[int, int]) -> float:
    """Time one frame occupies the bus, worst-case bit stuffing.

    Classic CAN: (g + 8n + 13 + floor((g + 8n - 1) / 4)) bits, g = 34 standard,
    54 extended, n = 8 data bytes whatever the payload, as ECUs often pad DAQ
    frames (Davis et al., 2007). CAN FD: the arbitration phase
    at the nominal rate and the data phase (DLC, data, CRC, stuff bits) at the
    data rate, stuffing counted the same way.
    """
    nominal, data_rate = rates
    if not fd:
        g, n = (54 if extended else 34), 64
        return (g + n + 13 + (g + n - 1) // 4) / nominal
    length = next(n for n in FD_LENGTHS if n >= payload) if payload > 8 else payload
    arbitration = (48 if extended else 29) + 13  # through BRS; CRC delimiter, ACK, EOF, IFS
    body = 8 * length + 4 + (17 if length <= 16 else 21)  # ESI, DLC, data, CRC
    return (arbitration + (arbitration - 1) // 4) / nominal + (
        body + 5 + math.ceil(body / 4)
    ) / data_rate
