from contextlib import ExitStack, contextmanager
import os
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("NYAA_REPLAY_TEST", "1")

from nyaatriggers import triggernometry_bridge as tn
from tests.test_sidecar_shutdown import launched, until
from tests.test_triggernometry_telesto import FakeTelesto, envelope, post


@contextmanager
def paused_retirement(bridge):
    entered, release = threading.Event(), threading.Event()
    original = bridge._close_telesto
    errors = []

    def close(relay, wait=False):
        if relay is not None and threading.current_thread() is bridge._reader:
            entered.set()
            if not release.wait(20):
                errors.append("Reader retirement was not released")
        return original(relay, wait=wait)

    with patch.object(bridge, "_close_telesto", side_effect=close):
        try:
            yield entered, release
        finally:
            release.set()
    if errors:
        raise AssertionError(errors)


class RelayShutdownTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from tests.test_session_ui import SessionUiTests
        SessionUiTests.setUpClass()

    def populate(self, relay, peer):
        for request in (envelope("Subscribe", id="memory", type="memory"),
                        envelope("EnableDoodle", name="circle", type="circle")):
            self.assertEqual(post(relay.url, request)[0], 200)
        self.assertTrue(peer.subscriptions)
        self.assertTrue(peer.drawings)
        return set(peer.subscriptions), set(peer.drawings)

    def assert_closed(self, relay, peer):
        self.assertFalse(peer.subscriptions, "Subscription survived shutdown")
        self.assertFalse(peer.drawings, "Drawing survived shutdown")
        self.assertTrue(relay.is_finished(), "Relay cleanup did not finish")
        with socket.socket() as probe:
            probe.settimeout(1)
            self.assertNotEqual(probe.connect_ex(relay._server.server_address), 0,
                                "Callback listener survived shutdown")

    def check_boundary(self, operation):
        from PyQt6.QtCore import QTimer
        from PyQt6.QtNetwork import QHostAddress
        from PyQt6.QtWebSockets import QWebSocketServer
        from PyQt6.QtWidgets import QApplication
        from nyaatriggers import app_common as ac, tts, updater
        from nyaatriggers.telesto_client import TelestoClient
        from nyaatriggers.triggevent_bridge import TriggeventBridge
        from tests.test_session_ui import SessionUiTests

        case = unittest.TestCase()
        try:
            with patch.object(TelestoClient, "start"), \
                    patch.object(TriggeventBridge, "is_available", return_value=False), \
                    patch.object(tn.TriggernometryBridge, "is_available", return_value=False):
                SessionUiTests.setUp(case)
            stack, window = case.stack, case.window
            for cls in (TriggeventBridge, tn.TriggernometryBridge):
                stack.enter_context(patch.object(cls, "is_available", return_value=False))
            stack.enter_context(patch.object(tts, "_ensure_worker"))
            stack.enter_context(patch.object(QApplication, "quit"))
            for name in ("_TRIGGEVENT_INVENTORY_CACHE", "_TRIGGEVENT_AUTOMARK_INVENTORY_CACHE"):
                stack.enter_context(patch.object(ac, name, case.temp / name))
            window._find_iinact_log_dir = lambda: None
            window._triggevent_mode = window._triggernometry_mode = False
            bridge = window._ensure_triggernometry_bridge()
            server = QWebSocketServer("relay shutdown fixture", QWebSocketServer.SslMode.NonSecureMode)
            self.assertTrue(server.listen(QHostAddress(QHostAddress.SpecialAddress.LocalHost), 0))
            stack.callback(server.close)
            peers, disconnected = [], []
            server.newConnection.connect(lambda: peers.append(server.nextPendingConnection()))
            window._ws.status_changed.connect(
                lambda connected, _message: disconnected.append(not connected))
            window._ws.connect_to(f"ws://127.0.0.1:{server.serverPort()}/ws")
            until(lambda: window._connected and bool(peers), pump=True)
            stack.callback(lambda: [peer.close() for peer in peers])
            window._triggernometry_mode = True

            with FakeTelesto() as peer, paused_retirement(bridge) as (entered, release):
                bridge.configure_telesto(peer.url)
                with launched(tn, bridge, case.temp, "graceful") as (proc, runtime, _):
                    relay = bridge._telesto
                    try:
                        self.populate(relay, peer)
                        tn.proc_env.signal_group(proc, graceful=True)
                        self.assertTrue(entered.wait(5), "Reader did not detach the relay")
                        self.assertIsNone(bridge._telesto)
                        self.assertFalse(relay.is_finished())
                        boundary, stops = [], []
                        original_stop = bridge.stop

                        def stop(wait=False):
                            stops.append(wait)
                            return original_stop(wait=wait)

                        def observe(*_args):
                            boundary.append((dict(peer.subscriptions), dict(peer.drawings),
                                             relay.is_finished(), proc.poll(), runtime.exists()))

                        with patch.object(bridge, "stop", side_effect=stop), \
                                patch.object(updater, "relaunch", side_effect=observe), \
                                patch.object(QTimer, "singleShot", side_effect=observe):
                            getattr(window, operation)()
                        self.assertTrue(disconnected and any(disconnected))
                        self.assertIn(False, stops, "Disconnect did not request async stop")
                        self.assertIn(True, stops, "Shutdown did not wait")
                        self.assert_closed(relay, peer)
                        self.assertIsNotNone(proc.poll())
                        self.assertFalse(runtime.exists())
                        if operation != "close":
                            self.assertTrue(boundary, "Relaunch or quit was not reached")
                            for subscriptions, drawings, finished, code, exists in boundary:
                                self.assertEqual((subscriptions, drawings, finished, exists),
                                                 ({}, {}, True, False))
                                self.assertIsNotNone(code)
                    finally:
                        release.set()
                        relay.close(wait=True)
        finally:
            case.doCleanups()

    def test_close_waits_for_detached_reader_relay(self):
        self.check_boundary("close")

    def test_restart_cleans_detached_relay_before_relaunch(self):
        self.check_boundary("_restart_for_update")

    def test_handoff_cleans_detached_relay_before_scheduling_quit(self):
        self.check_boundary("_quit_for_windows_handoff")

    def test_async_stop_keeps_cleanup_joinable_without_holding_state_lock(self):
        with tempfile.TemporaryDirectory() as temp, FakeTelesto() as peer:
            bridge = tn.TriggernometryBridge()
            bridge.configure_telesto(peer.url)
            with launched(tn, bridge, Path(temp), "graceful"):
                relay = bridge._telesto
                self.populate(relay, peer)
                entered, release, waiting = threading.Event(), threading.Event(), threading.Event()
                errors = []
                original_post, original_close = relay._post, relay.close

                def gated_post(message, cleanup=False):
                    if cleanup:
                        entered.set()
                        if not release.wait(5):
                            errors.append("Cleanup was not released by the waiting stop")
                    return original_post(message, cleanup=cleanup)

                def close(wait=False):
                    unlocked = bridge._state_lock.acquire(blocking=False)
                    if unlocked:
                        bridge._state_lock.release()
                    else:
                        errors.append("Relay close held the state lock")
                    if wait:
                        waiting.set()
                    return original_close(wait=wait)

                def unblock():
                    if not waiting.wait(5):
                        errors.append("Waiting stop skipped the relay")
                    release.set()

                with patch.object(relay, "_post", side_effect=gated_post), \
                        patch.object(relay, "close", side_effect=close):
                    worker = None
                    try:
                        start = time.monotonic()
                        bridge.stop()
                        self.assertLess(time.monotonic() - start, 1)
                        self.assertTrue(entered.wait(2))
                        self.assertFalse(relay.is_finished())
                        self.assertTrue(peer.subscriptions and peer.drawings)
                        worker = threading.Thread(target=unblock)
                        worker.start()
                        bridge.stop(wait=True)
                        bridge.stop(wait=True)
                        self.assert_closed(relay, peer)
                    finally:
                        release.set()
                        if worker is not None:
                            worker.join(6)
                self.assertFalse(errors, errors)

    def test_old_reader_cleanup_preserves_replacement_resources_and_callbacks(self):
        with tempfile.TemporaryDirectory() as temp, FakeTelesto() as peer, ExitStack() as stack:
            bridge = tn.TriggernometryBridge()
            bridge.configure_telesto(peer.url)
            with paused_retirement(bridge) as (entered, release):
                old_proc, _, _ = stack.enter_context(launched(tn, bridge, Path(temp), "graceful"))
                old_reader, old_relay = bridge._reader, bridge._telesto
                try:
                    self.populate(old_relay, peer)
                    tn.proc_env.signal_group(old_proc, graceful=True)
                    self.assertTrue(entered.wait(5))
                    new_proc, _, _ = stack.enter_context(launched(tn, bridge, Path(temp), "graceful"))
                    new_relay = bridge._telesto
                    before_subscriptions, before_drawings = set(peer.subscriptions), set(peer.drawings)
                    self.populate(new_relay, peer)
                    expected = (set(peer.subscriptions) - before_subscriptions,
                                set(peer.drawings) - before_drawings)
                    generation = bridge.generation()
                    endpoints = []
                    with patch.object(bridge, "_enqueue", side_effect=endpoints.append):
                        old_relay.callback("stale")
                        new_relay.callback("current")
                    self.assertEqual(endpoints, [{"t": "endpoint", "body": "current"}])
                    release.set()
                    old_reader.join(7)
                    self.assertFalse(old_reader.is_alive())
                    old_relay.close(wait=True)
                    self.assertEqual((set(peer.subscriptions), set(peer.drawings)), expected)
                    self.assertIs(bridge._telesto, new_relay)
                    self.assertIs(bridge._proc, new_proc)
                    self.assertIsNone(new_proc.poll())
                    self.assertTrue(bridge._gen_live(generation))
                    self.assertFalse(new_relay.is_finished())
                    bridge.stop(wait=True)
                    self.assert_closed(new_relay, peer)
                finally:
                    release.set()
                    old_relay.close(wait=True)


if __name__ == "__main__":
    unittest.main()
