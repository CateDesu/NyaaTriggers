from contextlib import ExitStack
import json
import sys
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt6.QtWidgets import QApplication
from PyQt6.QtTest import QTest
from nyaatriggers.triggevent_bridge import TriggeventBridge, _ByteQueue
from nyaatriggers.triggevent_recovery import TriggeventRecovery
from nyaatriggers import diagnostics
from nyaatriggers.ws_client import WSClient

_app = QApplication.instance() or QApplication([])


def log(kind, fields, second=0):
    return f"{kind:02}|2026-09-15T21:00:{second:02}.0000000-05:00|{fields}|0"


def frame(line):
    return json.dumps({"type": "LogLine", "rawLine": line})


class RecoveryTests(unittest.TestCase):
    def test_queue_overflow_restarts_without_evicting_the_handoff(self):
        for pressure in ("count", "bytes", "command"):
            with self.subTest(pressure=pressure):
                bridge = TriggeventBridge()
                bridge._active = True
                bridge._gen = bridge._recovery_gen = bridge._catchup_gen = 1
                if pressure == "bytes":
                    bridge._wq = _ByteQueue(10000, maxbytes=300)
                recovery = TriggeventRecovery(bridge, WSClient(), lambda: None)
                recovery._on_status(True, "Starting", 1)
                recovery._finish(1, None, "")
                bridge._wq.get_nowait()
                bridge._dispatch({"t": "recovery_checkpoint", "checkpoint": 1}, gen=1)
                self.assertTrue(recovery._ending)
                with patch.object(bridge, "start") as start, patch.object(bridge, "stop") as stop:
                    for _ in range(10001):
                        if pressure == "command":
                            bridge._send_command({"nyaa_cmd": "set_automark", "enable": False})
                        else:
                            recovery.feed(frame(log(0, "0038|Player|Pressure")))
                    self.assertEqual(json.loads(bridge._wq.get_nowait())["nyaa_cmd"], "recover_end")
                    start.assert_called_once()
                    stop.assert_called_once()

    def test_missing_acknowledgement_restarts_and_stale_timeout_cannot_restart(self):
        bridge = TriggeventBridge()
        bridge._active = True
        bridge._gen = bridge._recovery_gen = bridge._catchup_gen = 1
        recovery = TriggeventRecovery(bridge, WSClient(), lambda: None)
        recovery._on_status(True, "Starting", 1)
        recovery._progress_timer.setInterval(20)
        with patch.object(bridge, "start") as start, patch.object(bridge, "stop") as stop:
            recovery._finish(1, None, "")
            QTest.qWait(60)
            start.assert_called_once()
            stop.assert_called_once()
            bridge._gen = 2
            recovery._restart(1)
            recovery._recovery_timed_out()
            start.assert_called_once()
            bridge._active = False
            recovery._on_status(False, "Off", 2)
            self.assertFalse(recovery._progress_timer.isActive())

    def test_final_acknowledgement_cancels_timeout(self):
        bridge = TriggeventBridge()
        bridge._active = True
        bridge._gen = bridge._recovery_gen = bridge._catchup_gen = 1
        recovery = TriggeventRecovery(bridge, WSClient(), lambda: None)
        recovery._on_status(True, "Starting", 1)
        recovery._progress_timer.setInterval(20)
        with patch.object(bridge, "start") as start:
            recovery._finish(1, None, "")
            bridge._wq.get_nowait()
            bridge._dispatch({"t": "recovery_checkpoint", "checkpoint": 1}, gen=1)
            bridge._dispatch({"t": "recovered", "checkpoint": 2}, gen=1)
            QTest.qWait(60)
            start.assert_not_called()
            self.assertTrue(recovery._live)
            self.assertFalse(recovery._progress_timer.isActive())

    def test_slow_restore_drains_new_input_before_acknowledged_live_handoff(self):
        bridge = TriggeventBridge()
        bridge._active = True
        bridge._gen = bridge._recovery_gen = bridge._history_gen = bridge._catchup_gen = 1
        ws = WSClient()
        recovery = TriggeventRecovery(bridge, ws, lambda: "/logs")
        recovery._on_status(True, "Starting", 1)
        anchor = frame(log(0, "0038|Player|Anchor"))
        recovery.feed(anchor)
        recovery._finish(1, {"folder": "/logs"}, "")
        initial = [json.loads(line) for line in bridge._wq.get_nowait().splitlines()]
        self.assertEqual(initial[-1], {"nyaa_cmd": "recover_checkpoint", "checkpoint": 1})
        self.assertFalse(recovery._live)
        during_restore = frame(log(0, "0038|Player|During restore", 1))
        recovery.feed(during_restore)
        self.assertTrue(bridge._wq.empty())
        bridge._dispatch({"t": "recovery_checkpoint", "checkpoint": 1}, gen=1)
        batch = [json.loads(line) for line in bridge._wq.get_nowait().splitlines()]
        self.assertEqual(batch, [json.loads(during_restore), {"nyaa_cmd": "recover_checkpoint", "checkpoint": 2}])
        bridge._dispatch({"t": "recovery_checkpoint", "checkpoint": 1}, gen=1)
        self.assertTrue(bridge._wq.empty())
        bridge._dispatch({"t": "recovery_checkpoint", "checkpoint": 2}, gen=1)
        self.assertEqual(json.loads(bridge._wq.get_nowait()), {"nyaa_cmd": "recover_end", "checkpoint": 3})
        self.assertFalse(recovery._live)
        final_race = frame(log(0, "0038|Player|During acknowledgement", 2))
        recovery.feed(final_race)
        self.assertEqual(bridge._wq.get_nowait(), final_race)
        with patch("nyaatriggers.triggevent_recovery._log") as report:
            bridge._dispatch({"t": "recovered", "checkpoint": 3, "status": "degraded", "skipped": 1}, gen=1)
            self.assertTrue(recovery._live)
            self.assertIn("degraded, skipped 1", report.call_args.args[0])

    def test_catchup_queue_retry_preserves_order_and_rejects_stale_acknowledgements(self):
        bridge = TriggeventBridge()
        bridge._active = True
        bridge._gen = bridge._recovery_gen = bridge._catchup_gen = 2
        ws = WSClient()
        recovery = TriggeventRecovery(bridge, ws, lambda: None)
        recovery._on_status(True, "Starting", 2)
        recovery._finish(2, None, "")
        bridge._wq.get_nowait()
        first, second = frame(log(0, "0038|Player|First")), frame(log(0, "0038|Player|Second", 1))
        recovery.feed(first)
        with patch.object(bridge, "catch_up", side_effect=[False, True]) as catch_up:
            bridge._dispatch({"t": "recovery_checkpoint", "checkpoint": 1}, gen=1)
            catch_up.assert_not_called()
            bridge._dispatch({"t": "recovery_checkpoint", "checkpoint": 1}, gen=2)
            recovery.feed(second)
            QTest.qWait(150)
            self.assertEqual(catch_up.call_args.args, ([first, second], 2))
            self.assertFalse(recovery._live)
            self.assertEqual(recovery._pending, [])

    def test_previous_catchup_capability_does_not_apply_to_new_engine(self):
        bridge = TriggeventBridge()
        bridge._active = True
        bridge._gen = bridge._recovery_gen = 2
        bridge._catchup_gen = 1
        self.assertFalse(bridge.supports_catchup())
        self.assertFalse(bridge.catch_up([], 1))

    def test_buffer_overflow_restarts_only_an_active_engine(self):
        bridge = TriggeventBridge()
        ws = WSClient()
        recovery = TriggeventRecovery(bridge, ws, lambda: None)
        with patch("nyaatriggers.triggevent_recovery._MAX_PENDING_BYTES", 1), \
                patch.object(bridge, "start") as start, patch.object(bridge, "stop") as stop:
            recovery.feed(frame(log(0, "0038|Player|Inactive")))
            start.assert_not_called()
            bridge._active = True
            recovery.feed(frame(log(0, "0038|Player|Active")))
            start.assert_called_once()
            stop.assert_called_once()
            self.assertFalse(recovery._live)
            self.assertEqual(recovery._pending, [])

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

    def test_polling_and_engine_requests_use_owned_response_tags(self):
        ws = WSClient()
        sent = []
        with patch.object(ws._ws, "isValid", return_value=True), \
             patch.object(ws._ws, "sendTextMessage", side_effect=lambda msg: sent.append(json.loads(msg))):
            ws.set_engine_combatant_polling(True)
            ws.set_combatant_polling(False)
            self.assertTrue(ws._poll_timer.isActive())
            self.assertEqual(sent[-1], {"call": "getCombatants", "rseq": "nyaa:allCombatants:2:"})
            ws.request_engine_combatants([10, 20])
            ws.request_engine_combatants([20, 30])
            ws._flush_combatant_requests()
            self.assertEqual(sent[-1], {"call": "getCombatants", "rseq": "nyaa:specificCombatants:3:", "ids": [10, 20, 30]})
            ws.request_engine_combatants([10])
            ws.request_engine_combatants([])
            ws._flush_combatant_requests()
            self.assertEqual(sent[-1]["rseq"], "nyaa:allCombatants:4:")
            ws.set_engine_combatant_polling(False)
            self.assertFalse(ws._poll_timer.isActive())

    def test_reconnect_restarts_only_an_active_engine(self):
        bridge = TriggeventBridge()
        bridge._active = True
        ws = WSClient()
        recovery = TriggeventRecovery(bridge, ws, lambda: None)
        with patch.object(bridge, "start") as start, patch.object(bridge, "stop", wraps=bridge.stop) as stop:
            recovery._connection(True, "Connected")
            start.assert_not_called()
            recovery._connection(False, "Disconnected")
            self.assertFalse(bridge.is_active())
            recovery._connection(True, "Connected")
            start.assert_called_once()
            stop.assert_called_once()


