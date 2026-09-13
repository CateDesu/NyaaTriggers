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

from nyaatriggers import app_common as ac
from nyaatriggers import main_window as mw
from nyaatriggers import theme
from nyaatriggers.prog_session import CHECKPOINT_SECONDS, ProgSessions
from nyaatriggers.record_store import write_record
from tests.test_session_features import ability, PLAYER, Clock
from nyaatriggers.trigger_engine import Trigger
from nyaatriggers.trigger_profiles import capture_profile
from nyaatriggers.ui.prog_tab import DEATHS_COLUMN
from nyaatriggers.ui.prog_tab import PHASE_COLUMN
from tests.test_prog_phases import fixture_definition, marker


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

    def phase_pull(self):
        self.connect()
        self.window._on_ws_zone_changed(1363, "UMAD")
        self.window._on_ws_primary_player(int(PLAYER, 16), "Player")
        self.window._prog_sessions.definitions = (fixture_definition(),)
        self.window._prog_tab.start_button.click()
        self.window._on_in_combat(True, True)
        self.line(marker(0))
        self.line(ability())
        return self.window._prog_sessions.current["pulls"][0]

    def test_window_icon_loads_from_bundled_assets(self):
        self.assertFalse(self.window.windowIcon().isNull())

    def test_umad_without_verified_rules_is_explicitly_unavailable(self):
        self.connect()
        self.window._on_ws_zone_changed(1363, "UMAD")
        self.window._on_ws_primary_player(int(PLAYER, 16), "Player")
        tab = self.window._prog_tab
        tab.start_button.click()
        self.pull()
        self.assertEqual(tab.table.item(0, PHASE_COLUMN).text(), "Not recorded")
        self.assertIn("awaiting verified combat recordings", tab.phase_notice.text())
        self.assertEqual(tab.phase_table.rowCount(), 0)

    def test_live_phase_details_preserve_note_cursor_and_bookmark(self):
        pull = self.phase_pull()
        tab = self.window._prog_tab
        tab.note.setPlainText("Keep this edit")
        tab.bookmark.setChecked(True)
        cursor = tab.note.textCursor()
        cursor.setPosition(4)
        tab.note.setTextCursor(cursor)
        self.clock.value += 10
        self.line(marker(2))
        tab.tick()
        self.assertEqual(tab.table.item(0, PHASE_COLUMN).text(), "P3")
        self.assertEqual(tab.phase_table.rowCount(), 5)
        self.assertEqual(tab.phase_table.item(1, 1).text(), "—")
        self.assertEqual(tab.phase_table.item(2, 1).text(), "00:00:10")
        self.assertEqual(tab.note.textCursor().position(), 4)
        self.assertEqual(tab.note.toPlainText(), "Keep this edit")
        self.assertTrue(pull["bookmark"])
        self.window._nav_buttons[4].click()
        self.window.resize(900, 600)
        self.window.show()
        self.app.processEvents()
        self.window.grab().save("/tmp/nyaatriggers-phase-details.png")

    def test_transition_preserves_recap_buffer_and_logical_pull(self):
        pull = self.phase_pull()
        tab = self.window._prog_tab
        self.line(marker(0, transition=True))
        self.window._on_in_combat(False, False)
        self.clock.value += 5
        self.window._on_in_combat(True, True)
        self.line(marker(1))
        self.line(["25", "ts", PLAYER, "Player"])
        tab.tick()
        self.assertEqual(tab.table.rowCount(), 1)
        self.assertEqual(tab.table.item(0, PHASE_COLUMN).text(), "P2")
        recaps, errors = self.window._prog_sessions.recaps.load(self.window._prog_sessions.current["id"], pull["id"])
        self.assertEqual(errors, [])
        self.assertEqual(len(recaps), 1)
        self.assertTrue(any(e["kind"] == "damage" for e in recaps[0]["events"]))
        tab.recap_button.click()
        self.assertEqual(len(self.window._recap_records), 1)

    def test_corrupt_and_legacy_phase_views_clear_old_details(self):
        pull = self.phase_pull()
        tab = self.window._prog_tab
        tab.tick()
        self.assertEqual(tab.phase_table.rowCount(), 5)
        pull["phase_tracking"] = {"version": 999}
        tab.tick()
        self.assertEqual(tab.table.item(0, PHASE_COLUMN).text(), "Unavailable")
        self.assertIn("could not be read", tab.phase_notice.text())
        self.assertEqual(tab.phase_table.rowCount(), 0)
        self.assertTrue(tab.note.isEnabled())
        self.assertTrue(tab.recap_button.isEnabled())
        del pull["phase_tracking"]
        tab.tick()
        self.assertEqual(tab.table.item(0, PHASE_COLUMN).text(), "Not recorded")
        self.assertIn("was not recorded", tab.phase_notice.text())

    def test_timeout_updates_visible_ending_without_changing_notes(self):
        pull = self.phase_pull()
        tab = self.window._prog_tab
        tab.note.setPlainText("Review this transition")
        self.line(marker(0, transition=True))
        self.window._on_in_combat(False, False)
        self.clock.value += 31
        tab.tick()
        self.assertEqual(pull["ending"], "boundary-uncertain")
        self.assertIn("boundary could not be confirmed", tab.phase_notice.text())
        self.assertEqual(tab.note.toPlainText(), "Review this transition")

    def test_unrelated_damage_does_not_build_extra_meter_snapshots(self):
        self.phase_pull()
        with patch.object(self.window._dps_meter, "full_snapshot") as snapshot:
            self.line(ability())
        snapshot.assert_not_called()
        self.assertEqual(self.window._prog_sessions.current["pulls"][0]["ending"], "active")

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

    def test_triggers_resize_keeps_profiles_collapsed_at_bottom(self):
        window = self.window
        window.show()
        for width, height in ((900, 600), (1280, 720), (1280, 1000), (900, 600)):
            with self.subTest(width=width, height=height):
                window.resize(width, height)
                self.app.processEvents()
                page = window._stack.currentWidget()
                self.assertEqual((window.width(), window.height()), (width, height))
                self.assertFalse(window._profile_body.isVisible())
                self.assertEqual(window._profile_panel.geometry().bottom(), page.height() - 1)
                self.assertLess(window._profile_panel.height(), 2 * window._profile_toggle.height())
                self.assertGreater(window._table.height(), page.height() * 0.6)

    def test_profile_dropdown_handles_long_names_and_status(self):
        window = self.window
        window._refresh_table()
        window._profiles = [capture_profile(window, "W" * 200)]
        window._refresh_profiles()
        window.resize(900, 600)
        window.show()
        self.app.processEvents()
        closed_height = window._table.height()
        window._profile_toggle.click()
        self.app.processEvents()
        self.assertTrue(window._profile_picker.isVisible())
        self.assertEqual(window._profile_picker.toolTip(), "W" * 200)
        for control in (window._profile_picker, window._profile_apply,
                        window._profile_new, window._profile_update):
            self.assertTrue(window._profile_controls.rect().contains(control.geometry()))
        window._profile_apply.click()
        self.app.processEvents()
        self.assertTrue(window._profile_status.isVisible())
        self.assertIn("Applied profile", window._profile_status.text())
        self.assertEqual((window.width(), window.height()), (900, 600))
        self.assertLess(window._profile_panel.height(), window._stack.height() * 0.3)
        window._profile_toggle.click()
        # Hiding the body posts another layout pass to its parent.
        self.app.processEvents()
        self.app.processEvents()
        self.assertFalse(window._profile_body.isVisible())
        self.assertEqual(window._table.height(), closed_height)

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
        with patch("nyaatriggers.recap_store.write_record", side_effect=OSError("Disk failed")):
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

    def test_next_pull_keeps_preparation_statuses_and_healing_in_saved_recaps(self):
        self.connect()
        window = self.window
        tab = window._prog_tab
        tab.start_button.click()
        self.pull()
        self.clock.value += 3
        self.line(["26", "ts", "ABC", "Preparation buff", "30", PLAYER, "Player", PLAYER, "Player"])
        self.line(ability(pairs=[("04", "1F40000")]))
        self.line(["24", "ts", PLAYER, "Player", "HoT", "0", "A"])
        window._on_in_combat(True, True)
        self.clock.value += 1
        self.line(ability())
        self.line(["25", "ts", PLAYER, "Player"])
        death = window._recap_records[0]
        self.assertEqual([s["name"] for s in death["statuses"]], ["Preparation buff"])
        self.assertEqual([e["kind"] for e in death["events"]], ["gained", "heal", "hot", "damage"])
        self.assertEqual(death["events"][0]["time"], -1)
        saved, errors = window._prog_sessions.recaps.load(tab.session["id"], tab.session["pulls"][-1]["id"])
        self.assertEqual(errors, [])
        self.assertEqual(saved[0]["events"], death["events"])
        self.assertEqual(saved[0]["statuses"], [{"name": "Preparation buff", "source": "Player"}])

    def test_live_duration_and_break_time_are_checkpointed_without_edits(self):
        self.connect()
        window = self.window
        tab = window._prog_tab
        tab.start_button.click()
        window._on_in_combat(True, True)
        self.line(ability())
        with patch("nyaatriggers.prog_session.write_record", wraps=write_record) as save:
            for _ in range(60):
                self.clock.value += 1
                tab.tick()
        self.assertEqual(save.call_count, 60 // CHECKPOINT_SECONDS)
        saved = ProgSessions(self.temp / "prog_sessions").sessions[0]
        self.assertEqual(saved["pulls"][0]["duration"], 60)
        self.assertEqual(saved["pulls"][0]["ending"], "program-closed")
        self.assertEqual(saved["elapsed"], 60)
        self.line(ability())
        window._on_in_combat(False, False)
        self.clock.value += CHECKPOINT_SECONDS
        tab.tick()
        saved = ProgSessions(self.temp / "prog_sessions").sessions[0]
        self.assertEqual(saved["elapsed"], 60 + CHECKPOINT_SECONDS)
        self.assertEqual(saved["pulls"][0]["duration"], 60)

    def test_late_instant_death_restores_the_pull_row_and_saved_recap(self):
        self.connect()
        window = self.window
        tab = window._prog_tab
        tab.start_button.click()
        window._on_in_combat(True, True)
        self.line(ability(pairs=[("33", "0")]))
        self.clock.value += 1
        window._on_in_combat(False, False)
        self.assertEqual(tab.table.rowCount(), 0)
        self.clock.value += 0.2
        self.line(["25", "ts", PLAYER, "Player"])
        self.assertEqual(tab.table.rowCount(), 1)
        self.assertEqual(tab.table.item(0, DEATHS_COLUMN).text(), "1")
        self.assertTrue(tab.recap_button.isEnabled())
        tab.recap_button.click()
        self.assertEqual(window._recap_records[0]["events"][0]["kind"], "instant-death")
        loaded = ProgSessions(self.temp / "prog_sessions")
        pull = loaded.sessions[0]["pulls"][0]
        self.assertFalse(pull["complete"])
        self.assertEqual(pull["recap_count"], 1)
        self.assertEqual(len(loaded.recaps.load(tab.session["id"], pull["id"])[0]), 1)

    def test_duplicate_deaths_agree_between_prog_meter_and_recaps(self):
        self.connect()
        window = self.window
        tab = window._prog_tab
        tab.start_button.click()
        window._on_in_combat(True, True)
        self.line(ability())
        death = ["25", "ts", PLAYER, "Player"]
        self.line(death)
        self.line(death)
        tab.tick()
        self.assertEqual(tab.table.item(0, DEATHS_COLUMN).text(), "1")
        self.assertEqual(tab.pull["recap_count"], 1)
        self.assertEqual(window._dps_meter.full_snapshot()["Encounter"]["deaths"], 1)
        self.clock.value += 1.1
        self.line(death)
        window._on_in_combat(False, False)
        self.assertEqual(tab.pull["deaths"], 2)
        self.assertEqual(tab.pull["recap_count"], 2)
        window._on_in_combat(True, True)
        self.line(ability())
        self.line(death)
        tab.tick()
        pull = tab.session["pulls"][-1]
        self.assertEqual(pull["deaths"], 1)
        self.assertEqual(pull["recap_count"], 1)

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
        self.assertEqual(tab.table.item(0, DEATHS_COLUMN).text(), "1")


if __name__ == "__main__":
    unittest.main()
