from contextlib import ExitStack, nullcontext
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from queue import Queue
import shutil
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("NYAA_REPLAY_TEST", "1")
from PyQt6.QtCore import QTimer
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QMessageBox

from nyaatriggers import app_common as ac, diagnostics, triggevent_bridge as tv, tts, updater
from nyaatriggers.telesto_client import TelestoClient
from nyaatriggers.triggevent_bridge import TriggeventBridge, _ByteQueue
from nyaatriggers.triggernometry_bridge import TriggernometryBridge
from tests import test_session_ui
from tests.engine_fixture import joined_bridge_workers


SINGLE_SHOT = QTimer.singleShot
NATIVE_AVAILABLE = (os.name == "posix" and shutil.which("java") and shutil.which("xvfb-run")
                    and TriggeventBridge.is_available())


class TeardownFixture:
    @classmethod
    def setUpClass(cls):
        test_session_ui.SessionUiTests.setUpClass()

    def setUp(self):
        with patch.object(TelestoClient, "start"), \
                patch.object(TriggeventBridge, "is_available", return_value=False), \
                patch.object(TriggernometryBridge, "is_available", return_value=False):
            test_session_ui.SessionUiTests.setUp(self)
        self.stack.enter_context(patch.object(QTimer, "singleShot", SINGLE_SHOT))
        self.stack.enter_context(patch.object(QApplication, "quit"))
        self.stack.enter_context(patch("nyaatriggers.triggevent_bridge._log"))
        self.stack.enter_context(patch("nyaatriggers.triggevent_recovery._log"))
        self.stack.enter_context(patch.object(tts, "_ensure_worker"))
        self.stack.enter_context(patch.object(tts, "_queue", Queue()))
        config = self.temp / "triggevent-settings"
        config.mkdir()
        self.stack.enter_context(patch.dict(os.environ, {
            "JAVA_TOOL_OPTIONS": (f'-Duser.home="{self.temp}" -Djava.io.tmpdir="{self.temp}" '
                                  f'-Dtriggevent_settings_dir="{config}"'),
            "_JAVA_OPTIONS": "", "JDK_JAVA_OPTIONS": "",
            "triggevent_settings_dir": str(config), "APPDATA": str(self.temp),
            "TMPDIR": str(self.temp), "NYAA_AUTOMARK": "0"}))
        self.window._find_iinact_log_dir = lambda: None
        self.bridge = self.window._ensure_triggevent_bridge()
        self.workers = self.stack.enter_context(joined_bridge_workers(tv, self.bridge))
        self.recovery = self.window._triggevent_recovery
        self.addCleanup(self.recovery._retry_timer.stop)
        self.addCleanup(self.recovery._progress_timer.stop)
        self.addCleanup(lambda: self.bridge.stop(wait=True))
        self.window._triggevent_mode = True
        # The feed is simulated, so deliver its matching disconnect notification.
        self.stack.enter_context(patch.object(self.window._ws, "disconnect_from",
                                             side_effect=self.window._ws._on_disconnected))
        self.window._ws.status_changed.emit(True, "Connected")
        for event in (
                {"type": "ChangeZone", "zoneID": 0x553, "zoneName": "Raid"},
                {"type": "ChangePrimaryPlayer", "charID": 0x10000001},
                {"type": "LogLine", "rawLine": "00|" + datetime.now(timezone.utc).isoformat()
                 + "|0038|Player|Anchor|0"}):
            self.window._ws._on_message(json.dumps(event))

    def wait_for(self, predicate, timeout=15):
        deadline = time.monotonic() + timeout
        while not predicate() and time.monotonic() < deadline:
            QTest.qWait(10)
        self.assertTrue(predicate())

    def save_warning(self, operation, action=lambda: None, *, duration=160,
                     relaunch_error=None, until=None):
        observed = []

        def warning(parent, title, text):
            if observed:
                return QMessageBox.StandardButton.Ok
            dialog = QMessageBox(QMessageBox.Icon.Warning, title, text,
                                 QMessageBox.StandardButton.Ok, parent)
            start = QTimer(dialog)
            start.setSingleShot(True)
            start.timeout.connect(action)
            start.start(0)
            deadline = time.monotonic() + duration / 1000
            poll = QTimer(dialog)

            def finish():
                finished = until is not None and until()
                if not finished and time.monotonic() < deadline:
                    return
                observed.append((self.recovery._retry_timer.isActive(),
                                 self.recovery._progress_timer.isActive()))
                dialog.accept()

            poll.timeout.connect(finish)
            poll.start(10)
            return dialog.exec()

        self.window._save_warned = False
        self.window._settings_save_timer.start()
        with patch("nyaatriggers.ui.settings_tab._atomic_write_json",
                   side_effect=PermissionError("Storage unavailable")), \
                patch.object(ac.QMessageBox, "warning", side_effect=warning), \
                patch.object(updater, "relaunch", side_effect=relaunch_error):
            operation()
        self.assertEqual(len(observed), 1)
        return observed[0]


