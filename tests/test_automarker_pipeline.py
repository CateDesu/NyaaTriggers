from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from contextlib import nullcontext
import json
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from nyaatriggers.telesto_client import MAX_COMMAND_AGE_S, MarkerDelivery, TelestoClient, party_members_message
from nyaatriggers.ui.automarkers_tab import AutomarkersTabMixin
from nyaatriggers.ui.instance_tab import InstanceTabMixin
from nyaatriggers.umad_chains import (
    ACCELERATION_BOMB, FORKED_LIGHTNING, BlackHoleChains, CursedShriekPairs,
    GrandCrossPairs, StatusPairs,
)
from tests.test_overlay_retention import OverlayHost
from tests.test_transport_deadlines import wait_for


A, B, C, D = (f"1000000{i}" for i in range(1, 5))


class Endpoint:
    def __init__(self, actors=(A, B, C, D)):
        self.actors = tuple(actors)

    def __enter__(self):
        self.requests = []
        self.commands = []
        self.code = 200
        self.command_codes = {}
        self.stall = False
        self.entered = threading.Event()
        self.release = threading.Event()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                message = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                owner.requests.append(message)
                if message["type"] == "ExecuteCommand":
                    owner.commands.append(message["payload"]["command"])
                if owner.stall:
                    owner.entered.set()
                    owner.release.wait(3)
                body = b"null"
                if message["type"] == "GetPartyMembers":
                    body = json.dumps({"id": message["id"], "response": [
                        {"actor": actor, "order": slot}
                        for slot, actor in enumerate(owner.actors, 1)]}).encode()
                try:
                    command = message.get("payload", {}).get("command")
                    self.send_response(owner.command_codes.get(command, owner.code))
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": .02}, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/"
        return self

    def __exit__(self, *_args):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)


class Host(AutomarkersTabMixin, InstanceTabMixin):
    def __init__(self, client):
        self._telesto_client = client
        self._settings = {"telesto_enabled": True}
        self._current_fight_tag = "UMAD"
        self._me_id = ""
        self._me_name = ""
        self._umad_actor_names = {}
        self._umad_chain_enabled = False
        self._umad_gaze_enabled = True
        self._umad_chain_pending = []
        self._umad_gaze_pending = []
        self._umad_chain_pending_since = {}
        self._umad_gaze_pending_since = {}
        self._umad_chains = BlackHoleChains(lambda _actor: None)
        self._umad_gaze = CursedShriekPairs(slot_of=self._gaze_slot_of)
        self._umad_chain_flush_timer = SimpleNamespace(start=lambda: None)
        self._umad_gaze_flush_timer = SimpleNamespace(start=lambda: None)
        self._automark_pairs = StatusPairs([])
        self._automark_rules = []
        self._automark_active = {}
        self._automark_pending = []
        self._automark_cooldowns = {}
        self._automark_clear_on_loss = True
        self._automark_owners = {}

    def gain(self, actor, duration="60"):
        self._umad_gaze_line(["26", "ts", "15A7", "gaze", duration,
                              "E0000000", "", actor, "player"])

    def tell(self, timestamp="ts", vfx="462"):
        self._umad_gaze_line(["26", timestamp, "808", "vfx", "9999",
                              "E0000000", "", "40000001", "boss", vfx])


