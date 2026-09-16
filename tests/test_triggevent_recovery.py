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
from nyaatriggers.triggevent_recovery import TriggeventRecovery
from nyaatriggers.ws_client import WSClient

_app = QApplication.instance() or QApplication([])


def log(kind, fields, second=0):
    return f"{kind:02}|2026-09-15T21:00:{second:02}.0000000-05:00|{fields}|0"


def frame(line):
    return json.dumps({"type": "LogLine", "rawLine": line})


class RecoveryTests(unittest.TestCase):
    def test_engine_history_request_then_buffer_then_live_without_command_injection(self):
        bridge = TriggeventBridge()
        bridge._active = True
        bridge._gen = 1
        bridge._recovery_gen = bridge._history_gen = 1
        ws = WSClient()
        recovery = TriggeventRecovery(bridge, ws, lambda: None)
        recovery._on_status(True, "Starting", 1)
        state = {"type": "ChangeZone", "zoneID": 0x553, "zoneName": "Raid"}
        ws._on_message(json.dumps(state))
        live = frame(log(20, "40000001|Boss|1234", 15))
        recovery.feed(live)
        recovery.feed('{"nyaa_cmd":"set_automark","enable":true}')
        history = {"folder": "/logs", "anchor": log(20, "40000001|Boss|1234", 15), "zone": 0x553, "player": 0x10000001}
        recovery._finish(1, history, "")
        batch = [json.loads(line) for line in bridge._wq.get_nowait().splitlines()]
        self.assertEqual(batch[0]["nyaa_cmd"], "recover_log")
        self.assertEqual(batch[0]["history"], history)
        self.assertEqual(batch[0]["state"], [state])
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
        recovery._finish(1, None, "")
        self.assertEqual(recovery._generation, 3)
        self.assertFalse(recovery._ready)
        self.assertTrue(bridge._wq.empty())

    def test_engine_receives_log_location_and_the_first_buffered_event(self):
        anchor = log(20, "40000001|Boss|1234", 15)
        with tempfile.TemporaryDirectory() as temp:
            lines = [log(1, "553|Raid"), log(3, "10000001|Player"),
                     log(33, "800375AB|40000010|0|0|0|0", 10), anchor]
            (Path(temp) / "Network_test.log").write_text("\n".join(lines) + "\n")
            bridge = TriggeventBridge()
            bridge._active = True
            bridge._gen = bridge._recovery_gen = bridge._history_gen = 1
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
            self.assertEqual(batch[0]["history"], {"folder": temp, "anchor": anchor,
                                                  "zone": 0x553, "player": 0x10000001})
            self.assertFalse(any("40000010" in item.get("rawLine", "") for item in batch))

    def test_old_engine_never_receives_historical_events(self):
        bridge = TriggeventBridge()
        bridge._active = True
        bridge._gen = 1
        ws = WSClient()
        recovery = TriggeventRecovery(bridge, ws, lambda: None)
        recovery._on_status(True, "Starting", 1)
        live = frame(log(20, "40000001|Boss|1234", 15))
        recovery.feed(live)
        recovery._finish(1, {"folder": "/logs"}, "Old engine")
        self.assertEqual(bridge._wq.get_nowait(), live)
        self.assertTrue(bridge._wq.empty())

    def test_history_request_survives_a_full_queue(self):
        bridge = TriggeventBridge()
        bridge._active = True
        bridge._gen = bridge._recovery_gen = bridge._history_gen = 1
        ws = WSClient()
        recovery = TriggeventRecovery(bridge, ws, lambda: None)
        recovery._on_status(True, "Starting", 1)
        history = {"folder": "/logs", "anchor": log(20, "40000001|Boss|1234", 15),
                   "zone": 0x553, "player": 0x10000001}
        recovery.feed(frame(history["anchor"]))
        with patch.object(bridge, "recover", side_effect=[False, True]) as recover:
            recovery._finish(1, history, "")
            self.assertFalse(recovery._live)
            QTest.qWait(150)
            self.assertTrue(recovery._live)
            self.assertEqual(recover.call_count, 2)
            self.assertEqual(recover.call_args.kwargs["history"], history)

    def test_previous_history_capability_does_not_apply_to_new_engine(self):
        bridge = TriggeventBridge()
        bridge._active = True
        bridge._gen = bridge._recovery_gen = 2
        bridge._history_gen = 1
        self.assertFalse(bridge.supports_local_history())
        self.assertFalse(bridge.recover([], "2026-09-16T00:00:00Z", history={"folder": "/logs"}))

    def test_invalid_world_state_falls_back_without_reading_history(self):
        bridge = TriggeventBridge()
        bridge._active = True
        bridge._gen = bridge._recovery_gen = bridge._history_gen = 1
        ws = WSClient()
        recovery = TriggeventRecovery(bridge, ws, lambda: "/logs")
        recovery._on_status(True, "Starting", 1)
        ws._on_message('{"type":"ChangeZone","zoneID":"invalid"}')
        ws._on_message('{"type":"ChangePrimaryPlayer","charID":268435457}')
        ws._on_message(frame(log(20, "40000001|Boss|1234", 15)))
        recovery._on_ready(1)
        batch = [json.loads(raw) for raw in bridge._wq.get_nowait().splitlines()]
        self.assertEqual(batch[0]["nyaa_cmd"], "recover_begin")
        self.assertTrue(recovery._live)

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
