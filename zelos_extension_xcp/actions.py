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
from typing import TYPE_CHECKING, Any

from zelos_can.bus import discovery
from zelos_sdk.actions import ActionsRegistry, action

from zelos_extension_xcp.constants import DEMO_ECU

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
)
@action.select("ecu", title="ECU", choices=_available_ecus)
def get_status(ecu: str) -> dict[str, Any]:
    return _get_ecu(ecu).status()


# ─── A2L ────────────────────────────────────────────────────────────────────


@action("List Events", "ECU event channels from the ECU's A2L, with their cycle times.")
@action.select("ecu", title="ECU", choices=_available_ecus)
def list_events(ecu: str) -> dict[str, Any]:
    events = _get_ecu(ecu).list_events()
    return {"events": events, "count": len(events)}


@action(
    "List Measurements",
    "Measurements in the ECU's A2L whose name contains the search text (any case), "
    "one page at a time: name, unit, datatype and default event.",
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
)
@action.select("ecu", title="ECU", choices=_available_ecus)
def check_selection(ecu: str) -> dict[str, Any]:
    return _get_ecu(ecu).check_selection()


# ─── Config form ────────────────────────────────────────────────────────────


@action(
    "Auto-configure",
    "One demo ECU, for the config form's Auto-configure button. Review it, then save and start.",
    standalone=True,
)
def auto_config() -> dict[str, Any]:
    """The app's auto-configure contract: the keys of `config` replace the form's,
    `message` is shown under the button.

    Only `ecus` is returned, so whatever is set under Advanced survives.
    """
    from zelos_extension_xcp.demo.ecu import A2L_PATH

    if not A2L_PATH.is_file():
        return {
            "status": "error",
            "message": f"The demo A2L is missing from this install ({A2L_PATH}); reinstall.",
        }
    ecu = copy.deepcopy(DEMO_ECU)
    n = sum(len(g["signals"]) for g in ecu["measurements"])
    message = (
        f"Replaced the ECU list with the built-in demo ECU: {n} signals on their A2L events over a "
        "simulated CAN bus, no hardware. Review, then Save and Start. For a real ECU, pick its "
        "interface and A2L file instead"
    )
    found = discovery.list_interfaces()
    if found:
        message += f"; SocketCAN interfaces on this host: {', '.join(c['value'] for c in found)}"
    return {"status": "success", "config": {"ecus": [ecu]}, "message": message + "."}


@action(
    "List CAN Interfaces",
    "SocketCAN interfaces on the machine running the agent, as choices for an ECU's Channel "
    "field, which also accepts a name typed by hand. Empty on macOS/Windows, which have no "
    "SocketCAN.",
    standalone=True,
)
def list_interfaces() -> dict[str, Any]:
    """The app's `action-choices` contract. Reads sysfs only: no socket, no privileges."""
    return {"status": "success", "choices": discovery.list_interfaces()}


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
