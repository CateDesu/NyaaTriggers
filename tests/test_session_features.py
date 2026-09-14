"""Recorded deaths, session boundaries, local storage, and profile choices."""

from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

from nyaatriggers.death_recap import DeathRecap, MAX_DEATHS, MAX_ACTORS, MAX_EVENTS, MAX_STATUSES
from nyaatriggers.dps_meter import DpsMeter
from nyaatriggers.prog_session import CHECKPOINT_SECONDS, ProgSessions, summary
from nyaatriggers.record_store import read_record, write_record
from nyaatriggers.trigger_engine import Trigger
from nyaatriggers.trigger_profiles import SOURCES, apply_choices, capture_profile, preserve_default, validate_profile

PLAYER = "10FF0001"
BOSS = "40001234"


def ability(source=BOSS, target=PLAYER, pairs=None):
    fields = ["21", "ts", source, "Source", "A1", "Ability", target, "Target"]
    for flags, value in pairs or [("03", "3E80000")]:
        fields.extend((flags, value))
    return fields + ["0"] * (24 - len(fields))


def area_ability(source=BOSS, target=PLAYER, pairs=None, index=0, count=3):
    fields = ability(source, target, pairs)
    fields[0] = "22"
    fields.extend(["0"] * (45 - len(fields)))
    fields.extend([str(index), str(count), "00", ""])
    return fields


class Clock:
    value = 1000.0

    def __call__(self):
        return self.value


class RecapTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.recap = DeathRecap(self.clock)

    def death(self):
        self.recap.process(["25", "ts", PLAYER, "Player"])
        return self.recap.deaths[0]

    def status(self, kind="26", duration="30", source=BOSS):
        self.recap.process([kind, "ts", "ABC", "Vulnerability", duration, source, "Boss", PLAYER, "Player"])

    def test_history_window_and_frozen_statuses(self):
        self.recap.process(ability())
        self.clock.value += 20
        self.status()
        self.recap.process(ability(pairs=[("04", "1F40000")]))
        death = self.death()
        self.assertEqual([e["kind"] for e in death["events"]], ["gained", "heal"])
        self.assertEqual(death["events"][1]["amount"], 500)
        self.assertEqual(death["statuses"][0]["name"], "Vulnerability")
        self.status("30")
        self.assertEqual(len(death["statuses"]), 1)

    def test_refresh_loss_expiry_and_source_identity(self):
        self.status(duration="3")
        self.clock.value += 2
        self.status(duration="10")
        self.status(duration="1", source="40002222")
        self.clock.value += 2
        self.assertEqual(len(self.death()["statuses"]), 1)
        self.clock.value += 2
        self.status()
        self.status("30")
        self.assertEqual(self.death()["statuses"], [])

    def test_self_heal_reflection_and_instant_death(self):
        self.recap.process(ability(source=PLAYER, target=BOSS,
                                   pairs=[("03", "38FD0000"), ("104", "AA68000")]))
        self.recap.process(ability(source=PLAYER, target=BOSS,
                                   pairs=[("03", "10000"), ("1D", "50000"), ("03", "50000")]))
        self.recap.process(ability(pairs=[("33", "0")]))
        events = self.death()["events"]
        self.assertEqual([(e["kind"], e["amount"]) for e in events],
                         [("heal", 2726), ("damage", 5), ("instant-death", None)])

    def test_area_effects_keep_damage_and_healing_with_each_player(self):
        players = [PLAYER, "10FF0002", "10FF0003"]
        for index in (2, 0, 1):
            self.recap.process(area_ability(target=players[index].lower(), index=index,
                                            pairs=[("03", f"{(index + 1) * 100 << 16:X}")]))
            self.recap.process(area_ability(source=players[1], target=players[index], index=index,
                                            pairs=[("04", f"{(index + 1) * 50 << 16:X}")]))
        self.recap.process(area_ability(target=BOSS))
        for actor in reversed(players):
            self.recap.process(["25", "ts", actor, actor])
        self.assertEqual(len(self.recap.deaths), 3)
        for index, death in enumerate(self.recap.deaths):
            self.assertEqual(death["actor"], int(players[index], 16))
            self.assertEqual([(e["kind"], e["amount"]) for e in death["events"]],
                             [("damage", (index + 1) * 100), ("heal", (index + 1) * 50)])

    def test_area_reflection_and_self_healing_do_not_leak_between_targets(self):
        other = "10FF0002"
        first = area_ability(source=PLAYER, target=other, index=0, count=2,
                             pairs=[("03", "640000"), ("104", "140000"), ("1D", "0"), ("03", "50000")])
        first[3], first[7] = "Caster", "Reflector"
        second = area_ability(source=PLAYER, target=BOSS, index=1, count=2,
                              pairs=[("03", "C80000"), ("104", "1E0000")])
        second[3] = "Caster"
        self.recap.process(first)
        self.recap.process(second)
        self.recap.process(["25", "ts", PLAYER, "Caster"])
        self.recap.process(["25", "ts", other, "Reflector"])
        deaths = {d["actor"]: d for d in self.recap.deaths}
        self.assertEqual([(e["kind"], e["amount"], e["source"]) for e in deaths[int(PLAYER, 16)]["events"]],
                         [("heal", 20, "Caster"), ("damage", 5, "Reflector"), ("heal", 30, "Caster")])
        self.assertEqual([(e["kind"], e["amount"], e["source"]) for e in deaths[int(other, 16)]["events"]],
                         [("damage", 100, "Caster")])

    def test_ticks_and_reset_do_not_mix_actor_lifetimes(self):
        self.recap.process(["24", "ts", PLAYER, "Player", "DoT", "0", "A"])
        self.assertEqual(self.death()["events"][0]["amount"], 10)
        self.clock.value += 2
        self.recap.process(ability())
        self.recap.process(["01", "ts", "1", "New duty"])
        self.assertEqual(self.death()["events"], [])
        self.assertEqual(len(self.recap.deaths), 2)

    def test_bounds_malformed_input_and_duplicate_deaths(self):
        for fields in ([], ["21"], ["26"], ["24"], ["25"], ability(pairs=[("-3", "0")])):
            self.recap.process(fields)
        self.death()
        self.death()
        self.assertEqual(len(self.recap.deaths), 1)
        self.recap.process(["25", "ts", BOSS, "Boss"])
        self.assertEqual(len(self.recap.deaths), 1)
        for i in range(MAX_ACTORS + 20):
            self.recap.process(ability(target=f"{0x10000000 + i:X}"))
        self.assertLessEqual(len(self.recap.buffers), MAX_ACTORS)
        for i in range(MAX_DEATHS + 10):
            self.clock.value += 2
            self.death()
        self.assertEqual(len(self.recap.deaths), MAX_DEATHS)

    def test_wipe_keeps_late_deaths_but_next_pull_starts_fresh(self):
        self.recap.process(ability())
        self.recap.process(["33", "ts", "0", "4000000F"])
        self.assertEqual(len(self.death()["events"]), 1)
        self.clock.value += 2
        self.recap.process(ability())
        self.recap.begin_pull()
        self.assertEqual(self.death()["events"], [])

    def test_status_removal_accepts_equivalent_hex_ids(self):
        self.status()
        self.recap.process(["30", "ts", "0abc", "Vulnerability", "0", "040001234", "Boss", PLAYER, "Player"])
        self.assertEqual(self.death()["statuses"], [])

    def test_preparation_keeps_original_times_expiry_and_status_removals(self):
        self.status()
        self.recap.end_pull()
        self.status(duration="60")
        self.status(duration="1", source="40002222")
        self.status(duration="60", source="40003333")
        self.status("30", source="40003333")
        self.recap.process(ability(pairs=[("04", "1F40000")]))
        self.clock.value += 2
        self.recap.end_pull()
        self.recap.begin_pull()
        death = self.death()
        self.assertEqual(len(death["statuses"]), 1)
        self.assertEqual(death["statuses"][0]["expires"], self.clock() + 58)
        self.assertEqual(death["events"][-1]["time"], -2)
        self.assertEqual(death["events"][-1]["amount"], 500)

    def test_late_death_keeps_old_damage_and_clears_preparation_for_that_actor(self):
        self.recap.process(ability())
        self.recap.end_pull()
        self.status()
        self.recap.process(ability(pairs=[("04", "1F40000")]))
        self.assertEqual([e["kind"] for e in self.death()["events"]], ["damage", "gained", "heal"])
        self.recap.begin_pull()
        death = self.death()
        self.assertEqual(death["events"], [])
        self.assertEqual(death["statuses"], [])

    def test_preparation_stays_bounded_and_resets_with_the_feed(self):
        self.recap.end_pull()
        for index in range(MAX_ACTORS + 10):
            self.recap.process(ability(target=f"{0x10000000 + index:X}", pairs=[("04", "10000")]))
        self.assertLessEqual(len(self.recap.preparation), MAX_ACTORS)
        for index in range(MAX_EVENTS + 10):
            self.recap.process(["26", "ts", f"{index + 1:X}", "Status", "60", BOSS, "Boss", PLAYER, "Player"])
        buf = self.recap.preparation[int(PLAYER, 16)]
        self.assertEqual(len(buf["events"]), MAX_EVENTS)
        self.assertEqual(len(buf["statuses"]), MAX_STATUSES)
        self.recap.reset()
        self.recap.begin_pull()
        self.assertEqual(self.death()["events"], [])
        self.assertEqual(self.recap.preparation, {})


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.clock = Clock()
        self.sessions = ProgSessions(Path(self.temp.name), self.clock, lambda: 1000000)
        self.meter = DpsMeter(self.clock)
        self.meter.set_zone_metadata("Duty")
        self.meter.set_me(PLAYER)
        self.meter.on_pull_start = self.sessions.pull_started
        self.meter.on_pull_finish = self.sessions.pull_finished

    def start(self, in_combat=False):
        return self.sessions.start("Raid night", 1, "Duty", in_combat)

    def combat(self, game):
        self.sessions.combat(game)
        self.meter.set_in_combat(game, game)

    def test_repeated_pulls_duplicate_end_and_idle_display(self):
        session = self.start()
        self.combat(True)
        self.meter.process(ability())
        self.clock.value += 150
        self.meter.process(ability())
        self.assertEqual(self.meter.snapshot()["Encounter"]["DURATION"], 0)
        self.assertEqual(self.meter.full_snapshot()["Encounter"]["DURATION"], 150)
        self.combat(False)
        self.meter.process(["33", "ts", "0", "4000000F"])
        self.meter.process(["33", "ts", "0", "4000000F"])
        self.assertEqual(len(session["pulls"]), 1)
        self.assertEqual(session["pulls"][0]["ending"], "wipe")
        self.combat(True)
        self.meter.process(ability())
        self.clock.value += 20
        self.meter.process(ability())
        self.combat(False)
        self.assertEqual(summary(session), {"pulls": 2, "interrupted": 0, "longest": 150, "combat": 170})

    def test_mid_pull_start_and_reconnect_wait_for_idle(self):
        self.combat(True)
        self.meter.process(ability())
        session = self.start(in_combat=True)
        self.combat(False)
        self.assertEqual(session["pulls"], [])
        self.combat(True)
        self.meter.process(ability())
        self.clock.value += 3
        self.meter.feed_lost()
        self.sessions.feed_lost()
        self.meter.set_me(PLAYER)
        self.combat(True)
        self.meter.process(ability())
        self.combat(False)
        self.assertEqual(len(session["pulls"]), 1)
        self.assertFalse(session["pulls"][0]["complete"])
        self.combat(True)
        self.meter.process(ability())
        self.combat(False)
        self.assertEqual(summary(session)["pulls"], 1)

    def test_end_mid_pull_does_not_stop_meter_and_preserves_notes(self):
        session = self.start()
        self.combat(True)
        self.meter.process(ability())
        self.clock.value += 8
        pull = session["pulls"][0]
        pull.update(note="Reached towers", bookmark=True)
        self.sessions.end(self.meter.full_snapshot())
        self.assertIsNotNone(self.meter.current)
        self.assertFalse(pull["complete"])
        loaded = ProgSessions(self.temp.name).sessions[0]
        self.assertEqual(loaded["pulls"][0]["note"], "Reached towers")
        self.assertTrue(loaded["pulls"][0]["bookmark"])
        self.assertEqual(loaded["elapsed"], 8)

    def test_empty_pull_and_crash_recovery(self):
        session = self.start()
        self.combat(True)
        self.combat(False)
        self.assertEqual(session["pulls"], [])
        self.combat(True)
        self.meter.process(ability())
        loaded = ProgSessions(self.temp.name).sessions[0]
        self.assertEqual(loaded["state"], "interrupted")
        self.assertFalse(loaded["pulls"][0]["complete"])

    def test_atomic_write_failure_preserves_prior_file(self):
        session = self.start()
        path = Path(self.temp.name) / (session["id"] + ".json")
        before = path.read_bytes()
        session["name"] = "New name"
        with patch("nyaatriggers.record_store.os.replace", side_effect=OSError("Disk failed")):
            self.assertFalse(self.sessions.save(session))
        self.assertEqual(path.read_bytes(), before)
        self.assertIn("Disk failed", self.sessions.save_error)
        self.assertTrue(self.sessions.save(session))
        self.assertEqual(read_record(path)["name"], "New name")

    def test_corrupt_record_is_preserved_and_never_loaded(self):
        path = Path(self.temp.name) / (str(uuid4()) + ".json")
        path.write_text('{"broken":')
        loaded = ProgSessions(self.temp.name)
        self.assertEqual(loaded.sessions, [])
        self.assertEqual(len(loaded.errors), 1)
        self.assertEqual(path.read_text(), '{"broken":')
        bad = {"version": 1, "id": "../outside"}
        with self.assertRaises(ValueError):
            write_record(self.temp.name, bad)

    def test_end_without_snapshot_marks_pending_attempt_interrupted(self):
        session = self.start()
        self.combat(True)
        self.meter.process(ability())
        self.sessions.end()
        self.assertEqual(session["pulls"][0]["ending"], "session-ended")
        self.assertFalse(session["pulls"][0]["complete"])

    def test_large_dates_and_inconsistent_results_are_rejected(self):
        session = self.start()
        session["started"] = 10 ** 1000
        write_record(self.temp.name, session)
        loaded = ProgSessions(self.temp.name)
        self.assertEqual(loaded.sessions, [])
        self.assertEqual(len(loaded.errors), 1)

    def test_failed_saves_for_multiple_sessions_are_all_retried(self):
        first = self.start()
        self.sessions.end()
        second = self.start()
        first["name"] = "Unsaved first"
        second["name"] = "Unsaved second"
        with patch("nyaatriggers.prog_session.write_record", side_effect=OSError("Disk unavailable")):
            self.sessions.save(first)
            self.sessions.save(second)
        self.assertEqual(len(self.sessions.unsaved), 2)
        self.sessions.save(second)
        self.assertTrue(self.sessions.save_error)
        self.sessions.flush_pending()
        self.assertEqual(self.sessions.unsaved, {})
        self.assertEqual(self.sessions.save_error, "")
        loaded = ProgSessions(self.temp.name)
        self.assertEqual({s["name"] for s in loaded.sessions}, {"Unsaved first", "Unsaved second"})

    def test_checkpoint_failure_is_retried_at_the_next_interval(self):
        self.start()
        self.combat(True)
        self.meter.process(ability())
        with patch("nyaatriggers.prog_session.write_record", side_effect=OSError("Disk unavailable")) as save:
            for _ in range(CHECKPOINT_SECONDS * 2 - 1):
                self.clock.value += 1
                self.sessions.update_active(self.meter.full_snapshot())
                self.sessions.checkpoint()
            self.assertEqual(save.call_count, 1)
        self.assertIn("Disk unavailable", self.sessions.save_error)
        self.clock.value += 1
        self.sessions.update_active(self.meter.full_snapshot())
        self.sessions.checkpoint()
        loaded = ProgSessions(self.temp.name).sessions[0]
        self.assertEqual(loaded["pulls"][0]["duration"], CHECKPOINT_SECONDS * 2)
        self.assertEqual(loaded["elapsed"], CHECKPOINT_SECONDS * 2)
        self.assertEqual(self.sessions.save_error, "")
        self.assertEqual(self.sessions.unsaved, {})
        self.sessions.end(self.meter.full_snapshot())
        self.clock.value += CHECKPOINT_SECONDS
        with patch("nyaatriggers.prog_session.write_record") as save:
            self.sessions.checkpoint()
        save.assert_not_called()


