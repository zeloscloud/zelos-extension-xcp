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
    ecu = {
        "name": "dcdc",
        "interface": "XCP on TCP",
        "host": "127.0.0.1",
        "a2l_file": str(DEMO_A2L),
        "measurements": [{"event": "default", "signals": signals}],
    }
    run = _main(tmp_path, {"ecus": [ecu]})
    assert run.returncode == 1
    assert warning in run.stderr
    assert run.stderr.rstrip().endswith(f"ERROR zelos_extension_xcp.cli.app: not starting: {error}")
    assert "Connected" not in run.stderr


def _main(tmp_path, config):
    """main.py on `config`, as the app starts it."""
    root = Path(__file__).parents[1]
    shutil.copy(root / "config.schema.json", tmp_path)
    (tmp_path / "config.json").write_text(json.dumps(config))
    return subprocess.run(
        [sys.executable, str(root / "main.py")],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
    )


SIGNALS = [{"event": "default", "signals": ["motor.speed"]}]
VIRTUAL = json.dumps({"interface": "virtual", "channel": "xcp-init-errors"})


@pytest.mark.parametrize(
    ("ecu", "advanced", "error"),
    [
        (
            {"interface": "XCP on UDP", "host": "127.0.0.1", "a2l_file": "/missing.a2l"},
            {},
            "ECU 'e': A2L /missing.a2l:",
        ),
        (
            {"interface": "Other (python-can)", "config_json": VIRTUAL, "fd_mode": True},
            {},
            "ECU 'e': CAN-FD Mode is on in the XCP extension configuration but the A2L "
            "describes classic CAN: turn it off",
        ),
        (
            {
                "interface": "XCP on UDP",
                "host": "127.0.0.1",
                "measurements": [{"event": "a2l", "a2l_event": "5ms", "signals": ["motor.speed"]}],
            },
            {},
            "ECU 'e': A2L event '5ms' is not in the A2L; its events: 10ms, 100ms",
        ),
        (  # no connection: the interface does not open
            {
                "interface": "Other (python-can)",
                "config_json": '{"interface": "bogus", "channel": "x"}',
            },
            {},
            "ECU 'e': cannot open other://: CanInterfaceNotImplementedError:",
        ),
        (  # refused at the first session
            {"interface": "Demo"},
            {"max_bus_load": 1},
            "ECU 'e': selection needs about",
        ),
    ],
)
def test_init_error_exits(tmp_path, ecu, advanced, error):
    ecu = {"name": "e", "a2l_file": str(DEMO_A2L), "measurements": SIGNALS, **ecu}
    run = _main(tmp_path, {"ecus": [ecu], "advanced": advanced})
    assert run.returncode == 1
    last = run.stderr.rstrip().splitlines()[-1]
    assert f"ERROR zelos_extension_xcp.cli.app: not starting: {error}" in last