class RecoveryRetryTests(unittest.TestCase):
    def setUp(self):
        self.bridge, self.ws = TriggeventBridge(), WSClient()
        self.seed = [
            {"type": "ChangePrimaryPlayer", "charID": 0x10000001},
            {"type": "ChangeZone", "zoneID": 0x553},
            {"type": "InCombat", "inACTCombat": False, "inGameCombat": False},
        ]
        for message in self.seed:
            self.ws._on_message(json.dumps(message))
        self.recovery = TriggeventRecovery(self.bridge, self.ws, lambda: None)
        self.addCleanup(self.recovery._progress_timer.stop)
        self.addCleanup(self.recovery._retry_timer.stop)
        self.addCleanup(self.ws.disconnect_from)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch("nyaatriggers.triggevent_recovery._log"))
        self.starts = []
        self.stack.enter_context(patch.object(self.bridge, "start", side_effect=self.boot))
        self.boot()
        self.ws.status_changed.emit(True, "Connected")
        self.finish_restore()

    def boot(self):
        # Negotiate simulated capabilities without a local Java or jar requirement.
        self.bridge._active = True
        self.bridge._gen += 1
        generation = self.bridge._gen
        self.bridge._recovery_gen = self.bridge._catchup_gen = generation
        self.bridge._wq = _ByteQueue(100)
        self.starts.append(generation)
        self.bridge.status.emit(True, "Starting", generation)

    def queued(self):
        rows = []
        while not self.bridge._wq.empty():
            rows.extend(json.loads(raw) for raw in self.bridge._wq.get_nowait().splitlines())
        return rows

    def finish_restore(self):
        self.ws._on_message(frame(log(0, "0038|Player|Anchor")))
        self.bridge.ready.emit(self.bridge.generation())
        self.queued()
        self.bridge._dispatch({"t": "recovery_checkpoint", "checkpoint": 1}, gen=self.bridge.generation())
        self.queued()
        self.bridge._dispatch({"t": "recovered", "checkpoint": 2}, gen=self.bridge.generation())
        self.assertTrue(self.recovery._live)

    def exit(self, message="Sidecar exited"):
        self.bridge._active = False
        self.bridge.status.emit(False, message, self.bridge.generation())

    def retry(self):
        self.recovery._retry_timer.stop()
        self.recovery._retry_engine()

    def test_healthy_feed_retry_preserves_state_and_repeated_buffered_transitions(self):
        self.exit()
        self.assertTrue(self.recovery._retry_timer.isActive())
        self.assertEqual(self.recovery._retry_timer.interval(), 1000)
        line = json.loads(frame(log(0, "0038|Player|Repeated")))
        transitions = [{**self.seed[-1], "inGameCombat": True}, line, line, self.seed[-1]]
        for message in transitions:
            self.ws._on_message(json.dumps(message))
        self.retry()
        self.assertEqual(self.starts, [1, 2])
        self.assertEqual([json.loads(raw) for raw in self.recovery._pending], transitions)
        self.bridge.ready.emit(2)
        rows = self.queued()
        self.assertEqual(rows[0]["nyaa_cmd"], "recover_begin")
        self.assertEqual(rows[1:-1], [*self.seed, *transitions])
        self.assertEqual(rows[-1], {"nyaa_cmd": "recover_checkpoint", "checkpoint": 1})
        self.bridge._dispatch({"t": "recovery_checkpoint", "checkpoint": 1}, gen=2)
        self.assertEqual(self.queued(), [{"nyaa_cmd": "recover_end", "checkpoint": 2}])
        self.assertFalse(self.recovery._live)
        self.bridge._dispatch({"t": "recovered", "checkpoint": 2}, gen=1)
        self.assertFalse(self.recovery._live)
        self.bridge._dispatch({"t": "recovered", "checkpoint": 2}, gen=2)
        self.assertTrue(self.recovery._live)
        self.assertEqual(self.recovery._retry_delay_ms, 1000)
        self.ws._on_message(json.dumps(line))
        self.assertEqual(self.queued(), [line])

    def test_reader_exit_before_status_delivery_preserves_ordered_transitions(self):
        self.assert_reader_exit_preserves_transitions()

    def test_reader_exit_during_handoff_preserves_ordered_transitions(self):
        self.recovery._live = False
        self.recovery._ending = self.recovery._loading = True
        self.assert_reader_exit_preserves_transitions()

    def test_rejected_protocol_frames_do_not_interrupt_live_delivery(self):
        for raw in ("", "invalid", "[]", '{"nyaa_cmd":"set_automark","enable":true}'):
            self.recovery.feed(raw)
        self.assertTrue(self.recovery._live)
        self.assertEqual(self.recovery._pending, [])
        self.assertEqual(self.queued(), [])
        raw = frame(log(0, "0038|Player|Next callout"))
        self.recovery.feed(raw)
        self.assertEqual(self.queued(), [json.loads(raw)])

    def assert_reader_exit_preserves_transitions(self):
        self.bridge._active = False
        line = json.loads(frame(log(0, "0038|Player|Repeated")))
        transitions = [{**self.seed[-1], "inGameCombat": True}, line, line, self.seed[-1]]
        for message in transitions:
            self.ws._on_message(json.dumps(message))
        self.bridge.status.emit(False, "Sidecar exited", self.bridge.generation())
        self.assertEqual([json.loads(raw) for raw in self.recovery._pending], transitions)
        self.retry()
        self.bridge.ready.emit(2)
        self.assertEqual(self.queued()[1:-1], [*self.seed, *transitions])

    def test_startup_failures_back_off_without_feed_driven_or_duplicate_retries(self):
        self.exit("Failed to launch sidecar")
        delays = [self.recovery._retry_timer.interval()]

        def fail():
            self.bridge.status.emit(False, "Failed to launch sidecar", self.bridge.generation())

        with patch.object(self.bridge, "start", side_effect=fail) as start:
            for _ in range(7):
                self.ws._on_message(frame(log(0, "0038|Player|Still connected")))
                self.bridge.status.emit(False, "Failed to launch sidecar", self.bridge.generation())
                self.assertEqual(self.recovery._retry_timer.interval(), delays[-1])
                self.retry()
                self.assertTrue(self.recovery._retry_timer.isActive())
                delays.append(self.recovery._retry_timer.interval())
            self.assertEqual(start.call_count, 7)
        self.assertEqual(delays, [1000, 2000, 4000, 8000, 16000, 30000, 30000, 30000])
        self.retry()
        self.assertTrue(self.bridge.is_active())
        self.finish_restore()
        self.exit()
        self.assertEqual(self.recovery._retry_timer.interval(), 1000)

    def test_manual_stop_retires_a_failed_generation_and_cancels_retry(self):
        self.exit()
        failed_generation = self.bridge.generation()
        self.bridge._speech_cancel_pending = {"call": 1}
        self.bridge._speech_cancel_all_pending = 2
        self.bridge.stop()
        self.assertEqual(self.bridge.generation(), failed_generation + 1)
        self.assertFalse(self.recovery._retry_timer.isActive())
        self.assertFalse(self.recovery._restart_on_connect)
        self.assertEqual(self.bridge._speech_cancel_pending, {})
        self.assertIsNone(self.bridge._speech_cancel_all_pending)
        self.bridge.status.emit(False, "Sidecar exited", failed_generation)
        self.bridge.ready.emit(failed_generation)
        self.retry()
        self.assertEqual(self.starts, [1])
        self.assertFalse(self.bridge.is_active())

    def test_feed_loss_cancels_retry_and_reconnect_starts_only_one_replacement(self):
        self.exit()
        self.ws.status_changed.emit(False, "Disconnected")
        self.assertFalse(self.recovery._retry_timer.isActive())
        self.retry()
        self.assertEqual(self.starts, [1])
        self.ws.status_changed.emit(True, "Connected")
        self.ws.status_changed.emit(True, "Connected")
        self.retry()
        self.assertEqual(self.starts, [1, 2])
        self.assertTrue(self.bridge.is_active())

    def test_manual_start_cancels_retry_and_old_status_cannot_retire_replacement(self):
        self.exit()
        old_generation = self.bridge.generation()
        self.bridge.start()
        self.bridge.status.emit(False, "Sidecar exited", old_generation)
        self.retry()
        self.assertEqual(self.starts, [1, 2])
        self.assertFalse(self.recovery._retry_timer.isActive())
        self.assertTrue(self.bridge.is_active())

    def test_stop_before_first_start_leaves_no_retry_or_process_work(self):
        bridge = TriggeventBridge()
        ws = WSClient()
        manager = TriggeventRecovery(bridge, ws, lambda: None)
        with patch.object(bridge, "_reap") as reap, patch.object(bridge, "_signal_group") as signal:
            bridge.stop()
            ws.status_changed.emit(True, "Connected")
            manager._retry_engine()
            reap.assert_not_called()
            signal.assert_not_called()
        self.assertFalse(bridge.is_active())
        self.assertFalse(manager._retry_timer.isActive())

    def test_boot_stall_uses_progress_timeout_and_readiness_cancels_it(self):
        self.exit()
        self.retry()
        stalled_generation = self.bridge.generation()
        self.assertFalse(self.recovery._ready)
        self.assertTrue(self.recovery._progress_timer.isActive())
        self.recovery._recovery_timed_out()
        self.assertEqual(self.starts, [1, 2, 4])
        self.assertTrue(self.recovery._progress_timer.isActive())
        self.bridge.ready.emit(stalled_generation)
        self.assertFalse(self.recovery._ready)
        self.bridge.ready.emit(self.bridge.generation())
        self.assertTrue(self.recovery._ready)
        self.assertFalse(self.recovery._loading)
        self.assertFalse(self.recovery._progress_timer.isActive())
        self.recovery._recovery_timed_out()
        self.assertEqual(self.starts, [1, 2, 4])

    def test_duplicate_readiness_cannot_cancel_an_inflight_history_timeout(self):
        self.exit()
        self.ws._on_message(frame(log(0, "0038|Player|Buffered")))
        self.retry()
        self.bridge.ready.emit(2)
        self.assertTrue(self.recovery._loading)
        self.assertTrue(self.recovery._progress_timer.isActive())
        self.bridge.ready.emit(2)
        self.assertTrue(self.recovery._progress_timer.isActive())

    def test_boot_and_recovery_timeouts_preserve_undelivered_frames_in_order(self):
        for phase in ("boot", "recovery"):
            with self.subTest(phase=phase):
                self.exit()
                self.retry()
                if phase == "recovery":
                    self.ws._on_message(frame(log(0, "0038|Player|Submitted")))
                    self.bridge.ready.emit(self.bridge.generation())
                    self.queued()
                line = json.loads(frame(log(0, "0038|Player|Repeated")))
                transitions = [{**self.seed[-1], "inGameCombat": True}, line, line, self.seed[-1]]
                for message in transitions:
                    self.ws._on_message(json.dumps(message))
                old_generation = self.bridge.generation()
                self.recovery._recovery_timed_out()
                generation = self.bridge.generation()
                self.assertEqual(generation, old_generation + 2)
                self.assertEqual([json.loads(raw) for raw in self.recovery._pending], transitions)
                self.bridge.ready.emit(generation)
                self.assertEqual(self.queued()[1:-1], [*self.seed, *transitions])
                self.bridge._dispatch({"t": "recovery_checkpoint", "checkpoint": 1}, gen=generation)
                self.queued()
                self.bridge._dispatch({"t": "recovered", "checkpoint": 2}, gen=generation)
                self.assertTrue(self.recovery._live)

    def test_crash_during_history_retries_only_undelivered_buffered_frames(self):
        self.exit()
        submitted = frame(log(0, "0038|Player|Submitted"))
        undelivered = frame(log(0, "0038|Player|Undelivered", 1))
        self.ws._on_message(submitted)
        self.retry()
        self.recovery.log_folder = lambda: "/logs"
        self.bridge._history_gen = 2
        self.bridge.ready.emit(2)
        first = self.queued()
        self.assertEqual(first[0]["nyaa_cmd"], "recover_log")
        self.assertEqual(first[1:-1], [json.loads(submitted)])
        self.ws._on_message(undelivered)
        self.ws._on_message(undelivered)
        self.exit()
        self.retry()
        self.bridge._history_gen = 3
        self.bridge.ready.emit(3)
        second = self.queued()
        self.assertEqual(second[0]["nyaa_cmd"], "recover_log")
        self.assertEqual(second[0]["history"]["anchor"], json.loads(undelivered)["rawLine"])
        self.assertEqual(second[1:-1], [json.loads(undelivered), json.loads(undelivered)])
        self.assertEqual(self.recovery._pending, [])
        self.bridge._dispatch({"t": "recovery_checkpoint", "checkpoint": 1}, gen=2)
        self.assertEqual(self.queued(), [])
        self.assertFalse(self.recovery._live)
        self.bridge._dispatch({"t": "recovery_checkpoint", "checkpoint": 1}, gen=3)
        self.assertEqual(self.queued(), [{"nyaa_cmd": "recover_end", "checkpoint": 2}])
        self.bridge._dispatch({"t": "recovered", "checkpoint": 2}, gen=3)
        self.assertTrue(self.recovery._live)

    def test_inactive_retry_waits_for_reader_after_native_status_reports_failure(self):
        self.bridge.status.emit(False, "stdin closed", self.bridge.generation())
        self.assertTrue(self.bridge.is_active())
        self.assertFalse(self.recovery._retry_timer.isActive())
        self.exit()
        self.assertTrue(self.recovery._retry_timer.isActive())
        self.retry()
        self.assertEqual(self.starts, [1, 2])


class RecoveryDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        directory = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.log = directory / "diagnostics.log"
        self.stack.enter_context(patch.object(diagnostics, "_LOG_FILE", self.log))
        self.stack.enter_context(patch.object(diagnostics, "_ENABLED", True))
        self.stack.enter_context(patch("nyaatriggers.triggevent_recovery._log"))
        self.private = "PrivateSentinel /home/PrivatePerson token=PrivateSecret"

    def manager(self, *, generation=1, recovery=True, history=True, catchup=True, folder=None):
        bridge = TriggeventBridge()
        bridge._active = True
        bridge._gen = generation
        bridge._recovery_gen = generation if recovery else -1
        bridge._history_gen = generation if history else -1
        bridge._catchup_gen = generation if catchup else -1
        ws = WSClient()
        manager = TriggeventRecovery(bridge, ws, lambda: folder)
        self.addCleanup(lambda: manager._progress_timer.stop())
        manager._on_status(True, self.private, generation)
        manager._progress_timer.stop()
        return bridge, ws, manager

    def rows(self):
        text = self.log.read_text() if self.log.exists() else ""
        for secret in ("PrivateSentinel", "PrivatePerson", "PrivateSecret"):
            self.assertNotIn(secret, text)
        return [row["data"] for line in text.splitlines()
                if (row := json.loads(line))["event"] == "recovery_state"]

    def test_state_fallbacks_preserve_their_fixed_reason(self):
        cases = (
            ({"recovery": False}, False, "recovery_unsupported"),
            ({}, False, "no_local_log"),
            ({"history": False, "folder": self.private}, False, "history_unsupported"),
            ({"folder": self.private}, True, "invalid_world_state"),
        )
        for generation, (options, invalid_zone, reason) in enumerate(cases, 1):
            with self.subTest(reason=reason):
                bridge, ws, manager = self.manager(generation=generation, catchup=False, **options)
                zone_frame = json.dumps({"type": "ChangeZone", "zoneID": self.private if invalid_zone else 0x553})
                player_frame = json.dumps({"type": "ChangePrimaryPlayer", "charID": 0x10000001})
                ws._on_message(zone_frame)
                ws._on_message(player_frame)
                raw = frame(log(0, "0038|" + self.private))
                manager.feed(raw)
                manager._on_ready(generation)
                fallback = [row for row in self.rows() if row["gen"] == generation and row["state"] == "fallback"]
                self.assertEqual(len(fallback), 1)
                self.assertEqual(fallback[0]["reason"], reason)
                self.assertEqual((fallback[0]["pending_frames"], fallback[0]["pending_bytes"]),
                                 (3, sum(map(sys.getsizeof, (zone_frame, player_frame, raw)))))
                self.assertTrue(manager._live)

    def test_history_catchup_and_live_handoff_are_distinct(self):
        bridge, ws, manager = self.manager()
        raw = frame(log(0, "0038|" + self.private))
        manager.feed(raw)
        manager._finish(1, {"folder": self.private}, "")
        manager.feed(raw)
        bridge._dispatch({"t": "recovery_checkpoint", "checkpoint": 1}, gen=1)
        bridge._dispatch({"t": "recovery_checkpoint", "checkpoint": 2}, gen=1)
        bridge._dispatch({"t": "recovered", "checkpoint": 3,
                          "status": "degraded", "reason": self.private}, gen=1)
        rows = self.rows()
        self.assertEqual([row["state"] for row in rows],
                         ["starting", "restoring", "catchup", "ending", "live"])
        self.assertEqual(rows[2]["pending_frames"], 1)
        self.assertEqual(rows[-1]["reason"], "acknowledged")
        self.assertTrue(manager._live)

    def test_timeout_and_feed_queue_overflow_have_distinct_restart_reasons(self):
        bridge, ws, manager = self.manager()
        manager._loading = True
        with patch.object(bridge, "stop") as stop, patch.object(bridge, "start") as start:
            manager._recovery_timed_out()
            manager._restart(1)
            manager._restart(0)
        self.assertEqual(stop.call_count, 2)
        self.assertEqual(start.call_count, 2)
        self.assertEqual([row["reason"] for row in self.rows() if row["state"] == "restart"],
                         ["progress_timeout", "feed_overflow"])

    def test_buffer_overflow_records_pending_size_before_reset(self):
        bridge, ws, manager = self.manager()
        raw = frame(log(0, "0038|" + self.private))
        manager.feed(raw)
        with patch("nyaatriggers.triggevent_recovery._MAX_PENDING_BYTES", sys.getsizeof(raw)), \
                patch.object(bridge, "stop"), patch.object(bridge, "start"):
            manager.feed(raw)
        overflow = next(row for row in self.rows() if row["state"] == "buffer_overflow")
        self.assertEqual((overflow["pending_frames"], overflow["pending_bytes"]),
                         (1, sys.getsizeof(raw)))
        self.assertEqual(self.rows()[-1]["reason"], "buffer_overflow")
        self.assertEqual(manager._pending, [raw])

    def test_disconnect_reconnect_records_reason_without_status_text(self):
        bridge, ws, manager = self.manager()
        with patch.object(bridge, "stop", wraps=bridge.stop), patch.object(bridge, "start") as start:
            manager._connection(True, self.private)
            manager._connection(False, self.private)
            manager._connection(True, self.private)
        self.assertEqual([row["state"] for row in self.rows()],
                         ["starting", "connected", "disconnected", "stopped", "connected", "restart"])
        self.assertEqual(self.rows()[-1]["reason"], "reconnect")
        start.assert_called_once()

    def test_queue_retry_is_bounded_and_unknown_fallback_text_is_omitted(self):
        bridge, ws, manager = self.manager()
        with patch.object(bridge, "recover", return_value=False), \
                patch("nyaatriggers.triggevent_recovery.QTimer.singleShot"):
            for _ in range(20):
                manager._finish(1, None, self.private)
        self.assertEqual([row["state"] for row in self.rows()], ["starting", "fallback", "queue_wait"])
        self.assertEqual(self.rows()[1]["reason"], "unknown")


if __name__ == "__main__":
    unittest.main()
