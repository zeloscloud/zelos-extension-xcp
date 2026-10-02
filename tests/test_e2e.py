"""End to end against a running XCP slave, asserting on the written trace.

Targets:
- `can`, `can-ts2`, `can-ts0`, `udp`, `tcp`: the demo ECU
  (`zelos_extension_xcp.demo.DemoEcu`) in-process, reached through the real
  interface path. CAN runs on python-can's `virtual` bus; `-ts2` and `-ts0`
  give the ECU a 2-byte or no DAQ timestamp. Skipped until the demo package
  exists.
- `xcplite-udp`, `xcplite-tcp`: a slave built on Vector XCPlite, named by
  `XCP_TEST_SLAVE` (`<exe> <port> <tcp 0|1> <fast_hz>`, writes its A2L to its
  working directory). Skipped when unset. Its `sig_NNN` hold `counter + NNN`.

Signals are picked from each target's A2L, so one suite runs on every target.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import shutil
import signal
import socket
import struct
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import pytest
import zelos_sdk
from conftest import wait_until

from zelos_extension_xcp import a2l as xa2l
from zelos_extension_xcp import client as xclient
from zelos_extension_xcp.client import State, XcpConnection
from zelos_extension_xcp.constants import field_name
from zelos_extension_xcp.guard import ALLOWED, CommandRefused

HOST = "127.0.0.1"
SLAVE = os.environ.get("XCP_TEST_SLAVE")
DOWNLOAD = 0xF0
ALL = ["can", "udp", "tcp", "xcplite-udp", "xcplite-tcp"]
TIMESTAMPS = ["can", "can-ts2", "can-ts0", "udp", "tcp", "xcplite-udp", "xcplite-tcp"]


# ─── Targets ────────────────────────────────────────────────────────────────


def free_udp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind((HOST, 0))
        return s.getsockname()[1]


class XcpLite:
    """The XCPlite slave process in its own directory, disturbed by signals."""

    def __init__(self, cwd: Path, transport: str) -> None:
        self.cwd, self.tcp = cwd, transport == "tcp"
        self.port = free_udp_port()
        self.proc: subprocess.Popen | None = None
        cwd.mkdir(parents=True, exist_ok=True)

    def start(self) -> XcpLite:
        self.proc = subprocess.Popen(
            [SLAVE, str(self.port), "1" if self.tcp else "0", "1000"],
            cwd=self.cwd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        assert wait_until(lambda: list(self.cwd.glob("*.a2l")), timeout=5)
        time.sleep(0.2)
        return self

    @property
    def a2l_path(self) -> Path:
        return next(self.cwd.glob("*.a2l"))

    def signal(self, sig: int) -> None:
        if self.proc and self.proc.poll() is None:
            os.kill(self.proc.pid, sig)

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.signal(signal.SIGCONT)
            self.proc.terminate()
            try:
                self.proc.wait(3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()


def start_target(name: str, tmp_path: Path, channel: str) -> Any:
    """The slave for target `name`, started."""
    kind, _, transport = name.partition("-") if name.startswith("xcplite") else ("demo", "", name)
    if kind == "xcplite":
        if not SLAVE:
            pytest.skip("XCP_TEST_SLAVE is not set")
        return XcpLite(tmp_path / f"slave-{channel[-4:]}", transport).start()
    demo = pytest.importorskip("zelos_extension_xcp.demo")
    transport, _, ts = transport.partition("-ts")
    kw: dict[str, Any] = {"timestamp_size": int(ts)} if ts else {}
    if transport == "can":
        kw.update(interface="virtual", channel=channel)
    return demo.DemoEcu(transport=transport, host=HOST, port=0, **kw).start()


class Target:
    """A slave, how to reach it, and how to disturb it."""

    def __init__(self, name: str, tmp_path: Path) -> None:
        self.name = name
        self.kind = "xcplite" if name.startswith("xcplite") else "demo"
        self.transport = name.removeprefix("xcplite-").partition("-")[0]
        _, _, ts = name.partition("-ts")
        self.ts_size = int(ts) if ts else 4
        self.channel = f"xcp-e2e-{os.getpid()}-{id(self):x}"
        self.handle = start_target(name, tmp_path, self.channel)
        self.timers: list[threading.Timer] = []
        self.a2l = Path(self.handle.a2l_path)
        self.catalog = xa2l.load(str(self.a2l))
        self.fields = {field_name(m["name"]): m["name"] for m in self.catalog["measurements"]}

    def another(self, tmp_path: Path) -> Target:
        """A second slave of the same kind."""
        return Target(self.name, tmp_path)

    def stop(self) -> None:
        for t in self.timers:  # a restart must not outlive the test
            t.cancel()
            t.join()
        self.handle.stop()

    def later(self, seconds: float, fn, *args) -> None:
        t = threading.Timer(seconds, fn, args)
        self.timers.append(t)
        t.start()

    @property
    def interface(self) -> str:
        return "other" if self.transport == "can" else self.transport

    def link(self, port: int | None = None) -> dict[str, Any]:
        if self.transport == "can":
            bus = {"interface": "virtual", "channel": self.channel}
            return {"config_json": json.dumps(bus)}
        return {"host": HOST, "port": port or self.handle.port}

    def ecu(self, measurements, name="ecu", a2l: Path | None = None, port=None, **kw):
        return XcpConnection(
            name, self.interface, str(a2l or self.a2l), measurements, self.link(port), **kw
        )

    def pause(self, seconds: float) -> None:
        """Quiet for `seconds`, the ECU clock running."""
        if self.kind == "demo":
            self.handle.stall(seconds)
        else:
            self.handle.signal(signal.SIGSTOP)
            self.later(seconds, self.handle.signal, signal.SIGCONT)

    def outage(self, seconds: float) -> None:
        """Gone for `seconds`, then back."""
        if self.kind == "demo":
            self.handle.stall(seconds)
        else:
            self.handle.signal(signal.SIGKILL)
            self.handle.proc.wait()
            self.later(seconds, self.handle.start)

    def silence(self) -> None:
        if self.kind == "demo":
            self.handle.go_silent()
        else:
            self.handle.signal(signal.SIGSTOP)

    @property
    def wrap_s(self) -> float | None:
        """DAQ timestamp wrap period, None without timestamps."""
        if not self.ts_size:
            return None
        tick = 1e-9 if self.kind == "xcplite" else 1e-6
        return 2 ** (8 * self.ts_size) * tick

    # A2L picks

    def signals(self, channel: int | None = None, max_size: int = 8) -> list[dict]:
        """Measurable scalars of at most `max_size` bytes, `channel` only when given."""
        out = []
        for m in self.catalog["measurements"]:
            ev = m["events"]
            default = (ev.get("fixed") or ev.get("default") or [None])[0]
            if default is None or channel is not None and default != channel:
                continue
            try:
                s = xa2l.signal(m)
            except ValueError:
                continue
            if s.size <= max_size:
                out.append(m)
        return out

    def events(self) -> list[dict]:
        """Events with a cycle and measurements, fastest first."""
        found = [
            e for e in self.catalog["events"] if xa2l.cycle_ns(e) and self.signals(e["channel"])
        ]
        return sorted(found, key=xa2l.cycle_ns)

    def names(self, channel: int, n: int) -> list[str]:
        """Up to `n` measurements of `channel` that fit one classic CAN ODT entry."""
        ms = sorted(
            self.signals(channel, max_size=4), key=lambda m: (m["name"] != "counter", m["name"])
        )
        return [m["name"] for m in ms[:n]]


@pytest.fixture(params=ALL)
def target(request, tmp_path):
    pytest.importorskip("zelos_can.a2l")
    t = Target(request.param, tmp_path)
    try:
        yield t
    finally:
        t.stop()


# ─── Harness ────────────────────────────────────────────────────────────────


@contextlib.contextmanager
def measuring(trz: Path, *connections: XcpConnection):
    """Run `connections` in one event loop as the app does; stop them within 3 s on exit."""
    with zelos_sdk.TraceWriter(str(trz)):
        source = zelos_sdk.TraceSource("XCP")
        for c in connections:
            c.start("XCP", source)

        async def run_all():
            await asyncio.gather(*(c.run() for c in connections))

        loop = threading.Thread(target=asyncio.run, args=(run_all(),))
        loop.start()
        try:
            yield connections[0]
        finally:
            t0 = time.monotonic()
            for c in connections:
                c.stop()
            assert all(c.join(3.0) for c in connections)
            loop.join(3.0)
            assert time.monotonic() - t0 < 3.0
    ours = {f"xcp-{c.name}" for c in connections}
    left = [t.name for t in threading.enumerate() if t.name in ours or "listen)" in t.name]
    assert not left


def table(trz: Path, event: str):
    """Arrow table of `XCP/<event>`, or None without rows. Segments one by one: a
    writer opened later in a process also gets empty segments of older sources."""
    import pyarrow as pa

    tables = []
    with zelos_sdk.TraceReader(str(trz)) as reader:
        try:
            span = reader.time_range()
        except RuntimeError:  # a trace with no rows at all
            return None
        for seg in reader.list_data_segments():
            fields = [
                f.path
                for src in reader.list_fields(seg.id)
                for ev in src.events
                if f"{src.name}/{ev.name}" == f"XCP/{event}"
                for f in ev.fields
            ]
            if not fields or span is None:
                continue
            result = reader.query(
                data_segment_ids=[seg.id], fields=fields, start=span.start, end=span.end
            )
            if result.arrow_data:
                t = pa.ipc.open_stream(result.to_arrow()).read_all()
                if t.num_rows:
                    tables.append(t)
    return pa.concat_tables(tables) if tables else None


def read(trz: Path, event: str) -> dict[str, list]:
    """Columns of `XCP/<event>` by field name, and `time_s`. Empty without rows."""
    t = table(trz, event)
    if t is None:
        return {}
    return {name.rsplit(".", 1)[-1]: t.column(name).to_pylist() for name in t.column_names}


def arrow_types(trz: Path, event: str) -> dict[str, str]:
    return {f.name.rsplit(".", 1)[-1]: str(f.type) for f in table(trz, event).schema}


def trace_catalog(trz: Path, what: str) -> dict | None:
    """From the trace's own catalog, via the duckdb CLI: units ({`event.field`: unit})
    or value tables ({`event.field`: {key: text}}). None without the CLI: the SDK
    reader exposes neither for a nested event name."""
    duckdb = shutil.which("duckdb")
    if not duckdb:
        return None
    name = "catalog" if what == "units" else "catalog/values"

    def query(sql: str) -> list[dict]:
        out = subprocess.run(
            [duckdb, "-readonly", "-json", str(trz), "-c", sql],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        return json.loads(out or "[]")

    schemas = query(
        f"select table_schema s from information_schema.tables where table_name='{name}'"
    )
    out: dict = {}
    for s in schemas:
        if what == "units":
            for r in query(f'select message m, signal f, unit u from "{s["s"]}".catalog'):
                out[f"{r['m']}.{r['f']}"] = r["u"] or ""
        else:
            rows = query(
                f'select message m, signal f, key::varchar k, value v from "{s["s"]}"."{name}"'
            )
            for r in rows:
                out.setdefault(f"{r['m']}.{r['f']}", {})[int(float(r["k"]))] = r["v"]
    return out


def steps(values: list) -> list:
    return [b - a for a, b in zip(values, values[1:], strict=False)]


ARROW = {"UBYTE": "uint8", "SBYTE": "int8", "UWORD": "uint16", "SWORD": "int16",
         "ULONG": "uint32", "SLONG": "int32", "A_UINT64": "uint64", "A_INT64": "int64",
         "FLOAT32_IEEE": "float", "FLOAT64_IEEE": "double"}  # fmt: skip


def check_values(target: Target, rows: dict, status: dict, cycle_s: float) -> None:
    """Values are right: XCPlite's `counter + i`, or the demo ECU's own model at the
    row's ECU time (trace time minus the reported clock offset)."""
    if "counter" in rows:
        for name, values in rows.items():
            if name.startswith("sig_"):
                i = int(name[4:])
                assert values == [(k + i) & 0xFFFFFFFF for k in rows["counter"]], name
    offset = status["timestamps"].get("offset_s")
    physical = getattr(target.handle, "physical", None)
    if target.kind != "demo" or physical is None or status["timestamps"]["source"] != "ecu":
        return
    for field, values in rows.items():
        if field == "time_s":
            continue
        name = target.fields[field]
        for t, v in zip(rows["time_s"], values, strict=True):
            # The row is the task run at k * cycle; half a cycle absorbs the offset error.
            want = physical(name, t - offset + cycle_s / 2)
            assert v == pytest.approx(want, rel=1e-6, abs=1e-9), (name, t)


