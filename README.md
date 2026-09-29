# Zelos XCP

A Zelos extension for XCP (ASAM MCD-1 XCP) measurement. Reads ECU internals by their A2L names, on one timebase with the rest of your Zelos data.

> **Early development.** The configuration, actions and trace layout are in place. The XCP protocol layer and A2L parsing are not implemented yet: a configured ECU reports `XCP protocol layer is not implemented yet` and idles in the `error` state.

## Features

- **ECUs in parallel**: One entry per XCP slave, each with its own A2L file
- **XCP on Ethernet**: UDP or TCP
- **A2L names**: Signals are selected and traced by their A2L measurement names
- **Demo ECU**: A demo transport to try the extension without hardware

## Quick Start

From a checkout:

```bash
git clone --recurse-submodules https://github.com/zeloscloud/zelos-extension-xcp
cd zelos-extension-xcp
just install
uv run main.py --demo
```

The extension is not in the marketplace yet.

## Configuration

All configuration is managed through the Zelos App settings interface.

### Required Settings

| Setting | Description |
|---|---|
| **Transport** | `eth` (XCP on Ethernet) or `demo` |
| **A2L File** | The ECU's A2L database, from the same firmware build as the ECU. Required for `eth` |

### Per-ECU Settings

| Setting | Default | Description |
|---|---|---|
| **Name** | host or `demo` | Trace segment for this ECU. Letters, digits, space, `_`, `-` only. Defaults to the sanitized host |
| **Host** / **Port** | `127.0.0.1` / `5555` | `eth` only |
| **Protocol** | `udp` | `udp` or `tcp`, `eth` only |
| **Measurements** | | Groups of A2L measurement names, one per ECU event (`10ms`) or `poll` with a **Rate (ms)** |

Names are rejected, not renamed, when they hold anything else. `log` and `xcp_log` are reserved ECU names.

> **One tool per ECU.** The extension connects to every configured ECU when it starts, including when the agent starts it unattended. An ECU serves one XCP tool at a time, and XCP has no way to check for an existing session first. Connecting can end another tool's session (CANape, INCA) with no error on either side. Stop the extension, or remove the ECU, before another tool goes online.

### Advanced Settings

One value each, applied to every ECU.

| Setting | Default | Description |
|---|---|---|
| `prefix` | `XCP` | Trace source every ECU publishes under |
| `log_level` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `timestamp_mode` | `auto` | `auto`: the ECU's timestamp, anchored to local time once at measurement start, or local receive time when the ECU sends none. `host`: always local receive time |
| `epk_check` | `strict` | `strict` refuses to measure when the A2L's EPK does not match the ECU; `warn` logs it |
| `timeout` | `1.0` | Seconds to wait for each command response |
| `retries` | `1` | Extra attempts per command |

### Trace layout

| What | Prefix `XCP` | Prefix cleared |
|---|---|---|
| ECU events | `XCP/inverter/<event>` | `inverter/<event>` |
| Extension logs | `XCP/log` | `xcp_log/log` |

## A2L

A2L symbols are rewritten to trace field names: `.` to `_`, `[i]` to `_i` (`motor.ctrl.id_ref` reads `motor_ctrl_id_ref`). Two symbols that rewrite to one name are a load error that names both.

## Actions

Available from the Zelos App and to app extensions as `XCP/<action>`, whatever the trace prefix.

| Action | Description |
|---|---|
| `list_ecus` | Every configured ECU: name, transport, endpoint, state. The names the other actions accept as `ecu` |
| `get_status` | One ECU's state, last error and counters. Reads nothing from the ECU |
| `auto_config` | One demo ECU, for the config form's Auto-configure button. Runs with the extension stopped |

## What is XCP?

XCP is the ASAM standard for reading and writing ECU memory at runtime, used by calibration tools. The A2L file describes where each variable lives and how to convert it. See [ASAM MCD-1 XCP](https://www.asam.net/standards/detail/mcd-1-xcp/).

## Development

See [CONTRIBUTING.md](CONTRIBUTING.md). The CAN extension is a git submodule under `vendor/`: clone with `--recurse-submodules`, or run `git submodule update --init`.

## Links

- **Repository**: [github.com/zeloscloud/zelos-extension-xcp](https://github.com/zeloscloud/zelos-extension-xcp)
- **Issues**: [Report bugs or request features](https://github.com/zeloscloud/zelos-extension-xcp/issues)

## CLI Usage

```bash
# App mode with the config from the Zelos App
uv run main.py

# Add a demo ECU
uv run main.py --demo

# Record to a .trz file (UTC-stamped name when none is given)
uv run main.py --demo --file
```

## Support

- [Zelos Documentation](https://docs.zeloscloud.io)
- [GitHub Issues](https://github.com/zeloscloud/zelos-extension-xcp/issues)
- help@zeloscloud.io

## License

MIT License - see [LICENSE](LICENSE) for details.

---

**Built with [Zelos](https://zeloscloud.io)**
