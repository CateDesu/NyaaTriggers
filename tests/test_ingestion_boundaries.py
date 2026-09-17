"""Exercise connection changes and capture limits at their real transport boundaries."""

import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import time
import subprocess
import sys
from types import SimpleNamespace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt6.QtTest import QTest
from PyQt6.QtCore import QObject
from PyQt6.QtWidgets import QApplication, QLabel, QLineEdit
from websockets.sync.server import serve

from nyaatriggers import plugin_link, proc_env, pull_capture, ws_client
from nyaatriggers.telesto_client import TelestoClient
from nyaatriggers.ui.automarkers_tab import AutomarkersTabMixin
from nyaatriggers.ui.connection import ConnectionMixin
from nyaatriggers.updater_ui import UpdaterUiMixin

APP = QApplication.instance() or QApplication([])


def wait_for(predicate, timeout=4):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        QTest.qWait(10)
    return bool(predicate())


class TelestoPeer:
    def __init__(self):
        self.requests = []
        self.party = []
        self.code = 200
        self.entered = threading.Event()
        self.release = threading.Event()
        self.release.set()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.requests.append(request)
                outer.entered.set()
                outer.release.wait(4)
                body = json.dumps({"response": outer.party}).encode()
                try:
                    self.send_response(outer.code)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except OSError:
                    pass

            def log_message(self, *_args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.uri = f"http://127.0.0.1:{self.server.server_port}/"

    def close(self):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)