def info(target: Target) -> dict[str, dict]:
    return {m["name"]: m for m in target.catalog["measurements"]}


def segment(event: dict) -> str:
    return xa2l.event_segment(event["name"])


# ─── Cases ──────────────────────────────────────────────────────────────────


# 1
def test_polled_read_values_typed_with_units(target, tmp_path):
    names = [m["name"] for m in target.signals(max_size=4)][:6]
    meta = info(target)
    c = target.ecu([{"event": "poll", "rate_ms": 50, "signals": names}])
    trz = tmp_path / "t.trz"
    with measuring(trz, c):
        assert wait_until(lambda: c.events["poll_50"].rows >= 10, timeout=6), c.last_error
        one = c.read(names[0])
        assert one["unit"] == (meta[names[0]]["unit"] or "")
    rows = read(trz, "ecu/poll_50")
    types = arrow_types(trz, "ecu/poll_50")
    for name in names:
        m, values = meta[name], rows[field_name(name)]
        assert len(values) >= 10 and None not in values
        converted = m["conversion"]["kind"] in ("LINEAR", "RAT_FUNC")
        assert types[field_name(name)] == ("double" if converted else ARROW[m["datatype"]])
        assert all(m["lower"] - 1e-6 <= v <= m["upper"] + 1e-6 for v in values), name
    units = trace_catalog(trz, "units")
    if units is not None:
        for name in names:
            assert units[f"ecu/poll_50.{field_name(name)}"] == (meta[name]["unit"] or "")


# 2
@pytest.mark.parametrize("target", TIMESTAMPS, indirect=True)
def test_daq_one_event(target, tmp_path):
    event = target.events()[0]
    names = target.names(event["channel"], 10)
    c = target.ecu([{"event": event["name"], "signals": names}])
    trz = tmp_path / "t.trz"
    with measuring(trz, c):
        time.sleep(2.5)
        status = c.status()
    rows = read(trz, f"ecu/{segment(event)}")
    n = len(rows["time_s"])
    span = rows["time_s"][-1] - rows["time_s"][0]
    cycle = xa2l.cycle_ns(event) / 1e9
    assert n > 1.5 / cycle, status["error"]
    assert n == pytest.approx(span / cycle + 1, rel=0.02)
    # >= : an ECU catching up can sample faster than the trace's float seconds resolve.
    assert all(d >= 0 for d in steps(rows["time_s"]))
    assert all(None not in rows[field_name(name)] for name in names)
    assert any(len(set(rows[field_name(name)])) > 1 for name in names)
    ev = status["events"][segment(event)]
    assert ev["missing_rows"] == 0 and status["incomplete_rows"] == 0
    assert status["lost_packets"] == (None if target.transport == "can" else 0)
    expected_source = (
        "ecu" if target.ts_size else ("adapter" if target.transport == "can" else "host")
    )
    assert status["timestamps"]["source"] == expected_source
    check_values(target, rows, status, cycle)


# 3
def test_list_spanning_several_odts(target, tmp_path):
    event = target.events()[0]
    # Several ODTs: classic CAN carries 7 bytes per ODT, XCPlite about 1 kB; the
    # demo's 10 ms task holds ~100 bytes, so its Ethernet DTO is shrunk.
    count = 4 if target.transport == "can" else 300
    if target.kind == "demo" and target.transport != "can":
        target.handle.stop()
        demo = pytest.importorskip("zelos_extension_xcp.demo")
        target.handle = demo.DemoEcu(transport=target.transport, host=HOST, port=0, max_dto=32)
        target.handle.start()
    names = target.names(event["channel"], count)
    c = target.ecu([{"event": event["name"], "signals": names}], max_bus_load=100)
    trz = tmp_path / "t.trz"
    with measuring(trz, c):
        assert wait_until(lambda: c.state == State.CONNECTED, timeout=6), c.last_error
        check = c.check_selection()
        time.sleep(1.5)
        status = c.status()
    assert check["fits"] and check["events"][segment(event)]["odts"] >= 2, check
    rows = read(trz, f"ecu/{segment(event)}")
    assert len(rows["time_s"]) > 50
    assert all(None not in rows[field_name(name)] for name in names)
    assert status["incomplete_rows"] == 0 and status["events"][segment(event)]["missing_rows"] == 0
    check_values(target, rows, status, xa2l.cycle_ns(event) / 1e9)


