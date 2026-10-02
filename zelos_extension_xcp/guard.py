"""Command allowlist: the one choke point every outgoing XCP command passes.

pyxcp frames every command in `transport.framing.prepare_request`: plain
requests, optional-response requests, block and STIM transfers and multicast.
The guard wraps it, so a refused command raises before a byte is framed or
sent. Measurement only: nothing here can write ECU memory, switch pages,
program, unlock or stimulate.
"""

from __future__ import annotations

from typing import Any

from zelos_extension_xcp.compat import Command

#: The only commands the extension sends.
ALLOWED = frozenset(
    {
        Command.CONNECT,
        Command.DISCONNECT,
        Command.GET_STATUS,
        Command.SYNCH,
        Command.GET_COMM_MODE_INFO,
        Command.GET_ID,
        Command.SET_MTA,
        Command.UPLOAD,
        Command.SHORT_UPLOAD,
        # DAQ configuration, start and stop
        Command.FREE_DAQ,
        Command.ALLOC_DAQ,
        Command.ALLOC_ODT,
        Command.ALLOC_ODT_ENTRY,
        Command.SET_DAQ_PTR,
        Command.WRITE_DAQ,
        Command.SET_DAQ_LIST_MODE,
        Command.START_STOP_DAQ_LIST,
        Command.START_STOP_SYNCH,
        # GET_DAQ_*
        Command.GET_DAQ_CLOCK,
        Command.GET_DAQ_PROCESSOR_INFO,
        Command.GET_DAQ_RESOLUTION_INFO,
        Command.GET_DAQ_LIST_INFO,
        Command.GET_DAQ_EVENT_INFO,
        Command.GET_DAQ_LIST_MODE,
        Command.GET_DAQ_PACKED_MODE,
    }
)

#: SET_DAQ_LIST_MODE mode bit: the list runs in the STIM direction (master writes).
MODE_DIRECTION_STIM = 0x02


class CommandRefused(RuntimeError):
    """A command outside the allowlist; nothing was sent."""


def refusal(cmd: Any, data: tuple[Any, ...]) -> str | None:
    """Why `cmd` with `data` must not be sent, or None when it may."""
    if cmd not in ALLOWED:
        name = getattr(cmd, "name", None) or f"0x{int(cmd):02X}"
        return f"{name} is not allowed: this extension only measures"
    if cmd == Command.SET_DAQ_LIST_MODE and (not data or int(data[0]) & MODE_DIRECTION_STIM):
        return "SET_DAQ_LIST_MODE with the STIM direction is not allowed"
    return None


class GuardedFraming:
    """Wraps pyxcp's framing: refuses before framing, forwards everything else."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def prepare_request(self, cmd: Any, *data: Any) -> bytes:
        if reason := refusal(cmd, data):
            raise CommandRefused(reason)
        return self._inner.prepare_request(cmd, *data)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def install(master: Any) -> None:
    """Guard `master` before it sends anything."""
    transport = master.transport
    if not isinstance(transport.framing, GuardedFraming):
        transport.framing = GuardedFraming(transport.framing)
