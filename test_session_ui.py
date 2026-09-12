"""Exercise the session pages and profile switching in the real window."""

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from contextlib import ExitStack
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication

import app_common as ac
import main_window as mw
import theme
from prog_session import ProgSessions
from test_session_features import ability, PLAYER, Clock
from trigger_engine import Trigger
from trigger_profiles import capture_profile


class SessionUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setStyleSheet(theme.STYLESHEET)

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.temp = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        for key, value in {"_DATA_DIR": self.temp, "_SETTINGS_FILE": self.temp / "settings.json",
                           "TRIGGERS_LOCAL_FILE": self.temp / "triggers.local.json"}.items():
            self.stack.enter_context(patch.object(ac, key, value))
        def settings(window):
            window._settings.update(tts_engine="system", auto_connect=False, auto_check_updates=False,
                                    local_enabled=False, ui_language="en")
        self.stack.enter_context(patch.object(mw.MainWindow, "_load_settings", settings))
        for method in ("_load_triggers", "_load_timeline_for_zone", "_refresh_telesto_party"):
            self.stack.enter_context(patch.object(mw.MainWindow, method))
        for target in ((mw.PluginLink, "start"), (mw.QTimer, "singleShot"),
                       (mw, "kokoro_ready"), (mw, "_ensure_worker"), (ac.QMessageBox, "warning")):
            self.stack.enter_context(patch.object(*target))
        self.window = mw.MainWindow()
        self.addCleanup(self.window.close)
        self.clock = Clock()
        self.window._dps_meter._clock = self.clock
        self.window._prog_sessions.clock = self.clock
        self.window._death_recap.clock = self.clock

    def connect(self):
        window = self.window
        window._on_status_changed(True, "Connected")
        window._on_ws_zone_changed(1, "Test duty")
        window._on_ws_primary_player(int(PLAYER, 16), "Player")
        window._on_in_combat(False, False)
        window._prog_tab.tick()

    def line(self, fields):
        self.window._on_log_line("|".join(fields))

    def pull(self):
        self.window._on_in_combat(True, True)
        self.line(ability())
        self.clock.value += 12
        self.line(ability())
        self.window._on_in_combat(False, False)

    def test_navigation_recap_and_prog_end_to_end(self):
        window = self.window
        self.assertEqual(window._stack.count(), 7)
        for index, button in enumerate(window._nav_buttons):
            button.click()
            self.assertEqual(window._stack.currentIndex(), index)
            self.assertEqual(button.shortcut().toString(), f"Ctrl+{index + 1}")
        self.connect()
        tab = window._prog_tab
        tab.tick()
        self.assertTrue(tab.start_button.isEnabled())
        tab.start_button.click()
        self.pull()
        self.assertEqual(tab.table.rowCount(), 1)
        self.assertIn("1 complete pulls", tab.stats.text())
        tab.note.setPlainText("First clean towers")
        tab.bookmark.setChecked(True)
        self.pull()
        self.assertEqual(tab.table.rowCount(), 2)
        tab.table.selectRow(0)
        self.assertEqual(tab.note.toPlainText(), "First clean towers")
        self.assertTrue(tab.bookmark.isChecked())
        self.line(["25", "ts", PLAYER, "Player"])
        self.assertEqual(window._recap_list.count(), 1)
        self.assertGreater(window._recap_table.rowCount(), 0)
        tab.end_button.click()
        saved = ProgSessions(self.temp / "prog_sessions").sessions[0]
        self.assertEqual(saved["pulls"][0]["note"], "First clean towers")
        window._nav_buttons[4].click()
        window.show()
        self.app.processEvents()
        window.grab().save("/tmp/nyaatriggers-prog.png")

    def test_zone_change_and_shutdown_keep_partial_attempts(self):
        self.connect()
        window = self.window
        tab = window._prog_tab
        tab.start_button.click()
        window._on_in_combat(True, True)
        self.line(ability())
        self.clock.value += 5
        window._on_ws_zone_changed(2, "Other duty")
        self.assertIsNone(window._prog_sessions.current)
        pull = window._prog_sessions.sessions[0]["pulls"][0]
        self.assertEqual(pull["ending"], "duty-left")
        self.assertFalse(pull["complete"])

    def test_apply_profile_saves_choices_and_keeps_master_mode(self):
        window = self.window
        trigger = Trigger(id="local", enabled=True, tts_text="Stack")
        window._triggers = [trigger]
        window._local_ids.add(trigger.id)
        profile = capture_profile(window, "Tank")
        window._profiles = [profile]
        window._refresh_profiles()
        trigger.enabled = False
        trigger.tts_text = "Spread"
        window._profile_apply.click()
        self.assertTrue(trigger.enabled)
        self.assertEqual(trigger.tts_text, "Stack")
        self.assertFalse(window._cactbot_mode)
        stored = json.loads(ac.TRIGGERS_LOCAL_FILE.read_text())["triggers"][0]
        self.assertEqual(stored["tts_text"], "Stack")
        self.assertIn("Applied profile", window._profile_status.text())
        window._in_game_combat = True
        trigger.tts_text = "During combat"
        window._profile_apply.click()
        self.assertEqual(trigger.tts_text, "During combat")

    def test_failed_profile_save_restores_unknown_engine_choices(self):
        window = self.window
        trigger = Trigger(id="local", enabled=False, tts_text="Old")
        window._triggers = [trigger]
        profile = capture_profile(window, "Different")
        profile["local"]["local"].update(enabled=True, text="New")
        profile["engines"]["triggevent"]["unknown"] = {"enabled": False, "text": "Changed"}
        before = deepcopy(window._engine_disabled)
        window._profiles = [profile]
        window._refresh_profiles()
        with patch.object(window, "_save_settings", return_value=False):
            window._profile_apply.click()
        self.assertFalse(trigger.enabled)
        self.assertEqual(trigger.tts_text, "Old")
        self.assertEqual(window._engine_disabled, before)
        self.assertNotIn("unknown", window._triggevent_callout_edits)

    def test_nameless_duty_transition_ends_session(self):
        self.connect()
        window = self.window
        window._prog_tab.start_button.click()
        window._on_ws_zone_changed(2, "")
        self.assertIsNone(window._prog_sessions.current)

    def test_live_duration_and_name_edits_survive_new_pull(self):
        self.connect()
        window = self.window
        tab = window._prog_tab
        tab.start_button.click()
        tab.name.setText("Static prog")
        tab.name.textEdited.emit("Static prog")
        window._on_in_combat(True, True)
        self.line(ability())
        self.clock.value += 20
        tab.tick()
        self.assertEqual(tab.table.item(0, 2).text(), "00:00:20")
        self.assertEqual(tab.name.text(), "Static prog")

    def test_saved_pull_navigation_survives_restart_and_live_updates(self):
        self.connect()
        window = self.window
        tab = window._prog_tab
        tab.start_button.click()
        window._on_in_combat(True, True)
        self.line(ability())
        self.line(["25", "ts", PLAYER, "First player"])
        window._on_in_combat(False, False)
        tab.note.setPlainText("Review this death")
        tab.recap_button.click()
        self.assertIs(window._stack.currentWidget(), window._death_recap_tab)
        self.assertIn("Pull 1", window._recap_scope.text())
        self.assertEqual(window._recap_records[0]["name"], "First player")
        window._recap_back.click()
        self.assertIs(window._stack.currentWidget(), tab)
        self.assertEqual(tab.note.toPlainText(), "Review this death")
        window._on_in_combat(True, True)
        self.line(ability())
        self.clock.value += 2
        self.line(["25", "ts", PLAYER, "Second player"])
        self.assertEqual(len(window._recap_records), 1)
        self.assertEqual(window._recap_records[0]["name"], "First player")
        window._recap_recent.click()
        self.assertEqual(window._recap_list.count(), 2)
        tab.end_button.click()
        window._death_recap.deaths.clear()
        restarted = ProgSessions(self.temp / "prog_sessions")
        window._prog_sessions = tab.sessions = restarted
        tab.refresh()
        tab.table.selectRow(0)
        tab.recap_button.click()
        self.assertEqual(window._recap_records[0]["name"], "First player")
        self.assertGreater(window._recap_table.rowCount(), 0)
        window.show()
        self.app.processEvents()
        window.grab().save("/tmp/nyaatriggers-saved-recap.png")

    def test_empty_and_legacy_recaps_clear_previous_details(self):
        self.connect()
        window = self.window
        tab = window._prog_tab
        self.assertFalse(tab.recap_button.isEnabled())
        tab.start_button.click()
        self.pull()
        self.line(["25", "ts", PLAYER, "Player"])
        self.assertGreater(window._recap_table.rowCount(), 0)
        self.pull()
        tab.table.selectRow(1)
        tab.recap_button.click()
        self.assertEqual(window._recap_table.rowCount(), 0)
        self.assertIn("No death recaps", window._recap_statuses.text())
        del tab.pull["recap_count"]
        tab.recap_button.click()
        self.assertIn("before saved death recaps", window._recap_notice.text())
        self.assertEqual(window._recap_list.count(), 0)

    def test_active_saved_view_tracks_deaths_without_changing_selection(self):
        self.connect()
        window = self.window
        tab = window._prog_tab
        tab.start_button.click()
        window._on_in_combat(True, True)
        self.line(ability())
        tab.recap_button.click()
        self.line(["25", "ts", PLAYER, "First death"])
        selected = window._recap_list.currentItem().data(Qt.ItemDataRole.UserRole)
        self.clock.value += 2
        self.line(ability())
        self.line(["25", "ts", PLAYER, "Second death"])
        self.assertEqual(window._recap_list.count(), 2)
        self.assertEqual(window._recap_list.currentItem().data(Qt.ItemDataRole.UserRole), selected)
        self.assertEqual(len(window._prog_sessions.recaps.load(tab.session["id"], tab.pull["id"])[0]), 2)

    def test_recap_save_and_load_errors_are_visible_without_losing_history(self):
        self.connect()
        window = self.window
        tab = window._prog_tab
        tab.start_button.click()
        window._on_in_combat(True, True)
        self.line(ability())
        with patch("recap_store.write_record", side_effect=OSError("Disk failed")):
            self.line(["25", "ts", PLAYER, "Player"])
            tab.recap_button.click()
            tab.tick()
            self.assertIn("Disk failed", tab.status.text())
            self.assertIn("Disk failed", window._recap_notice.text())
            self.assertEqual(window._recap_list.count(), 1)
        tab.flush()
        tab.tick()
        self.assertNotIn("Disk failed", window._recap_notice.text())
        path = next((self.temp / "prog_sessions").glob("recaps/*/*/*.json"))
        path.write_text("bad json")
        tab.recap_button.click()
        self.assertIn("could not be read", window._recap_notice.text())
        self.assertIn("Only 0 of 1", window._recap_notice.text())
        self.assertEqual(window._recap_table.rowCount(), 0)
        self.assertEqual(path.read_text(), "bad json")

    def test_normal_pull_end_resets_observations_before_next_pull(self):
        self.connect()
        window = self.window
        window._prog_tab.start_button.click()
        self.pull()
        window._on_in_combat(True, True)
        self.line(["25", "ts", PLAYER, "Player"])
        self.assertEqual(window._recap_records[0]["events"], [])

    def test_crash_after_death_keeps_observed_pull_duration_and_death_count(self):
        self.connect()
        window = self.window
        window._prog_tab.start_button.click()
        window._on_in_combat(True, True)
        self.line(ability())
        self.clock.value += 12
        self.line(["25", "ts", PLAYER, "Player"])
        saved = ProgSessions(self.temp / "prog_sessions").sessions[0]["pulls"][0]
        self.assertEqual(saved["deaths"], 1)
        self.assertEqual(saved["duration"], 12)

    def test_zone_and_disconnect_stop_late_deaths_reaching_old_pull(self):
        self.connect()
        window = self.window
        window._prog_tab.start_button.click()
        self.pull()
        session = window._prog_sessions.current
        pull = session["pulls"][0]
        window._ws.status_changed.emit(False, "Lost feed")
        self.line(["25", "ts", PLAYER, "After disconnect"])
        self.assertEqual(window._prog_sessions.recaps.load(session["id"], pull["id"]), ([], []))
        window._on_ws_zone_changed(2, "Other duty")
        self.clock.value += 2
        self.line(["25", "ts", PLAYER, "Other duty"])
        self.assertIsNone(window._prog_sessions.current)
        self.assertEqual(pull["recap_count"], 0)

    def test_late_saved_death_updates_the_finished_pull_row(self):
        self.connect()
        window = self.window
        tab = window._prog_tab
        tab.start_button.click()
        self.pull()
        self.line(["25", "ts", PLAYER, "Player"])
        tab.tick()
        self.assertEqual(tab.table.item(0, 4).text(), "1")


if __name__ == "__main__":
    unittest.main()
