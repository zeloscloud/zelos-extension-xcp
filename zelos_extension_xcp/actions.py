"""Free-floating XCP action functions registered under `<ACTION_PREFIX>/<name>`.

One global namespace keyed by an `ecu` parameter, not per-ECU action paths:
consumers get one stable surface however many ECUs are configured, and discover
the ECUs through `list_ecus`.

Functions read the shared `XCP_ECUS` registry, which `cli/app.py` populates at
startup. Free functions, not methods, so `@action.select("ecu", choices=...)`
can reference a module-level callable at decoration time.
"""

from __future__ import annotations

import copy
import inspect
import logging
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from zelos_can.bus import discovery
from zelos_sdk.actions import ActionsRegistry, action

from zelos_extension_xcp.constants import DEMO_ECU, INTERFACES, Interface
from zelos_extension_xcp.selection import POLL_EVENT

if TYPE_CHECKING:
    from zelos_extension_xcp.client import XcpConnection

logger = logging.getLogger(__name__)

# Shared ECU registry, populated by `cli/app.py`.
XCP_ECUS: dict[str, XcpConnection] = {}


def _available_ecus(*_args: Any) -> list[str]:
    """`choices=` provider for the `ecu` select field, evaluated at form-render time."""
    return sorted(XCP_ECUS.keys())


def _get_ecu(name: str) -> XcpConnection:
    ecu = XCP_ECUS.get(name)
    if ecu is None:
        raise ValueError(f"Unknown ECU '{name}'. Available: {_available_ecus()}")
    return ecu


# ─── Discovery ──────────────────────────────────────────────────────────────


@action(
    "List ECUs",
    "Every configured ECU with its interface, transport, endpoint and session state: the "
    "names the other actions accept as `ecu`.",
    hidden=True,
)
def list_ecus() -> dict[str, Any]:
    ecus = [
        {
            "name": name,
            "interface": str(ecu.interface),
            "transport": str(ecu.transport),
            "endpoint": ecu.endpoint,
            "state": str(ecu.state),
        }
        for name, ecu in sorted(XCP_ECUS.items())
    ]
    return {"ecus": ecus, "count": len(ecus)}


# ─── Per-ECU state ──────────────────────────────────────────────────────────


@action(
    "Get Status",
    "Session state, last error, EPK result, per-event rows and rates, loss counters, "
    "watchdog, reconnects, clock offset and drift, A2L warnings, unknown and skipped "
    "names for one ECU. Reads nothing from the ECU.",
    hidden=True,
)
@action.select("ecu", title="ECU", choices=_available_ecus)
def get_status(ecu: str) -> dict[str, Any]:
    return _get_ecu(ecu).status()


# ─── A2L ────────────────────────────────────────────────────────────────────


@action(
    "List Events", "ECU event channels from the ECU's A2L, with their cycle times.", hidden=True
)
@action.select("ecu", title="ECU", choices=_available_ecus)
def list_events(ecu: str) -> dict[str, Any]:
    events = _get_ecu(ecu).list_events()
    return {"events": events, "count": len(events)}


@action(
    "List Measurements",
    "Measurements in the ECU's A2L whose name contains the search text (any case), "
    "one page at a time: name, unit, datatype and default event.",
    hidden=True,
)
@action.select("ecu", title="ECU", choices=_available_ecus)
@action.text("search", title="Search", default="")
@action.integer("offset", title="Offset", minimum=0, default=0)
@action.integer("limit", title="Limit", minimum=1, maximum=1000, default=100)
def list_measurements(
    ecu: str, search: str = "", offset: int = 0, limit: int = 100
) -> dict[str, Any]:
    return _get_ecu(ecu).list_measurements(search, int(offset), int(limit))


# ─── ECU reads ──────────────────────────────────────────────────────────────


@action(
    "Read",
    "Read one measurement by its A2L name, once, over the running session: physical "
    "value and unit. Reads memory only.",
    hidden=True,
)
@action.select("ecu", title="ECU", choices=_available_ecus)
@action.text("name", title="Measurement")
def read(ecu: str, name: str) -> dict[str, Any]:
    return _get_ecu(ecu).read(name)


