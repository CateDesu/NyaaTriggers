from dataclasses import replace
import unittest
from unittest.mock import Mock, patch

from nyaatriggers.dps_meter import DpsMeter
from nyaatriggers.ui.prog_tab import ENDING_COLUMN
from nyaatriggers.prog_phases import read_tracking
from tests import test_pull_deletion_ui as fixture
from tests.test_prog_phases import fixture_definition, marker
from tests.test_session_features import BOSS, PLAYER, Clock, ability


class ClearBoundaryTests(unittest.TestCase):
    setUpClass = classmethod(fixture.PullDeletionUiTests.setUpClass.__func__)
    setUp = fixture.PullDeletionUiTests.setUp
    cleanup_case = fixture.PullDeletionUiTests.cleanup_case
    reload = fixture.PullDeletionUiTests.reload

    def finish_pull(self):
        self.window._on_in_combat(True, True)
        self.case.line(ability(source=PLAYER, target=BOSS))
        self.case.clock.value += 12
        self.case.line(ability(source=PLAYER, target=BOSS))
        self.window._on_in_combat(False, False)

    def check_reconnected_ending(self, command):
        self.case.connect()
        self.finish_pull()
        session = self.sessions.current
        pull = session["pulls"][0]
        self.assertEqual(pull["ending"], "combat-ended")
        self.assertEqual(self.window._dps_history[0]["snapshot"]["Encounter"]["damage"], 2000)
        self.window._ws.status_changed.emit(False, "Disconnected")
        self.window._ws.status_changed.emit(True, "Connected")
        self.window._ws.zone_changed.emit(1, "Test duty")
        self.case.clock.value += 1
        self.case.line(["33", "ts", "80000001", command])
        self.assertEqual(pull["ending"], "combat-ended")
        final = self.window._dps_meter.snapshot()
        self.assertEqual(final["Encounter"]["damage"], 2000)
        self.assertEqual(final["Encounter"]["end_reason"], "combat-ended")
        self.assertEqual(self.window._dps_history[0]["snapshot"]["Encounter"]["end_reason"], "combat-ended")
        self.assertEqual(self.reload()[0]["pulls"][0]["ending"], "combat-ended")
        self.window._ws.in_combat.emit(False, False)
        self.finish_pull()
        self.case.clock.value += 1
        self.case.line(["33", "ts", "80000001", command])
        expected = "wipe" if command == "4000000F" else "cleared"
        self.assertEqual([row["ending"] for row in session["pulls"]], ["combat-ended", expected])
        self.assertEqual([row["ending"] for row in self.reload()[0]["pulls"]], ["combat-ended", expected])

    def test_reconnect_prevents_a_late_victory_reclassifying_an_earlier_pull(self):
        self.check_reconnected_ending("40000003")

    def test_reconnect_prevents_a_late_variant_victory_reclassifying_an_earlier_pull(self):
        self.check_reconnected_ending("40000002")

    def test_reconnect_prevents_a_late_wipe_reclassifying_an_earlier_pull(self):
        self.check_reconnected_ending("4000000F")

    def check_active_victory(self, command):
        self.case.connect()
        self.window._on_in_combat(True, True)
        self.case.line(ability(source=PLAYER, target=BOSS))
        self.case.clock.value += 12
        self.case.line(ability(source=PLAYER, target=BOSS))
        pull = self.sessions.current["pulls"][0]
        self.assertIsNone(pull["phase_tracking"])
        self.assertEqual(pull["ending"], "active")
        self.case.line(["33", "ts", "80000001", command])
        self.assertIsNone(self.window._dps_meter.current)
        self.assertIsNone(self.sessions.pending)
        self.assertEqual(len(self.window._dps_history), 1)
        encounter = self.window._dps_history[0]["snapshot"]["Encounter"]
        self.assertEqual(encounter["damage"], 2000)
        self.assertEqual(encounter["end_reason"], "cleared")
        self.assertEqual(pull["duration"], 12)
        self.assertEqual(pull["ending"], "cleared")
        self.assertTrue(pull["complete"])
        self.assertEqual(self.tab.table.item(0, ENDING_COLUMN).text(), "Cleared")
        self.assertEqual(self.reload()[0]["pulls"][0]["ending"], "cleared")
        self.case.line(["33", "ts", "80000001", command])
        self.window._on_in_combat(False, False)
        self.case.line(["33", "ts", "80000001", "4000000F"])
        self.assertEqual(len(self.window._dps_history), 1)
        self.assertEqual(len(self.sessions.current["pulls"]), 1)
        self.assertEqual(encounter["end_reason"], "cleared")
        self.assertEqual(self.reload()[0]["pulls"][0]["ending"], "cleared")

    def test_active_victory_finalizes_meter_and_saved_pull_without_phase_tracking(self):
        self.check_active_victory("40000003")

    def test_active_variant_victory_finalizes_meter_and_saved_pull_without_phase_tracking(self):
        self.check_active_victory("40000002")

    def test_delayed_clear_still_reaches_the_real_session_page_and_saved_record(self):
        self.case.connect()
        self.finish_pull()
        pull = self.sessions.current["pulls"][0]
        self.case.clock.value += 3.5
        self.case.line(["33", "ts", "80000001", "40000003"])
        self.assertEqual(pull["ending"], "cleared")
        self.assertEqual(self.tab.table.item(0, ENDING_COLUMN).text(), "Cleared")
        self.assertEqual(self.window._dps_history[0]["snapshot"]["Encounter"]["end_reason"], "cleared")
        self.assertEqual(self.reload()[0]["pulls"][0]["ending"], "cleared")

    def test_clear_closes_a_confirmed_phase_even_without_a_damage_sample(self):
        self.case.connect()
        definition = replace(fixture_definition(), transitions=(), continuous_combat=True)
        self.sessions.definitions = (definition,)
        self.window._on_ws_zone_changed(definition.zone_id, "UMAD")
        self.window._on_in_combat(True, True)
        self.case.line(marker(0))
        pull = self.sessions.current["pulls"][0]
        self.assertEqual(pull["phase_tracking"]["observations"][0]["phase"], "p1")
        self.case.line(["33", "ts", "80000001", "40000003"])
        self.assertEqual(pull["ending"], "cleared")
        self.assertTrue(pull["complete"])
        self.assertEqual(pull["phase_tracking"]["coverage"], "complete")
        self.assertEqual(read_tracking(pull, definition.zone_id, (definition,))[2], "")
        self.assertEqual(self.reload()[0]["pulls"][0]["ending"], "cleared")


