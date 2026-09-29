"""Shared test helpers: polling, trace inspection, ephemeral ports."""

import socket
import time

import zelos_sdk


def wait_until(pred, timeout=4.0, interval=0.01):
    """Poll ``pred`` until truthy or timeout; return its final value."""
    deadline = time.monotonic() + timeout
    val = pred()
    while not val and time.monotonic() < deadline:
        time.sleep(interval)
        val = pred()
    return val


def trace_events(trz) -> dict[str, str | None]:
    """Every event in a trace: `source/event` path to declared event type."""
    with zelos_sdk.TraceReader(str(trz)) as reader:
        return {
            f"{source.name}/{event.name}": event.event_type
            for segment in reader.list_data_segments()
            for source in reader.list_fields(segment.id)
            for event in source.events
        }


def free_port() -> int:
    """An OS-assigned free TCP port on 127.0.0.1."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_listening(port: int, timeout: float = 5.0) -> None:
    """Block until 127.0.0.1:``port`` accepts TCP connections."""
    end = time.monotonic() + timeout
    while True:
        with socket.socket() as s:
            s.settimeout(0.2)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return
        if time.monotonic() > end:
            raise TimeoutError(f"nothing listening on 127.0.0.1:{port} after {timeout}s")
        time.sleep(0.02)
