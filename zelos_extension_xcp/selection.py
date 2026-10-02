"""Which A2L measurements a group records: names typed in the config plus a list file."""

from pathlib import Path
from typing import Any

#: Event value that takes each measurement's default event from the A2L.
DEFAULT_EVENT = "default"
POLL_EVENT = "poll"


def read_names(path: str | Path) -> list[str]:
    """Measurement names from a list file, in file order, duplicates dropped.

    Plain text, one name per line. A label file (`.lab`) reads the same way:
    `[SETTINGS]` content is skipped, other section headers are ignored, and
    only the name before the first `;` is kept, so a rate column is ignored.
    Blank lines and lines starting with `#` are skipped.
    """
    file = Path(path).expanduser()
    if not file.is_absolute():
        raise ValueError(
            f"Signal list path must be absolute or start with ~: {path} "
            "(paths are on the agent's host)"
        )
    names: dict[str, None] = {}
    in_settings = False
    for raw in file.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            in_settings = line.upper() == "[SETTINGS]"
            continue
        if in_settings:
            continue
        if name := line.split(";", 1)[0].strip():
            names[name] = None
    return list(names)


def group_names(group: dict[str, Any]) -> list[str]:
    """A measurement group's names: typed ones first, then the list file's."""
    names = dict.fromkeys(group.get("signals") or [])
    if group.get("signals_file"):
        names.update(dict.fromkeys(read_names(group["signals_file"])))
    return list(names)
