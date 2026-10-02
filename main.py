#!/usr/bin/env python3
"""Zelos XCP extension - ECU measurement over XCP with A2L databases."""

import logging
import time
from pathlib import Path

import rich_click as click

from zelos_extension_xcp import ACTION_PREFIX as _ACTION_PREFIX
from zelos_extension_xcp import cli as cli_commands

#: Re-exported so the at-rest inventory dump, which reads this entry module,
#: sees the same namespace the live registration uses. See the definition in
#: `zelos_extension_xcp/__init__.py`.
ACTION_PREFIX = _ACTION_PREFIX

# Configure rich-click
click.rich_click.USE_RICH_MARKUP = True
click.rich_click.USE_MARKDOWN = True
click.rich_click.SHOW_ARGUMENTS = True
click.rich_click.GROUP_ARGUMENTS_OPTIONS = True
click.rich_click.STYLE_ERRORS_SUGGESTION = "yellow italic"

# UTC ISO 8601 with ms, matching the SDK's Rust tracing lines in the same log stream
logging.Formatter.converter = time.gmtime
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s.%(msecs)03dZ %(levelname)5s %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)


@click.group(invoke_without_command=True)
@click.option(
    "--demo",
    is_flag=True,
    help="Add a demo ECU to the configured ones",
)
@click.option(
    "--file",
    type=click.Path(path_type=Path),
    default=None,
    is_flag=False,
    flag_value=".",
    help="Record trace to .trz file (defaults to UTC.trz if no filename specified)",
)
@click.pass_context
def cli(ctx: click.Context, demo: bool, file: Path | None) -> None:
    """XCP measurement with A2L databases.

    Reads ECU measurements over XCP, named and scaled by the ECU's A2L file.
    Configure via Zelos extension settings or use --demo for testing.
    """
    # A subcommand runs on its own
    if ctx.invoked_subcommand is not None:
        return

    cli_commands.run_app_mode(demo, file)


if __name__ == "__main__":
    cli()
