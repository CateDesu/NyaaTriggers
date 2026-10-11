import unittest

from nyaatriggers.dps_meter import DpsMeter
from tests.test_session_features import BOSS, PLAYER, Clock, ability


PET = "40000002"
OTHER_PLAYER = "10FF0002"


class ActorReuseTests(unittest.TestCase):
    def setUp(self):
        self.meter = DpsMeter(Clock())
        self.meter.set_me(PLAYER)

    def spawn(self, owner, name="Pet"):
        self.meter.process(["03", "ts", PET, name, "00", "64", owner])

    def test_respawned_enemy_does_not_keep_the_previous_pets_owner(self):
        self.spawn(PLAYER)
        self.meter.process(ability(source=PET, target=BOSS))
        self.meter.process(["04", "ts", PET, "Pet"])
        self.spawn("0000", "Enemy")
        self.meter.process(ability(source=PET, target=OTHER_PLAYER))
        rows = self.meter.current.combatants
        self.assertEqual(rows[int(PLAYER, 16)].damage, 1000)
        self.assertEqual(rows[int(OTHER_PLAYER, 16)].damagetaken, 1000)
        self.assertEqual(self.meter.snapshot()["Encounter"]["damage"], 1000)

    def test_respawned_pet_contributes_to_its_new_owner(self):
        self.spawn(PLAYER)
        self.meter.process(ability(source=PET, target=BOSS))
        self.spawn(OTHER_PLAYER)
        self.meter.process(ability(source=PET, target=BOSS))
        rows = self.meter.current.combatants
        self.assertEqual(rows[int(PLAYER, 16)].damage, 1000)
        self.assertEqual(rows[int(OTHER_PLAYER, 16)].damage, 1000)

    def test_incomplete_spawn_metadata_keeps_known_pet_ownership(self):
        self.spawn(PLAYER)
        self.meter.process(["03", "ts", PET, "Pet"])
        self.meter.process(ability(source=PET, target=BOSS))
        self.assertEqual(self.meter.current.combatants[int(PLAYER, 16)].damage, 1000)


if __name__ == "__main__":
    unittest.main()
