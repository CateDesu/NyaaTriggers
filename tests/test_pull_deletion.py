from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

from nyaatriggers import prog_session
from nyaatriggers.prog_session import ProgSessions
from nyaatriggers.record_store import RecordWriter, read_record, write_record
from tests.test_prog_phases import fixture_definition, marker
from tests.test_session_features import Clock


class PullDeletionTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.clock = Clock()
        self.sessions = ProgSessions(self.directory, self.clock, wall=lambda: 1000)
        self.session = self.sessions.start("Raid night", 1, "Duty", False)

    def snapshot(self, ident=None, reason="active", duration=0):
        return {"Encounter": {"pull_id": ident or str(uuid4()), "wall_start": 1000,
                              "monotonic_start": self.clock(), "last_activity": self.clock(),
                              "DURATION": duration, "deaths": 0, "end_reason": reason}}

    def begin(self):
        snapshot = self.snapshot()
        self.sessions.combat(True)
        self.assertTrue(self.sessions.pull_started(snapshot))
        return self.session["pulls"][-1]

    def finish(self, pull, reason="combat-ended"):
        self.clock.value += 1
        self.sessions.combat(False)
        self.sessions.pull_finished(self.snapshot(pull["id"], reason, 1))

    def death(self):
        return {"id": str(uuid4()), "actor": 0x10000001, "name": "Player", "zone": "Duty",
                "when": 1000, "events": [], "statuses": []}

    def recap(self):
        result = self.sessions.record_death(self.death())
        self.assertIsNotNone(result)
        return result

    def saved(self):
        return read_record(self.directory / (self.session["id"] + ".json"))

    def test_deletes_saved_pull_and_owned_recaps_without_changing_other_pulls(self):
        first = self.begin()
        first_recap = self.recap()
        self.finish(first)
        second = self.begin()
        second_recap = self.recap()
        second.update(note="Keep this note", bookmark=True)
        self.finish(second)
        self.sessions.end()
        self.session["archived"] = True
        self.sessions.save(self.session)
        self.assertTrue(self.sessions.can_delete_pull(self.session, first))
        self.assertIsNone(self.sessions.delete_pull(self.session, first))
        self.assertEqual(self.session["pulls"], [second])
        self.assertIs(self.session["pulls"][0], second)
        self.assertEqual(self.saved()["pulls"], [second])
        self.assertTrue(self.saved()["archived"])
        self.assertEqual(self.sessions.recaps.load(self.session["id"], first["id"]), ([], []))
        self.assertEqual(self.sessions.recaps.load(self.session["id"], second["id"]), ([second_recap], []))
        self.assertFalse((self.directory / "recaps" / self.session["id"] / first["id"]).exists())
        self.assertNotIn(first_recap["id"], self.sessions.recaps.unsaved)
        self.assertEqual(ProgSessions(self.directory).sessions[0]["pulls"], [second])

    def test_deleting_previous_pull_preserves_current_capture_and_recap_ownership(self):
        first = self.begin()
        first_recap = self.recap()
        with patch("nyaatriggers.recap_store.write_record", side_effect=OSError("Old recap retry")):
            self.sessions.recaps.save(first_recap)
        self.finish(first)
        active = self.begin()
        current_recap = self.recap()
        with patch("nyaatriggers.recap_store.write_record", side_effect=OSError("Current recap retry")):
            self.sessions.recaps.save(current_recap)
        pending = self.sessions.pending
        recap_ids = set(self.sessions._recap_ids)
        self.sessions.delete_pull(self.session, first)
        self.assertIs(self.sessions.current, self.session)
        self.assertEqual(self.sessions.pending, pending)
        self.assertEqual(self.sessions._recap_pull, active["id"])
        self.assertEqual(self.sessions._recap_ids, recap_ids)
        self.assertEqual(self.sessions.recaps.load(self.session["id"], active["id"]), ([current_recap], []))
        self.assertEqual(self.sessions.recaps.unsaved, {current_recap["id"]: current_recap})
        self.assertEqual(self.sessions.recaps.save_error, "Current recap retry")
        self.assertEqual(self.saved()["elapsed"], self.sessions.elapsed(self.session))
        self.assertIsNotNone(self.sessions.record_death(self.death()))
        self.finish(active)
        self.sessions.flush_pending(wait=True)
        self.assertEqual(self.saved()["pulls"], [active])
        self.assertEqual(active["recap_count"], 2)

    def test_active_unowned_and_invalid_selections_are_rejected_without_writing(self):
        pull = self.begin()
        before = self.saved()
        for session, selected in ((self.session, pull), (deepcopy(self.session), pull),
                                  (self.session, deepcopy(pull)), (None, pull), (self.session, None)):
            with self.subTest(session=session is self.session, pull=selected is pull):
                self.assertFalse(self.sessions.can_delete_pull(session, selected))
                with self.assertRaises(ValueError):
                    self.sessions.delete_pull(session, selected)
        self.assertEqual(self.saved(), before)
        self.assertEqual(self.sessions.pending, pull["id"])

    def test_finished_pull_deletion_retires_late_death_and_wipe_attachments(self):
        pull = self.begin()
        self.recap()
        self.finish(pull)
        self.assertEqual(self.sessions._recap_pull, pull["id"])
        self.assertIs(self.sessions._wipe_candidate[0], pull)
        self.sessions.delete_pull(self.session, pull)
        self.assertIsNone(self.sessions.record_death(self.death()))
        self.sessions.process_event(["33", "ts", "0", "4000000F"], [])
        self.sessions.pull_finished(self.snapshot(pull["id"], "wipe", 1))
        self.sessions.flush_pending(wait=True)
        self.assertIsNone(self.sessions._recap_pull)
        self.assertIsNone(self.sessions._wipe_candidate)
        self.assertEqual(self.session["pulls"], [])
        self.assertEqual(self.saved()["pulls"], [])
        self.assertFalse((self.directory / "recaps" / self.session["id"] / pull["id"]).exists())

    def test_phase_transition_rejects_until_closed_and_deleted_attempt_cannot_reappear(self):
        self.sessions.end()
        definition = fixture_definition()
        self.sessions = ProgSessions(self.directory, self.clock, definitions=(definition,))
        self.session = self.sessions.start("Phases", definition.zone_id, "Duty", False)
        snapshot = self.snapshot()
        self.sessions.process_event([], [("start", snapshot)])
        self.sessions.process_event(marker(0), [], snapshot=snapshot)
        pull = self.session["pulls"][-1]
        self.sessions.process_event(marker(0, transition=True), [])
        self.sessions.pull_finished(self.snapshot(pull["id"], "combat-ended", 1))
        self.assertTrue(self.sessions.attempt.waiting)
        self.assertFalse(self.sessions.can_delete_pull(self.session, pull))
        with self.assertRaises(ValueError):
            self.sessions.delete_pull(self.session, pull)
        self.sessions.process_event(["33", "ts", "0", "4000000F"], [])
        self.assertIsNotNone(self.sessions.last_attempt)
        self.assertTrue(self.sessions.can_delete_pull(self.session, pull))
        self.sessions.delete_pull(self.session, pull)
        self.assertIsNone(self.sessions.last_attempt)
        self.sessions.pull_finished(self.snapshot(pull["id"], "wipe", 1))
        self.sessions.process_event(marker(1), [])
        self.assertEqual(self.session["pulls"], [])
        self.assertEqual(self.saved()["pulls"], [])

    def test_interrupted_pull_with_unknown_saved_transition_can_be_deleted_after_reload(self):
        self.sessions.end()
        definition = fixture_definition()
        self.sessions = ProgSessions(self.directory, self.clock, definitions=(definition,))
        self.session = self.sessions.start("Old phase rules", definition.zone_id, "Duty", False)
        snapshot = self.snapshot()
        self.sessions.process_event([], [("start", snapshot)])
        self.sessions.process_event(marker(0), [], snapshot=snapshot)
        self.sessions.process_event(marker(0, transition=True), [])
        restarted = ProgSessions(self.directory, self.clock, definitions=())
        session = next(s for s in restarted.sessions if s["id"] == self.session["id"])
        pull = session["pulls"][0]
        self.assertEqual(pull["ending"], "program-closed")
        self.assertIsNotNone(pull["phase_tracking"]["transition"])
        self.assertTrue(restarted.can_delete_pull(session, pull))
        restarted.delete_pull(session, pull)
        self.assertEqual(session["pulls"], [])
        self.assertEqual(self.saved()["pulls"], [])

    def test_failed_atomic_summary_write_preserves_pull_recaps_and_retry_state(self):
        pull = self.begin()
        recap = self.recap()
        self.finish(pull)
        with patch.object(prog_session, "write_record", side_effect=OSError("Earlier summary failure")):
            self.sessions.save(self.session)
        with patch("nyaatriggers.recap_store.write_record", side_effect=OSError("Earlier recap failure")):
            self.sessions.recaps.save(recap)
        before = self.saved()
        with patch("nyaatriggers.record_store.os.replace", side_effect=PermissionError("Summary locked")):
            with self.assertRaisesRegex(OSError, "Summary locked"):
                self.sessions.delete_pull(self.session, pull)
        self.assertIs(self.session["pulls"][0], pull)
        self.assertEqual(self.saved(), before)
        self.assertEqual(self.sessions._recap_pull, pull["id"])
        self.assertIs(self.sessions._wipe_candidate[0], pull)
        self.assertEqual(self.sessions.unsaved, {self.session["id"]: self.session})
        self.assertEqual(self.sessions.recaps.unsaved, {recap["id"]: recap})
        self.assertEqual(self.sessions.save_error, "Earlier summary failure")
        self.assertEqual(self.sessions.recaps.save_error, "Earlier recap failure")
        self.assertEqual(self.sessions.recaps.load(self.session["id"], pull["id"]), ([recap], []))
        self.assertEqual(list(self.directory.glob(".record-*.tmp")), [])
        self.sessions.delete_pull(self.session, pull)
        self.sessions.flush_pending(wait=True)
        self.assertEqual(self.saved()["pulls"], [])
        self.assertEqual(self.sessions.unsaved, {})
        self.assertEqual(self.sessions.recaps.unsaved, {})
        self.assertEqual(self.sessions.save_error, "")
        self.assertEqual(self.sessions.recaps.save_error, "")

    def test_postcommit_recap_cleanup_failure_returns_warning_and_cannot_resurrect_pull(self):
        pull = self.begin()
        recap = self.recap()
        self.finish(pull)
        with patch("nyaatriggers.recap_store.write_record", side_effect=OSError("Recap retry")):
            self.sessions.recaps.save(recap)
        with patch.object(prog_session.shutil, "rmtree", side_effect=PermissionError("Recap locked")):
            warning = self.sessions.delete_pull(self.session, pull)
        self.assertIn("Recap locked", warning)
        self.assertEqual(self.session["pulls"], [])
        self.assertEqual(self.saved()["pulls"], [])
        self.assertEqual(self.sessions.recaps.unsaved, {})
        self.assertEqual(self.sessions.recaps.save_error, "")
        self.assertTrue((self.directory / "recaps" / self.session["id"] / pull["id"]).exists())
        self.sessions.flush_pending(wait=True)
        self.assertIsNone(self.sessions.record_death(self.death()))
        self.assertEqual(ProgSessions(self.directory).sessions[0]["pulls"], [])

    def test_queued_summary_and_recaps_are_drained_before_deletion_commit(self):
        self.sessions.end()
        writer = RecordWriter()
        self.addCleanup(writer.close)
        self.sessions = ProgSessions(self.directory, self.clock, writer=writer)
        entered, release, deleting, finished = (threading.Event() for _ in range(4))
        summary_finished, recap_finished = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        failures = []
        delete_thread = None

        def blocked(directory, data):
            if threading.current_thread() is delete_thread:
                self.assertTrue(summary_finished.is_set())
                self.assertTrue(recap_finished.is_set())
            else:
                entered.set()
                if not release.wait(3):
                    raise OSError("Test writer was not released")
            write_record(directory, data)
            if data["pulls"] and data["pulls"][-1]["ending"] == "combat-ended":
                summary_finished.set()

        def save_recap(directory, data):
            write_record(directory, data)
            recap_finished.set()

        def delete():
            deleting.set()
            try:
                self.sessions.delete_pull(self.session, pull)
            except Exception as exc:
                failures.append(exc)
            finally:
                finished.set()

        with patch.object(prog_session, "write_record", side_effect=blocked), \
                patch("nyaatriggers.recap_store.write_record", side_effect=save_recap):
            self.session = self.sessions.start("Queued", 1, "Duty", False)
            pull = self.begin()
            self.recap()
            self.finish(pull)
            self.assertTrue(entered.wait(1))
            delete_thread = threading.Thread(target=delete)
            delete_thread.start()
            try:
                self.assertTrue(deleting.wait(1))
                self.assertFalse(finished.is_set())
            finally:
                release.set()
                delete_thread.join(3)
        self.assertFalse(delete_thread.is_alive())
        self.assertEqual(failures, [])
        self.assertTrue(finished.is_set())
        self.sessions.flush_pending(wait=True)
        self.assertEqual(self.saved()["pulls"], [])
        self.assertFalse((self.directory / "recaps" / self.session["id"] / pull["id"]).exists())

    def test_deletion_preserves_unlisted_empty_pull_for_late_recap_recovery(self):
        first = self.begin()
        self.finish(first)
        empty = self.begin()
        snapshot = self.snapshot(empty["id"], "empty", 0)
        snapshot["Encounter"]["boundary_reason"] = "combat-ended"
        self.sessions.pull_finished(snapshot)
        self.assertIs(self.sessions._empty_pull, empty)
        self.assertEqual(self.session["pulls"], [first])
        self.sessions.delete_pull(self.session, first)
        self.assertIs(self.sessions._empty_pull, empty)
        self.assertEqual(self.saved()["pulls"], [empty])
        death = self.recap()
        self.assertEqual(self.session["pulls"], [empty])
        self.assertEqual(ProgSessions(self.directory).sessions[0]["pulls"][0]["id"], empty["id"])
        self.assertEqual(self.sessions.recaps.load(self.session["id"], empty["id"]), ([death], []))

    def test_deletion_recalculates_cached_first_reach_and_best_health_from_remaining_pulls(self):
        self.sessions.end()
        definition = replace(fixture_definition(), continuous_combat=True, transitions=(),
                             bosses=(("p1", (1234,)),))
        self.sessions = ProgSessions(self.directory, self.clock, wall=self.clock, definitions=(definition,))
        self.session = self.sessions.start("Progress", definition.zone_id, "Duty", False)
        pulls = []
        for current_hp in (20, 60):
            pull = self.begin()
            self.sessions.observe_phase(marker(0))
            pull["phase_tracking"]["boss_hp"] = {
                "p1": {"npc_id": 1234, "current": current_hp, "maximum": 100}}
            pulls.append(pull)
            self.finish(pull)
        before = self.sessions.phase_progress(definition.zone_id)
        self.assertEqual((before[0].first_reached, before[0].best_hp_percent), (1000, 20))
        self.sessions.delete_pull(self.session, pulls[0])
        after = self.sessions.phase_progress(definition.zone_id)
        self.assertEqual((after[0].first_reached, after[0].best_hp_percent), (1001, 60))
        self.assertIsNot(after, before)
        self.sessions.delete_pull(self.session, pulls[1])
        empty = self.sessions.phase_progress(definition.zone_id)
        self.assertIsNone(empty[0].first_reached)
        self.assertIsNone(empty[0].best_hp_percent)


if __name__ == "__main__":
    unittest.main()