@action(
    "Check Selection",
    "Dry run of the configured measurements: per-event signal counts, skipped and unknown "
    "names with reasons, and whether the selection fits the ECU's reported DAQ limits. "
    "Does not start or change measurement.",
    hidden=True,
)
@action.select("ecu", title="ECU", choices=_available_ecus)
def check_selection(ecu: str) -> dict[str, Any]:
    return _get_ecu(ecu).check_selection()


# ─── Config form ────────────────────────────────────────────────────────────


#: Auto-configure: most measurements added to one ECU, and the DAQ share of the CAN bus
#: they may take when `max_bus_load` is not set.
FILL_MAX = 50
FILL_BUS_LOAD = 30.0

#: A2L DAQ timestamp size keyword to bytes.
TIMESTAMP_BYTES = {"SIZE_BYTE": 1, "SIZE_WORD": 2, "SIZE_DWORD": 4}


def _odt_bytes(sizes: list[int], room: int, ts: int) -> list[int]:
    """Estimated DAQ packet sizes: values filled into ODTs in order, after a 1-byte PID;
    the first ODT also holds the timestamp."""
    odts = [ts]
    for n in sizes:
        if odts[-1] + n > room:
            odts.append(0)
        odts[-1] += n
    return [1 + b for b in odts]


def _fill(ecu: dict[str, Any], catalog: dict[str, Any], cap: float) -> tuple[list[str], str]:
    """A2L measurements with an A2L event, by event then A2L order, at most FILL_MAX; on CAN
    only while the estimated DAQ bus load stays within `cap` %. Returns them and why the
    rest were left out. ValueError: the ECU's A2L transport cannot be used."""
    from zelos_extension_xcp import a2l, can_link
    from zelos_extension_xcp.cli.app import ECU_KEYS
    from zelos_extension_xcp.client import XcpConnection
    from zelos_extension_xcp.constants import DemoTransport

    link = {k: v for k, v in ecu.items() if k not in ECU_KEYS}
    conn = XcpConnection("fill", INTERFACES[ecu["interface"]], link=link)
    catalog = a2l.through(catalog, conn._a2l_transport(catalog))
    events = {e["channel"]: e for e in catalog.get("events", [])}
    order = {channel: i for i, channel in enumerate(events)}
    can = conn.transport == DemoTransport.CAN
    rates = None
    if can:
        section = can_link.a2l_can(catalog, link) or {}
        fd = can_link.fd(link, catalog)
        extended = bool(can_link.ids(link, catalog)[1] & can_link.EXTENDED)
        rates = can_link.bitrates(link, catalog)
        room = (section.get("max_dto") or (64 if fd else 8)) - 1
        stamp = ((catalog.get("daq") or {}).get("timestamp") or {}).get("size")
        ts = TIMESTAMP_BYTES.get(stamp, 0)
    candidates = []
    for i, m in enumerate(catalog.get("measurements", [])):
        ev = m["events"]
        channel = (ev.get("fixed") or ev.get("default") or [None])[0]
        if channel not in events or m.get("dims"):
            continue
        try:
            size = a2l.signal(m).size
        except ValueError:  # not measurable exactly
            continue
        if not can or size <= room:
            candidates.append((order[channel], i, channel, m["name"], size))
    candidates.sort()
    picked: list[str] = []
    sizes: dict[int, list[int]] = {}
    loads: dict[int, float] = {}
    over = False
    for _, _, channel, name, size in candidates:
        if len(picked) >= FILL_MAX:
            break
        if rates:
            cycle = a2l.cycle_ns(events[channel])
            packets = _odt_bytes([*sizes.get(channel, []), size], room, ts)
            busy = sum(can_link.frame_seconds(n, extended, fd, rates) for n in packets)
            pct = 100.0 * busy * 1e9 / cycle if cycle else 0.0  # no cycle: not counted
            if sum(loads.values()) - loads.get(channel, 0.0) + pct > cap:
                over = True
                continue
            loads[channel] = pct
        sizes.setdefault(channel, []).append(size)
        picked.append(name)
    left = len(candidates) - len(picked)
    why = f"; {left} left out" if left else ""
    if over:
        why += f" to stay under {cap:g}% bus load"
    if left and len(picked) >= FILL_MAX:
        why += f" (at most {FILL_MAX} per ECU)"
    if can and not rates:
        why += "; bus load not checked: no bitrate on the interface or in the A2L"
    return picked, why


