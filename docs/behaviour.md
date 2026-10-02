# Behaviour and limits

## One tool per ECU

The extension connects to every configured ECU when it starts, including when the agent starts it unattended, and reconnects after a loss. An ECU serves one XCP tool at a time, and XCP has no way to check for an existing session first. Connecting can end another tool's session (CANape, INCA) with no error on either side. Stop the extension, or remove the ECU, before another tool goes online.

When another tool connects while we measure:

- CAN: the first response to a command we did not send ends the session at once, nothing received after it is decoded: `response to a command we did not send: another master?` (a late answer to our own timed-out command does the same).
- Otherwise, within about a second, the `GET_DAQ_LIST_MODE` check (below) when the other tool's first DAQ list differs from ours (`DAQ configuration changed under us: another master?`), or no answer (UDP).
- The session ends as for a lost ECU: DAQ stop and `DISCONNECT` are sent, and the reconnect after the backoff takes the ECU back; this usually ends the other tool's session.
- On a shared CAN bus the order in which the ECU sees the two masters' commands is not ours to control: the other tool may keep its session, or decode our DAQ packets as its own.
- A foreign response that arrives while one of our commands waits is taken as that command's answer and cannot be told apart.
- TCP: the other tool is not served while we are connected.

## Session

| Situation | What happens |
|---|---|
| EPK mismatch | `strict`: nothing is measured, the ECU shows `error`. `warn`: logged once, measured. No EPK in the A2L: logged once, measured. An EPK read the ECU refuses is tried once more before `strict` refuses |
| Locked ECU | `GET_STATUS` after `CONNECT`: when seed and key protects DAQ (for events) or CAL/PAG (for polls), nothing is measured, `error` reads `locked: seed and key is not supported (DAQ)`, `locked` lists the resources, logged once; retried every 60 s from the first attempt |
| Temporary errors | `ERR_CMD_BUSY`: the command is repeated every 10 ms within `timeout`. `EV_CMD_PENDING`: each one extends the wait by one `timeout`, 5 s at most; a stop still ends it. A DAQ info query not answered ends the session (`... not answered`) and reconnects; refused, it refuses the ECU |
| Retries | `retries` applies to read-only commands. DAQ setup is not retried: a lost answer there restarts the session |
| ECU events | `EV_SESSION_TERMINATED` ends the session; `EV_DAQ_OVERLOAD` is counted. Others are logged at `INFO` the first time per code, then at `DEBUG` |
| Data stops | The watchdog reports the event as stalled after 10 cycles (at least 1 s). With DAQ running and nothing received for 1 s, `GET_STATUS` probes the ECU. Once a second while DAQ runs, `GET_DAQ_LIST_MODE` checks the first DAQ list's mode bits, event and prescaler against what was set (skipped, logged once, on an ECU without the command) |
| ECU lost | Reconnects with backoff, 3 s doubling to 60 s, back to 3 s after a session that measured for 10 s, and restarts measurement |
| Stop | Within 3 s in every state: one DAQ stop and one `DISCONNECT`, 0.3 s each, whatever `timeout` and `retries` say |

## Selection

| Situation | What happens |
|---|---|
| Selection too large | Refused before measurement starts, with the reason: DAQ lists, ODTs, entries, DAQ memory, or CAN bus load over `max_bus_load` when set. Never trimmed |
| Name not in the A2L | Logged once, listed as `unknown`, the rest measured |
| `default` event, measurement with none in the A2L | Polled at the group's `rate_ms` (100 ms when unset) in `poll_<rate_ms>`, logged once, listed as `polled_no_default_event` |
| Measurement that cannot be read exactly | Skipped with its reason, listed as `skipped`, not a field of the trace event: arrays and their elements (`name[i]`), `FORM`, `TAB_INTP`, `TAB_NOINTP`, `RAT_FUNC` with quadratic terms, multi-byte values with no byte order (the reason names a deprecated `LITTLE_ENDIAN` / `BIG_ENDIAN`), bit masks on signed or float values |
| Values too large | Measured by DAQ: values larger than one DAQ packet of the ECU (8-byte values on classic CAN). Polled: values larger than one response, on an ECU without slave block mode (8-byte values on classic CAN). Skipped as above |
| Names | Rejected, not renamed, when they hold anything but letters, digits, space, `_`, `-`. `log` and `xcp_log` are reserved ECU names |
| Signal list file | Plain text, one name per line; a `.lab` label file works, its rates are ignored |

## Polling

Signals of a group whose memory touches or overlaps, same address extension, are read together: `SHORT_UPLOAD` when one response holds them, else `SET_MTA` and one `UPLOAD` the ECU answers in several packets (slave block mode). Never across a gap, never one value over two commands. A read the ECU refuses splits that run into single reads. A group that falls a cycle behind skips the cycles missed (`missed_cycles`), warned once.

A polled row is stamped at the middle of the group's read window (`poll_window_ms`). Its values are read one after another, not from one ECU task run: poll rows are not task-consistent; use an ECU event (DAQ) where consistency matters.

`missed_cycles`: cycles skipped to keep the schedule. `poll_window_ms`: the last poll's read time. `frames_per_cycle`: command and response packets per poll.

## Timestamps

`timestamp_mode` `auto`: the ECU's DAQ timestamp plus one offset to local time, fixed at measurement start. Without an ECU timestamp: the CAN adapter's frame time plus one offset, else local receive time. `host`: always receive time.