# 4
def test_two_events_at_different_rates(target, tmp_path):
    events = target.events()[:2]
    assert len(events) == 2 and xa2l.cycle_ns(events[0]) != xa2l.cycle_ns(events[1])
    c = target.ecu([{"event": e["name"], "signals": target.names(e["channel"], 2)} for e in events])
    trz = tmp_path / "t.trz"
    with measuring(trz, c):
        time.sleep(3.0)
        status = c.status()
    for e in events:
        rows = read(trz, f"ecu/{segment(e)}")
        rate = (len(rows["time_s"]) - 1) / (rows["time_s"][-1] - rows["time_s"][0])
        assert rate == pytest.approx(1e9 / xa2l.cycle_ns(e), rel=0.03)
        assert status["events"][segment(e)]["missing_rows"] == 0
        check_values(target, rows, status, xa2l.cycle_ns(e) / 1e9)


# 5
@pytest.mark.parametrize("target", TIMESTAMPS, indirect=True)
def test_timestamps_continuous_across_counter_wrap_and_gap(target, tmp_path):
    wrap = target.wrap_s
    if wrap is None or wrap > 10:
        pytest.skip(f"no DAQ timestamp wrap within 10 s ({wrap})")
    event = target.events()[-1]  # slowest: a short catch-up burst after the pause
    pause = wrap + 1.0
    # A long timeout: the pause reads as a gap in the data, not as a lost ECU. A
    # probe sent into the pause may be discarded; its retry is answered.
    timeout = pause + 0.5
    c = target.ecu(
        [{"event": event["name"], "signals": target.names(event["channel"], 1)}], timeout=timeout
    )
    trz = tmp_path / "t.trz"
    with measuring(trz, c):
        time.sleep(max(1.2 * wrap, 1.0))  # crosses a counter wrap
        target.pause(pause)
        time.sleep(1.0 + timeout + 2.0)
        status = c.status()
    rows = read(trz, f"ecu/{segment(event)}")
    t = rows["time_s"]
    d = steps(t)
    assert all(x >= 0 for x in d)
    gap = max(range(len(d)), key=d.__getitem__)
    cycle = xa2l.cycle_ns(event) / 1e9
    assert max(d[:gap]) < 3 * cycle
    assert t[gap] - t[0] == pytest.approx(gap * cycle, rel=0.02)
    assert d[gap] == pytest.approx(pause, abs=0.3)  # not short by a wrap
    assert status["timestamps"]["source"] == "ecu" and status["timestamps"]["anchors"] >= 2
    check_values(target, rows, status, cycle)


# 6
@pytest.mark.parametrize("mode", ["strict", "warn"])
def test_epk_mismatch(target, tmp_path, mode):
    epk = target.catalog["epk"]["string"]
    wrong = epk[:-1] + ("0" if epk[-1] != "0" else "1")
    stale = tmp_path / "stale.a2l"
    stale.write_text(target.a2l.read_text().replace(f'EPK "{epk}"', f'EPK "{wrong}"'))
    event = target.events()[0]
    c = target.ecu(
        [{"event": event["name"], "signals": target.names(event["channel"], 1)}],
        a2l=stale,
        epk_check=mode,
    )
    trz = tmp_path / "t.trz"
    with measuring(trz, c):
        wait_until(lambda: c.status()["epk"]["result"] == "mismatch", timeout=6)
        time.sleep(1.0)
        status = c.status()
    assert status["epk"] == {"a2l": wrong, "ecu": epk, "result": "mismatch"}
    rows = read(trz, f"ecu/{segment(event)}").get("time_s", [])
    if mode == "strict":
        assert status["state"] == "error" and "EPK mismatch" in status["error"]
        assert rows == []
    else:
        assert status["state"] == "connected" and len(rows) > 10


class UdpRelay:
    """Loopback relay recording the PID of every master-to-slave packet."""

    def __init__(self, slave_port: int) -> None:
        self.front = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.front.bind((HOST, 0))
        self.port = self.front.getsockname()[1]
        self.back = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.back.connect((HOST, slave_port))
        for s in (self.front, self.back):
            s.settimeout(0.05)
        self.client = None
        self.pids: list[int] = []
        self.done = threading.Event()
        self.threads = [threading.Thread(target=f) for f in (self._m2s, self._s2m)]
        for t in self.threads:
            t.start()

    def _m2s(self):
        while not self.done.is_set():
            try:
                data, self.client = self.front.recvfrom(65535)
            except (TimeoutError, OSError):
                continue
            i = 0
            while i + 4 < len(data):
                self.pids.append(data[i + 4])
                i += 4 + struct.unpack_from("<H", data, i)[0]
            self.back.send(data)

    def _s2m(self):
        while not self.done.is_set():
            try:
                data = self.back.recv(65535)
            except (TimeoutError, OSError):
                continue
            if self.client:
                self.front.sendto(data, self.client)

    def close(self):
        self.done.set()
        for t in self.threads:
            t.join(1)
        self.front.close()
        self.back.close()


class CanSniffer:
    """Another node on the virtual bus recording the PID of every command frame."""

    def __init__(self, channel: str, tx_id: int) -> None:
        import can

        self.bus = can.Bus(interface="virtual", channel=channel)
        self.tx_id, self.pids, self.dlcs = tx_id, [], set()
        self.done = threading.Event()
        self.thread = threading.Thread(target=self._run)
        self.thread.start()

    def _run(self):
        while not self.done.is_set():
            m = self.bus.recv(0.05)
            if m is not None and m.arbitration_id == self.tx_id and m.data:
                self.pids.append(m.data[0])
                self.dlcs.add(m.dlc)

    def close(self):
        self.done.set()
        self.thread.join(1)
        self.bus.shutdown()


# 7
def test_wire_carries_only_allowlisted_commands(target, tmp_path):
    if target.transport == "tcp":
        pytest.skip("wire capture covers the UDP and CAN send paths")
    event = target.events()[0]
    poll = target.names(event["channel"], 1)
    groups = [
        {"event": event["name"], "signals": target.names(event["channel"], 2)},
        {"event": "poll", "rate_ms": 100, "signals": poll},
    ]
    if target.transport == "can":
        from zelos_extension_xcp.can_link import ids

        tap = CanSniffer(target.channel, ids({}, target.catalog)[0])
        c = target.ecu(groups)
    else:
        tap = UdpRelay(target.handle.port)
        c = target.ecu(groups, port=tap.port)
    try:
        with measuring(tmp_path / "t.trz", c):
            assert wait_until(lambda: c.state == State.CONNECTED, timeout=6), c.last_error
            time.sleep(0.5)
            c.read(poll[0])
            c.check_selection()
            time.sleep(0.2)
            sent = len(tap.pids)
            with pytest.raises(CommandRefused, match="DOWNLOAD"):
                c._master.download(b"\x2a")
            with pytest.raises(CommandRefused, match="STIM"):
                c._master.setDaqListMode(0x12, 0, 0, 1, 0)
            time.sleep(0.2)
            assert DOWNLOAD not in tap.pids[sent:]
    finally:
        tap.close()
    seen = sorted(set(tap.pids))
    print(f"{target.name} command codes on the wire:", [f"0x{p:02X}" for p in seen])
    assert seen and set(seen) <= {int(cmd) for cmd in ALLOWED}
    if target.kind == "demo":
        assert target.handle.stats["refused"] == {}


# 8
def test_selection_over_capacity_refused_before_start(target, tmp_path):
    event = target.events()[0]
    names = [m["name"] for m in target.signals(event["channel"], max_size=4)]
    if target.kind == "demo":
        # Past the demo's DAQ memory: more ODT entries than it allocates in all.
        target.handle.stop()
        demo = pytest.importorskip("zelos_extension_xcp.demo")
        kw = (
            {"interface": "virtual", "channel": target.channel} if target.transport == "can" else {}
        )
        target.handle = demo.DemoEcu(
            transport=target.transport, host=HOST, port=0, max_odt_entries=2, max_odt=2, **kw
        ).start()
    c = target.ecu([{"event": event["name"], "signals": names}], max_bus_load=100)
    trz = tmp_path / "t.trz"
    with measuring(trz, c):
        assert wait_until(lambda: c.state == State.ERROR, timeout=6)
        status = c.status()
    assert "does not fit" in status["error"]
    assert read(trz, f"ecu/{segment(event)}") == {}