class ProfileTests(unittest.TestCase):
    def window(self):
        return SimpleNamespace(_triggers=[Trigger(id="a", tts_text="Stack", enabled=True)],
                               _local_ids=set(), _engine_inventory=[], _engine_seen={s: {"one"} for s in SOURCES},
                               _engine_disabled={s: set() for s in SOURCES}, _triggevent_callout_edits={"one": "Left"},
                               _triggernometry_callout_edits={}, _settings={"cactbot_enabled": False, "token": "private"})

    def test_saved_choices_preserve_definitions_and_new_triggers(self):
        window = self.window()
        profile = capture_profile(window, "Tank prog")
        self.assertNotIn("private", json.dumps(profile))
        window._triggers[0].tts_text = "Different"
        window._triggers[0].enabled = False
        window._triggers[0].ability_id = "ABC"
        window._triggers.append(Trigger(id="new", tts_text="New", enabled=False))
        window._triggevent_callout_edits["one"] = "Right"
        apply_choices(window, profile)
        self.assertEqual(window._triggers[0].tts_text, "Stack")
        self.assertTrue(window._triggers[0].enabled)
        self.assertEqual(window._triggers[0].ability_id, "ABC")
        self.assertFalse(window._triggers[1].enabled)
        self.assertFalse(window._settings["cactbot_enabled"])
        self.assertEqual(window._triggevent_callout_edits["one"], "Left")

    def test_profile_default_resets_edits_and_rejects_bad_toggles(self):
        window = self.window()
        window._triggevent_callout_edits.clear()
        profile = capture_profile(window, "Default wording")
        window._triggevent_callout_edits["one"] = "Changed"
        apply_choices(window, profile)
        self.assertEqual(window._triggevent_callout_edits, {})
        profile["local"]["a"]["enabled"] = "false"
        with self.assertRaises(ValueError):
            validate_profile(profile)

    def test_default_covers_choices_introduced_by_other_profiles(self):
        window = self.window()
        first = capture_profile(window, "First")
        first["local"]["a"]["text"] = "First callout"
        first["engines"]["triggevent"]["unknown"] = {"enabled": False, "text": "First engine callout"}
        default = preserve_default(window, None, first)
        apply_choices(window, first)
        second = capture_profile(window, "Second")
        second["engines"]["triggevent"]["another"] = {"enabled": False, "text": "Second engine callout"}
        default = preserve_default(window, default, second)
        apply_choices(window, second)
        apply_choices(window, default)
        self.assertEqual(window._triggers[0].tts_text, "Stack")
        self.assertEqual(window._triggevent_callout_edits, {"one": "Left"})
        self.assertEqual(window._engine_disabled["triggevent"], set())


if __name__ == "__main__":
    unittest.main()
