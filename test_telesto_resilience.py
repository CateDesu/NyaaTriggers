"""Tests for the Telesto marks-resilience fixes (MainWindow), driven unbound on
a duck-typed window like test_automark_rules.py.

1. _refresh_telesto_party self-heals: while automarkers is on it re-asserts the
   client enabled flag and forces the reachability probe, so a desync or a Telesto
   hiccup recovers on the next 10s tick without a relaunch.
2. _on_telesto_status no longer clobbers the native client's status (the green
   light stops flapping to off. The native client is authoritative).
3. The real TelestoClient runs against a local fake Telesto HTTP server, the
   checks above stub it. Covers the roster from GetPartyMembers, mark_actor
   resolving to /mk on the wire, fail-closed on an unknown actor, and the
   reachable/degraded/unreachable transitions.
4. A truthy non-string telesto_uri from raw settings falls back to the default
   instead of killing the transport, at init and via configure.
5. Turning automarkers off runs the engine reset-with-clear forced, before the
   client goes dark, so engine-placed signs come down instead of stranding.

Run:  python test_telesto_resilience.py   (exit 0 = all pass)
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import main_window as mw

FAILS = []


def check(name, cond):
    print(("PASS  " if cond else "FAIL  ") + name)
    if not cond:
        FAILS.append(name)


class FakeClient:
    def __init__(self):
        self.calls = []

    def set_enabled(self, v):
        self.calls.append(("set_enabled", bool(v)))

    def request_party_members(self, force=False):
        self.calls.append(("refresh", bool(force)))


class FakeWin:
    _refresh_telesto_party = mw.MainWindow._refresh_telesto_party
    _on_telesto_status = mw.MainWindow._on_telesto_status
    _on_telesto_client_status = mw.MainWindow._on_telesto_client_status

    def __init__(self, enabled=True):
        self._telesto_client = FakeClient()
        self._settings = {"telesto_enabled": enabled}
        self._umad_chain_enabled = False
        self._umad_gaze_enabled = False
        self._telesto_status = "unknown"

    def _update_automark_status_label(self):
        pass


# ── self-heal: the 10s refresh re-asserts enabled + forces the probe ──
w = FakeWin(enabled=True)
w._refresh_telesto_party()
check("refresh re-asserts set_enabled(True)",
      ("set_enabled", True) in w._telesto_client.calls)
check("refresh forces the reachability probe (fires through a desync/hiccup)",
      ("refresh", True) in w._telesto_client.calls)
check("enabled is re-asserted before the probe",
      w._telesto_client.calls.index(("set_enabled", True))
      < w._telesto_client.calls.index(("refresh", True)))

# ── gated: nothing happens when automarkers is off ──
w = FakeWin(enabled=False)
w._refresh_telesto_party()
check("refresh is a no-op when automarkers is off", w._telesto_client.calls == [])

# ── the native client is the sole source of truth for the light ──
w = FakeWin(enabled=True)
w._on_telesto_client_status(True, "Connected")
check("native client turns the light green", w._telesto_status == "good")
w._on_telesto_status("bad")            # the vestigial engine signal tries to clobber
check("engine 'bad' no longer clobbers the native green", w._telesto_status == "good")
w._on_telesto_status("unknown")
check("engine 'unknown' is ignored too", w._telesto_status == "good")
w._on_telesto_client_status(False, "unreachable")
check("native client can still turn it red (real reachability)",
      w._telesto_status == "bad")

# ── the real client against a local fake Telesto HTTP server ──
# Status is polled via _reachable. status_changed emissions queue to an event
# loop the test doesn't run, same constraint as test_plugin_link.py.
import http.server
import json
import threading
import time

from telesto_client import TelestoClient, mark_command, DEFAULT_URI


class FakeTelesto:
    """Records /mk commands and answers GetPartyMembers with a fixed roster."""

    def __init__(self):
        self.commands = []
        self.party = [{"order": "0", "actor": "10FF0001"},
                      {"order": "1", "actor": "10FF0002"}]
        self.ok = True   # False answers every POST with HTTP 500

    def start(self):
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                if not outer.ok:
                    self.send_response(500)
                    self.end_headers()
                    return
                try:
                    msg = json.loads(body)
                except ValueError:
                    msg = {}
                if msg.get("type") == "ExecuteCommand":
                    outer.commands.append((msg.get("payload") or {}).get("command", ""))
                    reply = b'{"id":1,"response":null}'
                else:
                    reply = json.dumps({"id": msg.get("id"),
                                        "response": outer.party}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(reply)

        self._httpd = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._httpd.server_address[1]
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()

    def stop(self):
        self._httpd.shutdown()
        self._httpd.server_close()


def wait_for(pred, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return False


srv = FakeTelesto()
srv.start()
cli = TelestoClient(uri=f"http://127.0.0.1:{srv.port}/", enabled=True,
                    delay_base_ms=0, delay_plus_ms=0, timeout=2.0)
cli.start()
cli.request_party_members(force=True)
check("real client builds the roster from GetPartyMembers",
      wait_for(lambda: cli.slot_of_actor("10FF0002") == 2))
check("first roster entry lands in slot 1", cli.slot_of_actor("10FF0001") == 1)
check("reachability flips to connected",
      wait_for(lambda: cli._reachable == (True, False)))

cli.mark_actor("10FF0002", "attack1")
check("mark_actor resolves the slot, /mk attack1 <2> on the wire",
      wait_for(lambda: "/mk attack1 <2>" in srv.commands))
before = len(srv.commands)
check("unknown actor fails closed", cli.mark_actor("10FF9999", "attack1") is False)
time.sleep(0.3)
check("no command hit the wire for the unknown actor",
      len(srv.commands) == before)

srv.ok = False
cli.ping()
check("HTTP 500 reports reachable but degraded",
      wait_for(lambda: cli._reachable == (True, True)))
srv.ok = True
cli.ping()
check("recovery reports healthy again",
      wait_for(lambda: cli._reachable == (True, False)))

srv.stop()
cli.ping()
check("dead server reports unreachable",
      wait_for(lambda: cli._reachable is not None and cli._reachable[0] is False))
cli.stop()
check("stop() joins the worker", cli._thread is None)

# ── mark_command fallbacks ──
check("known token passes through", mark_command("bind2", 3) == "/mk bind2 <3>")
check("empty marker falls back to next-attack",
      mark_command("", "me") == "/mk attack <me>")
check("unknown marker falls back like empty, drop-logged",
      mark_command("cler", "me") == "/mk attack <me>")

# ── a truthy non-string uri falls back to the default, init and configure ──
# Raw settings values reach the client from four MainWindow call sites. A
# number from a hand edited settings file used to kill the whole transport,
# urllib.request.Request raises on it outside _post's try.
c = TelestoClient(uri=12345, enabled=False)
check("numeric uri at init falls back to the default", c.uri == DEFAULT_URI)
c = TelestoClient(uri={"host": "x"}, enabled=False)
check("truthy non-str uri at init falls back too", c.uri == DEFAULT_URI)
c = TelestoClient(uri="http://127.0.0.1:9/", enabled=False)
c.configure(uri=12345)
check("numeric uri via configure falls back to the default", c.uri == DEFAULT_URI)
c.configure(uri="")
check("empty uri via configure falls back to the default", c.uri == DEFAULT_URI)
c.configure(uri="http://127.0.0.1:8/")
check("a real uri via configure sticks", c.uri == "http://127.0.0.1:8/")

# ── parent automarkers off runs the forced reset-with-clear first ──
import types


class ApplyWin:
    """Just enough of MainWindow for _apply_automark_state: settings, a
    recording client, and recording stand-ins for the two engine resets."""
    _apply_automark_state = mw.MainWindow._apply_automark_state

    class _Client:
        def __init__(self, events):
            self.events = events

        def configure(self, uri=None, enabled=None):
            self.events.append(("configure", enabled))

        def request_party_members(self, force=False):
            self.events.append(("refresh", force))

        def clear_actor(self, actor, force=False):
            self.events.append(("clear-actor", actor, force))
            return True

        def clear_self(self, force=False):
            self.events.append(("clear-self", force))
            return True

    _clear_player = mw.MainWindow._clear_player
    _is_me_actor = mw.MainWindow._is_me_actor

    def __init__(self, enabled):
        self._settings = {"telesto_enabled": enabled, "telesto_uri": "http://x/"}
        self.events = []
        self._telesto_client = self._Client(self.events)
        self._automark_pending = ["stale"]
        self._automark_active = {}
        self._me_id = None
        self._me_name = ""

    def _umad_chain_reset(self, clear_marks=False, force=False):
        self.events.append(("chain-reset", clear_marks, force))

    def _umad_gaze_reset(self, clear_marks=False, force=False):
        self.events.append(("gaze-reset", clear_marks, force))

    def _update_automark_status_label(self):
        pass


aw = ApplyWin(enabled=False)
aw._apply_automark_state()
check("disable runs both engine resets with clear and force, before configure",
      aw.events == [("chain-reset", True, True), ("gaze-reset", True, True),
                    ("configure", False)])
check("disable still purges queued rule retries", aw._automark_pending == [])

aw = ApplyWin(enabled=True)
aw._apply_automark_state()
check("enable runs no resets, configures on and probes the party",
      aw.events == [("configure", True), ("refresh", False)])

# ── parent off force-clears rule-placed signs and drops the bookkeeping ──
aw = ApplyWin(enabled=False)
aw._automark_active = {"10FF0001": "644", "me": "63E"}
aw._apply_automark_state()
check("disable force-clears the actors named in the rule bookkeeping",
      [e for e in aw.events if e[0] in ("clear-actor", "clear-self")]
      == [("clear-actor", "10FF0001", True), ("clear-self", True)])
check("disable drops the rule bookkeeping", aw._automark_active == {})
check("the rule clears land before configure takes the client down",
      aw.events.index(("clear-actor", "10FF0001", True))
      < aw.events.index(("configure", False)))

# ── the chain reset threads force into the per-player clears ──
class ResetWin:
    _umad_chain_reset = mw.MainWindow._umad_chain_reset
    _umad_name_of = mw.MainWindow._umad_name_of

    class _Chains:
        def outstanding(self):
            return ["10FF0001"]

        def reset(self):
            pass

    def __init__(self):
        self._umad_chains = self._Chains()
        self._umad_chain_pending = []
        self._umad_actor_names = {}
        self.clears = []

    def _clear_player(self, actor, name="", force=False):
        self.clears.append((actor, force))
        return True


rw = ResetWin()
rw._umad_chain_reset(clear_marks=True, force=True)
check("chain reset passes force down to the clears",
      rw.clears == [("10FF0001", True)])
rw = ResetWin()
rw._umad_chain_reset(clear_marks=True)
check("wipe-path reset stays unforced by default",
      rw.clears == [("10FF0001", False)])

# ── a forced clear lands even with the client disabled, unforced fails closed ──
srv2 = FakeTelesto()
srv2.start()
cli2 = TelestoClient(uri=f"http://127.0.0.1:{srv2.port}/", enabled=False,
                     delay_base_ms=0, delay_plus_ms=0, timeout=2.0)
cli2.start()
win = types.SimpleNamespace(_telesto_client=cli2,
                            _is_me_actor=lambda *a: True)
check("forced clear queues through a disabled client",
      mw.MainWindow._clear_player(win, "10FF0001", "n", force=True) is True)
check("forced clear reaches the wire while disabled",
      wait_for(lambda: "/mk clear <me>" in srv2.commands))
before = len(srv2.commands)
check("unforced clear still fails closed while disabled",
      mw.MainWindow._clear_player(win, "10FF0001", "n") is False)
time.sleep(0.3)
check("no unforced command hit the wire", len(srv2.commands) == before)
cli2.stop()
srv2.stop()

print()
if FAILS:
    print(f"{len(FAILS)} FAILED: {FAILS}")
    sys.exit(1)
print("all tests passed")
