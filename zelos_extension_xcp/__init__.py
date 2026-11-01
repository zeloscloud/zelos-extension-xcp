"""Zelos XCP

A Zelos extension for XCP measurement with A2L databases.
"""

from zelos_extension_xcp.client import XcpConnection

#: Action namespace for live registration; equals the manifest name.
ACTION_PREFIX = "XCP"

__all__: list[str] = [
    "ACTION_PREFIX",
    "XcpConnection",
]
