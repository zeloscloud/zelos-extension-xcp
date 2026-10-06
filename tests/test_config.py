"""Config: the schema, its defaults and what auto_config returns."""

import json
from importlib.resources import files
from pathlib import Path

import jsonschema

from zelos_extension_xcp.actions import auto_config
from zelos_extension_xcp.cli.app import ADVANCED_DEFAULTS, resolve_advanced
from zelos_extension_xcp.constants import CAN_INTERFACES, INTERFACES

SCHEMA = json.loads((Path(__file__).parent.parent / "config.schema.json").read_text())


def test_resolve_advanced():
    assert resolve_advanced({}) == ADVANCED_DEFAULTS
    assert resolve_advanced({"advanced": {"prefix": ""}})["prefix"] == ""
    assert resolve_advanced({"advanced": {"retries": 3}})["retries"] == 3


def test_schema_defaults_match_advanced_defaults():
    advanced = SCHEMA["properties"]["advanced"]["properties"]
    assert {k: v.get("default") for k, v in advanced.items()} == ADVANCED_DEFAULTS


def test_auto_config_validates():
    jsonschema.Draft7Validator.check_schema(SCHEMA)
    result = auto_config()
    assert result["status"] == "success"
    jsonschema.validate(result["config"], SCHEMA)


def _branches(one_of):
    return {i: b for b in one_of for i in b["properties"]["interface"]["enum"]}


def test_can_fields_match_zelos_can():
    shipped = files("zelos_can.bus").joinpath("interfaces.schema.json").read_text()
    theirs = _branches(json.loads(shipped))
    ecus = SCHEMA["properties"]["ecus"]["items"]
    ours = _branches(ecus["dependencies"]["interface"]["oneOf"])
    picker = {"action": "XCP/list_interfaces"}  # ours: the CAN extension may not be installed
    for label in (k for k, v in INTERFACES.items() if v in CAN_INTERFACES):
        # The fields XCP offers; zelos-can's CAN-node fields (e.g. J1939 claim) are not XCP's.
        mine_props, spec_props = ours[label]["properties"], theirs[label]["properties"]
        for field in mine_props.keys() & spec_props.keys():
            mine, spec = dict(mine_props[field]), spec_props[field]
            if mine.get("ui:options") == picker:
                mine["ui:options"] = spec["ui:options"]
            assert mine == spec, f"{label}.{field} drifted"
        # Fields that follow another (fd_mode's data bitrate).
        assert ours[label].get("dependencies") == theirs[label].get("dependencies"), label


def test_each_label_resolves_to_its_interface():
    from zelos_extension_xcp.cli.app import _interface

    expect = {
        "SocketCAN": "zelos-socketcan",
        "SocketCAN over SSH": "zelos-ssh-socketcan",
        "PCAN": "pcan",
        "Kvaser": "kvaser",
        "Vector": "vector",
        "slcan (serial)": "slcan",
        "Other (python-can)": "other",
        "XCP on UDP": "udp",
        "XCP on TCP": "tcp",
        "Demo": "demo",
    }
    assert {label: str(_interface("ecu", {"interface": label})) for label in expect} == expect


def test_every_interface_label_resolves():
    assert set(SCHEMA["properties"]["ecus"]["items"]["properties"]["interface"]["enum"]) == set(
        INTERFACES
    )


def test_list_interfaces_answers_the_picker(monkeypatch):
    from zelos_extension_xcp import actions

    monkeypatch.setattr(actions.discovery, "list_interfaces", lambda: [{"value": "can0"}])
    assert actions.list_interfaces() == {"status": "success", "choices": [{"value": "can0"}]}


def test_list_a2l_measurements_answers_the_signal_picker(tmp_path, monkeypatch):
    from zelos_extension_xcp import actions

    demo = {"ecus": [{"interface": "XCP on UDP"}, {"interface": "Demo", "a2l_file": "/ignored"}]}
    result = actions.list_a2l_measurements(demo, ["ecus", 1, "measurements", 0, "signals", 0])
    assert result["status"] == "success" and "message" not in result
    assert result["choices"][0] == {"value": "inv.state", "detail": "10ms"}

    # Older app (no config/path), no A2L set, unreadable A2L.
    assert actions.list_a2l_measurements()["choices"] == []
    assert "A2L File" in actions.list_a2l_measurements(demo, ["ecus", 0])["message"]
    missing = {"ecus": [{"interface": "XCP on UDP", "a2l_file": str(tmp_path / "nope.a2l")}]}
    assert actions.list_a2l_measurements(missing, ["ecus", 0])["status"] == "error"

    monkeypatch.setattr(actions, "MAX_CHOICES", 2)
    capped = actions.list_a2l_measurements(demo, ["ecus", 1])
    assert len(capped["choices"]) == 2 and "first 2 of" in capped["message"]
