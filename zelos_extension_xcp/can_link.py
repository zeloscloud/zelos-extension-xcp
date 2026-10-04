"""XCP on CAN: the bus, the CAN ids and the DAQ bus-load estimate.

The bus comes from `zelos_can.bus`. The extension owns the bus: no DBC
decode, no raw frame trace.
"""

from __future__ import annotations

import math
from typing import Any

import can
from zelos_can.bus import BUS_DEFAULTS, open_python_can_bus, prepare_bus_config

#: Bit 31 of a CAN id in pyxcp marks a 29-bit extended id. The A2L catalog
#: carries it as the `<key>_extended` sibling.
EXTENDED = 0x8000_0000
MAX_STANDARD = 0x7FF
MAX_EXTENDED = 0x1FFF_FFFF

#: CAN FD data lengths.
FD_LENGTHS = (8, 12, 16, 20, 24, 32, 48, 64)


def a2l_can(catalog: dict[str, Any] | None, link: dict[str, Any]) -> dict[str, Any] | None:
    """The A2L's XCP on CAN section in use, or None.

    An A2L may describe the ECU on several CAN buses. Then the configured
    Command and Response CAN ID pick the section; ValueError when they are
    not both set or match none.
    """
    found = [t for t in (catalog or {}).get("transports", []) if t.get("protocol") == "CAN"]
    if len(found) < 2:
        return found[0] if found else None
    listed = ", ".join(
        f"{id_text(a2l_id(t, 'can_id_master'))}/{id_text(a2l_id(t, 'can_id_slave'))}" for t in found
    )
    configured = _configured(link)
    if None in configured:
        raise ValueError(
            f"the A2L describes XCP on CAN {len(found)} times ({listed}): "
            "set the Command and Response CAN ID of the bus in use"
        )
    for t in found:
        if (a2l_id(t, "can_id_master"), a2l_id(t, "can_id_slave")) == configured:
            return t
    raise ValueError(
        f"Command/Response CAN ID {id_text(configured[0])}/{id_text(configured[1])} match "
        f"none of the A2L's XCP on CAN sections ({listed})"
    )


def parse_id(text: str) -> int:
    return int(text, 16)


def a2l_id(section: dict[str, Any], key: str) -> int | None:
    """CAN id `key` of an A2L CAN section, bit 31 set when extended; None when not stated."""
    value = section.get(key)
    if value is None:
        return None
    return int(value) | (EXTENDED if section.get(f"{key}_extended") else 0)


def _configured(link: dict[str, Any]) -> tuple[int | None, int | None]:
    """(command id, response id) from the config, None when not set."""
    extended = bool(link.get("extended_ids"))
    out = []
    for key, label in (("tx_id", "Command CAN ID"), ("rx_id", "Response CAN ID")):
        text = (link.get(key) or "").strip()
        if not text:
            out.append(None)
            continue
        value = parse_id(text)
        limit = MAX_EXTENDED if extended else MAX_STANDARD
        if value > limit:
            kind = "extended" if extended else "standard (turn on Extended IDs)"
            raise ValueError(f"{label} {text} does not fit a {kind} CAN id")
        out.append(value | EXTENDED if extended else value)
    return out[0], out[1]


def ids(link: dict[str, Any], catalog: dict[str, Any] | None) -> tuple[int, int]:
    """(command id, response id), bit 31 set for extended ids.

    From the config when given, else from the A2L. Raises ValueError when
    neither states one: ids are never guessed.
    """
    section = a2l_can(catalog, link) or {}
    out = []
    for value, a2l_key, label in zip(
        _configured(link),
        ("can_id_master", "can_id_slave"),
        ("Command CAN ID", "Response CAN ID"),
        strict=True,
    ):
        if value is None:
            value = a2l_id(section, a2l_key)
        if value is None:
            raise ValueError(
                f"no {label}: set it on the ECU, or state {a2l_key.upper()} in the A2L's "
                "XCP on CAN section"
            )
        out.append(value)
    return out[0], out[1]


def id_text(can_id: int | None) -> str:
    if can_id is None:
        return "none"
    raw = can_id & ~EXTENDED
    return f"0x{raw:08X}x" if can_id & EXTENDED else f"0x{raw:03X}"


def max_dlc_required(link: dict[str, Any], catalog: dict[str, Any] | None) -> bool:
    """The A2L asks for command frames at MAX_DLC (the section or its CAN FD block)."""
    section = a2l_can(catalog, link) or {}
    return bool(
        section.get("max_dlc_required") or (section.get("can_fd") or {}).get("max_dlc_required")
    )


def foreign_ids(
    link: dict[str, Any], catalog: dict[str, Any] | None, lists: int, channels: list[int], rx: int
) -> str | None:
    """Why DAQ lists 0..`lists`-1 on event `channels` cannot be received on `rx`, or None.

    The master receives on the response id only: a DAQ list or event the A2L
    assigns another id would never arrive.
    """
    section = a2l_can(catalog, link) or {}
    for kind, entries, wanted in (
        ("DAQ list", section.get("daq_list_can_ids") or [], range(lists)),
        ("event", section.get("event_can_ids") or [], channels),
    ):
        for number, can_id, extended, _ in entries:
            got = int(can_id) | (EXTENDED if extended else 0)
            if number in wanted and got != rx:
                return (
                    f"the A2L sends {kind} {number} on CAN id {id_text(got)}, not the response "
                    f"id {id_text(rx)}; receiving on several ids is not supported"
                )
    return None


def fd(link: dict[str, Any], catalog: dict[str, Any] | None) -> bool:
    """CAN FD in use. An A2L CAN section and the CAN-FD Mode setting must agree."""
    section = a2l_can(catalog, link)
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
    section = a2l_can(catalog, link) or {}
    nominal = link.get("bitrate") or section.get("bitrate")
    if not nominal:
        return None
    data = (section.get("can_fd") or {}).get("data_bitrate") or nominal
    return int(nominal), int(data)


def open_bus(link: dict[str, Any], name: str) -> can.BusABC:
    """Open the ECU's bus with the zelos-can factory; own frames are not received.

    Over ssh they always are; the response-id filter drops them.
    """
    own = link.get("interface") == "zelos-ssh-socketcan"
    advanced = {**BUS_DEFAULTS, "receive_own_messages": own, "log_raw_frames": False}
    config = prepare_bus_config({**link, "name": name}, advanced=advanced)
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
