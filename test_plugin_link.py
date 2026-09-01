"""Tests for the plugin link (plugin_link.py), against a local fake of the
Dalamud plugin's WebSocket server.

Covers the wire contract from the NyaaTriggers-Overlay repo's
docs/DEVELOPING.md: connect and validate the hello (protocol gate), exact
alert/timeline/tick/clear frames, non-finite floats never reaching the JSON,
liveness ping on idle, reconnect after the server drops the connection,
outbox eviction that keeps the fire-once alerts, and that the handshake
carries no Origin header (the plugin refuses any that does).

Run:  python test_plugin_link.py   (exit 0 = all pass)

No game, Qt, or display needed: plugin_link imports without Qt, and the fake
plugin is the `websockets` sync server on an ephemeral loopback port, never
the real plugin's 27080.
"""
import json
import os
import queue
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import plugin_link as pl
from websockets.sync.server import serve

FAILS = []


def check(name, cond):
    print(("PASS  " if cond else "FAIL  ") + name)
    if not cond:
        FAILS.append(name)


def wait_for(pred, timeout=6.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return False


class FakePlugin:
    """Tiny WS server standing in for the Dalamud plugin. Sends the hello on
    connect, then records every handshake's headers and every received frame."""

    def __init__(self, protocol=1):
        self.protocol = protocol
        self.frames = []          # decoded JSON frames, in arrival order
        self.handshakes = []      # request headers per handshake
        self.connections = 0
        self._conns = set()
        self._lock = threading.Lock()
        self._server = serve(self._serve_conn, "127.0.0.1", 0,
                             process_request=self._process_request, logger=None)
        self.port = self._server.socket.getsockname()[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def _process_request(self, conn, request):
        with self._lock:
            self.handshakes.append({k.lower(): v for k, v in request.headers.items()})
        return None

    def _serve_conn(self, conn):
        with self._lock:
            self.connections += 1
            self._conns.add(conn)
        conn.send(json.dumps({"ev": "hello", "protocol": self.protocol,
                              "plugin": "0.1.0"}))
        try:
            for raw in conn:
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                with self._lock:
                    self.frames.append(msg)
        except Exception:
            pass
        finally:
            with self._lock:
                self._conns.discard(conn)

    def snapshot(self):
        with self._lock:
            return list(self.frames)

    def drop(self):
        """Close every live connection server-side (plugin reload / eviction)."""
        with self._lock:
            conns = list(self._conns)
        for conn in conns:
            try:
                conn.close()
            except Exception:
                pass

    def shutdown(self):
        self.drop()
        self._server.shutdown()
        self._thread.join(timeout=3)


def make_link(fake, **kwargs):
    """A link on the fake plugin's port, not yet started. Callers connect
    status_changed first, so the worker's first report can't race them."""
    kwargs.setdefault("idle_ping_s", 0.6)
    return pl.PluginLink(port=fake.port, **kwargs)


# ── frame builders (pure) ────────────────────────────────────────────────
check("tick frame exact",
      pl.tick_frame(12.5) == {"c": "tick", "t": 12.5})
check("tick frame int seconds become float",
      pl.tick_frame(3) == {"c": "tick", "t": 3.0})
check("timeline frame exact, tagged with kinds",
      pl.timeline_frame([(18.0, "Wing"), (24.5, "Dive")])
      == {"c": "timeline", "v": [[18.0, "Wing", "mechanic"], [24.5, "Dive", "mechanic"]]})
check("timeline kind tags the words authors write",
      pl.timeline_kind("Tankbuster on MT") == "tankbuster"
      and pl.timeline_kind("Akh Morn raidwide") == "raidwide"
      and pl.timeline_kind("Raid-wide bleed") == "raidwide"
      and pl.timeline_kind("Wing of Ruin") == "mechanic")
check("alert frame exact",
      pl.alert_frame("Stack", "alarm") == {"c": "alert", "text": "Stack", "sev": "alarm"})
check("alert severities pass through 1:1",
      [pl.alert_frame("x", s)["sev"] for s in ("info", "alert", "alarm")]
      == ["info", "alert", "alarm"])
check("alert default + unknown severity degrades to info",
      pl.alert_frame("x")["sev"] == "info" and pl.alert_frame("x", "weird")["sev"] == "info")
check("clear frame exact", pl.clear_frame() == {"c": "clear"})
check("ping frame exact", pl.ping_frame() == {"c": "ping"})

# ── pure helpers: port parsing and the dps capability check ──────────────
check("port parse accepts the plugin's clamp range",
      pl.parse_port(27080) == 27080 and pl.parse_port("27081") == 27081
      and pl.parse_port(1024) == 1024 and pl.parse_port(65535) == 65535)
check("port parse rejects junk, bools and out of range",
      pl.parse_port("abc") is None and pl.parse_port(True) is None
      and pl.parse_port(None) is None and pl.parse_port(80) is None
      and pl.parse_port(70000) is None)
check("dps capable versions, three or four parts",
      pl.plugin_supports_dps("0.2.0") and pl.plugin_supports_dps("0.2.0.8")
      and pl.plugin_supports_dps("1.0.0"))
check("pre meter plugin versions read as too old",
      not pl.plugin_supports_dps("0.1.0") and not pl.plugin_supports_dps("0.0.9"))
check("junk versions never raise the warning",
      pl.plugin_supports_dps("") and pl.plugin_supports_dps("dev"))

# ── connect: hello, protocol check, no Origin header ─────────────────────
# Note: status_changed emissions from the worker thread are queued to an
# event loop the test doesn't run, so status is polled via last_status().
fake = FakePlugin()
link = make_link(fake)
link.start()
check("connects and validates the hello",
      wait_for(link.is_connected) and fake.connections >= 1)
check("status reports the plugin version",
      wait_for(lambda: link.last_status()[0] and "0.1.0" in link.last_status()[1]))
check("no Origin header in the handshake",
      fake.handshakes and all("origin" not in h for h in fake.handshakes))
check("handshake is a plain upgrade",
      all(h.get("upgrade") == "websocket" and h.get("sec-websocket-version") == "13"
          for h in fake.handshakes))

# ── exact frames over the wire ────────────────────────────────────────────
link.send_alert("Stack", "alarm")
check("alert frame arrives verbatim",
      wait_for(lambda: {"c": "alert", "text": "Stack", "sev": "alarm"} in fake.snapshot()))

link.send_timeline([(18.0, "Wing"), (24.5, "Dive")])
check("timeline frame arrives verbatim",
      wait_for(lambda: {"c": "timeline",
                        "v": [[18.0, "Wing", "mechanic"], [24.5, "Dive", "mechanic"]]}
               in fake.snapshot()))

link.send_tick(12.5)
check("tick frame arrives verbatim",
      wait_for(lambda: {"c": "tick", "t": 12.5} in fake.snapshot()))

link.send_clear()
check("clear frame arrives verbatim",
      wait_for(lambda: {"c": "clear"} in fake.snapshot()))

check("liveness ping on idle",
      wait_for(lambda: {"c": "ping"} in fake.snapshot(), timeout=5.0))

# ── reconnect after the server drops the connection ──────────────────────
before = len(fake.snapshot())
fake.drop()
check("reconnects after a server-side drop",
      wait_for(lambda: fake.connections >= 2 and link.is_connected(), timeout=8.0))
link.send_alert("Back", "info")
check("frames flow again after reconnect",
      wait_for(lambda: {"c": "alert", "text": "Back", "sev": "info"} in fake.snapshot()))
link.stop()
check("stop() joins the worker", wait_for(lambda: not link.is_connected(), timeout=3.0))
fake.shutdown()

# ── an alert queued during reconnect backoff survives to delivery ────────
# Regression for the drop window: the loop-top queue sweep used to discard
# alerts too, so a callout fired while the plugin was unreachable vanished
# before the next connect attempt was even made.
fake = FakePlugin()
link = make_link(fake)
link.start()
check("backoff window: connected before the drop",
      wait_for(link.is_connected) and fake.connections >= 1)
real_connect = link._connect
attempts = []
gate = threading.Event()


def gated_connect():
    attempts.append(1)
    if not gate.is_set():
        raise OSError("fake plugin still down")
    return real_connect()


link._connect = gated_connect
fake.drop()
check("backoff window: the failed attempt backs off",
      wait_for(lambda: len(attempts) >= 1 and not link.is_connected()))
gate.set()
link.send_alert("MidBackoff", "alert")
check("alert queued during backoff arrives after the reconnect",
      wait_for(lambda: {"c": "alert", "text": "MidBackoff", "sev": "alert"}
               in fake.snapshot(), timeout=8.0))
link.stop()
fake.shutdown()

# ── an alert whose send fails is re-queued and survives the reconnect ────
# Regression for the mid-write failure: the worker pops a frame, sends, and
# on an error tears the connection down. The popped frame used to be gone at
# that point, so a callout that hit a dying socket never reached the game.
fake = FakePlugin()
link = make_link(fake)
link.start()
check("send failure: connected before the fault",
      wait_for(link.is_connected) and fake.connections >= 1)
real_send = link._send
failed_once = []


def flaky_send(ws, msg):
    if isinstance(msg, dict) and msg.get("c") == "alert" \
            and msg.get("text") == "SendFail" and not failed_once:
        failed_once.append(1)
        raise OSError("fake plugin vanished mid write")
    real_send(ws, msg)


link._send = flaky_send
link.send_alert("SendFail", "alert")
check("alert whose send failed arrives after the reconnect",
      wait_for(lambda: {"c": "alert", "text": "SendFail", "sev": "alert"}
               in fake.snapshot(), timeout=8.0))
check("the send fault fired exactly once", len(failed_once) == 1)
link.stop()
fake.shutdown()

# ── set_port re-dials a live link ────────────────────────────────────────
fake_a = FakePlugin()
fake_b = FakePlugin()
link = make_link(fake_a)
link.start()
check("set_port: connected on the first port", wait_for(link.is_connected))
link.set_port(fake_b.port)
check("set_port: re-dials to the new port",
      wait_for(lambda: fake_b.connections >= 1 and link.is_connected(), timeout=6.0))
link.send_alert("Moved", "info")
check("set_port: frames flow to the new plugin",
      wait_for(lambda: {"c": "alert", "text": "Moved", "sev": "info"} in fake_b.snapshot()))
link.stop()
fake_a.shutdown()
fake_b.shutdown()

# ── protocol mismatch: gate, do not drive ────────────────────────────────
fake = FakePlugin(protocol=2)
link = make_link(fake)
link.start()
link.send_alert("never", "info")
time.sleep(2.5)
check("protocol mismatch never connects",
      not link.is_connected() and fake.connections >= 1)
check("protocol mismatch sends no frames", fake.snapshot() == [])
check("protocol mismatch reports why",
      "protocol" in link.last_status()[1].lower() and not link.last_status()[0])
link.stop()
fake.shutdown()

# ── disabled gate: no connect, no frames, live re-enable ─────────────────
fake = FakePlugin()
link = make_link(fake, enabled=False)
link.start()
link.send_alert("never", "info")
time.sleep(1.0)
check("disabled: no connection attempted", fake.connections == 0)
check("disabled: no frames queued", fake.snapshot() == [])
link.set_enabled(True)
check("re-enabling connects promptly",
      wait_for(lambda: link.is_connected() and fake.connections >= 1, timeout=4.0))
link.stop()
fake.shutdown()

# ── stale worker exit keeps the live generation's status ─────────────────
fake = FakePlugin()
link = make_link(fake)
link.start()
check("stale generation: connected before the churn",
      wait_for(link.is_connected) and fake.connections >= 1)
old_stopping = link._stopping
old_thread = link._thread
# Simulate a stop whose join timed out. Setting the events directly, without
# the _STOP sentinel stop would queue, keeps the old worker parked in its
# queue get until the 1 s timeout, so the start below always runs first.
old_stopping.set()
link._wake.set()
link.start()
check("stale generation: the replacement connects",
      wait_for(lambda: fake.connections >= 2 and link.is_connected(), timeout=8.0))
old_thread.join(timeout=3.0)
check("stale worker exit leaves the live status alone",
      not old_thread.is_alive()
      and link.is_connected() and link.last_status()[0])
link.stop()
fake.shutdown()

# ── a flooding peer cannot wedge the inbound drain ───────────────────────
# Regression for the unbounded drain: _drain_inbound used to recv until one
# call timed out, so a peer past the hello gate that never lets the socket
# go idle parked the worker in the drain. The outbox starved and stop joins
# timed out. The sweep is now a bounded batch that honors stopping.
fake = FakePlugin()
link = make_link(fake)
link.start()
check("flood: connected before the flood",
      wait_for(link.is_connected) and fake.connections >= 1)
with fake._lock:
    flood_conn = next(iter(fake._conns))
stop_flood = threading.Event()


def flood():
    # No pacing on purpose, the link's inbound must never run dry. This is
    # what keeps an unbounded drain from ever seeing a recv time out.
    pong = json.dumps({"ev": "pong"})
    while not stop_flood.is_set():
        try:
            flood_conn.send(pong)
        except Exception:
            return


flood_thread = threading.Thread(target=flood, daemon=True)
flood_thread.start()
time.sleep(0.2)   # let the flood keep the link's inbound busy
link.send_alert("Flood", "alert")
check("flood: outbound frames still flow",
      wait_for(lambda: {"c": "alert", "text": "Flood", "sev": "alert"}
               in fake.snapshot(), timeout=8.0))
link.stop()
check("flood: stop joins the worker, no drain wedge",
      link._thread is None and not link.is_connected())
stop_flood.set()
flood_thread.join(timeout=2)
fake.shutdown()

# ── the drain sweep itself is bounded and honors stopping ────────────────
# The deterministic half of the flood regression. Over a real socket the
# consumer can outrun the flood and catch a lucky recv timeout, so the wedge
# is proven here with a recv that always has a frame. The old while True
# drain never returned from this.
class EndlessFloodWS:
    """recv stand-in whose frames never run out. Optionally trips a stopping
    event partway through, a stop request landing mid drain."""

    def __init__(self, stopping=None, stop_after=0):
        self.stopping = stopping
        self.stop_after = stop_after
        self.recvd = 0

    def recv(self, timeout=None):
        self.recvd += 1
        if self.stopping is not None and self.recvd >= self.stop_after:
            self.stopping.set()
        return '{"ev":"pong"}'


fake_ws = EndlessFloodWS()
t0 = time.monotonic()
pl.PluginLink._drain_inbound(fake_ws, threading.Event())
check("drain sweep stops at the batch budget",
      fake_ws.recvd == pl.INBOUND_DRAIN_BATCH and time.monotonic() - t0 < 1.0)

stopping = threading.Event()
fake_ws = EndlessFloodWS(stopping, stop_after=5)
pl.PluginLink._drain_inbound(fake_ws, stopping)
check("drain sweep breaks out when stopping sets mid sweep",
      0 < fake_ws.recvd < pl.INBOUND_DRAIN_BATCH)

fake_ws = EndlessFloodWS()
pl.PluginLink._drain_inbound(fake_ws, stopping)
check("drain sweep with stopping already set reads nothing",
      fake_ws.recvd == 0)

# ── non-finite floats never reach the wire ───────────────────────────────
# Regression for the strict parser rejection: json.dumps writes inf and nan
# as bare Infinity and NaN tokens, and the plugin rejects the whole frame.
# tick drops the frame like any junk seconds, dps falls back or drops the
# offending row, and nothing non-finite may survive into the JSON.
INF, NAN = float("inf"), float("nan")
for bad in (INF, -INF, NAN):
    check(f"tick frame drops non-finite seconds ({bad})",
          pl.tick_frame(bad) is None)
check("dps frame falls back to 0.0 on non-finite enc dps",
      [pl.dps_frame({"dps": bad}, [])["enc"]["dps"] for bad in (INF, -INF, NAN)]
      == [0.0, 0.0, 0.0])
check("dps frame drops rows holding a non-finite value",
      pl.dps_frame({"dps": 1.0},
                   [["Me", "BLM", NAN, 50.0, 1.0, True],
                    ["Me", "BLM", 100.0, INF, 1.0, True],
                    ["Me", "BLM", 100.0, 50.0, -INF, True],
                    ["You", "WHM", 90.0, 45.0, 2.0, False]])["rows"]
      == [["You", "WHM", 90.0, 45.0, 2.0, False, 0]])
check("dps frame carries the deaths field and defaults it",
      pl.dps_frame({"dps": 1.0},
                   [["Me", "BLM", 100.0, 50.0, 1.0, True, 2],
                    ["You", "WHM", 90.0, 45.0, 2.0, False]])["rows"]
      == [["Me", "BLM", 100.0, 50.0, 1.0, True, 2],
          ["You", "WHM", 90.0, 45.0, 2.0, False, 0]])
check("dps frame drops rows with junk deaths",
      pl.dps_frame({"dps": 1.0},
                   [["Me", "BLM", 100.0, 50.0, 1.0, True, -1],
                    ["You", "WHM", 90.0, 45.0, 2.0, False]])["rows"]
      == [["You", "WHM", 90.0, 45.0, 2.0, False, 0]])
check("dps frame drops rows with non-finite deaths",
      pl.dps_frame({"dps": 1.0},
                   [["Me", "BLM", 100.0, 50.0, 1.0, True, INF],
                    ["Me", "BLM", 100.0, 50.0, 1.0, True, NAN],
                    ["You", "WHM", 90.0, 45.0, 2.0, False]])["rows"]
      == [["You", "WHM", 90.0, 45.0, 2.0, False, 0]])
check("dps JSON carries no bare Infinity or NaN token",
      all(token not in json.dumps(pl.dps_frame({"t": "F", "d": "1:23", "dps": bad},
                                               [["Me", "BLM", bad, 50.0, 1.0, True]]))
          for bad in (INF, -INF, NAN) for token in ("Infinity", "NaN")))

# ── outbox eviction keeps alerts and logs the loss ───────────────────────
# Regression for the blind drop-oldest: the overlay plugin dying mid-fight
# fills the outbox during reconnect backoff, 4 Hz ticks plus preserved
# alerts. Eviction must keep the fire-once alerts, drop the oldest
# non-alert frame, and leave a drop-log line as evidence.
link = pl.PluginLink()
alerts = [pl.alert_frame(f"keep{i}") for i in range(4)]
ticks = [pl.tick_frame(float(i)) for i in range(pl.OUTBOX_CAPACITY - len(alerts))]
for frame in ticks[:2] + alerts + ticks[2:]:
    link._enqueue(frame)
check("outbox fills to capacity", link._queue.qsize() == pl.OUTBOX_CAPACITY)

drops = []
real_log_drop = pl.log_drop
pl.log_drop = lambda site, detail, *a, **k: drops.append((site, detail))
try:
    link._enqueue(pl.tick_frame(999.0))
finally:
    pl.log_drop = real_log_drop

survivors = []
while True:
    try:
        survivors.append(link._queue.get_nowait())
    except queue.Empty:
        break
check("eviction keeps every queued alert",
      all(a in survivors for a in alerts))
check("eviction drops the oldest non-alert frame only",
      ticks[0] not in survivors and all(t in survivors for t in ticks[1:]))
check("the new frame still lands",
      {"c": "tick", "t": 999.0} in survivors)
check("the outbox stays at capacity", len(survivors) == pl.OUTBOX_CAPACITY)
check("eviction leaves a drop-log line",
      any(site == "plugin-drop" for site, _detail in drops))

# An outbox of nothing but alerts has nothing evictable. The bound holds
# and the new frame is the casualty, also logged.
link = pl.PluginLink()
for i in range(pl.OUTBOX_CAPACITY):
    link._enqueue(pl.alert_frame(f"x{i}"))
drops = []
pl.log_drop = lambda site, detail, *a, **k: drops.append((site, detail))
try:
    link._enqueue(pl.tick_frame(1.0))
finally:
    pl.log_drop = real_log_drop

survivors = []
while True:
    try:
        survivors.append(link._queue.get_nowait())
    except queue.Empty:
        break
check("an all-alert outbox eats the new frame, bound intact",
      len(survivors) == pl.OUTBOX_CAPACITY
      and {"c": "tick", "t": 1.0} not in survivors
      and pl.alert_frame("x0") in survivors
      and pl.alert_frame(f"x{pl.OUTBOX_CAPACITY - 1}") in survivors)
check("the eaten frame is logged too",
      any(site == "plugin-drop" for site, _detail in drops))

print()
if FAILS:
    print(f"{len(FAILS)} FAILED: {', '.join(FAILS)}")
    sys.exit(1)
print("all passed")


def test_module_suite():
    """pytest entry: the checks above run at import; report them as one test."""
    assert not FAILS
