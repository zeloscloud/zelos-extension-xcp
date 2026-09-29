"""Common CLI utilities."""

import logging
import signal
import sys
import time
from collections.abc import Iterable
from types import FrameType

from zelos_extension_xcp.client import STOP_BOUND, XcpConnection

logger = logging.getLogger(__name__)


def stop_all(connections: Iterable[XcpConnection]) -> None:
    """Stop every connection, then wait for all of them within one shared bound."""
    connections = list(connections)
    for connection in connections:
        connection.stop()
    deadline = time.monotonic() + STOP_BOUND
    for connection in connections:
        if not connection.join(max(0.0, deadline - time.monotonic())):
            logger.error("[%s] did not stop within %gs", connection.name, STOP_BOUND)


def setup_shutdown_handler(connections: Iterable[XcpConnection]) -> None:
    """Stop every connection and exit on SIGTERM or SIGINT."""
    connections = list(connections)

    def shutdown_handler(signum: int, frame: FrameType | None) -> None:
        logger.info("Shutting down XCP extension...")
        stop_all(connections)
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown_handler)
    signal.signal(signal.SIGINT, shutdown_handler)