@action(
    "Auto-configure",
    "Fills each ECU that has an A2L file and no measurements with a Default group of its A2L "
    "measurements, within the CAN bus load; with no ECU, one demo ECU. For the config form's "
    "Auto-configure button. Review, then save and start.",
    standalone=True,
    hidden=True,
)
@action.object(
    "config",
    properties={},
    title="Config",
    description="The config form's current (possibly unsaved) data",
    required=False,
)
def auto_config(config: dict[str, Any] | None = None) -> dict[str, Any]:
    """The app's auto-configure contract: the keys of `config` replace the form's.

    Only `ecus` is returned, so whatever is set under Advanced survives.
    """
    ecus = copy.deepcopy((config or {}).get("ecus") or [])
    if not ecus:
        return {"status": "success", "config": {"ecus": [copy.deepcopy(DEMO_ECU)]}}
    cap = ((config or {}).get("advanced") or {}).get("max_bus_load") or FILL_BUS_LOAD
    done = []
    for i, ecu in enumerate(ecus):
        if not isinstance(ecu, dict) or ecu.get("interface") not in INTERFACES:
            continue
        if any(g.get("signals") for g in ecu.get("measurements") or []):
            continue  # already selected
        demo = INTERFACES[ecu["interface"]] == Interface.DEMO
        if not (demo or ecu.get("a2l_file")):
            continue
        name = (ecu.get("name") or "").strip()
        label = f"ECU '{name}'" if name else f"ECU {i + 1}"
        catalog, reply = _form_a2l(config, ["ecus", i])
        if catalog is None:
            return {"status": "error", "message": f"{label}: {reply['message']}"}
        try:
            picked, why = _fill(ecu, catalog, cap)
        except ValueError as e:
            return {"status": "error", "message": f"{label}: {e}"}
        if picked:
            ecu["measurements"] = [{"event": "default", "signals": picked}]
            done.append(f"Added {len(picked)} measurements to {label} (Default group){why}.")
    if not done:
        return {
            "status": "success",
            "config": {"ecus": ecus},
            "message": "Nothing to fill: every ECU with an A2L file already has measurements. "
            "Clear an ECU's Measurements to fill it with a Default group.",
        }
    return {
        "status": "success",
        "config": {"ecus": ecus},
        "message": " ".join([*done, "Review, then Save."]),
    }


@action(
    "List CAN Interfaces",
    "SocketCAN interfaces on the machine running the agent, as choices for an ECU's Channel "
    "field, which also accepts a name typed by hand. Empty on macOS/Windows, which have no "
    "SocketCAN.",
    standalone=True,
    hidden=True,
)
def list_interfaces() -> dict[str, Any]:
    """The app's `action-choices` contract. Reads sysfs only: no socket, no privileges."""
    return {"status": "success", "choices": discovery.list_interfaces()}


#: Most names `list_a2l_measurements` offers; the field takes any name typed by hand.
MAX_CHOICES = 500