A receive gap that leaves the ECU timestamp ambiguous takes a fresh anchor, counted in `timestamps.gaps`. A row held by the ECU through a gap too long to time exactly is dropped and counted in `unplaced_rows`.

## Loss accounting

- Ethernet: gaps in the packet counter are counted and logged.
- CAN has no counter: each event's rows are compared with its cycle and a shortfall is logged.
- Incomplete samples and packets not of their ODT's length are dropped and counted; on CAN a packet filled to DLC 8 (CAN FD: the next frame length) is read.
- `expected_rows`, `missing_rows`, `lost_packets`, `incomplete_rows`, `rejected_packets`, `queue_overflow`, `stale_responses` and `daq_overloads` count across reconnects.
- `stale_responses`: responses that came after their command was given up on, dropped. `daq_overloads`: overloads the ECU reports (event or PID MSB), each dropping the samples in progress.
- A value the A2L marks as a status (`STATUS_STRING_REF`) is written as null and counted in `status_values`.

## Frame logging

`debug_frames` logs every command, response and event packet at `DEBUG` (direction, PID, bytes), and DAQ packets per second, never their data.

## Transports

| Item | Handling |
|---|---|
| `ssh-socketcan` | Not supported: request and response timing over SSH does not fit XCP |
| CAN ids | Empty: `CAN_ID_MASTER` / `CAN_ID_SLAVE` of the A2L's XCP on CAN section; neither set: the ECU does not start |
| `MAX_DLC_REQUIRED` (or in `CAN_FD`) | Command frames padded to DLC 8 with `0x00` |
| `DAQ_LIST_CAN_ID`, `EVENT_CAN_ID_LIST` (`FIXED`) | The master receives on the response id only: a DAQ list or event assigned another id refuses the ECU, naming the id |
| CAN bus load | `max_bus_load`: measurement is refused when its DAQ frames are estimated above this percentage of the bus bitrate; empty: no limit, the estimate is only reported. The bitrate comes from the interface settings or the A2L; with neither (SocketCAN and an A2L without one), there is no estimate and a warning |
| Ethernet fill bytes | UDP: packets filled to a 2 or 4 byte boundary are read, the smallest alignment that frames the whole datagram. TCP: not supported. A stream has no datagram end to tell fill from the next header, so the alignment would have to be stated, and neither the A2L nor the settings state it; a TCP slave that fills its packets breaks framing. Use UDP with such a slave |

## A2L

- Symbols are rewritten to trace field names: `.` to `_`, `[i]` to `_i` (`motor.ctrl.id_ref` reads `motor_ctrl_id_ref`). Two selected symbols that rewrite to one name refuse the ECU, naming both.
- Bit masks are applied. Units come from the A2L. `FLOAT16_IEEE` is traced as a 32-bit float.
- The A2L is read leniently: its warnings are logged and listed in `get_status`; a file that cannot be read refuses that ECU with the file and line.
- The DAQ block and events of the transport in use (`XCP_ON_CAN`, `XCP_ON_UDP_IP`, `XCP_ON_TCP_IP`) overrule the module-level ones when it has them; with several Ethernet sections of one protocol, the configured port picks one.
- `a2l_sha256` in `session` covers the top-level file, not `/include`d ones.

## Demo ECU

| Item | Behaviour |
|---|---|
| Model | A small traction inverter: speed, torque, DC bus, phase currents, temperatures, state enum, 96 cell voltages. Values are exact functions of ECU time (`DemoEcu.expected()`) |
| Events | `10ms` (channel 0) and `100ms` (channel 1). A variable holds the value of its task's last run |
| DAQ | Dynamic, absolute ODT PIDs, fixed timestamp in the first ODT (4 bytes, 1 us; 0, 1 or 2 bytes selectable). All ODTs of a sample come from one snapshot, in order |
| XCP on CAN | Ids 0x7F0 (master to slave) and 0x7F1, classic CAN (8 bytes, unpadded) or CAN FD (64, padded to FD lengths). Default bus: the in-process `zelos_can` virtual bus, which serves tests, not other programs; `--interface` takes any python-can bus. A receive error (interface down) closes the bus, reopened with backoff (0.5 s doubling to 5 s) |
| Uploads | `SHORT_UPLOAD`, and `UPLOAD` in slave block mode: up to 255 bytes, answered in as many packets as it takes (`block_mode=False`: one packet) |
| Refused | Every write, page, store, flash, seed/key, user command and STIM list: an XCP error, counted, never executed |
| Test hooks | `protected` (`--protected daq`, `calpag`): flagged in `GET_STATUS`, those commands answered `ERR_ACCESS_LOCKED`. `can_max_dlc_required`: command frames under DLC 8 ignored. `fail()`: the next commands answered with an error, `ERR_CMD_BUSY` by default. `respond_late(pending_every=)`: `EV_CMD_PENDING` while a response waits |
| Default selection | `--demo`: about 740 DAQ frames/s, 14 % of a 500 kbit/s bus measured on SocketCAN (estimate 20 %) |
| Second master | UDP: a CONNECT from another address takes the session, the first master is not told. TCP: the second connection waits unanswered until the first closes. CAN: any CONNECT restarts the session |
| `demo.a2l` | Generated from the model table: `uv run python -m zelos_extension_xcp.demo.a2l > zelos_extension_xcp/demo/demo.a2l`. A test fails when they differ |
| CAN tests on Linux | `XCP_DEMO_CAN_INTERFACE=socketcan XCP_DEMO_CAN_CHANNEL=vcan0 just test` runs them on `vcan0` |