class SimulatedTeardownCase(TeardownFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.starts = []
        self.stack.enter_context(patch.object(TriggeventBridge, "is_available", return_value=True))
        self.stack.enter_context(patch.object(self.bridge, "start", side_effect=self.boot))
        self.boot()
        self.starts.clear()

    def boot(self, complete=True):
        # Simulate native capability negotiation independently of installed Java.
        bridge = self.bridge
        bridge._active = True
        bridge._gen += 1
        bridge._recovery_gen = bridge._catchup_gen = bridge.generation()
        bridge._wq = _ByteQueue(100)
        self.starts.append(QApplication.activeModalWidget() is not None)
        bridge.status.emit(True, "Starting", bridge.generation())
        if complete and not self.recovery._lost_connection:
            bridge.ready.emit(bridge.generation())
            self.recovery._try_start(True)
            bridge._dispatch({"t": "recovery_checkpoint", "checkpoint": 1}, gen=bridge.generation())
            bridge._dispatch({"t": "recovered", "checkpoint": 2}, gen=bridge.generation())
            self.assertTrue(self.recovery._live)
            while not bridge._wq.empty():
                bridge._wq.get_nowait()

    def prepare(self, path):
        recovery, bridge = self.recovery, self.bridge
        recovery._retry_delay_ms = 0
        if path == "retry":
            bridge._active = False
            bridge.status.emit(False, "Sidecar exited", bridge.generation())
            return lambda: None
        if path == "late_exit":
            def exit_engine():
                bridge._active = False
                bridge.status.emit(False, "Sidecar exited", bridge.generation())
            return exit_engine
        if path == "timeout":
            bridge.stop()
            self.boot(complete=False)
            self.starts.clear()
            recovery._progress_timer.setInterval(0)
            return recovery._recovery_timed_out
        if path == "feed_overflow":
            return bridge._queue_overflow
        if path == "buffer_overflow":
            recovery._live = False
            self.stack.enter_context(patch("nyaatriggers.triggevent_recovery._MAX_PENDING_BYTES", 100))
            return lambda: recovery.feed(json.dumps({"type": "LogLine", "rawLine": "x" * 200}))
        if path == "reconnect":
            def reconnect():
                self.window._ws.status_changed.emit(False, "Disconnected")
                self.window._ws.status_changed.emit(True, "Connected")
            return reconnect
        if path == "startup":
            bridge.stop()
            return self.window._reconcile_triggevent_engine
        raise AssertionError(path)


class EngineTeardownTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        TeardownFixture.setUpClass()

    def test_shared_fixture_isolates_media_and_pull_recordings(self):
        for teardown in (False, True):
            with self.subTest(teardown=teardown), tempfile.TemporaryDirectory() as temp, ExitStack() as stack:
                external = Path(temp)
                for name in ("voices", "sounds"):
                    (external / name).mkdir()
                voice = external / "voices/private.onnx"
                sound = external / "sounds/private.wav"
                voice.write_bytes(b"external voice")
                voice.with_suffix(".onnx.json").write_text("{}")
                sound.write_bytes(b"external sound")
                stack.enter_context(patch.object(ac, "_BUNDLE_DIR", external))
                stack.enter_context(patch.object(ac, "_USER_VOICES_DIR", voice.parent))
                stack.enter_context(patch.object(ac, "_USER_SOUNDS_DIR", sound.parent))
                stack.enter_context(patch.object(test_session_ui.mw, "_DATA_DIR", external))
                stack.enter_context(patch.object(TelestoClient, "start"))
                stack.enter_context(patch.object(TriggeventBridge, "is_available", return_value=False))
                stack.enter_context(patch.object(TriggernometryBridge, "is_available", return_value=False))
                case = SimulatedTeardownCase() if teardown else unittest.TestCase()
                try:
                    (case.setUp if teardown else lambda: test_session_ui.SessionUiTests.setUp(case))()
                    window = case.window
                    self.assertEqual(window._voice_combo.findData(str(voice)), -1)
                    self.assertEqual(window._alert_sound_combo.findData(sound.name), -1)
                    capture = window._pull_capture
                    capture.set_recording(True)
                    capture.on_log_line("20|now|40000001|Boss")
                    capture.on_raw_message("fixture capture")
                    saved = capture._path
                    self.assertTrue(saved.is_relative_to(case.temp))
                finally:
                    self.assertTrue(case.doCleanups())
                self.assertFalse(saved.exists())
                self.assertFalse((external / "pull_logs").exists())
                self.assertEqual(voice.read_bytes(), b"external voice")
                self.assertEqual(sound.read_bytes(), b"external sound")

    def test_shared_fixture_isolates_caches_during_construction_and_cleanup(self):
        caches = ("_TRIGGEVENT_INVENTORY_CACHE", "_TRIGGEVENT_AUTOMARK_INVENTORY_CACHE",
                  "_TRIGGERNOMETRY_INVENTORY_CACHE", "_CALLOUTS_JA_CACHE")
        loaders = ("_load_cached_triggevent_inventory", "_load_cached_native_automark_inventory",
                   "_load_cached_triggernometry_inventory", "_load_cached_callouts_ja")
        for teardown in (False, True):
            with self.subTest(teardown=teardown), tempfile.TemporaryDirectory() as temp, ExitStack() as stack:
                external = {name: Path(temp) / (name + ".json") for name in caches}
                for name, path in external.items():
                    path.write_text("external cache")
                    stack.enter_context(patch.object(ac, name, path))
                reads = []
                read_text = Path.read_text

                def read(path, *args, **kwargs):
                    reads.append(path)
                    return read_text(path, *args, **kwargs)

                stack.enter_context(patch.object(Path, "read_text", read))
                stack.enter_context(patch.object(TelestoClient, "start"))
                stack.enter_context(patch.object(TriggeventBridge, "is_available", return_value=False))
                stack.enter_context(patch.object(TriggernometryBridge, "is_available", return_value=False))
                case = SimulatedTeardownCase() if teardown else unittest.TestCase()
                try:
                    (case.setUp if teardown else lambda: test_session_ui.SessionUiTests.setUp(case))()
                    isolated = [getattr(ac, name) for name in caches]
                    self.assertTrue(all(path.parent == case.temp for path in isolated))
                    self.assertTrue(all(path in reads for path in isolated))
                    reads.clear()
                    for method in loaders:
                        case.addCleanup(getattr(case.window, method))
                finally:
                    self.assertTrue(case.doCleanups())
                self.assertTrue(all(path in reads for path in isolated))
                self.assertTrue(set(external.values()).isdisjoint(reads))
                self.assertFalse(case.temp.exists())
                for path in external.values():
                    self.assertEqual(read_text(path), "external cache")

    @unittest.skipUnless(NATIVE_AVAILABLE, "Requires the Triggevent jar, Java and Xvfb")
    def test_native_fixture_joins_old_workers_before_restoring_storage(self):
        for fail in (False, True):
            with self.subTest(fail=fail), tempfile.TemporaryDirectory() as temp:
                external = Path(temp)
                config = external / "inherited-settings"
                config.mkdir()
                option = f'-Dtriggevent_settings_dir="{config}"'
                environment = {"triggevent_settings_dir": str(config), "APPDATA": str(config),
                               "JAVA_TOOL_OPTIONS": option, "_JAVA_OPTIONS": option,
                               "JDK_JAVA_OPTIONS": option}
                case = NativeEngineTeardownTests()
                pending, observed = {}, []
                log, join = tv._log, threading.Thread.join

                def join_worker(worker, *args, **kwargs):
                    if worker in pending:
                        pending[worker].set()
                    return join(worker, *args, **kwargs)

                with patch.dict(os.environ, environment), \
                        patch.object(diagnostics, "_LOG_FILE", external / "unexpected.log"), \
                        patch.object(diagnostics, "_ENABLED", True):
                    try:
                        case.setUp()
                        err_loop = case.bridge._err_loop
                        entered = threading.Event()

                        def delayed_stderr(*args):
                            err_loop(*args)
                            release = threading.Event()
                            pending[threading.current_thread()] = release
                            entered.set()
                            if not release.wait(20):
                                return
                            log("fixture worker finished")
                            diagnostics.record("engine_exit", reason="eof", expected=True)
                            observed.append((tv._log_path(), diagnostics._LOG_FILE,
                                             (case.temp / "diagnostics.log").read_text()))

                        case.stack.enter_context(patch.object(case.bridge, "_err_loop", delayed_stderr))
                        outcome = self.assertRaisesRegex(RuntimeError, "fixture failure") if fail else nullcontext()
                        with outcome:
                            for _ in range(2):
                                entered.clear()
                                case.bridge.start()
                                case.wait_for(lambda: case.recovery._live)
                                self.assertTrue(any((case.temp / "triggevent-settings").iterdir()))
                                case.bridge.stop(wait=True)
                                self.assertTrue(entered.wait(2))
                            self.assertEqual(len(pending), 2)
                            self.assertTrue(all(worker.is_alive() for worker in pending))
                            if fail:
                                raise RuntimeError("fixture failure")
                    finally:
                        with patch.object(threading.Thread, "join", join_worker):
                            self.assertTrue(case.doCleanups())
                    self.assertEqual(len(observed), 2)
                    self.assertFalse(any(worker.is_alive() for worker in case.workers))
                    for log_path, support_path, contents in observed:
                        self.assertEqual(log_path.parent, case.temp)
                        self.assertEqual(support_path.parent, case.temp)
                        self.assertIn('"event":"engine_exit"', contents)
                    self.assertFalse(case.temp.exists())
                    self.assertFalse((external / "unexpected.log").exists())
                    self.assertEqual(list(config.iterdir()), [])
                    for key, value in environment.items():
                        self.assertEqual(os.environ[key], value)

    def test_fixture_redirects_status_alias_and_diagnostics_through_cleanup(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            profile = root / "inaccessible-profile"
            profile.write_text("not a directory")
            case = SimulatedTeardownCase()
            observed = []
            with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(profile), "APPDATA": str(profile)}), \
                    patch.object(diagnostics, "_LOG_FILE", root / "unexpected-diagnostics.log"), \
                    patch.object(diagnostics, "_ENABLED", True):
                try:
                    case.setUp()
                    log = case.temp / "triggevent.log"
                    self.assertEqual(tv._log_path(), log)
                    self.assertIn("status: active=True Starting", log.read_text())
                    support_log = case.temp / "diagnostics.log"
                    self.assertEqual(diagnostics._LOG_FILE, support_log)
                    self.assertIn('"event":"recovery_state"', support_log.read_text())

                    def observe_cleanup():
                        observed.append(log.read_text())
                        observed.append(support_log.read_text())

                    case.stack.callback(observe_cleanup)
                finally:
                    self.assertTrue(case.doCleanups())
                self.assertIn("status: active=False Off", observed[0])
                self.assertIn('"event":"engine_stop"', observed[1])
                self.assertFalse(log.exists())
                self.assertFalse(support_log.exists())
                self.assertFalse((root / "unexpected-diagnostics.log").exists())

    def check_paths(self, operation):
        for path in ("retry", "late_exit", "timeout", "feed_overflow", "buffer_overflow", "reconnect", "startup"):
            with self.subTest(path=path):
                case = SimulatedTeardownCase()
                try:
                    case.setUp()
                    action = case.prepare(path)
                    timers = case.save_warning(getattr(case.window, operation), action)
                    ordinary = operation == "_flush_pending_settings_save"
                    self.assertEqual(case.starts, [True] if ordinary else [])
                    if not ordinary:
                        self.assertEqual(timers, (False, False))
                finally:
                    case.doCleanups()

    def test_update_restart_blocks_all_restart_paths_in_save_warning(self):
        self.check_paths("_restart_for_update")

    def test_windows_handoff_blocks_all_restart_paths_in_save_warning(self):
        self.check_paths("_quit_for_windows_handoff")

    def test_close_blocks_all_restart_paths_in_save_warning(self):
        self.check_paths("close")

    def test_ordinary_save_warning_keeps_recovery_active(self):
        self.check_paths("_flush_pending_settings_save")

    def test_queued_recovery_work_cannot_rearm_teardown_timers(self):
        for operation in ("_restart_for_update", "_quit_for_windows_handoff", "close"):
            for stage in ("history_retry", "catchup_retry", "checkpoint", "ready"):
                with self.subTest(operation=operation, stage=stage):
                    case = SimulatedTeardownCase()
                    try:
                        case.setUp()
                        bridge, recovery = case.bridge, case.recovery
                        bridge.stop()
                        case.boot(complete=False)
                        action = lambda: None
                        recover = case.stack.enter_context(patch.object(bridge, "recover", wraps=bridge.recover))
                        catch_up = case.stack.enter_context(patch.object(bridge, "catch_up", wraps=bridge.catch_up))
                        if stage == "ready":
                            recovery.feed(json.dumps({"type": "LogLine", "rawLine":
                                "00|2026-11-13T12:00:00Z|0038|Player|Anchor|0"}))
                            action = lambda: bridge.ready.emit(bridge.generation())
                        else:
                            if stage == "history_retry":
                                recover.side_effect = [False, True]
                            recovery._finish(bridge.generation(), None, "")
                            if stage == "catchup_retry":
                                catch_up.side_effect = [False, True]
                                recovery._on_progress({"t": "recovery_checkpoint", "checkpoint": 1}, bridge.generation())
                            elif stage == "checkpoint":
                                action = lambda: recovery._on_progress(
                                    {"t": "recovery_checkpoint", "checkpoint": 1}, bridge.generation())
                        counts = recover.call_count, catch_up.call_count
                        case.starts.clear()
                        timers = case.save_warning(getattr(case.window, operation), action)
                        self.assertEqual((recover.call_count, catch_up.call_count), counts)
                        self.assertEqual(case.starts, [])
                        self.assertEqual(timers, (False, False))
                    finally:
                        case.doCleanups()

    def test_failed_relaunch_restores_reconnect_and_manual_enable_recovery(self):
        for resume in ("reconnect", "enable"):
            with self.subTest(resume=resume):
                case = SimulatedTeardownCase()
                try:
                    case.setUp()
                    action = case.prepare("retry")
                    case.save_warning(case.window._restart_for_update, action,
                                      relaunch_error=OSError("Relaunch failed"))
                    self.assertEqual(case.starts, [])
                    self.assertFalse(tts._speech_suspended)
                    if resume == "reconnect":
                        case.window._ws.status_changed.emit(True, "Connected")
                    else:
                        case.window._set_triggevent_enabled(True)
                        self.assertTrue(case.bridge.is_active())
                        case.window._ws.status_changed.emit(True, "Connected")
                    self.assertTrue(case.recovery._live)
                    count = 1 if resume == "reconnect" else 2
                    self.assertEqual(case.starts, [False] * count)
                    case.prepare("late_exit")()
                    case.wait_for(lambda: len(case.starts) == count + 1)
                    self.assertTrue(case.recovery._live)
                    self.assertEqual(case.starts, [False] * (count + 1))
                finally:
                    case.doCleanups()

    def test_failed_windows_handoff_leaves_recovery_running(self):
        case = SimulatedTeardownCase()
        try:
            case.setUp()
            case.prepare("retry")
            with patch.object(updater, "install_kind", return_value="frozen-windows"):
                case.save_warning(lambda: case.window._handle_update_done(False, "Handoff failed"))
            self.assertEqual(case.starts, [True])
            self.assertTrue(case.recovery._live)
        finally:
            case.doCleanups()


