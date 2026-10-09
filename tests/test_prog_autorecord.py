from contextlib import ExitStack
import unittest
from unittest.mock import patch

from PyQt6 import sip
from PyQt6.QtCore import QCoreApplication, QEvent

from nyaatriggers.telesto_client import TelestoClient
from nyaatriggers.triggevent_bridge import TriggeventBridge
from tests import test_session_ui as fixture
from tests.test_prog_comparison import pull as saved_pull
from tests.test_session_features import ability, PLAYER


class AutomaticProgTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture.SessionUiTests.setUpClass()

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(TriggeventBridge, "is_available", return_value=False))
        self.stack.enter_context(patch.object(TelestoClient, "start"))
        self.case = fixture.SessionUiTests()
        self.case.setUp()
        self.window = self.case.window
        self.clock = self.case.clock
        self.sessions = self.window._prog_sessions
        self.tab = self.window._prog_tab
        self.addCleanup(self.dispose)

    def dispose(self):
        self.case.doCleanups()
        self.window.deleteLater()
        fixture.SessionUiTests.app.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.assertTrue(sip.isdeleted(self.window))

    def connect(self, *, idle=True, zone=True):
        self.window._ws.status_changed.emit(True, "Connected")
        if zone:
            self.window._ws.zone_changed.emit(1, "Test duty")
        self.window._ws.primary_player.emit(int(PLAYER, 16), "Player")
        if idle:
            self.combat(False)

    def combat(self, active):
        self.window._ws.in_combat.emit(active, active)

    def damage(self):
        self.case.line(ability())

    def pull(self, duration=12):
        self.combat(True)
        self.damage()
        self.clock.value += duration
        self.damage()
        self.combat(False)

    def pulls(self):
        return [pull for session in self.sessions.sessions for pull in session["pulls"]]

    def test_first_full_pull_records_without_start_and_wipes_reuse_the_session(self):
        self.connect()
        self.tab.tick()
        for control in ("start_button", "end_button", "start_session", "end_session"):
            self.assertFalse(hasattr(self.tab, control))
        self.assertIsNone(self.sessions.current)
        self.assertEqual(self.sessions.sessions, [])
        self.combat(True)
        self.damage()
        session = self.sessions.current
        self.assertIsNotNone(session)
        self.assertEqual(len(session["pulls"]), 1)
        self.assertEqual(session["pulls"][0]["ending"], "active")
        self.clock.value += 12
        self.damage()
        self.combat(False)
        self.assertEqual(session["pulls"][0]["duration"], 12)
        self.assertTrue(session["pulls"][0]["complete"])
        self.combat(True)
        self.damage()
        self.clock.value += 6
        self.case.line(["33", "wipe", "80000000", "4000000F"])
        self.assertIs(self.sessions.current, session)
        self.assertEqual(len(self.sessions.sessions), 1)
        self.assertEqual([pull["ending"] for pull in session["pulls"]], ["combat-ended", "wipe"])
        self.assertTrue(all(pull["complete"] for pull in session["pulls"]))

    def test_damage_after_confirmed_idle_records_a_pull_without_a_combat_flag(self):
        self.connect()
        self.damage()
        session = self.sessions.current
        self.assertIsNotNone(session)
        self.assertEqual(len(session["pulls"]), 1)
        self.clock.value += 12
        self.damage()
        self.case.line(["33", "wipe", "80000000", "4000000F"])
        self.assertTrue(session["pulls"][0]["complete"])
        self.assertEqual(session["pulls"][0]["duration"], 12)

    def test_first_midcombat_observation_waits_for_a_fresh_pull(self):
        for act, game in ((True, True), (True, False), (False, True)):
            with self.subTest(act=act, game=game):
                self.window._ws.status_changed.emit(False, "Disconnected")
                self.sessions.end()
                self.connect(idle=False)
                count = len(self.pulls())
                self.window._ws.in_combat.emit(act, game)
                self.damage()
                self.clock.value += 8
                self.damage()
                self.tab.tick()
                self.assertEqual(len(self.pulls()), count)
                self.combat(False)
                self.pull()
                self.assertEqual(len(self.pulls()), count + 1)
                self.assertEqual(len(self.sessions.current["pulls"]), 1)
                self.assertTrue(self.sessions.current["pulls"][0]["complete"])
                self.assertEqual(self.sessions.current["pulls"][0]["duration"], 12)

    def test_unconfirmed_zone_does_not_create_a_session_or_import_a_partial_pull(self):
        self.connect(zone=False)
        self.combat(True)
        self.damage()
        self.assertIsNone(self.sessions.current)
        self.window._ws.zone_changed.emit(1, "Test duty")
        self.clock.value += 8
        self.damage()
        self.assertEqual(self.pulls(), [])
        self.combat(False)
        self.pull()
        self.assertEqual(len(self.pulls()), 1)
        self.assertTrue(self.pulls()[0]["complete"])

    def test_known_zone_without_combat_state_does_not_treat_damage_as_a_full_pull(self):
        self.connect(idle=False)
        self.damage()
        self.assertIsNone(self.sessions.current)
        self.tab.tick()
        self.assertIsNone(self.sessions.current)
        self.combat(True)
        self.damage()
        self.assertEqual(self.pulls(), [])
        self.combat(False)
        self.pull()
        self.assertEqual(len(self.pulls()), 1)
        self.assertTrue(self.pulls()[0]["complete"])

    def test_same_duty_reconnect_retains_the_session_and_skips_the_partial_pull(self):
        self.connect()
        self.pull()
        session = self.sessions.current
        self.combat(True)
        self.damage()
        self.clock.value += 4
        self.window._ws.status_changed.emit(False, "Disconnected")
        self.assertIs(self.sessions.current, session)
        self.assertEqual(session["pulls"][-1]["ending"], "feed-lost")
        self.window._ws.status_changed.emit(True, "Connected")
        self.combat(True)
        self.damage()
        self.window._ws.zone_changed.emit(1, "Test duty")
        self.damage()
        self.assertIs(self.sessions.current, session)
        self.assertEqual(len(session["pulls"]), 2)
        self.combat(False)
        self.pull()
        self.assertIs(self.sessions.current, session)
        self.assertEqual(len(self.sessions.sessions), 1)
        self.assertEqual([pull["complete"] for pull in session["pulls"]], [True, False, True])

    def test_duty_change_ends_the_old_session_and_starts_the_next_on_a_pull(self):
        self.connect()
        self.pull()
        previous = self.sessions.current
        self.window._ws.zone_changed.emit(2, "Other duty")
        self.tab.tick()
        self.assertIsNone(self.sessions.current)
        self.assertEqual(previous["state"], "ended")
        self.assertEqual(len(self.sessions.sessions), 1)
        self.pull()
        current = self.sessions.current
        self.assertIsNot(current, previous)
        self.assertEqual((current["zone_id"], current["zone"]), (2, "Other duty"))
        self.assertEqual(len(current["pulls"]), 1)
        self.assertTrue(current["pulls"][0]["complete"])

    def test_model_end_while_idle_waits_for_the_next_pull_without_tick_restart(self):
        self.connect()
        self.pull()
        previous = self.sessions.current
        self.case.end_session()
        self.clock.value += 30
        for _ in range(3):
            self.tab.tick()
            self.combat(False)
        self.assertIsNone(self.sessions.current)
        self.assertEqual(len(self.sessions.sessions), 1)
        self.pull()
        self.assertIsNot(self.sessions.current, previous)
        self.assertEqual(len(self.sessions.sessions), 2)
        self.assertEqual([len(session["pulls"]) for session in self.sessions.sessions], [1, 1])

    def test_raw_damage_after_idle_model_end_starts_a_new_full_session(self):
        self.connect()
        self.pull()
        previous = self.sessions.current
        self.case.end_session()
        self.damage()
        current = self.sessions.current
        self.assertIsNotNone(current)
        self.assertIsNot(current, previous)
        self.assertEqual(len(current["pulls"]), 1)
        self.clock.value += 5
        self.damage()
        self.case.line(["33", "wipe", "80000000", "4000000F"])
        self.assertTrue(current["pulls"][0]["complete"])

    def test_model_end_during_combat_does_not_record_the_remainder_as_a_new_full_pull(self):
        self.connect()
        self.combat(True)
        self.damage()
        previous = self.sessions.current
        self.case.end_session()
        self.clock.value += 4
        self.tab.tick()
        self.damage()
        self.combat(True)
        self.assertIsNone(self.sessions.current)
        self.assertFalse(previous["pulls"][0]["complete"])
        self.assertEqual(previous["pulls"][0]["ending"], "session-ended")
        self.combat(False)
        self.pull()
        self.assertIsNot(self.sessions.current, previous)
        self.assertEqual(len(self.sessions.current["pulls"]), 1)
        self.assertTrue(self.sessions.current["pulls"][0]["complete"])

    def test_act_fall_then_model_end_and_repeated_game_combat_waits_for_a_fresh_pull(self):
        self.connect()
        self.combat(True)
        self.damage()
        self.clock.value += 4
        self.window._ws.in_combat.emit(False, True)
        previous = self.sessions.current
        self.case.end_session()
        self.window._ws.in_combat.emit(True, True)
        self.damage()
        self.assertEqual(len(self.pulls()), 1)
        self.assertEqual(previous["state"], "ended")
        self.assertEqual(self.sessions.current["pulls"], [])
        self.combat(False)
        self.pull()
        self.assertEqual(len(self.sessions.current["pulls"]), 1)
        self.assertTrue(self.sessions.current["pulls"][0]["complete"])

    def test_raw_damage_after_an_act_fall_and_model_end_does_not_label_the_remainder_full(self):
        self.connect()
        self.combat(True)
        self.damage()
        self.clock.value += 4
        self.window._ws.in_combat.emit(False, True)
        previous = self.sessions.current
        self.case.end_session()
        self.damage()
        self.assertEqual(self.sessions.current["pulls"], [])
        self.assertEqual(len(previous["pulls"]), 1)
        self.combat(False)
        self.pull()
        self.assertEqual(len(self.sessions.current["pulls"]), 1)
        self.assertTrue(self.sessions.current["pulls"][0]["complete"])

    def test_offline_saved_selection_search_notes_and_comparison_survive_auto_recording(self):
        baseline = self.sessions.start("Baseline", 1, "Test duty", False)
        baseline["pulls"].append(saved_pull(tracked=False))
        self.sessions.end()
        saved = self.sessions.start("Earlier review", 1, "Test duty", False)
        saved["pulls"].extend(saved_pull(tracked=False) for _ in range(2))
        self.sessions.end()
        self.tab.refresh()
        self.window._ws.status_changed.emit(False, "Disconnected")
        self.tab.session_search.setText("Earlier")
        self.tab.picker.setCurrentIndex(self.tab.picker.findData(saved["id"]))
        self.tab.table.selectRow(1)
        selected_pull = self.tab.pull
        self.tab.note.setPlainText("Review these towers")
        cursor = self.tab.note.textCursor()
        cursor.setPosition(6)
        self.tab.note.setTextCursor(cursor)
        self.tab.bookmark.setChecked(True)
        self.tab.compare_button.click()
        self.tab.comparison.picker.setCurrentIndex(self.tab.comparison.picker.findData(baseline["id"]))
        self.assertTrue(self.tab.archive_button.isEnabled())
        self.assertTrue(self.tab.delete_button.isEnabled())
        self.assertTrue(self.tab.name.isEnabled())
        self.connect()
        self.pull()
        self.tab.tick()
        self.assertIsNot(self.sessions.current, saved)
        self.assertEqual(len(self.sessions.current["pulls"]), 1)
        self.assertIs(self.tab.session, saved)
        self.assertIs(self.tab.pull, selected_pull)
        self.assertEqual(self.tab.session_search.text(), "Earlier")
        self.assertEqual(self.tab.note.toPlainText(), "Review these towers")
        self.assertEqual(self.tab.note.textCursor().position(), 6)
        self.assertTrue(self.tab.bookmark.isChecked())
        self.assertTrue(self.tab.compare_button.isChecked())
        self.assertEqual(self.tab.comparison.picker.currentData(), baseline["id"])
        self.assertTrue(self.sessions.current["pulls"][0]["complete"])


if __name__ == "__main__":
    unittest.main()