def _form_a2l(
    config: dict[str, Any] | None, path: list[Any] | None
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """The catalog of the A2L of the form's ECU at `path`, else None and the reply saying why."""
    from zelos_extension_xcp import a2l
    from zelos_extension_xcp.demo.ecu import A2L_PATH

    ecus = (config or {}).get("ecus")
    i = path[1] if path and len(path) > 1 and path[0] == "ecus" else None
    if not isinstance(ecus, list) or not isinstance(i, int) or not 0 <= i < len(ecus):
        # An app older than the config/path hand-off.
        return None, {
            "status": "success",
            "choices": [],
            "message": "Update the Zelos App to list A2L names.",
        }
    ecu = ecus[i] if isinstance(ecus[i], dict) else {}
    if INTERFACES.get(ecu.get("interface")) == Interface.DEMO:  # always its bundled A2L
        a2l_path = A2L_PATH
    elif ecu.get("a2l_file"):
        a2l_path = Path(ecu["a2l_file"]).expanduser()
    else:
        return None, {
            "status": "success",
            "choices": [],
            "message": "Set this ECU's A2L File to list its names.",
        }
    try:
        return a2l.load(str(a2l_path)), {}
    except (a2l.A2lUnavailable, ValueError, OSError) as e:  # A2lError is a ValueError
        return None, {"status": "error", "message": f"A2L {a2l_path}: {e}"}


@action(
    "List A2L Measurements",
    "Measurement names in an ECU's A2L, as choices for its Signals, with unit and default "
    "event (or poll) beside each. Fed the unsaved form and the field's place in it by the "
    "config form; the demo interface uses the bundled A2L.",
    standalone=True,
    hidden=True,
)
@action.object(
    "config",
    properties={},
    title="Config",
    description="The config form's current (possibly unsaved) data",
    required=False,
)
# Keys and indices mixed (`["ecus", 0, ...]`): untyped items, which the SDK leaves unchecked.
@action.array(
    "path", item=[], title="Path", description="The field's place in config", required=False
)
def list_a2l_measurements(
    config: dict[str, Any] | None = None, path: list[Any] | None = None
) -> dict[str, Any]:
    """The app's `action-choices` contract for `ecus[i].measurements[j].signals[k]`."""
    catalog, reply = _form_a2l(config, path)
    if catalog is None:
        return reply
    channels = {e["channel"]: e["name"] for e in catalog.get("events", [])}
    found = catalog.get("measurements", [])
    choices = []
    for m in found[:MAX_CHOICES]:
        ev = m["events"]
        default = (ev.get("fixed") or ev.get("default") or [None])[0]
        detail = ", ".join(x for x in (m.get("unit"), channels.get(default, POLL_EVENT)) if x)
        choices.append({"value": m["name"], "detail": detail})
    result: dict[str, Any] = {"status": "success", "choices": choices}
    if len(found) > MAX_CHOICES:
        result["message"] = (
            f"The first {MAX_CHOICES} of {len(found)} measurements; type any other name."
        )
    return result


@action(
    "List A2L Events",
    "Event names in an ECU's A2L, as choices for a measurement group's A2L Event, with "
    "cycle time beside each. Fed the unsaved form and the field's place in it by the "
    "config form; the demo interface uses the bundled A2L.",
    standalone=True,
    hidden=True,
)
@action.object(
    "config",
    properties={},
    title="Config",
    description="The config form's current (possibly unsaved) data",
    required=False,
)
# Keys and indices mixed (`["ecus", 0, ...]`): untyped items, which the SDK leaves unchecked.
@action.array(
    "path", item=[], title="Path", description="The field's place in config", required=False
)
def list_a2l_events(
    config: dict[str, Any] | None = None, path: list[Any] | None = None
) -> dict[str, Any]:
    """The app's `action-choices` contract for `ecus[i].measurements[j].a2l_event`."""
    from zelos_extension_xcp import a2l

    catalog, reply = _form_a2l(config, path)
    if catalog is None:
        return reply
    choices = []
    for e in catalog.get("events", []):
        cycle = a2l.cycle_ns(e)
        choice = {"value": e["name"]}
        if cycle:
            choice["detail"] = f"{cycle / 1e6:g} ms"
        choices.append(choice)
    return {"status": "success", "choices": choices}


# ─── Registration helper ────────────────────────────────────────────────────


def register_actions(registry: ActionsRegistry) -> list[str]:
    """Register every @action-decorated free function in this module by its bare
    function name. The leading `XCP/` segment comes from
    `zelos_sdk.init(name=ACTION_PREFIX, actions=True)`.

    Returns the list of registered names (without the service prefix)."""
    module = sys.modules[__name__]
    registered: list[str] = []
    for name, obj in inspect.getmembers(module):
        if name.startswith("_"):
            continue
        if inspect.isfunction(obj) and hasattr(obj, "_action"):
            registry.register(obj, name=name)
            registered.append(name)
    logger.info("Registered %d XCP actions", len(registered))
    return registered
