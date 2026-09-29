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
git clone --recurse-submodules https://github.com/zeloscloud/zelos-extension-xcp
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
| **Interface** | How the ECU is reached, no default. CAN: `socketcan`, `socketcan-py`, `pcan`, `kvaser`, `vector`, `slcan`, `other`. Ethernet: `udp`, `tcp`. `demo`. `ssh-socketcan` is not supported: request and response timing over SSH does not fit XCP |
| **A2L File** | The ECU's A2L database, from the same firmware build as the ECU. Required except for `demo` |

### Per-ECU Settings

| Setting | Default | Description |
|---|---|---|
| **Name** | host, channel or `demo` | Trace segment for this ECU. Letters, digits, space, `_`, `-` only |
| **Host** / **Port** | / `5555` | `udp`, `tcp`: the ECU's address |
| **Channel**, **Bitrate**, **CAN-FD Mode**, **Advanced Configuration (JSON)** | as the CAN extension | CAN: the adapter, with the CAN extension's fields and defaults |
| **Command CAN ID** / **Response CAN ID** | from the A2L | CAN: master-to-ECU and ECU-to-master ids, hex. Empty: `CAN_ID_MASTER` / `CAN_ID_SLAVE` of the A2L's XCP on CAN section. Neither: the ECU does not start |
| **Extended IDs** | off | CAN: the two ids above are 29-bit |
| **Demo Transport** | `can` | `demo`: `can` (in-memory virtual bus), `udp` or `tcp` |
| **Measurements** | | Groups of A2L measurement names. **Event** `default` samples each signal on the event its A2L entry names (its fixed event first); or name one ECU event (`10ms`) for the whole group, or `poll` with a **Rate (ms)** |
| **Signals** / **Signal List File** | | Names typed in the form, a list file, or both. The file is plain text, one name per line; a `.lab` label file works, its rates are ignored |

Names are rejected, not renamed, when they hold anything else. `log` and `xcp_log` are reserved ECU names.

> **One tool per ECU.** The extension connects to every configured ECU when it starts, including when the agent starts it unattended, and reconnects after a loss. An ECU serves one XCP tool at a time, and XCP has no way to check for an existing session first. Connecting can end another tool's session (CANape, INCA) with no error on either side. Stop the extension, or remove the ECU, before another tool goes online.

### Advanced Settings

One value each, applied to every ECU.

| Setting | Default | Description |
|---|---|---|
| `prefix` | `XCP` | Trace source every ECU publishes under |
| `log_level` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `timestamp_mode` | `auto` | `auto`: the ECU's DAQ timestamp plus one offset to local time, fixed at measurement start. Without an ECU timestamp: the CAN adapter's frame time plus one offset, else local receive time. `host`: always receive time |
| `epk_check` | `strict` | `strict` refuses to measure when the A2L's EPK does not match the ECU, or cannot be read; `warn` logs it and measures |
| `timeout` | `1.0` | Seconds to wait for each command response |
| `retries` | `1` | Extra attempts per read-only command. DAQ setup is not retried: a lost answer there restarts the session |
| `max_bus_load` | `30` | XCP on CAN: measurement is refused when its DAQ frames are estimated above this percentage of the bus bitrate |

### Trace layout

| What | Prefix `XCP` | Prefix cleared |
|---|---|---|
| ECU events | `XCP/inverter/<event>` | `inverter/<event>` |
| Polled groups | `XCP/inverter/poll_<rate_ms>` | `inverter/poll_<rate_ms>` |
| Extension logs | `XCP/log` | `xcp_log/log` |

One row per ECU sample of an event, holding that event's selected measurements.

## Behaviour

| Situation | What happens |
|---|---|
| EPK mismatch | `strict`: nothing is measured, the ECU shows `error`. `warn`: logged once, measured. No EPK in the A2L: logged once, measured |
| Selection too large | Refused before measurement starts, with the reason: DAQ lists, ODTs, entries, DAQ memory, or CAN bus load over `max_bus_load`. Never trimmed |
| Name not in the A2L | Logged once, listed as `unknown`, the rest measured |
| Measurement that cannot be read exactly | Skipped with its reason, listed as `skipped`: arrays, `FORM`, `TAB_INTP`, `TAB_NOINTP`, `RAT_FUNC` with quadratic terms, multi-byte values with no byte order, `FLOAT16`, bit masks on signed or float values |
| Data stops | The watchdog reports the event as stalled after 10 cycles (at least 1 s). With DAQ running and nothing received for 1 s, `GET_STATUS` probes the ECU |
| ECU lost | Reconnects with backoff, 3 s doubling to 60 s, and restarts measurement |
| Lost or short packets | Ethernet: gaps in the packet counter are counted and logged. CAN has no counter: each event's rows are compared with its cycle and a shortfall is logged. Incomplete samples and packets too short to decode are dropped and counted |
| Stop | Within 3 s in every state: one DAQ stop and one `DISCONNECT`, 0.3 s each, whatever `timeout` and `retries` say |

## A2L

A2L symbols are rewritten to trace field names: `.` to `_`, `[i]` to `_i` (`motor.ctrl.id_ref` reads `motor_ctrl_id_ref`). Two selected symbols that rewrite to one name refuse the ECU, naming both.

| Conversion | Traced as |
|---|---|
| `IDENTICAL` | The ECU value, integer or float type kept |
| `LINEAR`, `RAT_FUNC` | Physical value, float |
| `TAB_VERB` | The raw integer with the table as the field's value table |

Bit masks are applied. Units come from the A2L. The A2L is read leniently: its warnings are logged and listed in `get_status`; a file that cannot be read refuses that ECU with the file and line.

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

`get_status` fields:

| Field | Meaning |
|---|---|
| `state`, `error`, `errors`, `reconnects` | Session state, last error, error and reconnect counts |
| `epk` | A2L and ECU EPK, `match`, `mismatch`, `absent` or `unreadable` |
| `events` | Per event: rows, rate, expected rate, rows the cycle predicts, missing rows, stalled |
| `watchdog` | `stalled` when any event has stopped |
| `lost_packets` | Ethernet packet counter gaps; `null` on CAN |
| `incomplete_rows`, `rejected_packets`, `malformed_datagrams`, `queue_overflow`, `unplaced_rows` | Samples dropped: incomplete, too short to decode, broken UDP framing, host too slow, held by the ECU through a gap too long to time exactly |
| `bus_load` | CAN: per event and total estimate, bitrate, ceiling |
| `timestamps` | Source (`ecu`, `adapter`, `host`), anchors, offset to local time, drift in ppm |
| `a2l_warnings`, `unknown`, `skipped` | A2L reader warnings, names not in the A2L, names skipped with reasons |

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
