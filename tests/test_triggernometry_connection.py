from contextlib import ExitStack
import errno
import json
import os
from pathlib import Path
import shutil
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
import xml.etree.ElementTree as ET

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt6.QtCore import QObject, QTimer
from PyQt6.QtNetwork import QHostAddress
from PyQt6.QtTest import QTest
from PyQt6.QtWebSockets import QWebSocketServer
from PyQt6.QtWidgets import QApplication, QMessageBox

from nyaatriggers import app_common as ac
from nyaatriggers import triggernometry_bridge as bridge
from nyaatriggers import updater
from nyaatriggers.dps_meter import DpsMeter
from nyaatriggers.telesto_client import TelestoClient
from nyaatriggers.timeline_engine import TimelineEngine
from nyaatriggers.triggevent_bridge import TriggeventBridge
from nyaatriggers.triggernometry_editor import PackDocument
from nyaatriggers.ui.connection import ConnectionMixin
from nyaatriggers.ui.engines import EnginesMixin
from nyaatriggers.ui.instance_tab import InstanceTabMixin
from nyaatriggers.ui.voice_tab import VoiceTabMixin
from nyaatriggers.ws_client import WSClient
from tests import test_session_ui

APP = QApplication.instance() or QApplication([])


class Host(QObject, EnginesMixin, InstanceTabMixin, VoiceTabMixin, ConnectionMixin):
    def __init__(self):
        super().__init__()
        self._ws = WSClient(self)
        self._ws.status_changed.connect(self._on_status_changed)
        self._triggernometry = None
        self._triggernometry_mode = False
        self._triggernometry_disabled = set()
        self._settings = {}
        self._current_zone = ""
        self._current_zone_id = 0
        self._connected = False
        self._umad_chain_enabled = False
        for name in ("_status_lbl", "_conn_btn", "_zone_lbl", "_plugin_link",
                     "_clear_status_timers", "_clear_seq_runners", "_clear_callout_dedup",
                     "_push_timeline_to_plugin", "_apply_engine_overrides",
                     "_update_triggernometry_pack_inventory",
                     "_on_engine_sidecar_status", "_umad_chain_reset", "_umad_gaze_reset",
                     "_automark_pairs"):
            setattr(self, name, Mock())
        self._zone_banner_text = lambda: ""
        self.inventory = []
        self._on_triggernometry_inventory = lambda *args: self.inventory.append(args)
        self.alerts = []
        self._emit_alert = lambda text, severity: self.alerts.append(text)
        self._localize_text = lambda text: text
        self._reading_for = lambda text: text
        self._callout_edits_for = lambda _source: {}
        self._triggers = []
        self._dps_meter = DpsMeter()
        self._actor_jobs = {}
        self._umad_actor_names = {}
        self._automark_pending = {}
        self._automark_active = {}


