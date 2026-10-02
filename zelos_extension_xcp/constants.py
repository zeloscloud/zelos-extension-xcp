"""Shared constants and the trace-naming rules for the XCP extension."""

from collections.abc import Collection, Iterable
from enum import StrEnum

import zelos_sdk

#: Default leading trace-source name (`advanced.prefix`).
DEFAULT_PREFIX = "XCP"

#: Source the extension's logs take when the prefix is cleared; with a prefix
#: they land in it as `<prefix>/log`. Both names are reserved as ECU names.
LOG_SOURCE_NAME = "xcp_log"
RESERVED_ECU_NAMES = ("log", LOG_SOURCE_NAME)


class Interface(StrEnum):
    """How an ECU is reached: a CAN interface, XCP on Ethernet, or the demo ECU."""

    ZELOS_SOCKETCAN = "zelos-socketcan"
    ZELOS_SSH_SOCKETCAN = "zelos-ssh-socketcan"
    SOCKETCAN = "socketcan"
    PCAN = "pcan"
    KVASER = "kvaser"
    VECTOR = "vector"
    SLCAN = "slcan"
    OTHER = "other"
    UDP = "udp"
    TCP = "tcp"
    DEMO = "demo"


#: XCP on CAN interfaces, each opened by the zelos-can bus factory.
CAN_INTERFACES = frozenset(
    {
        Interface.ZELOS_SOCKETCAN,
        Interface.ZELOS_SSH_SOCKETCAN,
        Interface.SOCKETCAN,
        Interface.PCAN,
        Interface.KVASER,
        Interface.VECTOR,
        Interface.SLCAN,
        Interface.OTHER,
    }
)


#: An ECU's `interface` as configured (the label the form shows) -> how it is
#: reached: a python-can interface for XCP on CAN.
INTERFACES = {
    "SocketCAN (Zelos)": Interface.ZELOS_SOCKETCAN,
    "SocketCAN over SSH (Zelos)": Interface.ZELOS_SSH_SOCKETCAN,
    "SocketCAN (python-can)": Interface.SOCKETCAN,
    "PCAN": Interface.PCAN,
    "Kvaser": Interface.KVASER,
    "Vector": Interface.VECTOR,
    "slcan (serial)": Interface.SLCAN,
    "Other (python-can)": Interface.OTHER,
    "XCP on UDP": Interface.UDP,
    "XCP on TCP": Interface.TCP,
    "Demo": Interface.DEMO,
}


class DemoTransport(StrEnum):
    CAN = "can"
    UDP = "udp"
    TCP = "tcp"


#: The ECU `--demo` and Auto-configure add: the demo ECU on CAN, a small selection
#: on each of its A2L's events (about 14 % of a 500 kbit/s bus).
DEMO_ECU = {
    "name": "demo",
    "interface": "Demo",
    "demo_transport": "can",
    "measurements": [
        {
            "event": "default",
            "signals": [
                "inv.state",
                "inv.flag.running",
                "inv.flag.derate",
                "motor.speed",
                "motor.angle",
                "motor.torque",
                "motor.ctrl.iq_ref",
                "inv.dc.voltage",
                "inv.dc.current",
                "sys.tick_10ms",
                "bms.soc",
                "cooling.coolant_temp",
                "cooling.pump_speed",
                "diag.cpu_load",
            ],
        }
    ],
}


def trace_layout(prefix: str, ecu: str) -> tuple[str, str | None]:
    """The one trace-naming rule: (source name, event prefix or None) for an ECU.

    With a prefix, one shared source carries every ECU and an ECU's events read
    `<ecu>/<event>`. Cleared, each ECU owns its source and its events are
    unprefixed.
    """
    if prefix:
        return prefix, ecu
    return ecu, None


def name_error(value: str, label: str, reserved: Collection[str] = ()) -> str | None:
    """Why user-typed `value` is not a legal trace name, or None if it is.

    Names become trace path segments, so a separator (`/ . @ :`) would silently
    re-nest the tree. Anything the SDK sanitizer rewrites is rejected, not renamed.
    """
    if not value:
        return None  # cleared prefix / unset ECU name; the caller decides
    if value in reserved:
        return f"Invalid {label} {value!r}: reserved for the extension's own logs."
    clean = zelos_sdk.sanitize_name(value, kind="source")
    if clean == value:
        return None
    offender = next((c for c, ok in zip(value, clean, strict=False) if c != ok), value[-1])
    return (
        f"Invalid {label} {value!r}: {offender!r} is not allowed. "
        "Use letters, digits, space, '_' or '-'."
    )


def field_name(symbol: str) -> str:
    """Trace field name for an A2L symbol: `.` to `_`, `[i]` to `_i`.

    `motor.ctrl.id_ref` reads `motor_ctrl_id_ref`, `cell_v[3]` reads `cell_v_3`.
    The SDK sanitizer is the rewrite; it is not hand-rolled here.
    """
    return zelos_sdk.sanitize_name(symbol, kind="field")


def field_names(symbols: Iterable[str]) -> dict[str, str]:
    """Field name per A2L symbol. Two symbols that rewrite alike are a load error.

    The trace layer treats identical names as one signal, so a collision would
    silently merge two measurements.
    """
    fields: dict[str, str] = {}
    owner: dict[str, str] = {}
    for symbol in symbols:
        name = field_name(symbol)
        if name in owner and owner[name] != symbol:
            raise ValueError(
                f"A2L symbols {owner[name]!r} and {symbol!r} both map to field {name!r}"
            )
        owner[name] = symbol
        fields[symbol] = name
    return fields
