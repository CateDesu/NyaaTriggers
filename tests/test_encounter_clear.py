from dataclasses import replace
import unittest

from nyaatriggers.prog_phases import read_tracking
from nyaatriggers.prog_session import ProgSessions
from tests import test_prog_phases as fixture
from tests.test_session_features import ability


class ClearTests(unittest.TestCase):
    setUp = fixture.PhaseTests.setUp
    dispatch = fixture.PhaseTests.dispatch
    combat = fixture.PhaseTests.combat
    line = fixture.PhaseTests.line
    begin = fixture.PhaseTests.begin

    def victory(self, command="40000003"):
        self.line(["33", "ts", "80000001", command, "00", "00", "00", "00"])

    def test_victory_before_combat_end_survives_duplicate_events_and_restart(self):
        pull = self.begin()
        self.clock.value += 15
        self.line(ability())
        self.victory()
        self.assertEqual(pull["ending"], "cleared")
        self.assertTrue(pull["complete"])
        self.assertEqual(pull["duration"], 15)
        self.assertEqual(pull["phase_tracking"]["coverage"], "complete")
        self.assertEqual(read_tracking(pull, self.session["zone_id"], (self.definition,))[2], "")
        self.victory()
        self.combat(False)
        self.line(["33", "ts", "80000001", "4000000F"])
        self.assertEqual(pull["ending"], "cleared")
        self.assertEqual(len(self.session["pulls"]), 1)
        self.sessions.end()
        restored = ProgSessions(self.directory, definitions=(self.definition,))
        self.assertEqual(restored.sessions[0]["pulls"], [pull])

    def test_delayed_victory_and_variant_victory_update_only_recent_combat_end(self):
        for command, delay, expected in (("40000003", 3.5, "cleared"),
                                         ("40000002", 1, "cleared"),
                                         ("40000003", 6, "combat-ended")):
            with self.subTest(command=command, delay=delay):
                pull = self.begin()
                self.combat(False)
                deadline = self.sessions._recap_until
                self.clock.value += delay
                self.victory(command)
                self.assertEqual(pull["ending"], expected)
                self.assertEqual(self.meter.snapshot()["Encounter"]["end_reason"], expected)
                self.assertEqual(self.sessions._recap_until, deadline)

    def test_clear_during_a_phase_transition_closes_the_whole_pull(self):
        pull = self.begin()
        self.clock.value += 10
        self.line(fixture.marker(0, transition=True))
        self.combat(False)
        self.assertEqual(pull["ending"], "active")
        self.victory()
        self.assertEqual(pull["ending"], "cleared")
        self.assertIsNone(self.sessions.pending)
        self.assertIsNone(pull["phase_tracking"]["transition"])

    def test_combat_end_and_other_director_commands_do_not_claim_a_clear(self):
        pull = self.begin()
        self.combat(False)
        for command in ("40000001", "40000007", "80000001", "invalid"):
            self.victory(command)
        self.assertEqual(pull["ending"], "combat-ended")

    def test_feed_loss_and_zone_change_prevent_a_late_clear(self):
        pull = self.begin()
        self.meter.feed_lost()
        self.dispatch()
        self.sessions.feed_lost()
        self.victory()
        self.assertEqual(pull["ending"], "feed-lost")
        self.combat(False)
        pull = self.begin()
        self.combat(False)
        self.sessions.end(reason="duty-left")
        self.victory()
        self.assertEqual(pull["ending"], "combat-ended")

    def test_victory_is_supported_without_phase_rules(self):
        self.sessions.definition = None
        self.combat(False)
        self.combat(True)
        self.line(ability())
        pull = self.session["pulls"][-1]
        self.combat(False)
        self.clock.value += 3
        self.victory()
        self.assertEqual(pull["ending"], "cleared")
        self.assertTrue(pull["complete"])
        self.assertIsNone(pull["phase_tracking"])

    def test_clear_remains_complete_with_continuous_phase_tracking(self):
        self.definition = replace(self.definition, transitions=(), continuous_combat=True)
        self.sessions.definitions = (self.definition,)
        self.sessions.definition = self.definition
        pull = self.begin()
        self.victory()
        self.assertEqual(pull["ending"], "cleared")
        self.assertEqual(pull["phase_tracking"]["coverage"], "complete")


if __name__ == "__main__":
    unittest.main()
