"""Measurement selection: what an empty or wrong selection reports."""

import json
import logging
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from zelos_extension_xcp import client
from zelos_extension_xcp.client import XcpConnection

DEMO_A2L = Path(__file__).parents[1] / "zelos_extension_xcp" / "demo" / "demo.a2l"


def _start(caplog, measurements):
    caplog.set_level(logging.WARNING, logger="zelos_extension_xcp.client")
    XcpConnection("ecu", "tcp", a2l_file=str(DEMO_A2L), measurements=measurements).start("")
    return [r.getMessage() for r in caplog.records]


def test_no_selection_lists_the_a2l_names(caplog, monkeypatch):
    monkeypatch.setattr(client, "LISTED_OPTIONS", 3)
    (message,) = _start(caplog, [{"event": "default", "signals": []}])
    assert "under Measurements -> Signals" in message
    assert "  10ms:\n    inv.dc.current (A)\n    inv.dc.flow\n    inv.dc.power (W)\n" in message
    assert message.endswith("  ... and 38 more in the A2L")


@pytest.mark.parametrize(
    ("signals", "warning", "error"),
    [
        (
            [],
            "    motor.speed (rpm)",
            "ECU 'dcdc' has no measurements selected (see the list above)",
        ),
        (
            ["motor.sped", "sys.tick_10ms"],
            "[dcdc] motor.sped not in the A2L, not measured "
            "(did you mean motor.speed, motor.omega, motor.angle?)",
            "ECU 'dcdc': 1 measurement name is not in the A2L (see the suggestions above)",
        ),
    ],
)
def test_bad_selection_does_not_start(tmp_path, signals, warning, error):
    root = Path(__file__).parents[1]
    shutil.copy(root / "config.schema.json", tmp_path)
    ecu = {
        "name": "dcdc",
        "interface": "XCP on TCP",
        "host": "127.0.0.1",
        "a2l_file": str(DEMO_A2L),
        "measurements": [{"event": "default", "signals": signals}],
    }
    (tmp_path / "config.json").write_text(json.dumps({"ecus": [ecu]}))
    run = subprocess.run(
        [sys.executable, str(root / "main.py")],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert run.returncode == 1
    assert warning in run.stderr
    assert run.stderr.rstrip().endswith(f"ERROR zelos_extension_xcp.cli.app: not starting: {error}")
    assert "Connected" not in run.stderr
