"""Native reader output must respect checkbox changes before GUI delivery."""

import json
import os
import subprocess
import sys
import time
from queue import Queue
import threading
import unittest
from unittest.mock import Mock, patch

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QMessageBox

from nyaatriggers import app_common as ac
from nyaatriggers import tts
from nyaatriggers.triggevent_bridge import TriggeventBridge
from nyaatriggers.trigger_engine import Trigger
from nyaatriggers.trigger_profiles import capture_profile
from tests import test_session_ui as session_ui


_ACTUAL_WARNING = QMessageBox.warning


class QueuedCalloutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        session_ui.SessionUiTests.setUpClass()
        cls.app = session_ui.SessionUiTests.app

    def setUp(self):
        session_ui.SessionUiTests.setUp(self)
        self.stack.enter_context(patch.object(TriggeventBridge, "is_available", return_value=True))
        self.bridge = TriggeventBridge(self.window)
        self.window._triggevent = self.bridge
        self.window._triggevent_mode = True
        self.window._connected = True
        self.bridge._active = True
        self.bridge._gen = 7
        self.bridge._handle_diagnostic("reading WS messages on stdin; speech_cancel=1", 7)
        self.bridge.callout.connect(self.window._on_triggevent_callout)
        self.bridge.tts.connect(self.window._on_triggevent_tts)
        self.speech, self.alerts, self.reader_threads, self.delivery_threads = [], [], [], []
        self.gui_thread = threading.get_ident()
        self.window._triggevent_speak = self._speak
        self.window._emit_alert = lambda *args: self.alerts.append(args)

    def _speak(self, text):
        self.speech.append(text)
        self.delivery_threads.append(threading.get_ident())

    def read(self, *frames):
        def run():
            self.reader_threads.append(threading.get_ident())
            for frame in frames:
                self.bridge._dispatch(frame, gen=7)

        worker = threading.Thread(target=run)
        worker.start()
        worker.join(timeout=2)
        self.assertFalse(worker.is_alive())

    @staticmethod
    def frame(text, cid="delayed", delayed=True):
        return {"t": "callout", "id": cid, "text": None if delayed else text,
                "tts": text, "tts_only": delayed}

    def test_actual_checkbox_save_cannot_release_queued_native_speech(self):
        self.window._table.blockSignals(True)
        self.window._append_engine_row({"source": "triggevent", "id": "delayed", "name": "Probe"})
        self.window._table.blockSignals(False)
        item = self.window._table.item(self.window._table.rowCount() - 1, ac._C_EN)
        self.window._table.setCurrentItem(item)

        def save_while_reader_emits():
            self.read(self.frame("Old speech"))

        with patch.object(self.window, "_save_settings", save_while_reader_emits):
            QTest.keyClick(self.window._table, Qt.Key.Key_Space)
        self.assertEqual(item.checkState(), Qt.CheckState.Unchecked)
        self.assertIn("delayed", self.bridge._disabled)
        self.assertEqual(self.speech, [])
        self.app.processEvents()
        self.assertEqual(self.speech, [])

    def test_acknowledgements_preserve_reader_order_and_fresh_gui_delivery(self):
        self.read(self.frame("Before disable"))
        self.bridge.set_disabled(["delayed"])
        self.bridge.set_disabled([])
        first = json.loads(list(self.bridge._wq.queue)[-1])["token"]
        self.read({"t": "speech_canceled", "token": first}, self.frame("Between toggles"))
        self.bridge.set_disabled(["delayed"])
        self.bridge.set_disabled([])
        latest = json.loads(list(self.bridge._wq.queue)[-1])["token"]
        self.read({"t": "speech_canceled", "token": latest},
                  self.frame("Fresh speech"), self.frame("Other source", "other"))
        self.assertEqual(self.speech, [])
        self.app.processEvents()
        self.assertEqual(self.speech, ["Fresh speech", "Other source"])
        self.assertTrue(all(thread != self.gui_thread for thread in self.reader_threads))
        self.assertEqual(self.delivery_threads, [self.gui_thread, self.gui_thread])
        self.assertEqual(self.bridge.thread(), self.window.thread())

    def failed_save(self, action):
        settings = ac._SETTINGS_FILE
        settings.unlink(missing_ok=True)
        settings.mkdir()
        self.window._save_warned = False
        modal_states = []
        timer = QTimer()
        ticks = []

        def inside_warning():
            dialog = QApplication.activeModalWidget()
            if not isinstance(dialog, QMessageBox):
                return
            ticks.append(True)
            if len(ticks) == 1:
                modal_states.append(frozenset(self.bridge._disabled))
                self.read(self.frame("Stale speech"))
            else:
                dialog.accept()
                timer.stop()

        timer.timeout.connect(inside_warning)
        try:
            with patch.object(ac.QMessageBox, "warning", _ACTUAL_WARNING):
                timer.start(10)
                action()
        finally:
            timer.stop()
            settings.rmdir()
        self.assertTrue(modal_states, "The real settings failure must open its warning")
        self.app.processEvents()
        return modal_states

    def test_checkbox_disables_before_actual_save_failure_modal(self):
        self.window._table.blockSignals(True)
        self.window._append_engine_row({"source": "triggevent", "id": "delayed", "name": "Probe"})
        self.window._table.blockSignals(False)
        item = self.window._table.item(self.window._table.rowCount() - 1, ac._C_EN)
        self.window._table.setCurrentItem(item)
        states = self.failed_save(lambda: QTest.keyClick(self.window._table, Qt.Key.Key_Space))
        self.assertEqual(item.checkState(), Qt.CheckState.Unchecked)
        self.assertIn("delayed", states[0])
        self.assertEqual(self.speech, [])

    def test_bulk_disable_applies_before_actual_save_failure_modal(self):
        for operation in ("fight", "global", "reset"):
            with self.subTest(operation=operation):
                self.window._engine_inventory = [{"source": "triggevent", "id": "delayed",
                                                  "fight": "Probe duty", "text": "Visual"}]
                self.window._triggevent_disabled.clear()
                self.bridge._disabled = frozenset()
                self.bridge._speech_cancel_pending.clear()
                self.window._global_tv_on_flag = True
                actions = {"fight": lambda: self.window._set_fight_tv("Probe duty", False),
                           "global": self.window._toggle_global_tv,
                           "reset": self.window._reset_all_to_default}
                states = self.failed_save(actions[operation])
                self.assertIn("delayed", states[0])
                self.assertEqual(self.speech, [])

    def test_changed_edit_and_reset_cancel_queued_speech_before_failed_save(self):
        self.window._engine_inventory = [{"source": "triggevent", "id": "delayed",
                                          "text": "Visual text", "tts": "Native speech"}]
        self.read(self.frame("Native speech"))
        self.failed_save(lambda: self.window._set_triggevent_callout_edit("delayed", "Edited speech"))
        self.assertEqual(self.speech, [])
        token = json.loads(list(self.bridge._wq.queue)[-1])["token"]
        self.read({"t": "speech_canceled", "token": token}, self.frame("Edited speech"))
        self.app.processEvents()
        self.assertEqual(self.speech, ["Edited speech"])
        self.speech.clear()
        self.read(self.frame("Edited speech"))
        self.failed_save(lambda: self.window._reset_triggevent_callout_edit("delayed"))
        self.assertEqual(self.speech, [])

    def test_unchanged_edits_preserve_native_shipped_user_and_silent_speech(self):
        for source in ("native", "shipped", "user", "silent"):
            with self.subTest(source=source):
                text = "" if source == "silent" else "Known speech"
                self.window._engine_inventory = [{"source": "triggevent", "id": "delayed",
                                                  "text": "Different visual", "tts": text}]
                self.window._triggevent_callout_edits.clear()
                self.window._shipped_callout_defaults["triggevent"] = {}
                if source == "shipped":
                    self.window._shipped_callout_defaults["triggevent"]["delayed"] = text
                if source == "user":
                    self.window._triggevent_callout_edits["delayed"] = text
                self.read(self.frame("Pending work"))
                self.window._set_triggevent_callout_edit("delayed", text)
                self.app.processEvents()
                self.assertFalse(self.bridge._speech_cancel_pending)
                self.assertEqual(self.speech, ["Pending work"])
                self.speech.clear()
                self.read(self.frame("Pending reset"))
                self.window._reset_triggevent_callout_edit("delayed")
                self.app.processEvents()
                self.assertFalse(self.bridge._speech_cancel_pending)
                self.assertEqual(self.speech, ["Pending reset"])
                self.speech.clear()

    def test_legacy_inventory_visual_text_is_not_assumed_to_be_native_speech(self):
        self.window._engine_inventory = [{"source": "triggevent", "id": "delayed", "text": "Visual"}]
        self.read(self.frame("Actual native speech"))
        self.window._set_triggevent_callout_edit("delayed", "Visual")
        self.app.processEvents()
        self.assertEqual(self.speech, [])
        self.assertIn("delayed", self.bridge._speech_cancel_pending)

    def test_optional_native_speech_metadata_survives_inventory_cache(self):
        items = [{"id": "spoken", "text": "Visual", "tts": "Speech"},
                 {"id": "silent", "text": "Visual", "tts": ""},
                 {"id": "legacy", "text": "Visual"}]
        with patch.object(ac, "_TRIGGEVENT_INVENTORY_CACHE", self.temp / "inventory.json"):
            self.window._on_triggevent_inventory(json.dumps(items), 7)
            loaded = {e["id"]: e for e in self.window._engine_inventory}
            self.assertEqual(loaded["spoken"]["tts"], "Speech")
            self.assertEqual(loaded["silent"]["tts"], "")
            self.assertNotIn("tts", loaded["legacy"])
            self.window._engine_inventory = []
            self.window._load_cached_triggevent_inventory()
            self.assertEqual({e["id"]: e for e in self.window._engine_inventory}, loaded)

    def test_successful_profile_text_change_rejects_speech_queued_during_commit(self):
        self.window._engine_inventory = [{"source": "triggevent", "id": "delayed",
                                          "text": "Visual", "tts": "Native speech"}]
        profile = capture_profile(self.window, "Edited speech profile")
        profile["engines"]["triggevent"]["delayed"]["text"] = "Profile speech"
        save = self.window._save_settings
        emitted = []

        def save_while_reader_emits():
            if not emitted:
                emitted.append(True)
                self.read(self.frame("Native speech"))
            return save()

        with patch.object(self.window, "_save_settings", save_while_reader_emits):
            self.assertTrue(self.window._activate_profile(profile))
        self.app.processEvents()
        self.assertEqual(self.speech, [])
        self.assertEqual(self.window._triggevent_callout_edits["delayed"], "Profile speech")

    def test_profile_noop_effective_speech_preserves_pending_work(self):
        for source in ("native", "shipped", "user", "silent"):
            with self.subTest(source=source):
                text = "" if source == "silent" else "Known speech"
                self.window._engine_inventory = [{"source": "triggevent", "id": "delayed",
                                                  "text": "Visual", "tts": text}]
                self.window._triggevent_callout_edits.clear()
                self.window._shipped_callout_defaults["triggevent"] = {}
                if source == "shipped":
                    self.window._shipped_callout_defaults["triggevent"]["delayed"] = text
                if source == "user":
                    self.window._triggevent_callout_edits["delayed"] = text
                profile = capture_profile(self.window, "Same effective speech")
                profile["engines"]["triggevent"]["delayed"]["text"] = text
                self.read(self.frame("Pending work"))
                self.assertTrue(self.window._activate_profile(profile))
                self.app.processEvents()
                self.assertNotIn("delayed", self.bridge._speech_cancel_pending)
                self.assertEqual(self.speech, ["Pending work"])
                self.speech.clear()

    def test_failed_profile_commit_keeps_previous_pending_native_speech(self):
        self.window._engine_inventory = [{"source": "triggevent", "id": "delayed",
                                          "text": "Visual", "tts": "Native speech"}]
        profile = capture_profile(self.window, "Failed speech profile")
        profile["engines"]["triggevent"]["delayed"]["text"] = "Profile speech"
        save = self.window._save_settings
        attempts = []

        def fail_first_save():
            attempts.append(True)
            return False if len(attempts) == 1 else save()

        self.read(self.frame("Native speech"))
        with patch.object(self.window, "_save_settings", fail_first_save), \
                patch.object(self.bridge, "set_callout") as changed, \
                patch.object(self.bridge, "reset_callout") as reset:
            self.assertFalse(self.window._activate_profile(profile))
        changed.assert_not_called()
        reset.assert_not_called()
        self.app.processEvents()
        self.assertEqual(self.speech, ["Native speech"])
        self.assertFalse(self.bridge._speech_cancel_pending)
        self.assertNotIn("delayed", self.window._triggevent_callout_edits)

    def test_profile_without_native_speech_metadata_cancels_only_its_changed_ids(self):
        self.window._engine_inventory = [{"source": "triggevent", "id": "delayed", "text": "Visual"}]
        profile = capture_profile(self.window, "Legacy speech profile")
        profile["engines"]["triggevent"]["delayed"]["text"] = "Visual"
        self.read(self.frame("Unknown prior speech"), self.frame("Other source", "other"))
        self.assertTrue(self.window._activate_profile(profile))
        self.app.processEvents()
        self.assertEqual(self.speech, ["Other source"])
        self.assertIn("delayed", self.bridge._speech_cancel_pending)

    def test_teardown_rejects_private_queued_frames_from_the_old_reader(self):
        self.read(self.frame("Old delayed"), self.frame("Old popup", delayed=False))
        self.bridge.stop()
        self.app.processEvents()
        self.assertEqual(self.speech, [])
        self.assertEqual(self.alerts, [])

    def test_mode_changes_cancel_known_and_unidentified_queued_speech(self):
        self.bridge._handle_diagnostic("reading WS messages on stdin; speech_cancel_all=1", 7)
        self.read(self.frame("Before mode change"))
        self.window._set_triggevent_enabled(False)
        self.assertFalse(self.window._triggevent_mode)
        self.read(self.frame("Created while off", cid=None))
        self.window._set_triggevent_enabled(True)
        self.assertTrue(self.window._triggevent_mode)
        commands = [json.loads(value) for value in self.bridge._wq.queue]
        first, latest = [row["token"] for row in commands if row.get("all")]
        self.read({"t": "speech_canceled", "token": first}, self.frame("Before latest ack", cid=None))
        self.app.processEvents()
        self.assertEqual(self.speech, [])
        self.read({"t": "speech_canceled", "token": latest},
                  self.frame("Fresh known"), self.frame("Fresh unidentified", cid=None))
        self.app.processEvents()
        self.assertEqual(self.speech, ["Fresh known", "Fresh unidentified"])
        self.assertEqual(self.bridge.generation(), 7)
        self.assertTrue(self.bridge.is_active())
        self.read(self.frame("Unchanged mode"))
        self.window._set_triggevent_enabled(True)
        self.app.processEvents()
        self.assertEqual(self.speech[-1], "Unchanged mode")
        self.assertEqual(len(self.bridge._wq.queue), len(commands))


class ModeSwitchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        session_ui.SessionUiTests.setUpClass()

    def setUp(self):
        session_ui.SessionUiTests.setUp(self)

    def check_switch(self, *, enabled, failed_start=False):
        window = self.window
        window._cactbot_mode = not enabled
        window._local_enabled = enabled
        window._triggers_enabled = enabled
        window._connected = True
        window._set_cactbot_button(not enabled)
        window._triggers = [Trigger(ability_id="ABCD", tts_text="Local cast", cooldown_s=0)]
        reader = Mock()
        reader.is_active.return_value = False
        if failed_start:
            reader.start.side_effect = RuntimeError("Browser unavailable")
        window._cactbot_reader = reader
        settings = ac._SETTINGS_FILE
        settings.unlink(missing_ok=True)
        settings.mkdir()
        observed = []
        timer = QTimer()
        try:
            with patch("nyaatriggers.main_window.speak") as speech:
                def inspect_warning():
                    dialog = QApplication.activeModalWidget()
                    if not isinstance(dialog, QMessageBox):
                        return
                    window._ws.log_line.emit(
                        "20|ts|40000001|Boss|ABCD|Test cast|10000001|Player")
                    observed.append((window._cactbot_mode, window._local_enabled,
                                     window._triggers_enabled, speech.call_count))
                    timer.stop()
                    dialog.accept()

                timer.timeout.connect(inspect_warning)
                with patch.object(window, "_ensure_cactbot_reader", return_value=reader), \
                        patch.object(window, "_set_triggevent_enabled"), \
                        patch.object(window, "_set_triggernometry_enabled"), \
                        patch.object(ac.QMessageBox, "warning", _ACTUAL_WARNING):
                    timer.start(20)
                    QTest.mouseClick(window._cactbot_btn, Qt.MouseButton.LeftButton)
                active = enabled and not failed_start
                self.assertEqual(observed, [(active, not active, not active, int(not active))])
                self.assertEqual(window._settings["cactbot_enabled"], active)
                self.assertEqual(window._settings["local_enabled"], not active)
                settings.rmdir()
                self.assertTrue(window._save_settings())
                saved = json.loads(settings.read_text())
                self.assertEqual(saved["cactbot_enabled"], active)
                self.assertEqual(saved["local_enabled"], not active)
        finally:
            timer.stop()
            if settings.is_dir():
                settings.rmdir()

    def test_stopping_cactbot_restores_local_before_a_save_warning(self):
        self.check_switch(enabled=False)

    def test_starting_cactbot_mutes_local_before_a_save_warning(self):
        self.check_switch(enabled=True)

    def test_failed_cactbot_start_restores_local_before_a_save_warning(self):
        self.check_switch(enabled=True, failed_start=True)

    def test_direct_mode_setters_still_save_by_default(self):
        with patch.object(self.window, "_save_settings") as save:
            self.window._set_cactbot_enabled(False)
            self.assertEqual(save.call_count, 1)
            self.window._set_local_enabled(False)
            self.assertEqual(save.call_count, 2)

    def test_reader_retarget_and_stop_cancel_accepted_speech(self):
        window = self.window
        for action in ("retarget", "stop", "noop", "disconnect", "inactive", "failed_stop", "failed_start"):
            with self.subTest(action=action):
                window._cactbot_mode = True
                window._triggers_enabled = False
                window._connected = False
                window._url_edit.setText("ws://127.0.0.1:10501/ws")
                reader = Mock()
                reader.is_active.return_value = action not in {"inactive", "failed_start"}
                reader.websocket_url.return_value = window._url_edit.text()
                if action == "failed_stop":
                    reader.stop.side_effect = RuntimeError("Stop failed")
                if action == "failed_start":
                    reader.start.side_effect = RuntimeError("Start failed")
                    window._cactbot_mode = False
                    window._triggers_enabled = True
                window._cactbot_reader = reader
                proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
                pending = Queue()
                try:
                    with patch.object(tts, "_current_proc", proc), \
                            patch.object(tts, "_interrupted_proc", None), \
                            patch.object(tts, "_queue", pending), \
                            patch.object(tts, "_ensure_worker"), \
                            patch.object(tts, "_master_volume", 1.0), \
                            patch.object(window._ws, "connect_to"):
                        if action == "failed_start":
                            tts.speak(f"Accepted speech {action}")
                        else:
                            window._on_cactbot_tts(f"Accepted speech {action}")
                        self.assertEqual(pending.qsize(), 1)
                        if action == "retarget":
                            window._url_edit.setText("ws://127.0.0.1:10502/ws")
                            window._toggle_connection()
                        elif action in {"noop", "failed_start"}:
                            window._set_cactbot_enabled(True)
                        elif action == "disconnect":
                            window._on_status_changed(False, "Disconnected")
                        elif action == "failed_stop":
                            with self.assertRaisesRegex(RuntimeError, "Stop failed"):
                                window._stop_cactbot_reader()
                        else:
                            window._set_cactbot_enabled(False)
                        cancelled = action in {"retarget", "stop", "failed_stop"}
                        self.assertEqual(pending.empty(), cancelled)
                        if cancelled:
                            proc.wait(timeout=2)
                            tts.speak("Fresh speech")
                            self.assertEqual(pending.get_nowait()[1], "Fresh speech")
                        else:
                            self.assertIsNone(proc.poll())
                            self.assertEqual(pending.get_nowait()[1], f"Accepted speech {action}")
                finally:
                    if proc.poll() is None:
                        proc.kill()
                    proc.wait(timeout=2)
                    reader.stop.side_effect = None


class SpeechShutdownTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        session_ui.SessionUiTests.setUpClass()

    def setUp(self):
        session_ui.SessionUiTests.setUp(self)

    def check_speech_stop(self, stop, *, ignore_term=False):
        tts.resume()
        ready = self.temp / "speaking"
        ready.unlink(missing_ok=True)
        program = ("from pathlib import Path; import sys, time, signal; "
                   + ("signal.signal(signal.SIGTERM, signal.SIG_IGN); " if ignore_term else "")
                   + "Path(sys.argv[1]).touch(); time.sleep(30)")
        result = []
        worker = threading.Thread(target=lambda: result.append(tts._run_speak_proc(
            [sys.executable, "-c", program, str(ready)], "Pending speech", stdin_text=False)))
        with patch.object(tts, "_queue", Queue()), patch.object(tts, "_generation", 100):
            try:
                worker.start()
                deadline = time.monotonic() + 5
                while not ready.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(ready.exists())
                process = tts._current_proc
                self.assertIsNotNone(process)
                tts._enqueue(("tts", "Queued after active speech", 1.0, 1.0, None))
                stop()
                self.assertIsNotNone(process.poll(), "Speech must not outlive teardown")
                self.assertTrue(tts._queue.empty())
                worker.join(timeout=2)
                self.assertFalse(worker.is_alive())
                self.assertEqual(result, [True])
            finally:
                tts.interrupt()
                process = tts._current_proc
                if process is not None and process.poll() is None:
                    process.kill()
                worker.join(timeout=5)

    def test_window_close_stops_owned_speech_and_discards_queued_callouts(self):
        self.check_speech_stop(self.window.close)

    @unittest.skipIf(os.name == "nt", "POSIX signal refusal")
    def test_window_close_kills_a_speech_backend_that_ignores_terminate(self):
        self.check_speech_stop(self.window.close, ignore_term=True)

    def test_update_restart_and_windows_handoff_stop_speech_before_relaunch(self):
        for operation in ("restart", "handoff"):
            with self.subTest(operation=operation):
                if operation == "restart":
                    with patch("nyaatriggers.updater.relaunch"):
                        self.check_speech_stop(self.window._restart_for_update)
                else:
                    self.check_speech_stop(self.window._quit_for_windows_handoff)

    def test_restart_save_failure_drops_late_speech_and_failed_exec_accepts_fresh_work(self):
        settings = ac._SETTINGS_FILE
        settings.unlink(missing_ok=True)
        settings.mkdir()
        self.window._save_warned = False
        self.window._settings_save_timer.start()
        observed = []
        timer = QTimer()

        def inside_warning():
            dialog = QApplication.activeModalWidget()
            if isinstance(dialog, QMessageBox):
                text = "Late teardown speech" if tts._speech_suspended else "Fresh after failure"
                tts._enqueue(("tts", text, 1.0, 1.0, None))
                observed.append((tts._speech_suspended, tts._queue.empty()))
                dialog.accept()

        timer.timeout.connect(inside_warning)
        with patch.object(tts, "_queue", Queue()), \
                patch("nyaatriggers.updater.relaunch", side_effect=OSError("exec failed")):
            try:
                with patch.object(ac.QMessageBox, "warning", _ACTUAL_WARNING):
                    timer.start(10)
                    self.window._restart_for_update()
                self.assertTrue(observed)
                self.assertEqual(observed[0], (True, True))
                self.assertFalse(tts._speech_suspended)
                tts._enqueue(("tts", "Fresh after failed restart", 1.0, 1.0, None))
                self.assertEqual(tts._queue.get_nowait()[1], "Fresh after failure")
                self.assertEqual(tts._queue.get_nowait()[1], "Fresh after failed restart")
            finally:
                timer.stop()
                settings.rmdir()

    def test_suspension_and_process_registration_cannot_leave_a_speech_child_alive(self):
        from subprocess import Popen
        entered, release = threading.Event(), threading.Event()
        children = []

        def register_late(*args, **kwargs):
            process = Popen(*args, **kwargs)
            children.append(process)
            entered.set()
            release.wait(3)
            return process

        worker = threading.Thread(target=lambda: tts._run_speak_proc(
            [sys.executable, "-c", "import time; time.sleep(30)"], "", stdin_text=False, gen=100))
        stopper = threading.Thread(target=tts.suspend)
        with patch.object(tts, "_generation", 100), patch.object(tts, "_queue", Queue()), \
                patch.object(tts.subprocess, "Popen", side_effect=register_late):
            try:
                tts.resume()
                worker.start()
                self.assertTrue(entered.wait(3))
                stopper.start()
                deadline = time.monotonic() + 3
                while not tts._speech_suspended and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(tts._speech_suspended)
                release.set()
                stopper.join(timeout=3)
                worker.join(timeout=3)
                self.assertFalse(stopper.is_alive())
                self.assertFalse(worker.is_alive())
                self.assertIsNotNone(children[0].poll())
            finally:
                release.set()
                for process in children:
                    if process.poll() is None:
                        process.kill()
                if stopper.ident is not None:
                    stopper.join(timeout=5)
                worker.join(timeout=5)

    def test_repeated_teardown_without_a_child_remains_resumable(self):
        with patch.object(tts, "_queue", Queue()):
            self.window._stop_background_timers()
            self.window._stop_background_timers()
            tts._enqueue(("tts", "Old", 1.0, 1.0, None))
            self.assertTrue(tts._queue.empty())
            tts.resume()
            tts._enqueue(("tts", "Fresh", 1.0, 1.0, None))
            self.assertEqual(tts._queue.get_nowait()[1], "Fresh")


if __name__ == "__main__":
    unittest.main()
