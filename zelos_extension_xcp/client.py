"""XCP connection lifecycle: one ECU, one session.

The transport and protocol calls are seams (`NotImplementedError`) until the
protocol layer lands. The lifecycle around them is final: `start` declares the
trace target, `run` drives the session until `stop`.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from enum import StrEnum
from typing import Any

import zelos_sdk

from zelos_extension_xcp.constants import Transport, trace_layout

logger = logging.getLogger(__name__)

#: Reason every protocol seam gives until the protocol layer lands.
PROTOCOL_PENDING = "XCP protocol layer is not implemented yet"

#: Seconds between checks for a stop while idle.
IDLE_TICK = 0.1


class State(StrEnum):
    STOPPED = "stopped"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    ERROR = "error"


@dataclasses.dataclass
class Metrics:
    """Counters `get_status` reports."""

    rows: int = 0
    missing_odts: int = 0
    errors: int = 0


class XcpConnection:
    """One ECU: transport, XCP session, measurement and trace output."""

    def __init__(
        self,
        name: str,
        transport: str,
        host: str = "",
        port: int = 5555,
        a2l_file: str = "",
        measurements: list[dict[str, Any]] | None = None,
        timeout: float = 1.0,
        retries: int = 1,
        timestamp_mode: str = "auto",
        epk_check: str = "strict",
    ) -> None:
        self.name = name
        self.transport = Transport(transport)
        self.host = host
        self.port = port
        self.a2l_file = a2l_file
        self.measurements = measurements or []
        self.timeout = timeout
        self.retries = retries
        self.timestamp_mode = timestamp_mode
        self.epk_check = epk_check

        self.state = State.STOPPED
        self.last_error = ""
        self.metrics = Metrics()
        self._running = False
        # Protocol-stack session, set by `_connect`.
        self._session: Any = None
        self._source: zelos_sdk.TraceSource | None = None
        self._event_prefix: str | None = None

    @property
    def endpoint(self) -> str:
        """`udp://host:port` or `tcp://host:port`, `demo` for the demo ECU."""
        if self.transport == Transport.DEMO:
            return str(self.transport)
        return f"{self.transport}://{self.host}:{self.port}"

    def start(self, prefix: str, source: zelos_sdk.TraceSource | None = None) -> None:
        """Resolve the trace target.

        Args:
            prefix: Trace prefix (see `trace_layout`); empty = cleared.
            source: The shared source named `prefix`. Required with a prefix;
                cleared, the connection creates its own source.
        """
        source_name, self._event_prefix = trace_layout(prefix, self.name)
        self._source = source or zelos_sdk.TraceSource(source_name)
        self._running = True

    def stop(self) -> None:
        """Ask `run` to end the session and return."""
        self._running = False

    def status(self) -> dict[str, Any]:
        """State only: nothing is read from the ECU."""
        return {
            "ecu": self.name,
            "transport": str(self.transport),
            "endpoint": self.endpoint,
            "a2l_file": self.a2l_file,
            "state": str(self.state),
            "error": self.last_error,
            **dataclasses.asdict(self.metrics),
        }

    async def run(self) -> None:
        """Connect, start measurement and receive until `stop`.

        A missing protocol layer is reported once; the ECU then idles in
        `error` so its state stays visible to the actions.
        """
        self.state = State.CONNECTING
        try:
            await self._open_transport()
            await self._connect()
            await self._start_daq()
            self.state = State.CONNECTED
            await self._receive()
        except NotImplementedError as e:
            self._fail(str(e))
            while self._running:
                await asyncio.sleep(IDLE_TICK)
        finally:
            await self._close()
            if self.state != State.ERROR:
                self.state = State.STOPPED
            logger.info("[%s] stopped", self.name)

    def _fail(self, reason: str) -> None:
        self.state = State.ERROR
        self.last_error = reason
        self.metrics.errors += 1
        logger.error("[%s] %s (%s)", self.name, reason, self.endpoint)

    # ─── Seams: transport and protocol ─────────────────────────────────────

    async def _open_transport(self) -> None:
        """Open the demo simulator or the Ethernet socket (UDP or TCP)."""
        raise NotImplementedError(PROTOCOL_PENDING)

    async def _connect(self) -> None:
        """CONNECT, GET_STATUS and the EPK check; sets `_session`."""
        raise NotImplementedError(PROTOCOL_PENDING)

    async def _start_daq(self) -> None:
        """Allocate and start one DAQ list per configured measurement event."""
        raise NotImplementedError(PROTOCOL_PENDING)

    async def _receive(self) -> None:
        """Decode DAQ rows into the trace until `stop`."""
        raise NotImplementedError(PROTOCOL_PENDING)

    async def _close(self) -> None:
        """Stop DAQ, DISCONNECT and close the transport."""
        if self._session is None:
            return
        raise NotImplementedError(PROTOCOL_PENDING)
