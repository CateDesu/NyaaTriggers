import json
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt6.QtWidgets import QApplication

from nyaatriggers.dps_meter import DpsMeter
from nyaatriggers.ws_client import WSClient

APP = QApplication.instance() or QApplication([])


def ability(source, target, amount, owner="00", flags="150003"):
    fields = ["21", "2026-09-24T21:36:15.8080000-05:00", source, "Player Mage",
              "98", "Fire III", target, "Kefka", flags, f"{amount << 16:X}"]
    fields.extend(["0"] * (45 - len(fields)))
    fields.extend(["0", "1", owner, "Pet Owner"])
    return fields


class DelayedPlayerMetadataTests(unittest.TestCase):
    def setUp(self):
        self.now = 1000.0
        self.meter = DpsMeter(clock=lambda: self.now)
        self.ws = WSClient()
        self.ws.log_line.connect(lambda raw: self.meter.process(raw.split("|"), raw))
        self.ws.primary_player.connect(lambda actor, _name: self.meter.set_me(actor))
        self.ws.party_jobs.connect(lambda jobs: [self.meter.note_job(actor, job)
                                                for actor, job in jobs.items()])

    def feed(self, fields):
        self.ws._on_message(json.dumps({"type": "LogLine", "rawLine": "|".join(fields)}))

    def test_damage_before_identity_and_roster_is_retained_after_metadata(self):
        self.feed(["01", "ts", "553", "Dancing Mad"])
        self.feed(ability("10068719", "40003102", 29405))
        self.now += 5
        self.ws._on_message(json.dumps({"type": "ChangePrimaryPlayer", "charID": 0x10000001,
                                       "charName": "Another Player"}))
        self.ws._on_message(json.dumps({"type": "PartyChanged", "party": [
            {"id": "10068719", "job": 25, "inParty": True}]}))
        self.feed(ability("10068719", "40003102", 100))
        snapshot = self.meter.full_snapshot()
        self.assertEqual(snapshot["Combatant"]["Player Mage"]["damage"], 29505)
        self.assertEqual(snapshot["Combatant"]["Player Mage"]["Job"], "BLM")
        self.assertEqual(snapshot["Encounter"]["monotonic_start"], 1000)

    def test_damage_taken_and_death_before_jobs_are_not_dropped(self):
        self.feed(ability("40003102", "10068719", 50000))
        self.feed(["25", "ts", "10068719", "Player Victim"])
        snapshot = self.meter.full_snapshot()
        self.assertIsNotNone(snapshot)
        row = next(iter(snapshot["Combatant"].values()))
        self.assertEqual(row["damagetaken"], 50000)
        self.assertEqual(row["deaths"], 1)

    def test_pet_damage_uses_the_owner_before_owner_jobs_arrive(self):
        self.feed(ability("40001234", "40003102", 1200, owner="10068719"))
        snapshot = self.meter.full_snapshot()
        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot["Combatant"]["Pet Owner"]["damage"], 1200)
        self.assertEqual(len(snapshot["Combatant"]), 1)

    def test_npc_only_damage_and_unowned_pets_do_not_start_player_encounters(self):
        for source in ("40001234", "40003102", "E0000000"):
            self.feed(ability(source, "40003103", 100))
        self.assertIsNone(self.meter.full_snapshot())

    def test_zone_boundary_does_not_reuse_old_pet_ownership(self):
        self.feed(ability("40001234", "40003102", 1200, owner="10068719"))
        self.feed(["01", "ts", "554", "Next duty"])
        self.feed(ability("40001234", "40003102", 500))
        self.assertIsNone(self.meter.current)
        self.feed(ability("10068719", "40003102", 700))
        snapshot = self.meter.full_snapshot()
        self.assertEqual(snapshot["Encounter"]["title"], "Next duty")
        self.assertEqual(snapshot["Combatant"]["Player Mage"]["damage"], 700)

    def test_reconnect_accepts_damage_without_reusing_old_jobs(self):
        self.meter.note_job(0x10068719, 25)
        self.feed(ability("10068719", "40003102", 1200))
        self.meter.feed_lost()
        self.feed(ability("10068719", "40003102", 500))
        snapshot = self.meter.full_snapshot()
        self.assertEqual(snapshot["Combatant"]["Player Mage"]["damage"], 500)
        self.assertEqual(snapshot["Combatant"]["Player Mage"]["Job"], "")
        self.meter.note_job(0x10068719, 21)
        row = self.meter.full_snapshot()["Combatant"]["Player Mage"]
        self.assertEqual(row["damage"], 500)
        self.assertEqual(row["Job"], "WAR")


if __name__ == "__main__":
    unittest.main()
