"""App-based configuration mode for XCP measurement."""

import asyncio
import contextlib
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import zelos_sdk
from zelos_sdk.extensions import load_config
from zelos_sdk.hooks.logging import TraceLoggingHandler

from .. import ACTION_PREFIX
from .. import actions as xcp_actions
from ..client import XcpConnection
from ..constants import (
    DEFAULT_PREFIX,
    LOG_SOURCE_NAME,
    RESERVED_ECU_NAMES,
    Transport,
    name_error,
    trace_layout,
)
from .utils import setup_shutdown_handler

logger = logging.getLogger(__name__)

#: `advanced` settings and their defaults. One value applies to every ECU.
ADVANCED_DEFAULTS: dict[str, Any] = {
    "prefix": DEFAULT_PREFIX,
    "log_level": "INFO",
    "timestamp_mode": "auto",
    "epk_check": "strict",
    "timeout": 1.0,
    "retries": 1,
}

#: `advanced` keys each XcpConnection takes.
CONNECTION_KEYS = ("timestamp_mode", "epk_check", "timeout", "retries")
ETH_KEYS = ("host", "port", "protocol")

#: The ECU `--demo` adds.
DEMO_ECU = {"name": "demo", "transport": Transport.DEMO}


def resolve_advanced(config: dict[str, Any]) -> dict[str, Any]:
    """Merge the `advanced` object over its defaults.

    An absent `prefix` takes the default; a present-but-empty one clears it.
    """
    return {**ADVANCED_DEFAULTS, **(config.get("advanced") or {})}


def _exit_on(error: str | None) -> None:
    """Exit with a one-line reason when `error` is set."""
    if error:
        logger.error("%s", error)
        sys.exit(1)


def _ecu_name(ecu_config: dict[str, Any]) -> str:
    """The configured name, else `demo` or the sanitized host."""
    name = (ecu_config.get("name") or "").strip()
    if name:
        _exit_on(name_error(name, "ECU Name", RESERVED_ECU_NAMES))
        return name
    if ecu_config.get("transport") == Transport.DEMO:
        return "demo"
    return zelos_sdk.sanitize_name(ecu_config.get("host") or "ecu", kind="source")


def _create_connections(config: dict[str, Any], advanced: dict[str, Any]) -> list[XcpConnection]:
    """One XcpConnection per configured ECU. Names must be legal and unique."""
    ecus = config.get("ecus") or []
    if not ecus:
        _exit_on("No ECUs configured. Add at least one ECU to the 'ecus' array.")

    connections: list[XcpConnection] = []
    for ecu_config in ecus:
        name = _ecu_name(ecu_config)
        if any(c.name == name for c in connections):
            _exit_on(f"Duplicate ECU name '{name}'. Set Name on one of them.")
        transport = ecu_config.get("transport", Transport.ETH)
        a2l_file = ecu_config.get("a2l_file") or ""
        if transport != Transport.DEMO and not a2l_file:
            _exit_on(f"ECU '{name}' has no A2L file.")
        connection = XcpConnection(
            name=name,
            transport=transport,
            a2l_file=a2l_file,
            measurements=ecu_config.get("measurements") or [],
            **{k: ecu_config[k] for k in ETH_KEYS if k in ecu_config},
            **{k: advanced[k] for k in CONNECTION_KEYS},
        )
        connections.append(connection)
        logger.info("Created ECU %s (%s)", name, connection.endpoint)
    return connections


async def _run_connections(connections: list[XcpConnection]) -> None:
    """Run every connection until a stop; stop them all on the way out."""
    try:
        await asyncio.gather(*(c.run() for c in connections))
    finally:
        for connection in reversed(connections):
            connection.stop()


def run_app_mode(demo: bool, file: Path | None) -> None:
    """Run the XCP extension in app-based configuration mode.

    :param demo: Add a demo ECU to the configured ones
    :param file: Optional output file for trace recording
    """
    config = load_config()
    advanced = resolve_advanced(config)
    prefix = str(advanced.get("prefix") or "").strip()
    _exit_on(name_error(prefix, "Prefix"))

    log_level = advanced["log_level"]
    logging.getLogger().setLevel(getattr(logging, log_level, logging.INFO))
    logger.info("Log level set to: %s", log_level)

    if demo:
        logger.info("Demo mode enabled via --demo flag")
        config["ecus"] = [*(config.get("ecus") or []), dict(DEMO_ECU)]

    output_file = None
    if file is not None:
        # --file without a value records to a UTC-stamped file.
        if str(file) == ".":
            file = Path(f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.trz")
        output_file = file
        logger.info("Recording trace to: %s", output_file)

    # Register actions BEFORE init: init advertises them to the agent. The
    # address prefix comes from `init(name=ACTION_PREFIX)`.
    xcp_actions.register_actions(zelos_sdk.actions_registry)

    # Log source first, so init below reuses it. Same layout rule as the data:
    # a prefix means one source for every ECU, cleared each ECU owns its own.
    log_source_name, _ = trace_layout(prefix, LOG_SOURCE_NAME)
    global_source = zelos_sdk.init_global_source(log_source_name)
    shared_source = global_source if prefix else None
    if prefix:
        logger.info("Trace prefix: %s", prefix)
    else:
        logger.info("Trace prefix cleared: one trace source per ECU")

    zelos_sdk.init(name=ACTION_PREFIX, log_level="info", actions=True)

    handler = TraceLoggingHandler(global_source)
    handler.setLevel(logging.INFO)
    logging.getLogger().addHandler(handler)

    with contextlib.ExitStack() as stack:
        if output_file:
            stack.enter_context(zelos_sdk.TraceWriter(str(output_file)))

        connections = _create_connections(config, advanced)
        for connection in connections:
            xcp_actions.XCP_ECUS[connection.name] = connection
            connection.start(prefix, shared_source)
        setup_shutdown_handler(connections)

        count = len(connections)
        logger.info("Starting XCP extension with %d ECU%s", count, "s" if count > 1 else "")
        try:
            asyncio.run(_run_connections(connections))
        except (OSError, ValueError) as e:
            logger.error("XCP session failed: %s", e)
            sys.exit(1)