class RetryTests(unittest.TestCase):
    def test_teardown_preserves_enable_request_without_creating_a_bridge(self):
        for requested in (False, True):
            with self.subTest(requested=requested):
                host = Host()
                host._triggernometry_mode = requested
                host._triggernometry_reload_pending = True
                host._triggernometry_retry_stopped = True
                with patch.object(bridge.TriggernometryBridge, "is_available", return_value=False) as available, \
                        patch.object(host, "_has_triggernometry_packs") as packs:
                    host._set_triggernometry_enabled(True)
                available.assert_not_called()
                packs.assert_not_called()
                self.assertTrue(host._triggernometry_mode)
                self.assertTrue(host._triggernometry_reload_pending)
                self.assertIsNone(host._triggernometry)

    def test_teardown_still_allows_disable_and_cancels_pending_retry(self):
        host = Host()
        with patch.object(bridge, "_log"):
            native = host._ensure_triggernometry_bridge()
        timer = host._triggernometry_retry_timer
        self.addCleanup(timer.stop)
        host._triggernometry_mode = host._connected = True
        host._triggernometry_reload_pending = True
        timer.start(1000)
        host._triggernometry_retry_stopped = True
        with patch.object(native, "stop", wraps=native.stop) as stop:
            host._set_triggernometry_enabled(False)
        stop.assert_called_once_with()
        self.assertFalse(host._triggernometry_mode)
        self.assertFalse(timer.isActive())
        self.assertTrue(host._triggernometry_reload_pending)

    def test_enable_rollback_cancels_a_synchronously_scheduled_retry(self):
        host = Host()
        host._connected = True
        with patch.object(bridge, "_log"):
            native = host._ensure_triggernometry_bridge()
        timer = host._triggernometry_retry_timer
        self.addCleanup(timer.stop)
        self.assertFalse(host._triggernometry_mode)

        def failed_start():
            native.status.emit(False, "Failed to launch sidecar", native.generation())

        # Simulate native availability and a caught launch failure without spawning a host.
        with patch.object(bridge.TriggernometryBridge, "is_available", return_value=True), \
                patch.object(host, "_has_triggernometry_packs", return_value=True), \
                patch.object(native, "start", side_effect=failed_start), \
                patch.object(host._ws, "set_combatant_polling", side_effect=RuntimeError("Enable aborted")), \
                patch("sys.stderr"):
            host._set_triggernometry_enabled(True)
        self.assertFalse(host._triggernometry_mode)
        self.assertFalse(native.is_active())
        self.assertFalse(timer.isActive())

    def test_retry_backs_off_and_stale_or_cancelled_work_cannot_restart(self):
        host = Host()
        with patch.object(bridge, "_log"):
            native = host._ensure_triggernometry_bridge()
        host._triggernometry_mode = host._connected = True
        timer = host._triggernometry_retry_timer
        self.addCleanup(timer.stop)
        for delay in (1000, 2000, 4000, 8000, 16000, 30000, 30000):
            host._on_triggernometry_status(False, "Sidecar exited", native.generation())
            self.assertEqual(timer.interval(), delay)
            timer.stop()
        with patch.object(host, "_set_triggernometry_enabled") as enable:
            native._gen += 1
            host._retry_triggernometry_engine()
            enable.assert_not_called()
            host._on_triggernometry_status(False, "Sidecar exited", native.generation())
            host._connected = False
            host._retry_triggernometry_engine()
            enable.assert_not_called()
            host._connected = True
            host._triggernometry_mode = False
            host._retry_triggernometry_engine()
            enable.assert_not_called()
        host._on_triggernometry_status(True, "ready", native.generation())
        self.assertFalse(timer.isActive())
        self.assertEqual(host._triggernometry_retry_delay, 1000)

    def test_disable_and_disconnect_cancel_the_retry(self):
        host = Host()
        with patch.object(bridge, "_log"):
            native = host._ensure_triggernometry_bridge()
        host._triggernometry_mode = host._connected = True
        timer = host._triggernometry_retry_timer
        self.addCleanup(timer.stop)
        host._on_triggernometry_status(False, "Sidecar exited", native.generation())
        self.assertTrue(timer.isActive())
        host._set_triggernometry_enabled(False)
        self.assertFalse(timer.isActive())
        host._triggernometry_mode = True
        host._on_triggernometry_status(False, "Sidecar exited", native.generation())
        self.assertTrue(timer.isActive())
        host._on_triggernometry_connection(False, "Disconnected")
        self.assertFalse(timer.isActive())


class RetryShutdownTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        test_session_ui.SessionUiTests.setUpClass()

    def setUp(self):
        with patch.object(TelestoClient, "start"), \
                patch.object(TriggeventBridge, "is_available", return_value=False), \
                patch.object(bridge.TriggernometryBridge, "is_available", return_value=False):
            test_session_ui.SessionUiTests.setUp(self)
        self.stack.enter_context(patch.object(bridge, "_log"))
        self.native = self.window._ensure_triggernometry_bridge()
        self.addCleanup(self.window._triggernometry_retry_timer.stop)
        # Simulate a launchable host without spawning a native process.
        self.stack.enter_context(patch.object(bridge.TriggernometryBridge, "is_available",
                                             return_value=True))
        self.stack.enter_context(patch.object(self.window, "_has_triggernometry_packs",
                                             return_value=True))
        self.attempts = []
        self.stack.enter_context(patch.object(self.native, "start", side_effect=lambda:
            self.attempts.append(QApplication.activeModalWidget() is not None)))
        test_session_ui.SessionUiTests.connect(self)
        self.window._triggernometry_mode = True
        self.native.status.emit(False, "Sidecar exited", self.native.generation())
        self.assertTrue(self.window._triggernometry_retry_timer.isActive())

    def check_save_warning(self, operation, *, cancelled, late_status=False,
                           reconnect=False, overflow=False, relaunch_error=None):
        window = self.window
        retry = window._triggernometry_retry_timer
        if late_status:
            retry.stop()
        observed = []
        if reconnect or overflow:
            def failed_start():
                self.attempts.append(QApplication.activeModalWidget() is not None)
                self.native.status.emit(False, "Failed to launch sidecar", self.native.generation())
            self.native.start.side_effect = failed_start

        def warning(parent, title, text):
            if observed:
                return QMessageBox.StandardButton.Ok
            dialog = QMessageBox(QMessageBox.Icon.Warning, title, text,
                                 QMessageBox.StandardButton.Ok, parent)

            def report_crash():
                if reconnect:
                    window._ws.status_changed.emit(False, "Disconnected")
                    window._ws.status_changed.emit(True, "Connected")
                if overflow:
                    window._on_triggernometry_overflow(self.native.generation())
                if late_status:
                    self.native.status.emit(False, "Sidecar exited", self.native.generation())
                observed.append((window._connected, retry.isActive()))
                # Make an uncancelled retry due during the nested modal event loop.
                retry.setInterval(0)
                close.start(50)

            status = QTimer(dialog)
            status.setSingleShot(True)
            status.timeout.connect(report_crash)
            status.start(0)
            close = QTimer(dialog)
            close.setSingleShot(True)
            close.timeout.connect(dialog.accept)
            return dialog.exec()

        window._save_warned = False
        window._settings_save_timer.start()
        with patch("nyaatriggers.ui.settings_tab._atomic_write_json",
                   side_effect=PermissionError("Storage unavailable")), \
                patch.object(ac.QMessageBox, "warning", side_effect=warning), \
                patch.object(updater, "relaunch", side_effect=relaunch_error):
            getattr(window, operation)()
        expected = [True] if (reconnect or overflow) and not cancelled else []
        if not cancelled:
            expected.append(True)
        self.assertEqual(self.attempts, expected)
        self.assertEqual(observed, [(True, not cancelled)])
        self.assertEqual(retry.isActive(), (reconnect or overflow) and not cancelled)

    def test_update_restart_cancels_retry_before_failed_settings_save(self):
        self.check_save_warning("_restart_for_update", cancelled=True)

    def test_windows_handoff_cancels_retry_before_failed_settings_save(self):
        self.check_save_warning("_quit_for_windows_handoff", cancelled=True)

    def test_windows_handoff_cancels_retry_before_processing_pending_events(self):
        self.window._triggernometry_retry_timer.setInterval(0)
        self.check_save_warning("_quit_for_windows_handoff", cancelled=True)

    def test_pending_retry_runs_during_an_ordinary_settings_save_warning(self):
        self.check_save_warning("_flush_pending_settings_save", cancelled=False)

    def test_update_restart_rejects_crash_status_during_failed_settings_save(self):
        self.check_save_warning("_restart_for_update", cancelled=True, late_status=True)

    def test_windows_handoff_rejects_crash_status_during_failed_settings_save(self):
        self.check_save_warning("_quit_for_windows_handoff", cancelled=True, late_status=True)

    def test_crash_status_retries_during_an_ordinary_settings_save_warning(self):
        self.check_save_warning("_flush_pending_settings_save", cancelled=False, late_status=True)

    def test_update_restart_keeps_retries_suspended_after_a_modal_reconnect(self):
        self.check_save_warning("_restart_for_update", cancelled=True, reconnect=True)

    def test_windows_handoff_keeps_retries_suspended_after_a_modal_reconnect(self):
        self.check_save_warning("_quit_for_windows_handoff", cancelled=True, reconnect=True)

    def test_update_restart_rejects_overflow_during_save_warning(self):
        self.check_save_warning("_restart_for_update", cancelled=True, overflow=True)

    def test_windows_handoff_rejects_overflow_during_save_warning(self):
        self.check_save_warning("_quit_for_windows_handoff", cancelled=True, overflow=True)

    def test_close_rejects_reconnect_during_save_warning(self):
        self.check_save_warning("close", cancelled=True, reconnect=True)

    def test_close_rejects_overflow_during_save_warning(self):
        self.check_save_warning("close", cancelled=True, overflow=True)

    def test_ordinary_save_warning_preserves_retry_after_a_reconnect(self):
        self.check_save_warning("_flush_pending_settings_save", cancelled=False, reconnect=True)

    def test_ordinary_save_warning_preserves_retry_after_overflow(self):
        self.check_save_warning("_flush_pending_settings_save", cancelled=False, overflow=True)

    def test_teardown_preserves_pending_pack_reload_until_recovery(self):
        window = self.window
        window._triggernometry_reload_pending = True
        window._triggers_enabled = True
        window._cactbot_mode = window._in_game_combat = False
        window._stop_background_timers()
        window._apply_pending_triggernometry_packs()
        self.assertTrue(window._triggernometry_reload_pending)
        self.assertTrue(window._triggernometry_mode)
        self.assertFalse(self.attempts)
        self.assertFalse(window._triggernometry_editor_status.isHidden())
        window._triggernometry_retry_stopped = False
        window._apply_pending_triggernometry_packs()
        self.assertFalse(window._triggernometry_reload_pending)
        self.assertTrue(window._triggernometry_mode)
        self.assertEqual(self.attempts, [False])
        self.assertTrue(window._triggernometry_editor_status.isHidden())

    def test_reconnect_after_aborted_restart_restores_automatic_retries(self):
        self.check_save_warning("_restart_for_update", cancelled=True, late_status=True,
                                relaunch_error=OSError("exec failed"))
        self.window._ws.status_changed.emit(True, "Connected")
        self.assertEqual(self.attempts, [False])
        self.native.status.emit(False, "Sidecar exited", self.native.generation())
        retry = self.window._triggernometry_retry_timer
        self.assertTrue(retry.isActive())
        retry.setInterval(0)
        QTest.qWait(10)
        self.assertEqual(self.attempts, [False, False])

    def test_enable_after_aborted_restart_restores_automatic_retries(self):
        self.check_save_warning("_restart_for_update", cancelled=True, late_status=True,
                                relaunch_error=OSError("exec failed"))
        self.window._connected = True
        self.window._set_triggernometry_enabled(True)
        self.assertEqual(self.attempts, [False])
        self.native.status.emit(False, "Sidecar exited", self.native.generation())
        retry = self.window._triggernometry_retry_timer
        self.assertTrue(retry.isActive())
        retry.setInterval(0)
        QTest.qWait(10)
        self.assertEqual(self.attempts, [False, False])