class OverlayPeer:
    def __init__(self):
        self.frames = []
        self.sockets = []
        self.hello_release = threading.Event()
        self.hello_release.set()
        self.server = serve(self.handle, "127.0.0.1", 0)
        self.port = self.server.socket.getsockname()[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def handle(self, ws):
        self.sockets.append(ws)
        try:
            self.hello_release.wait(4)
            ws.send('{"ev":"hello","protocol":1,"plugin":"0.2.0"}')
            for raw in ws:
                message = json.loads(raw)
                self.frames.append(message)
                if message.get("c") == "ping":
                    ws.send('{"ev":"pong"}')
        except Exception:
            pass

    def close(self):
        self.hello_release.set()
        for ws in self.sockets:
            ws.close()
        self.server.shutdown()
        self.thread.join(2)


class TransportChanges(unittest.TestCase):
    def telesto(self, peer):
        client = TelestoClient(uri=peer.uri, enabled=True, timeout=2,
                               delay_base_ms=0, delay_plus_ms=0)
        self.addCleanup(client.stop)
        return client

    def peer(self, cls):
        peer = cls()
        self.addCleanup(peer.close)
        return peer

    def test_telesto_endpoint_change_reclaims_cancelled_queue_capacity(self):
        old, new = self.peer(TelestoPeer), self.peer(TelestoPeer)
        old.release.clear()
        client = TelestoClient(uri=old.uri, enabled=False, max_queue=2,
                               timeout=2, delay_base_ms=0, delay_plus_ms=0)
        self.addCleanup(client.stop)
        client.start()
        client.ping()
        self.assertTrue(old.entered.wait(2))
        self.assertTrue(client.mark_self("attack1", force=True))
        self.assertTrue(client.clear_self(force=True))
        client.configure(uri=new.uri)
        client.ping()
        old.release.set()
        self.assertTrue(wait_for(lambda: new.requests))
        self.assertEqual([r["type"] for r in old.requests], ["GetPartyMembers"])
        self.assertEqual([r["type"] for r in new.requests], ["GetPartyMembers"])

    def test_telesto_disable_reclaims_capacity_without_losing_forced_cleanup(self):
        peer = self.peer(TelestoPeer)
        peer.release.clear()
        client = TelestoClient(uri=peer.uri, enabled=True, max_queue=3,
                               timeout=2, delay_base_ms=0, delay_plus_ms=0)
        self.addCleanup(client.stop)
        client.start()
        client.ping()
        self.assertTrue(peer.entered.wait(2))
        self.assertTrue(client.send_game_command("/mk clear <1>", force=True))
        self.assertTrue(client.mark_self("attack1"))
        self.assertTrue(client.send_game_command("/mk clear <2>", force=True))
        client.set_enabled(False)
        accepted = client.send_game_command("/mk clear <3>", force=True)
        peer.release.set()
        self.assertTrue(accepted, "Cancelled marks must not block forced cleanup")
        self.assertTrue(wait_for(lambda: len(peer.requests) == 4))
        self.assertEqual([r["payload"]["command"] for r in peer.requests[1:]],
                         ["/mk clear <1>", "/mk clear <2>", "/mk clear <3>"])

    def test_telesto_unchanged_settings_preserve_a_full_valid_queue(self):
        peer = self.peer(TelestoPeer)
        client = TelestoClient(uri=peer.uri, enabled=True, max_queue=2,
                               timeout=2, delay_base_ms=0, delay_plus_ms=0)
        self.addCleanup(client.stop)
        self.assertTrue(client.mark_self("attack1"))
        self.assertTrue(client.clear_self(force=True))
        client.configure(uri=peer.uri, enabled=True)
        self.assertFalse(client.mark_self("attack2"))
        client.start()
        self.assertTrue(wait_for(lambda: len(peer.requests) == 2))
        self.assertEqual([r["payload"]["command"] for r in peer.requests],
                         ["/mk attack1 <me>", "/mk clear <me>"])

    def test_telesto_endpoint_change_drops_old_commands_including_forced_ones(self):
        old, new = self.peer(TelestoPeer), self.peer(TelestoPeer)
        client = self.telesto(old)
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        client._sleep_command_delay = lambda _stop: (entered.set(), release.wait(4))
        client.mark_self("attack1")
        client.clear_self(force=True)
        client.start()
        self.assertTrue(entered.wait(2))
        client.configure(uri=new.uri)
        client.ping()
        release.set()
        self.assertTrue(wait_for(lambda: any(r["type"] == "GetPartyMembers" for r in new.requests)))
        client.stop()
        self.assertEqual(old.requests, [])
        self.assertEqual([r["type"] for r in new.requests], ["GetPartyMembers"])

    def test_telesto_endpoint_change_clears_roster_and_ignores_old_response(self):
        old, new = self.peer(TelestoPeer), self.peer(TelestoPeer)
        old.party = [{"order": "0", "actor": "10000001"}]
        new.party = [{"order": "0", "actor": "10000002"}]
        client = self.telesto(old)
        client.start()
        client.ping()
        self.assertTrue(wait_for(lambda: client.slot_of_actor("10000001") == 1))
        old.entered.clear()
        old.release.clear()
        client.ping()
        self.assertTrue(old.entered.wait(2))
        client.configure(uri=new.uri)
        cleared = client.party_slot_count()
        old.release.set()
        new.release.clear()
        client.ping()
        self.assertTrue(new.entered.wait(2))
        self.assertEqual(cleared, 0)
        self.assertIsNone(client.slot_of_actor("10000001"))
        self.assertIsNone(client._reachable)
        new.release.set()
        self.assertTrue(wait_for(lambda: client.slot_of_actor("10000002") == 1))
        self.assertEqual(client._reachable, (True, False))

    def test_telesto_stop_does_not_replay_pending_commands_on_restart(self):
        peer = self.peer(TelestoPeer)
        client = self.telesto(peer)
        client.mark_self("attack1")
        client.clear_self(force=True)
        client.stop()
        client.start()
        client.ping()
        self.assertTrue(wait_for(lambda: any(r["type"] == "GetPartyMembers" for r in peer.requests)))
        client.stop()
        self.assertEqual([r["type"] for r in peer.requests], ["GetPartyMembers"])

    def test_telesto_old_error_does_not_overwrite_a_changed_endpoint(self):
        old, new = self.peer(TelestoPeer), self.peer(TelestoPeer)
        old.code = 500
        old.release.clear()
        new.release.clear()
        client = self.telesto(old)
        client.start()
        client.ping()
        self.assertTrue(old.entered.wait(2))
        client.configure(uri=new.uri)
        client.ping()
        old.release.set()
        self.assertTrue(new.entered.wait(2))
        self.assertIsNone(client._reachable)
        new.release.set()
        self.assertTrue(wait_for(lambda: client._reachable == (True, False)))

    def test_telesto_restart_while_an_old_request_is_blocked(self):
        old, new = self.peer(TelestoPeer), self.peer(TelestoPeer)
        old.party = [{"order": "0", "actor": "10000001"}]
        old.release.clear()
        new.party = [{"order": "0", "actor": "10000002"}]
        client = self.telesto(old)
        client.start()
        client.ping()
        self.assertTrue(old.entered.wait(2))
        old_worker = client._thread
        client.mark_self("attack1")
        client.stop(join_timeout=0)
        client.configure(uri=new.uri)
        client.start()
        client.ping()
        self.assertTrue(wait_for(lambda: client.slot_of_actor("10000002") == 1))
        old.release.set()
        old_worker.join(2)
        self.assertFalse(old_worker.is_alive())
        self.assertIsNone(client.slot_of_actor("10000001"))
        self.assertEqual([r["type"] for r in new.requests], ["GetPartyMembers"])

    def test_telesto_invalid_saved_uri_reports_failure_and_recovers(self):
        peer = self.peer(TelestoPeer)
        client = self.telesto(peer)
        client.configure(uri="missing-scheme")
        client.start()
        client.ping()
        self.assertTrue(wait_for(lambda: client._reachable == (False, False)))
        client.configure(uri=peer.uri)
        client.ping()
        self.assertTrue(wait_for(lambda: client._reachable == (True, False)))

    def test_telesto_queued_status_does_not_describe_a_replaced_endpoint(self):
        old, new = self.peer(TelestoPeer), self.peer(TelestoPeer)
        new.release.clear()
        client = self.telesto(old)

        class StatusView(QObject, AutomarkersTabMixin, UpdaterUiMixin):
            def __init__(self):
                super().__init__()
                self._telesto_client = client
                self._telesto_status = "unknown"
                self._automark_status_lbl = QLabel()

        view = StatusView()
        client.status_changed.connect(view._on_telesto_client_status)
        emitted = threading.Event()
        original_report = client._report_reachable

        def report(*args, **kwargs):
            original_report(*args, **kwargs)
            emitted.set()

        client._report_reachable = report
        client.start()
        client.ping()
        self.assertTrue(emitted.wait(2))
        client.configure(uri=new.uri)
        client.ping()
        self.assertTrue(new.entered.wait(2))
        APP.processEvents()
        self.assertEqual(view._telesto_status, "unknown")
        new.release.set()
        self.assertTrue(wait_for(lambda: view._telesto_status == "good"))
        old.release.clear()
        old.code = 500
        view._settings = {"telesto_enabled": True, "telesto_uri": old.uri}
        view._apply_automark_state()
        self.assertEqual(view._telesto_status, "unknown")
        old.release.set()
        self.assertTrue(wait_for(lambda: view._telesto_status == "degraded"))

    def hold_overlay_message(self, link):
        entered, release = threading.Event(), threading.Event()
        waiting = threading.Event()
        original_get = link._queue.get

        def get(*args, **kwargs):
            waiting.set()
            item = original_get(*args, **kwargs)
            if isinstance(item, dict) and item.get("text") == "Pending":
                entered.set()
                release.wait(4)
            return item

        link._queue.get = get
        self.addCleanup(release.set)
        link.send_tick(0)
        self.assertTrue(waiting.wait(2))
        link.send_alert("Pending")
        self.assertTrue(entered.wait(2))
        return release

    def test_overlay_port_change_during_dequeue_uses_the_new_peer(self):
        old, new = self.peer(OverlayPeer), self.peer(OverlayPeer)
        link = plugin_link.PluginLink(port=old.port)
        self.addCleanup(link.stop)
        link.start()
        self.assertTrue(wait_for(link.is_connected))
        release = self.hold_overlay_message(link)
        link.set_port(new.port)
        release.set()
        self.assertTrue(wait_for(lambda: any(m.get("text") == "Pending" for m in new.frames)))
        self.assertFalse(any(m.get("text") == "Pending" for m in old.frames))

    def test_overlay_disable_during_dequeue_discards_pending_alerts(self):
        peer = self.peer(OverlayPeer)
        link = plugin_link.PluginLink(port=peer.port)
        self.addCleanup(link.stop)
        link.start()
        self.assertTrue(wait_for(link.is_connected))
        release = self.hold_overlay_message(link)
        link.send_alert("Queued before disable")
        link.set_enabled(False)
        release.set()
        self.assertTrue(wait_for(lambda: link.last_status() == (False, "Off")))
        link.set_enabled(True)
        self.assertTrue(wait_for(link.is_connected))
        link.send_alert("New")
        self.assertTrue(wait_for(lambda: any(m.get("text") == "New" for m in peer.frames)))
        self.assertEqual([m["text"] for m in peer.frames if m.get("c") == "alert"], ["New"])

    def test_overlay_quick_toggle_does_not_revive_a_dequeued_alert(self):
        peer = self.peer(OverlayPeer)
        link = plugin_link.PluginLink(port=peer.port)
        self.addCleanup(link.stop)
        link.start()
        self.assertTrue(wait_for(link.is_connected))
        release = self.hold_overlay_message(link)
        link.set_enabled(False)
        link.set_enabled(True)
        release.set()
        link.send_alert("New")
        self.assertTrue(wait_for(lambda: any(m.get("text") == "New" for m in peer.frames)))
        self.assertEqual([m["text"] for m in peer.frames if m.get("c") == "alert"], ["New"])

    def test_overlay_reconnect_cleanup_cannot_refill_with_disabled_alerts(self):
        peer = self.peer(OverlayPeer)
        peer.hello_release.clear()
        with patch.object(plugin_link, "OUTBOX_CAPACITY", 1):
            link = plugin_link.PluginLink(port=peer.port)
        self.addCleanup(link.stop)
        link.send_alert("Old")
        retaining, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        original_put = link._queue.put_nowait

        def put(message):
            if isinstance(message, dict) and message.get("text") == "Old":
                retaining.set()
                release.wait(4)
            return original_put(message)

        link._queue.put_nowait = put
        link.start()
        self.assertTrue(retaining.wait(2))
        toggling = threading.Event()

        def toggle():
            toggling.set()
            link.set_enabled(False)
            link.set_enabled(True)

        thread = threading.Thread(target=toggle, daemon=True)
        thread.start()
        self.assertTrue(toggling.wait(2))
        thread.join(.1)
        release.set()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertTrue(wait_for(lambda: peer.sockets))
        link.send_alert("New")
        peer.hello_release.set()
        self.assertTrue(wait_for(lambda: any(m.get("text") == "New" for m in peer.frames)))
        self.assertEqual([m["text"] for m in peer.frames if m.get("c") == "alert"], ["New"])

    def test_overlay_failed_send_does_not_retry_an_invalidated_alert(self):
        peer = self.peer(OverlayPeer)
        with patch.object(plugin_link, "OUTBOX_CAPACITY", 1):
            link = plugin_link.PluginLink(port=peer.port)
        self.addCleanup(link.stop)
        link.start()
        self.assertTrue(wait_for(link.is_connected))
        release = self.hold_overlay_message(link)
        link.set_enabled(False)
        link.set_enabled(True)
        peer.hello_release.clear()
        peer.sockets[0].close()
        release.set()
        self.assertTrue(wait_for(lambda: len(peer.sockets) == 2))
        link.send_alert("New")
        peer.hello_release.set()
        self.assertTrue(wait_for(lambda: any(m.get("text") == "New" for m in peer.frames)))
        self.assertEqual([m["text"] for m in peer.frames if m.get("c") == "alert"], ["New"])

    def test_overlay_connection_loss_retains_a_current_alert(self):
        peer = self.peer(OverlayPeer)
        link = plugin_link.PluginLink(port=peer.port)
        self.addCleanup(link.stop)
        link.start()
        self.assertTrue(wait_for(link.is_connected))
        release = self.hold_overlay_message(link)
        peer.sockets[0].close()
        release.set()
        self.assertTrue(wait_for(lambda: any(m.get("text") == "Pending" for m in peer.frames)))
        self.assertEqual([m["text"] for m in peer.frames if m.get("c") == "alert"], ["Pending"])


class CaptureBoundaries(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.folder = Path(directory.name)
        self.ws = ws_client.WSClient()
        self.capture = pull_capture.PullCapture(self.folder, state_snapshot=self.ws.state_snapshot)
        self.addCleanup(self.capture.close)
        self.ws.raw_message.connect(self.capture.on_raw_message)
        self.ws.log_line.connect(self.capture.on_log_line)
        self.ws.zone_changed.connect(self.capture.on_zone_changed)
        self.capture.set_recording(True)

    def send(self, raw):
        self.ws._on_message(json.dumps({"type": "LogLine", "rawLine": raw}, ensure_ascii=False))

    def begin(self):
        self.send("20|ts|40000001|Boss|1234|Cast|")

    def test_capture_size_limit_counts_utf8_bytes(self):
        self.begin()
        path = next(self.folder.rglob("*.jsonl"))
        size = path.stat().st_size
        with patch.object(pull_capture, "_MAX_PULL_BYTES", size + 250):
            self.send("00|ts|" + "猫" * 100)
        self.assertFalse(self.capture._in_pull)
        self.assertEqual(json.loads(path.with_suffix(".meta.json").read_text())["outcome"], "truncated")
        self.assertEqual(self.capture._bytes, path.stat().st_size)

    def test_prepull_size_limit_counts_utf8_bytes(self):
        with patch.object(pull_capture, "_PRE_PULL_MAX_BYTES", 450):
            self.send("00|old|" + "猫" * 100)
            self.send("00|new|" + "猫" * 100)
            self.begin()
        self.capture.close()
        text = next(self.folder.rglob("*.jsonl")).read_text()
        self.assertNotIn("00|old|", text)
        self.assertIn("00|new|", text)

    def test_raw_zone_change_discards_previous_zone_prepull_events(self):
        self.send("26|ts|old zone status")
        self.send("01|ts|04CA|New zone")
        self.begin()
        self.capture.close()
        text = next(self.folder.rglob("*.jsonl")).read_text()
        self.assertNotIn("old zone status", text)
        self.assertIn("01|ts|04CA|New zone", text)

    def test_raw_zone_boundary_is_in_the_next_pull_after_an_active_pull(self):
        self.begin()
        self.send("01|ts|04CA|New zone")
        first = next(self.folder.rglob("*.jsonl"))
        self.begin()
        self.capture.close()
        second = next(p for p in self.folder.rglob("*.jsonl") if p != first)
        self.assertIn("01|ts|04CA|New zone", second.read_text())
        self.assertEqual(json.loads(first.with_suffix(".meta.json").read_text())["outcome"], "reset")

    def test_raw_zone_updates_recording_state_after_the_buffer_expires(self):
        self.ws._on_message(json.dumps({"type": "ChangeZone", "zoneID": 1, "zoneName": "Old zone"}))
        self.send("01|ts|04CA|New zone")
        with patch.object(pull_capture, "_PRE_PULL_SECONDS", .01):
            QTest.qWait(30)
            self.begin()
        self.capture.close()
        frames = [json.loads(line) for line in next(self.folder.rglob("*.jsonl")).read_text().splitlines()]
        zones = [frame for frame in frames if frame.get("type") == "ChangeZone"]
        self.assertEqual(zones, [{"type": "ChangeZone", "zoneID": 1226, "zoneName": "New zone"}])


class FeedBoundaries(unittest.TestCase):
    def test_raw_and_broadcast_zone_lines_update_replay_without_duplicate_signals(self):
        client = ws_client.WSClient()
        zones, logs = [], []
        client.zone_changed.connect(lambda *args: zones.append(args))
        client.log_line.connect(logs.append)
        for message in ("01|ts|04CA|First|0", json.dumps({
                "type": "broadcast", "msgtype": "LogLine", "msg": "01|ts|04CB|Second|0"})):
            client._on_message(message)
        self.assertEqual(json.loads(client.state_snapshot()[0])["zoneID"], 1227)
        self.assertEqual(zones, [])
        self.assertEqual(len(logs), 2)
        for zone in ("invalid", "F" * 10000, "-1"):
            client._on_message(f"01|ts|{zone}|Broken|0")
        self.assertEqual(json.loads(client.state_snapshot()[0])["zoneName"], "Second")

    def connect(self, messages, limit=None):
        def handle(ws):
            try:
                ws.recv(timeout=2)
                for message in messages:
                    ws.send(message)
                for _ in ws:
                    pass
            except Exception:
                pass

        server = serve(handle, "127.0.0.1", 0, close_timeout=.2)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.shutdown)
        with patch.object(ws_client, "_MAX_WS_MESSAGE", limit or ws_client._MAX_WS_MESSAGE):
            client = ws_client.WSClient()
        self.addCleanup(client.disconnect_from)
        raw, lines, status = [], [], []
        client.raw_message.connect(raw.append)
        client.log_line.connect(lines.append)
        client.status_changed.connect(lambda *args: status.append(args))
        client.connect_to(f"ws://127.0.0.1:{server.socket.getsockname()[1]}/")
        return client, raw, lines, status

    def test_fragmented_messages_and_malformed_json_do_not_break_the_next_log(self):
        valid = json.dumps({"type": "LogLine", "line": ["00", "ts", "猫"]}, ensure_ascii=False)
        malformed = ["{bad", "[]", "[" * 1200 + "]" * 1200,
                     '{"type":"PartyChanged","party":[null,{}, {"id":1e400}]}',
                     '{"type":"combatants","combatants":[null,{"ID":1e400,"PosX":"NaN"}]}']
        client, raw, lines, status = self.connect([*malformed, [valid[:20], valid[20:]]])
        self.assertTrue(wait_for(lambda: "00|ts|猫" in lines))
        self.assertEqual(raw, [*malformed, valid])
        self.assertEqual(lines.count("00|ts|猫"), 1)
        self.assertTrue(status[-1][0])
        self.assertFalse(client._reconnect_timer.isActive())

    def test_qt_enforces_size_limit_across_fragments_before_emitting_text(self):
        _client, raw, _lines, status = self.connect([["x" * 100, "y" * 100]], limit=128)
        self.assertTrue(wait_for(lambda: any(not connected for connected, _ in status)))
        self.assertEqual(raw, [])

    def test_stalled_upgrade_is_aborted_and_manual_stop_cancels_retry(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        listener.settimeout(.1)
        accepted = []
        stop = threading.Event()

        def accept():
            while not stop.is_set():
                try:
                    peer, _ = listener.accept()
                    accepted.append(peer)
                except socket.timeout:
                    continue
                except OSError:
                    return

        thread = threading.Thread(target=accept, daemon=True)
        thread.start()
        client = ws_client.WSClient()
        try:
            client.connect_to(f"ws://127.0.0.1:{listener.getsockname()[1]}/")
            self.assertTrue(wait_for(lambda: len(accepted) == 1))
            client._reconnect_timer.start(20)
            self.assertTrue(wait_for(lambda: len(accepted) == 2))
            client.disconnect_from()
            QTest.qWait(100)
            self.assertEqual(len(accepted), 2)
            self.assertFalse(client._reconnect_timer.isActive())
        finally:
            client.disconnect_from()
            stop.set()
            listener.close()
            thread.join(2)
            for peer in accepted:
                peer.close()


@unittest.skipUnless(os.name == "posix", "Wine drive fixtures use POSIX symlinks")
class LogDirectoryPaths(unittest.TestCase):
    def test_configured_wine_drive_is_used_before_default_folder(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            prefix = home / ".xlcore" / "wineprefix"
            drives = prefix / "dosdevices"
            drives.mkdir(parents=True)
            target = home / "custom-drive"
            logs = target / "Logs" / "IINACT"
            logs.mkdir(parents=True)
            (drives / "d:").symlink_to(target, target_is_directory=True)
            fallback = home / "Documents" / "IINACT"
            fallback.mkdir(parents=True)
            cfg = home / ".xlcore" / "pluginConfigs" / "IINACT.json"
            cfg.parent.mkdir()
            cfg.write_text(json.dumps({"LogFilePath": "D:\\Logs\\IINACT"}))
            with patch.object(Path, "home", return_value=home):
                actual = ConnectionMixin._find_iinact_log_dir()
            self.assertIsNotNone(actual)
            self.assertEqual(actual.resolve(), logs.resolve())

    def test_existing_c_drive_fallback_and_invalid_config_still_work(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            logs = home / ".xlcore" / "wineprefix" / "drive_c" / "users" / "test" / "Logs"
            logs.mkdir(parents=True)
            fallback = home / "Documents" / "IINACT"
            fallback.mkdir(parents=True)
            cfg = home / ".xlcore" / "pluginConfigs" / "IINACT.json"
            cfg.parent.mkdir()
            for raw in ("C:\\users\\test\\Logs", "C:\\Users\\test\\Logs", "C:/users/test/Logs"):
                with self.subTest(raw=raw), patch.object(Path, "home", return_value=home):
                    cfg.write_text(json.dumps({"LogFilePath": raw}))
                    self.assertEqual(ConnectionMixin._find_iinact_log_dir(), logs)
            for content in ("{broken", "[]", '{"LogFilePath":null}', '{"LogFilePath":"C:\\\\"}'):
                with self.subTest(content=content), patch.object(Path, "home", return_value=home):
                    cfg.write_text(content)
                    self.assertEqual(ConnectionMixin._find_iinact_log_dir(), fallback)


class TelestoUrlValidation(unittest.TestCase):
    def test_malformed_urls_leave_the_saved_endpoint_unchanged(self):
        saved = "http://127.0.0.1:45678/"
        for uri in ("http://[", "http://[not-an-ip]/", "http://localhost:bad/",
                    "http://localhost:65536/", "http://user@/"):
            with self.subTest(uri=uri):
                edit = QLineEdit(uri)
                changes = []
                window = SimpleNamespace(
                    _automark_uri_edit=edit, _settings={"telesto_uri": saved},
                    _save_settings=lambda: changes.append("saved"),
                    _apply_automark_state=lambda: changes.append("applied"),
                    _telesto_client=SimpleNamespace(ping=lambda: changes.append("ping")))
                AutomarkersTabMixin._on_automark_uri_changed(window)
                self.assertEqual(edit.text(), saved)
                self.assertEqual(window._settings["telesto_uri"], saved)
                self.assertTrue(edit.toolTip())
                self.assertEqual(changes, [])

    def test_valid_urls_still_save_apply_and_probe(self):
        for uri in ("http://localhost/", "https://example.invalid:443/path", "http://[::1]:45678/"):
            with self.subTest(uri=uri):
                changes = []
                edit = QLineEdit(uri)
                window = SimpleNamespace(
                    _automark_uri_edit=edit, _settings={},
                    _save_settings=lambda: changes.append("saved"),
                    _apply_automark_state=lambda: changes.append("applied"),
                    _telesto_client=SimpleNamespace(ping=lambda: changes.append("ping")))
                edit.editingFinished.connect(lambda: AutomarkersTabMixin._on_automark_uri_changed(window))
                edit.editingFinished.emit()
                self.assertEqual(window._settings["telesto_uri"], uri)
                self.assertEqual(changes, ["saved", "applied", "ping"])
                self.assertFalse(edit.toolTip())


class ProcessEnvironment(unittest.TestCase):
    def test_child_process_gets_the_restored_library_path_without_mutating_parent(self):
        for frozen, original, expected in ((False, "/system", "/bundle"),
                                           (True, "/system", "/system"),
                                           (True, "", ""), (True, None, None)):
            with self.subTest(frozen=frozen, original=original), \
                 patch.object(sys, "frozen", frozen, create=True), \
                 patch.dict(os.environ, {"LD_LIBRARY_PATH": "/bundle"}):
                if original is None:
                    os.environ.pop("LD_LIBRARY_PATH_ORIG", None)
                else:
                    os.environ["LD_LIBRARY_PATH_ORIG"] = original
                environment = proc_env.child_env()
                result = subprocess.check_output(
                    [sys.executable, "-c", "import os,json; print(json.dumps(os.environ.get('LD_LIBRARY_PATH')))"],
                    env=environment, text=True, timeout=5)
                self.assertEqual(json.loads(result), expected)
                self.assertEqual(os.environ["LD_LIBRARY_PATH"], "/bundle")


if __name__ == "__main__":
    unittest.main()
