# Contributing

## Prerequisites

- [Zelos](https://zeloscloud.io) 26.0.10 or later
- [Zelos CLI](https://docs.zeloscloud.io/reference/cli/) 0.1.11 or later
- [just](https://github.com/casey/just)
- [uv](https://docs.astral.sh/uv/)
- CMake and a C compiler, for the XCPlite interop tests only

## Layout

`extension.toml` describes everything the extension contributes:

| Part | Files | Manifest |
| ---- | ----- | -------- |
| Agent runtime | `main.py`, the Python package, `pyproject.toml`, `config.schema.json` | `[agent]`, `[config]` |

| Path                                   | Purpose                                                                    |
| -------------------------------------- | -------------------------------------------------------------------------- |
| `main.py`                              | Entry point; app mode, `--demo`, `--file` and the `demo-ecu` command       |
| `zelos_extension_xcp/client.py`        | One ECU's connection: session thread, reconnects, status, trace writes     |
| `zelos_extension_xcp/a2l.py`           | A2L catalog to measurement plan: conversions, skip reasons, events         |
| `zelos_extension_xcp/daq.py`           | DAQ list layout, the capacity check and the receive side                   |
| `zelos_extension_xcp/poll.py`          | Polled reads merged into as few upload commands as fit                     |
| `zelos_extension_xcp/timestamps.py`    | ECU DAQ timestamps to host time                                            |
| `zelos_extension_xcp/can_link.py`      | XCP on CAN: the bus, the CAN ids and the bus-load estimate                 |
| `zelos_extension_xcp/guard.py`         | The command allowlist every outgoing XCP command passes                    |
| `zelos_extension_xcp/compat.py`        | Fixes for defects in the pinned pyxcp version                              |
| `zelos_extension_xcp/actions.py`       | The XCP actions                                                            |
| `zelos_extension_xcp/constants.py`     | Interfaces, the demo ECU entry and the trace-naming rules                  |
| `zelos_extension_xcp/cli/app.py`       | Config load, `advanced` resolution and startup validation                  |
| `zelos_extension_xcp/cli/demo_ecu.py`  | The `demo-ecu` command                                                     |
| `zelos_extension_xcp/demo/`            | The demo ECU: model, A2L, XCP slave and its CAN, UDP and TCP transports    |
| `tests/test_e2e.py`                    | End to end against the demo ECU and XCPlite, asserting on the trace        |
| `tests/test_demo_ecu.py`, `tests/test_demo_ecu_can.py` | The demo ECU against a raw XCP master                  |
| `tests/xcplite_slave/`                 | An XCP slave on Vector XCPlite, for the interop tests                      |
| `docs/behavior.md`                     | Behavior and limits, linked from the README                                |

## Commands

| Command                       | Description                                                       |
| ----------------------------- | ----------------------------------------------------------------- |
| `just install`                | Install dependencies and the pre-commit hooks                     |
| `just format`                 | Format code with ruff                                             |
| `just check`                  | Check formatting and lint with ruff                               |
| `just test`                   | Run tests (pytest)                                                |
| `just package`                | Package for the Zelos marketplace                                 |
| `just ci`                     | Install from the lockfile, check, test and package                |
| `just release VERSION`        | Bump version, check, test, commit, tag                            |
| `just delete-release VERSION` | Delete a GitHub release and its tag, then force-push              |
| `just clean`                  | Remove build output, caches and packages                          |
| `just run`                    | Run the extension against `./config.json`                         |
| `just sim`                    | Run the demo ECU on UDP `127.0.0.1:5555`; takes `demo-ecu` options |
| `just xcplite-slave`          | Build the XCPlite test slave into `build/xcplite_slave/`          |
| `just lock`                   | Update `uv.lock` after changing dependencies                      |

`just install` installs the pre-commit hooks in `.pre-commit-config.yaml`. CI runs on every
push and pull request: it checks that `uv.lock` is in sync, then runs `just check`, builds the
XCPlite slave and runs `just test` with it.

## Testing

`just test` runs every test against the demo ECU in process: XCP on CAN on python-can's
`virtual` bus, and XCP on UDP and TCP on loopback. No hardware is needed.

The `xcplite-udp` and `xcplite-tcp` targets of `tests/test_e2e.py` run the same suite against
a slave built on [Vector XCPlite](https://github.com/vectorgrp/XCPlite), fetched at a pinned
commit. They skip unless `XCP_TEST_SLAVE` names the binary:

```bash
just xcplite-slave
XCP_TEST_SLAVE=build/xcplite_slave/xcp_test_slave just test
```

On Linux, `XCP_DEMO_CAN_INTERFACE` and `XCP_DEMO_CAN_CHANNEL` run the demo ECU tests on a real
CAN interface instead of the virtual bus, for example `socketcan` and `vcan0`.

## Run it locally

`uv run main.py --demo` adds the demo ECU on CAN to the configured ECUs and needs no config.
`just run` reads `./config.json`. To try XCP on UDP, start the demo ECU in one terminal:

```bash
just sim
```

Then run the extension in another:

```bash
echo '{"ecus": [{"interface": "XCP on UDP", "host": "127.0.0.1", "a2l_file": "zelos_extension_xcp/demo/demo.a2l", "measurements": [{"event": "default", "signals": ["motor.speed"]}]}]}' > config.json
just run
```

The extension connects to the agent at `ZELOS_AGENT_URL`, else `http://localhost:2300`.

## Run it in Zelos

```bash
zelos extensions install-local .
zelos extensions start local.xcp --config '{"ecus": [{"interface": "Demo", "measurements": [{"event": "default", "signals": ["motor.speed"]}]}]}'
zelos extensions logs local.xcp
```

A local install runs from your project directory, so you do not install again after a code
change:

- Agent runtime: stop and start the extension. After a dependency change, stop it and run
  `zelos extensions reinstall <id>`.
- `extension.toml`: run `zelos extensions install-local .` again.

## Packaging

```bash
just package
```

The archive lands next to `extension.toml` as `<name>-<version>.tar.gz`, where `<name>` is the
manifest name in lowercase with every other run of characters turned into one hyphen. It holds
`extension.toml`, the README, the icon, the config schema and the paths listed in
`[package] paths`. With an agent runtime it also holds `pyproject.toml`, `uv.lock` and
`actions.json`, the list of actions that run while the extension is stopped.

## Release

```bash
just release 1.0.0
git push --follow-tags
```

The release workflow checks, tests and packages the extension, then attaches the archive to a
GitHub release. The Zelos marketplace picks up each new release.
