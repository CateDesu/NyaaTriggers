"""Offline log replay, pull boundaries and temporary recap storage."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from nyaatriggers.recap_log import ImportedRecaps, MAX_LINE_BYTES, read_log
from tests.test_death_recap_details import health, hit
from tests.test_session_features import PLAYER, BOSS


def line(fields, seconds):
    fields = list(fields)
    fields[1] = (datetime(2026, 9, 27, tzinfo=timezone.utc) + timedelta(seconds=seconds)).isoformat()
    return "|".join(fields) + "\n"


class LogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "Network.log"

    def load(self, text):
        self.path.write_text(text, encoding="utf-8")
        result = read_log(self.path)
        self.addCleanup(result.close)
        return result

    def test_log_dates_health_shields_icons_and_all_deaths_are_preserved(self):
        text = line(["01", "", "1", "Duty"], 0)
        for index in range(85):
            start = index * 10
            text += line(health(statuses=[(71, 20, BOSS, 0)]), start)
            text += line(hit(), start + 1)
            text += line(["25", "", PLAYER, "Player"], start + 3)
        result = self.load(text)
        self.assertEqual(len(result.records), 85)
        self.assertNotIn("events", result.records[0])
        death = result.read(result.records[0])
        self.assertEqual(death["when"], datetime(2026, 9, 27, tzinfo=timezone.utc).timestamp() + 843)
        event = death["events"][-1]
        self.assertEqual(event["time"], -2)
        self.assertEqual((event["hp"], event["shield"]), (8000, 20))
        self.assertEqual(event["statuses"][0]["id"], 71)
        self.assertEqual(death["zone"], "Duty")
        self.assertIn("71", result.statuses)

    def test_wipe_late_death_and_next_pull_have_separate_history(self):
        text = line(["01", "", "1", "Duty"], 0)
        text += line(hit(), 1)
        text += line(["33", "", "0", "4000000F"], 2)
        text += line(["25", "", PLAYER, "Player"], 2.5)
        text += line(hit(healing=True), 5)
        text += line(hit(amount=2000), 10)
        text += line(["25", "", PLAYER, "Player"], 12)
        result = self.load(text)
        self.assertEqual([death["pull"] for death in result.records], [2, 1])
        latest = result.read(result.records[0])
        self.assertEqual([event["amount"] for event in latest["events"]], [1000, 2000])
        older = result.read(result.records[1])
        self.assertEqual([event["amount"] for event in older["events"]], [1000])

    def test_imported_attacker_mitigation_is_indexed_and_removed_with_combatant(self):
        text = line(["26", "", "4A9", "Reprisal", "15", PLAYER, "Tank", BOSS, "Boss", "0"], 0)
        text += line(hit(), 1) + line(["04", "", BOSS, "Boss"], 2) + line(hit(), 3)
        text += line(["25", "", PLAYER, "Player"], 4)
        result = self.load(text)
        first, second = result.read(result.records[0])["events"]
        self.assertEqual([s["id"] for s in first["source_statuses"]], [1193])
        self.assertEqual(second["source_statuses"], [])
        self.assertEqual(result.statuses["1193"]["name"], "Reprisal")

    def test_recorded_combat_transitions_separate_pulls_without_a_wipe(self):
        text = line(["260", "", "1", "1", "1", "1"], 0)
        text += line(hit(), 1)
        text += line(["25", "", "10FF0002", "Other player"], 2)
        text += line(["260", "", "0", "0", "1", "1"], 3)
        text += line(["260", "", "1", "1", "1", "1"], 10)
        text += line(hit(amount=2000), 11)
        text += line(["25", "", PLAYER, "Player"], 12)
        result = self.load(text)
        self.assertEqual([death["pull"] for death in result.records], [2, 1])
        latest = result.read(result.records[0])
        self.assertEqual([event["amount"] for event in latest["events"]], [2000])

    def test_invalid_combat_transition_does_not_clear_a_pull(self):
        text = line(["260", "", "1", "1", "1", "1"], 0)
        text += line(hit(), 1)
        text += line(["260", "", "0", "invalid", "1", "1"], 2)
        text += line(hit(amount=2000), 3)
        text += line(["25", "", PLAYER, "Player"], 4)
        result = self.load(text)
        self.assertEqual([event["amount"] for event in result.read(result.records[0])["events"]], [1000, 2000])
        self.assertEqual(result.pulls, 1)
        self.assertEqual(result.skipped, 1)

    def test_partial_log_combat_exit_still_separates_the_next_pull(self):
        text = line(hit(), 1)
        text += line(["260", "", "0", "0", "1", "1"], 3)
        text += line(hit(amount=2000), 10)
        text += line(["25", "", PLAYER, "Player"], 12)
        result = self.load(text)
        self.assertEqual(result.pulls, 2)
        self.assertEqual([event["amount"] for event in result.read(result.records[0])["events"]], [2000])

    def test_older_ability_checksum_is_not_treated_as_a_pet_owner(self):
        outgoing = hit() + ["0", "1", "f67be79f31081dd8"]
        outgoing[2], outgoing[6] = PLAYER, BOSS
        incoming = hit() + ["0", "1", "235a167d9113e2db"]
        result = self.load(line(outgoing, 1) + line(incoming, 2) +
                           line(["25", "", PLAYER, "Player"], 3))
        self.assertEqual(result.pulls, 1)
        self.assertEqual(result.records[0]["pull"], 1)

    def test_current_ability_owner_fields_still_start_pet_only_pulls(self):
        pet = hit() + ["0", "1", PLAYER, "Pet owner", "checksum"]
        pet[2], pet[6] = "40000002", BOSS
        result = self.load(line(pet, 1) + line(["25", "", PLAYER, "Player"], 3))
        self.assertEqual(result.pulls, 1)
        self.assertEqual(result.records[0]["pull"], 1)

    def test_duplicate_death_and_backward_clock_do_not_reuse_future_events(self):
        result = self.load(line(hit(), 100) + line(["25", "", PLAYER, "Player"], 101) +
                           line(["25", "", PLAYER, "Player"], 101.1) + line(hit(), 110) +
                           line(["25", "", PLAYER, "Player"], 50))
        self.assertEqual(len(result.records), 2)
        self.assertEqual(result.read(result.records[-1])["events"], [])

    def test_oversized_invalid_and_unrelated_lines_do_not_hide_valid_deaths(self):
        text = "\ufeff" + line(["01", "", "1", "Duty"], 0)
        text += "x" * (MAX_LINE_BYTES * 2) + "\n"
        text += "25|not-a-date|10000001|Player\n00|chat\n"
        text += line(["25", "", PLAYER, "Player"], 1)
        result = self.load(text)
        self.assertEqual(len(result.records), 1)
        self.assertEqual(result.skipped, 2)
        self.assertEqual(result.records[0]["zone"], "Duty")

    def test_cancel_and_failure_close_temporary_storage(self):
        self.path.write_text(line(hit(), 1))
        cancel = threading.Event()
        cancel.set()
        store = ImportedRecaps(self.path)
        with patch("nyaatriggers.recap_log.ImportedRecaps", return_value=store):
            self.assertIsNone(read_log(self.path, cancel))
        self.assertTrue(store._file.closed)
        store = ImportedRecaps(self.path)
        with patch("nyaatriggers.recap_log.ImportedRecaps", return_value=store), self.assertRaises(OSError):
            read_log(self.path.parent / "missing.log")
        self.assertTrue(store._file.closed)

    def test_small_timestamp_jitter_cannot_put_events_after_death(self):
        result = self.load(line(hit(), 10) + line(["25", "", PLAYER, "Player"], 9.8))
        death = result.read(result.records[0])
        self.assertEqual(death["events"][0]["time"], 0)
        self.assertAlmostEqual(death["when"], datetime(2026, 9, 27, tzinfo=timezone.utc).timestamp() + 9.8)

    def test_incomplete_tail_is_not_imported_as_a_death(self):
        result = self.load(line(["25", "", PLAYER, "Player"], 1).rstrip("\n"))
        self.assertEqual(result.records, [])
        self.assertEqual(result.skipped, 1)

    def test_growing_log_finishes_at_its_original_extent(self):
        self.path.write_text("00|chat\n" * 2048)
        progress = []
        def extend(percent):
            progress.append(percent)
            with self.path.open("a") as stream:
                stream.write(line(["25", "", PLAYER, "Player"], 1))
        result = read_log(self.path, progress=extend)
        self.addCleanup(result.close)
        self.assertEqual(result.records, [])
        self.assertEqual(progress[-1], 100)


if __name__ == "__main__":
    unittest.main()
