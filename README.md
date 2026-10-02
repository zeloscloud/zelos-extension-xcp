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
| **Transport** | `udp` or `tcp` (XCP on Ethernet, as the ECU provides) or `demo`. No default |
| **A2L File** | The ECU's A2L database, from the same firmware build as the ECU. Required for `udp` and `tcp` |

### Per-ECU Settings

| Setting | Default | Description |
|---|---|---|
| **Name** | host or `demo` | Trace segment for this ECU. Letters, digits, space, `_`, `-` only. Defaults to the sanitized host |
| **Host** | | Required for `udp` and `tcp`. IP address or hostname of the ECU |
| **Port** | `5555` | `udp` and `tcp` |
| **Measurements** | | Groups of A2L measurement names. **Event** `default` samples each signal on the event its A2L entry names; or name one ECU event (`10ms`), or `poll` with a **Rate (ms)** |
| **Signals** / **Signal List File** | | Names typed in the form, a list file, or both. The file is plain text, one name per line; a `.lab` label file works, its rates are ignored |

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

### Demo ECU

`zelos_extension_xcp/demo/` is a measurement-only XCP slave in pure Python, with its A2L (`demo/demo.a2l`, EPK `ZELOS_XCP_DEMO_V1`). It backs the `demo` transport and the integration tests. XCP on CAN is its primary transport; UDP and TCP are also served.

```bash
just sim                                           # UDP 127.0.0.1:5555
just sim --transport tcp -p 5556                   # TCP
just sim --interface socketcan --channel vcan0     # XCP on CAN, Linux, existing interface
uv run main.py demo-ecu --help                     # --fd, --extended, --rx-id, --tx-id, --bitrate, --timestamp-size
```

| Item | Behaviour |
|---|---|
| Model | A small traction inverter: speed, torque, DC bus, phase currents, temperatures, state enum, 96 cell voltages. Values are exact functions of ECU time (`DemoEcu.expected()`) |
| Events | `10ms` (channel 0) and `100ms` (channel 1). A variable holds the value of its task's last run |
| DAQ | Dynamic, absolute ODT PIDs, fixed timestamp in the first ODT (4 bytes, 1 us; 0, 1 or 2 bytes selectable). All ODTs of a sample come from one snapshot, in order |
| XCP on CAN | Ids 0x7F0 (master to slave) and 0x7F1, classic CAN (8 bytes, unpadded) or CAN FD (64, padded to FD lengths). Default bus: the in-process `zelos_can` virtual bus, which serves tests, not other programs; `--interface` takes any python-can bus |
| Refused | Every write, page, store, flash, seed/key, user command and STIM list: an XCP error, counted, never executed |
| Second master | UDP: a CONNECT from another address takes the session, the first master is not told. TCP: the second connection waits unanswered until the first closes. CAN: any CONNECT restarts the session |

`demo.a2l` is generated from the model table: `uv run python -m zelos_extension_xcp.demo.a2l > zelos_extension_xcp/demo/demo.a2l`. A test fails when they differ. On Linux, `XCP_DEMO_CAN_INTERFACE=socketcan XCP_DEMO_CAN_CHANNEL=vcan0 just test` runs the CAN tests on `vcan0`.

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
