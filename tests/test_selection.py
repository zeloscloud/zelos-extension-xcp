"""Measurement selection: typed names plus a list file."""

import logging
from pathlib import Path

import pytest

from zelos_extension_xcp import client
from zelos_extension_xcp.client import XcpConnection
from zelos_extension_xcp.selection import group_names, read_names

LAB = """[SETTINGS]
Version;V1.1
MultiRasterSeparator;&

[RAMCELL]
motor.ctrl.id_ref;10ms
cells[3]
motor.ctrl.id_ref;100ms
"""


def test_list_file_plain_and_lab(tmp_path):
    plain = tmp_path / "signals.txt"
    plain.write_text("# inverter\nspeed\n\n  torque  \nspeed\n")
    lab = tmp_path / "signals.lab"
    lab.write_text(LAB)

    assert read_names(plain) == ["speed", "torque"]
    assert read_names(lab) == ["motor.ctrl.id_ref", "cells[3]"]
    assert group_names({"signals": ["torque", "vdc"], "signals_file": str(plain)}) == [
        "torque",
        "vdc",
        "speed",
    ]


def test_list_file_path_must_be_absolute():
    with pytest.raises(ValueError, match="absolute"):
        read_names("signals.txt")


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
    assert message.endswith(
        "... and 38 more; run the XCP List Measurements action with a search to find others"
    )


def test_unknown_name_suggests_close_matches(caplog):
    messages = _start(caplog, [{"event": "default", "signals": ["motor.sped", "motor.speed"]}])
    assert messages == [
        "[ecu] motor.sped not in the A2L, not measured "
        "(did you mean motor.speed, motor.omega, motor.angle?)"
    ]
