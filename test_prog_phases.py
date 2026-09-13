"""Phase evidence, logical attempts, and durable observations."""

from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from death_recap import DeathRecap
from dps_meter import DpsMeter
from prog_phases import (DEFINITIONS, UMAD_PHASES, UMAD_ZONE, PhaseDefinition,
                         PhaseRule, TransitionRule, definition_for, read_tracking)
from prog_session import ProgSessions
from record_store import read_record, write_record
from test_session_features import Clock, PLAYER, BOSS, ability


def fixture_definition(revision=1):
    return PhaseDefinition("fixture.umad", revision, UMAD_ZONE, UMAD_PHASES,
                           tuple(PhaseRule(f"phase-{i}", "20", 0xA000 + i, phase, starts_pull=i == 0)
                                 for i, phase in enumerate(UMAD_PHASES)),
                           tuple(TransitionRule(f"transition-{i}", "20", 0xB000 + i,
                                                UMAD_PHASES[i], UMAD_PHASES[i + 1], 30)
                                 for i in range(4)), verified=True)


def marker(index, transition=False, actor=BOSS):
    return ["20", "ts", actor, "Localized actor", f"{(0xB000 if transition else 0xA000) + index:X}", "Localized action"]


class PhaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.clock = Clock()
        self.definition = fixture_definition()
        self.sessions = ProgSessions(self.directory, self.clock, definitions=(self.definition,))
        self.meter = DpsMeter(self.clock)
        self.meter.set_me(PLAYER)
        self.notifications = []
        self.meter.on_pull_start = lambda s: self.notifications.append(("start", s))
        self.meter.on_pull_finish = lambda s: self.notifications.append(("finish", s))
        self.recap = DeathRecap(self.clock)
        self.recap.on_death = self.sessions.record_death
        self.session = self.sessions.start("Phase test", UMAD_ZONE, "Duty", False)

    def dispatch(self, fields=()):
        self.sessions.process_event(fields, self.notifications, self.clock())
        self.notifications.clear()
        self.sessions.update_active(self.meter.full_snapshot())
        if fields:
            self.recap.process(fields)

    def combat(self, value):
        self.sessions.combat(value)
        self.meter.set_in_combat(value, value)
        self.dispatch()

    def line(self, fields):
        self.meter.process(fields)
        self.dispatch(fields)

    def begin(self):
        self.combat(True)
        self.line(marker(0))
        self.line(ability())
        return self.session["pulls"][-1]

    def transition(self):
        pull = self.begin()
        self.clock.value += 2
        self.line(marker(0, transition=True))
        self.combat(False)
        return pull

    def restart(self):
        return ProgSessions(self.directory, self.clock, definitions=(self.definition,))

    def test_umad_candidates_are_not_enabled(self):
        self.assertIsNone(definition_for(UMAD_ZONE, DEFINITIONS))
        self.assertIsNone(definition_for(1, (self.definition,)))

    def test_confirmations_deduplicate_and_do_not_invent_earlier_times(self):
        pull = self.begin()
        self.clock.value += 10
        self.line(marker(0))
        self.line(marker(2, actor=PLAYER))
        self.line(marker(2))
        self.clock.value += 2
        self.line(marker(1))
        self.line(marker(2))
        self.assertEqual([(o["phase"], o["at"]) for o in pull["phase_tracking"]["observations"]],
                         [("p1", 0), ("p3", 10)])
        self.assertEqual(read_tracking(pull, UMAD_ZONE, (self.definition,))[2], "")

    def test_verified_transition_keeps_ids_deaths_notes_and_recaps(self):
        pull = self.begin()
        pull.update(note="Remember towers", bookmark=True)
        self.clock.value += 1
        self.line(["25", "ts", PLAYER, "Player"])
        self.clock.value += 1
        self.line(marker(0, transition=True))
        self.combat(False)
        self.assertEqual(pull["ending"], "active")
        self.assertEqual(self.sessions.pending, pull["id"])
        self.clock.value += 8
        self.combat(True)
        second_id = self.meter.current.pull_id
        self.assertNotEqual(second_id, pull["id"])
        self.line(marker(1))
        self.clock.value += 1
        self.line(["25", "ts", PLAYER, "Player"])
        self.clock.value += 9
        self.line(ability())
        self.combat(False)
        self.assertEqual(len(self.session["pulls"]), 1)
        self.assertEqual(pull["duration"], 20)
        self.assertEqual(pull["deaths"], 2)
        self.assertEqual(pull["recap_count"], 2)
        self.assertEqual(pull["phase_tracking"]["coverage"], "complete")
        self.assertTrue(pull["bookmark"])
        self.assertEqual(pull["note"], "Remember towers")
        recaps, errors = self.sessions.recaps.load(self.session["id"], pull["id"])
        self.assertEqual(errors, [])
        self.assertEqual(len(recaps), 2)
        restored = self.restart().sessions[0]["pulls"][0]
        self.assertEqual(restored, pull)

    def test_transition_without_combat_drop(self):
        pull = self.begin()
        self.line(marker(0, transition=True))
        self.clock.value += 10
        self.line(marker(1))
        self.combat(False)
        self.assertEqual(pull["duration"], 10)
        self.assertEqual(pull["phase_tracking"]["coverage"], "complete")

    def test_stalled_transition_expires_without_counting_idle_tail(self):
        pull = self.transition()
        self.clock.value += 30
        self.sessions.check_phase_timeout()
        self.assertIsNone(self.sessions.pending)
        self.assertEqual(pull["ending"], "boundary-uncertain")
        self.assertFalse(pull["complete"])
        self.assertEqual(pull["duration"], 0)
        self.assertFalse(self.sessions.ready)

    def test_duplicate_transition_does_not_extend_deadline(self):
        pull = self.transition()
        self.clock.value += 20
        self.line(marker(0, transition=True))
        self.clock.value += 10
        self.sessions.check_phase_timeout()
        self.assertEqual(pull["ending"], "boundary-uncertain")

    def test_wipe_during_intermission_closes_pull_and_accepts_next(self):
        pull = self.transition()
        self.line(["33", "ts", "0", "4000000F"])
        self.assertEqual(pull["ending"], "wipe")
        self.assertIsNone(self.sessions.pending)
        self.begin()
        self.assertEqual(len(self.session["pulls"]), 2)

    def test_feed_loss_in_gap_keeps_observations_and_waits_for_reset(self):
        pull = self.transition()
        self.meter.feed_lost()
        self.dispatch()
        self.sessions.feed_lost()
        self.assertEqual(pull["ending"], "feed-lost")
        self.assertEqual(len(pull["phase_tracking"]["observations"]), 1)
        self.combat(False)
        self.combat(True)
        self.line(marker(1))
        self.assertEqual(len(self.session["pulls"]), 1)
        self.line(["33", "ts", "0", "4000000F"])
        self.combat(False)
        self.begin()
        self.assertEqual(len(self.session["pulls"]), 2)

    def test_shutdown_in_transition_and_restart_are_interrupted(self):
        pull = self.transition()
        restored = self.restart().sessions[0]["pulls"][0]
        self.assertEqual(restored["phase_tracking"]["coverage"], "interrupted")
        self.assertIsNone(restored["phase_tracking"]["transition"])
        self.sessions.end(reason="program-closed")
        self.assertEqual(pull["ending"], "program-closed")

    def test_candidate_ending_without_confirmation_is_uncertain(self):
        pull = self.transition()
        self.combat(True)
        self.line(ability())
        self.combat(False)
        self.assertEqual(pull["ending"], "boundary-uncertain")
        self.assertEqual(len(self.session["pulls"]), 1)

    def test_duplicate_old_segment_ending_does_not_close_continuation(self):
        pull = self.transition()
        old = deepcopy(self.meter._last_final)
        self.combat(True)
        self.line(marker(1))
        self.sessions.pull_finished(old)
        self.assertEqual(pull["ending"], "active")
        self.assertEqual(self.sessions.pending, pull["id"])

    def test_missing_transition_evidence_does_not_join_pulls(self):
        first = self.begin()
        self.combat(False)
        second = self.begin()
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(len(self.session["pulls"]), 2)

    def test_malformed_optional_data_preserves_notes_recaps_and_raw_block(self):
        pull = self.begin()
        self.line(["25", "ts", PLAYER, "Player"])
        self.combat(False)
        pull["phase_tracking"] = {"version": 999, "unknown": ["keep this"]}
        write_record(self.directory, self.session)
        loaded = self.restart()
        saved = loaded.sessions[0]
        self.assertTrue(read_tracking(saved["pulls"][0], UMAD_ZONE, (self.definition,))[2])
        saved["pulls"][0]["note"] = "Still editable"
        self.assertTrue(loaded.save(saved))
        stored = read_record(self.directory / (saved["id"] + ".json"))
        self.assertEqual(stored["pulls"][0]["phase_tracking"], pull["phase_tracking"])
        self.assertEqual(len(loaded.recaps.load(saved["id"], pull["id"])[0]), 1)

    def test_phase_validation_rejects_invalid_times_and_order(self):
        pull = self.begin()
        for value in (True, -1, float("nan"), float("inf"), 10 ** 1000):
            damaged = deepcopy(pull)
            damaged["phase_tracking"]["observations"][0]["at"] = value
            self.assertTrue(read_tracking(damaged, UMAD_ZONE, (self.definition,))[2])
        damaged = deepcopy(pull)
        damaged["phase_tracking"]["observations"] *= 2
        self.assertTrue(read_tracking(damaged, UMAD_ZONE, (self.definition,))[2])

    def test_observation_retry_preserves_newer_note_and_disk_record(self):
        pull = self.begin()
        path = self.directory / (self.session["id"] + ".json")
        before = path.read_bytes()
        self.clock.value += 5
        with patch("record_store.os.replace", side_effect=OSError("Disk full")):
            self.line(marker(1))
        self.assertEqual(path.read_bytes(), before)
        self.assertTrue(self.sessions.save_error)
        pull["note"] = "Newest note"
        self.sessions.flush_pending()
        saved = read_record(path)["pulls"][0]
        self.assertEqual(saved["note"], "Newest note")
        self.assertEqual(saved["phase_tracking"]["observations"][-1]["phase"], "p2")

    def test_wall_clock_changes_do_not_move_confirmations(self):
        pull = self.begin()
        self.sessions.wall = lambda: 1
        self.clock.value += 5
        self.line(marker(1))
        self.assertEqual(pull["phase_tracking"]["observations"][-1]["at"], 5)

    def test_phase_only_wipe_retains_confirmed_pull(self):
        self.combat(True)
        self.line(marker(0))
        self.line(["33", "ts", "0", "4000000F"])
        pull = self.session["pulls"][0]
        self.assertEqual(pull["ending"], "wipe")
        self.assertTrue(pull["complete"])

    def test_new_initial_marker_cannot_be_accepted_as_a_continuation(self):
        pull = self.transition()
        self.combat(True)
        self.line(marker(0))
        self.assertEqual(pull["ending"], "boundary-uncertain")
        self.line(marker(1))
        self.assertEqual([o["phase"] for o in pull["phase_tracking"]["observations"]], ["p1"])

    def test_old_first_segment_cannot_overwrite_a_saved_joined_pull(self):
        pull = self.transition()
        old = deepcopy(self.meter._last_final)
        self.clock.value += 10
        self.combat(True)
        self.line(marker(1))
        self.line(ability())
        self.combat(False)
        before = deepcopy(pull)
        self.begin()
        self.sessions.pull_finished(old)
        self.assertEqual(pull, before)

    def test_phase_only_disconnect_retains_actual_ending_reason(self):
        self.combat(True)
        self.line(marker(0))
        pull = self.session["pulls"][0]
        self.meter.feed_lost()
        self.dispatch()
        self.sessions.feed_lost()
        self.assertEqual(pull["ending"], "feed-lost")
        self.assertEqual(pull["phase_tracking"]["reason"], "feed-lost")

    def test_session_started_in_an_intermission_waits_for_initial_evidence(self):
        self.combat(True)
        self.line(marker(2))
        self.combat(False)
        self.assertEqual(self.session["pulls"], [])
        self.assertFalse(self.sessions.ready)
        self.begin()
        self.assertEqual(len(self.session["pulls"]), 1)

    def test_detector_exception_preserves_observations_as_uncertain(self):
        pull = self.begin()
        with patch.object(self.sessions.attempt, "observe", side_effect=RuntimeError("Detector failed")):
            self.line(marker(1))
        self.assertEqual(pull["phase_tracking"]["coverage"], "uncertain")
        self.assertIsNone(self.sessions.pending)

    def test_ended_segment_updates_cannot_reduce_joined_death_totals(self):
        pull = self.begin()
        self.line(["25", "ts", PLAYER, "Player"])
        self.line(marker(0, transition=True))
        self.combat(False)
        stale = deepcopy(self.meter._last_final)
        stale["Encounter"]["deaths"] = 0
        self.combat(True)
        self.line(marker(1))
        self.sessions.update_active(stale)
        self.assertEqual(self.sessions.attempt.segments[pull["id"]][2], 1)


if __name__ == "__main__":
    unittest.main()
