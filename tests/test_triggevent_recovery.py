"""Recovery ordering, log matching and the combatant request protocol."""

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt6.QtWidgets import QApplication
from PyQt6.QtTest import QTest
from nyaatriggers.triggevent_bridge import TriggeventBridge
from nyaatriggers.triggevent_recovery import TriggeventRecovery, read_history
from nyaatriggers.ws_client import WSClient

_app = QApplication.instance() or QApplication([])


def log(kind, fields, second=0):
    return f"{kind:02}|2026-09-15T21:00:{second:02}.0000000-05:00|{fields}|0"


def frame(line):
    return json.dumps({"type": "LogLine", "rawLine": line})


class RecoveryTests(unittest.TestCase):
    def history(self, lines, anchor, **kwargs):
        with tempfile.TemporaryDirectory() as temp:
            (Path(temp) / "Network_test.log").write_text("\n".join(lines) + "\n")
            return read_history(Path(temp), anchor, kwargs.get("zone", 0x553), 0x10000001)

    def test_recovers_only_current_pull_and_seeds_existing_combatants(self):
        player = log(3, "10000001|Player")
        wipe = log(33, "800375AB|40000010|0|0|0|0", 10)
        old_cast = log(20, "40000001|Boss|1234", 5)
        current = log(20, "40000001|Boss|5678", 12)
        anchor = log(26, "1111|Buff", 14)
        history, reason = self.history([
            log(1, "553|Raid"), player, old_cast, wipe, current, anchor, log(20, "future", 15)
        ], anchor)
        restored = [json.loads(raw)["rawLine"] for raw in history]
        self.assertEqual(reason, "")
        self.assertEqual(restored[1:], [wipe, current])
        self.assertIn("|10000001|Player|", restored[0])
        self.assertEqual(restored[0].split("|")[1], wipe.split("|")[1])

    def test_rejects_unmatched_or_wrong_zone_history(self):
        anchor = log(20, "40000001|Boss|1234", 15)
        lines = [log(1, "553|Raid"), log(3, "10000001|Player"), anchor]
        self.assertFalse(self.history(lines, "missing")[0])
        self.assertFalse(self.history(lines, anchor, zone=123)[0])
        self.assertFalse(self.history(lines[1:], anchor)[0])

    def test_restores_static_arena_objects_and_merges_position_changes(self):
        anchor = log(20, "40000001|Boss|1234", 15)
        history, reason = self.history([
            log(1, "553|Raid"), log(3, "10000001|Player"),
            log(261, "Add|40000003|Type|7|PosX|90|PosY|110", 2),
            log(261, "Change|40000003|PosX|105", 3),
            log(33, "800375AB|40000010|0|0|0|0", 10), anchor,
        ], anchor)
        self.assertEqual(reason, "")
        positions = [json.loads(raw)["rawLine"] for raw in history
                     if json.loads(raw)["rawLine"].startswith("261|")]
        self.assertEqual(len(positions), 1)
        self.assertIn("|Type|7|PosX|105|PosY|110|", positions[0])

    def test_zone_changes_and_actor_removals_do_not_seed_stale_actors(self):
        anchor = log(20, "40000001|Boss|1234", 15)
        lines = [log(1, "553|Raid"), log(3, "10000001|Player"), log(3, "40000002|Pet"),
                 log(4, "40000002|Pet"), log(33, "800375AB|4000000F|0|0|0|0", 10), anchor]
        history, _ = self.history(lines, anchor)
        self.assertNotIn("Pet", "\n".join(history))
        lines.insert(-1, log(1, "123|Other Zone", 14))
        self.assertFalse(self.history(lines, anchor)[0])

    def test_state_then_history_then_buffer_then_live_without_command_injection(self):
        bridge = TriggeventBridge()
        bridge._active = True
        bridge._gen = 1
        bridge._recovery_gen = 1
        ws = WSClient()
        recovery = TriggeventRecovery(bridge, ws, lambda: None)
        recovery._on_status(True, "Starting", 1)
        state = {"type": "ChangeZone", "zoneID": 0x553, "zoneName": "Raid"}
        ws._on_message(json.dumps(state))
        live = frame(log(20, "40000001|Boss|1234", 15))
        recovery.feed(live)
        recovery.feed('{"nyaa_cmd":"set_automark","enable":true}')
        history = frame(log(20, "40000001|Boss|5678", 12))
        recovery._finish(1, [history], "")
        batch = [json.loads(line) for line in bridge._wq.get_nowait().splitlines()]
        self.assertEqual(batch[0]["nyaa_cmd"], "recover_begin")
        self.assertEqual(batch[1], state)
        self.assertEqual(batch[2], json.loads(history))
        self.assertEqual(batch[-2], json.loads(live))
        self.assertEqual(batch[-1]["nyaa_cmd"], "recover_end")
        self.assertEqual(sum("nyaa_cmd" in item for item in batch), 2)
        recovery.feed(live)
        self.assertEqual(bridge._wq.get_nowait(), live)

    def test_old_generation_cannot_finish_or_start_recovery(self):
        bridge = TriggeventBridge()
        bridge._active = True
        bridge._gen = 3
        ws = WSClient()
        recovery = TriggeventRecovery(bridge, ws, lambda: None)
        recovery._on_status(True, "Starting", 3)
        recovery._on_status(True, "Ready", 1)
        recovery._on_ready(1)
        recovery._finish(1, [], "")
        self.assertEqual(recovery._generation, 3)
        self.assertFalse(recovery._ready)
        self.assertTrue(bridge._wq.empty())

    def test_background_loader_joins_history_to_the_first_buffered_event(self):
        anchor = log(20, "40000001|Boss|1234", 15)
        with tempfile.TemporaryDirectory() as temp:
            lines = [log(1, "553|Raid"), log(3, "10000001|Player"),
                     log(33, "800375AB|40000010|0|0|0|0", 10), anchor]
            (Path(temp) / "Network_test.log").write_text("\n".join(lines) + "\n")
            bridge = TriggeventBridge()
            bridge._active = True
            bridge._gen = bridge._recovery_gen = 1
            ws = WSClient()
            recovery = TriggeventRecovery(bridge, ws, lambda: Path(temp))
            recovery._on_status(True, "Starting", 1)
            ws._on_message(json.dumps({"type": "ChangeZone", "zoneID": 0x553}))
            ws._on_message(json.dumps({"type": "ChangePrimaryPlayer", "charID": 0x10000001}))
            ws._on_message(frame(anchor))
            recovery._on_ready(1)
            for _ in range(100):
                if recovery._live:
                    break
                QTest.qWait(10)
            self.assertTrue(recovery._live)
            batch = [json.loads(raw) for raw in bridge._wq.get_nowait().splitlines()]
            self.assertEqual(sum(item.get("rawLine") == anchor for item in batch), 1)
            self.assertTrue(any("40000010" in item.get("rawLine", "") for item in batch))

    def test_old_engine_never_receives_historical_events(self):
        bridge = TriggeventBridge()
        bridge._active = True
        bridge._gen = 1
        ws = WSClient()
        recovery = TriggeventRecovery(bridge, ws, lambda: None)
        recovery._on_status(True, "Starting", 1)
        live = frame(log(20, "40000001|Boss|1234", 15))
        recovery.feed(live)
        recovery._finish(1, [frame(log(20, "40000001|Boss|5678", 12))], "Old engine")
        self.assertEqual(bridge._wq.get_nowait(), live)
        self.assertTrue(bridge._wq.empty())

    def test_polling_and_engine_requests_use_triggevent_response_tags(self):
        ws = WSClient()
        sent = []
        with patch.object(ws._ws, "isValid", return_value=True), \
             patch.object(ws._ws, "sendTextMessage", side_effect=lambda msg: sent.append(json.loads(msg))):
            ws.set_engine_combatant_polling(True)
            ws.set_combatant_polling(False)
            self.assertTrue(ws._poll_timer.isActive())
            self.assertEqual(sent[-1], {"call": "getCombatants", "rseq": "allCombatants"})
            ws.request_engine_combatants([10, 20])
            ws.request_engine_combatants([20, 30])
            ws._flush_combatant_requests()
            self.assertEqual(sent[-1], {"call": "getCombatants", "rseq": "specificCombatants", "ids": [10, 20, 30]})
            ws.request_engine_combatants([10])
            ws.request_engine_combatants([])
            ws._flush_combatant_requests()
            self.assertEqual(sent[-1]["rseq"], "allCombatants")
            ws.set_engine_combatant_polling(False)
            self.assertFalse(ws._poll_timer.isActive())

    def test_reconnect_restarts_only_an_active_engine(self):
        bridge = TriggeventBridge()
        bridge._active = True
        ws = WSClient()
        recovery = TriggeventRecovery(bridge, ws, lambda: None)
        with patch.object(bridge, "start") as start, patch.object(bridge, "stop") as stop:
            recovery._connection(True, "Connected")
            start.assert_not_called()
            recovery._connection(False, "Disconnected")
            recovery._connection(True, "Connected")
            start.assert_called_once()
            stop.assert_called_once()


if __name__ == "__main__":
    unittest.main()
