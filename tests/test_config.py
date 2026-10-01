"""Config: the schema, its defaults and what auto_config returns."""

import json
from importlib.resources import files
from pathlib import Path

import jsonschema

from zelos_extension_xcp.actions import auto_config
from zelos_extension_xcp.cli.app import ADVANCED_DEFAULTS, resolve_advanced
from zelos_extension_xcp.constants import CAN_INTERFACES

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
    return {i: b["properties"] for b in one_of for i in b["properties"]["interface"]["enum"]}


def test_can_fields_match_zelos_can():
    shipped = files("zelos_can.bus").joinpath("interfaces.schema.json").read_text()
    theirs = _branches(json.loads(shipped))
    ecus = SCHEMA["properties"]["ecus"]["items"]
    ours = _branches(ecus["dependencies"]["interface"]["oneOf"])
    picker = {"action": "XCP/list_interfaces"}  # ours: the CAN extension may not be installed
    for interface in sorted(CAN_INTERFACES):
        for field, spec in theirs[interface].items():
            mine = dict(ours[interface][field])
            if mine.get("ui:options") == picker:
                mine["ui:options"] = spec["ui:options"]
            assert mine == spec, f"{interface}.{field} drifted"
    assert "ssh-socketcan" not in ours


def test_list_interfaces_answers_the_picker(monkeypatch):
    from zelos_extension_xcp import actions

    monkeypatch.setattr(actions.discovery, "list_interfaces", lambda: [{"value": "can0"}])
    assert actions.list_interfaces() == {"status": "success", "choices": [{"value": "can0"}]}
