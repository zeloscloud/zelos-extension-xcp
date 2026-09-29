"""Trace naming: layout, user-typed names, A2L symbol rewrite."""

import pytest

from zelos_extension_xcp.constants import RESERVED_ECU_NAMES, field_names, name_error, trace_layout


def test_trace_layout():
    assert trace_layout("XCP", "inverter") == ("XCP", "inverter")
    assert trace_layout("", "inverter") == ("inverter", None)


def test_name_error():
    assert name_error("inverter 1", "ECU Name") is None
    assert name_error("", "Prefix") is None
    assert "'.' is not allowed" in name_error("ecu.1", "ECU Name")
    assert "reserved" in name_error("xcp_log", "ECU Name", RESERVED_ECU_NAMES)


def test_field_names_rewrite_and_collision():
    assert field_names(["motor.ctrl.id_ref", "cell_v[3]", "m[1][2]"]) == {
        "motor.ctrl.id_ref": "motor_ctrl_id_ref",
        "cell_v[3]": "cell_v_3",
        "m[1][2]": "m_1_2",
    }
    with pytest.raises(ValueError, match=r"'a\.b' and 'a_b' both map to field 'a_b'"):
        field_names(["a.b", "a_b"])
