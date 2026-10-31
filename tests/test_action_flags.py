"""Pins each action's flags, so a new action or a changed flag is a deliberate choice."""

from __future__ import annotations

import inspect

from zelos_extension_xcp import actions


def test_every_action_has_its_flags():
    actual = {
        name: (obj._action.read_only, obj._action.hidden)
        for name, obj in inspect.getmembers(actions, inspect.isfunction)
        if not name.startswith("_") and hasattr(obj, "_action")
    }
    assert actual == {
        "list_ecus": (True, True),
        "get_status": (True, True),
        "list_events": (True, True),
        "list_measurements": (True, True),
        "read": (True, True),
        "check_selection": (True, True),
        "auto_config": (True, True),
        "list_interfaces": (True, True),
        "list_a2l_measurements": (True, True),
        "list_a2l_events": (True, True),
    }