@unittest.skipUnless(os.name == "posix" and shutil.which("mono") and shutil.which("xvfb-run"),
                     "Requires Mono and Xvfb")
class ConnectionTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        packs, runtime = root / "packs", root / "runtime"
        packs.mkdir()
        runtime.mkdir()
        document = PackDocument(packs / "delayed.xml")
        trigger = document.add_trigger()
        trigger.set("RegularExpression", r"TEST (?<label>\w+)")
        actions = trigger.find("Actions")
        ET.SubElement(actions, "Action", OrderNumber="1", ActionType="UseTTS", Asynchronous="False",
                      UseTTSTextExpression="armed ${label} ${_incombat}")
        ET.SubElement(actions, "Action", OrderNumber="2", ActionType="Placeholder", Asynchronous="False",
                      ExecutionDelayExpression="1200")
        ET.SubElement(actions, "Action", OrderNumber="3", ActionType="UseTTS", Asynchronous="False",
                      UseTTSTextExpression="late ${label}")
        document.save()
        self.stack.enter_context(patch.object(bridge, "_rundata_dir", return_value=runtime))
        self.stack.enter_context(patch.object(bridge, "_packs_dir", return_value=packs))
        self.stack.enter_context(patch.object(bridge, "_make_bundled_mono_executable"))
        self.stack.enter_context(patch.object(bridge, "_log"))
        self.host = Host()
        self.speech = []
        self.stack.enter_context(patch("nyaatriggers.ui.engines.speak", side_effect=
                                      lambda text, **kwargs: self.speech.append(text)))
        self.server = QWebSocketServer("test", QWebSocketServer.SslMode.NonSecureMode)
        self.assertTrue(self.server.listen(QHostAddress(QHostAddress.SpecialAddress.LocalHost), 0))
        self.peers = []
        self.server.newConnection.connect(lambda: self.peers.append(self.server.nextPendingConnection()))
        self.addCleanup(self.shutdown)

    def shutdown(self):
        self.host._ws.disconnect_from()
        if self.host._triggernometry:
            self.host._triggernometry.stop(wait=True)
        for peer in self.peers:
            peer.close()
        self.server.close()
        QTest.qWait(50)

    def wait(self, predicate):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not predicate():
            QTest.qWait(10)
        self.assertTrue(predicate(), self.speech)

    def connect(self):
        previous = len(self.peers)
        self.host._ws.connect_to(f"ws://127.0.0.1:{self.server.serverPort()}/ws")
        self.wait(lambda: self.host._connected and len(self.peers) > previous)

    def send(self, data):
        self.peers[-1].sendTextMessage(json.dumps(data))

    def probe(self, label, combat=0):
        raw = f"00|2026-09-25T12:00:00.0000000-05:00|0038|Tester|TEST {label}|checksum"
        self.send({"type": "LogLine", "rawLine": raw})
        self.wait(lambda: f"armed {label} {combat}" in self.speech)

    def enable(self):
        previous = len(self.host.inventory)
        self.host._set_triggernometry_enabled(True)
        self.wait(lambda: len(self.host.inventory) > previous)

    def check_synchronous_launch_recovery(self, failure, *, reload=False):
        self.connect()
        self.assertFalse(self.host._triggernometry_mode)
        if reload:
            self.enable()
            self.host._triggers_enabled = True
            self.host._triggernometry_reload_pending = True
            self.host._in_game_combat = self.host._cactbot_mode = False
        self.send({"type": "InCombat", "inACTCombat": True, "inGameCombat": False})
        self.wait(lambda: bool(self.host._ws.state_snapshot()))
        native = self.host._ensure_triggernometry_bridge()
        timer = self.host._triggernometry_retry_timer
        self.addCleanup(timer.stop)
        statuses = []
        native.status.connect(lambda *args: statuses.append(args))
        previous = len(self.host.inventory)
        generation = native.generation()
        target, method = {
            "runtime": (bridge, "_rundata_dir"),
            "listener": (bridge.TriggernometryTelesto, "start"),
            "spawn": (bridge.subprocess, "Popen"),
        }[failure]
        with patch.object(native, "start", wraps=native.start) as start:
            with patch.object(target, method, side_effect=BlockingIOError(
                    errno.EAGAIN, "Launch resource temporarily unavailable")) as fault:
                if reload:
                    self.host._apply_pending_triggernometry_packs()
                else:
                    self.host._set_triggernometry_enabled(True)
                fault.assert_called_once()
                self.assertEqual(start.call_count, 1)
                self.assertTrue(self.host._triggernometry_mode)
                self.assertTrue(self.host._connected)
                self.assertFalse(native.is_active())
                self.assertTrue(timer.isActive())
                self.assertEqual(timer.interval(), 1000)
                self.assertEqual(self.host._triggernometry_retry_delay, 2000)
            self.wait(lambda: len(self.host.inventory) > previous
                      and any(active and message == "ready" for active, message, _gen in statuses))
            self.assertEqual(start.call_count, 2)
        self.assertTrue(native.is_active())
        self.assertGreater(native.generation(), generation)
        self.assertFalse(timer.isActive())
        self.assertEqual(self.host._triggernometry_retry_delay, 1000)
        self.assertTrue(self.host._connected)
        self.assertEqual(len(self.peers), 1)
        self.probe("recovered", combat=1)

    def test_first_enable_recovers_after_a_transient_runtime_directory_failure(self):
        self.check_synchronous_launch_recovery("runtime")

    def test_first_enable_recovers_after_a_transient_callback_listener_failure(self):
        self.check_synchronous_launch_recovery("listener")

    def test_first_enable_recovers_after_a_transient_process_spawn_failure(self):
        self.check_synchronous_launch_recovery("spawn")

    def test_pack_reload_recovers_after_a_transient_process_spawn_failure(self):
        self.check_synchronous_launch_recovery("spawn", reload=True)

    def test_crashed_engine_restarts_on_a_healthy_feed_and_restores_combat_state(self):
        self.connect()
        self.enable()
        self.send({"type": "InCombat", "inACTCombat": True, "inGameCombat": True})
        self.probe("before", combat=1)
        native = self.host._triggernometry
        generation = native.generation()
        os.killpg(native._proc.pid, 9)
        self.wait(lambda: native.generation() > generation and len(self.host.inventory) == 2)
        self.assertTrue(self.host._connected)
        self.assertEqual(len(self.peers), 1)
        self.probe("after", combat=1)
        self.assertNotIn("late before", self.speech)

    def check_external_pack_reimport(self, use_copy):
        self.connect()
        self.enable()
        self.host._triggers_enabled = True
        self.host._local_ids = set()
        managed = bridge.packs_dir() / "delayed.xml"
        source = managed.parent.parent / "copy.xml" if use_copy else managed
        original = managed.read_bytes()
        if use_copy:
            source.write_bytes(original)
        self.stack.enter_context(patch.object(ac.QFileDialog, "getOpenFileName",
                                             return_value=(str(source), "")))
        information = self.stack.enter_context(patch.object(ac.QMessageBox, "information"))
        critical = self.stack.enter_context(patch.object(ac.QMessageBox, "critical"))
        warning = self.stack.enter_context(patch.object(ac.QMessageBox, "warning"))
        question = self.stack.enter_context(patch.object(ac.QMessageBox, "question"))
        generation = self.host._triggernometry.generation()
        self.probe("before")
        self.host._import_triggernometry()
        self.assertEqual(self.host._triggernometry.generation(), generation)
        self.wait(lambda: "late before" in self.speech)

        modified = original.replace(b"armed ${label}", b"fresh ${label}")
        stamp = managed.stat()
        managed.write_bytes(modified)
        os.utime(managed, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        if use_copy:
            source.write_bytes(modified)
        self.host._import_triggernometry()
        self.assertGreater(self.host._triggernometry.generation(), generation)
        self.wait(lambda: len(self.host.inventory) == 2)
        self.send({"type": "LogLine", "rawLine":
                   "00|2026-09-26T12:00:00-05:00|0038|Tester|TEST after|checksum"})
        self.wait(lambda: "fresh after 0" in self.speech)
        self.assertNotIn("armed after 0", self.speech)

        generation = self.host._triggernometry.generation()
        self.host._import_triggernometry()
        self.assertEqual(self.host._triggernometry.generation(), generation)
        self.wait(lambda: "late after" in self.speech)
        self.assertIn("is now running", information.call_args.args[-1])
        critical.assert_not_called()
        warning.assert_not_called()
        question.assert_not_called()

    def test_reimport_reloads_a_pack_edited_in_place(self):
        self.check_external_pack_reimport(use_copy=False)

    def test_reimport_reloads_an_external_copy_matching_the_edited_managed_pack(self):
        self.check_external_pack_reimport(use_copy=True)

    def check_pack_reload_after_combat(self, use_import):
        self.connect()
        self.enable()
        self.host._triggers_enabled = True
        self.host._local_ids = set()
        self.host._cactbot_mode = False
        self.host._local_enabled = False
        self.host._in_game_combat = False
        self.host._awaiting_zone_metadata = True
        self.host._pending_timeline_events = []
        self.host._timeline_reset_on_combat_end = False
        self.host._timeline = TimelineEngine(self.host)
        self.host._ws.in_combat.connect(self.host._on_in_combat)
        self.send({"type": "InCombat", "inACTCombat": True, "inGameCombat": True})
        self.wait(lambda: self.host._in_game_combat)
        self.probe("before", combat=1)
        generation = self.host._triggernometry.generation()
        managed = bridge.packs_dir() / "delayed.xml"
        original = managed.read_bytes()
        source = managed.parent.parent / "updated.xml" if use_import else managed
        updated = original.replace(b"armed ${label}", b"fresh ${label}")
        source.write_bytes(updated)
        with patch.object(ac.QFileDialog, "getOpenFileName", return_value=(str(source), "")), \
                patch.object(ac.QMessageBox, "information") as information, \
                patch.object(ac.QMessageBox, "question", return_value=ac.QMessageBox.StandardButton.Yes):
            self.host._import_triggernometry()
        self.wait(lambda: "late before" in self.speech)
        self.assertEqual(managed.read_bytes(), updated)
        if use_import:
            self.assertEqual(managed.with_suffix(".xml.bak").read_bytes(), original)
        self.assertEqual(self.host._triggernometry.generation(), generation)
        self.assertTrue(self.host._triggernometry_reload_pending)
        self.assertNotIn("is now running", information.call_args.args[-1])
        self.send({"type": "InCombat", "inACTCombat": False, "inGameCombat": False})
        self.wait(lambda: len(self.host.inventory) == 2)
        self.assertFalse(self.host._triggernometry_reload_pending)
        self.assertGreater(self.host._triggernometry.generation(), generation)
        self.send({"type": "LogLine", "rawLine":
                   "00|2026-09-26T12:00:00-05:00|0038|Tester|TEST after|checksum"})
        self.wait(lambda: "fresh after 0" in self.speech)
        self.wait(lambda: "late after" in self.speech)
        self.assertNotIn("armed after 0", self.speech)

    def test_changed_import_preserves_pending_actions_until_combat_ends(self):
        self.check_pack_reload_after_combat(use_import=True)

    def test_changed_managed_pack_preserves_pending_actions_until_combat_ends(self):
        self.check_pack_reload_after_combat(use_import=False)

    def test_disconnect_cancels_pending_speech_and_reconnect_starts_fresh(self):
        self.connect()
        self.enable()
        self.probe("old")
        generation = self.host._triggernometry.generation()
        self.host._toggle_connection()
        self.wait(lambda: not self.host._connected)
        self.assertFalse(self.host._triggernometry.is_active())
        self.assertGreater(self.host._triggernometry.generation(), generation)
        QTest.qWait(1400)
        self.assertNotIn("late old", self.speech)
        self.assertNotIn("late old", self.host.alerts)
        self.connect()
        self.wait(lambda: len(self.host.inventory) == 2)
        self.probe("fresh")
        self.wait(lambda: "late fresh" in self.speech)
        self.assertNotIn("late old", self.speech)
        self.host._set_triggernometry_enabled(False)
        self.host._toggle_connection()
        self.wait(lambda: not self.host._connected)
        self.connect()
        self.assertFalse(self.host._triggernometry.is_active())

    def test_combat_state_is_replayed_when_enabled_midfight(self):
        self.connect()
        self.send({"type": "InCombat", "inACTCombat": True, "inGameCombat": False})
        self.wait(lambda: bool(self.host._ws.state_snapshot()))
        self.enable()
        self.probe("midfight", combat=1)
        self.send({"type": "InCombat", "inACTCombat": False, "inGameCombat": True})
        self.probe("ended", combat=0)


if __name__ == "__main__":
    unittest.main()
