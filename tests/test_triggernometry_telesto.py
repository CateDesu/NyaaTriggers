from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import queue
import threading
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

from nyaatriggers.telesto_client import read_telesto_response
from nyaatriggers.triggernometry_telesto import TriggernometryTelesto, _Server


def post(url, message):
    request = urllib.request.Request(url, json.dumps(message).encode(),
                                     {"Content-Type": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=5) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        with exc:
            return exc.code, exc.read()


class FakeTelesto:
    def __enter__(self):
        self.messages = queue.Queue()
        self.subscriptions = {}
        self.drawings = {}
        self.fail_unsubscribe = False
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                message = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if owner.fail_unsubscribe and message["type"] == "Unsubscribe":
                    self.send_response(503)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                owner.apply(message)
                body = b'{"version":1,"id":0,"response":null}'
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/"
        return self

    def __exit__(self, *_args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def apply(self, message):
        kind = message["type"]
        payload = message.get("payload", {})
        if kind == "Bundle":
            for item in payload:
                self.apply(item)
            return
        if kind == "Subscribe":
            self.subscriptions[payload["id"]] = payload
        elif kind == "Unsubscribe":
            self.subscriptions.pop(payload["id"], None)
        elif kind == "EnableDoodle":
            self.drawings[payload["name"]] = payload
        elif kind == "DisableDoodle":
            self.drawings.pop(payload["name"], None)
        self.messages.put(message)

    def next(self, kind, timeout=10):
        while True:
            message = self.messages.get(timeout=timeout)
            if message["type"] == kind:
                return message


def envelope(kind, **payload):
    return {"version": 1, "id": 123456, "type": kind, "payload": payload}


class TelestoRelayTests(unittest.TestCase):
    def test_failed_deadline_worker_reports_io_error_without_opening_a_request(self):
        request = urllib.request.Request("http://127.0.0.1:1/", data=b"{}")
        with patch.object(threading.Thread, "start", side_effect=RuntimeError("cannot start new thread")), \
                patch.object(urllib.request.OpenerDirector, "open") as opened:
            with self.assertRaises(OSError) as error:
                read_telesto_response(request, 1, threading.Event(), use_proxy=False)
        self.assertIsInstance(error.exception.__cause__, RuntimeError)
        opened.assert_not_called()

    def test_global_worker_exhaustion_still_finishes_local_relay_cleanup(self):
        errors = []
        with FakeTelesto() as peer:
            relay = TriggernometryTelesto(lambda _body: None, errors.append, peer.url)
            relay.start()
            try:
                self.assertEqual(post(relay.url, envelope("Subscribe", id="memory", type="memory"))[0], 200)
                before = dict(peer.subscriptions)
                with patch.object(threading.Thread, "start", side_effect=RuntimeError("cannot start new thread")), \
                        patch.object(urllib.request.OpenerDirector, "open") as opened:
                    relay.close(wait=True)
                    self.assertTrue(relay.is_finished())
                    relay.close(wait=True)
                opened.assert_not_called()
                self.assertEqual(relay._server.socket.fileno(), -1)
                self.assertFalse(relay._owned_subscriptions)
                self.assertEqual(peer.subscriptions, before)
                self.assertTrue(any("Telesto deadline worker could not start" in error for error in errors))
            finally:
                relay._finish()

    def test_failed_listener_worker_reports_bridge_startup_failure_without_spawning(self):
        from nyaatriggers import triggernometry_bridge as bridge_module

        bridge = bridge_module.TriggernometryBridge()
        statuses = []
        bridge.status.connect(lambda *state: statuses.append(state))
        with patch.object(bridge_module, "_find_exe", return_value=Path(__file__)), \
                patch.object(bridge_module, "has_mono", return_value=True), \
                patch.object(bridge_module, "_find_packs", return_value=[]), \
                patch.object(bridge_module, "_make_bundled_mono_executable"), \
                patch.object(bridge_module, "_log"), \
                patch.object(bridge, "_launch_cmd", return_value=["simulated-sidecar"]), \
                patch.object(bridge_module.subprocess, "Popen") as spawn, \
                patch.object(threading.Thread, "start", side_effect=RuntimeError("cannot start new thread")):
            bridge.start()
        spawn.assert_not_called()
        self.assertFalse(bridge.is_active())
        self.assertEqual(bridge.generation(), 0)
        self.assertEqual(statuses, [(False,
            "Telesto callback listener failed: Telesto callback worker could not start", 0)])

    def test_failed_listener_worker_releases_its_socket_and_can_close(self):
        relay = TriggernometryTelesto(lambda _body: None, self.fail)
        servers = []

        def create_server(owner):
            server = _Server(owner)
            servers.append(server)
            self.addCleanup(server.server_close)
            return server

        with patch("nyaatriggers.triggernometry_telesto._Server", side_effect=create_server), \
                patch.object(threading.Thread, "start", side_effect=RuntimeError("cannot start new thread")):
            with self.assertRaises(OSError) as error:
                relay.start()
        self.assertIsInstance(error.exception.__cause__, RuntimeError)
        self.assertEqual(servers[0].socket.fileno(), -1)
        self.assertIsNone(relay._server)
        relay.close(wait=True)
        self.assertTrue(relay.is_finished())
        relay.close(wait=True)

    def test_failed_cleanup_worker_releases_owned_resources_before_reporting_finished(self):
        with FakeTelesto() as peer:
            relay = TriggernometryTelesto(lambda _body: None, self.fail, peer.url)
            relay.start()

            def cleanup_relay():
                with patch.object(relay, "report", lambda _message: None):
                    relay._finish()

            self.addCleanup(cleanup_relay)
            self.assertEqual(post(relay.url, envelope("Subscribe", id="memory", type="memory"))[0], 200)
            self.assertEqual(post(relay.url, envelope("EnableDoodle", name="circle", type="circle"))[0], 200)
            entered, release = threading.Event(), threading.Event()
            original_post = relay._post
            original_start = threading.Thread.start
            failures = []

            def fail_cleanup_start(thread):
                if thread.name == "tn-telesto-cleanup":
                    raise RuntimeError("cannot start new thread")
                return original_start(thread)

            def stalled_cleanup(message, **options):
                entered.set()
                if not release.wait(3):
                    raise RuntimeError("cleanup was not released")
                return original_post(message, **options)

            def close_relay():
                try:
                    relay.close(wait=True)
                except Exception as exc:
                    failures.append(exc)

            with patch.object(threading.Thread, "start", fail_cleanup_start), \
                    patch.object(relay, "_post", side_effect=stalled_cleanup):
                worker = threading.Thread(target=close_relay, daemon=True)
                worker.start()
                try:
                    self.assertTrue(entered.wait(2), failures)
                    self.assertFalse(relay.is_finished())
                    self.assertEqual(relay._server.socket.fileno(), -1)
                    self.assertEqual(relay.receive({}, relay.callback_path), 410)
                    relay.close()
                    self.assertFalse(relay.is_finished())
                finally:
                    release.set()
                    worker.join(timeout=5)
                self.assertFalse(worker.is_alive())
            self.assertEqual(failures, [])
            self.assertTrue(relay.is_finished())
            relay.close(wait=True)
            self.assertFalse(peer.subscriptions)
            self.assertFalse(peer.drawings)

    def test_drawing_replacement_in_a_bundle_keeps_callbacks_and_cleanup(self):
        received = []
        with FakeTelesto() as peer:
            relay = TriggernometryTelesto(received.append, self.fail, peer.url)
            relay.start()
            try:
                request = envelope("EnableDoodle", name="circle", type="circle", notifyonexpiry=True)
                post(relay.url, request)
                peer.next("EnableDoodle")
                replacement = {"type": "Bundle", "payload": [
                    envelope("DisableDoodle", name="circle"), request]}
                self.assertEqual(post(relay.url, replacement)[0], 200)
                drawing = peer.next("EnableDoodle")["payload"]
                self.assertIn(drawing["name"], relay._owned_drawings)
                self.assertEqual(post(drawing["notifyonexpiry"], {
                    "notificationid": drawing["name"], "notificationtype": "doodleexpired"})[0], 200)
                self.assertEqual(len(received), 1)
                post(relay.url, replacement)
            finally:
                relay.close(wait=True)
            self.assertFalse(peer.drawings)

    def test_invalid_bundle_keeps_the_existing_subscription(self):
        received, errors = [], []
        with FakeTelesto() as peer:
            relay = TriggernometryTelesto(received.append, errors.append, peer.url)
            relay.start()
            try:
                request = envelope("Subscribe", id="memory", type="memory", start="1", length="1")
                post(relay.url, request)
                original = peer.next("Subscribe")["payload"]
                invalid = {"type": "Bundle", "payload": [request, envelope("EnableDoodle", type="circle")]}
                self.assertEqual(post(relay.url, invalid)[0], 400)
                self.assertEqual(post(original["endpoint"], {
                    "notificationid": original["id"], "notificationtype": "memory"})[0], 200)
                self.assertEqual(len(received), 1)
                self.assertEqual(list(peer.subscriptions), [original["id"]])
                self.assertTrue(errors)
            finally:
                relay.close(wait=True)

    def test_failed_unsubscribe_is_retried_on_shutdown(self):
        errors = []
        with FakeTelesto() as peer:
            relay = TriggernometryTelesto(lambda _body: None, errors.append, peer.url)
            relay.start()
            try:
                post(relay.url, envelope("Subscribe", id="memory", type="memory", start="1", length="1"))
                peer.fail_unsubscribe = True
                self.assertEqual(post(relay.url, envelope("Unsubscribe", id="memory"))[0], 503)
                self.assertTrue(peer.subscriptions)
                peer.fail_unsubscribe = False
            finally:
                relay.close(wait=True)
            self.assertTrue(errors)
            self.assertFalse(peer.subscriptions)

    def test_drawing_expression_timeout_returns_error_without_removing_resources(self):
        errors = []
        with FakeTelesto() as peer:
            relay = TriggernometryTelesto(lambda _body: None, errors.append, peer.url)
            relay.start()
            try:
                name = "a" * 1000 + "!"
                self.assertEqual(post(relay.url, envelope("EnableDoodle", name=name, type="circle"))[0], 200)
                before = dict(peer.drawings)
                self.assertEqual(post(relay.url, envelope("DisableDoodleRegex", regex="(a|aa)+$"))[0], 400)
                self.assertEqual(peer.drawings, before)
                self.assertTrue(any("Drawing expression timed out" in error for error in errors))
                self.assertEqual(post(relay.url, envelope("DisableDoodle", name=name))[0], 200)
                self.assertFalse(peer.drawings)
            finally:
                relay.close(wait=True)

    def test_drawing_expiry_cannot_remove_a_new_activation(self):
        received = []
        with FakeTelesto() as peer:
            relay = TriggernometryTelesto(received.append, self.fail, peer.url)
            relay.start()
            try:
                request = envelope("EnableDoodle", name="circle", type="circle", radius="5")
                post(relay.url, request)
                old = peer.next("EnableDoodle")["payload"]
                post(relay.url, request)
                new = peer.next("EnableDoodle")["payload"]
                self.assertEqual(old["name"], new["name"])
                self.assertNotEqual(old["notifyonexpiry"], new["notifyonexpiry"])
                def expiry(drawing):
                    return post(drawing["notifyonexpiry"], {
                        "notificationid": drawing["name"], "notificationtype": "doodleexpired"})[0]
                self.assertEqual(expiry(old), 410)
                self.assertTrue(relay._drawings)
                self.assertEqual(expiry(new), 200)
                self.assertFalse(relay._drawings)
                self.assertFalse(received)
            finally:
                relay.close(wait=True)

    def test_callback_identity_and_subscription_retirement(self):
        received = []
        with FakeTelesto() as peer:
            relay = TriggernometryTelesto(received.append, self.fail, peer.url)
            relay.start()
            try:
                request = envelope("Subscribe", id="partysynergy", type="memory",
                                   endpoint="http://localhost:51423/", start="¤{_addr[GameObject]}+6922", length="1")
                self.assertEqual(post(relay.url, request)[0], 200)
                old = peer.next("Subscribe")["payload"]
                self.assertEqual(old["endpoint"], relay.callback_url)
                self.assertEqual(old["start"], request["payload"]["start"])
                message = {"notificationid": old["id"], "notificationtype": "memory",
                           "payload": {"name": "Omega-M", "newvalue": "4"}}
                self.assertEqual(post(old["endpoint"], message)[0], 200)
                self.assertIn('"notificationid":"partysynergy"', received[-1])
                self.assertEqual(post(relay.url, request)[0], 200)
                new = peer.next("Subscribe")["payload"]
                self.assertNotEqual(old["id"], new["id"])
                self.assertEqual(post(old["endpoint"], message)[0], 410)
                self.assertEqual(len(received), 1)
                self.assertEqual(post(relay.url, envelope("Unsubscribe", id="partysynergy"))[0], 200)
                message["notificationid"] = new["id"]
                self.assertEqual(post(new["endpoint"], message)[0], 410)
            finally:
                relay.close(wait=True)
            self.assertFalse(peer.subscriptions)

    def test_drawings_keep_geometry_and_cleanup_only_this_run(self):
        with FakeTelesto() as peer:
            peer.drawings["unrelated"] = {}
            relay = TriggernometryTelesto(lambda _body: None, self.fail, peer.url)
            relay.start()
            try:
                request = envelope("EnableDoodle", name="buddy", type="line", expiresin="40000",
                                   start={"coords": "entity", "name": "Spike Player"},
                                   end={"coords": "entity", "name": "Buddy Player"})
                self.assertEqual(post(relay.url, request)[0], 200)
                drawing = peer.next("EnableDoodle")["payload"]
                self.assertEqual(drawing["start"], request["payload"]["start"])
                self.assertEqual(drawing["end"], request["payload"]["end"])
                self.assertEqual(drawing["expiresin"], "40000")
                self.assertNotEqual(drawing["name"], "buddy")
                linked = envelope("EnableDoodle", name="follow", type="circle", radius="5",
                                  position={"coords": "doodle", "name": "buddy/end"})
                self.assertEqual(post(relay.url, linked)[0], 200)
                self.assertEqual(peer.next("EnableDoodle")["payload"]["position"]["name"],
                                 drawing["name"] + "/end")
                self.assertEqual(post(relay.url, envelope("DisableDoodleRegex", regex="^buddy$"))[0], 200)
                self.assertNotIn(drawing["name"], peer.drawings)
                post(relay.url, request)
                post(relay.url, envelope("Subscribe", id="memory", type="memory", start="1", length="1"))
            finally:
                relay.close(wait=True)
            self.assertEqual(peer.drawings, {"unrelated": {}})
            self.assertFalse(peer.subscriptions)

    def test_drawings_do_not_enable_automarker_commands(self):
        errors = []
        with FakeTelesto() as peer:
            relay = TriggernometryTelesto(lambda _body: None, errors.append, peer.url)
            relay.start()
            try:
                command = envelope("ExecuteCommand", command="/mk attack1 <1>")
                drawing = envelope("EnableDoodle", name="circle", type="circle", radius="5")
                self.assertEqual(post(relay.url, {"type": "Bundle", "payload": [command, drawing]})[0], 200)
                self.assertEqual(peer.messages.qsize(), 1)
                self.assertEqual(peer.next("EnableDoodle")["payload"]["radius"], "5")
                self.assertTrue(errors)
                relay.configure(True)
                self.assertEqual(post(relay.url, command)[0], 200)
                self.assertEqual(peer.next("ExecuteCommand")["payload"], command["payload"])
            finally:
                relay.close(wait=True)


if __name__ == "__main__":
    unittest.main()
