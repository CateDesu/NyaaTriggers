import json
import os
from pathlib import Path
from queue import Queue
import shutil
import time
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("NYAA_REPLAY_TEST", "1")

from PyQt6.QtCore import QTimer
from PyQt6.QtNetwork import QHostAddress
from PyQt6.QtTest import QTest
from PyQt6.QtWebSockets import QWebSocketServer
from PyQt6.QtWidgets import QMessageBox

from nyaatriggers import app_common as ac, triggernometry_bridge as tn, tts, updater
from nyaatriggers.telesto_client import TelestoClient
from nyaatriggers.triggevent_bridge import TriggeventBridge
from nyaatriggers.triggernometry_editor import PackDocument
from tests import test_session_ui
from tests.engine_fixture import joined_bridge_workers
from tests.test_triggernometry_telesto import FakeTelesto


@unittest.skipUnless(os.name == "posix" and shutil.which("mono") and shutil.which("xvfb-run"),
                     "Requires Mono and Xvfb")
class NativeTeardownTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        test_session_ui.SessionUiTests.setUpClass()

    def setUp(self):
        with patch.object(TelestoClient, "start"), \
                patch.object(TriggeventBridge, "is_available", return_value=False), \
                patch.object(tn.TriggernometryBridge, "is_available", return_value=False):
            test_session_ui.SessionUiTests.setUp(self)
        self.stack.enter_context(patch.object(TriggeventBridge, "is_available", return_value=False))
        self.stack.enter_context(patch.object(tts, "_ensure_worker"))
        self.stack.enter_context(patch.object(tts, "_queue", Queue()))
        self.diagnostics = []
        self.stack.enter_context(patch.object(tn, "_log", side_effect=self.diagnostics.append))
        self.stack.enter_context(patch.object(tn, "_make_bundled_mono_executable"))
        self.peer = self.stack.enter_context(FakeTelesto())
        packs, runtime = self.temp / "triggernometry-packs", self.temp / "runtime"
        runtime.mkdir()
        self.stack.enter_context(patch.object(tn, "_packs_dir", return_value=packs))
        self.stack.enter_context(patch.object(tn, "_rundata_dir", return_value=runtime))
        self.stack.enter_context(patch.dict(os.environ, {
            "NYAA_TRIGGERNOMETRY_EXE": str(Path(__file__).resolve().parents[1]
                                            / "triggernometry-core/bin/triggernometry-core.exe")}))
        document = PackDocument(packs / "teardown.xml")
        trigger = document.add_trigger()
        trigger.set("RegularExpression", "TEARDOWN_PROBE")
        request = json.dumps({"version": 1, "id": 1, "type": "EnableDoodle",
                              "payload": {"name": "teardown-probe", "type": "circle"}})
        script = ('Triggernometry.Interpreter.StaticHelpers.SetScalarVariable(false, '
                  '"teardown_result", (40+2).ToString());')
        actions = trigger.find("Actions")
        ET.SubElement(actions, "Action", OrderNumber="1", ActionType="ExecuteScript",
                      Asynchronous="False", ExecScriptExpression=script)
        ET.SubElement(actions, "Action", OrderNumber="2", ActionType="GenericJson",
                      Asynchronous="False", JsonPayloadExpression=request,
                      JsonEndpointExpression="http://${_const[TelestoEndpoint]}:${_const[TelestoPort]}")
        ET.SubElement(actions, "Action", OrderNumber="3", ActionType="UseTTS",
                      Asynchronous="False", UseTTSTextExpression="script computed ${var:teardown_result}")
        document.save()
        self.window._settings["telesto_uri"] = self.peer.url
        self.window._find_iinact_log_dir = lambda: None
        self.bridge = self.window._ensure_triggernometry_bridge()
        self.stack.enter_context(joined_bridge_workers(tn, self.bridge))
        self.inventory, self.scripts = [], []
        self.bridge.inventory.connect(lambda _payload, gen: self.inventory.append(gen))
        self.bridge.tts.connect(lambda text, gen: self.scripts.append((text, gen)))
        self.addCleanup(lambda: self.bridge.stop(wait=True))
        self.addCleanup(self.window._triggernometry_retry_timer.stop)
        self.server = QWebSocketServer("teardown fixture", QWebSocketServer.SslMode.NonSecureMode)
        self.assertTrue(self.server.listen(QHostAddress(QHostAddress.SpecialAddress.LocalHost), 0))
        self.addCleanup(self.server.close)
        self.peers = []
        self.server.newConnection.connect(lambda: self.peers.append(self.server.nextPendingConnection()))
        self.url = f"ws://127.0.0.1:{self.server.serverPort()}/ws"
        self.window._ws.connect_to(self.url)
        self.wait_for(lambda: self.window._connected and bool(self.peers))
        self.window._set_triggernometry_enabled(True)
        self.wait_for(lambda: self.bridge.generation() in self.inventory)
        self.probe()
        self.wait_for(lambda: bool(self.scripts) and bool(self.peer.drawings))
        self.assertEqual(self.scripts, [("script computed 42", self.bridge.generation())])
        tts.interrupt()

    def wait_for(self, predicate, timeout=20):
        deadline = time.monotonic() + timeout
        while not predicate() and time.monotonic() < deadline:
            QTest.qWait(10)
        self.assertTrue(predicate(), (self.inventory, self.scripts, self.bridge.is_active(),
                                      self.diagnostics[-15:]))

    def probe(self):
        self.peers[-1].sendTextMessage(json.dumps({
            "type": "LogLine", "rawLine": "00|2026-11-13T12:00:00Z|0038|Player|TEARDOWN_PROBE|0"}))

    def check_modal(self, operation, route, *, teardown=True, relaunch_error=None):
        bridge, window = self.bridge, self.window
        generation, old_drawings = bridge.generation(), set(self.peer.drawings)
        processes, relays = [bridge._proc], [bridge._telesto]
        starts, observed, sent = [], [], set()
        start = bridge.start

        def launch():
            starts.append(window._triggernometry_retry_stopped)
            start()
            if bridge._proc is not None:
                processes.append(bridge._proc)
            if bridge._telesto is not None:
                relays.append(bridge._telesto)

        def warning(parent, title, text):
            if observed:
                return QMessageBox.StandardButton.Ok
            dialog = QMessageBox(QMessageBox.Icon.Warning, title, text,
                                 QMessageBox.StandardButton.Ok, parent)
            deadline, transitioned = time.monotonic() + 20, []

            def inject():
                if route == "overflow":
                    bridge.feed_overflow.emit(generation)
                else:
                    window._ws._reconnect_delay = 1
                    self.peers[-1].close()

            begin = QTimer(dialog)
            begin.setSingleShot(True)
            begin.timeout.connect(inject)
            begin.start(0)
            poll = QTimer(dialog)

            def inspect():
                reached = (bridge.generation() > generation if route == "overflow"
                           else len(self.peers) > 1 and window._connected)
                if reached and not transitioned:
                    transitioned.append(time.monotonic())
                current = bridge.generation()
                if current > generation and current in self.inventory and current not in sent:
                    sent.add(current)
                    self.probe()
                scripts = [(text, gen) for text, gen in self.scripts if gen > generation]
                drawings = set(self.peer.drawings) - old_drawings
                blocked = (transitioned and not starts and not bridge.is_active()
                           and time.monotonic() - transitioned[0] >= 0.15)
                if blocked or scripts and drawings or time.monotonic() >= deadline:
                    observed.append((bool(transitioned), list(starts), scripts, drawings,
                                     window._triggernometry_mode, tts._queue.qsize(),
                                     window._triggernometry_retry_stopped))
                    dialog.accept()

            poll.timeout.connect(inspect)
            poll.start(10)
            return dialog.exec()

        window._save_warned = False
        window._settings_save_timer.start()
        try:
            with patch("nyaatriggers.ui.settings_tab._atomic_write_json",
                       side_effect=PermissionError("Storage unavailable")), \
                    patch.object(ac.QMessageBox, "warning", side_effect=warning), \
                    patch.object(updater, "relaunch", side_effect=relaunch_error), \
                    patch.object(bridge, "start", side_effect=launch):
                getattr(window, operation)()
            self.assertEqual(len(observed), 1)
            reached, attempts, scripts, drawings, mode, queued, suspended = observed[0]
            self.assertTrue(reached, "Recovery route did not run during the modal")
            self.assertTrue(mode, "Requested mode was lost")
            self.assertEqual(suspended, teardown)
            if teardown:
                self.assertEqual(attempts, [], observed[0])
                self.assertEqual(scripts, [])
                self.assertFalse(drawings)
                self.assertEqual(queued, 0)
            else:
                self.assertEqual(attempts, [False])
                self.assertEqual(scripts, [("script computed 42", bridge.generation())])
                self.assertTrue(drawings)
                self.assertGreater(queued, 0)
        finally:
            bridge.stop(wait=True)
            for relay in relays:
                relay.close(wait=True)
            self.assertTrue(all(proc.poll() is not None for proc in processes))
            self.assertTrue(all(relay.is_finished() for relay in relays))
            self.assertFalse(self.peer.drawings)

    def test_restart_blocks_overflow_host_and_script_during_warning(self):
        self.check_modal("_restart_for_update", "overflow")

    def test_restart_blocks_reconnect_host_and_script_during_warning(self):
        self.check_modal("_restart_for_update", "reconnect")

    def test_handoff_blocks_overflow_host_and_script_during_warning(self):
        self.check_modal("_quit_for_windows_handoff", "overflow")

    def test_handoff_blocks_reconnect_host_and_script_during_warning(self):
        self.check_modal("_quit_for_windows_handoff", "reconnect")

    def test_ordinary_warning_allows_overflow_host_script_and_drawing(self):
        self.check_modal("_flush_pending_settings_save", "overflow", teardown=False)

    def test_ordinary_warning_allows_reconnect_host_script_and_drawing(self):
        self.check_modal("_flush_pending_settings_save", "reconnect", teardown=False)

    def test_failed_relaunch_restores_native_reconnect_and_scripts(self):
        self.window._triggernometry_reload_pending = True
        self.check_modal("_restart_for_update", "overflow", relaunch_error=OSError("exec failed"))
        generation = self.bridge.generation()
        self.assertFalse(self.window._triggernometry_retry_stopped)
        self.assertTrue(self.window._triggernometry_mode)
        self.assertTrue(self.window._triggernometry_reload_pending)
        self.window._ws.connect_to(self.url)
        self.wait_for(lambda: self.bridge.generation() > generation
                      and self.bridge.generation() in self.inventory)
        self.assertFalse(self.window._triggernometry_reload_pending)
        self.probe()
        self.wait_for(lambda: ("script computed 42", self.bridge.generation()) in self.scripts
                      and bool(self.peer.drawings))
        self.assertGreater(tts._queue.qsize(), 0)


if __name__ == "__main__":
    unittest.main()
