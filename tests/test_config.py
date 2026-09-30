"""Config: the schema, its defaults and what auto_config returns."""

import json
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


def _branches(schema, items):
    one_of = items(schema)["dependencies"]["interface"]["oneOf"]
    return {i: b["properties"] for b in one_of for i in b["properties"]["interface"]["enum"]}


def test_can_fields_match_the_vendored_can_extension():
    vendored = json.loads(
        (Path(__file__).parent.parent / "vendor/zelos-extension-can/config.schema.json").read_text()
    )
    theirs = _branches(vendored, lambda s: s["properties"]["buses"]["items"])
    ours = _branches(SCHEMA, lambda s: s["properties"]["ecus"]["items"])
    for interface in sorted(CAN_INTERFACES):
        for field, spec in theirs[interface].items():
            assert ours[interface][field] == spec, f"{interface}.{field} drifted"
    assert "ssh-socketcan" not in ours