@pytest.mark.parametrize("ceiling", ["set", "unset"])
def test_can_bus_load_reported_and_ceiling_enforced(target, tmp_path, ceiling):
    if target.transport != "can":
        pytest.skip("CAN only")
    event = target.events()[0]
    if ceiling == "unset":  # reported, never refused
        names = [m["name"] for m in target.signals(event["channel"], max_size=4)]
        c = target.ecu([{"event": event["name"], "signals": names}])
        with measuring(tmp_path / "a.trz", c):
            assert wait_until(lambda: c.events[segment(event)].rows > 20, timeout=6), c.last_error
            check = c.check_selection()
            status = c.status()
        assert check["fits"] and check["bus_load"]["total_pct"] > 30
        assert status["bus_load"]["ceiling_pct"] is None
        assert status["bus_load"]["bitrate_source"] == "A2L"
        return
    names = target.names(event["channel"], 4)
    c = target.ecu([{"event": event["name"], "signals": names}], max_bus_load=100)
    with measuring(tmp_path / "a.trz", c):
        assert wait_until(lambda: c.state == State.CONNECTED, timeout=6), c.last_error
        check = c.check_selection()
    ev = check["events"][segment(event)]
    load = check["bus_load"]["total_pct"]
    assert ev["frames_per_s"] == pytest.approx(ev["odts"] * 1e9 / xa2l.cycle_ns(event))
    assert 0 < load < 100
    tight = target.ecu([{"event": event["name"], "signals": names}], max_bus_load=load / 2)
    with measuring(tmp_path / "b.trz", tight):
        assert wait_until(lambda: tight.state == State.ERROR, timeout=6)
    assert "ceiling" in tight.last_error and f"{load:.1f}%" in tight.last_error
    assert read(tmp_path / "b.trz", f"ecu/{segment(event)}") == {}


# 9
def test_slave_loss_watchdog_stop_and_recovery(target, tmp_path):
    event = target.events()[0]
    c = target.ecu([{"event": event["name"], "signals": target.names(event["channel"], 1)}])
    seg = segment(event)
    trz = tmp_path / "t.trz"
    outage = 6.0
    with measuring(trz, c):
        assert wait_until(lambda: c.events[seg].rows > 50, timeout=6), c.last_error
        target.outage(outage)
        lost = time.monotonic()
        assert wait_until(lambda: c.status()["watchdog"] == "stalled", timeout=5)
        to_watchdog = time.monotonic() - lost
        assert wait_until(lambda: c.state != State.CONNECTED, timeout=10)
        to_loss = time.monotonic() - lost
        rows = c.events[seg].rows
        assert wait_until(lambda: c.events[seg].rows > rows + 50, timeout=40)
        to_recover = time.monotonic() - lost - outage
        assert c.reconnects == 1 and c.state == State.CONNECTED
        target.silence()  # stop must still end within the bound (asserted on exit)
        time.sleep(1.5)
        silent_stop = time.monotonic()
    print(
        f"{target.name}: loss to watchdog {to_watchdog:.2f}s, to reconnecting {to_loss:.2f}s, "
        f"back {to_recover:.2f}s after the ECU returned, stop with a silent ECU "
        f"{time.monotonic() - silent_stop:.2f}s"
    )
    assert len(read(trz, f"ecu/{seg}")["time_s"]) > 100


def test_faults_counted_not_decoded(target, tmp_path):
    if target.kind != "demo":
        pytest.skip("fault injection is a demo ECU hook")
    event = target.events()[0]
    c = target.ecu(
        [{"event": event["name"], "signals": target.names(event["channel"], 6)}], max_bus_load=100
    )
    seg = segment(event)
    trz = tmp_path / "t.trz"
    with measuring(trz, c):
        assert wait_until(lambda: c.events[seg].rows > 20, timeout=6), c.last_error
        target.handle.drop_dto(3)
        target.handle.truncate_dto(2)
        time.sleep(2.5)
        status = c.status()
        target.handle.emit_event(EV_SESSION_TERMINATED)  # counters span sessions
        assert wait_until(lambda: c.reconnects == 1 and c.state == State.CONNECTED, timeout=10)
        after = c.status()
    assert status["state"] == "connected" and status["rejected_packets"] == 2
    if target.transport == "can":
        assert status["events"][seg]["missing_rows"] >= 1
    else:
        assert status["lost_packets"] == after["lost_packets"] == 3
    assert after["rejected_packets"] == 2
    assert after["events"][seg]["expected_rows"] >= status["events"][seg]["expected_rows"]
    assert after["events"][seg]["missing_rows"] >= status["events"][seg]["missing_rows"]
    rows = read(trz, f"ecu/{seg}")
    check_values(target, rows, status, xa2l.cycle_ns(event) / 1e9)


# 10
def test_unknown_and_skipped_names_reported(target, tmp_path):
    event = target.events()[0]
    keep, victim = target.names(event["channel"], 2)
    text = target.a2l.read_text()
    begin = text.index(f"/begin MEASUREMENT {victim} ")
    end = text.index("/end MEASUREMENT", begin)
    edited = tmp_path / "edited.a2l"
    edited.write_text(text[:end] + " MATRIX_DIM 2 " + text[end:])
    c = target.ecu(
        [{"event": event["name"], "signals": [keep, "no_such_signal", victim]}], a2l=edited
    )
    seg = segment(event)
    trz = tmp_path / "t.trz"
    with measuring(trz, c):
        assert wait_until(lambda: c.events[seg].rows > 20, timeout=6), c.last_error
        status = c.status()
    assert status["unknown"] == ["no_such_signal"]
    assert "array" in status["skipped"][victim]
    assert set(read(trz, f"ecu/{seg}")) == {"time_s", field_name(keep)}


# 11
def test_value_table_and_units_in_trace(target, tmp_path):
    verbs = [m for m in target.signals(max_size=4) if m["conversion"]["kind"] == "TAB_VERB"]
    units = [m for m in target.signals(max_size=4) if m["unit"]]
    if not verbs or not units:
        pytest.skip("no enum or no unit in this A2L")
    names = [verbs[0]["name"], units[0]["name"]]
    c = target.ecu([{"event": "default", "signals": names}])
    trz = tmp_path / "t.trz"
    with measuring(trz, c):
        time.sleep(1.5)
    tables, got_units = trace_catalog(trz, "tables"), trace_catalog(trz, "units")
    if tables is None:
        pytest.skip("duckdb CLI not on PATH")
    channels = {e["channel"]: segment(e) for e in target.catalog["events"]}
    verb, unit = verbs[0], units[0]
    verb_key = f"ecu/{channels[verb['events']['default'][0]]}.{field_name(verb['name'])}"
    unit_key = f"ecu/{channels[unit['events']['default'][0]]}.{field_name(unit['name'])}"
    table_rows = verb["conversion"]["table"]
    assert tables[verb_key] == {int(lo): text for lo, hi, text in table_rows if lo == hi}
    assert got_units[unit_key] == unit["unit"]
    assert len(read(trz, verb_key.split(".")[0])[field_name(verb["name"])]) > 5


# 12
def test_two_ecus_concurrently(target, tmp_path):
    event = target.events()[0]
    group = [{"event": event["name"], "signals": target.names(event["channel"], 3)}]
    second = target.another(tmp_path)
    try:
        a, b = target.ecu(group, name="ecu_a"), second.ecu(group, name="ecu_b")
        trz = tmp_path / "t.trz"
        with measuring(trz, a, b):
            time.sleep(2.0)
            status = [a.status(), b.status()]
    finally:
        second.stop()
    cycle = xa2l.cycle_ns(event) / 1e9
    for name, st, t in zip(("ecu_a", "ecu_b"), status, (target, second), strict=True):
        rows = read(trz, f"{name}/{segment(event)}")
        assert len(rows["time_s"]) > 1.5 / cycle, st["error"]
        assert st["events"][segment(event)]["missing_rows"] == 0
        check_values(t, rows, st, cycle)


def zelos_virtual_works() -> bool:
    """The zelos-can virtual bus lets one bus send while another thread blocks in its recv."""
    import can

    bus = can.Bus(interface="zelos-virtual", channel=f"probe-{os.getpid()}")
    reader = threading.Thread(target=bus.recv, args=(0.3,))
    reader.start()
    time.sleep(0.05)
    try:
        bus.send(can.Message(arbitration_id=0x1, data=b"\x00"))
        return True
    except RuntimeError:
        return False
    finally:
        reader.join()
        bus.shutdown()