class MeterEndingBoundaryTests(unittest.TestCase):
    def test_real_zone_boundaries_retire_corrections_and_metadata_refresh_keeps_them(self):
        for boundary in ("log", "id", "name", "metadata", "metadata-name"):
            with self.subTest(boundary=boundary):
                clock = Clock()
                meter = DpsMeter(clock)
                meter.set_me(PLAYER)
                meter.set_zone_metadata("First duty")
                meter.set_in_combat(True, True)
                meter.process(ability(source=PLAYER, target=BOSS))
                clock.value += 10
                meter.set_in_combat(False, False)
                observer = Mock()
                with patch.object(meter, "on_pull_finish", observer):
                    if boundary == "log":
                        meter.process(["01", "ts", "2", "Second duty"])
                    elif boundary == "id":
                        meter.set_zone_metadata("First duty", zone_changed=True)
                    elif boundary == "name":
                        meter.set_zone_metadata("Second duty")
                    elif boundary == "metadata":
                        meter.set_zone_metadata("First duty", zone_changed=False)
                    else:
                        meter.set_zone_metadata("Localized first duty", zone_changed=False)
                    clock.value += 1
                    meter.process(["33", "ts", "80000001", "40000003"])
                    if boundary.startswith("metadata"):
                        observer.assert_called_once()
                        self.assertEqual(observer.call_args.args[0]["Encounter"]["end_reason"], "cleared")
                    else:
                        observer.assert_not_called()
                final = meter.snapshot()["Encounter"]
                self.assertEqual(final["damage"], 1000)
                self.assertEqual(final["end_reason"], "cleared" if boundary.startswith("metadata") else "combat-ended")


if __name__ == "__main__":
    unittest.main()
