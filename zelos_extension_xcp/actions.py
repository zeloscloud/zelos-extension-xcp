"""Free-floating XCP action functions registered under `<ACTION_PREFIX>/<name>`.

One global namespace keyed by an `ecu` parameter, not per-ECU action paths:
consumers get one stable surface however many ECUs are configured, and discover
the ECUs through `list_ecus`.

Functions read the shared `XCP_ECUS` registry, which `cli/app.py` populates at
startup. Free functions, not methods, so `@action.select("ecu", choices=...)`
can reference a module-level callable at decoration time.
"""

from __future__ import annotations

import inspect
import logging
import sys
from typing import TYPE_CHECKING, Any

from zelos_sdk.actions import ActionsRegistry, action

from zelos_extension_xcp.constants import Transport

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
    "Every configured ECU with its transport, endpoint and session state: the "
    "names the other actions accept as `ecu`.",
)
def list_ecus() -> dict[str, Any]:
    ecus = [
        {
            "name": name,
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
    "Session state, last error and counters for one ECU. Reads nothing from the ECU.",
)
@action.select("ecu", title="ECU", choices=_available_ecus)
def get_status(ecu: str) -> dict[str, Any]:
    return _get_ecu(ecu).status()


# ─── Config form ────────────────────────────────────────────────────────────


@action(
    "Auto-configure",
    "One demo ECU, for the config form's Auto-configure button. Review it, then save and start.",
    standalone=True,
)
def auto_config() -> dict[str, Any]:
    """The app's auto-configure contract: the keys of `config` replace the form's.

    Only `ecus` is returned, so whatever is set under Advanced survives.
    """
    return {
        "status": "success",
        "config": {"ecus": [{"name": "demo", "transport": str(Transport.DEMO)}]},
    }


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