@pytest.mark.parametrize("transport", ["can", "udp", "tcp"])
def test_demo_interface_owns_its_ecu(tmp_path, transport):
    pytest.importorskip("zelos_can.a2l")
    pytest.importorskip("zelos_extension_xcp.demo")
    if transport == "can" and not zelos_virtual_works():
        pytest.skip("the installed zelos-can virtual bus cannot send while another thread reads")
    from zelos_extension_xcp.cli.app import DEMO_ECU

    c = XcpConnection(
        "demo", "demo", measurements=DEMO_ECU["measurements"], link={"demo_transport": transport}
    )
    with measuring(tmp_path / "t.trz", c):
        assert wait_until(lambda: c.state == State.CONNECTED, timeout=6), c.last_error
        time.sleep(1.0)
        status = c.status()
    assert c._demo is None  # stopped with the ECU
    assert status["epk"]["result"] == "match"
    assert all(ev["rows"] > 0 for ev in status["events"].values())
    if transport == "can":
        assert 0 < status["bus_load"]["total_pct"] < 30


# ─── Protocol faults (demo ECU hooks) ───────────────────────────────────────

SYNCH, GET_STATUS, UPLOAD, SHORT_UPLOAD = 0xFC, 0xFD, 0xF5, 0xF4
EV_DAQ_OVERLOAD, EV_SESSION_TERMINATED, EV_WAKE_UP = 0x06, 0x07, 0x0B
SIGNATURE = 0x5A5A_1234  # diag.signature, a constant


def demo_only(target: Target, **kw) -> Any:
    """The demo ECU restarted with `kw`; skips other targets."""
    if target.kind != "demo":
        pytest.skip("fault injection is a demo ECU hook")
    if not kw:
        return target.handle
    target.handle.stop()
    demo = pytest.importorskip("zelos_extension_xcp.demo")
    if target.transport == "can":
        kw.update(interface="virtual", channel=target.channel)
    target.handle = demo.DemoEcu(transport=target.transport, host=HOST, port=0, **kw).start()
    return target.handle


def command_tap(target: Target) -> tuple[Any, dict]:
    """A recorder of the master's command codes and the `ecu()` arguments routing through it."""
    if target.transport == "can":
        from zelos_extension_xcp.can_link import ids

        return CanSniffer(target.channel, ids({}, target.catalog)[0]), {}
    if target.transport == "udp":
        tap = UdpRelay(target.handle.port)
        return tap, {"port": tap.port}
    return None, {}


def check_polls(rows: dict) -> None:
    """Each polled value is its own signal's: the constant, and a rising task counter."""
    tick = rows[field_name("sys.tick_10ms")]
    assert set(rows[field_name("diag.signature")]) == {SIGNATURE}
    assert all(d >= 0 for d in steps(tick)) and max(tick) < SIGNATURE


def synch_evidence(tap: Any, target: Target) -> None:
    """SYNCH went out after the timed out command and before its retry."""
    if tap is None:
        return
    pids = list(tap.pids)
    i = pids.index(SYNCH)
    print(f"{target.name} commands around SYNCH:", [f"0x{p:02X}" for p in pids[i - 2 : i + 3]])
    assert pids[i - 1] == pids[i + 1] != SYNCH


def test_late_response_not_taken_by_the_next_command(target, tmp_path):
    ecu = demo_only(target)
    tap, kw = command_tap(target)
    poll = {"event": "poll", "rate_ms": 20, "signals": ["sys.tick_10ms", "diag.signature"]}
    c = target.ecu([poll], timeout=0.5, retries=1, **kw)
    trz = tmp_path / "t.trz"
    try:
        with measuring(trz, c):
            assert wait_until(lambda: c.events["poll_20"].rows >= 5, timeout=6), c.last_error
            ecu.respond_late(0.75, command=SHORT_UPLOAD)
            assert wait_until(lambda: ecu.stats["commands"].get(SYNCH), timeout=6)
            rows = c.events["poll_20"].rows
            assert wait_until(lambda: c.events["poll_20"].rows >= rows + 20, timeout=6)
            assert wait_until(lambda: c.status().get("stale_responses"), timeout=3)
            status = c.status()
    finally:
        if tap is not None:
            tap.close()
    check_polls(read(trz, "ecu/poll_20"))
    assert status["state"] == "connected" and status["reconnects"] == 0
    synch_evidence(tap, target)


def test_late_liveness_reply_not_read_as_memory(target, tmp_path):
    ecu = demo_only(target)
    tap, kw = command_tap(target)
    event = target.events()[-1]  # 100 ms
    groups = [
        {"event": event["name"], "signals": ["diag.heartbeat"]},
        {"event": "poll", "rate_ms": 20, "signals": ["sys.tick_10ms", "diag.signature"]},
    ]
    c = target.ecu(groups, timeout=0.5, retries=1, **kw)
    trz = tmp_path / "t.trz"
    try:
        with measuring(trz, c):
            assert wait_until(lambda: c.events["poll_20"].rows >= 5, timeout=6), c.last_error
            ecu.respond_late(0.75, command=GET_STATUS)
            ecu.drop_dto(30)  # 3 s without DAQ: the liveness probe goes out
            assert wait_until(lambda: ecu.stats["commands"].get(SYNCH), timeout=6)
            rows = c.events["poll_20"].rows
            assert wait_until(lambda: c.events["poll_20"].rows >= rows + 20, timeout=6)
            assert wait_until(lambda: c.status().get("stale_responses"), timeout=3)
            status = c.status()
    finally:
        if tap is not None:
            tap.close()
    check_polls(read(trz, "ecu/poll_20"))
    assert status["state"] == "connected" and status["reconnects"] == 0
    synch_evidence(tap, target)


