"""Phase evidence, logical attempts, and durable observations."""

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from nyaatriggers.death_recap import DeathRecap
from nyaatriggers.dps_meter import DpsMeter
from nyaatriggers.prog_phases import (DEFINITIONS, UMAD_PHASES, UMAD_ZONE, PhaseDefinition,
                         PhaseRule, TransitionRule, definition_for, read_tracking)
from nyaatriggers.prog_session import ProgSessions
from nyaatriggers.record_store import read_record, write_record
from tests.test_session_features import Clock, PLAYER, BOSS, ability


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

    def test_umad_definition_is_available_only_in_its_duty(self):
        self.assertEqual(definition_for(UMAD_ZONE, DEFINITIONS).phases, UMAD_PHASES)
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
        with patch("nyaatriggers.record_store.os.replace", side_effect=OSError("Disk full")):
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


class UmadPhaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.clock = Clock()
        self.sessions = ProgSessions(self.temp.name, self.clock)
        self.meter = DpsMeter(self.clock)
        self.meter.set_me("10000001")
        self.events = []
        self.meter.on_pull_start = lambda value: self.events.append(("start", value))
        self.meter.on_pull_finish = lambda value: self.events.append(("finish", value))
        self.session = self.sessions.start("UMAD", UMAD_ZONE, "Duty", False)

    def dispatch(self, fields=()):
        self.sessions.process_event(fields, self.events, self.clock(), self.meter.full_snapshot())
        self.events.clear()

    def combat(self, act, game=None):
        game = act if game is None else game
        self.sessions.combat(game)
        self.meter.set_in_combat(act, game)
        self.dispatch()

    def line(self, fields):
        self.meter.process(fields)
        self.dispatch(fields)

    def cast(self, ability_id, event="20", actor=BOSS):
        self.line([event, "ts", actor, "Localized boss", ability_id, "Localized action"])

    def begin(self):
        self.combat(True)
        self.line(ability(target="10000001"))
        return self.session["pulls"][-1]

    def wipe(self):
        self.line(["33", "ts", "80000001", "4000000F"])

    def test_recorded_umad_pulls_confirm_p4_without_false_p5(self):
        path = Path(__file__).parent / "fixtures/prog/umad-2026-09-13.jsonl"
        for raw in path.read_text().splitlines():
            event = json.loads(raw)
            self.clock.value = 1000 + event["at"]
            if "combat" in event:
                self.combat(*event["combat"])
            else:
                self.line(event["line"])
        pulls = self.session["pulls"]
        self.assertEqual(len(pulls), 5)
        self.assertEqual([p["ending"] for p in pulls], ["wipe"] * 5)
        self.assertEqual([p["phase_tracking"]["observations"][-1]["phase"] for p in pulls],
                         ["p1", "p4", "p1", "p4", "p1"])
        observations = pulls[1]["phase_tracking"]["observations"]
        self.assertEqual([o["phase"] for o in observations], ["p1", "p2", "p3", "p4"])
        self.assertAlmostEqual(observations[2]["at"], 385.0, delta=2)
        for pull in pulls:
            self.assertEqual(read_tracking(pull, UMAD_ZONE)[2], "")
        self.sessions.end()
        saved = ProgSessions(self.temp.name).sessions[0]
        self.assertEqual(saved["pulls"], pulls)

    def test_reused_actions_and_elapsed_time_do_not_confirm_p5(self):
        pull = self.begin()
        for ident in ("C403", "C24C", "C3F7", "C2DC"):
            self.cast(ident)
            self.clock.value += 10
        self.cast("C24A")
        for _ in range(8):
            self.cast("C3FD", "21")
        self.clock.value += 1800
        self.sessions.update_active(self.meter.full_snapshot())
        self.cast("BB40", actor=PLAYER)
        observations = pull["phase_tracking"]["observations"]
        self.assertEqual(observations[-1]["phase"], "p4")
        self.cast("BB40")
        first_p5 = deepcopy(observations[-1])
        self.clock.value += 30
        self.cast("BB40", "22")
        self.cast("C403")
        self.assertEqual(observations[-1], first_p5)
        self.assertEqual([o["phase"] for o in observations], list(UMAD_PHASES))

    def test_effect_confirmations_recover_missed_casts_without_inventing_times(self):
        pull = self.begin()
        self.clock.value += 450
        self.cast("C3F7", "22")
        self.cast("C24C", "21")
        self.assertEqual([(o["phase"], o["at"]) for o in pull["phase_tracking"]["observations"]],
                         [("p3", 450)])

    def test_quick_wipe_without_phase_evidence_is_still_a_pull(self):
        pull = self.begin()
        self.clock.value += 2
        self.combat(False)
        self.clock.value += 3.5
        self.wipe()
        self.assertEqual(pull["ending"], "wipe")
        self.assertEqual(pull["phase_tracking"]["observations"], [])
        self.assertTrue(pull["complete"])
        self.assertEqual(pull["duration"], 0)

    def test_empty_encounter_does_not_skip_the_next_umad_pull(self):
        self.combat(True)
        self.clock.value += 2
        self.combat(False)
        self.assertEqual(self.session["pulls"], [])
        self.assertTrue(self.sessions.ready)
        pull = self.begin()
        self.cast("C403")
        self.assertEqual(self.session["pulls"], [pull])
        self.assertEqual(pull["phase_tracking"]["observations"][0]["phase"], "p1")

    def test_phase_only_combat_end_keeps_its_boundary_and_late_wipe(self):
        self.combat(True)
        self.clock.value += 4
        self.cast("C403")
        pull = self.session["pulls"][0]
        self.clock.value += 2
        self.combat(False)
        self.assertEqual(pull["ending"], "combat-ended")
        self.assertTrue(pull["complete"])
        self.assertTrue(self.sessions.ready)
        self.assertEqual(pull["duration"], 4)
        death_deadline = self.sessions._recap_until
        self.clock.value += 3.5
        self.wipe()
        self.assertEqual(pull["ending"], "wipe")
        self.assertEqual(self.sessions._recap_until, death_deadline)
        self.assertEqual(read_tracking(pull, UMAD_ZONE)[2], "")
        saved = ProgSessions(self.temp.name).sessions[0]["pulls"][0]
        self.assertEqual(saved, pull)
        self.begin()
        self.assertEqual(len(self.session["pulls"]), 2)

    def test_midpull_start_and_reconnect_wait_for_a_new_pull(self):
        self.sessions.end()
        self.combat(True)
        self.session = self.sessions.start("Late", UMAD_ZONE, "Duty", True)
        self.cast("C403")
        self.cast("C2DC")
        self.assertEqual(self.session["pulls"], [])
        self.combat(False)
        first = self.begin()
        self.cast("C403")
        self.meter.feed_lost()
        self.dispatch()
        self.sessions.feed_lost()
        self.combat(True)
        self.cast("C2DC")
        self.assertEqual(self.session["pulls"], [first])
        self.combat(False)
        second = self.begin()
        self.cast("C403")
        self.assertIsNot(first, second)
        self.assertEqual(second["phase_tracking"]["observations"][0]["phase"], "p1")

    def test_late_wipe_window_expires_and_does_not_extend_death_attribution(self):
        pull = self.begin()
        self.combat(False)
        death_deadline = self.sessions._recap_until
        self.clock.value += 3
        self.wipe()
        self.assertEqual(pull["ending"], "wipe")
        self.assertEqual(self.sessions._recap_until, death_deadline)
        self.wipe()
        self.assertEqual(self.sessions._recap_until, death_deadline)
        next_pull = self.begin()
        self.combat(False)
        self.clock.value += 3
        self.sessions.pull_finished(self.meter._last_final)
        self.clock.value += 3
        self.wipe()
        self.assertEqual(next_pull["ending"], "combat-ended")

    def test_new_pull_and_feed_loss_clear_late_wipe_candidate(self):
        first = self.begin()
        self.combat(False)
        second = self.begin()
        self.wipe()
        self.assertEqual(first["ending"], "combat-ended")
        self.assertEqual(second["ending"], "wipe")
        self.combat(False)
        third = self.begin()
        self.combat(False)
        self.sessions.feed_lost()
        self.wipe()
        self.assertEqual(third["ending"], "combat-ended")


if __name__ == "__main__":
    unittest.main()
