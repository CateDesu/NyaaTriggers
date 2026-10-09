from dataclasses import replace
from datetime import datetime
import unittest
from unittest.mock import patch

from nyaatriggers.prog_session import ProgSessions
from tests.test_prog_comparison import pull as saved_pull
from tests.test_prog_phases import fixture_definition
from tests import test_pull_deletion_ui as deletion_ui


class ProgressUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        deletion_ui.PullDeletionUiTests.setUpClass()

    setUp = deletion_ui.PullDeletionUiTests.setUp
    cleanup_case = deletion_ui.PullDeletionUiTests.cleanup_case
    confirm = deletion_ui.PullDeletionUiTests.confirm

    def history(self):
        definition = replace(fixture_definition(), transitions=(), continuous_combat=True,
                             bosses=(("p1", (1234,)),))
        self.sessions.definitions = (definition,)
        old = saved_pull("p1", definition=definition)
        old["started"] = 1700000000
        old["phase_tracking"]["boss_hp"] = {
            "p1": {"npc_id": 1234, "current": 35, "maximum": 100}}
        archived = self.sessions.start("Earlier", definition.zone_id, "Duty", False)
        archived["pulls"].append(old)
        archived["archived"] = True
        self.sessions.end()
        recent = saved_pull("p1", definition=definition)
        recent["started"] = 1700100000
        recent["phase_tracking"]["observations"][0]["observed_at"] = 1700100007
        recent["phase_tracking"]["boss_hp"] = {
            "p1": {"npc_id": 1234, "current": 60, "maximum": 100}}
        later_phase = saved_pull("p3", definition=definition)
        later_phase["started"] = 1700200000
        selected = self.sessions.start("Recent", definition.zone_id, "Duty", False)
        selected["pulls"].extend([recent, later_phase])
        self.sessions.end()
        self.sessions.close()
        self.sessions = ProgSessions(self.case.temp / "prog_sessions", definitions=(definition,))
        self.addCleanup(self.sessions.close)
        self.window._prog_sessions = self.sessions
        self.tab.sessions = self.sessions
        self.tab.refresh()
        return self.sessions.sessions[1], self.sessions.sessions[0]

    def test_saved_duty_history_displays_first_date_and_lowest_hp_offline_including_archived(self):
        self.history()
        table = self.tab.milestone_table
        self.assertFalse(self.window._connected)
        self.assertFalse(table.isHidden())
        self.assertEqual(table.rowCount(), 5)
        self.assertEqual(table.item(0, 1).text(), f"{datetime.fromtimestamp(1700000005):%Y-%m-%d %H:%M:%S}")
        self.assertEqual(table.item(0, 2).text(), "35.00%")
        self.assertEqual(table.item(1, 1).text(), "Not recorded")
        self.assertEqual(table.item(1, 2).text(), "Not recorded")
        self.assertEqual(table.item(2, 1).text(), f"{datetime.fromtimestamp(1700200005):%Y-%m-%d %H:%M:%S}")
        self.assertEqual(table.item(2, 2).text(), "Not recorded")

    def test_deleting_first_reach_updates_the_selected_duty_milestone(self):
        archived, selected = self.history()
        self.assertIs(self.tab.session, selected)
        with self.confirm():
            self.assertTrue(self.tab.delete_pull(archived, archived["pulls"][0]))
        self.assertIs(self.tab.session, selected)
        self.assertEqual(self.tab.milestone_table.item(0, 1).text(),
                         f"{datetime.fromtimestamp(1700100007):%Y-%m-%d %H:%M:%S}")
        self.assertEqual(self.tab.milestone_table.item(0, 2).text(), "60.00%")

    def test_unchanged_progress_does_not_rebuild_the_table_on_ticks_or_note_edits(self):
        self.history()
        with patch.object(self.tab.milestone_table, "setRowCount") as rebuild:
            self.tab.tick()
            self.tab.note.setPlainText("Saved review note")
            self.tab.flush()
            self.tab.tick()
        rebuild.assert_not_called()

    def test_duty_metadata_change_clears_boss_identity_without_waiting_for_a_zone_log(self):
        self.case.connect()
        actor = "40000001"
        self.case.line(["03", "ts", actor, "Boss", "00", "80", "00000000",
                        "0", "0", "0", "19504", "6050", "1000000"])
        self.assertIn(int(actor, 16), self.sessions._boss_actors)
        self.window._on_ws_zone_changed(1, "Test duty")
        self.assertIn(int(actor, 16), self.sessions._boss_actors)
        self.assertIsNone(self.sessions.current)
        self.window._on_ws_zone_changed(2, "Other duty")
        self.assertEqual(self.sessions._boss_actors, {})


if __name__ == "__main__":
    unittest.main()