def test_loss_across_a_sample_boundary_drops_the_row(target, tmp_path):
    if target.transport == "can":
        pytest.skip("XCP on CAN has no packet counter to see the loss")
    ecu = demo_only(target, max_dto=16)  # ODT 0: the 8-byte time and the timestamp; ODT 1: tick
    event = target.events()[0]
    c = target.ecu([{"event": event["name"], "signals": ["sys.tick_10ms", "sys.uptime_ns"]}])
    seg = segment(event)
    trz = tmp_path / "t.trz"
    with measuring(trz, c):
        assert wait_until(lambda: c.events[seg].rows > 20, timeout=6), c.last_error
        assert c.check_selection()["events"][seg]["odts"] == 2
        ecu.drop_dto(2, from_odt=1)  # last ODT of one sample, first of the next
        assert wait_until(lambda: c.status()["lost_packets"] == 2, timeout=6)
        rows = c.events[seg].rows
        assert wait_until(lambda: c.events[seg].rows > rows + 20, timeout=6)
        status = c.status()
    rows = read(trz, f"ecu/{seg}")
    tick, uptime = rows[field_name("sys.tick_10ms")], rows[field_name("sys.uptime_ns")]
    assert [u // 10_000_000 for u in uptime] == tick  # one task run per row
    assert status["incomplete_rows"] == 1


@pytest.mark.parametrize("rule", [1, 3])
def test_address_extension_rule(target, tmp_path, rule):
    demo_only(target, address_extension=rule)
    event = target.events()[-1]  # 100 ms: extension 0 and 1
    names = ["cooling.coolant_temp", "diag.heartbeat", "diag.signature"]
    c = target.ecu([{"event": event["name"], "signals": names}])
    seg = segment(event)
    trz = tmp_path / "t.trz"
    with measuring(trz, c):
        if rule == 3:
            assert wait_until(lambda: c.state == State.ERROR, timeout=6)
        else:
            assert wait_until(lambda: c.events[seg].rows > 10, timeout=6), c.last_error
        status = c.status()
    rows = read(trz, f"ecu/{seg}")
    if rule == 3:
        assert "address extensions 0, 1" in status["error"] and rows == {}
        return
    assert set(rows[field_name("diag.signature")]) == {SIGNATURE}
    check_values(target, rows, status, xa2l.cycle_ns(event) / 1e9)


class SecondMaster:
    """Another tool on the demo ECU's link: CAN on the same ids, UDP or TCP from another port."""

    def __init__(self, target: Target) -> None:
        self.can = target.transport == "can"
        if self.can:
            import can

            from zelos_extension_xcp.can_link import ids

            self.bus = can.Bus(interface="virtual", channel=target.channel)
            self.tx, self.rx = ids({}, target.catalog)
        else:
            kind = socket.SOCK_DGRAM if target.transport == "udp" else socket.SOCK_STREAM
            self.s = socket.socket(socket.AF_INET, kind)
            self.s.connect((HOST, target.handle.port))
            self.s.settimeout(0.05)
        self.ctr, self.buf = 0, b""

    def cmd(self, *b: int) -> bytes | None:
        """First response within 0.5 s. On CAN it may be the other master's: no identity."""
        if self.can:
            import can

            self.bus.send(can.Message(arbitration_id=self.tx, data=bytes(b), is_extended_id=False))
        else:
            self.s.send(struct.pack("<HH", len(b), self.ctr) + bytes(b))
            self.ctr += 1
        end = time.monotonic() + 0.5
        while time.monotonic() < end:
            for p in self._packets():
                if p[0] >= 0xFE:
                    return p
        return None

    def _packets(self) -> list[bytes]:
        if self.can:
            m = self.bus.recv(0.05)
            return [bytes(m.data)] if m is not None and m.arbitration_id == self.rx else []
        try:
            self.buf += self.s.recv(65535)
        except (TimeoutError, OSError):
            return []
        out = []
        while len(self.buf) >= 4 and len(self.buf) >= 4 + (n := self.buf[0] | self.buf[1] << 8):
            out.append(self.buf[4 : 4 + n])
            self.buf = self.buf[4 + n :]
        return out

    def daq(self, lists: list[tuple[int, list[list[tuple[int, int, int]]]]]) -> bool:
        """Its own DAQ layout, lists of (event, ODTs of (ext, address, size)), started."""
        steps = [(0xFF, 0), (0xD6,), (0xD5, 0, len(lists), 0)]
        steps += [(0xD4, 0, d, 0, len(odts)) for d, (_, odts) in enumerate(lists)]
        for d, (event, odts) in enumerate(lists):
            steps += [(0xD3, 0, d, 0, o, len(odt)) for o, odt in enumerate(odts)]
            for o, odt in enumerate(odts):
                steps.append((0xE2, 0, d, 0, o, 0))
                steps += [(0xE1, 0xFF, n, ext, *struct.pack("<I", a)) for ext, a, n in odt]
            steps += [(0xE0, 0x10, d, 0, event, 0, 1, 0), (0xDE, 2, d, 0)]
        steps.append((0xDD, 1))
        return all(self.cmd(*s) is not None for s in steps)

    def close(self) -> None:
        self.cmd(0xFE)  # DISCONNECT
        if self.can:
            self.bus.shutdown()
        else:
            self.s.close()


def test_second_master_takes_the_session(target, tmp_path):
    """XCP has no master identity on CAN: after another tool's DAQ setup its packets
    arrive on our id. None may become a row; the change is found, the session restored.
    Its first DAQ list has our event, mode and ODT sizes (8, 5, 5 bytes), other content."""
    demo_only(target)
    names = ["sys.tick_10ms", "motor.speed", "inv.state", "inv.dc.voltage"]
    c = target.ecu([{"event": "10ms", "signals": names}], timeout=0.5, retries=0)
    trz = tmp_path / "t.trz"
    wanted = {*names, "motor.torque", "inv.dc.current"}
    at = {n: xa2l.signal(m) for n, m in info(target).items() if n in wanted}
    tick, speed = at["sys.tick_10ms"], at["motor.speed"]
    torque, current = at["motor.torque"], at["inv.dc.current"]
    odts = [[(0, tick.address, 2), (0, speed.address, 1)], [(0, torque.address, 4)]]
    theirs = [(0, [*odts, [(0, current.address, 4)]])]
    with measuring(trz, c):
        assert wait_until(lambda: c.events["10ms"].rows > 50, timeout=6), c.last_error
        if target.transport == "can":
            receiver = c._receiver
            ours = [receiver.length[receiver.pid_map[pid]] for pid in sorted(receiver.pid_map)]
            sizes = [
                1 + 4 * (o == 0) + sum(n for *_, n in odt)
                for _, odts in theirs
                for o, odt in enumerate(odts)
            ]
            assert sizes == ours, (ours, sizes)
        other = SecondMaster(target)
        t0 = time.monotonic()
        started = other.daq(theirs)
        try:
            if target.transport == "tcp":  # served one connection at a time: never answered
                assert not started
                time.sleep(2.0)
                assert c.state == State.CONNECTED and c.reconnects == 0
            else:
                assert started
                assert wait_until(lambda: c.state != State.CONNECTED, timeout=3)
                found = time.monotonic() - t0
                time.sleep(max(0.0, 2.0 - found))  # it leaves before our 3 s backoff ends
        finally:
            other.close()
        if target.transport != "tcp":
            assert wait_until(lambda: c.state == State.CONNECTED, timeout=10), c.last_error
            rows = c.events["10ms"].rows
            assert wait_until(lambda: c.events["10ms"].rows > rows + 50, timeout=6)
            print(f"{target.name}: another master found after {found:.2f}s")
            assert found < 2.0
    rows = read(trz, "ecu/10ms")
    for i, k in enumerate(rows[field_name("sys.tick_10ms")]):
        for name in names:  # every row is the ECU's at its own tick
            want = target.handle.physical(name, k / 100)
            assert rows[field_name(name)][i] == pytest.approx(want, rel=1e-6), (name, k)
    sessions = read(trz, "ecu/session")["epk_result"]
    assert sessions == ["match"] * (1 if target.transport == "tcp" else 2)


def test_ecu_events(target, tmp_path, caplog):
    ecu = demo_only(target)
    caplog.set_level(logging.INFO, logger="zelos_extension_xcp.client")
    event = target.events()[0]
    c = target.ecu([{"event": event["name"], "signals": target.names(event["channel"], 1)}])
    seg = segment(event)
    with measuring(tmp_path / "t.trz", c):
        assert wait_until(lambda: c.events[seg].rows > 20, timeout=6), c.last_error
        for code in (EV_DAQ_OVERLOAD, EV_DAQ_OVERLOAD, EV_WAKE_UP, EV_WAKE_UP):
            ecu.emit_event(code)
        assert wait_until(lambda: c.status()["daq_overloads"] == 2, timeout=3)
        assert wait_until(lambda: "ECU event 0x0B EV_WAKE_UP" in caplog.text, timeout=3)
        assert c.state == State.CONNECTED
        t0 = time.monotonic()
        ecu.emit_event(EV_SESSION_TERMINATED)
        assert wait_until(lambda: c.state != State.CONNECTED, timeout=6)
        to_loss = time.monotonic() - t0
        rows = c.events[seg].rows
        assert wait_until(lambda: c.events[seg].rows > rows + 20, timeout=20)
        assert c.reconnects == 1
    print(f"{target.name}: EV_SESSION_TERMINATED to reconnecting {to_loss:.2f}s")
    assert "the ECU ended the session" in caplog.text
    assert to_loss < 1.0  # the liveness probe needs 1 s of silence, then a 1 s timeout
    assert caplog.text.count("EV_WAKE_UP") == 1  # later ones at debug
    # Provenance: one row per session, the reconnect included.
    sessions = read(tmp_path / "t.trz", "ecu/session")
    digest = hashlib.sha256(target.a2l.read_bytes()).hexdigest()
    assert sessions["a2l_sha256"] == [digest] * 2 and sessions["epk_result"] == ["match"] * 2
    assert sessions["ecu_epk"] == sessions["a2l_epk"] == [target.catalog["epk"]["string"]] * 2
    assert sessions["protocol_version"] == [1] * 2


def test_padded_frames(target, tmp_path):
    if target.transport == "tcp":
        pytest.skip("fill is covered on UDP; TCP framing is the library's")
    pad = {"can_padding": 0xAA} if target.transport == "can" else {"eth_align": 4, "eth_pack": 4}
    demo_only(target, **pad)
    event = target.events()[0]
    names = target.names(event["channel"], 4)
    poll = {"event": "poll", "rate_ms": 50, "signals": ["diag.signature"]}
    c = target.ecu([{"event": event["name"], "signals": names}, poll], max_bus_load=100)
    seg = segment(event)
    trz = tmp_path / "t.trz"
    with measuring(trz, c):
        assert wait_until(lambda: c.events[seg].rows > 100, timeout=6), c.last_error
        assert c.read("diag.signature")["value"] == SIGNATURE
        status = c.status()
    assert status["epk"]["result"] == "match"
    assert status["malformed_datagrams"] == status["rejected_packets"] == 0
    assert status["incomplete_rows"] == 0 and status["lost_packets"] in (0, None)
    assert status["events"][seg]["missing_rows"] == 0
    check_values(target, read(trz, f"ecu/{seg}"), status, xa2l.cycle_ns(event) / 1e9)
    assert set(read(trz, "ecu/poll_50")[field_name("diag.signature")]) == {SIGNATURE}


def test_value_larger_than_a_daq_packet_skipped(target, tmp_path):
    demo_only(target)
    event = target.events()[0]
    names = ["sys.tick_10ms", "motor.speed", "sys.uptime_ns"]  # the last one 8 bytes
    c = target.ecu([{"event": event["name"], "signals": names}], max_bus_load=100)
    seg = segment(event)
    trz = tmp_path / "t.trz"
    with measuring(trz, c):
        assert wait_until(lambda: c.events[seg].rows > 20, timeout=6), c.last_error
        check = c.check_selection()
        status = c.status()
    rows = read(trz, f"ecu/{seg}")
    tick, uptime = rows[field_name("sys.tick_10ms")], rows.get(field_name("sys.uptime_ns"))
    assert status["state"] == "connected" and None not in rows[field_name("motor.speed")]
    if target.transport != "can":
        assert status["skipped"] == check["skipped"] == {}
        assert [u // 10_000_000 for u in uptime] == tick
        return
    reason = "8 bytes do not fit one DAQ packet of this ECU (7 data bytes); use CAN FD or poll it"
    assert status["skipped"] == check["skipped"] == {"sys.uptime_ns": reason}
    assert check["fits"] and None not in tick and uptime is None  # not a field of the event


def test_no_default_event_polled(target, tmp_path, caplog):
    if target.transport != "can":
        pytest.skip("CAN is the case asked for; the poll path is transport independent")
    ecu = demo_only(target)
    caplog.set_level(logging.WARNING, logger="zelos_extension_xcp.client")
    event = target.events()[0]
    daq_names = target.names(event["channel"], 2)
    c = target.ecu([{"event": "default", "signals": [*daq_names, "sys.build"]}])
    seg = segment(event)
    trz = tmp_path / "t.trz"
    with measuring(trz, c):
        assert wait_until(lambda: c.events.get("poll_100") and c.events["poll_100"].rows > 5), (
            c.last_error
        )
        assert wait_until(lambda: c.events[seg].rows > 20, timeout=6), c.last_error
        check = c.check_selection()
        status = c.status()
    note = {"sys.build": 100}
    assert status["polled_no_default_event"] == check["polled_no_default_event"] == note
    assert "sys.build" not in status["skipped"]
    polled = read(trz, "ecu/poll_100")
    assert set(polled) == {"time_s", field_name("sys.build")}
    assert set(polled[field_name("sys.build")]) == {ecu.physical("sys.build", 0.0)}
    daq_rows = read(trz, f"ecu/{seg}")
    assert field_name("sys.build") not in daq_rows
    check_values(target, daq_rows, status, xa2l.cycle_ns(event) / 1e9)
    warned = [r.message for r in caplog.records if "no default event" in r.message]
    assert warned == ["[ecu] polled at 100 ms: no default event in the A2L: sys.build"]


@pytest.mark.parametrize("mode", ["msb", "event"])
def test_daq_overload_drops_the_sample_in_progress(target, tmp_path, mode):
    if target.transport != "can":
        pytest.skip("CAN: no packet counter, the overload indication is the only sign")
    ecu = demo_only(target, overload=mode)
    event = target.events()[0]
    # ODT 0: the timestamp and the status word; ODT 1: the tick.
    c = target.ecu([{"event": event["name"], "signals": ["sys.tick_10ms", "inv.flag.toggle"]}])
    seg = segment(event)
    trz = tmp_path / "t.trz"
    with measuring(trz, c):
        assert wait_until(lambda: c.events[seg].rows > 20, timeout=6), c.last_error
        assert c.check_selection()["events"][seg]["odts"] == 2
        ecu.overload(2, from_odt=1)  # last ODT of one sample, first of the next
        assert wait_until(lambda: c.status()["daq_overloads"] == 1, timeout=6)
        rows = c.events[seg].rows
        assert wait_until(lambda: c.events[seg].rows > rows + 20, timeout=6)
        status = c.status()
    rows = read(trz, f"ecu/{seg}")
    tick, toggle = rows[field_name("sys.tick_10ms")], rows[field_name("inv.flag.toggle")]
    assert [t & 1 for t in tick] == toggle  # one task run per row
    assert status["state"] == "connected" and status["rejected_packets"] == 0
    assert status["incomplete_rows"] == 1


# ─── Reads, timing and protocol options on CAN (demo ECU) ───────────────────

CAN = pytest.mark.parametrize("target", ["can"], indirect=True)


@CAN
@pytest.mark.parametrize("block", [True, False])
def test_poll_batched_into_few_commands(target, tmp_path, block):
    from zelos_extension_xcp.constants import DEMO_ECU

    demo_only(target, block_mode=block)
    names = [*DEMO_ECU["measurements"][0]["signals"], "diag.signature"]
    groups = [
        {"event": "poll", "rate_ms": 50, "signals": names},
        {"event": "poll", "rate_ms": 20, "signals": ["sys.tick_10ms", "sys.uptime_ns"]},
    ]
    c = target.ecu(groups)
    trz = tmp_path / "t.trz"
    with measuring(trz, c):
        assert wait_until(lambda: c.events["poll_20"].rows >= 20, timeout=6), c.last_error
        status = c.status()
    ev = status["events"]["poll_50"]
    print(
        f"block mode {block}: {len(names)} signals, frames per cycle "
        f"{2 * len(names)} one by one, {ev['frames_per_cycle']} batched"
    )
    assert ev["frames_per_cycle"] < 2 * len(names)
    assert 0 < ev["poll_window_ms"] < 50 and ev["missed_cycles"] == 0
    rows = read(trz, "ecu/poll_50")
    assert set(rows[field_name("diag.signature")]) == {SIGNATURE}  # offsets within a run
    ticks = read(trz, "ecu/poll_20")
    tick, uptime = ticks[field_name("sys.tick_10ms")], ticks.get(field_name("sys.uptime_ns"))
    if not block:  # 8 bytes need two commands on classic CAN: could tear, not read
        assert "no block mode" in status["skipped"]["sys.uptime_ns"] and not any(uptime or [])
        return
    assert status["skipped"] == {}
    assert all(u % 10_000_000 == 0 for u in uptime)
    assert all(abs(u // 10_000_000 - k) <= 1 for u, k in zip(uptime, tick, strict=True))


@CAN
def test_poll_that_cannot_keep_its_rate_reported(target, tmp_path, caplog):
    ecu = demo_only(target)
    caplog.set_level(logging.WARNING, logger="zelos_extension_xcp.client")
    c = target.ecu([{"event": "poll", "rate_ms": 20, "signals": ["diag.signature"]}])
    with measuring(tmp_path / "t.trz", c):
        assert wait_until(lambda: c.events["poll_20"].rows >= 5, timeout=6), c.last_error
        ecu.respond_late(0.07, n=30, command=SHORT_UPLOAD)
        assert wait_until(lambda: "poll_20 cannot keep 20 ms" in caplog.text, timeout=6)
        ev = c.status()["events"]["poll_20"]
    assert ev["missed_cycles"] > 0 and ev["poll_window_ms"] > 50
    assert caplog.text.count("cannot keep") == 1


@CAN
def test_backoff_resets_after_a_healthy_session(target, tmp_path, monkeypatch):
    ecu = demo_only(target)
    monkeypatch.setattr(xclient, "HEALTHY", 1.0)
    event = target.events()[0]
    c = target.ecu([{"event": event["name"], "signals": target.names(event["channel"], 1)}])
    back = []
    with measuring(tmp_path / "t.trz", c):
        for n in range(2):  # each loss after a healthy session: the initial 3 s
            assert wait_until(lambda: c.state == State.CONNECTED, timeout=10), c.last_error
            time.sleep(1.5)
            t0 = time.monotonic()
            ecu.emit_event(EV_SESSION_TERMINATED)
            assert wait_until(lambda n=n: c.reconnects == n + 1, timeout=15)
            back.append(time.monotonic() - t0)
    assert all(b < 4.5 for b in back), back


@pytest.mark.parametrize("target", ["can-ts2"], indirect=True)
def test_short_timestamp_keeps_one_offset(target, tmp_path):
    # The 2-byte, 1 us timestamp wraps every 65.5 ms, under the 100 ms cycle.
    event = target.events()[-1]
    c = target.ecu([{"event": event["name"], "signals": target.names(event["channel"], 2)}])
    seg = segment(event)
    trz = tmp_path / "t.trz"
    with measuring(trz, c):
        assert wait_until(lambda: c.status()["timestamps"].get("drift_ppm") is not None, 10)
        status = c.status()
    ts = status["timestamps"]
    print(f"can-ts2 100 ms: anchors {ts['anchors']}, offset {ts['offset_s']:.6f} s, "
          f"drift {ts['drift_ppm']:.1f} ppm")  # fmt: skip
    rows = read(trz, f"ecu/{seg}")
    # One anchor, and one more per reported receive gap (a busy host), never per row.
    assert ts["source"] == "ecu" and ts["anchors"] == 1 + ts["gaps"]
    assert len(rows["time_s"]) > 10 * ts["anchors"]
    cycle = xa2l.cycle_ns(event) / 1e9
    cycles = [d / cycle for d in steps(rows["time_s"])]
    assert all(round(n) >= 1 for n in cycles)
    # One offset per anchor: only a step across a new anchor may leave the cycle grid.
    off = [n for n in cycles if abs(n - round(n)) * cycle >= 1e-3]
    assert len(off) <= ts["gaps"], off
    check_values(target, rows, status, cycle)


@CAN
@pytest.mark.parametrize("option", ["max_dlc", "daq_list_id"])
def test_a2l_can_options(target, tmp_path, option):
    render = pytest.importorskip("zelos_extension_xcp.demo.a2l").render
    text = render(max_dlc_required=option == "max_dlc")
    if option == "daq_list_id":
        text = text.replace(
            "BAUDRATE", "/begin DAQ_LIST_CAN_ID 0x0 FIXED 0x7F5 /end DAQ_LIST_CAN_ID\nBAUDRATE"
        )
    a2l = tmp_path / "options.a2l"
    a2l.write_text(text)
    demo_only(target, can_max_dlc_required=option == "max_dlc", can_padding=0xAA)
    event = target.events()[0]
    groups = [
        {"event": event["name"], "signals": target.names(event["channel"], 2)},
        {"event": "poll", "rate_ms": 50, "signals": ["diag.signature"]},
    ]
    tap = CanSniffer(target.channel, 0x7F0)
    c = target.ecu(groups, a2l=a2l)
    try:
        with measuring(tmp_path / "t.trz", c):
            if option == "daq_list_id":
                assert wait_until(lambda: c.state == State.ERROR, timeout=6)
            else:
                assert wait_until(lambda: c.events["poll_50"].rows >= 5, timeout=6), c.last_error
                assert wait_until(lambda: c.events[segment(event)].rows >= 20, timeout=6)
    finally:
        tap.close()
    if option == "daq_list_id":
        assert "DAQ list 0 on CAN id 0x7F5" in c.last_error
    else:
        assert tap.dlcs == {8}


@CAN
def test_status_strings_traced_as_null(target, tmp_path):
    epk = target.catalog["epk"]["string"]
    text = target.a2l.read_text().replace(f'EPK "{epk}"', "")  # ADDR_EPK alone: no EPK
    text = text.replace(
        "COEFFS_LINEAR 0.25 0 /end COMPU_METHOD",
        "COEFFS_LINEAR 0.25 0 STATUS_STRING_REF all /end COMPU_METHOD\n"
        '/begin COMPU_VTAB_RANGE all "" 1 -1e9 1e9 "SNA" /end COMPU_VTAB_RANGE',
    )
    a2l = tmp_path / "status.a2l"
    a2l.write_text(text)
    speed = [m["name"] for m in target.catalog["measurements"]
             if m["conversion"].get("coeffs") == [0.25, 0.0]][0]  # fmt: skip
    event = target.events()[0]
    groups = [
        {"event": event["name"], "signals": [speed, "sys.tick_10ms"]},
        {"event": "poll", "rate_ms": 50, "signals": [speed, "diag.signature"]},
    ]
    c = target.ecu(groups, a2l=a2l)
    trz = tmp_path / "t.trz"
    with measuring(trz, c):
        assert wait_until(lambda: c.events["poll_50"].rows >= 5, timeout=6), c.last_error
        assert c.read(speed)["status"] == "SNA"
    status = c.status()
    daq_rows, poll_rows = read(trz, f"ecu/{segment(event)}"), read(trz, "ecu/poll_50")
    assert (
        set(daq_rows[field_name(speed)]) == {None}
        and None not in daq_rows[field_name("sys.tick_10ms")]
    )
    assert set(poll_rows[field_name(speed)]) == {None} and status["epk"] == {"result": "absent"}
    assert set(poll_rows[field_name("diag.signature")]) == {SIGNATURE}
    assert status["status_values"][speed] >= len(daq_rows["time_s"]) + len(poll_rows["time_s"])


@CAN
def test_temporary_errors_are_retried(target, tmp_path):
    ecu = demo_only(target)
    GET_DAQ_PROCESSOR_INFO = 0xDA
    ecu.respond_late(0.8, command=GET_DAQ_PROCESSOR_INFO)  # one timeout at setup
    ecu.fail(3, command=SHORT_UPLOAD)  # ERR_CMD_BUSY on the first polls
    ecu.fail(1, command=UPLOAD, code=0x24)  # the EPK read refused once: read again
    event = target.events()[0]
    groups = [
        {"event": event["name"], "signals": target.names(event["channel"], 1)},
        {"event": "poll", "rate_ms": 20, "signals": ["sys.tick_10ms", "diag.signature"]},
    ]
    c = target.ecu(groups, timeout=0.5, retries=1)
    trz = tmp_path / "t.trz"
    with measuring(trz, c):
        assert wait_until(lambda: c.events[segment(event)].rows > 20, timeout=6), c.last_error
        assert wait_until(lambda: c.events["poll_20"].rows > 20, timeout=6)
        status = c.status()
    assert status["state"] == "connected" and status["reconnects"] == 0 and status["errors"] == 0
    assert status["epk"]["result"] == "match"
    check_polls(read(trz, "ecu/poll_20"))


@CAN
def test_cmd_pending_extends_the_wait(target, tmp_path):
    ecu = demo_only(target)
    c = target.ecu([{"event": "poll", "rate_ms": 50, "signals": ["diag.signature"]}], timeout=0.3)
    with measuring(tmp_path / "t.trz", c):
        assert wait_until(lambda: c.events["poll_50"].rows >= 3, timeout=6), c.last_error
        ecu.respond_late(1.0, command=SHORT_UPLOAD, pending_every=0.2)
        rows = c.events["poll_50"].rows
        assert wait_until(lambda: c.events["poll_50"].rows > rows + 5, timeout=6)
        status = c.status()
    assert SYNCH not in ecu.stats["commands"]  # waited, never timed out
    assert status["stale_responses"] == 0 and status["reconnects"] == 0


@CAN
@pytest.mark.parametrize("resource", ["daq", "calpag"])
def test_locked_ecu_reported_and_backed_off(target, tmp_path, caplog, resource):
    demo_only(target, protected=(resource,))
    caplog.set_level(logging.WARNING, logger="zelos_extension_xcp.client")
    event = target.events()[0]
    groups = [
        {"event": event["name"], "signals": target.names(event["channel"], 1)},
        {"event": "poll", "rate_ms": 50, "signals": ["diag.signature"]},
    ]
    c = target.ecu(groups)
    with measuring(tmp_path / "t.trz", c):
        assert wait_until(lambda: "reconnecting in 60s" in caplog.text, timeout=6), c.last_error
        status = c.status()
    locked = {"daq": ["DAQ"], "calpag": ["CAL/PAG"]}[resource]
    assert status["locked"] == locked and status["state"] in ("error", "connecting")
    assert status["error"] == f"locked: seed and key is not supported ({locked[0]})"


@CAN
def test_debug_frames_log_commands_not_daq_data(target, tmp_path, caplog):
    demo_only(target)
    caplog.set_level(logging.DEBUG, logger="zelos_extension_xcp.client")
    event = target.events()[0]
    c = target.ecu(
        [{"event": event["name"], "signals": target.names(event["channel"], 1)}], debug_frames=True
    )
    with measuring(tmp_path / "t.trz", c):
        assert wait_until(lambda: "DAQ packets in 1s" in caplog.text, timeout=6), c.last_error
    lines = [r.getMessage() for r in caplog.records if " PID 0x" in r.getMessage()]
    assert any("tx PID 0xFF" in m for m in lines) and any("rx PID 0xFF" in m for m in lines)
    assert all(int(m.split("PID 0x")[1][:2], 16) >= 0xC0 for m in lines)  # no DAQ PID
