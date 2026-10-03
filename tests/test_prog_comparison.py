from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from nyaatriggers.prog_phases import UMAD, new_tracking
from nyaatriggers.prog_session import ProgSessions, compare_sessions, phase_summary
from nyaatriggers.record_store import read_record, write_record
from tests import test_prog_phases as phase_tests


def pull(phase=None, ending="wipe", definition=UMAD, tracked=True):
    data = {"id": str(uuid4()), "started": 1, "duration": 10,
            "ending": ending, "complete": ending in ("wipe", "combat-ended"),
            "deaths": 0, "bookmark": False, "note": "", "recap_count": 0}
    if tracked:
        tracking = new_tracking(definition)
        tracking["coverage"] = "complete" if data["complete"] else "recording" if ending == "active" else "interrupted"
        tracking["reason"] = "" if data["complete"] or ending == "active" else ending
        if phase is not None:
            rule = next(r for r in definition.rules if r.phase == phase)
            tracking["observations"].append({"phase": phase, "at": 5, "rule": rule.ident})
        data["phase_tracking"] = tracking
    return data


def session(pulls, zone_id=UMAD.zone_id, started=1, state="ended"):
    return {"version": 1, "id": str(uuid4()), "name": "Raid night", "zone": "Duty",
            "zone_id": zone_id, "started": started, "elapsed": 20,
            "state": state, "pulls": pulls}


