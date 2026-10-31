"""The extension process: its stop signals and its log."""

import json
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from conftest import wait_until

MAIN = Path(__file__).resolve().parent.parent / "main.py"


def _measuring_demo(tmp_path: Path) -> tuple[subprocess.Popen, Path]:
    """`main.py --demo` in `tmp_path`, once it measures; its process and its log."""
    log = tmp_path / "out.log"
    with log.open("w") as out:
        proc = subprocess.Popen(
            [sys.executable, str(MAIN), "--demo"], cwd=tmp_path, stdout=out, stderr=out
        )
    if not wait_until(lambda: "measuring" in log.read_text(), timeout=30):
        proc.kill()
        pytest.fail(log.read_text())
    return proc, log


def test_second_interrupt_stops_without_a_traceback(tmp_path):
    proc, log = _measuring_demo(tmp_path)
    try:
        proc.send_signal(signal.SIGINT)
        time.sleep(0.3)  # into the shutdown, past the stop of the connections
        proc.send_signal(signal.SIGINT)
        assert proc.wait(timeout=10) == 0
    finally:
        proc.kill()
    text = log.read_text()
    assert "Traceback" not in text, text


@pytest.mark.parametrize(("log_level", "logged"), [("INFO", False), ("DEBUG", True)])
def test_pyxcp_logs_its_connect_details_only_at_debug(tmp_path, log_level, logged):
    (tmp_path / "config.json").write_text(json.dumps({"advanced": {"log_level": log_level}}))
    proc, log = _measuring_demo(tmp_path)
    try:
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=10) == 0
    finally:
        proc.kill()
    text = log.read_text()
    assert (" pyxcp.transport: " in text) is logged, text
