"""Measurement selection: typed names plus a list file."""

import pytest

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
