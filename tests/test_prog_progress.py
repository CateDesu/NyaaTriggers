from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from nyaatriggers.prog_phases import PhaseDefinition, PhaseRule, TransitionRule, UMAD
from nyaatriggers.prog_session import ProgSessions


class Clock:
    value = 1000
    date = 1791500000

    def __call__(self):
        return self.value

    def wall(self):
        return self.date


def spawn(actor="40000001", npc=111, hp=1000, maximum=1000, owner="0000"):
    return ["03", "ts", actor, "Localized actor", "00", "64", owner,
            "00", "", "7131", str(npc), str(hp), str(maximum)]


def health(hp, maximum=1000, actor="40000001", kind="38"):
    fields = [kind, "ts", actor, "Localized actor"]
    if kind != "39":
        fields.append("0")
    return fields + [str(hp), str(maximum)]


class ProgressTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.clock = Clock()
        self.definition = PhaseDefinition(
            "progress.fixture", 1, 123, ("p1", "p2", "p3"),
            tuple(PhaseRule(f"entry-{index}", "20", 0xA000 + index, phase)
                  for index, phase in enumerate(("p1", "p2", "p3"))),
            verified=True, continuous_combat=True,
            bosses=(("p1", (111,)), ("p2", (222, 223))))
        self.model = ProgSessions(self.directory, self.clock, self.clock.wall,
                                  definitions=(self.definition,))

    def begin(self):
        session = self.model.start("Progress", 123, "Duty", False)
        encounter = {"pull_id": str(uuid4()), "wall_start": self.clock.date,
                     "monotonic_start": self.clock.value, "last_activity": self.clock.value,
                     "DURATION": 0, "deaths": 0}
        self.model.pull_started({"Encounter": encounter})
        self.entry(0)
        return session, session["pulls"][-1]

    def entry(self, index):
        self.model.process_event(["20", "ts", "40000001", "Localized actor",
                                  f"{0xA000 + index:X}", "Localized action"], [])

    def sample(self, fields):
        self.model.process_event(fields, [])

    def test_new_dates_use_actual_wall_time_and_survive_reload(self):
        session, pull = self.begin()
        self.clock.value += 10
        self.clock.date += 70
        self.entry(1)
        rows = self.model.phase_progress(123)
        self.assertEqual([row.first_reached for row in rows], [1791500000, 1791500070, None])
        self.assertEqual(pull["phase_tracking"]["observations"][-1]["at"], 10)
        self.model.end()
        restored = ProgSessions(self.directory, self.clock, definitions=(self.definition,))
        self.assertEqual(restored.phase_progress(123), rows)

    def test_legacy_dates_require_a_sound_continuous_clock(self):
        session, pull = self.begin()
        self.clock.value += 8
        self.entry(1)
        for observation in pull["phase_tracking"]["observations"]:
            observation.pop("observed_at")
        self.assertEqual([r.first_reached for r in self.model.phase_progress(123)],
                         [self.clock.date, self.clock.date + 8, None])
        segmented = replace(self.definition, continuous_combat=False,
                            transitions=(TransitionRule("gap", "20", 0xB000, "p1", "p2", 30),))
        self.model.definitions = (segmented,)
        self.model.invalidate_phase_progress()
        self.assertTrue(all(r.first_reached is None for r in self.model.phase_progress(123)))
        pull["phase_tracking"]["observations"][1]["observed_at"] = self.clock.date + 88
        self.model.invalidate_phase_progress()
        self.assertEqual(self.model.phase_progress(123)[1].first_reached, self.clock.date + 88)

    def test_later_confirmation_does_not_invent_earlier_dates(self):
        self.begin()
        data = self.model.attempt.data
        data["observations"].clear()
        self.entry(2)
        self.assertEqual([r.first_reached for r in self.model.phase_progress(123)],
                         [None, None, self.clock.date])

    def test_verified_boss_health_is_separate_for_each_phase(self):
        self.sample(spawn())
        session, pull = self.begin()
        self.sample(health(420))
        self.sample(health(600))
        self.sample(spawn("40000002", 222))
        self.sample(spawn("40000003", 223))
        self.entry(1)
        self.sample(health(900, actor="40000002"))
        self.sample(health(700, actor="40000003"))
        self.sample(health(1))
        self.assertEqual([r.best_hp_percent for r in self.model.phase_progress(123)], [42, 70, None])
        self.sample(health(300, 500, "40000002"))
        self.assertEqual(self.model.phase_progress(123)[1].best_hp_percent, 60)
        self.model.end()
        restored = ProgSessions(self.directory, definitions=(self.definition,))
        self.assertEqual([r.best_hp_percent for r in restored.phase_progress(123)], [42, 60, None])

    def test_players_pets_adds_and_spawn_placeholders_cannot_set_health(self):
        self.sample(spawn(hp=0))
        self.begin()
        for actor, npc, owner in (("10000001", 111, "0000"),
                                  ("40000002", 111, "10000001"),
                                  ("40000003", 999, "0000")):
            self.sample(spawn(actor, npc, owner=owner))
            self.sample(health(1, actor=actor))
        self.assertIsNone(self.model.phase_progress(123)[0].best_hp_percent)
        self.sample(health(750))
        self.assertEqual(self.model.phase_progress(123)[0].best_hp_percent, 75)
        self.sample(health(0))
        self.assertEqual(self.model.phase_progress(123)[0].best_hp_percent, 0)

    def test_invalid_health_is_ignored_and_missing_maximum_uses_known_metadata(self):
        self.sample(spawn())
        self.begin()
        for current, maximum in (("", "1000"), ("NaN", "1000"), ("-1", "1000"),
                                 ("1001", "1000"), ("500", "0"), ("500", "bad")):
            self.sample(health(current, maximum))
        self.assertIsNone(self.model.phase_progress(123)[0].best_hp_percent)
        self.sample(health(400, "", kind="37"))
        self.assertEqual(self.model.phase_progress(123)[0].best_hp_percent, 40)

    def test_damage_target_and_source_health_offsets(self):
        self.sample(spawn())
        self.begin()
        fields = [""] * 36
        fields[:8] = ["21", "ts", "10000001", "Player", "1", "Attack", "40000001", "Boss"]
        fields[24:26] = ["650", "1000"]
        self.sample(fields)
        self.assertEqual(self.model.phase_progress(123)[0].best_hp_percent, 65)
        fields[2], fields[6] = fields[6], fields[2]
        fields[34:36] = ["350", "1000"]
        self.sample(fields)
        self.assertEqual(self.model.phase_progress(123)[0].best_hp_percent, 35)

    def test_health_is_saved_by_the_existing_checkpoint_without_ending_the_pull(self):
        self.sample(spawn())
        self.begin()
        self.sample(health(250))
        self.clock.value += 15
        self.model.checkpoint()
        restored = ProgSessions(self.directory, definitions=(self.definition,))
        self.assertEqual(restored.phase_progress(123)[0].best_hp_percent, 25)
        self.assertEqual(restored.sessions[0]["pulls"][0]["ending"], "program-closed")

    def test_actor_removal_reuse_feed_loss_and_zone_change_retire_metadata(self):
        for boundary in (["04", "ts", "40000001"], spawn(npc=999), ["01", "ts", "456", "Other"]):
            with self.subTest(boundary=boundary):
                self.model._boss_actors.clear()
                self.sample(spawn())
                if self.model.current is None:
                    self.begin()
                self.sample(boundary)
                self.sample(health(1))
                self.assertIsNone(self.model.phase_progress(123)[0].best_hp_percent)
        self.sample(spawn())
        self.model.feed_lost()
        self.assertFalse(self.model._boss_actors)

    def test_cache_changes_only_with_progress_evidence_or_explicit_invalidation(self):
        self.sample(spawn())
        self.begin()
        rows = self.model.phase_progress(123)
        with patch("nyaatriggers.prog_session.phase_progress", side_effect=AssertionError("rescanned")):
            self.assertIs(self.model.phase_progress(123), rows)
            self.sample(health("bad"))
            self.assertIs(self.model.phase_progress(123), rows)
        self.sample(health(500))
        updated = self.model.phase_progress(123)
        self.assertIsNot(updated, rows)
        self.assertEqual(updated[0].best_hp_percent, 50)

    def test_aggregation_uses_remaining_matching_records_and_ignores_bad_optional_health(self):
        self.sample(spawn())
        first, pull = self.begin()
        self.sample(health(500))
        self.model.end(reason="feed-lost")
        first["archived"] = True
        self.clock.date += 100
        second, newer = self.begin()
        self.sample(health(300))
        self.model.end()
        unrelated = deepcopy(first)
        unrelated["zone_id"] = 456
        self.model.sessions.append(unrelated)
        self.model.invalidate_phase_progress()
        self.assertEqual(self.model.phase_progress(123)[0].first_reached, pull["started"])
        self.assertEqual(self.model.phase_progress(123)[0].best_hp_percent, 30)
        newer["phase_tracking"]["boss_hp"]["p1"]["current"] = -1
        self.model.invalidate_phase_progress(123)
        self.assertEqual(self.model.phase_progress(123)[0].best_hp_percent, 50)
        self.model.delete(first)
        self.assertEqual(self.model.phase_progress(123)[0].first_reached, newer["started"])
        self.assertIsNone(self.model.phase_progress(123)[0].best_hp_percent)
        self.assertEqual(self.model.phase_progress(999), ())

    def test_umad_actual_target_bosses_are_phase_specific(self):
        self.assertEqual(dict(UMAD.bosses), {"p1": (19504,), "p2": (19506,),
                                            "p3": (19508, 19509), "p4": (18475,)})

    def test_recorded_umad_health_fields_and_both_p3_targets(self):
        self.model = ProgSessions(self.directory, self.clock, self.clock.wall)
        self.sample(spawn(npc=19504, hp=56331828, maximum=56331828))
        session = self.model.start("UMAD", UMAD.zone_id, "Dancing Mad", False)
        encounter = {"pull_id": str(uuid4()), "wall_start": self.clock.date,
                     "monotonic_start": self.clock.value, "last_activity": self.clock.value,
                     "DURATION": 0, "deaths": 0}
        self.model.pull_started({"Encounter": encounter})
        self.sample(["20", "ts", "40000001", "Kefka", "C403", "Revolting Ruin III"])
        self.sample(health(44941877, "", kind="37"))
        self.assertAlmostEqual(self.model.phase_progress(UMAD.zone_id)[0].best_hp_percent,
                               100 * 44941877 / 56331828)
        self.sample(spawn("40000002", 19508))
        self.sample(spawn("40000003", 19509))
        self.sample(["20", "ts", "40000001", "Kefka", "C3F7", "Aero III Assault"])
        self.sample(health(1))
        self.sample(health(650, actor="40000002"))
        self.sample(health(550, actor="40000003"))
        self.assertEqual(self.model.phase_progress(UMAD.zone_id)[2].best_hp_percent, 55)
        self.assertEqual(session["pulls"][0]["phase_tracking"]["boss_hp"]["p3"]["npc_id"], 19509)

    def test_p5_requires_damage_to_the_exact_confirmed_kefka_actor(self):
        self.definition = replace(self.definition, caster_bosses=(("p3", 7131),))
        self.model = ProgSessions(self.directory, self.clock, self.clock.wall,
                                  definitions=(self.definition,))
        self.sample(spawn(npc=777))
        self.sample(spawn("40000002", 777))
        session, pull = self.begin()
        self.entry(2)
        fields = [""] * 36
        fields[:8] = ["21", "ts", "10000001", "Player", "1", "Attack", "40000001", "Kefka"]
        fields[24:26] = ["600", "1000"]
        for flags, amount, source, target in (("4", "10000", "10000001", "40000001"),
                                              ("1", "10000", "10000001", "40000001"),
                                              ("3", "10000", "40000003", "40000001"),
                                              ("3", "10000", "10000001", "40000002"),
                                              ("3", "100", "10000001", "40000001")):
            fields[2], fields[6] = source, target
            fields[8:10] = [flags, amount]
            self.sample(fields)
            self.sample(health(1))
            self.sample(health(0, actor="40000002"))
            self.assertIsNone(self.model.phase_progress(123)[2].best_hp_percent)
        fields[2], fields[6] = "10000001", "40000001"
        fields[8:10] = ["3", "10000"]
        self.sample(fields)
        self.assertEqual(self.model.phase_progress(123)[2].best_hp_percent, 60)
        self.sample(health(400))
        self.assertEqual(self.model.phase_progress(123)[2].best_hp_percent, 40)
        self.model.end()
        restored = ProgSessions(self.directory, definitions=(self.definition,))
        self.assertEqual(restored.phase_progress(123)[2].best_hp_percent, 40)
        for key, value in (("actor", 0x40000002), ("npc_name_id", 999), ("target_confirmed", False)):
            with self.subTest(key=key):
                saved = deepcopy(pull["phase_tracking"]["boss_hp"]["p3"])
                pull["phase_tracking"]["boss_hp"]["p3"][key] = value
                self.model.invalidate_phase_progress()
                self.assertIsNone(self.model.phase_progress(123)[2].best_hp_percent)
                pull["phase_tracking"]["boss_hp"]["p3"] = saved

    def test_duty_end_and_new_pull_clear_caster_confirmation(self):
        self.sample(spawn())
        self.model._attacked_bosses.add(0x40000001)
        self.begin()
        self.assertFalse(self.model._attacked_bosses)
        self.model._attacked_bosses.add(0x40000001)
        self.model.end(reason="duty-left")
        self.assertFalse(self.model._boss_actors)
        self.assertFalse(self.model._attacked_bosses)
        self.sample(spawn())
        self.model.end(reason="duty-left")
        self.assertFalse(self.model._boss_actors)

    def test_umad_bb40_wire_actor_confirmation_and_saved_provenance(self):
        self.model = ProgSessions(self.directory, self.clock, self.clock.wall)
        self.sample(spawn("40000005", 777))
        self.sample(spawn("40000006", 777))
        self.model.start("UMAD", UMAD.zone_id, "Dancing Mad", False)
        encounter = {"pull_id": str(uuid4()), "wall_start": self.clock.date,
                     "monotonic_start": self.clock.value, "last_activity": self.clock.value,
                     "DURATION": 0, "deaths": 0}
        self.model.pull_started({"Encounter": encounter})
        self.sample(["20", "ts", "40000005", "Kefka", "BB40", "Ultima Repeater"])
        fields = [""] * 36
        fields[:10] = ["21", "ts", "10000001", "Player", "1", "Attack",
                       "40000006", "Kefka", "3", "10000"]
        fields[24:26] = ["500", "1000"]
        self.sample(fields)
        self.sample(health(1, actor="40000006"))
        self.sample(health(1, actor="40000005"))
        self.assertIsNone(self.model.phase_progress(UMAD.zone_id)[4].best_hp_percent)
        fields[6] = "40000005"
        self.sample(fields)
        self.assertEqual(self.model.phase_progress(UMAD.zone_id)[4].best_hp_percent, 50)
        self.model.end()
        restored = ProgSessions(self.directory)
        self.assertEqual(restored.phase_progress(UMAD.zone_id)[4].best_hp_percent, 50)


if __name__ == "__main__":
    unittest.main()
