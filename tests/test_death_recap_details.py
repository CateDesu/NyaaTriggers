from copy import deepcopy
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4
from zipfile import ZIP_DEFLATED, ZipFile

from nyaatriggers import status_metadata
from nyaatriggers.death_recap import DeathRecap
from nyaatriggers.recap_store import RecapStore, validate_recap
from tests.test_session_features import ability, PLAYER, BOSS, Clock


def corrupt_zip_member(path, member):
    """Damage a deflated member while preserving the ZIP directory."""
    with ZipFile(path) as archive:
        offset = archive.getinfo(member).header_offset
    raw = bytearray(path.read_bytes())
    name_size, extra_size = struct.unpack_from("<HH", raw, offset + 26)
    raw[offset + 30 + name_size + extra_size] = 7
    path.write_bytes(raw)


def health(kind="38", hp=8000, maximum=10000, shield=20, sequence=17, statuses=()):
    if kind == "39":
        return [kind, "ts", PLAYER, "Player", str(hp), str(maximum)]
    fields = [kind, "ts", PLAYER, "Player", f"{sequence:X}" if kind == "37" else "0",
              str(hp), str(maximum), "10000", "10000", str(shield)] + ["0"] * (5 if kind == "43" else 8)
    for ident, duration, source, stacks in statuses:
        bits = struct.unpack("!I", struct.pack("!f", duration))[0]
        fields.extend((f"{stacks << 16 | ident:X}", f"{bits:X}", source))
    return fields + ["checksum"]


def hit(hp=8000, maximum=10000, amount=1000, healing=False, sequence=17):
    fields = ability(pairs=[("24" if healing else "03", f"{amount << 16:X}")])
    if healing:
        fields[8] = "200004"
    fields += [str(hp), str(maximum)] + ["0"] * 18 + [f"{sequence:X}"]
    return fields


class DetailTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.recap = DeathRecap(self.clock)

    def status(self, kind="26", duration=30, source=BOSS):
        self.recap.process([kind, "ts", "ABC", "Mitigation", str(duration), source, "Healer",
                            PLAYER, "Player", "2"])

    def death(self):
        self.recap.process(["25", "ts", PLAYER, "Player"])
        return self.recap.deaths[0]

    def test_damaged_status_catalog_keeps_combat_recording_available(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "assets").mkdir()
            archive = root / "assets" / "recap_icons.zip"
            with ZipFile(archive, "w", ZIP_DEFLATED) as output:
                output.writestr("catalog.json", '{"Status":{"360":{"is_permanent":true}}}')
            with patch.object(status_metadata, "bundle_root", return_value=root):
                status_metadata._permanent_statuses.cache_clear()
                self.addCleanup(status_metadata._permanent_statuses.cache_clear)
                self.assertTrue(status_metadata.is_permanent(360))
                corrupt_zip_member(archive, "catalog.json")
                status_metadata._permanent_statuses.cache_clear()
                self.recap.process(health(statuses=[(360, 30, PLAYER, 0)]))
                self.assertEqual(self.death()["statuses"][0]["id"], 360)
                self.assertFalse(status_metadata.is_permanent(360))

    def test_minute_window_and_each_hits_statuses_remain_frozen(self):
        self.recap.process(hit())
        self.clock.value += 2
        self.status()
        self.clock.value += 18
        self.recap.process(hit(hp=7000))
        self.status("30")
        self.clock.value += 31
        events = self.death()["events"]
        hits = [e for e in events if e["kind"] == "damage"]
        self.assertEqual([e["time"] for e in hits], [-51, -31])
        self.assertEqual(hits[0]["statuses"], [])
        self.assertEqual(hits[1]["statuses"][0]["remaining"], 12)
        self.assertEqual(hits[1]["statuses"][0]["id"], 0xABC)
        self.assertEqual(hits[1]["hp"], 7000)
        self.assertEqual(self.recap.deaths[0]["statuses"], [])

    def test_status_list_recovers_buffs_active_before_connection_and_stack_value(self):
        self.recap.process(health(statuses=[(0xABC, -20, BOSS, 3)]))
        self.clock.value += 1
        self.recap.process(hit())
        event = self.death()["events"][-1]
        self.assertEqual((event["hp"], event["max_hp"], event["shield"]), (8000, 10000, 20))
        self.assertEqual(event["statuses"][0]["stacks"], 3)
        self.assertEqual(event["statuses"][0]["remaining"], 19)

    def test_status_snapshots_replace_earlier_packet_types(self):
        self.recap.process(health(statuses=[(0xABC, 20, BOSS, 2)]))
        self.recap.process(["42", "ts", PLAYER, "Player", "DEF", "41A00000", BOSS, "checksum"])
        self.assertEqual([s["id"] for s in self.recap.buffers[int(PLAYER, 16)]["statuses"].values()], [0xDEF])
        self.recap.process(health(statuses=[]))
        self.recap.process(hit())
        self.assertEqual(self.death()["events"][-1]["statuses"], [])

    def test_foray_snapshots_replace_gains_and_other_status_snapshots(self):
        self.status()
        self.recap.process(health(kind="43", statuses=[(0xDEF, 20, BOSS, 2)]))
        self.assertEqual([s["id"] for s in self.recap.buffers[int(PLAYER, 16)]["statuses"].values()], [0xDEF])
        self.recap.process(health(statuses=[(0xABC, 20, BOSS, 1)]))
        self.recap.process(hit())
        self.assertEqual([s["id"] for s in self.death()["events"][-1]["statuses"]], [0xABC])

    def test_empty_player_snapshot_removes_effects_from_other_formats(self):
        self.status()
        self.recap.process(["42", "ts", PLAYER, "Player", "0", "0", "0", "checksum"])
        self.recap.process(hit())
        self.assertEqual(self.death()["events"][-1]["statuses"], [])

    def test_matching_snapshot_retains_source_names_and_expires_timed_effects(self):
        self.status(duration=30)
        self.recap.process(health(kind="43", statuses=[(0xABC, 2, BOSS, 1)]))
        self.recap.process(hit())
        snapshot = self.recap.buffers[int(PLAYER, 16)]["events"][-1]["statuses"]
        self.assertEqual((snapshot[0]["name"], snapshot[0]["source"]), ("Mitigation", "Healer"))
        self.clock.value += 3
        self.recap.process(hit())
        self.assertEqual(self.death()["events"][-1]["statuses"], [])

    def test_foray_status_list_starts_immediately_after_heading(self):
        fields = ("43|ts|10FF0001|Player|00646428|107111|107111|10000|10000|0||"
                  "906.17|906.98|259.99|-2.36|131082|0|E0000000|1083|0|E0000000|"
                  "130214D3|0|10FF0001|checksum").split("|")
        fields[2] = PLAYER
        self.recap.process(fields)
        statuses = self.death()["statuses"]
        self.assertEqual([status["id"] for status in statuses], [0x1082, 0x1083, 0x14D3])
        self.assertEqual(statuses[0]["stacks"], 19)

    def test_empty_foray_status_list_clears_its_previous_statuses(self):
        fields = health(kind="43", statuses=[(71, 20, PLAYER, 0)])
        self.recap.process(fields)
        self.assertEqual(len(self.recap.buffers[int(PLAYER, 16)]["statuses"]), 1)
        self.recap.process(fields[:15] + ["checksum"])
        self.assertEqual(self.death()["statuses"], [])

    def test_permanent_status_snapshot_does_not_expire_its_placeholder_duration(self):
        fields = ("38|ts|108A7015|PLD Player|005A5A13|85648|90215|10000|10000|0||"
                  "101.55|111.07|0.00|-3.14|DF54|68|0|0A0168|41F00000|E0000000|"
                  "14016A|41F00000|E0000000|29310030|44C33E37|108A7015|4F|0|108A7015|"
                  "8c51e6f9dc094526").split("|")
        fields[2] = PLAYER
        self.recap.process(fields)
        self.clock.value += 45
        self.recap.process(hit())
        event = self.death()["events"][-1]
        statuses = {status["id"]: status for status in event["statuses"]}
        self.assertIn(0x168, statuses)
        self.assertIn(0x16A, statuses)
        self.assertIsNone(statuses[0x168]["remaining"])
        self.assertIsNone(statuses[0x16A]["remaining"])
        self.assertGreater(statuses[48]["remaining"], 0)

    def test_permanent_gain_survives_until_removal_and_unknown_statuses_still_expire(self):
        self.recap.process(["26", "ts", "168", "Meat and Mead", "30", "E0000000", "",
                            PLAYER, "Player", "A"])
        self.assertIsNone(self.recap.buffers[int(PLAYER, 16)]["events"][-1]["status_duration"])
        self.recap.process(["26", "ts", "FFFF", "Unknown status", "5", BOSS, "Boss",
                            PLAYER, "Player", "0"])
        self.assertEqual([status["id"] for status in self.recap.buffers[int(PLAYER, 16)]["events"][-1]["statuses"]],
                         [0x168, 0xFFFF])
        self.clock.value += 45
        self.recap.process(hit())
        event = self.recap.buffers[int(PLAYER, 16)]["events"][-1]
        self.assertEqual([status["id"] for status in event["statuses"]], [0x168])
        self.assertIsNone(event["statuses"][0]["remaining"])
        self.recap.process(["30", "ts", "168", "Meat and Mead", "0", "E0000000", "",
                            PLAYER, "Player", "A"])
        self.assertEqual(self.death()["statuses"], [])

    def test_heal_sync_records_actual_health_without_counting_overheal(self):
        self.recap.process(health(hp=8000, shield=0))
        self.recap.process(hit(amount=5000, healing=True))
        self.clock.value += .7
        self.recap.process(health(kind="37", hp=10000, shield=35))
        events = self.death()["events"]
        self.assertEqual(len(events), 2)
        event = events[-1]
        self.assertEqual(event["amount"], 5000)
        self.assertTrue(event["critical"])
        self.assertEqual((event["hp"], event["hp_after"], event["shield"], event["shield_after"]),
                         (8000, 10000, 0, 35))

    def test_sync_matches_both_sequence_and_recipient(self):
        self.recap.process(hit())
        other = health(kind="37", hp=0)
        other[2] = "10FF0002"
        self.recap.process(other)
        self.recap.process(health(kind="37", hp=6000, sequence=18))
        hit_event = next(e for e in self.death()["events"] if e["kind"] == "damage")
        self.assertNotIn("hp_after", hit_event)

    def test_hp_only_update_keeps_maximum_and_shields(self):
        self.recap.process(health())
        update = health(kind="39", hp=6000, maximum="")
        self.recap.process(update)
        event = self.death()["events"][-1]
        self.assertEqual((event["hp"], event["max_hp"], event["shield"]), (6000, 10000, 20))

    def test_confirmed_health_is_not_overwritten_by_stale_ability_memory(self):
        self.recap.process(health(hp=4000))
        self.recap.process(hit(hp=10000))
        event = self.death()["events"][-1]
        self.assertEqual(event["hp"], 4000)

    def test_first_status_id_is_a_real_buff(self):
        self.recap.process(health(statuses=[(1, 10, BOSS, 0)]))
        self.recap.process(hit())
        self.assertEqual(self.death()["events"][-1]["statuses"][0]["id"], 1)

    def test_environmental_status_snapshots_keep_names_and_do_not_duplicate(self):
        self.recap.process(["26", "ts", "13DB", "Spells' Trouble", "30", "E0000000", "",
                            PLAYER, "Player", "0"])
        self.recap.process(health(statuses=[(0x13DB, 30, "E0000000", 0)]))
        self.recap.process(hit())
        event = self.recap.buffers[int(PLAYER, 16)]["events"][-1]
        self.assertEqual(event["statuses"][0]["name"], "Spells' Trouble")
        self.recap.process(["26", "ts", "13DB", "Spells' Trouble", "30", "E0000000", "",
                            PLAYER, "Player", "0"])
        self.assertEqual(len(self.recap.buffers[int(PLAYER, 16)]["statuses"]), 1)
        self.recap.process(["30", "ts", "13DB", "Spells' Trouble", "0", "E0000000", "",
                            PLAYER, "Player", "0"])
        self.assertEqual(self.death()["statuses"], [])

    def test_later_healing_cannot_mutate_a_finished_recap(self):
        self.recap.process(hit(healing=True))
        death = deepcopy(self.death())
        self.recap.process(health(kind="37", hp=10000))
        self.assertEqual(self.recap.deaths[0], death)

    def test_tick_keeps_buff_context_and_missing_health_is_unknown(self):
        self.status()
        self.recap.process(["24", "ts", PLAYER, "Player", "HoT", "0", "100"])
        tick = self.death()["events"][-1]
        self.assertEqual(tick["kind"], "hot")
        self.assertEqual(tick["amount"], 256)
        self.assertIsNone(tick["hp"])
        self.assertEqual(tick["statuses"][0]["name"], "Mitigation")

    def test_tick_health_confirmation_requires_the_same_packet_timestamp(self):
        self.recap.process(health(hp=4000))
        tick = ["24", "tick", PLAYER, "Player", "HoT", "0", "3E8"]
        self.recap.process(tick)
        unrelated = health(hp=4500)
        self.recap.process(unrelated)
        confirmed = health(hp=5000)
        confirmed[1] = "tick"
        self.recap.process(confirmed)
        event = next(e for e in self.death()["events"] if e["kind"] == "hot")
        self.assertEqual((event["hp"], event["hp_after"]), (4000, 5000))
        self.assertNotIn("_tick_stamp", event)

    def test_attacker_mitigation_is_separate_from_player_statuses_and_frozen(self):
        self.status()
        self.recap.process(["26", "ts", "4A9", "Reprisal", "15", PLAYER, "Tank", BOSS, "Boss", "0"])
        self.recap.process(hit())
        self.recap.process(["30", "ts", "4A9", "Reprisal", "0", PLAYER, "Tank", BOSS, "Boss", "0"])
        self.recap.process(hit(healing=True))
        death = self.death()
        event = next(e for e in death["events"] if e["kind"] == "damage")
        self.assertEqual([s["id"] for s in event["source_statuses"]], [1193])
        self.assertEqual([s["id"] for s in event["statuses"]], [0xABC])
        self.assertEqual(event["source_statuses"][0]["source"], "Tank")
        self.assertNotIn("source_statuses", death["events"][-1])
        self.assertEqual([s["id"] for s in death["statuses"]], [0xABC])

    def test_attacker_status_list_recovery_removal_expiry_and_pull_reset(self):
        packet = health(statuses=[(1203, 5, PLAYER, 0), (1195, 15, PLAYER, 0), (202, 30, PLAYER, 3)])
        packet[2] = BOSS
        self.recap.process(packet)
        self.clock.value += 6
        self.recap.process(hit())
        event = self.recap.buffers[int(PLAYER, 16)]["events"][-1]
        self.assertEqual([s["id"] for s in event["source_statuses"]], [1195])
        self.recap.end_pull()
        self.recap.begin_pull()
        self.recap.process(hit())
        self.assertEqual(self.death()["events"][-1]["source_statuses"], [])

    def test_attacker_statuses_do_not_follow_a_different_enemy_or_reused_actor(self):
        self.recap.process(["26", "ts", "4A9", "Reprisal", "15", PLAYER, "Tank", BOSS, "Boss", "0"])
        other = hit()
        other[2] = "40009999"
        self.recap.process(other)
        self.recap.process(["04", "ts", BOSS, "Boss"])
        self.recap.process(hit())
        self.assertTrue(all(e["source_statuses"] == [] for e in self.death()["events"]))

    def test_attacker_mitigation_removal_and_fresh_preparation_survive_late_death(self):
        gain = ["26", "ts", "4A9", "Reprisal", "15", PLAYER, "Tank", BOSS, "Boss", "0"]
        self.recap.process(gain)
        self.recap.process(hit())
        empty = health()
        empty[2] = BOSS
        self.recap.process(empty)
        self.recap.process(hit())
        self.assertEqual(self.recap.buffers[int(PLAYER, 16)]["events"][-1]["source_statuses"], [])
        self.recap.end_pull()
        self.recap.process(gain)
        self.death()
        self.recap.begin_pull()
        self.clock.value += 2
        self.recap.process(hit())
        event = self.death()["events"][-1]
        self.assertEqual([s["id"] for s in event["source_statuses"]], [1193])
        self.assertEqual(event["source_statuses"][0]["remaining"], 13)

    def test_attacker_state_is_bounded_without_evicting_players(self):
        self.recap.process(hit())
        for index in range(150):
            self.recap.process(["26", "ts", "4A9", "Reprisal", "15", PLAYER, "Tank",
                                f"{0x40000000 + index:X}", "Boss", "0"])
        self.assertLessEqual(len(self.recap.sources), 128)
        self.assertEqual(len(self.recap.buffers[int(PLAYER, 16)]["events"]), 1)

    def test_status_change_keeps_stack_count_and_duration(self):
        self.status()
        self.status("30")
        gained, lost = self.death()["events"]
        self.assertEqual(gained["status_stacks"], 2)
        self.assertEqual(gained["status_duration"], 30)
        self.assertEqual(lost["status_stacks"], 2)

    def test_damage_details_and_misses_match_wire_effects(self):
        for flags in ("56005", "306", "1"):
            event = hit()
            event[8] = flags
            self.recap.process(event)
        blocked, parried, missed = self.death()["events"]
        self.assertTrue(blocked["blocked"])
        self.assertTrue(blocked["critical"])
        self.assertTrue(blocked["direct_hit"])
        self.assertEqual(blocked["damage_type"], 5)
        self.assertTrue(parried["parried"])
        self.assertEqual(missed["amount"], 0)

    def test_critical_heal_uses_the_healing_severity_byte(self):
        for flags in ("200004", "2004", "04"):
            event = hit(healing=True)
            event[8] = flags
            self.recap.process(event)
        self.assertEqual([e["critical"] for e in self.death()["events"]], [True, False, False])

    def test_rich_saved_details_round_trip_and_legacy_records_still_load(self):
        self.status()
        self.recap.process(["26", "ts", "4A9", "Reprisal", "15", PLAYER, "Tank", BOSS, "Boss", "0"])
        self.recap.process(health(statuses=[(0xABC, 20, BOSS, 2)]))
        self.recap.process(hit())
        self.recap.process(health(kind="37", hp=7000))
        death = self.death()
        with tempfile.TemporaryDirectory() as directory:
            store = RecapStore(directory)
            session, pull = str(uuid4()), str(uuid4())
            saved = store.record(session, pull, death)
            restored, errors = RecapStore(directory).load(session, pull)
            self.assertEqual(errors, [])
            self.assertEqual(restored, [saved])
            self.assertEqual(restored[0]["events"], death["events"])
            legacy = deepcopy(saved)
            legacy["events"] = [{k: v for k, v in event.items() if k in ("time", "name", "source", "kind", "amount")}
                                for event in legacy["events"] if event["kind"] != "health"]
            legacy["statuses"] = [{"name": "Mitigation", "source": "Healer"}]
            validate_recap(legacy)

    def test_invalid_nested_fields_are_rejected_before_display(self):
        self.status()
        self.recap.process(hit())
        data = self.death() | {"session_id": str(uuid4()), "pull_id": str(uuid4())}
        for key, value in (("hp", -1), ("max_hp", True), ("shield", 256), ("hp_after", float("nan")),
                           ("action_id", "../../icon"), ("critical", 1), ("statuses", [None]),
                           ("source_statuses", [None]), ("status_stacks", 65536), ("direct_hit", 1),
                           ("damage_type", 16), ("status_duration", float("nan"))):
            invalid = deepcopy(data)
            invalid["events"][-1][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate_recap(invalid)
        for key, value in (("id", []), ("remaining", float("inf")), ("stacks", 65536), ("name", None)):
            invalid = deepcopy(data)
            invalid["events"][-1]["statuses"][0][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate_recap(invalid)


if __name__ == "__main__":
    unittest.main()
