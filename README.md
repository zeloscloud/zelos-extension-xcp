# Zelos XCP

A Zelos extension for XCP (ASAM MCD-1 XCP) measurement. Reads ECU internals by their A2L names, on one timebase with the rest of your Zelos data.

> **Early development.** XCP on CAN and XCP on Ethernet measure against the demo ECU and a Vector XCPlite slave. The `demo` interface needs the demo ECU package, which is not in this build yet. Not yet run against a production ECU.

## Features

- **ECUs in parallel**: One entry per XCP slave, each with its own A2L file and interface
- **XCP on CAN**: Classic CAN and CAN FD through the CAN extension's adapters (SocketCAN, PCAN, Kvaser, Vector, slcan, any python-can interface)
- **XCP on Ethernet**: UDP or TCP
- **DAQ and polling**: ECU events push samples (DAQ); `poll` groups read on a fixed rate
- **A2L names and units**: Signals are selected by their A2L names and traced as physical values with their units and value tables
- **Measurement only**: The extension never writes to the ECU. Every command passes one allowlist; nothing else is sent
- **Demo ECU**: A demo interface to try the extension without hardware

## Quick Start

From a checkout:

```bash
git clone https://github.com/zeloscloud/zelos-extension-xcp
cd zelos-extension-xcp
just install
uv run main.py --demo
```

The extension is not in the marketplace yet.

### Platforms

The XCP stack (pyxcp) ships prebuilt for Linux x86-64 and arm64 (glibc), macOS arm64 and Windows x86-64 and arm64. Not supported: macOS on Intel, musl Linux (Alpine), 32-bit ARM. The `socketcan` interfaces are Linux only.

## Configuration

All configuration is managed through the Zelos App settings interface.

### Required Settings

| Setting | Description |
|---|---|
| **Interface** | How the ECU is reached, no default. CAN: SocketCAN (Zelos), SocketCAN over SSH (Zelos), SocketCAN (python-can), PCAN, Kvaser, Vector, slcan (serial), Other (python-can); each opens a python-can interface (`zelos-socketcan`, `zelos-ssh-socketcan`, `socketcan`, `pcan`, ...). Ethernet: XCP on UDP, XCP on TCP. Demo |
| **A2L File** | The ECU's A2L database, from the same firmware build as the ECU. Required except for `demo` |

### Per-ECU Settings

| Setting | Default | Description |
|---|---|---|
| **Name** | host, channel or `demo` | Trace segment for this ECU. Letters, digits, space, `_`, `-` only |
| **Host** / **Port** | / `5555` | `udp`, `tcp`: the ECU's address |
| **Channel**, **Bitrate**, **CAN-FD Mode**, **Advanced Configuration (JSON)** | as the CAN extension | CAN: the adapter, with the CAN extension's fields and defaults. The SocketCAN channel picker lists this machine's interfaces (`list_interfaces`) |
| **Command CAN ID** / **Response CAN ID** | from the A2L | CAN: master-to-ECU and ECU-to-master ids, hex |
| **Extended IDs** | off | CAN: the two ids above are 29-bit |
| **Demo Transport** | `can` | `demo`: `can` (in-memory virtual bus), `udp` or `tcp` |
| **Measurements** | | Groups of A2L measurement names. **Event** `default` samples each signal on the event its A2L entry names (its fixed event first); or name one ECU event (`10ms`) for the whole group, or `poll` with a **Rate (ms)** |
| **Signals** / **Signal List File** | | Names typed in the form, a list file (plain text or `.lab`), or both |

> **One tool per ECU.** Connecting can end another tool's session (CANape, INCA). Stop the extension, or remove the ECU, before another tool goes online.

Behaviour and limits: [docs/behaviour.md](docs/behaviour.md).

### Advanced Settings

One value each, applied to every ECU.

| Setting | Default | Description |
|---|---|---|
| `prefix` | `XCP` | Trace source every ECU publishes under |
| `log_level` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `timestamp_mode` | `auto` | `auto`: the ECU's DAQ timestamp plus one offset to local time; `host`: receive time |
| `epk_check` | `strict` | `strict` refuses to measure when the A2L's EPK does not match the ECU, or cannot be read; `warn` logs it and measures |
| `timeout` | `1.0` | Seconds to wait for each command response |
| `retries` | `1` | Extra attempts per read-only command |
| `max_bus_load` | none | XCP on CAN: refuse a selection estimated above this percentage of the bus; empty: only reported |
| `debug_frames` | off | Logs every command, response and event packet at `DEBUG`, never DAQ data. Needs `log_level` `DEBUG` |

### Trace layout

| What | Prefix `XCP` | Prefix cleared |
|---|---|---|
| ECU events | `XCP/inverter/<event>` | `inverter/<event>` |
| Polled groups | `XCP/inverter/poll_<rate_ms>` | `inverter/poll_<rate_ms>` |
| Session provenance | `XCP/inverter/session` | `inverter/session` |
| Extension logs | `XCP/log` | `xcp_log/log` |