class AutomarkerPipelineTests(unittest.TestCase):
    def setUp(self):
        logs = tempfile.TemporaryDirectory()
        self.addCleanup(logs.cleanup)
        patcher = patch("nyaatriggers.drop_log._LOG_FILE", Path(logs.name) / "nyaatriggers.log")
        patcher.start()
        self.addCleanup(patcher.stop)

    def client(self, endpoint, *, roster=True, **kwargs):
        client = TelestoClient(uri=endpoint.url, enabled=True,
                               delay_base_ms=0, delay_plus_ms=0, **kwargs)
        self.addCleanup(client.stop)
        if roster:
            client._update_party_slots(json.dumps({"response": [
                {"actor": actor, "order": slot}
                for slot, actor in enumerate((A, B, C, D), 1)]}).encode())
        return client

    def drain(self, client, endpoint):
        self._probe_id = getattr(self, "_probe_id", 1_000_001) + 1
        message = party_members_message()
        message["id"] = self._probe_id
        client.start()
        self.assertTrue(wait_for(lambda: client._enqueue(message, delay=False, force=True)))
        self.assertTrue(wait_for(lambda: any(item["id"] == message["id"]
                                             for item in endpoint.requests)))

    def rule_host(self, client, status, fight="UMAD"):
        host = Host(client)
        host._umad_gaze_enabled = False
        host._current_fight_tag = fight
        host._automark_rules = [{"fight": fight, "status": status,
                                 "marker": "triangle", "scope": "any"}]
        host._automark_pairs = StatusPairs(status.split("+"))
        host._save_settings = lambda: None
        host._ws = SimpleNamespace(request_combatants_once=lambda: None)
        return host

    def rule_gain(self, host, status):
        for part in status.split("+"):
            host._automark_pairs.on_gain(part, A, time.monotonic())
            host._match_automark_rules(["26", "ts", part, "status", "60",
                                        "E0000000", "", A, "player"])

    def grand_cross_host(self, client, mechanic):
        status, count = (ACCELERATION_BOMB, 4) if mechanic == "bomb" else (FORKED_LIGHTNING, 2)
        host = Host(client)
        host._settings[f"umad_{mechanic}_enabled"] = True
        host._umad_grand_cross = {mechanic: GrandCrossPairs(status, count,
            markers={f"{kind}{index}": f"attack{offset + index}"
                     for kind, offset in (("real", 0), ("fake", count))
                     for index in range(1, count + 1)})}
        host._umad_grand_cross_pending = {mechanic: []}
        host._umad_grand_cross_pending_since = {mechanic: {}}
        host._umad_grand_cross_flush_timer = SimpleNamespace(start=lambda: None, stop=lambda: None)
        return host, status, (A, B, C, D)[:count]

    def grand_cross_wave(self, host, status, actors, duration=60):
        host._umad_grand_cross_line(["26", "tell", "808", "VFX", "9999",
                                    "E0000000", "", "40000001", "boss", "462"])
        for actor in actors:
            host._umad_grand_cross_line(["26", "gain", status, "status", str(duration),
                                        "40000001", "boss", actor, "player"])

    def test_older_mechanic_loss_cannot_clear_newer_local_mark(self):
        with Endpoint() as endpoint:
            client = self.client(endpoint)
            host = Host(client)
            host._dispatch_umad_chain_actions([("mark", A, "attack1")])
            self.drain(client, endpoint)
            host._dispatch_umad_gaze_actions([("mark", A, "ignore1")])
            host._dispatch_umad_chain_actions([("clear", A)])
            self.drain(client, endpoint)
            self.assertEqual(endpoint.commands, ["/mk attack1 <1>", "/mk ignore1 <1>"])
            host._dispatch_umad_gaze_actions([("clear", A)])
            self.drain(client, endpoint)
            self.assertEqual(endpoint.commands[-1], "/mk clear <1>")

    def test_sign_transfer_does_not_lend_old_status_ownership_to_a_new_sign(self):
        with Endpoint() as endpoint:
            client = self.client(endpoint)
            host = Host(client)
            host._dispatch_umad_chain_actions([("mark", A, "attack1")])
            self.drain(client, endpoint)
            host._dispatch_umad_chain_actions([("mark", B, "attack1")])
            self.drain(client, endpoint)
            host._dispatch_umad_gaze_actions([("mark", A, "ignore1")])
            host._dispatch_umad_chain_actions([("clear", A)])
            self.drain(client, endpoint)
            self.assertEqual(endpoint.commands, ["/mk attack1 <1>", "/mk attack1 <2>",
                                                 "/mk ignore1 <1>"])

    def test_pending_old_clear_is_removed_before_new_mechanic_mark(self):
        with Endpoint() as endpoint:
            client = self.client(endpoint)
            host = Host(client)
            host._dispatch_umad_chain_actions([("mark", A, "attack1")])
            self.drain(client, endpoint)
            client._update_party_slots(b'{"response":[]}')
            host._dispatch_umad_chain_actions([("clear", A)])
            self.assertEqual(host._umad_chain_pending, [("clear", A)])
            client._update_party_slots(b'{"response":[{"actor":"10000001","order":1}]}')
            host._dispatch_umad_gaze_actions([("mark", A, "ignore1")])
            host._retry_umad_chain_pending()
            self.drain(client, endpoint)
            self.assertEqual(endpoint.commands, ["/mk attack1 <1>", "/mk ignore1 <1>"])

    def test_delayed_roster_cannot_restore_superseded_rule_mark(self):
        with Endpoint() as endpoint:
            client = self.client(endpoint, roster=False)
            host = Host(client)
            host._umad_gaze_enabled = False
            host._automark_rules = [
                {"fight": "UMAD", "status": status, "scope": "any", "marker": "attack1"}
                for status in ("15A8", "15A9")]
            for actor, status in ((A, "15A8"), (B, "15A9")):
                host._match_automark_rules(["26", "ts", status, "status", "60",
                                            "E0000000", "", actor, "player"])
            self.assertEqual(len(host._automark_pending), 2)
            client._update_party_slots(b'{"response":[{"actor":"10000002","order":2}]}')
            host._retry_automark_pending()
            client._update_party_slots(b'{"response":[{"actor":"10000001","order":1},'
                                      b'{"actor":"10000002","order":2}]}')
            host._retry_automark_pending()
            self.drain(client, endpoint)
            self.assertEqual(endpoint.commands, ["/mk attack1 <2>"])
            self.assertFalse(host._automark_pending)
            self.assertEqual(host._automark_owners, {B: ("rule", "attack1")})

    def test_refused_replacement_preserves_existing_rule_cleanup(self):
        with Endpoint() as endpoint:
            client = self.client(endpoint, max_queue=1)
            host = self.rule_host(client, "15A8")
            client.start()
            self.rule_gain(host, "15A8")
            self.assertTrue(wait_for(lambda: endpoint.commands == ["/mk triangle <1>"]))
            self.drain(client, endpoint)
            entered, release = threading.Event(), threading.Event()

            def delay(_stopping):
                entered.set()
                release.wait(3)

            try:
                with patch.object(client, "_sleep_command_delay", side_effect=delay):
                    self.assertTrue(client.mark_actor(C, "circle"))
                    self.assertTrue(entered.wait(2))
                    self.assertTrue(client.mark_actor(D, "cross"))
                    host._dispatch_umad_gaze_actions([("mark", A, "ignore1")])
                    self.assertEqual(host._automark_active, {A: "15A8"})
                    self.assertEqual(host._automark_owners, {A: ("rule", "triangle")})
                    release.set()
                    self.assertTrue(wait_for(lambda: len(endpoint.commands) == 3))
                    host._match_automark_unmark(["30", "ts", "15A8", "status", "0",
                                               "E0000000", "", A, "player"])
                    self.drain(client, endpoint)
                self.assertEqual(endpoint.commands, ["/mk triangle <1>", "/mk circle <3>",
                                                     "/mk cross <4>", "/mk clear <1>"])
            finally:
                release.set()

    def test_rejected_transfer_preserves_original_holder_cleanup(self):
        for owner in ("rule", "chains", "gaze"):
            for self_target in (False, True):
                with self.subTest(owner=owner, self_target=self_target), Endpoint() as endpoint:
                    client = self.client(endpoint)
                    host = self.rule_host(client, "15A8")
                    if self_target:
                        host._me_id = A
                    host._automark_rules.append({"fight": "UMAD", "status": "15A9",
                                                "marker": "triangle", "scope": "any"})
                    if owner == "rule":
                        self.rule_gain(host, "15A8")
                    else:
                        host._dispatch_mark_actions([("mark", A, "triangle")], [], owner=owner)
                    self.drain(client, endpoint)
                    endpoint.code = 503
                    host._match_automark_rules(["26", "ts", "15A9", "status", "60",
                                               "E0000000", "", B, "Player B"])
                    self.assertTrue(wait_for(lambda: client.last_status() == (True, True)))
                    endpoint.code = 200
                    if owner == "rule":
                        host._match_automark_unmark(["30", "ts", "15A8", "status", "0",
                                                    "E0000000", "", A, "Player A"])
                    else:
                        host._dispatch_mark_actions([("clear", A)], [], owner=owner)
                    self.drain(client, endpoint)
                    target = "me" if self_target else "1"
                    self.assertEqual(endpoint.commands, [f"/mk triangle <{target}>",
                                                         "/mk triangle <2>", f"/mk clear <{target}>"])
                    self.assertEqual(host._automark_owners, {B: ("rule", "triangle")})

    def test_refused_replacement_preserves_rule_retry_until_claim_succeeds(self):
        with Endpoint() as endpoint:
            client = self.client(endpoint, roster=False)
            host = self.rule_host(client, "15A8")
            self.rule_gain(host, "15A8")
            host._dispatch_umad_gaze_actions([("mark", A, "ignore1")])
            self.assertEqual(len(host._automark_pending), 1)
            client._update_party_slots(b'{"response":[{"actor":"10000001","order":1}]}')
            host._retry_umad_gaze_pending()
            self.assertFalse(host._automark_pending)
            self.drain(client, endpoint)
            self.assertEqual(endpoint.commands, ["/mk ignore1 <1>"])

    def test_same_actor_replacement_cleanup_follows_delivery(self):
        for owner in ("rule", "chains", "gaze"):
            for replacement in ("rule", "gaze"):
                if owner == replacement == "gaze":
                    continue
                for code in (200, 503):
                    for self_target in (False, True):
                        with self.subTest(owner=owner, replacement=replacement, code=code,
                                          self_target=self_target), Endpoint() as endpoint:
                            client = self.client(endpoint)
                            host = self.rule_host(client, "15A8")
                            host._me_id = A if self_target else ""
                            target = "me" if self_target else "1"
                            host._automark_rules.append({"fight": "UMAD", "status": "15A9",
                                                        "marker": "ignore1", "scope": "any"})
                            if owner == "rule":
                                self.rule_gain(host, "15A8")
                            else:
                                host._dispatch_mark_actions([("mark", A, "triangle")], [], owner=owner)
                            self.drain(client, endpoint)
                            endpoint.command_codes[f"/mk ignore1 <{target}>"] = code
                            entered, release = threading.Event(), threading.Event()

                            def delay(_stopping):
                                entered.set()
                                release.wait(3)

                            try:
                                with patch.object(client, "_sleep_command_delay", side_effect=delay):
                                    if replacement == "rule":
                                        self.rule_gain(host, "15A9")
                                    else:
                                        host._dispatch_umad_gaze_actions([("mark", A, "ignore1")])
                                    self.assertTrue(entered.wait(2))
                                    if owner == "rule":
                                        host._match_automark_unmark(["30", "ts", "15A8", "status", "0",
                                                                   "E0000000", "", A, "player"])
                                    else:
                                        host._dispatch_mark_actions([("clear", A)], [], owner=owner)
                                    release.set()
                                    self.drain(client, endpoint)
                                expected = [f"/mk triangle <{target}>", f"/mk ignore1 <{target}>"]
                                if code == 503:
                                    expected.append(f"/mk clear <{target}>")
                                self.assertEqual(endpoint.commands, expected)
                                if code == 200:
                                    if replacement == "rule":
                                        host._match_automark_unmark(["30", "ts", "15A9", "status", "0",
                                                                   "E0000000", "", A, "player"])
                                    else:
                                        host._dispatch_umad_gaze_actions([("clear", A)])
                                    self.drain(client, endpoint)
                                    self.assertEqual(endpoint.commands, expected + [f"/mk clear <{target}>"])
                            finally:
                                release.set()

    def test_failed_cleanup_remains_available_when_disabling(self):
        with Endpoint() as endpoint:
            client = self.client(endpoint)
            host = self.rule_host(client, "15A8")
            host._settings["telesto_uri"] = endpoint.url
            host._update_automark_status_label = lambda: None
            self.rule_gain(host, "15A8")
            self.drain(client, endpoint)
            endpoint.command_codes["/mk clear <1>"] = 503
            host._match_automark_unmark(["30", "ts", "15A8", "status", "0",
                                       "E0000000", "", A, "player"])
            self.drain(client, endpoint)
            endpoint.command_codes.clear()
            host._settings["telesto_enabled"] = False
            host._apply_automark_state()
            self.drain(client, endpoint)
            self.assertEqual(endpoint.commands, ["/mk triangle <1>", "/mk clear <1>", "/mk clear <1>"])

    def test_disabled_cleanup_retries_after_recovery(self):
        for missing_roster in (False, True):
            with self.subTest(missing_roster=missing_roster), Endpoint() as endpoint:
                client = self.client(endpoint)
                host = self.rule_host(client, "15A8")
                host._settings["telesto_uri"] = endpoint.url
                host._update_automark_status_label = lambda: None
                self.rule_gain(host, "15A8")
                self.drain(client, endpoint)
                if missing_roster:
                    client._update_party_slots(b'{"response":[]}')
                else:
                    endpoint.command_codes["/mk clear <1>"] = 503
                host._settings["telesto_enabled"] = False
                host._apply_automark_state()
                self.drain(client, endpoint)
                endpoint.command_codes.clear()
                for _ in range(2):
                    host._refresh_telesto_party()
                    self.drain(client, endpoint)
                clears = 1 if missing_roster else 2
                self.assertEqual(endpoint.commands, ["/mk triangle <1>"] + ["/mk clear <1>"] * clears)
                self.assertFalse(client.is_enabled())
                self.assertFalse(host._automark_deliveries[A][0].delivery.current)
                before = len(endpoint.requests)
                host._refresh_telesto_party()
                self.drain(client, endpoint)
                self.assertEqual(len(endpoint.requests), before + 1)

    def test_timeout_replacement_keeps_cleanup_with_the_new_owner(self):
        for self_target in (False, True):
            for transfer in (False, True):
                with self.subTest(self_target=self_target, transfer=transfer), Endpoint() as endpoint:
                    client = self.client(endpoint, timeout=.15)
                    host = self.rule_host(client, "15A8")
                    host._me_id = A if self_target else ""
                    target = "me" if self_target else "1"
                    self.rule_gain(host, "15A8")
                    self.drain(client, endpoint)
                    endpoint.stall = True
                    actor, marker = (B, "triangle") if transfer else (A, "ignore1")
                    host._dispatch_umad_gaze_actions([("mark", actor, marker)])
                    self.assertTrue(endpoint.entered.wait(2))
                    host._match_automark_unmark(["30", "ts", "15A8", "status", "0",
                                               "E0000000", "", A, "player"])
                    self.assertTrue(wait_for(lambda: not host._automark_deliveries[actor][-1].delivery.pending))
                    endpoint.stall = False
                    endpoint.release.set()
                    self.drain(client, endpoint)
                    expected = [f"/mk triangle <{target}>", f"/mk {marker} <{'2' if transfer else target}>"]
                    if transfer:
                        expected.append(f"/mk clear <{target}>")
                    self.assertEqual(endpoint.commands, expected)
                    host._dispatch_umad_gaze_actions([("clear", actor)])
                    self.drain(client, endpoint)
                    self.assertEqual(endpoint.commands, expected + [f"/mk clear <{'2' if transfer else target}>"])

    def test_self_test_commands_retire_numeric_delivery_after_identity_arrives(self):
        for command in ("mark", "clear"):
            for code in (200, 503):
                with self.subTest(command=command, code=code), Endpoint() as endpoint:
                    client = self.client(endpoint)
                    host = self.rule_host(client, "15A8")
                    host._settings["telesto_uri"] = endpoint.url
                    host._automark_test_combo = SimpleNamespace(currentData=lambda: "circle")
                    self.rule_gain(host, "15A8")
                    self.drain(client, endpoint)
                    host._me_id = A
                    client._update_party_slots(b'{"response":[]}')
                    manual = "/mk circle <me>" if command == "mark" else "/mk clear <me>"
                    endpoint.command_codes[manual] = code
                    (host._on_automark_test if command == "mark" else host._on_automark_clear)()
                    self.drain(client, endpoint)
                    endpoint.command_codes.clear()
                    host._match_automark_unmark(["30", "ts", "15A8", "status", "0",
                                               "E0000000", "", A, "player"])
                    self.drain(client, endpoint)
                    expected = ["/mk triangle <1>", manual]
                    if code == 503:
                        expected.append("/mk clear <me>")
                    self.assertEqual(endpoint.commands, expected)

    def test_stopped_worker_reply_cannot_retire_a_new_workers_mark(self):
        with Endpoint() as endpoint:
            client = self.client(endpoint)
            host = self.rule_host(client, "15A8")
            entered, release = threading.Event(), threading.Event()
            read_response = client._read_response

            def hold_reply(request, timeout):
                result = read_response(request, timeout)
                if json.loads(request.data).get("payload", {}).get("command") == "/mk circle <me>":
                    entered.set()
                    release.wait(3)
                return result

            try:
                with patch.object(client, "_read_response", side_effect=hold_reply):
                    client.start()
                    client.mark_self("circle", actor_id=A)
                    self.assertTrue(entered.wait(2))
                    old_worker = client._thread
                    client.request_stop()
                    client.start()
                    self.rule_gain(host, "15A8")
                    self.drain(client, endpoint)
                    release.set()
                    old_worker.join(2)
                    self.assertFalse(old_worker.is_alive())
                    host._match_automark_unmark(["30", "ts", "15A8", "status", "0",
                                               "E0000000", "", A, "player"])
                    self.drain(client, endpoint)
                self.assertEqual(endpoint.commands, ["/mk circle <me>", "/mk triangle <1>", "/mk clear <1>"])
            finally:
                release.set()

    def test_disable_cleans_a_mark_with_an_unacknowledged_delivery(self):
        with Endpoint() as endpoint:
            client = self.client(endpoint)
            host = self.rule_host(client, "15A8")
            host._settings["telesto_uri"] = endpoint.url
            host._update_automark_status_label = lambda: None
            endpoint.stall = True
            client.start()
            self.rule_gain(host, "15A8")
            self.assertTrue(endpoint.entered.wait(2))
            host._settings["telesto_enabled"] = False
            host._apply_automark_state()
            endpoint.stall = False
            endpoint.release.set()
            self.drain(client, endpoint)
            self.assertEqual(endpoint.commands, ["/mk triangle <1>", "/mk clear <1>"])

    def test_lost_status_cleanup_survives_missing_roster_and_failed_replacement(self):
        for code in (200, 503):
            with self.subTest(code=code), Endpoint() as endpoint:
                client = self.client(endpoint)
                host = Host(client)
                host._dispatch_umad_chain_actions([("mark", A, "attack1")])
                self.drain(client, endpoint)
                client._update_party_slots(b'{"response":[]}')
                host._dispatch_umad_chain_actions([("clear", A)])
                self.assertTrue(host._umad_chain_pending)
                client._update_party_slots(b'{"response":[{"actor":"10000001","order":1}]}')
                endpoint.command_codes["/mk ignore1 <1>"] = code
                host._dispatch_umad_gaze_actions([("mark", A, "ignore1")])
                self.drain(client, endpoint)
                host._refresh_telesto_party()
                self.drain(client, endpoint)
                expected = ["/mk attack1 <1>", "/mk ignore1 <1>"]
                if code == 503:
                    expected.append("/mk clear <1>")
                self.assertEqual(endpoint.commands, expected)

    def test_manual_party_clear_retires_self_marker_ownership(self):
        with Endpoint() as endpoint:
            client = self.client(endpoint)
            host = self.rule_host(client, "15A8")
            host._me_id = A
            self.rule_gain(host, "15A8")
            self.drain(client, endpoint)
            client.clear_all()
            self.drain(client, endpoint)
            host._match_automark_unmark(["30", "ts", "15A8", "status", "0",
                                       "E0000000", "", A, "player"])
            self.drain(client, endpoint)
            self.assertEqual(endpoint.commands, ["/mk triangle <me>"]
                             + [f"/mk clear <{slot}>" for slot in range(1, 9)])

    def test_mechanic_toggles_suspend_old_rule_retries_in_umad(self):
        for feature, status in (("gaze", "15A7"), ("chains", "644+BBC")):
            for fight in ("UMAD", "", "FRU"):
                with self.subTest(feature=feature, fight=fight), Endpoint() as endpoint:
                    client = self.client(endpoint, roster=False)
                    host = self.rule_host(client, status, fight)
                    self.rule_gain(host, status)
                    self.assertEqual(len(host._automark_pending), 1)
                    getattr(host, "_on_umad_gaze_toggled" if feature == "gaze"
                            else "_on_umad_chain_toggled")(True)
                    client._update_party_slots(b'{"response":[{"actor":"10000001","order":1}]}')
                    host._refresh_telesto_party()
                    self.drain(client, endpoint)
                    expected = ["/mk triangle <1>"] if fight == "FRU" else []
                    self.assertEqual(endpoint.commands, expected)
                    self.assertFalse(host._automark_pending)

    def test_compound_other_part_cannot_bypass_mechanic_suspension(self):
        for feature, status in (("gaze", "15A7+15AA"), ("chains", "644+15AA")):
            for fight in ("UMAD", "FRU"):
                with self.subTest(feature=feature, fight=fight), Endpoint() as endpoint:
                    client = self.client(endpoint)
                    host = self.rule_host(client, status, fight)
                    setattr(host, "_umad_gaze_enabled" if feature == "gaze"
                            else "_umad_chain_enabled", True)
                    self.rule_gain(host, status)
                    self.drain(client, endpoint)
                    expected = ["/mk triangle <1>"] if fight == "FRU" else []
                    self.assertEqual(endpoint.commands, expected)

    def test_expired_gaze_is_not_retried_when_party_slots_arrive(self):
        with Endpoint() as endpoint:
            client = self.client(endpoint, roster=False)
            host = Host(client)
            now = time.monotonic()
            with patch("nyaatriggers.ui.automarkers_tab.time.monotonic", return_value=now):
                host.tell()
                host.gain(A, "5")
                host.gain(B, "5")
            self.assertEqual(len(host._umad_gaze_pending), 2)
            client._update_party_slots(b'{"response":[{"actor":"10000001","order":1},'
                                      b'{"actor":"10000002","order":2}]}')
            with patch("nyaatriggers.ui.automarkers_tab.time.monotonic", return_value=now + 6):
                host._retry_umad_gaze_pending()
            self.drain(client, endpoint)
            self.assertEqual(endpoint.commands, [])
            self.assertFalse(host._umad_gaze_pending)

    def test_reordered_gaze_tell_and_duplicate_inputs_send_exactly_one_pair(self):
        with Endpoint() as endpoint:
            client = self.client(endpoint)
            host = Host(client)
            host.gain(B)
            host.gain(A)
            host.tell()
            host.tell()
            self.drain(client, endpoint)
            self.assertEqual(endpoint.commands, ["/mk ignore1 <1>", "/mk ignore2 <2>"])
            host.gain(A)
            host.gain(B)
            self.drain(client, endpoint)
            self.assertEqual(endpoint.commands, ["/mk ignore1 <1>", "/mk ignore2 <2>"])

    def test_expired_incomplete_gaze_cannot_reach_telesto_or_consume_a_wave(self):
        for tell_first in (True, False):
            with self.subTest(tell_first=tell_first), Endpoint() as endpoint:
                client = self.client(endpoint)
                host = Host(client)
                clock = [time.monotonic()]
                with patch("nyaatriggers.ui.automarkers_tab.time.monotonic", side_effect=lambda: clock[0]):
                    if tell_first:
                        host.tell("first")
                    host.gain(A, "1")
                    if not tell_first:
                        host.gain(B, "10")
                    clock[0] += 1.1
                    if tell_first:
                        host.gain(B, "10")
                    else:
                        host.tell("first")
                    self.drain(client, endpoint)
                    self.assertEqual(endpoint.commands, [])
                    self.assertEqual(host._umad_gaze._sets_done, 0)
                    host.tell("second", "461")
                    host.gain(C, "10")
                    self.drain(client, endpoint)
                self.assertEqual(endpoint.commands, ["/mk bind1 <2>", "/mk bind2 <3>"])

    def test_recorded_same_polarity_gaze_waves_and_losses_reach_exact_slots(self):
        case = json.loads((Path(__file__).parent / "fixtures/umad_gazes.json").read_text())[0]
        with Endpoint() as endpoint:
            client = self.client(endpoint)
            host = Host(client)
            start = time.monotonic()
            for now, raw in case["events"]:
                fields = raw.split("|")
                with patch("nyaatriggers.ui.automarkers_tab.time.monotonic", return_value=start + now):
                    host._on_umad_gaze_flush()
                    if fields[0] == "20":
                        host._umad_gaze_cast(fields)
                    else:
                        host._umad_gaze_line(fields)
                self.drain(client, endpoint)
            self.drain(client, endpoint)
            self.assertEqual(endpoint.commands, ["/mk bind1 <2>", "/mk bind2 <4>",
                                                 "/mk clear <2>", "/mk bind1 <1>",
                                                 "/mk clear <4>", "/mk clear <1>"])

    def test_chain_input_delays_duplicates_and_cleanse_order_reach_exact_slots(self):
        with Endpoint() as endpoint:
            client = self.client(endpoint)
            host = Host(client)
            host._umad_chain_enabled = True
            actors = [A, B, C, D, "10000011", "10000012", "10000021", "10000022"]
            roles = {actor: "dps" if index < 4 else "support"
                     for index, actor in enumerate(actors)}
            host._umad_chains = BlackHoleChains(roles.get)
            client._update_party_slots(json.dumps({"response": [
                {"actor": actor, "order": index + 1}
                for index, actor in enumerate(actors)]}).encode())
            now = time.monotonic()
            statuses = [(D, "644"), (actors[-1], "644")]
            statuses += [(actor, status) for actor, order in zip(actors, ("BBC", "BBD", "BBE", "BBC",
                                                                          "BBC", "BBD", "BBE", "BBD"))
                         for status in (order, "154E") if actor != A]
            for actor, status in statuses:
                with patch("nyaatriggers.ui.automarkers_tab.time.monotonic", return_value=now):
                    host._umad_chain_line(["26", "ts", status, "status", "60",
                                           "E0000000", "", actor, "player"])
            self.drain(client, endpoint)
            with patch("nyaatriggers.ui.automarkers_tab.time.monotonic", return_value=now + 1.2):
                host._on_umad_chain_flush()
            for status in ("BBC", "154E", "154E"):
                with patch("nyaatriggers.ui.automarkers_tab.time.monotonic", return_value=now + 2):
                    host._umad_chain_line(["26", "ts", status, "status", "60",
                                           "E0000000", "", A, "player"])
            self.drain(client, endpoint)
            for index, actor in enumerate((A, A, B, C)):
                with patch("nyaatriggers.ui.automarkers_tab.time.monotonic", return_value=now + 3 + index):
                    host._umad_chain_line(["30", "ts", "154E", "status", "0",
                                           "E0000000", "", actor, "player"])
                self.drain(client, endpoint)
            self.drain(client, endpoint)
            self.assertEqual(endpoint.commands, ["/mk attack2 <5>", "/mk attack3 <4>",
                                                 "/mk attack1 <1>", "/mk attack1 <2>",
                                                 "/mk attack1 <3>", "/mk clear <3>"])

    def test_chain_handoff_cancels_the_unattempted_finished_holder(self):
        with Endpoint() as endpoint:
            client = self.client(endpoint)
            host = Host(client)
            host._umad_chain_enabled = True
            host._umad_chains = BlackHoleChains({A: "dps", B: "dps", C: "dps"}.get,
                                               include_accretion=False)
            for actor, status in ((D, "644"), ("10000005", "644"),
                                  (A, "BBC"), (A, "154E"), (B, "BBD"), (B, "154E"),
                                  (C, "BBE"), (C, "154E")):
                host._umad_chain_line(["26", "ts", status, "Status", "120",
                                       "40000001", "Chaos", actor, "Player"])
            first = host._automark_deliveries[A][0].delivery
            host._umad_chain_line(["30", "ts", "154E", "Status", "0",
                                   "40000001", "Chaos", A, "Player"])
            self.assertTrue(first.cancelled)
            self.assertFalse(first.pending or first.attempted)
            self.drain(client, endpoint)
            self.assertEqual(endpoint.commands, ["/mk attack1 <2>"])

    def test_new_actor_or_sign_claim_cancels_only_superseded_unattempted_marks(self):
        for transfer in (False, True):
            with self.subTest(transfer=transfer), Endpoint() as endpoint:
                client = self.client(endpoint)
                host = Host(client)
                host._dispatch_mark_actions([("mark", A, "triangle"), ("mark", C, "square")],
                                            [], owner="chains")
                old = host._automark_deliveries[A][0].delivery
                unrelated = host._automark_deliveries[C][0].delivery
                replacement = ("mark", B, "triangle") if transfer else ("mark", A, "ignore1")
                host._dispatch_mark_actions([replacement], [], owner="gaze")
                self.assertTrue(old.cancelled)
                self.assertFalse(unrelated.cancelled)
                self.drain(client, endpoint)
                expected = "/mk triangle <2>" if transfer else "/mk ignore1 <1>"
                self.assertEqual(endpoint.commands, ["/mk square <3>", expected])

    def test_rejected_replacement_enqueue_keeps_the_previous_queued_mark(self):
        for transfer in (False, True):
            with self.subTest(transfer=transfer), Endpoint() as endpoint:
                client = self.client(endpoint, max_queue=1)
                host = Host(client)
                host._dispatch_mark_actions([("mark", A, "triangle")], [], owner="chains")
                old = host._automark_deliveries[A][0].delivery
                replacement = ("mark", B, "triangle") if transfer else ("mark", A, "ignore1")
                pending = host._dispatch_mark_actions([replacement], [], owner="gaze")
                self.assertEqual(pending, [replacement])
                self.assertTrue(old.pending)
                self.assertFalse(old.cancelled)
                self.drain(client, endpoint)
                self.assertEqual(endpoint.commands, ["/mk triangle <1>"])

    def test_superseding_an_unattempted_rule_releases_its_unused_cooldown(self):
        with Endpoint() as endpoint:
            client = self.client(endpoint)
            host = self.rule_host(client, "ABC")
            clock = [1000.0]
            with patch("time.monotonic", side_effect=lambda: clock[0]):
                self.rule_gain(host, "ABC")
                old = host._automark_deliveries[A][0].delivery
                clock[0] += .1
                host._dispatch_mark_actions([("mark", A, "ignore1")], [], owner="gaze")
                self.assertTrue(old.cancelled)
                clock[0] += .1
                self.rule_gain(host, "ABC")
                self.drain(client, endpoint)
            self.assertEqual(endpoint.commands, ["/mk triangle <1>"])

    def test_superseded_attempted_mark_keeps_cleanup_if_replacement_is_rejected(self):
        for transfer in (False, True):
            with self.subTest(transfer=transfer), Endpoint() as endpoint:
                client = self.client(endpoint)
                host = Host(client)
                host._dispatch_mark_actions([("mark", A, "triangle")], [], owner="chains")
                self.drain(client, endpoint)
                old = host._automark_deliveries[A][0].delivery
                self.assertTrue(old.attempted and old.current)
                command = "/mk triangle <2>" if transfer else "/mk ignore1 <1>"
                endpoint.command_codes[command] = 503
                replacement = ("mark", B, "triangle") if transfer else ("mark", A, "ignore1")
                host._dispatch_mark_actions([replacement], [], owner="gaze")
                self.assertFalse(old.cancelled)
                self.drain(client, endpoint)
                self.assertTrue(old.current)
                host._dispatch_mark_actions([("clear", A)], [], owner="chains")
                self.drain(client, endpoint)
                self.assertEqual(endpoint.commands, ["/mk triangle <1>", command, "/mk clear <1>"])

    def test_overdue_queued_mark_is_dropped_while_cleanup_still_runs(self):
        with Endpoint() as endpoint:
            client = self.client(endpoint)
            now = time.monotonic()
            with patch("nyaatriggers.telesto_client.time.monotonic", return_value=now - MAX_COMMAND_AGE_S - 1):
                client.mark_actor(A, "attack1")
                client.clear_actor(A)
            self.drain(client, endpoint)
            self.assertEqual(endpoint.commands, ["/mk clear <1>"])

    def test_grand_cross_losses_cancel_marks_before_transport_attempts(self):
        for mechanic in ("bomb", "lightning"):
            with self.subTest(mechanic=mechanic), Endpoint() as endpoint:
                client = self.client(endpoint)
                host, status, actors = self.grand_cross_host(client, mechanic)
                entered, release = threading.Event(), threading.Event()

                def delay(_stopping):
                    entered.set()
                    release.wait(3)

                try:
                    with patch.object(client, "_sleep_command_delay", side_effect=delay):
                        self.grand_cross_wave(host, status, actors)
                        client.start()
                        self.assertTrue(entered.wait(2))
                        for actor in actors:
                            host._umad_grand_cross_line(["30", "loss", status, "status", "0",
                                                        "40000001", "boss", actor, "player"])
                        self.assertEqual(endpoint.commands, [])
                        self.assertTrue(all(not claim.delivery.pending
                                            for claims in host._automark_deliveries.values()
                                            for claim in claims))
                        release.set()
                        self.drain(client, endpoint)
                    self.assertEqual(endpoint.commands, [])
                    self.assertEqual(host._umad_grand_cross[mechanic].outstanding(), [])
                finally:
                    release.set()

    def test_grand_cross_expiry_or_reset_cancels_queued_transport(self):
        for mechanic in ("bomb", "lightning"):
            for boundary in ("expiry", "reset", "clear reset", "other duty"):
                with self.subTest(mechanic=mechanic, boundary=boundary), Endpoint() as endpoint:
                    client = self.client(endpoint)
                    host, status, actors = self.grand_cross_host(client, mechanic)
                    now = time.monotonic()
                    with patch("nyaatriggers.ui.automarkers_tab.time.monotonic", return_value=now):
                        self.grand_cross_wave(host, status, actors, duration=3)
                    if boundary == "expiry":
                        with patch("nyaatriggers.ui.automarkers_tab.time.monotonic", return_value=now + 3):
                            host._retry_umad_grand_cross_pending()
                    elif boundary == "other duty":
                        host._current_fight_tag = "FRU"
                        host._retry_umad_grand_cross_pending()
                    else:
                        host._umad_grand_cross_reset(clear_marks=boundary == "clear reset")
                    self.drain(client, endpoint)
                    self.assertEqual(endpoint.commands, [])
                    self.assertEqual(host._umad_grand_cross[mechanic].outstanding(), [])

    def test_grand_cross_delivery_deadline_survives_delayed_roster_and_timer_flush(self):
        for mechanic in ("bomb", "lightning"):
            for boundary in ("queued", "delay", "post"):
                with self.subTest(mechanic=mechanic, boundary=boundary), Endpoint() as endpoint:
                    client = self.client(endpoint, roster=False)
                    host, status, actors = self.grand_cross_host(client, mechanic)
                    now = time.monotonic()
                    clock = [now]
                    with patch("time.monotonic", side_effect=lambda: clock[0]):
                        self.grand_cross_wave(host, status, actors, duration=51)
                        self.assertEqual(len(host._umad_grand_cross_pending[mechanic]), len(actors))
                        client._update_party_slots(json.dumps({"response": [
                            {"actor": actor, "order": index}
                            for index, actor in enumerate(actors, 1)]}).encode())
                        clock[0] = now + 25
                        host._retry_umad_grand_cross_pending()
                        self.assertFalse(host._umad_grand_cross_pending[mechanic])
                        self.assertEqual(len(host._automark_deliveries), len(actors))
                        if boundary == "queued":
                            clock[0] = now + 51.1

                        def expire(_argument):
                            clock[0] = now + 51.1

                        post = client._post

                        def expire_before_post(message):
                            expire(message)
                            post(message)

                        with patch.object(client, "_sleep_command_delay",
                                          side_effect=expire if boundary == "delay" else None), \
                                patch.object(client, "_post",
                                             side_effect=expire_before_post if boundary == "post" else post):
                            while not client._queue.empty():
                                item = client._queue.get_nowait()
                                try:
                                    client._send_queued(item, client._stopping)
                                finally:
                                    client._finish_delivery(item)
                    self.assertEqual(endpoint.commands, [])
                    self.assertTrue(all(not claim.delivery.pending and not claim.delivery.current
                                        for claims in host._automark_deliveries.values() for claim in claims))

    def test_raw_loss_cancels_unattempted_marks_with_auto_cleanse_disabled(self):
        for status in ("15AA", "15AA+15A7"):
            for self_target in (False, True):
                with self.subTest(status=status, self_target=self_target), Endpoint() as endpoint:
                    client = self.client(endpoint)
                    host = self.rule_host(client, status)
                    host._me_id = A if self_target else ""
                    host._automark_clear_on_loss = False
                    self.rule_gain(host, status)
                    delivery = host._automark_deliveries[A][-1].delivery
                    host._match_automark_unmark(["30", "loss", "FFFF", "status", "0",
                                               "E0000000", "", A, "player"])
                    self.assertFalse(delivery.cancelled)
                    host._match_automark_unmark(["30", "loss", "15AA", "status", "0",
                                               "E0000000", "", A, "player"])
                    self.drain(client, endpoint)
                    self.assertEqual(endpoint.commands, [])
                    self.assertTrue(delivery.cancelled)
                    self.assertFalse(host._automark_active)
                    self.assertFalse(host._automark_owners)
                    self.assertFalse(host._automark_cleanup)
                    self.assertFalse(host._automark_cooldowns)

    def test_gaze_and_rule_delivery_expiry_is_checked_before_delay_and_post(self):
        for feature in ("gaze", "rule"):
            for boundary in ("queued", "delay", "post"):
                with self.subTest(feature=feature, boundary=boundary), Endpoint() as endpoint:
                    client = self.client(endpoint)
                    host = Host(client) if feature == "gaze" else self.rule_host(client, "ABC")
                    now = time.monotonic()
                    clock = [now]
                    with patch("time.monotonic", side_effect=lambda: clock[0]):
                        if feature == "gaze":
                            host.tell()
                            host.gain(A, "3")
                            host.gain(B, "3")
                        else:
                            host._match_automark_rules(["26", "gain", "ABC", "status", "3",
                                                       "E0000000", "", A, "player"])
                        if boundary == "queued":
                            clock[0] = now + 3.1

                        def expire(_argument):
                            clock[0] = now + 3.1

                        post = client._post

                        def expire_before_post(message):
                            expire(message)
                            post(message)

                        with patch.object(client, "_sleep_command_delay",
                                          side_effect=expire if boundary == "delay" else None), \
                                patch.object(client, "_post",
                                             side_effect=expire_before_post if boundary == "post" else post):
                            while not client._queue.empty():
                                item = client._queue.get_nowait()
                                try:
                                    client._send_queued(item, client._stopping)
                                finally:
                                    client._finish_delivery(item)
                    self.assertEqual(endpoint.commands, [])
                    self.assertTrue(all(not claim.delivery.pending and not claim.delivery.current
                                        for claims in host._automark_deliveries.values() for claim in claims))

    def test_raw_refresh_updates_pending_delivery_without_resetting_its_command_age(self):
        for duration, dispatch_at, expected in ((6, 3.1, ["/mk triangle <1>"]), (1, 3.1, []),
                                                (60, MAX_COMMAND_AGE_S + 1, [])):
            with self.subTest(duration=duration), Endpoint() as endpoint:
                client = self.client(endpoint)
                host = self.rule_host(client, "ABC")
                now = time.monotonic()
                clock = [now]
                with patch("time.monotonic", side_effect=lambda: clock[0]):
                    gain = ["26", "gain", "ABC", "status", "3", "E0000000", "", A, "player"]
                    host._match_automark_rules(gain)
                    clock[0] = now + 2
                    gain[4] = str(duration)
                    host._match_automark_rules(gain)
                    self.assertEqual(len(host._automark_deliveries[A]), 1)
                    self.assertEqual(host._automark_deliveries[A][0].delivery.expires_at, now + 2 + duration)
                    clock[0] = now + dispatch_at
                    while not client._queue.empty():
                        item = client._queue.get_nowait()
                        try:
                            client._send_queued(item, client._stopping)
                        finally:
                            client._finish_delivery(item)
                self.assertEqual(endpoint.commands, expected)

    def test_gaze_and_rule_expiry_preserves_attempted_claims_and_unbounded_cleanup(self):
        for feature in ("gaze", "rule"):
            with self.subTest(feature=feature), Endpoint() as endpoint:
                client = self.client(endpoint)
                host = Host(client) if feature == "gaze" else self.rule_host(client, "ABC")
                now = time.monotonic()
                clock = [now]
                endpoint.stall = True
                try:
                    with patch("time.monotonic", side_effect=lambda: clock[0]):
                        if feature == "gaze":
                            host.tell()
                            host.gain(A, "3")
                            host.gain(B, "3")
                        else:
                            host._match_automark_rules(["26", "gain", "ABC", "status", "3",
                                                       "E0000000", "", A, "player"])
                        client.start()
                        self.assertTrue(endpoint.entered.wait(2))
                        delivery = host._automark_deliveries[A][0].delivery
                        self.assertTrue(delivery.current)
                        clock[0] = now + 4
                        self.assertFalse(client.update_pending_mark_expiry(delivery, None))
                        if feature == "gaze":
                            host._on_umad_gaze_flush()
                        else:
                            host._match_automark_unmark(["30", "loss", "ABC", "status", "0",
                                                       "E0000000", "", A, "player"])
                        self.assertFalse(delivery.cancelled)
                        queued_cleanup = [item for item in list(client._queue.queue) if item[6] is not None]
                        self.assertTrue(queued_cleanup)
                        self.assertTrue(all(item[8] is None for item in queued_cleanup))
                        endpoint.stall = False
                        endpoint.release.set()
                        self.drain(client, endpoint)
                    marker = "ignore1" if feature == "gaze" else "triangle"
                    self.assertEqual(endpoint.commands, [f"/mk {marker} <1>", "/mk clear <1>"])
                    self.assertFalse(delivery.current)
                finally:
                    endpoint.stall = False
                    endpoint.release.set()

    def test_raw_roster_retry_refreshes_its_deadline_when_pending_capacity_is_full(self):
        with Endpoint() as endpoint:
            client = self.client(endpoint, roster=False)
            host = self.rule_host(client, "ABC")
            host._automark_rules = [{"status": f"{0xABC + index:X}", "marker": "circle", "scope": "party"}
                                    for index in range(16)]
            now = time.monotonic()
            clock = [now]
            with patch("time.monotonic", side_effect=lambda: clock[0]):
                for rule in host._automark_rules:
                    host._match_automark_rules(["26", "gain", rule["status"], "status", "3",
                                               "E0000000", "", A, "player"])
                self.assertEqual(len(host._automark_pending), 16)
                clock[0] = now + 2
                host._match_automark_rules(["26", "refresh", "ABC", "status", "6",
                                           "E0000000", "", A, "player"])
                self.assertEqual(len(host._automark_pending), 16)
                self.assertEqual(host._automark_pending[0][3], now)
                self.assertEqual(host._automark_pending[0][6], host._automark_rules[0])
                self.assertEqual(host._automark_pending[0][7], now + 8)
                clock[0] = now + 3.1
                client._update_party_slots(b'{"response":[{"actor":"10000001","order":1}]}')
                host._retry_automark_pending()
                self.assertFalse(host._automark_pending)
                while not client._queue.empty():
                    item = client._queue.get_nowait()
                    try:
                        client._send_queued(item, client._stopping)
                    finally:
                        client._finish_delivery(item)
            self.assertEqual(endpoint.commands, ["/mk circle <1>"])

    def test_expired_roster_retries_do_not_discard_a_fresh_status_at_capacity(self):
        for duration, elapsed in (("3", 3.1), ("0", MAX_COMMAND_AGE_S + 0.1)):
            with self.subTest(duration=duration), Endpoint() as endpoint:
                client = self.client(endpoint, roster=False)
                host = self.rule_host(client, "ABC")
                host._automark_rules = [
                    {"status": f"{0xABC + index:X}", "marker": "circle", "scope": "party"}
                    for index in range(16)]
                host._automark_rules.append({"status": "DEF", "marker": "square", "scope": "party"})
                now = time.monotonic()
                clock = [now]
                with patch("time.monotonic", side_effect=lambda: clock[0]):
                    for rule in host._automark_rules[:16]:
                        host._match_automark_rules(["26", "gain", rule["status"], "status", duration,
                                                   "E0000000", "", A, "player"])
                    self.assertEqual(len(host._automark_pending), 16)
                    clock[0] = now + elapsed
                    host._match_automark_rules(["26", "fresh", "DEF", "status", "60",
                                               "E0000000", "", A, "player"])
                    self.assertEqual(len(host._automark_pending), 1)
                    self.assertEqual(host._automark_pending[0][4], "DEF")
                    self.assertEqual(host._automark_pending[0][3], clock[0])
                    client._update_party_slots(b'{"response":[{"actor":"10000001","order":1}]}')
                    host._retry_automark_pending()
                    while not client._queue.empty():
                        item = client._queue.get_nowait()
                        try:
                            client._send_queued(item, client._stopping)
                        finally:
                            client._finish_delivery(item)
                self.assertEqual(endpoint.commands, ["/mk square <1>"])

    def test_expired_unsent_rule_does_not_block_a_fresh_roster_retry(self):
        for boundary in ("queued status", "finished status", "queued command", "finished command"):
            for identity in ("party", "id", "name"):
                self_target = identity != "party"
                with self.subTest(boundary=boundary, identity=identity), Endpoint() as endpoint:
                    client = self.client(endpoint)
                    host = self.rule_host(client, "ABC")
                    host._me_id = A if identity == "id" else ""
                    host._me_name = "player" if identity == "name" else ""
                    host._automark_rules.append({"status": "DEF", "marker": "square", "scope": "party"})
                    clock = [time.monotonic()]
                    with patch("time.monotonic", side_effect=lambda: clock[0]):
                        duration = "120" if "command" in boundary else "3"
                        host._match_automark_rules(["26", "gain", "ABC", "status", duration,
                                                   "40000001", "boss", A, "player"])
                        expired = host._automark_deliveries[A][-1].delivery
                        clock[0] += MAX_COMMAND_AGE_S + .1 if "command" in boundary else 3.1
                        if boundary.startswith("finished"):
                            self.drain(client, endpoint)
                        self.assertFalse(expired.current)
                        client._update_party_slots(b'{"response":[]}')
                        with patch.object(client, "mark_self", return_value=False) if self_target else nullcontext():
                            host._match_automark_rules(["26", "fresh", "DEF", "status", "60",
                                                       "40000001", "boss", A, "player"])
                        self.assertEqual(len(host._automark_pending), 1)
                        client._update_party_slots(b'{"response":[{"actor":"10000001","order":1}]}')
                        host._retry_automark_pending()
                        self.assertFalse(host._automark_pending)
                        self.drain(client, endpoint)
                    target = "me" if self_target else "1"
                    self.assertEqual(endpoint.commands, [f"/mk square <{target}>"])
                    self.assertEqual(host._automark_active, {"me" if self_target else A: "DEF"})
                    self.assertEqual(host._automark_owners, {A: ("rule", "square")})
                    self.assertEqual(len(host._automark_deliveries[A]), 1)

    def test_expired_rule_replacement_restores_a_delivered_owner_before_retry(self):
        with Endpoint() as endpoint:
            client = self.client(endpoint)
            host = self.rule_host(client, "ABC")
            host._automark_rules.extend({"status": effect, "marker": marker, "scope": "party"}
                                       for effect, marker in (("DEF", "square"), ("123", "circle")))
            clock = [time.monotonic()]
            with patch("time.monotonic", side_effect=lambda: clock[0]):
                self.rule_gain(host, "ABC")
                self.drain(client, endpoint)
                current = host._automark_deliveries[A][-1].delivery
                host._match_automark_rules(["26", "gain", "DEF", "status", "3",
                                           "40000001", "boss", A, "player"])
                expired = host._automark_deliveries[A][-1].delivery
                clock[0] += 3.1
                client._update_party_slots(b'{"response":[]}')
                host._match_automark_rules(["26", "fresh", "123", "status", "60",
                                           "40000001", "boss", A, "player"])
                client._update_party_slots(b'{"response":[{"actor":"10000001","order":1}]}')
                host._retry_automark_pending()
                self.assertFalse(host._automark_pending)
                self.assertTrue(expired.cancelled)
                self.assertTrue(current.current)
                self.assertEqual(host._automark_active, {A: "ABC"})
                self.assertEqual(host._automark_owners, {A: ("rule", "triangle")})
                host._match_automark_unmark(["30", "loss", "ABC", "status", "0",
                                           "40000001", "boss", A, "player"])
                self.drain(client, endpoint)
            self.assertEqual(endpoint.commands, ["/mk triangle <1>", "/mk clear <1>"])
            self.assertFalse(current.current)

    def test_expired_delivered_rule_keeps_its_owner_until_cleanup(self):
        with Endpoint() as endpoint:
            client = self.client(endpoint)
            host = self.rule_host(client, "ABC")
            host._automark_rules.append({"status": "DEF", "marker": "square", "scope": "party"})
            clock = [time.monotonic()]
            with patch("time.monotonic", side_effect=lambda: clock[0]):
                host._match_automark_rules(["26", "gain", "ABC", "status", "3",
                                           "40000001", "boss", A, "player"])
                self.drain(client, endpoint)
                current = host._automark_deliveries[A][-1].delivery
                clock[0] += 3.1
                client._update_party_slots(b'{"response":[]}')
                host._match_automark_rules(["26", "fresh", "DEF", "status", "60",
                                           "40000001", "boss", A, "player"])
                client._update_party_slots(b'{"response":[{"actor":"10000001","order":1}]}')
                host._retry_automark_pending()
                self.assertTrue(current.current)
                self.assertFalse(current.cancelled)
                self.assertEqual(host._automark_active, {A: "ABC"})
                host._match_automark_unmark(["30", "loss", "ABC", "status", "0",
                                           "40000001", "boss", A, "player"])
                self.drain(client, endpoint)
            self.assertEqual(endpoint.commands, ["/mk triangle <1>", "/mk clear <1>"])

    def test_expired_unattempted_rule_releases_cooldown_for_a_fresh_gain(self):
        for boundary in ("queued", "finished"):
            for identity in ("party", "id", "name"):
                self_target = identity != "party"
                with self.subTest(boundary=boundary, identity=identity), Endpoint() as endpoint:
                    client = self.client(endpoint)
                    host = self.rule_host(client, "ABC")
                    host._me_id = A if identity == "id" else ""
                    host._me_name = "player" if identity == "name" else ""
                    clock = [time.monotonic()]
                    with patch("time.monotonic", side_effect=lambda: clock[0]):
                        fields = ["26", "gain", "ABC", "status", "1",
                                  "40000001", "boss", A, "player"]
                        host._match_automark_rules(fields)
                        expired = host._automark_deliveries[A][-1].delivery
                        clock[0] += 1.1
                        if boundary == "finished":
                            self.drain(client, endpoint)
                        self.assertFalse(expired.attempted)
                        fields[4] = "60"
                        host._match_automark_rules(fields)
                        self.drain(client, endpoint)
                        self.assertTrue(host._automark_deliveries[A][-1].delivery.attempted)
                        host._match_automark_rules(fields)
                        self.drain(client, endpoint)
                    target = "me" if self_target else "1"
                    self.assertEqual(endpoint.commands, [f"/mk triangle <{target}>"])

    def test_attempted_rule_retains_its_cooldown_after_http_failure(self):
        with Endpoint() as endpoint:
            endpoint.code = 400
            client = self.client(endpoint)
            host = self.rule_host(client, "ABC")
            clock = [time.monotonic()]
            with patch("time.monotonic", side_effect=lambda: clock[0]):
                fields = ["26", "gain", "ABC", "status", "1",
                          "40000001", "boss", A, "player"]
                host._match_automark_rules(fields)
                delivery = host._automark_deliveries[A][-1].delivery
                self.drain(client, endpoint)
                self.assertTrue(delivery.attempted)
                self.assertFalse(delivery.current)
                clock[0] += 1.1
                fields[4] = "60"
                host._match_automark_rules(fields)
                self.drain(client, endpoint)
            self.assertEqual(endpoint.commands, ["/mk triangle <1>"])

    def test_worker_rejection_before_rule_claim_does_not_block_a_fresh_retry(self):
        for boundary in ("gain", "roster retry"):
            with self.subTest(boundary=boundary), Endpoint() as endpoint:
                endpoint.code = 400
                client = self.client(endpoint, roster=boundary == "gain")
                host = self.rule_host(client, "ABC")
                host._automark_cleanup = set()
                fields = ["26", "gain", "ABC", "status", "60",
                          "40000001", "boss", A, "player"]
                if boundary == "roster retry":
                    host._match_automark_rules(fields)
                    client._update_party_slots(b'{"response":[{"actor":"10000001","order":1}]}')
                client.start()
                original = AutomarkersTabMixin._claim_automark
                deliveries = []

                def claim_after_response(window, actor, marker, owner, delivery=None, status=None, **kwargs):
                    self.assertTrue(wait_for(lambda: not delivery.pending))
                    deliveries.append(delivery)
                    original(window, actor, marker, owner, delivery, status, **kwargs)

                with patch.object(AutomarkersTabMixin, "_claim_automark", claim_after_response):
                    if boundary == "gain":
                        host._match_automark_rules(fields)
                    else:
                        host._retry_automark_pending()
                self.assertTrue(deliveries[0].attempted)
                self.assertFalse(deliveries[0].current)
                self.assertEqual(host._automark_active, {})
                self.assertEqual(host._automark_owners, {})
                self.assertEqual(host._automark_cleanup, set())
                host._match_automark_rules(fields)
                self.drain(client, endpoint)
                self.assertEqual(endpoint.commands, ["/mk triangle <1>"])
                endpoint.code = 200
                client._update_party_slots(b'{"response":[]}')
                host._automark_rules.append({"status": "DEF", "marker": "circle", "scope": "party"})
                fields[2] = "DEF"
                host._match_automark_rules(fields)
                self.assertEqual(len(host._automark_pending), 1)
                client._update_party_slots(b'{"response":[{"actor":"10000001","order":1}]}')
                host._retry_automark_pending()
                self.drain(client, endpoint)
                self.assertEqual(endpoint.commands, ["/mk triangle <1>", "/mk circle <1>"])
                self.assertEqual(host._automark_active, {A: "DEF"})

    def test_worker_rejection_before_replacement_claim_restores_the_delivered_owner(self):
        for boundary in ("mechanic", "gain", "roster retry"):
            for self_target in (False, True):
                with self.subTest(boundary=boundary, self_target=self_target), Endpoint() as endpoint:
                    client = self.client(endpoint)
                    host = self.rule_host(client, "ABC")
                    host._me_id = A if self_target else ""
                    self.rule_gain(host, "ABC")
                    self.drain(client, endpoint)
                    current = host._automark_deliveries[A][-1].delivery
                    target = "me" if self_target else "1"
                    endpoint.command_codes[f"/mk circle <{target}>"] = 400
                    host._automark_rules.append({"status": "DEF", "marker": "circle", "scope": "party"})
                    fields = ["26", "gain", "DEF", "status", "60",
                              "40000001", "boss", A, "player"]
                    if boundary == "roster retry":
                        with patch.object(host, "_mark_player", return_value=False):
                            host._match_automark_rules(fields)
                        host._automark_active.clear()
                    original = AutomarkersTabMixin._claim_automark

                    def claim_after_response(window, actor, marker, owner, delivery=None, status=None, **kwargs):
                        self.assertTrue(wait_for(lambda: not delivery.pending))
                        self.assertTrue(delivery.attempted)
                        self.assertFalse(delivery.current)
                        original(window, actor, marker, owner, delivery, status, **kwargs)

                    with patch.object(AutomarkersTabMixin, "_claim_automark", claim_after_response):
                        if boundary == "mechanic":
                            self.assertEqual(host._dispatch_mark_actions(
                                [("mark", A, "circle")], [], owner="gaze"), [])
                        elif boundary == "gain":
                            host._match_automark_rules(fields)
                        else:
                            host._retry_automark_pending()
                    self.assertEqual(host._automark_active, {"me" if self_target else A: "ABC"})
                    self.assertEqual(host._automark_owners, {A: ("rule", "triangle")})
                    self.assertEqual([claim.delivery for claim in host._automark_deliveries[A]], [current])
                    host._match_automark_unmark(["30", "loss", "ABC", "status", "0",
                                               "40000001", "boss", A, "player"])
                    self.drain(client, endpoint)
                    self.assertEqual(endpoint.commands, [f"/mk triangle <{target}>",
                                                         f"/mk circle <{target}>", f"/mk clear <{target}>"])
                    self.assertFalse(current.current)

    def test_successful_roster_retry_starts_the_rule_cooldown(self):
        with Endpoint() as endpoint:
            client = self.client(endpoint, roster=False)
            host = self.rule_host(client, "ABC")
            clock = [time.monotonic()]
            with patch("time.monotonic", side_effect=lambda: clock[0]):
                fields = ["26", "gain", "ABC", "status", "60",
                          "40000001", "boss", A, "player"]
                host._match_automark_rules(fields)
                host._retry_automark_pending()
                self.assertEqual(len(host._automark_pending), 1)
                self.assertFalse(host._automark_cooldowns)
                clock[0] += 5
                client._update_party_slots(b'{"response":[{"actor":"10000001","order":1}]}')
                host._retry_automark_pending()
                self.assertFalse(host._automark_pending)
                self.assertEqual(host._automark_cooldowns, {("ABC", A, "triangle"): clock[0]})
                retried_at = clock[0]
                clock[0] = retried_at + .1
                host._match_automark_rules(fields)
                self.drain(client, endpoint)
                self.assertEqual(endpoint.commands, ["/mk triangle <1>"])
                clock[0] = retried_at + 3
                host._match_automark_rules(fields)
                self.drain(client, endpoint)
            self.assertEqual(endpoint.commands, ["/mk triangle <1>", "/mk triangle <1>"])

    def test_older_expired_command_cannot_release_a_newer_attempted_cooldown(self):
        with Endpoint() as endpoint:
            endpoint.code = 400
            client = self.client(endpoint)
            host = self.rule_host(client, "ABC")
            clock = [1000.0]
            with patch("time.monotonic", side_effect=lambda: clock[0]):
                fields = ["26", "gain", "ABC", "status", "120",
                          "40000001", "boss", A, "player"]
                host._match_automark_rules(fields)
                old = host._automark_deliveries[A][-1].delivery
                clock[0] = 1029.0
                host._match_automark_rules(fields)
                newer = host._automark_deliveries[A][-1].delivery
                self.assertEqual(old.command_expires_at, 1030)
                self.assertEqual(newer.command_expires_at, 1059)
                clock[0] = 1030.1
                self.drain(client, endpoint)
                self.assertFalse(old.attempted)
                self.assertTrue(newer.attempted)
                self.assertFalse(newer.current)
                clock[0] = 1030.2
                host._match_automark_rules(fields)
                self.drain(client, endpoint)
                self.assertEqual(host._automark_cooldowns, {("ABC", A, "triangle"): 1029})
            self.assertEqual(endpoint.commands, ["/mk triangle <1>"])

    def test_newer_unattempted_expiry_releases_its_own_cooldown(self):
        with Endpoint() as endpoint:
            client = self.client(endpoint)
            host = self.rule_host(client, "ABC")
            clock = [1000.0]
            with patch("time.monotonic", side_effect=lambda: clock[0]):
                fields = ["26", "gain", "ABC", "status", "120",
                          "40000001", "boss", A, "player"]
                host._match_automark_rules(fields)
                self.drain(client, endpoint)
                old = host._automark_deliveries[A][-1].delivery
                clock[0] = 1004.0
                fields[4] = "1"
                host._match_automark_rules(fields)
                newer = host._automark_deliveries[A][-1].delivery
                clock[0] = 1005.1
                self.drain(client, endpoint)
                self.assertTrue(old.attempted)
                self.assertTrue(old.current)
                self.assertFalse(newer.attempted)
                clock[0] = 1005.2
                fields[4] = "60"
                host._match_automark_rules(fields)
                self.drain(client, endpoint)
                self.assertEqual(host._automark_cooldowns, {("ABC", A, "triangle"): 1005.2})
            self.assertEqual(endpoint.commands, ["/mk triangle <1>", "/mk triangle <1>"])

    def test_older_http_failure_cannot_keep_a_newer_unattempted_cooldown(self):
        with Endpoint() as endpoint:
            endpoint.stall = True
            endpoint.code = 400
            client = self.client(endpoint, timeout=10)
            host = self.rule_host(client, "ABC")
            clock = [1000.0]
            try:
                with patch("time.monotonic", side_effect=lambda: clock[0]):
                    fields = ["26", "gain", "ABC", "status", "120",
                              "40000001", "boss", A, "player"]
                    host._match_automark_rules(fields)
                    old = host._automark_deliveries[A][-1].delivery
                    client.start()
                    self.assertTrue(endpoint.entered.wait(2))
                    clock[0] = 1004.0
                    fields[4] = "1"
                    host._match_automark_rules(fields)
                    newer = host._automark_deliveries[A][-1].delivery
                    clock[0] = 1005.1
                    endpoint.stall = False
                    endpoint.release.set()
                    self.drain(client, endpoint)
                    self.assertTrue(old.attempted)
                    self.assertFalse(old.current)
                    self.assertFalse(newer.attempted)
                    clock[0] = 1005.2
                    fields[4] = "60"
                    host._match_automark_rules(fields)
                    self.drain(client, endpoint)
                    self.assertEqual(host._automark_cooldowns, {("ABC", A, "triangle"): 1005.2})
                self.assertEqual(endpoint.commands, ["/mk triangle <1>", "/mk triangle <1>"])
            finally:
                endpoint.stall = False
                endpoint.release.set()

    def test_status_expiry_interrupts_a_stalled_reply_without_retiring_cleanup(self):
        with Endpoint() as endpoint:
            client = self.client(endpoint, timeout=4)
            host = self.rule_host(client, "ABC")
            clock = [time.monotonic()]
            finished = threading.Event()
            finish = client._finish_delivery
            endpoint.stall = True
            try:
                with patch("time.monotonic", side_effect=lambda: clock[0]):
                    host._match_automark_rules(["26", "gain", "ABC", "status", "3",
                                               "E0000000", "", A, "player"])
                    delivery = host._automark_deliveries[A][0].delivery

                    def finish_marker(item):
                        finish(item)
                        if item[-1] is delivery and item[6] is None:
                            finished.set()

                    with patch.object(client, "_finish_delivery", side_effect=finish_marker):
                        client.start()
                        self.assertTrue(endpoint.entered.wait(2))
                        clock[0] += 4
                        self.assertTrue(finished.wait(1))
                        self.assertFalse(delivery.pending)
                        self.assertTrue(delivery.current)
                        host._match_automark_unmark(["30", "loss", "ABC", "status", "0",
                                                   "E0000000", "", A, "player"])
                        endpoint.stall = False
                        endpoint.release.set()
                        self.drain(client, endpoint)
                self.assertEqual(endpoint.commands, ["/mk triangle <1>", "/mk clear <1>"])
                self.assertFalse(delivery.current)
            finally:
                endpoint.stall = False
                endpoint.release.set()

    def test_raw_loss_with_auto_cleanse_disabled_preserves_earlier_delivered_owner(self):
        with Endpoint() as endpoint:
            client = self.client(endpoint)
            host = self.rule_host(client, "15A9")
            host._settings["telesto_uri"] = endpoint.url
            host._update_automark_status_label = lambda: None
            host._automark_clear_on_loss = False
            self.rule_gain(host, "15A9")
            self.drain(client, endpoint)
            host._automark_rules.append({"fight": "UMAD", "status": "15AA",
                                        "marker": "circle", "scope": "any"})
            self.rule_gain(host, "15AA")
            host._match_automark_unmark(["30", "loss", "15AA", "status", "0",
                                       "E0000000", "", A, "player"])
            self.drain(client, endpoint)
            self.assertEqual(endpoint.commands, ["/mk triangle <1>"])
            self.assertEqual(host._automark_active, {A: "15A9"})
            self.assertEqual(host._automark_owners, {A: ("rule", "triangle")})
            host._match_automark_unmark(["30", "loss", "15A9", "status", "0",
                                       "E0000000", "", A, "player"])
            self.drain(client, endpoint)
            self.assertEqual(endpoint.commands, ["/mk triangle <1>"])
            host._settings["telesto_enabled"] = False
            host._apply_automark_state()
            self.drain(client, endpoint)
            self.assertEqual(endpoint.commands, ["/mk triangle <1>", "/mk clear <1>"])

    def test_raw_loss_with_auto_cleanse_disabled_keeps_an_attempted_mark(self):
        with Endpoint() as endpoint:
            client = self.client(endpoint)
            host = self.rule_host(client, "15AA")
            host._settings["telesto_uri"] = endpoint.url
            host._update_automark_status_label = lambda: None
            host._automark_clear_on_loss = False
            endpoint.stall = True
            try:
                self.rule_gain(host, "15AA")
                client.start()
                self.assertTrue(endpoint.entered.wait(2))
                delivery = host._automark_deliveries[A][-1].delivery
                host._match_automark_unmark(["30", "loss", "15AA", "status", "0",
                                           "E0000000", "", A, "player"])
                self.assertFalse(delivery.cancelled)
                endpoint.stall = False
                endpoint.release.set()
                self.drain(client, endpoint)
                self.assertEqual(endpoint.commands, ["/mk triangle <1>"])
                self.assertTrue(delivery.current)
                host._settings["telesto_enabled"] = False
                host._apply_automark_state()
                self.drain(client, endpoint)
                self.assertEqual(endpoint.commands, ["/mk triangle <1>", "/mk clear <1>"])
            finally:
                endpoint.stall = False
                endpoint.release.set()

    def test_grand_cross_cleanup_keeps_an_ambiguous_attempt(self):
        with Endpoint() as endpoint:
            client = self.client(endpoint, timeout=.15)
            host, status, actors = self.grand_cross_host(client, "lightning")
            endpoint.stall = True
            try:
                self.grand_cross_wave(host, status, actors)
                client.start()
                self.assertTrue(endpoint.entered.wait(2))
                delivery = host._automark_deliveries[A][0].delivery
                self.assertTrue(delivery.current)
                host._umad_grand_cross_line(["30", "loss", status, "status", "0",
                                            "40000001", "boss", A, "player"])
                self.assertFalse(delivery.cancelled)
                self.assertTrue(wait_for(lambda: not delivery.pending))
                endpoint.stall = False
                endpoint.release.set()
                self.drain(client, endpoint)
                self.assertEqual(endpoint.commands, ["/mk attack1 <1>", "/mk attack2 <2>",
                                                     "/mk clear <1>"])
                self.assertFalse(delivery.current)
            finally:
                endpoint.stall = False
                endpoint.release.set()

    def test_pending_cancel_between_delay_and_post_never_reaches_endpoint(self):
        with Endpoint() as endpoint:
            client = self.client(endpoint)
            delivery = MarkerDelivery(int(A, 16), "attack1")
            post = client._post

            def cancel_before_post(message):
                if message["type"] == "ExecuteCommand":
                    self.assertTrue(client.cancel_pending_mark(delivery))
                post(message)

            with patch.object(client, "_post", side_effect=cancel_before_post):
                self.assertTrue(client.mark_actor(A, "attack1", delivery=delivery))
                self.drain(client, endpoint)
            self.assertEqual(endpoint.commands, [])
            self.assertTrue(delivery.cancelled)
            self.assertFalse(delivery.pending)
            self.assertFalse(delivery.current)

    def test_chain_and_gaze_reset_cancel_only_unattempted_deliveries(self):
        for owner, method in (("chains", "chain"), ("gaze", "gaze")):
            for boundary in ("reset", "other duty"):
                for state in ("queued", "attempted", "delivered"):
                    with self.subTest(owner=owner, boundary=boundary, state=state), Endpoint() as endpoint:
                        client = self.client(endpoint)
                        host = Host(client)
                        host._settings["telesto_uri"] = endpoint.url
                        host._update_automark_status_label = lambda: None
                        host._current_fight_tag = ""
                        dispatch = getattr(host, f"_dispatch_umad_{method}_actions")
                        endpoint.stall = state == "attempted"
                        try:
                            dispatch([("mark", A, "triangle")])
                            delivery = host._automark_deliveries[A][-1].delivery
                            if state == "attempted":
                                client.start()
                                self.assertTrue(endpoint.entered.wait(2))
                            elif state == "delivered":
                                self.drain(client, endpoint)
                            if boundary == "other duty":
                                host._current_fight_tag = "FRU"
                                dispatch([])
                            else:
                                getattr(host, f"_umad_{method}_reset")()
                            self.assertEqual(delivery.cancelled, state == "queued")
                            endpoint.stall = False
                            endpoint.release.set()
                            self.drain(client, endpoint)
                            expected = [] if state == "queued" else ["/mk triangle <1>"]
                            self.assertEqual(endpoint.commands, expected)
                            host._settings["telesto_enabled"] = False
                            host._apply_automark_state()
                            self.drain(client, endpoint)
                            self.assertEqual(endpoint.commands,
                                             expected + (["/mk clear <1>"] if expected else []))
                        finally:
                            endpoint.stall = False
                            endpoint.release.set()

    def test_repeated_wipes_clear_rule_marks_once_before_new_pull_marks(self):
        with Endpoint() as endpoint:
            client = self.client(endpoint)
            host = OverlayHost([1000.0])
            host.prepare_zone()
            host._telesto_client = client
            host._clear_player = lambda actor: client.clear_actor(actor)
            host._automark_active = {"me": "15A8", B: "15A9"}
            host._automark_owners = {A: ("rule", "triangle"), B: ("rule", "circle")}
            host._automark_cooldowns = {("15A8", "me", "triangle"): time.monotonic()}
            client.mark_self("triangle")
            client.mark_actor(B, "circle")
            self.drain(client, endpoint)
            host.wipe()
            host.wipe()
            client.mark_actor(C, "cross")
            self.drain(client, endpoint)
            self.assertEqual(endpoint.commands, ["/mk triangle <me>", "/mk circle <2>",
                                                 "/mk clear <me>", "/mk clear <2>",
                                                 "/mk cross <3>"])
            self.assertFalse(host._automark_owners)
            self.assertFalse(host._automark_cooldowns)

    def test_cancelled_sign_transfer_keeps_original_holder_cleanup(self):
        for boundary in ("disable", "wipe"):
            with self.subTest(boundary=boundary), Endpoint() as endpoint:
                client = self.client(endpoint)
                host = self.rule_host(client, "15A8")
                host._settings["telesto_uri"] = endpoint.url
                host._update_automark_status_label = lambda: None
                host._automark_rules.append({"fight": "UMAD", "status": "15A9",
                                             "marker": "triangle", "scope": "any"})
                self.rule_gain(host, "15A8")
                self.drain(client, endpoint)
                entered, release = threading.Event(), threading.Event()

                def delay(_stopping):
                    entered.set()
                    release.wait(3)

                try:
                    with patch.object(client, "_sleep_command_delay", side_effect=delay):
                        host._match_automark_rules(["26", "ts", "15A9", "status", "60",
                                                   "E0000000", "", B, "player"])
                        self.assertTrue(entered.wait(2))
                        if boundary == "disable":
                            host._settings["telesto_enabled"] = False
                            host._apply_automark_state()
                            host._apply_automark_state()
                        else:
                            wipe = OverlayHost([1000.0])
                            wipe.prepare_zone()
                            wipe._telesto_client = client
                            wipe._clear_player = host._clear_player
                            wipe._automark_active = host._automark_active
                            wipe._automark_owners = host._automark_owners
                            wipe._automark_deliveries = host._automark_deliveries
                            wipe._automark_cleanup = getattr(host, "_automark_cleanup", set())
                            wipe.wipe()
                            wipe.wipe()
                        release.set()
                        self.drain(client, endpoint)
                    self.assertEqual(endpoint.commands,
                                     ["/mk triangle <1>", "/mk clear <1>"])
                    self.assertFalse(host._automark_owners)
                    self.assertFalse(host._automark_active)
                    self.assertFalse(getattr(host, "_automark_cleanup", set()))
                finally:
                    release.set()

    def test_completed_cleanup_is_not_repeated_on_disable(self):
        for owner in ("rule", "chains", "gaze"):
            with self.subTest(owner=owner), Endpoint() as endpoint:
                client = self.client(endpoint)
                host = self.rule_host(client, "15A8")
                host._settings["telesto_uri"] = endpoint.url
                host._update_automark_status_label = lambda: None
                if owner == "rule":
                    self.rule_gain(host, "15A8")
                else:
                    dispatch = (host._dispatch_umad_chain_actions if owner == "chains"
                                else host._dispatch_umad_gaze_actions)
                    dispatch([("mark", A, "triangle")])
                self.drain(client, endpoint)
                if owner == "rule":
                    host._match_automark_unmark(["30", "ts", "15A8", "status", "0",
                                               "E0000000", "", A, "player"])
                else:
                    dispatch([("clear", A)])
                self.drain(client, endpoint)
                host._settings["telesto_enabled"] = False
                host._apply_automark_state()
                self.drain(client, endpoint)
                self.assertEqual(endpoint.commands, ["/mk triangle <1>", "/mk clear <1>"])

    def test_queued_cleanup_survives_disable_after_ownership_is_released(self):
        for owner in ("rule", "chains", "gaze"):
            with self.subTest(owner=owner), Endpoint() as endpoint:
                client = self.client(endpoint)
                host = self.rule_host(client, "15A8")
                host._settings["telesto_uri"] = endpoint.url
                host._update_automark_status_label = lambda: None
                if owner == "rule":
                    self.rule_gain(host, "15A8")
                else:
                    dispatch = (host._dispatch_umad_chain_actions if owner == "chains"
                                else host._dispatch_umad_gaze_actions)
                    dispatch([("mark", A, "triangle")])
                self.drain(client, endpoint)
                entered, release = threading.Event(), threading.Event()

                def delay(_stopping):
                    entered.set()
                    release.wait(3)

                try:
                    with patch.object(client, "_sleep_command_delay", side_effect=delay):
                        if owner == "rule":
                            host._match_automark_unmark(["30", "ts", "15A8", "status", "0",
                                                       "E0000000", "", A, "player"])
                        else:
                            dispatch([("clear", A)])
                        self.assertTrue(entered.wait(2))
                        self.assertFalse(host._automark_owners)
                        self.assertFalse(getattr(host, "_automark_cleanup", set()))
                        host._settings["telesto_enabled"] = False
                        host._apply_automark_state()
                        release.set()
                        self.drain(client, endpoint)
                    self.assertEqual(endpoint.commands, ["/mk triangle <1>", "/mk clear <1>"])
                finally:
                    release.set()

    def test_actor_reset_discards_cleanup_targets_before_new_party(self):
        with Endpoint() as endpoint:
            client = self.client(endpoint)
            host = self.rule_host(client, "15A8")
            host._settings["telesto_uri"] = endpoint.url
            host._update_automark_status_label = lambda: None
            host._actor_jobs = {}
            self.rule_gain(host, "15A8")
            self.drain(client, endpoint)
            host._clear_actor_state()
            client._update_party_slots(b'{"response":[{"actor":"10000002","order":1},'
                                      b'{"actor":"10000001","order":2}]}')
            host._settings["telesto_enabled"] = False
            host._apply_automark_state()
            self.drain(client, endpoint)
            self.assertEqual(endpoint.commands, ["/mk triangle <1>"])

    def test_queued_cleanup_is_cancelled_before_party_or_endpoint_replacement(self):
        for boundary in ("party", "endpoint"):
            with self.subTest(boundary=boundary), Endpoint() as original, Endpoint() as replacement:
                client = self.client(original)
                client.mark_actor(A, "triangle")
                self.drain(client, original)
                entered, release = threading.Event(), threading.Event()

                def delay(_stopping):
                    entered.set()
                    release.wait(3)

                try:
                    with patch.object(client, "_sleep_command_delay", side_effect=delay):
                        self.assertTrue(client.clear_actor(A))
                        self.assertTrue(entered.wait(2))
                        if boundary == "party":
                            client.cancel_pending(clear_party=True)
                            destination = original
                        else:
                            client.configure(uri=replacement.url)
                            destination = replacement
                        client._update_party_slots(b'{"response":[{"actor":"10000002","order":1},'
                                                  b'{"actor":"10000001","order":2}]}')
                        client.mark_actor(B, "circle")
                        release.set()
                        self.drain(client, destination)
                    self.assertEqual(original.commands, ["/mk triangle <1>"] +
                                     (["/mk circle <1>"] if boundary == "party" else []))
                    self.assertEqual(replacement.commands,
                                     ["/mk circle <1>"] if boundary == "endpoint" else [])
                finally:
                    release.set()

    def test_rejected_request_is_degraded_and_is_not_retried(self):
        with Endpoint() as endpoint:
            endpoint.code = 503
            client = self.client(endpoint)
            client.mark_actor(A, "attack1")
            self.drain(client, endpoint)
            self.assertTrue(wait_for(lambda: client.last_status() == (True, True)))
            self.assertEqual(endpoint.commands, ["/mk attack1 <1>"])

    def test_ambiguous_timeout_is_not_retried(self):
        with Endpoint() as endpoint:
            endpoint.stall = True
            client = self.client(endpoint, timeout=.15)
            client.start()
            client.mark_actor(A, "attack1")
            self.assertTrue(endpoint.entered.wait(2))
            self.assertTrue(wait_for(lambda: client.last_status() == (False, False)))
            endpoint.stall = False
            endpoint.release.set()
            self.drain(client, endpoint)
            self.assertEqual(endpoint.commands, ["/mk attack1 <1>"])

    def test_disabled_and_full_queue_requests_never_reach_endpoint(self):
        with Endpoint() as endpoint:
            client = self.client(endpoint, max_queue=1)
            client.set_enabled(False)
            self.assertFalse(client.mark_actor(A, "attack1"))
            client.set_enabled(True)
            self.assertTrue(client.mark_actor(B, "attack2"))
            self.assertFalse(client.mark_actor(A, "attack1"))
            client.start()
            self.assertTrue(wait_for(lambda: endpoint.commands == ["/mk attack2 <2>"]))

    def test_invalid_rule_marker_cannot_compete_with_native_umad(self):
        with Endpoint() as endpoint:
            client = self.client(endpoint)
            host = self.rule_host(client, "15A7")
            host._automark_rules[0]["marker"] = "attcak1"
            self.assertFalse(host._native_umad_owned_locally())
            self.rule_gain(host, "15A7")
            self.drain(client, endpoint)
            self.assertEqual(endpoint.commands, [])
            self.assertFalse(host._automark_pending)
            self.assertFalse(host._automark_owners)


