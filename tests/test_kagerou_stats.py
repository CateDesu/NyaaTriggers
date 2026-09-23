"""Carry real combat statistics from the meter through the plugin frame."""

import math
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from nyaatriggers.dps_meter import DpsMeter
from nyaatriggers.plugin_link import dps_frame
from nyaatriggers.ui.dps_tab import DpsTabMixin


def ability(source, target, flags, amount):
    fields = ["21", "ts", source, "Player", "07", "Ability", target, "Target", flags, amount]
    return fields + [""] * (24 - len(fields))


class KagerouStatsTests(unittest.TestCase):
    def test_damage_healing_and_received_totals_reach_the_plugin(self):
        meter = DpsMeter(clock=lambda: 0)
        meter.process(["02", "ts", "10000001", "Player"])
        meter.process(["03", "ts", "10000001", "Player", "1F", "90", "0"])
        meter.process(["03", "ts", "10000002", "Healer", "18", "90", "0"])
        meter.process(ability("10000001", "40000010", "6003", "00640000"))
        meter.process(ability("10000001", "40000010", "0003", "00640000"))
        meter.process(ability("40000010", "10000001", "0003", "00320000"))
        meter.process(ability("10000002", "10000001", "0104", "004B0000"))
        snapshot = meter.snapshot()
        rows = meter.overlay_rows(snapshot, detailed=True)
        player, healer = rows
        self.assertEqual(player[7]["crit"], 50)
        self.assertEqual(player[7]["direct"], 50)
        self.assertEqual(player[7]["critDirect"], 50)
        self.assertEqual(player[7]["taken"], 50)
        self.assertEqual(player[7]["healingTaken"], 75)
        self.assertEqual(healer[7]["healShare"], 100)
        self.assertEqual(healer[7]["heals"], 1)
        link = Mock()
        link.is_connected.return_value = True
        window = SimpleNamespace(_dps_meter=meter, _plugin_link=link, _update_live_dps=lambda: None)
        DpsTabMixin._dps_tick(window)
        args, kwargs = link.send_dps.call_args
        frame = dps_frame(*args, **kwargs)
        self.assertEqual(frame["rows"], rows)
        self.assertEqual(frame["enc"]["hps"], 75)
        self.assertEqual(frame["enc"]["participants"], 2)
        self.assertEqual(frame["enc"]["id"], snapshot["Encounter"]["pull_id"])
        self.assertNotIn("overheal", frame["rows"][1][7])

    def test_legacy_rows_and_missing_metrics_remain_usable(self):
        row = ["Player", "MCH", 100, 100, 0, True, 0]
        self.assertEqual(dps_frame({}, [row])["rows"], [row])
        row.append({"crit": math.nan, "taken": -1, "healed": "bad", "hits": 4})
        self.assertEqual(dps_frame({}, [row])["rows"][0][7], {"hits": 4})


if __name__ == "__main__":
    unittest.main()