One row per ECU sample of an event; polled groups one row per poll.

`session` has one row per session, reconnects included:

| Field | Meaning |
|---|---|
| `ecu_epk`, `a2l_epk`, `epk_result` | EPK read from the ECU, EPK in the A2L, and the check's result (`get_status` `epk`) |
| `a2l_path`, `a2l_sha256` | The A2L file measured with and the SHA-256 of its bytes |
| `protocol_version`, `transport_version` | From the ECU's `CONNECT` response |

## A2L

A2L symbols become trace field names with `.` and `[i]` rewritten (`motor.ctrl.id_ref` reads `motor_ctrl_id_ref`).

| Conversion | Traced as |
|---|---|
| `IDENTICAL` | The ECU value, integer or float type kept |
| `LINEAR`, `RAT_FUNC` | Physical value, float |
| `TAB_VERB` | The raw integer with the table as the field's value table |

## Actions

Available from the Zelos App and to app extensions as `XCP/<action>`, whatever the trace prefix.

| Action | Description |
|---|---|
| `list_ecus` | Every configured ECU: name, interface, transport, endpoint, state. The names the other actions accept as `ecu` |
| `get_status` | One ECU's state and counters (below). Reads nothing from the ECU |
| `list_events` | The ECU's event channels from its A2L, with cycle times |
| `list_measurements` | A2L measurements whose name contains the search text, paged: name, unit, datatype, default event |
| `read` | One measurement by A2L name, once: physical value and unit. Reads memory only |
| `check_selection` | Dry run of the configured selection: per-event signals, ODTs, CAN frames per second and bus load, skipped and unknown names, and whether it fits the ECU's reported limits. Does not start or change measurement |
| `auto_config` | One demo ECU, for the config form's Auto-configure button. Runs with the extension stopped |
| `list_interfaces` | This machine's SocketCAN interfaces, for the Channel picker. Empty on macOS and Windows. Runs with the extension stopped |

`get_status` fields:

| Field | Meaning |
|---|---|
| `state`, `error`, `errors`, `reconnects` | Session state, last error, error and reconnect counts |
| `epk` | A2L and ECU EPK, `match`, `mismatch`, `absent` or `unreadable` |
| `locked` | Resources the selection needs that seed and key protects (`DAQ`, `CAL/PAG`); empty when none |
| `events` | Per event: rows, rate, expected rate, `expected_rows`, `missing_rows`, stalled. Polled: `missed_cycles`, `poll_window_ms`, `frames_per_cycle` |
| `watchdog` | `stalled` when any event has stopped |
| `lost_packets` | Ethernet packet counter gaps; `null` on CAN |
| `incomplete_rows`, `rejected_packets`, `malformed_datagrams`, `queue_overflow`, `unplaced_rows` | Samples dropped: incomplete, wrong length or unknown PID, broken UDP framing, host too slow, untimeable after a gap |
| `stale_responses`, `daq_overloads` | Responses after their command was given up on; DAQ overloads the ECU reports |
| `status_values` | Per signal: raw values in a `STATUS_STRING_REF` range, written as null |
| `bus_load` | CAN: per event and total estimate, bitrate and its source, ceiling (`null`: none) |
| `timestamps` | Source (`ecu`, `adapter`, `host`), anchors, `gaps`, offset to local time, drift in ppm |
| `a2l_warnings`, `unknown`, `skipped`, `polled_no_default_event` | A2L reader warnings, names not in the A2L, names skipped with reasons, names polled for lack of a default event with their rate |

## What is XCP?

XCP is the ASAM standard for reading and writing ECU memory at runtime, used by calibration tools. The A2L file describes where each variable lives and how to convert it. See [ASAM MCD-1 XCP](https://www.asam.net/standards/detail/mcd-1-xcp/).

## Development

See [CONTRIBUTING.md](CONTRIBUTING.md).

### Demo ECU

`zelos_extension_xcp/demo/` is a measurement-only XCP slave in pure Python, with its A2L (`demo/demo.a2l`, EPK `ZELOS_XCP_DEMO_V1`). It backs the `demo` transport and the integration tests. XCP on CAN is its primary transport; UDP and TCP are also served.

```bash
just sim                                           # UDP 127.0.0.1:5555
just sim --transport tcp -p 5556                   # TCP
just sim --interface socketcan --channel vcan0     # XCP on CAN, Linux, existing interface
uv run main.py demo-ecu --help                     # --fd, --extended, --rx-id, --tx-id, --bitrate, --timestamp-size, --protected
```

## Links

- **Repository**: [github.com/zeloscloud/zelos-extension-xcp](https://github.com/zeloscloud/zelos-extension-xcp)
- **Issues**: [Report bugs or request features](https://github.com/zeloscloud/zelos-extension-xcp/issues)

## CLI Usage

```bash
# App mode with the config from the Zelos App
uv run main.py

# Add a demo ECU on CAN
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