class NativeUmadOwnershipTests(unittest.TestCase):
    def host(self, *, gaze=False, rules=()):
        host = AutomarkersTabMixin()
        host._settings = {"telesto_enabled": True, "telesto_uri": "http://127.0.0.1:45678/",
                          "umad_gaze_enabled": gaze}
        host._automark_rules = list(rules)
        host._automark_pending = []
        host.commands = []
        host._triggevent = SimpleNamespace(set_automark=lambda *args, **kwargs:
                                           host.commands.append((args, kwargs)))
        host._save_settings = lambda: None
        host._umad_gaze_reset = lambda **kwargs: None
        return host

    def test_local_umad_producers_select_one_native_pack_owner(self):
        cases = [
            (False, {}, True),
            (True, {}, False),
            (False, {"fight": "UMAD", "status": "15A7", "marker": ""}, True),
            (False, {"fight": "UMAD", "status": "15A7", "marker": "circle", "enabled": False}, True),
            (False, {"fight": "FRU", "status": "15A7", "marker": "circle"}, True),
            (False, {"fight": "UMAD", "status": "130C", "marker": "attack1"}, True),
            (False, {"fight": "UMAD", "status": "644+BBC", "marker": "attack1"}, True),
            (False, {"fight": "UMAD", "status": "15A7", "marker": "circle"}, False),
            (False, {"fight": "umad", "status": " 0x0015a8 ", "marker": "circle"}, False),
            (False, {"fight": "UMAD", "status": "0x15a9+0015aa", "marker": "circle"}, False),
            (False, {"fight": "UMAD", "status": "Cursed Shriek", "marker": "circle"}, False),
            (False, {"fight": "UMAD", "status": "forked lightning", "marker": "circle"}, False),
            (False, {"fight": "UMAD", "status": " Compressed Water ", "marker": "circle"}, False),
            (False, {"fight": "UMAD", "status": "White Wound", "marker": "circle"}, False),
            (False, {"fight": "UMAD", "status": "15AA", "marker": "circle"}, False),
            (False, {"fight": "UMAD", "status": "0566+1C6", "marker": "circle"}, False),
            (False, {"fight": "", "status": "15A9", "marker": "circle"}, False),
            (False, {"fight": "", "status": "Cursed Shriek", "marker": "ignore1"}, False),
            (False, {"fight": "UMAD", "status": "Cursed", "marker": "circle"}, True),
            (False, {"fight": "UMAD", "status": "15A7+644+BBC", "marker": "circle"}, True),
            (False, {"fight": "UMAD", "status": "15A7", "marker": "invalid"}, True),
            (False, {"fight": "UMAD", "status": "", "marker": "circle"}, True),
        ]
        for gaze, rule, native in cases:
            with self.subTest(gaze=gaze, rule=rule):
                host = self.host(gaze=gaze, rules=[rule] if rule else [])
                host._apply_native_automark_state()
                expected = [((True, host._settings["telesto_uri"]), {"native_umad": native})]
                if not native:
                    expected.insert(0, ((None,), {"native_umad": False}))
                self.assertEqual(host.commands, expected)

    def test_gaze_toggles_and_rule_edits_replay_native_owner_without_resetting_local_marks(self):
        host = self.host()
        host._automark_active = {A: "15A7"}
        host._on_umad_gaze_toggled(True)
        self.assertFalse(host.commands[-1][1]["native_umad"])
        self.assertEqual(host._automark_active, {A: "15A7"})
        host._on_umad_gaze_toggled(False)
        self.assertTrue(host.commands[-1][1]["native_umad"])
        host._automark_rules = [{"fight": "UMAD", "status": "15A9", "marker": "triangle"}]
        host._cancel_changed_rule_marks()
        self.assertFalse(host.commands[-1][1]["native_umad"])
        host._automark_rules = []
        host._cancel_changed_rule_marks()
        self.assertTrue(host.commands[-1][1]["native_umad"])
        self.assertEqual(host._automark_active, {A: "15A7"})


if __name__ == "__main__":
    unittest.main()
