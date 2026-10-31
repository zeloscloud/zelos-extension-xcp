"""The extension process on stop signals."""

import signal
import subprocess
import sys
import time
from pathlib import Path

from conftest import wait_until

MAIN = Path(__file__).resolve().parent.parent / "main.py"


def test_second_interrupt_stops_without_a_traceback(tmp_path):
    log = tmp_path / "out.log"
    with log.open("w") as out:
        proc = subprocess.Popen(
            [sys.executable, str(MAIN), "--demo"], cwd=tmp_path, stdout=out, stderr=out
        )
    try:
        assert wait_until(lambda: "measuring" in log.read_text(), timeout=30), log.read_text()
        proc.send_signal(signal.SIGINT)
        time.sleep(0.3)  # into the shutdown, past the stop of the connections
        proc.send_signal(signal.SIGINT)
        assert proc.wait(timeout=10) == 0
    finally:
        proc.kill()
    text = log.read_text()
    assert "Traceback" not in text, text
