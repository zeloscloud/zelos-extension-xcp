"""The actions' parameters, through the SDK as the agent calls them."""

from __future__ import annotations

import pytest
from zelos_sdk.actions import ActionExecutionError

from zelos_extension_xcp import actions


class Ecu:
    def __init__(self, name: str) -> None:
        self.name = name

    def status(self) -> dict:
        return {"name": self.name}


def test_the_only_ecu_is_used_when_the_call_names_none(monkeypatch):
    monkeypatch.setattr(actions, "XCP_ECUS", {"demo": Ecu("demo")})
    schemas = [f._action.to_schema() for f in vars(actions).values() if hasattr(f, "_action")]
    takes_ecu = [s for s in schemas if "ecu" in s["properties"]]
    assert len(takes_ecu) == 5
    assert [s["title"] for s in takes_ecu if "ecu" in s.get("required", [])] == []
    assert actions.get_status._action.execute().value == {"name": "demo"}
    monkeypatch.setitem(actions.XCP_ECUS, "bench", Ecu("bench"))
    with pytest.raises(ActionExecutionError, match="set ecu to one of: bench, demo$"):
        actions.get_status._action.execute()
    assert actions.get_status._action.execute(ecu="bench").value == {"name": "bench"}
