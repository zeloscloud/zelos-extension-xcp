"""Common CLI utilities."""

import logging
import signal
import sys
from collections.abc import Iterable
from types import FrameType

from zelos_extension_xcp.client import XcpConnection

logger = logging.getLogger(__name__)


def setup_shutdown_handler(connections: Iterable[XcpConnection]) -> None:
    """Stop every connection and exit on SIGTERM or SIGINT."""
    connections = list(connections)

    def shutdown_handler(signum: int, frame: FrameType | None) -> None:
        logger.info("Shutting down XCP extension...")
        for connection in connections:
            connection.stop()
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown_handler)
    signal.signal(signal.SIGINT, shutdown_handler)