class ComparisonTests(unittest.TestCase):
    def test_phase_rates_include_quick_wipes_and_later_confirmations(self):
        selected = session([pull("p3") for _ in range(6)] + [pull() for _ in range(14)] +
                           [pull("p4", "feed-lost") for _ in range(3)])
        previous = session([pull("p3") for _ in range(3)] + [pull() for _ in range(12)])
        original = deepcopy((selected, previous))
        result = compare_sessions(selected, previous)
        self.assertEqual(result["phase_status"], "available")
        row = result["phases"][2]
        self.assertEqual((row["phase"], row["selected"], row["compared"]), ("p3", 6, 3))
        self.assertEqual((result["selected"]["eligible"], result["compared"]["eligible"]), (20, 15))
        self.assertAlmostEqual(row["change"], 10)
        self.assertEqual(result["selected"]["counts"], (6, 6, 6, 0, 0))
        self.assertEqual(result["selected"]["furthest"], "p3")
        self.assertEqual(result["selected"]["excluded"], {"interrupted": 3})
        self.assertTrue(result["durations_comparable"])
        self.assertEqual((selected, previous), original)

    def test_zero_eligible_pulls_has_no_rate_or_change(self):
        selected = session([pull("p4", "feed-lost"), pull("p5", "active")], state="active")
        result = compare_sessions(selected, session([pull("p3")]))
        self.assertEqual(result["selected"]["eligible"], 0)
        self.assertIsNone(result["selected"]["furthest"])
        self.assertTrue(all(row["change"] is None for row in result["phases"]))
        self.assertEqual(result["selected"]["excluded"], {"interrupted": 1, "active": 1})

    def test_exclusions_keep_unreadable_optional_data_and_ordinary_counts(self):
        malformed = pull("p4")
        malformed["phase_tracking"]["observations"][0]["at"] = float("nan")
        uncertain = pull("p4", "boundary-uncertain")
        uncertain["phase_tracking"]["coverage"] = "uncertain"
        old = pull(tracked=False)
        selected = session([pull("p3"), malformed, uncertain, old])
        result = compare_sessions(selected, session([pull("p2")]))
        self.assertEqual(result["selected"]["eligible"], 1)
        self.assertEqual(result["selected"]["excluded"],
                         {"unavailable": 1, "uncertain": 1, "not-recorded": 1})
        self.assertEqual(result["selected"]["counts"], (1, 1, 1, 0, 0))
        self.assertFalse(result["durations_comparable"])
        self.assertNotEqual(malformed["phase_tracking"], None)

    def test_different_revisions_do_not_compare_rates_or_durations(self):
        revision = replace(UMAD, revision=2)
        definitions = (UMAD, revision)
        selected = session([pull("p3")])
        previous = session([pull("p2", definition=revision)])
        result = compare_sessions(selected, previous, definitions)
        self.assertEqual(result["phase_status"], "tracking-differs")
        self.assertEqual(result["phases"], [])
        self.assertFalse(result["durations_comparable"])
        mixed = session([pull("p3"), pull("p2", definition=revision)])
        result = compare_sessions(mixed, selected, definitions)
        self.assertEqual(result["phase_status"], "tracking-differs")
        self.assertEqual(result["selected"]["status"], "tracking-differs")
        self.assertEqual(result["selected"]["excluded"], {"tracking-differs": 2})

    def test_unknown_revision_cannot_hide_among_current_pulls(self):
        unknown = pull("p5")
        unknown["phase_tracking"]["definition_revision"] = 999
        mixed = phase_summary(session([pull("p2"), unknown]))
        self.assertEqual(mixed["status"], "tracking-differs")
        self.assertEqual(mixed["eligible"], 0)
        self.assertEqual(mixed["excluded"], {"unavailable": 1, "tracking-differs": 1})

    def test_older_and_unsupported_sessions_keep_duration_comparisons(self):
        old = session([pull(tracked=False)])
        result = compare_sessions(old, session([pull(tracked=False)]))
        self.assertEqual(result["phase_status"], "not-recorded")
        self.assertTrue(result["durations_comparable"])
        unsupported = session([{**pull(tracked=False), "phase_tracking": None}], zone_id=1)
        previous = session([{**pull(tracked=False), "phase_tracking": None}], zone_id=1)
        result = compare_sessions(unsupported, previous)
        self.assertEqual(result["phase_status"], "not-supported")
        self.assertTrue(result["durations_comparable"])
        self.assertEqual(compare_sessions(old, unsupported)["phase_status"], "different-duty")
        self.assertFalse(compare_sessions(old, unsupported)["durations_comparable"])

    def test_bookmarks_notes_and_live_updates_do_not_change_eligibility(self):
        selected = session([pull("p3"), pull("p5", "active")], state="active")
        before = phase_summary(selected)
        selected["pulls"][0].update(bookmark=True, note="Review towers")
        self.assertEqual(phase_summary(selected), before)
        current = selected["pulls"][1]
        current.update(ending="wipe", complete=True)
        current["phase_tracking"].update(coverage="complete", reason="")
        after = phase_summary(selected)
        self.assertEqual((after["eligible"], after["furthest"]), (2, "p5"))
        self.assertEqual(after["counts"], (2, 2, 2, 1, 1))

    def test_saved_and_recovered_attempts_follow_the_same_denominator(self):
        selected = session([pull("p3"), pull("p5", "active")], state="active")
        with tempfile.TemporaryDirectory() as directory:
            write_record(Path(directory), selected)
            recovered = ProgSessions(directory).sessions[0]
            result = phase_summary(recovered)
        self.assertEqual(result["eligible"], 1)
        self.assertEqual(result["furthest"], "p3")
        self.assertEqual(result["excluded"], {"interrupted": 1})
        self.assertEqual(recovered["pulls"][1]["phase_tracking"]["observations"][0]["phase"], "p5")

    def test_recorded_umad_observations_have_the_expected_comparison_rates_after_restart(self):
        replay = phase_tests.UmadPhaseTests("test_recorded_umad_pulls_confirm_p4_without_false_p5")
        replay.setUp()
        self.addCleanup(lambda: self.assertTrue(replay.doCleanups()))
        replay.test_recorded_umad_pulls_confirm_p4_without_false_p5()
        recovered = ProgSessions(replay.temp.name)
        self.assertFalse(recovered.errors)
        recorded = recovered.sessions[0]
        previous = session(deepcopy(recorded["pulls"][:3]), started=0)
        result = compare_sessions(recorded, previous)
        self.assertEqual(result["selected"]["eligible"], 5)
        self.assertEqual(result["selected"]["counts"], (5, 2, 2, 2, 0))
        self.assertEqual(result["compared"]["eligible"], 3)
        self.assertEqual(result["compared"]["counts"], (3, 1, 1, 1, 0))
        self.assertAlmostEqual(result["phases"][3]["change"], 40 - 100 / 3)
        self.assertEqual(result["phases"][4]["change"], 0)
        self.assertTrue(result["durations_comparable"])

    def test_archiving_survives_restart_and_preserves_phase_data_and_recaps(self):
        record = session([pull("p3")])
        record["pulls"][0]["phase_tracking"]["version"] = 999
        record["pulls"][0]["phase_tracking"]["future"] = {"keep": [1, 2]}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_record(root, record)
            recap = root / "recaps" / record["id"] / record["pulls"][0]["id"] / "saved-recap.json"
            recap.parent.mkdir(parents=True)
            recap.write_text('{"future": "preserved"}')
            model = ProgSessions(root)
            saved = model.sessions[0]
            before = phase_summary(saved)
            self.assertTrue(model.set_archived(saved, True))
            self.assertEqual(phase_summary(saved), before)
            restored = ProgSessions(root)
            archived = restored.sessions[0]
            self.assertTrue(archived["archived"])
            self.assertEqual(archived["pulls"], record["pulls"])
            self.assertEqual(recap.read_text(), '{"future": "preserved"}')
            self.assertTrue(restored.set_archived(archived, False))
            self.assertFalse(ProgSessions(root).sessions[0]["archived"])

    def test_archiving_rejects_active_sessions_and_foreign_records(self):
        with tempfile.TemporaryDirectory() as directory:
            model = ProgSessions(directory)
            active = model.start("Active", UMAD.zone_id, "Duty", False)
            with self.assertRaises(ValueError):
                model.set_archived(active, True)
            model.end()
            with self.assertRaises(ValueError):
                model.set_archived(deepcopy(active), True)
            with self.assertRaises(ValueError):
                model.set_archived(active, 1)
            self.assertNotIn("archived", read_record(Path(directory) / (active["id"] + ".json")))

    def test_failed_archive_write_keeps_visibility_and_the_latest_notes(self):
        for original in (None, True):
            with self.subTest(original=original), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                record = session([pull("p3")])
                if original is not None:
                    record["archived"] = original
                write_record(root, record)
                model = ProgSessions(root)
                target = model.sessions[0]
                path = root / (target["id"] + ".json")
                backup = path.with_suffix(".saved")
                path.rename(backup)
                path.mkdir()
                target["pulls"][0]["note"] = "Latest note"
                self.assertFalse(model.set_archived(target, original is not True))
                self.assertEqual(target.get("archived"), original)
                self.assertEqual("archived" in target, original is not None)
                path.rmdir()
                backup.rename(path)
                model.flush_pending()
                saved = read_record(path)
                self.assertEqual(saved.get("archived"), original)
                self.assertEqual(saved["pulls"][0]["note"], "Latest note")
                self.assertFalse(model.save_error)


if __name__ == "__main__":
    unittest.main()
