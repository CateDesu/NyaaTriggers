"""Local callouts stay in their duties and match decoded game events."""

import json
import os
from pathlib import Path
import re
import unittest
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

_app = QApplication.instance() or QApplication([])

from nyaatriggers.app_common import _TREE_FIGHTS
from nyaatriggers.trigger_engine import Trigger, compile_user_regex


ROOT = Path(__file__).resolve().parents[1]
ROWS = json.loads((ROOT / "assets" / "triggers.json").read_text(encoding="utf-8"))
ZONES = json.loads((ROOT / "assets" / "zone_names.json").read_text(encoding="utf-8"))


class LocalTriggerDataTests(unittest.TestCase):
    def test_fights_have_categories_and_real_zones(self):
        self.assertEqual(len(ROWS), len({row["id"] for row in ROWS}))
        for row in ROWS:
            with self.subTest(trigger=row["name"]):
                if row.get("fight"):
                    self.assertIn(row["fight"], _TREE_FIGHTS)
                    self.assertTrue(row.get("zone_regex"))
                if row.get("zone_regex"):
                    pattern = row["zone_regex"]
                    self.assertIsNotNone(compile_user_regex(pattern, re.IGNORECASE))
                    matches = [name for name in ZONES.values()
                               if re.search(pattern, name, re.IGNORECASE)]
                    self.assertTrue(matches)
                    for name in matches:
                        self.assertIsNone(re.search(pattern, "Other " + name, re.IGNORECASE))
                        self.assertIsNone(re.search(pattern, name + " Other", re.IGNORECASE))
                ids = row.get("ability_id", "").split("|")
                if row.get("ability_id"):
                    self.assertTrue(all(int(ident, 16) > 0 for ident in ids))
                else:
                    self.assertTrue(row.get("ability_regex"))

    def test_fight_patterns_resolve_only_to_their_duties(self):
        expected = {
            "Queen EX": {1243},
            "Doomtrain EX": {1308},
            "Enuo EX": {1362},
            "Zelenia EX": {1271},
            "Zeromus EX": {1169},
            "UMAD": {1363},
            "Zadnor": {975},
            "The Ridorana Lighthouse": {776},
            "Another Aloalo Island": {1179, 1180},
            "Another Sil'dihn Subterrane": {1075, 1076},
            "Delubrum Reginae": {936, 937},
            "The Wreath of Snakes": {824},
            "The Crown of the Immaculate": {846},
            "The Dancing Plague": {845},
            "Seat of Sacrifice": {922},
            "Ultima's Bane EX": {348},
            "Windurst: The Third Walk": {1368},
            "Shinryu Unreal": {1372},
        }
        for floor in range(9, 13):
            expected[f"M{floor}N"] = {1320 + 2 * (floor - 9)}
            expected[f"M{floor}S"] = {1321 + 2 * (floor - 9)}
        for fight, ids in expected.items():
            rows = [row for row in ROWS if row.get("fight") == fight]
            self.assertTrue(rows, fight)
            for row in rows:
                with self.subTest(fight=fight, trigger=row["name"]):
                    actual = {int(ident) for ident, name in ZONES.items()
                              if re.search(row["zone_regex"], name, re.IGNORECASE)}
                    self.assertEqual(actual, ids)

    def test_player_actions_match_without_english_names(self):
        expected = {
            "Provoke": "1D6D", "Frog Legs (BLU)": "4783", "Shirk": "1D71",
            "Holmgang": "2B", "Hallowed Ground": "1E", "Superbolide": "3F18",
            "Living Dead": "E36", "Arm's Length": "1D7C", "Rampart": "1D6B",
            "Reprisal": "1D6F", "Feint": "1D7D", "Addle": "1D88",
        }
        for name, ident in expected.items():
            with self.subTest(action=name):
                row = next(row for row in ROWS if row["name"] == name)
                trigger = Trigger.from_dict({**row, "enabled": True})
                self.assertIsNotNone(trigger.matches([
                    "21", "timestamp", "10000001", "Player", ident,
                    "Localized ability name", "40000001", "Boss",
                ]))

    def test_verified_casts_match_in_their_duties(self):
        events = [
            (1363, "BABC", "Forsaken", "Large Raidwide"),
            (1363, "BAF2", "Bowels of Agony", "Raidwide"),
            (1362, "C370", "Airy Emptiness", "Stack With Partner"),
            (1362, "C37F", "Dimension Zero", "Stack"),
            (1368, "C41E", "Empirical Research", "Away From Front"),
            (1372, "C438", "Judgment Bolt", "Out of water"),
            (1003, "65F8", "Gaoler's Flail", "Out => In"),
        ]
        for zone, ident, ability, text in events:
            with self.subTest(zone=zone, ability=ability):
                matches = []
                for row in ROWS:
                    if not row.get("zone_regex") or not re.search(
                            row["zone_regex"], ZONES[str(zone)], re.IGNORECASE):
                        continue
                    trigger = Trigger.from_dict({**row, "enabled": True})
                    if trigger.matches(["20", "timestamp", "40000001", "Boss",
                                        ident, ability, "40000001", "Boss"]):
                        matches.append(trigger.tts_text)
                self.assertEqual(matches, [text])

    def test_ability_callouts_accept_both_damage_event_types(self):
        for row in ROWS:
            if row["log_type"] != "21|22":
                continue
            for event_type in ("21", "22"):
                with self.subTest(trigger=row["name"], event_type=event_type):
                    trigger = Trigger.from_dict({**row, "enabled": True})
                    self.assertIsNotNone(trigger.matches([
                        event_type, "timestamp", "40000001", "Boss",
                        row["ability_id"].split("|")[0], "Ability", "10000001", "Player",
                    ]))

    def test_instant_mechanics_use_ability_events(self):
        mechanics = [("A10N", "1AB8"), ("A10N", "1AB9"),
                     ("The Wreath of Snakes", "37E5"),
                     ("The Wreath of Snakes", "37E6")]
        for fight, ident in mechanics:
            with self.subTest(fight=fight, ability=ident):
                rows = [row for row in ROWS if row.get("fight") == fight
                        and ident in row.get("ability_id", "").split("|")]
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["log_type"], "21|22")

    def test_nael_dialogue_keeps_its_text_matcher(self):
        rows = [row for row in ROWS if row.get("fight") == "UCoB"
                and row["log_type"] == "00"]
        self.assertTrue(rows)
        for row in rows:
            self.assertFalse(row.get("ability_id"))
            self.assertTrue(row.get("ability_regex"))

    def test_misfiled_abilities_use_their_recorded_difficulty(self):
        expected = {
            "Queen EX": ("9398", 1201),
            "A11N": ("1A6E", 586),
            "O7N": ("2788", 754),
            "E4N": ("4114", 856),
            "The Dark Inside": ("67F0", 993),
            "The Mothercrystal": ("65C0", 996),
            "The Final Day": ("702C", 998),
        }
        for former_fight, (ident, zone) in expected.items():
            rows = [r for r in ROWS if ident in r.get("ability_id", "").split("|")]
            self.assertTrue(rows, ident)
            for row in rows:
                with self.subTest(trigger=row["name"]):
                    self.assertNotEqual(row.get("fight"), former_fight)
                    self.assertRegex(ZONES[str(zone)], row["zone_regex"])

    def test_m7n_calls_do_not_match_savage(self):
        rows = [r for r in ROWS if r.get("fight") == "M7N"]
        self.assertGreaterEqual(len(rows), 20)
        for row in rows:
            self.assertRegex(ZONES["1260"], row["zone_regex"])
            self.assertNotRegex(ZONES["1261"], row["zone_regex"])
        expected = {"A51C": "Out", "A51D": "In", "A52D": "Hide behind an add",
                    "A557": "Stack in tower"}
        for ident, text in expected.items():
            calls = [r["tts_text"] for r in rows
                     if ident in r["ability_id"].split("|")]
            self.assertEqual(calls, [text])

    def test_looper_tower_calls_use_recorded_durations(self):
        rows = [r for r in ROWS if r.get("fight") == "TOP"
                and r.get("ability_id") == "D80"]
        for duration, expected in [(16, "First tower"), (25, "Second tower"),
                                   (34, "Third tower"), (43, "Fourth tower"), (18, None)]:
            fields = ["26", "timestamp", "D80", "Looper", str(duration),
                      "E0000000", "", "10000001", "Player", "00"]
            calls = []
            for row in rows:
                trigger = Trigger.from_dict({**row, "enabled": True})
                if trigger.matches(fields, "Player"):
                    calls.append(trigger.tts_text)
                self.assertIsNone(trigger.matches(fields, "Someone Else"))
            self.assertEqual(calls, [] if expected is None else [expected])

    def test_simultaneous_helpers_share_one_callout(self):
        row = next(r for r in ROWS if r.get("fight") == "M1S"
                   and r.get("ability_id") == "945E")
        trigger = Trigger.from_dict({**row, "enabled": True})
        calls = []
        with patch("nyaatriggers.trigger_engine.time.monotonic", return_value=100):
            for actor in ("4000AEAA", "4000AEAB", "4000AEAC", "4000AEAD"):
                fields = ["20", "timestamp", actor, "Black Cat", "945E",
                          "Quadruple Swipe", "10000001", "Player", "4.700"]
                if trigger.matches(fields):
                    calls.append(trigger.tts_text)
        self.assertEqual(calls, ["Partner Stacks"])
        with patch("nyaatriggers.trigger_engine.time.monotonic", return_value=106):
            self.assertIsNotNone(trigger.matches(fields))

    def test_shared_expiry_warnings_use_one_cooldown(self):
        from nyaatriggers.ui.instance_tab import InstanceTabMixin

        class Host(InstanceTabMixin):
            def _drop_status_timer(self, runner):
                pass

            def _fire(self, trigger, captured):
                self.calls.append(trigger.tts_text)

        trigger = Trigger(log_type="26", ability_id="EEA|EE9", expiry_warn_s=6,
                          cooldown_scope="trigger", tts_text="Healer Groups")
        host = Host()
        host._local_enabled = True
        host._triggers = [trigger]
        host.calls = []
        with patch("nyaatriggers.ui.instance_tab.time.monotonic", return_value=100):
            for effect in ("EEA", "EE9"):
                host._on_status_timer(SimpleNamespace(trigger=trigger, effect_id=effect), {})
        self.assertEqual(host.calls, ["Healer Groups"])

    def test_fuse_field_distinguishes_recorded_durations(self):
        rows = [r for r in ROWS if r.get("fight") == "M3S"
                and "FB4" in r.get("ability_id", "").split("|")]
        for duration, expected in [(26, "Short Fuse"), (44, "Long Fuse")]:
            fields = ["26", "timestamp", "FB4", "Bombarium", str(duration),
                      "E0000000", "", "10000001", "Player", "00"]
            calls = []
            for row in rows:
                trigger = Trigger.from_dict({**row, "enabled": True})
                if trigger.matches(fields, "Player"):
                    calls.append(trigger.tts_text)
            self.assertEqual(calls, [expected])

    def test_burn_baby_burn_distinguishes_both_phases(self):
        rows = [r for r in ROWS if r.get("fight") == "M5S"
                and r.get("ability_id") == "116D"]
        for duration, expected in [(23.5, "Short cleanse"), (31.5, "Long cleanse"),
                                   (9.5, "Cleanse in spotlight"), (19.5, "Bait Frog")]:
            fields = ["26", "timestamp", "116D", "Burn Baby Burn", str(duration),
                      "E0000000", "", "10000001", "Player", "00"]
            calls = []
            for row in rows:
                if row.get("expiry_warn_s"):
                    continue
                trigger = Trigger.from_dict({**row, "enabled": True})
                if trigger.matches(fields, "Player"):
                    calls.append(trigger.tts_text)
            self.assertEqual(calls, [expected])

    def test_empty_wipe_rearms_shared_cooldown(self):
        from tests.test_overlay_retention import OverlayHost

        host = OverlayHost([100.0])
        trigger = Trigger(log_type="20", ability_id="5175", cooldown_s=9999,
                          cooldown_scope="trigger")
        host._triggers = [trigger]
        fields = ["20", "timestamp", "40000001", "Dawon", "5175", "Obey",
                  "40000001", "Dawon"]
        self.assertIsNotNone(trigger.matches(fields))
        self.assertIsNone(trigger.matches(fields))
        self.assertIsNone(host._dps_meter.current)
        host.wipe()
        self.assertIsNotNone(trigger.matches(fields))


if __name__ == "__main__":
    unittest.main()
