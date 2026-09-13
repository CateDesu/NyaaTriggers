"""Persistent recap history and its association with prog attempts."""

from copy import deepcopy
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from nyaatriggers.death_recap import DeathRecap, MAX_DEATHS
from nyaatriggers.dps_meter import DpsMeter
from nyaatriggers.prog_session import ProgSessions
from nyaatriggers.recap_store import MAX_PENDING_RECAPS, validate_recap
from nyaatriggers.record_store import write_record
from tests.test_session_features import Clock, PLAYER, ability


class SavedRecapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.clock = Clock()
        self.sessions = ProgSessions(self.directory, self.clock)
        self.recap = DeathRecap(self.clock)
        self.recap.zone = "Duty"
        self.recap.on_death = self.sessions.record_death
        self.meter = DpsMeter(self.clock)
        self.meter.set_me(PLAYER)
        self.meter.on_pull_start = self.sessions.pull_started
        self.meter.on_pull_finish = self.sessions.pull_finished
        self.session = self.sessions.start("Raid night", 1, "Duty", False)

    def line(self, fields):
        self.meter.process(fields)
        self.recap.process(fields)

    def combat(self, value):
        self.sessions.combat(value)
        self.meter.set_in_combat(value, value)

    def begin(self):
        self.combat(True)
        self.line(ability())
        return self.session["pulls"][-1]

    def death(self):
        self.clock.value += 1.1
        self.line(["25", "ts", PLAYER, "Player"])
        return self.recap.deaths[0]

    def load(self, pull, sessions=None):
        return (sessions or self.sessions).recaps.load(self.session["id"], pull["id"])

    def test_restart_and_live_eviction_keep_every_saved_death(self):
        pull = self.begin()
        for _ in range(MAX_DEATHS + 5):
            self.line(ability())
            self.death()
        self.combat(False)
        self.sessions.end()
        loaded = ProgSessions(self.directory)
        recaps, errors = self.load(pull, loaded)
        self.assertEqual(errors, [])
        self.assertEqual(len(recaps), MAX_DEATHS + 5)
        self.assertEqual(len(self.recap.deaths), MAX_DEATHS)
        self.assertEqual(loaded.sessions[0]["pulls"][0]["recap_count"], MAX_DEATHS + 5)
        self.assertTrue(all(d["events"][0]["amount"] == 1000 for d in recaps))

    def test_back_to_back_pulls_and_unrelated_deaths_stay_separate(self):
        self.death()
        first = self.begin()
        first_death = self.death()
        self.combat(False)
        self.clock.value += 3
        self.death()
        second = self.begin()
        second_death = self.death()
        self.assertEqual([d["id"] for d in self.load(first)[0]], [first_death["id"]])
        self.assertEqual([d["id"] for d in self.load(second)[0]], [second_death["id"]])
        self.assertIsNone(self.sessions.record_death(second_death))
        self.assertEqual(second["recap_count"], 1)

    def test_late_wipe_and_duplicate_endings_do_not_extend_attribution(self):
        pull = self.begin()
        self.combat(False)
        self.clock.value += 0.2
        self.line(["33", "ts", "0", "4000000F"])
        first = self.death()
        self.clock.value += 0.2
        self.line(["33", "ts", "0", "4000000F"])
        self.death()
        recaps, _ = self.load(pull)
        self.assertEqual([d["id"] for d in recaps], [first["id"]])
        self.assertEqual(pull["ending"], "wipe")
        self.assertEqual(pull["deaths"], 1)

    def test_feed_loss_and_mid_pull_session_start_do_not_attach_tail(self):
        first = self.begin()
        self.death()
        self.meter.feed_lost()
        self.sessions.feed_lost()
        self.recap.reset()
        self.combat(True)
        self.death()
        self.combat(False)
        self.assertEqual(len(self.load(first)[0]), 1)
        self.sessions.end()
        self.combat(True)
        self.session = self.sessions.start("Late start", 1, "Duty", True)
        self.death()
        self.combat(False)
        self.assertEqual(self.session["pulls"], [])
        second = self.begin()
        self.death()
        self.assertEqual(len(self.load(second)[0]), 1)

    def test_session_end_stops_saving_without_stopping_live_recap(self):
        pull = self.begin()
        self.death()
        self.sessions.end(self.meter.full_snapshot())
        self.death()
        self.assertEqual(len(self.load(pull)[0]), 1)
        self.assertEqual(len(self.recap.deaths), 2)
        self.assertIsNotNone(self.meter.current)

    def test_each_death_is_durable_before_pull_finishes(self):
        pull = self.begin()
        self.line(["26", "ts", "ABC", "Vulnerability", "30", "40001234", "Boss", PLAYER, "Player"])
        death = self.death()
        loaded = ProgSessions(self.directory)
        saved = self.load(pull, loaded)[0][0]
        self.assertEqual(saved["id"], death["id"])
        self.assertEqual(saved["statuses"], [{"name": "Vulnerability", "source": "Boss"}])
        self.assertEqual(loaded.sessions[0]["state"], "interrupted")

    def test_failure_keeps_earlier_recaps_and_retries_across_pulls(self):
        first = self.begin()
        self.death()
        existing = next(self.directory.glob("recaps/*/*/*.json"))
        before = existing.read_bytes()
        with patch("nyaatriggers.recap_store.write_record", side_effect=OSError("Disk unavailable")):
            self.death()
            self.combat(False)
            second = self.begin()
            self.death()
        self.assertEqual(existing.read_bytes(), before)
        self.assertEqual(len(self.load(first)[0]), 2)
        self.assertEqual(len(self.sessions.recaps.unsaved), 2)
        self.assertIn("Disk unavailable", self.sessions.recaps.save_error)
        self.sessions.flush_pending()
        self.assertEqual(self.sessions.recaps.unsaved, {})
        self.assertEqual(self.sessions.recaps.save_error, "")
        restarted = ProgSessions(self.directory)
        self.assertEqual(len(self.load(first, restarted)[0]), 2)
        self.assertEqual(len(self.load(second, restarted)[0]), 1)

    def test_failed_session_write_does_not_hide_durable_recap(self):
        pull = self.begin()
        with patch("nyaatriggers.prog_session.write_record", side_effect=OSError("Session write failed")):
            self.death()
        restarted = ProgSessions(self.directory)
        self.assertEqual(len(self.load(pull, restarted)[0]), 1)
        self.assertEqual(restarted.sessions[0]["pulls"][0]["recap_count"], 0)

    def test_corrupt_and_misplaced_recaps_leave_valid_history_readable(self):
        pull = self.begin()
        self.death()
        directory = self.directory / "recaps" / self.session["id"] / pull["id"]
        bad_path = directory / (str(uuid4()) + ".json")
        bad_path.write_text('{"broken":')
        wrong = deepcopy(self.load(pull)[0][0])
        wrong.update(id=str(uuid4()), pull_id=str(uuid4()))
        write_record(directory, wrong)
        recaps, errors = self.load(pull)
        self.assertEqual(len(recaps), 1)
        self.assertEqual(len(errors), 2)
        self.assertEqual(bad_path.read_text(), '{"broken":')

    def test_malformed_observations_are_rejected_before_rendering(self):
        pull = self.begin()
        self.death()
        valid = self.load(pull)[0][0]
        for key, value in (("when", float("nan")), ("when", 10 ** 1000),
                           ("actor", True), ("pull_id", "../outside"),
                           ("events", [None]), ("statuses", [None])):
            with self.subTest(key=key, value=str(value)[:30]):
                data = deepcopy(valid)
                data[key] = value
                with self.assertRaises(ValueError):
                    validate_recap(data)
        for key, value in (("kind", []), ("kind", "fake"), ("time", float("inf")),
                           ("amount", "1,000"), ("source", None)):
            with self.subTest(event_key=key):
                data = deepcopy(valid)
                data["events"][0][key] = value
                with self.assertRaises(ValueError):
                    validate_recap(data)

    def test_missing_observation_fields_are_rejected(self):
        pull = self.begin()
        self.death()
        valid = self.load(pull)[0][0]
        for key in ("time", "kind", "name", "source", "amount"):
            with self.subTest(key=key):
                data = deepcopy(valid)
                del data["events"][0][key]
                with self.assertRaises(ValueError):
                    validate_recap(data)

    def test_long_zone_text_still_saves_and_updates_live_history(self):
        pull = self.begin()
        self.recap.zone = "Duty " * 100
        self.death()
        self.assertEqual(len(self.load(pull)[0]), 1)

    def test_persistent_disk_failure_bounds_retry_memory_and_reports_loss(self):
        pull = self.begin()
        with patch("nyaatriggers.recap_store.write_record", side_effect=OSError("Disk full")):
            for _ in range(MAX_PENDING_RECAPS + 3):
                self.death()
        self.assertEqual(len(self.sessions.recaps.unsaved), MAX_PENDING_RECAPS)
        self.assertEqual(len(self.sessions.recaps.errors), MAX_PENDING_RECAPS)
        self.assertEqual(self.sessions.recaps.dropped, 3)
        self.sessions.flush_pending()
        self.assertEqual(len(self.load(pull, ProgSessions(self.directory))[0]), MAX_PENDING_RECAPS)
        self.assertEqual(pull["recap_count"], MAX_PENDING_RECAPS + 3)
        self.assertEqual(self.sessions.recaps.dropped, 3)

    def test_failed_atomic_replace_leaves_no_partial_recap_and_can_retry(self):
        pull = self.begin()
        original = os.replace

        def fail_recap(source, destination):
            if "recaps" in Path(destination).parts:
                raise OSError("Rename failed")
            return original(source, destination)

        with patch("nyaatriggers.record_store.os.replace", side_effect=fail_recap):
            self.death()
        self.assertEqual(list(self.directory.glob("recaps/*/*/*.json")), [])
        self.assertEqual(list(self.directory.glob("recaps/*/*/*.tmp")), [])
        self.assertEqual(len(self.load(pull)[0]), 1)
        self.sessions.flush_pending()
        self.assertEqual(len(self.load(pull, ProgSessions(self.directory))[0]), 1)

    def test_unreadable_recap_directory_reports_an_error(self):
        pull = self.begin()
        self.death()
        with patch("nyaatriggers.recap_store.Path.iterdir", side_effect=PermissionError("Access denied")):
            recaps, errors = self.load(pull)
        self.assertEqual(recaps, [])
        self.assertEqual(errors, ["Access denied"])

    def test_death_without_damage_keeps_a_reviewable_attempt(self):
        self.combat(True)
        pull = self.session["pulls"][-1]
        self.death()
        self.combat(False)
        self.assertEqual(self.session["pulls"], [pull])
        self.assertEqual(len(self.load(pull)[0]), 1)
        self.assertFalse(pull["complete"])

    def test_late_death_after_empty_wipe_is_saved(self):
        self.combat(True)
        self.line(ability(pairs=[("33", "0")]))
        pull = self.session["pulls"][-1]
        self.line(["33", "ts", "0", "4000000F"])
        self.assertEqual(self.session["pulls"], [])
        self.death()
        self.assertEqual(self.session["pulls"], [pull])
        self.assertFalse(pull["complete"])
        self.assertEqual(pull["recap_count"], 1)
        self.assertEqual(len(self.load(pull, ProgSessions(self.directory))[0]), 1)

    def test_duplicate_empty_ending_does_not_extend_late_death_window(self):
        self.combat(True)
        pull = self.session["pulls"][-1]
        snapshot = deepcopy(self.meter.full_snapshot())
        snapshot["Encounter"].update(end_reason="empty", boundary_reason="combat-ended")
        self.combat(False)
        self.clock.value += 1.5
        self.sessions.pull_finished(snapshot)
        self.death()
        self.assertEqual(self.session["pulls"], [])
        self.assertEqual(self.load(pull), ([], []))

    def test_new_pull_closes_the_empty_pull_grace_period(self):
        self.combat(True)
        first = self.session["pulls"][-1]
        self.combat(False)
        second = self.begin()
        self.death()
        self.assertEqual(self.session["pulls"], [second])
        self.assertEqual(self.load(first), ([], []))
        self.assertEqual(len(self.load(second)[0]), 1)

    def test_interruptions_close_the_empty_pull_grace_period(self):
        for reason in ("feed-lost", "duty-left", "session-ended"):
            with self.subTest(reason=reason):
                self.sessions.end()
                self.session = self.sessions.start("Next session", 1, "Duty", False)
                self.combat(True)
                pull = self.session["pulls"][-1]
                self.combat(False)
                if reason == "feed-lost":
                    self.sessions.feed_lost()
                else:
                    self.sessions.end(reason=reason)
                self.death()
                self.assertEqual(self.session["pulls"], [])
                self.assertEqual(self.load(pull), ([], []))

    def test_old_sessions_load_without_a_recap_count(self):
        pull = self.begin()
        self.combat(False)
        del pull["recap_count"]
        self.sessions.save(self.session)
        loaded = ProgSessions(self.directory)
        self.assertEqual(len(loaded.sessions), 1)
        self.assertEqual(self.load(pull, loaded), ([], []))

    def test_pending_recaps_follow_their_original_session_after_a_switch(self):
        first_session = self.session
        first = self.begin()
        with patch("nyaatriggers.recap_store.write_record", side_effect=OSError("Disk unavailable")):
            first_death = self.death()
            self.combat(False)
            self.sessions.end()
            self.session = self.sessions.start("Next night", 1, "Duty", False)
            second = self.begin()
            second_death = self.death()
        self.sessions.flush_pending()
        saved_first, _ = self.sessions.recaps.load(first_session["id"], first["id"])
        saved_second, _ = self.load(second)
        self.assertEqual([d["id"] for d in saved_first], [first_death["id"]])
        self.assertEqual([d["id"] for d in saved_second], [second_death["id"]])

    def test_full_pull_identity_survives_the_meter_display_reset(self):
        pull = self.begin()
        first = self.death()
        self.clock.value += 150
        self.line(ability())
        second = self.death()
        self.assertLess(self.meter.snapshot()["Encounter"]["DURATION"], 2)
        self.assertEqual({d["id"] for d in self.load(pull)[0]}, {first["id"], second["id"]})
        self.assertEqual(len(self.session["pulls"]), 1)


if __name__ == "__main__":
    unittest.main()