@unittest.skipUnless(NATIVE_AVAILABLE, "Requires the Triggevent jar, Java and Xvfb")
class NativeEngineTeardownTests(TeardownFixture, unittest.TestCase):
    def check_native_retry(self, operation):
        bridge, recovery = self.bridge, self.recovery
        bridge.start()
        self.wait_for(lambda: recovery._live)
        calls, speech = [], []
        bridge.callout.connect(lambda *args: calls.append(args))
        bridge.tts.connect(lambda *args: speech.append(args))
        generation = bridge.generation()
        bridge._signal_group(bridge._proc, graceful=False)
        self.wait_for(lambda: recovery._retry_timer.isActive())
        recovery._retry_timer.setInterval(0)
        sent = []

        def probe():
            if not sent and recovery._live and bridge.generation() != generation:
                sent.append(True)
                raw = json.dumps({"type": "LogLine", "rawLine":
                    "20|" + datetime.now(timezone.utc).isoformat()
                    + "|40000001|Boss|C622|Light of Judgment|10000001|Player|5|100|100|0|0|0"})
                self.window._ws._on_message(raw)
                self.window._ws._on_message(raw)
            return len(speech) == 2

        with patch.object(bridge, "start", wraps=bridge.start) as start:
            self.save_warning(getattr(self.window, operation), duration=15000, until=probe)
        ordinary = operation == "_flush_pending_settings_save"
        self.assertEqual((start.call_count, len(calls), len(speech), tts._queue.qsize()),
                         (1, 2, 2, 2) if ordinary else (0, 0, 0, 0),
                         (sent, recovery._live, recovery._ready, recovery._loading,
                          recovery._lost_connection, recovery._checkpoint, generation, bridge.generation()))

    def test_native_retry_is_blocked_during_update_restart(self):
        self.check_native_retry("_restart_for_update")

    def test_native_retry_is_blocked_during_simulated_windows_handoff(self):
        self.check_native_retry("_quit_for_windows_handoff")

    def test_native_retry_and_repeated_callouts_survive_an_ordinary_save_warning(self):
        self.check_native_retry("_flush_pending_settings_save")


if __name__ == "__main__":
    unittest.main()
