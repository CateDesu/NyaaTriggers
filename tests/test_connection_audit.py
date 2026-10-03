import builtins
from contextlib import ExitStack
from datetime import datetime
import errno
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication

from nyaatriggers import pull_capture, triggernometry_bridge
from nyaatriggers.triggernometry_editor import PackDocument
from nyaatriggers.triggevent_bridge import TriggeventBridge, _ByteQueue
from nyaatriggers.triggevent_recovery import TriggeventRecovery
from nyaatriggers.ws_client import WSClient


APP = QApplication.instance() or QApplication([])
ANCHOR = json.dumps({"type": "LogLine", "rawLine":
                     "20|2026-09-26T01:00:00-05:00|40000001|Boss|1234|Cast|"})


class RecoveryDisconnectTests(unittest.TestCase):
    def setUp(self):
        self.bridge = TriggeventBridge()
        self.bridge._active = True
        self.bridge._gen = self.bridge._recovery_gen = self.bridge._catchup_gen = 1
        self.ws = WSClient()
        self.recovery = TriggeventRecovery(self.bridge, self.ws, lambda: None)
        self.recovery._on_status(True, "Starting", 1)
        self.addCleanup(self.recovery._progress_timer.stop)
        self.addCleanup(self.ws.disconnect_from)
        quiet = patch("nyaatriggers.triggevent_recovery._log")
        quiet.start()
        self.addCleanup(quiet.stop)

    def disconnect(self):
        self.ws._on_disconnected()
        self.assertFalse(self.bridge.is_active())
        self.bridge._wq.get_nowait()

    def test_late_checkpoint_cannot_resume_a_disconnected_feed(self):
        self.recovery._finish(1, None, "")
        self.bridge._wq.get_nowait()
        self.disconnect()
        self.bridge._dispatch({"t": "recovery_checkpoint", "checkpoint": 1}, gen=1)
        self.assertTrue(self.bridge._wq.empty())
        self.assertFalse(self.recovery._progress_timer.isActive())
        self.assertFalse(self.recovery._live)
        with patch.object(self.bridge, "start") as start, patch.object(self.bridge, "stop") as stop:
            self.ws.status_changed.emit(True, "Connected")
            start.assert_called_once()
            stop.assert_not_called()

    def test_pending_history_retry_cannot_follow_a_disconnect_pause(self):
        self.recovery.feed(ANCHOR)
        with patch.object(self.bridge, "recover", side_effect=[False, True]) as recover:
            self.recovery._finish(1, None, "")
            self.assertEqual(recover.call_count, 1)
            self.disconnect()
            QTest.qWait(150)
            self.assertEqual(recover.call_count, 1)
        self.assertEqual(self.recovery._pending, [ANCHOR])
        self.assertFalse(self.recovery._progress_timer.isActive())

    def test_pending_catchup_retry_cannot_follow_a_disconnect_pause(self):
        self.recovery._finish(1, None, "")
        self.bridge._wq.get_nowait()
        self.recovery.feed(ANCHOR)
        with patch.object(self.bridge, "catch_up", side_effect=[False, True]) as catch_up:
            self.bridge._dispatch({"t": "recovery_checkpoint", "checkpoint": 1}, gen=1)
            self.assertEqual(catch_up.call_count, 1)
            self.disconnect()
            QTest.qWait(150)
            self.assertEqual(catch_up.call_count, 1)
        self.assertEqual(self.recovery._pending, [ANCHOR])
        self.assertFalse(self.recovery._progress_timer.isActive())

    def test_ready_callback_does_not_start_recovery_after_feed_loss(self):
        self.disconnect()
        with patch.object(self.bridge, "recover") as recover:
            self.recovery._on_ready(1)
            self.recovery._try_start(True)
            recover.assert_not_called()
        self.assertFalse(self.recovery._progress_timer.isActive())

    def test_feed_loss_cancels_recovery_timeout(self):
        self.recovery._progress_timer.setInterval(20)
        self.recovery._finish(1, None, "")
        self.bridge._wq.get_nowait()
        self.disconnect()
        with patch.object(self.bridge, "start") as start, patch.object(self.bridge, "stop") as stop:
            QTest.qWait(60)
            self.recovery._recovery_timed_out()
            start.assert_not_called()
            stop.assert_not_called()

    def test_late_finish_acknowledgement_keeps_the_feed_paused(self):
        self.recovery._finish(1, None, "")
        self.bridge._wq.get_nowait()
        self.bridge._dispatch({"t": "recovery_checkpoint", "checkpoint": 1}, gen=1)
        self.bridge._wq.get_nowait()
        self.assertTrue(self.recovery._ending)
        self.disconnect()
        self.bridge._dispatch({"t": "recovered", "checkpoint": 2}, gen=1)
        self.assertFalse(self.recovery._live)
        self.assertFalse(self.recovery._progress_timer.isActive())


class RecoveryOverflowStateTests(unittest.TestCase):
    def test_restarted_engine_keeps_the_frame_that_caused_overflow(self):
        cases = [
            ({"type": "PartyChanged", "party": [{"id": "10000001", "job": 24}]},
             {"type": "PartyChanged", "party": [{"id": "10000002", "job": 21}]}),
            ({"type": "ChangeZone", "zoneID": 100, "zoneName": "Old"},
             {"type": "ChangeZone", "zoneID": 200, "zoneName": "New"}),
            ({"type": "InCombat", "inACTCombat": True, "inGameCombat": True},
             {"type": "InCombat", "inACTCombat": False, "inGameCombat": False}),
            ({"type": "PartyChanged", "party": []}, json.loads(ANCHOR)),
        ]
        for stage in ("live", "ending", "buffered"):
            for previous, current in cases:
                with self.subTest(stage=stage, kind=current["type"]), ExitStack() as stack:
                    bridge = TriggeventBridge()
                    bridge._active = True
                    bridge._gen = bridge._recovery_gen = 1
                    ws = WSClient()
                    ws._on_message(json.dumps(previous))
                    recovery = TriggeventRecovery(bridge, ws, lambda: None)
                    recovery._on_status(True, "Starting", 1)
                    stack.callback(recovery._progress_timer.stop)
                    stack.enter_context(patch("nyaatriggers.triggevent_recovery._log"))
                    stack.enter_context(patch("nyaatriggers.triggevent_bridge._log"))
                    stack.enter_context(patch("nyaatriggers.triggevent_bridge.log_drop"))

                    def start():
                        bridge._active = True
                        bridge._gen += 1
                        bridge._recovery_gen = bridge._gen
                        bridge._wq = _ByteQueue(maxsize=100)
                        bridge.status.emit(True, "Starting", bridge._gen)

                    stack.enter_context(patch.object(bridge, "start", side_effect=start))
                    message = json.dumps(current)
                    if stage == "buffered":
                        pending = json.dumps({"type": "Other", "value": "old"})
                        stack.enter_context(patch("nyaatriggers.triggevent_recovery._MAX_PENDING_BYTES",
                                                  max(sys.getsizeof(pending), sys.getsizeof(message))))
                        recovery.feed(pending)
                    else:
                        recovery._live = stage == "live"
                        recovery._ending = stage == "ending"
                        bridge._wq = _ByteQueue(maxsize=1)
                        bridge._wq.put_nowait('{"type":"Old"}')
                    ws._on_message(message)
                    self.assertGreater(bridge.generation(), 1)
                    following = {"type": "LogLine", "rawLine":
                                 "20|2026-09-26T01:00:01-05:00|40000001|Boss|5678|Next|"}
                    if stage != "buffered":
                        recovery.feed(json.dumps(following))
                    recovery._on_ready(bridge.generation())
                    recovery._try_start(True)
                    batch = [json.loads(raw) for raw in bridge._wq.get_nowait().splitlines()]
                    states = [item for item in batch
                              if item.get("type") == current["type"] and item != following]
                    self.assertEqual(states[-1], current)
                    self.assertEqual(states.count(current), 1)
                    if stage != "buffered":
                        self.assertEqual(batch.count(following), 1)
                        self.assertLess(batch.index(current), batch.index(following))

    def test_successful_live_events_are_queued_once_without_recovery_buffering(self):
        for stage in ("live", "ending"):
            with self.subTest(stage=stage):
                bridge = TriggeventBridge()
                bridge._active = True
                bridge._gen = bridge._recovery_gen = 1
                recovery = TriggeventRecovery(bridge, WSClient(), lambda: None)
                recovery._on_status(True, "Starting", 1)
                recovery._live = stage == "live"
                recovery._ending = stage == "ending"
                second = ANCHOR.replace("1234|Cast", "5678|Next")
                recovery.feed(ANCHOR)
                recovery.feed(second)
                self.assertEqual(bridge._wq.get_nowait(), ANCHOR)
                self.assertEqual(bridge._wq.get_nowait(), second)
                self.assertTrue(bridge._wq.empty())
                self.assertEqual(recovery._pending, [])


class RecoveryConnectionLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.bridge = TriggeventBridge()
        self.bridge._active = True
        self.bridge._gen = self.bridge._recovery_gen = self.bridge._history_gen = 1
        self.ws = WSClient()
        self.recovery = TriggeventRecovery(self.bridge, self.ws, lambda: "/logs")
        self.recovery._on_status(True, "Starting", 1)
        self.addCleanup(self.recovery._progress_timer.stop)
        self.starts = []

        def start():
            if self.bridge.is_active():
                return
            self.bridge._active = True
            self.bridge._gen += 1
            self.bridge._recovery_gen = self.bridge._history_gen = self.bridge._gen
            self.bridge._wq = _ByteQueue(maxsize=100)
            self.starts.append(self.bridge._gen)
            self.bridge.status.emit(True, "Starting", self.bridge._gen)

        patched = patch.object(self.bridge, "start", side_effect=start)
        patched.start()
        self.addCleanup(patched.stop)
        quiet = patch("nyaatriggers.triggevent_recovery._log")
        quiet.start()
        self.addCleanup(quiet.stop)

    def test_first_connection_restarts_an_offline_restore_before_reading_history(self):
        self.recovery._on_ready(1)
        self.recovery._try_start(True)
        self.bridge._wq.get_nowait()
        self.assertTrue(self.recovery._live)
        self.ws.status_changed.emit(True, "Connected")
        self.assertEqual(len(self.starts), 1)
        self.ws._on_message('{"type":"ChangeZone","zoneID":1363}')
        self.ws._on_message('{"type":"ChangePrimaryPlayer","charID":268435457}')
        self.ws._on_message(ANCHOR)
        self.recovery._on_ready(self.bridge.generation())
        batch = [json.loads(raw) for raw in self.bridge._wq.get_nowait().splitlines()]
        self.assertEqual(batch[0]["nyaa_cmd"], "recover_log")
        self.assertEqual(batch[0]["history"]["anchor"], json.loads(ANCHOR)["rawLine"])

    def test_repeated_disconnects_retire_once_and_reconnect_starts_once(self):
        generation = self.bridge.generation()
        for _ in range(3):
            self.ws.status_changed.emit(False, "Disconnected")
        self.assertFalse(self.bridge.is_active())
        self.assertEqual(self.bridge.generation(), generation + 1)
        self.recovery._on_ready(generation)
        self.recovery._on_status(True, "Ready", generation)
        self.recovery._try_start(True)
        self.assertFalse(self.recovery._progress_timer.isActive())
        self.assertFalse(self.recovery._ready)
        self.assertEqual(self.starts, [])
        self.ws.status_changed.emit(True, "Connected")
        self.assertEqual(len(self.starts), 1)
        self.assertTrue(self.bridge.is_active())

    def test_first_connection_preserves_an_engine_still_waiting_for_feed(self):
        self.recovery._on_ready(1)
        self.ws.status_changed.emit(True, "Connected")
        self.assertEqual(self.starts, [])
        self.assertEqual(self.bridge.generation(), 1)

    def test_first_connection_retires_an_unacknowledged_empty_restore(self):
        self.bridge._catchup_gen = 1
        self.recovery._on_ready(1)
        self.recovery._try_start(True)
        self.bridge._wq.get_nowait()
        self.assertTrue(self.recovery._loading)
        self.assertFalse(self.recovery._live)
        self.ws.status_changed.emit(True, "Connected")
        self.assertEqual(len(self.starts), 1)
        self.bridge._dispatch({"t": "recovery_checkpoint", "checkpoint": 1}, gen=1)
        self.assertTrue(self.bridge._wq.empty())
        self.assertFalse(self.recovery._loading)

    def test_exited_engine_restarts_once_on_next_connection_and_ignores_late_ready(self):
        self.recovery._ready = self.recovery._loading = self.recovery._live = True
        self.recovery._progress_timer.start()
        self.bridge._active = False
        self.bridge.status.emit(False, "Sidecar exited", self.bridge.generation())
        self.assertFalse(self.recovery._ready)
        self.assertFalse(self.recovery._loading)
        self.assertFalse(self.recovery._live)
        self.assertFalse(self.recovery._progress_timer.isActive())
        self.recovery._on_ready(self.bridge.generation())
        self.recovery._try_start(True)
        self.assertFalse(self.recovery._ready)
        self.assertEqual(self.starts, [])
        self.ws.status_changed.emit(False, "Disconnected")
        self.ws.status_changed.emit(True, "Connected")
        self.ws.status_changed.emit(True, "Connected")
        self.assertEqual(len(self.starts), 1)
        self.assertTrue(self.bridge.is_active())

    def test_intentional_stop_does_not_arm_a_connection_restart(self):
        self.bridge.stop()
        self.ws.status_changed.emit(True, "Connected")
        self.assertEqual(self.starts, [])
        self.assertFalse(self.bridge.is_active())

    def test_manual_replacement_already_booting_satisfies_pending_restart(self):
        self.bridge._active = False
        self.bridge.status.emit(False, "Sidecar exited", self.bridge.generation())
        self.bridge.start()
        generation = self.bridge.generation()
        self.ws.status_changed.emit(True, "Connected")
        self.assertEqual(self.starts, [generation])
        self.assertEqual(self.bridge.generation(), generation)

    def test_failed_start_waits_for_another_successful_connection_before_retrying(self):
        self.bridge._active = False
        self.bridge.status.emit(False, "Could not start", self.bridge.generation())

        def fail_start():
            self.starts.append("failed")
            self.bridge.status.emit(False, "Could not start", self.bridge.generation())

        with patch.object(self.bridge, "start", side_effect=fail_start):
            self.ws.status_changed.emit(True, "Connected")
            self.assertEqual(self.starts, ["failed"])
            self.ws.status_changed.emit(True, "Connected")
            self.assertEqual(self.starts, ["failed"])
            self.recovery._try_start(True)
            self.recovery._recovery_timed_out()
            self.assertEqual(self.starts, ["failed"])
            self.ws.status_changed.emit(False, "Disconnected")
            self.ws.status_changed.emit(True, "Connected")
            self.assertEqual(self.starts, ["failed", "failed"])

    def test_offline_callout_toggles_preserve_the_independent_engine_and_one_reconnect(self):
        from nyaatriggers.ui.engines import EnginesMixin

        bridge = self.bridge

        class Host(EnginesMixin):
            _triggevent = bridge
            _triggevent_mode = False

            def _ensure_triggevent_bridge(self):
                return bridge

            def _update_automark_status_label(self):
                pass

            def _sync_custom_triggevent(self):
                pass

        self.ws.status_changed.emit(False, "Disconnected")
        host = Host()
        with patch.object(TriggeventBridge, "is_available", return_value=True):
            for enabled in (False, True, False):
                host._set_triggevent_enabled(enabled)
                self.assertEqual(host._triggevent_mode, enabled)
                self.assertTrue(self.bridge.is_active())
        self.assertEqual(len(self.starts), 1)
        self.recovery._on_ready(self.bridge.generation())
        self.recovery._try_start(True)
        self.assertFalse(self.recovery._live)
        self.assertTrue(self.bridge._wq.empty())
        self.ws.status_changed.emit(True, "Connected")
        self.assertEqual(len(self.starts), 2)
        self.assertFalse(host._triggevent_mode)


class NativeRecoveryExitTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "posix" and shutil.which("java") and shutil.which("xvfb-run")
                         and TriggeventBridge.is_available(), "Requires the Triggevent jar, Java and Xvfb")
    def test_feed_reconnect_restores_an_exited_native_engine_once(self):
        from PyQt6.QtNetwork import QHostAddress
        from PyQt6.QtWebSockets import QWebSocketServer

        with ExitStack() as stack:
            home = stack.enter_context(tempfile.TemporaryDirectory())
            stack.enter_context(patch.dict(os.environ, {
                "JAVA_TOOL_OPTIONS": f"-Duser.home={home}", "NYAA_AUTOMARK": "0"}))
            stack.enter_context(patch("nyaatriggers.triggevent_bridge._log"))
            stack.enter_context(patch("nyaatriggers.triggevent_recovery._log"))
            bridge, ws = TriggeventBridge(), WSClient()
            recovery = TriggeventRecovery(bridge, ws, lambda: None)
            stack.callback(recovery._progress_timer.stop)
            stack.callback(lambda: bridge.stop(wait=True))
            stack.callback(ws.disconnect_from)
            server = QWebSocketServer("recovery", QWebSocketServer.SslMode.NonSecureMode)
            self.assertTrue(server.listen(QHostAddress("127.0.0.1"), 0))
            stack.callback(server.close)
            peers, callouts = [], []
            bridge.callout.connect(lambda *args: callouts.append(args))

            def accept():
                peer = server.nextPendingConnection()
                peers.append(peer)

                def receive(raw):
                    if json.loads(raw).get("call") == "subscribe":
                        for frame in (
                                {"type": "ChangeZone", "zoneID": 0x553, "zoneName": "Raid"},
                                {"type": "ChangePrimaryPlayer", "charID": 0x10000001},
                                {"type": "LogLine", "rawLine": "00|"
                                 + datetime.now().astimezone().isoformat() + "|0038|Player|Anchor|0"}):
                            peer.sendTextMessage(json.dumps(frame))

                peer.textMessageReceived.connect(receive)

            def wait(predicate):
                deadline = time.monotonic() + 15
                while not predicate() and time.monotonic() < deadline:
                    QTest.qWait(10)
                self.assertTrue(predicate(), callouts)

            server.newConnection.connect(accept)
            url = f"ws://127.0.0.1:{server.serverPort()}/ws"
            bridge.start()
            ws.connect_to(url)
            wait(lambda: recovery._live)
            generation = bridge.generation()
            os.killpg(bridge._proc.pid, 9)
            wait(lambda: not bridge.is_active() and not recovery._ready)
            QTest.qWait(100)
            self.assertEqual(bridge.generation(), generation)
            ws.disconnect_from()
            ws.connect_to(url)
            wait(lambda: bridge.is_active() and recovery._live)
            self.assertEqual(bridge.generation(), generation + 1)
            ws.status_changed.emit(True, "Connected")
            self.assertEqual(bridge.generation(), generation + 1)
            line = ("20|" + datetime.now().astimezone().isoformat()
                    + "|40000001|Boss|C622|Light of Judgment|10000001|Player|5|100|100|0|0|0")
            peers[-1].sendTextMessage(json.dumps({"type": "LogLine", "rawLine": line}))
            wait(lambda: len(callouts) == 1)
            QTest.qWait(100)
            self.assertEqual(len(callouts), 1)
            for peer in peers:
                peer.abort()


class CombatantSnapshotTests(unittest.TestCase):
    def test_specific_replies_preserve_full_snapshots_and_reach_raw_consumers(self):
        ws = WSClient()
        snapshots, raw = [], []
        ws.combatants.connect(snapshots.append)
        ws.raw_message.connect(raw.append)
        player = {"ID": 0x10000001, "Name": "Player", "CurrentHP": 50000}
        boss = {"ID": 0x40000001, "Name": "Boss", "CurrentHP": 1000000}
        full = json.dumps({"rseq": "allCombatants", "combatants": [player, boss]})
        partial = json.dumps({"rseq": "specificCombatants", "combatants": [boss]})
        ws._on_message(full)
        ws._on_message(partial)
        self.assertEqual(raw, [full, partial])
        self.assertEqual([c["id"] for c in snapshots[-1]["list"]],
                         [player["ID"], boss["ID"]])
        ws._on_message(json.dumps({"rseq": "specificCombatants", "combatants": []}))
        self.assertEqual([c["id"] for c in snapshots[-1]["list"]],
                         [player["ID"], boss["ID"]])
        ws._on_message(json.dumps({"rseq": "allCombatants", "combatants": [player]}))
        self.assertEqual([c["id"] for c in snapshots[-1]["list"]], [player["ID"]])
        ws._on_message(json.dumps({"type": "combatants", "combatants": []}))
        self.assertEqual(snapshots[-1]["list"], [])

    @unittest.skipUnless(os.name == "posix" and shutil.which("mono") and shutil.which("xvfb-run")
                         and triggernometry_bridge._find_exe(), "Requires the Triggernometry host, Mono and Xvfb")
    def test_specific_boss_reply_does_not_erase_triggernometry_player_hp(self):
        with ExitStack() as stack:
            root = Path(stack.enter_context(tempfile.TemporaryDirectory()))
            packs, runtime = root / "packs", root / "runtime"
            packs.mkdir()
            runtime.mkdir()
            document = PackDocument(packs / "player.xml")
            trigger = document.add_trigger()
            trigger.set("RegularExpression", r"PROBE (?<label>\w+)")
            ET.SubElement(trigger.find("Actions"), "Action", OrderNumber="1", ActionType="UseTTS",
                          Asynchronous="False", UseTTSTextExpression="${label} hp ${_me.currenthp}")
            document.save()
            stack.enter_context(patch.object(triggernometry_bridge, "_rundata_dir", return_value=runtime))
            stack.enter_context(patch.object(triggernometry_bridge, "_packs_dir", return_value=packs))
            stack.enter_context(patch.object(triggernometry_bridge, "_make_bundled_mono_executable"))
            stack.enter_context(patch.object(triggernometry_bridge, "_log"))
            bridge = triggernometry_bridge.TriggernometryBridge()
            ws = WSClient()
            ws.combatants.connect(bridge.feed_combatants)
            ws.log_line.connect(bridge.feed_log)
            inventory, speech = [], []
            bridge.inventory.connect(lambda *args: inventory.append(args))
            bridge.tts.connect(lambda text, _generation: speech.append(text))
            stack.callback(lambda: bridge.stop(wait=True))
            bridge.start()

            def wait(predicate):
                end = time.monotonic() + 10
                while not predicate() and time.monotonic() < end:
                    QTest.qWait(10)
                self.assertTrue(predicate(), speech)

            def probe(label):
                ws._on_message(json.dumps({"type": "LogLine", "rawLine":
                    f"00|2026-09-26T01:00:00-05:00|0038|Tester|PROBE {label}|checksum"}))
                wait(lambda: any(text.startswith(label + " hp") for text in speech))

            wait(lambda: bool(inventory))
            ws._on_message(json.dumps({"type": "ChangePrimaryPlayer", "charID": 0x10000001}))
            ws._on_message(json.dumps({"rseq": "allCombatants", "combatants": [
                {"ID": 0x10000001, "Name": "Player", "CurrentHP": 50000},
                {"ID": 0x40000001, "Name": "Boss", "CurrentHP": 1000000}]}))
            probe("before")
            self.assertEqual(speech[-1], "before hp 50000")
            ws._on_message(json.dumps({"rseq": "specificCombatants", "combatants": [
                {"ID": 0x40000001, "Name": "Boss", "CurrentHP": 900000}]}))
            probe("after")
            self.assertEqual(speech[-1], "after hp 50000")


    @unittest.skipUnless(os.name == "posix" and shutil.which("mono") and shutil.which("xvfb-run")
                         and triggernometry_bridge._find_exe(), "Requires the Triggernometry host, Mono and Xvfb")
    def test_native_self_id_recovers_after_early_lookup_and_player_changes(self):
        with ExitStack() as stack:
            root = Path(stack.enter_context(tempfile.TemporaryDirectory()))
            packs, runtime = root / "packs", root / "runtime"
            packs.mkdir()
            runtime.mkdir()
            document = PackDocument(packs / "identity.xml")
            trigger = document.add_trigger()
            trigger.set("Sequential", "True")
            trigger.set("RegularExpression", r"PROBE (?<label>\w+) (?<target>[0-9A-F]+)")
            actions = trigger.find("Actions")
            ET.SubElement(actions, "Action", OrderNumber="1", ActionType="UseTTS",
                          Asynchronous="False", UseTTSTextExpression="${label} id ${_me.id}")
            own = ET.SubElement(actions, "Action", OrderNumber="2", ActionType="UseTTS",
                                Asynchronous="False", UseTTSTextExpression="${label} targets you")
            condition = ET.SubElement(own, "Condition", Enabled="true", Grouping="And")
            ET.SubElement(condition, "ConditionSingle", Enabled="true", ExpressionL="${_me.id}",
                          ExpressionTypeL="String", ExpressionR="${target}",
                          ExpressionTypeR="String", ConditionType="StringEqualNocase")
            ET.SubElement(actions, "Action", OrderNumber="3", ActionType="UseTTS",
                          Asynchronous="False", UseTTSTextExpression="${label} done")
            document.save()
            for name, value in (("_rundata_dir", runtime), ("_packs_dir", packs)):
                stack.enter_context(patch.object(triggernometry_bridge, name, return_value=value))
            stack.enter_context(patch.object(triggernometry_bridge, "_make_bundled_mono_executable"))
            stack.enter_context(patch.object(triggernometry_bridge, "_log"))
            bridge = triggernometry_bridge.TriggernometryBridge()
            ws = WSClient()
            ws.combatants.connect(bridge.feed_combatants)
            ws.log_line.connect(bridge.feed_log)
            ws.zone_changed.connect(bridge.feed_zone)
            inventory, speech = [], []
            bridge.inventory.connect(lambda *args: inventory.append(args))
            bridge.tts.connect(lambda text, _gen: speech.append(text))
            stack.callback(lambda: bridge.stop(wait=True))

            def wait(predicate):
                end = time.monotonic() + 10
                while not predicate() and time.monotonic() < end:
                    QTest.qWait(10)
                self.assertTrue(predicate(), speech)

            def send(frame):
                ws._on_message(json.dumps(frame))

            def snapshot(ident):
                send({"type": "ChangePrimaryPlayer", "charID": ident})
                send({"rseq": "allCombatants", "combatants": [
                    {"ID": 0x10000001, "Name": "First", "CurrentHP": 50000},
                    {"ID": 0x10000002, "Name": "Second", "CurrentHP": 40000}]})

            def probe(label, expected, target=0x10000001):
                begin = len(speech)
                send({"type": "LogLine", "rawLine":
                      f"00|2026-09-26T04:00:00-05:00|0038|Tester|PROBE {label} {target:08X}|checksum"})
                wait(lambda: label + " done" in speech)
                correct = [f"{label} id {expected:08X}"]
                if expected == target:
                    correct.append(label + " targets you")
                correct.append(label + " done")
                self.assertEqual(speech[begin:], correct)

            bridge.start()
            wait(lambda: bool(inventory))
            probe("early", 0)
            snapshot(0x10000001)
            probe("ready", 0x10000001)
            send({"type": "ChangeZone", "zoneID": 1363, "zoneName": "Duty"})
            send({"type": "LogLine", "rawLine": "01|ts|553|Duty|checksum"})
            probe("sameplayer", 0x10000001)
            snapshot(0x10000002)
            probe("changed", 0x10000002, 0x10000002)
            probe("other", 0x10000002, 0x10000001)
            snapshot(0)
            probe("unknown", 0)
            snapshot(0x10000002)
            probe("restored", 0x10000002, 0x10000002)
            bridge.stop(wait=True)
            ws._on_disconnected()
            old_inventory = len(inventory)
            bridge.start()
            wait(lambda: len(inventory) > old_inventory)
            probe("reconnected", 0)
            snapshot(0x10000001)
            probe("newready", 0x10000001)


    @unittest.skipUnless(os.name == "posix" and shutil.which("mono") and shutil.which("xvfb-run")
                         and triggernometry_bridge._find_exe(), "Requires the Triggernometry host, Mono and Xvfb")
    def test_identity_metadata_updates_native_without_waiting_for_another_full_reply(self):
        from PyQt6.QtNetwork import QHostAddress
        from PyQt6.QtWebSockets import QWebSocketServer
        from tests.test_triggernometry_connection import Host

        with ExitStack() as stack:
            root = Path(stack.enter_context(tempfile.TemporaryDirectory()))
            packs, runtime = root / "packs", root / "runtime"
            packs.mkdir()
            runtime.mkdir()
            document = PackDocument(packs / "identity.xml")
            trigger = document.add_trigger()
            trigger.set("Sequential", "True")
            trigger.set("RegularExpression", r"PROBE (?<label>\w+) (?<target>[0-9A-F]+)")
            actions = trigger.find("Actions")
            ET.SubElement(actions, "Action", OrderNumber="1", ActionType="UseTTS",
                          Asynchronous="False", UseTTSTextExpression="${label} id ${_me.id}")
            own = ET.SubElement(actions, "Action", OrderNumber="2", ActionType="UseTTS",
                                Asynchronous="False", UseTTSTextExpression="${label} targets you")
            condition = ET.SubElement(own, "Condition", Enabled="true", Grouping="And")
            ET.SubElement(condition, "ConditionSingle", Enabled="true", ExpressionL="${_me.id}",
                          ExpressionTypeL="String", ExpressionR="${target}",
                          ExpressionTypeR="String", ConditionType="StringEqualNocase")
            ET.SubElement(actions, "Action", OrderNumber="3", ActionType="UseTTS",
                          Asynchronous="False", UseTTSTextExpression="${label} done")
            hp = document.add_trigger()
            hp.set("RegularExpression", "ROSTER")
            ET.SubElement(hp.find("Actions"), "Action", OrderNumber="1", ActionType="UseTTS",
                          Asynchronous="False", UseTTSTextExpression="retained hp ${_me.currenthp}")
            document.save()
            for name, value in (("_rundata_dir", runtime), ("_packs_dir", packs)):
                stack.enter_context(patch.object(triggernometry_bridge, name, return_value=value))
            stack.enter_context(patch.object(triggernometry_bridge, "_make_bundled_mono_executable"))
            stack.enter_context(patch.object(triggernometry_bridge, "_log"))
            host = Host()
            speech = []
            stack.enter_context(patch("nyaatriggers.ui.engines.speak",
                                      side_effect=lambda text, **_kwargs: speech.append(text)))
            host._set_triggernometry_enabled(True)
            stack.callback(lambda: host._triggernometry.stop(wait=True))
            stack.callback(host._ws.disconnect_from)
            server = QWebSocketServer("identity", QWebSocketServer.SslMode.NonSecureMode)
            self.assertTrue(server.listen(QHostAddress("127.0.0.1"), 0))
            stack.callback(server.close)
            peers, requests = [], []

            def send(frame):
                peers[-1].sendTextMessage(json.dumps(frame))

            def accept():
                peer = server.nextPendingConnection()
                peers.append(peer)

                def receive(raw):
                    data = json.loads(raw)
                    requests.append(data)
                    if len(requests) == 1:
                        self.assertEqual(data["call"], "getCombatants")
                        send({"rseq": data["rseq"], "combatants": [
                            {"ID": 0x10000001, "Name": "First", "CurrentHP": 50000}]})
                    elif data.get("call") == "subscribe":
                        player = 0x10000001 if len(peers) == 1 else 0x10000002
                        label = "startup" if len(peers) == 1 else "reconnected"
                        send({"type": "ChangePrimaryPlayer", "charID": player})
                        send({"type": "LogLine", "rawLine":
                              f"00|ts|0038|Tester|PROBE {label} {player:08X}|checksum"})

                peer.textMessageReceived.connect(receive)

            server.newConnection.connect(accept)
            host._ws.connect_to(f"ws://127.0.0.1:{server.serverPort()}/ws")

            def check(label, expected, target=0x10000001):
                deadline = time.monotonic() + 10
                while label + " done" not in speech and time.monotonic() < deadline:
                    QTest.qWait(10)
                correct = [f"{label} id {expected:08X}"]
                if expected == target:
                    correct.append(label + " targets you")
                correct.append(label + " done")
                self.assertEqual([text for text in speech if text.startswith(label + " ")], correct)

            def probe(label, expected, target=0x10000001):
                send({"type": "LogLine", "rawLine":
                      f"00|ts|0038|Tester|PROBE {label} {target:08X}|checksum"})
                check(label, expected, target)

            check("startup", 0x10000001)
            send({"type": "ChangePrimaryPlayer", "charID": 0x10000002})
            probe("changed", 0x10000002, 0x10000002)
            send({"type": "ChangePrimaryPlayer", "charID": 0})
            probe("unknown", 0)
            send({"type": "LogLine", "rawLine": "02|ts|10000001|First|checksum"})
            probe("raw", 0x10000001)
            send({"type": "LogLine", "rawLine": "00|ts|0038|Tester|ROSTER|checksum"})
            deadline = time.monotonic() + 3
            while "retained hp 50000" not in speech and time.monotonic() < deadline:
                QTest.qWait(10)
            self.assertIn("retained hp 50000", speech)
            host._set_triggernometry_enabled(False)
            host._set_triggernometry_enabled(True)
            probe("restarted", 0x10000001)
            send({"rseq": requests[0]["rseq"], "combatants": []})
            probe("late", 0x10000001)
            host._ws.disconnect_from()
            host._ws.connect_to(f"ws://127.0.0.1:{server.serverPort()}/ws")
            check("reconnected", 0x10000002, 0x10000002)
            for peer in peers:
                peer.abort()


class ManualDisconnectTests(unittest.TestCase):
    def test_manual_disconnect_cancels_work_without_waiting_for_peer_close(self):
        from tests.test_connection_recovery import FeedPeer, wait_for

        class SlowClosePeer(FeedPeer):
            def read(self, connection, size):
                data = super().read(connection, size)
                if size == 2 and data[0] & 15 == 8:
                    self.stop.wait(5)
                return data

        peer = SlowClosePeer()
        self.addCleanup(peer.close)
        client = WSClient()
        self.addCleanup(client.disconnect_from)
        status, callouts = [], []
        client.status_changed.connect(lambda connected, _message: status.append(connected))
        client.connect_to(peer.url)
        self.assertTrue(wait_for(lambda: status and status[-1]))
        client.set_combatant_polling(True)
        client._on_message('{"type":"ChangeZone","zoneID":1363}')
        client.request_engine_combatants([0x40000001])
        pending = QTimer()
        pending.setSingleShot(True)
        pending.timeout.connect(lambda: callouts.append("Old pull"))
        client.status_changed.connect(lambda connected, _message: None if connected else pending.stop())
        pending.start(50)
        client.disconnect_from()
        QTest.qWait(100)
        self.assertEqual(callouts, [])
        self.assertFalse(status[-1])
        self.assertEqual(client.state_snapshot(), ())
        self.assertFalse(client._poll_timer.isActive())
        self.assertFalse(client._refresh_timer.isActive())
        self.assertFalse(client._reconnect_timer.isActive())
        self.assertEqual(peer.connections, 1)


class TriggernometryOverflowTests(unittest.TestCase):
    def test_overflow_preserves_controls_and_requests_one_restart_per_generation(self):
        bridge = triggernometry_bridge.TriggernometryBridge()
        bridge._active = True
        bridge._gen = 3
        overflows = []
        bridge.feed_overflow.connect(overflows.append)
        with patch.object(triggernometry_bridge, "log_drop"):
            for generation in (3, 4):
                bridge._gen = generation
                bridge._wq = triggernometry_bridge._ByteQueue(maxsize=2)
                bridge.feed_combat(False, False)
                bridge.set_disabled(["disabled trigger"])
                for _ in range(3):
                    bridge.feed_log("00|ts|Ignored")
                self.assertEqual(overflows, list(range(3, generation + 1)))
                commands = [json.loads(bridge._wq.get_nowait()) for _ in range(2)]
                self.assertEqual(commands, [{"t": "combat", "active": False},
                                           {"t": "set_disabled", "ids": ["disabled trigger"]}])
                self.assertEqual(bridge._disabled, frozenset(["disabled trigger"]))
            bridge._active = False
            bridge.feed_log("00|ts|Inactive")
            self.assertEqual(overflows, [3, 4])

    def test_byte_overflow_keeps_zone_state_before_a_large_command(self):
        bridge = triggernometry_bridge.TriggernometryBridge()
        bridge._active = True
        bridge._gen = 1
        bridge._wq = triggernometry_bridge._ByteQueue(maxsize=20, maxbytes=512)
        overflows = []
        bridge.feed_overflow.connect(overflows.append)
        bridge.feed_zone(1363, "Dancing Mad")
        with patch.object(triggernometry_bridge, "log_drop"):
            bridge.set_callout("callout", text="word " * 1000)
        self.assertEqual(overflows, [1])
        self.assertEqual(json.loads(bridge._wq.get_nowait()),
                         {"t": "zone", "id": 1363, "name": "Dancing Mad"})
        self.assertTrue(bridge._wq.empty())


    @unittest.skipUnless(os.name == "posix" and shutil.which("mono") and shutil.which("xvfb-run")
                         and triggernometry_bridge._find_exe(), "Requires the Triggernometry host, Mono and Xvfb")
    def test_real_host_overflow_restores_disabled_calls_edits_and_combat_state(self):
        from tests.test_triggernometry_connection import Host

        with ExitStack() as stack:
            root = Path(stack.enter_context(tempfile.TemporaryDirectory()))
            packs, runtime = root / "packs", root / "runtime"
            packs.mkdir()
            runtime.mkdir()
            document = PackDocument(packs / "overflow.xml")
            for name, pattern, text in (("Disabled", "DISABLED", "disabled call"),
                                        ("Control", r"CONTROL (?<label>\w+)", "original ${label}")):
                trigger = document.add_trigger()
                trigger.set("Name", name)
                trigger.set("RegularExpression", pattern)
                ET.SubElement(trigger.find("Actions"), "Action", OrderNumber="1", ActionType="UseTTS",
                              Asynchronous="False", UseTTSTextExpression=text)
            document.save()
            stack.enter_context(patch.object(triggernometry_bridge, "_rundata_dir", return_value=runtime))
            stack.enter_context(patch.object(triggernometry_bridge, "_packs_dir", return_value=packs))
            stack.enter_context(patch.object(triggernometry_bridge, "_make_bundled_mono_executable"))
            stack.enter_context(patch.object(triggernometry_bridge, "_log"))
            stack.enter_context(patch.object(triggernometry_bridge, "log_drop"))
            host = Host()
            host._connected = True
            edits, definitions, speech = {}, [], []
            host._callout_edits_for = lambda _source: edits

            def inventory(payload, generation):
                definitions[:] = json.loads(payload)
                control = next(item for item in definitions if item["name"] == "Control")
                edits[control["id"]] = "edited ${label} ${_incombat}"
                host._replay_triggernometry_callout_edits()
                host.inventory.append(generation)

            host._on_triggernometry_inventory = inventory
            stack.enter_context(patch("nyaatriggers.ui.engines.speak",
                                      side_effect=lambda text, **_kwargs: speech.append(text)))
            host._ws._on_message('{"type":"InCombat","inACTCombat":true,"inGameCombat":true}')
            host._set_triggernometry_enabled(True)
            bridge = host._triggernometry
            stack.callback(lambda: bridge.stop(wait=True))

            def wait(predicate):
                end = time.monotonic() + 10
                while not predicate() and time.monotonic() < end:
                    QTest.qWait(10)
                self.assertTrue(predicate(), speech)

            def line(text):
                host._ws._on_message(json.dumps({"type": "LogLine", "rawLine":
                    f"00|2026-09-26T01:00:00-05:00|0038|Tester|{text}|checksum"}))

            wait(lambda: bool(host.inventory))
            line("CONTROL before")
            line("DISABLED")
            wait(lambda: "edited before 1" in speech and "disabled call" in speech)
            generation = bridge.generation()
            disabled = next(item["id"] for item in definitions if item["name"] == "Disabled")
            host._triggernometry_disabled = {disabled}
            bridge._wq._maxbytes = 1
            bridge.set_disabled([disabled])
            self.assertEqual(bridge.generation(), generation)
            wait(lambda: len(host.inventory) == 2)
            self.assertGreater(bridge.generation(), generation)
            self.assertTrue(bridge.is_active())
            line("DISABLED")
            line("CONTROL after")
            wait(lambda: "edited after 1" in speech)
            QTest.qWait(100)
            self.assertEqual(speech.count("disabled call"), 1)
            self.assertIn(disabled, bridge._disabled)


class TriggernometryZoneTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "posix" and shutil.which("mono") and shutil.which("xvfb-run")
                         and triggernometry_bridge._find_exe(), "Requires the Triggernometry host, Mono and Xvfb")
    def test_reconnect_waits_for_current_zone_and_known_id_only_restarts_restore_it(self):
        from tests.test_triggernometry_connection import Host

        with ExitStack() as stack:
            root = Path(stack.enter_context(tempfile.TemporaryDirectory()))
            packs, runtime = root / "packs", root / "runtime"
            packs.mkdir()
            runtime.mkdir()
            document = PackDocument(packs / "zones.xml")
            probe = document.add_trigger()
            probe.set("Name", "Zone probe")
            probe.set("RegularExpression", r"PROBE (?<label>\w+)")
            ET.SubElement(probe.find("Actions"), "Action", OrderNumber="1", ActionType="UseTTS",
                          Asynchronous="False", UseTTSTextExpression="${label} zone ${_ffxivzoneid}")
            old_document = PackDocument(packs / "old-zone.xml")
            old_document.root.find("ExportedFolder").attrib.update(
                FFXIVZoneFilterEnabled="True", FfxivZoneFilterRegularExpression="^1363$")
            old = old_document.add_trigger()
            old.attrib.update(Name="Old duty", RegularExpression="OLD DUTY")
            ET.SubElement(old.find("Actions"), "Action", OrderNumber="1", ActionType="UseTTS",
                          Asynchronous="False", UseTTSTextExpression="Old duty callout")
            document.save()
            old_document.save()
            stack.enter_context(patch.object(triggernometry_bridge, "_rundata_dir", return_value=runtime))
            stack.enter_context(patch.object(triggernometry_bridge, "_packs_dir", return_value=packs))
            stack.enter_context(patch.object(triggernometry_bridge, "_make_bundled_mono_executable"))
            stack.enter_context(patch.object(triggernometry_bridge, "_log"))
            host = Host()
            speech = []
            stack.enter_context(patch("nyaatriggers.ui.engines.speak",
                                      side_effect=lambda text, **_kwargs: speech.append(text)))
            host._current_zone = "Dancing Mad"
            host._current_zone_id = 1363
            host._awaiting_zone_metadata = False
            host._mute_until_zone = False
            host._refresh_zone_column = lambda: None
            host._redetect_zone_fight = lambda: None
            host._ws.zone_changed.connect(host._on_ws_zone_changed)
            host._ws.status_changed.emit(True, "Connected")
            host._set_triggernometry_enabled(True)
            bridge = host._triggernometry
            stack.callback(lambda: bridge.stop(wait=True))

            def wait(predicate):
                end = time.monotonic() + 10
                while not predicate() and time.monotonic() < end:
                    QTest.qWait(10)
                self.assertTrue(predicate(), speech)

            def line(text):
                host._ws._on_message(json.dumps({"type": "LogLine", "rawLine":
                    f"00|2026-09-26T01:00:00-05:00|0038|Tester|{text}|checksum"}))

            def probe_zone(label, zone):
                line("PROBE " + label)
                wait(lambda: any(text.startswith(label + " zone ") for text in speech))
                self.assertIn(f"{label} zone {zone}", speech)

            wait(lambda: len(host.inventory) == 1)
            probe_zone("known", 1363)
            host._ws._on_disconnected()
            self.assertTrue(host._awaiting_zone_metadata)
            self.assertEqual(host._ws.state_snapshot(), ())
            host._ws.status_changed.emit(True, "Connected")
            wait(lambda: len(host.inventory) == 2)
            line("OLD DUTY")
            probe_zone("unknown", 0)
            self.assertNotIn("Old duty callout", speech)
            host._ws._on_message('{"type":"ChangeZone","zoneID":200}')
            probe_zone("confirmed", 200)
            self.assertEqual(host._current_zone, "")
            self.assertEqual(host._current_zone_id, 200)
            self.assertFalse(host._awaiting_zone_metadata)
            host._set_triggernometry_enabled(False)
            host._set_triggernometry_enabled(True)
            wait(lambda: len(host.inventory) == 3)
            probe_zone("idonly", 200)


class TriggernometryStartupTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "posix" and shutil.which("mono") and shutil.which("xvfb-run")
                         and triggernometry_bridge._find_exe(), "Requires the Triggernometry host, Mono and Xvfb")
    def test_saved_text_and_empty_edits_apply_before_first_startup_and_reconnect_events(self):
        from tests.test_triggernometry_connection import Host

        with ExitStack() as stack:
            root = Path(stack.enter_context(tempfile.TemporaryDirectory()))
            packs, runtime = root / "packs", root / "runtime"
            packs.mkdir()
            runtime.mkdir()
            document = PackDocument(packs / "startup.xml")
            edits, disabled = {}, set()
            for pattern, original, edited in ((r"PROBE (?<label>\w+)", "original ${label}", "edited ${label}"),
                                              ("MUTED", "unwanted call", ""),
                                              ("DISABLED", "disabled original", "disabled edit")):
                trigger = document.add_trigger()
                trigger.set("RegularExpression", pattern)
                ET.SubElement(trigger.find("Actions"), "Action", OrderNumber="1", ActionType="UseTTS",
                              Asynchronous="False", UseTTSTextExpression=original)
                callout_id = trigger.get("Id") + "#0"
                edits[callout_id] = edited
                if pattern.startswith("PROBE"):
                    ET.SubElement(trigger.find("Actions"), "Action", OrderNumber="2",
                                  ActionType="Placeholder", Asynchronous="False", ExecutionDelayExpression="600")
                    ET.SubElement(trigger.find("Actions"), "Action", OrderNumber="3", ActionType="UseTTS",
                                  Asynchronous="False", UseTTSTextExpression="late ${label}")
                elif pattern == "DISABLED":
                    disabled.add(callout_id)
            document.save()
            stack.enter_context(patch.object(triggernometry_bridge, "_rundata_dir", return_value=runtime))
            stack.enter_context(patch.object(triggernometry_bridge, "_packs_dir", return_value=packs))
            stack.enter_context(patch.object(triggernometry_bridge, "_make_bundled_mono_executable"))
            stack.enter_context(patch.object(triggernometry_bridge, "_log"))
            host = Host()
            host._connected = True
            host._triggernometry_disabled = disabled
            host._callout_edits_for = lambda _source: edits
            host._on_triggernometry_inventory = lambda *_args: host._replay_triggernometry_callout_edits()
            bridge = host._ensure_triggernometry_bridge()
            stack.callback(lambda: bridge.stop(wait=True))
            received = threading.Event()
            native, delivered = [], []

            def from_reader(text, _generation):
                native.append(text)
                if text.endswith(("first", "reconnect")):
                    received.set()

            bridge.tts.connect(from_reader, Qt.ConnectionType.DirectConnection)
            stack.enter_context(patch("nyaatriggers.ui.engines.speak",
                                      side_effect=lambda text, **_kwargs: delivered.append(text)))
            host._set_triggernometry_enabled(True)
            for label in ("first", "reconnect"):
                received.clear()
                for text in ("MUTED", "DISABLED", "PROBE " + label):
                    host._ws._on_message(json.dumps({"type": "LogLine", "rawLine":
                        f"00|2026-09-26T01:00:00-05:00|0038|Tester|{text}|checksum"}))
                self.assertTrue(received.wait(10), native)
                self.assertEqual(native[-1], "edited " + label)
                self.assertNotIn("unwanted call", native)
                self.assertFalse(any(text.startswith("disabled") for text in native))
                QTest.qWait(100)
                self.assertEqual(delivered[-1], "edited " + label)
                generation = bridge.generation()
                host._set_triggernometry_enabled(True)
                self.assertEqual(bridge.generation(), generation)
                end = time.monotonic() + 3
                while "late " + label not in delivered and time.monotonic() < end:
                    QTest.qWait(10)
                self.assertEqual(delivered.count("late " + label), 1)
                if label == "first":
                    host._ws._on_disconnected()
                    host._ws.status_changed.emit(True, "Connected")
            self.assertEqual(delivered, ["edited first", "late first", "edited reconnect", "late reconnect"])


class TriggernometryEditTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "posix" and shutil.which("mono") and shutil.which("xvfb-run")
                         and triggernometry_bridge._find_exe(), "Requires the Triggernometry host, Mono and Xvfb")
    def test_edit_and_reset_apply_before_settings_failure_opens_a_modal(self):
        from PyQt6.QtWidgets import QMessageBox
        from nyaatriggers import app_common as ac
        from tests.test_session_ui import SessionUiTests

        actual_warning = QMessageBox.warning
        SessionUiTests.setUpClass()
        fixture = SessionUiTests("test_window_icon_loads_from_bundled_assets")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        window = fixture.window
        packs, runtime = fixture.temp / "packs", fixture.temp / "runtime"
        packs.mkdir()
        runtime.mkdir()
        document = PackDocument(packs / "modal.xml")
        trigger = document.add_trigger()
        trigger.set("RegularExpression", r"PROBE (?<label>\w+)")
        ET.SubElement(trigger.find("Actions"), "Action", OrderNumber="1", ActionType="UseTTS",
                      Asynchronous="False", UseTTSTextExpression="original ${label}")
        document.save()
        cid = trigger.get("Id") + "#0"
        for name, value in (("_rundata_dir", runtime), ("_packs_dir", packs)):
            fixture.stack.enter_context(patch.object(triggernometry_bridge, name, return_value=value))
        fixture.stack.enter_context(patch.object(triggernometry_bridge, "_make_bundled_mono_executable"))
        fixture.stack.enter_context(patch.object(triggernometry_bridge, "_log"))
        bridge = triggernometry_bridge.TriggernometryBridge(window)
        window._triggernometry = bridge
        window._triggernometry_mode = True
        window._connected = True
        speech, inventory = [], []
        window._triggernometry_speak = speech.append
        bridge.tts.connect(window._on_triggernometry_tts)
        bridge.inventory.connect(lambda *args: inventory.append(args))
        bridge.start()
        deadline = time.monotonic() + 10
        while not inventory and time.monotonic() < deadline:
            QTest.qWait(10)
        self.assertTrue(inventory)
        for label, change, expected in (
                ("edited", lambda: window._set_triggernometry_callout_edit(cid, "edited ${label}"),
                 "edited edited"),
                ("reset", lambda: window._reset_triggernometry_callout_edit(cid), "original reset")):
            with self.subTest(operation=label):
                settings = ac._SETTINGS_FILE
                settings.unlink(missing_ok=True)
                settings.mkdir()
                window._save_warned = False
                observed, launched, begin = [], [], len(speech)
                timer = QTimer()
                deadline = time.monotonic() + 5

                def inside_warning():
                    modal = QApplication.activeModalWidget()
                    if not isinstance(modal, QMessageBox):
                        return
                    if not launched:
                        launched.append(True)
                        bridge.feed_log("00|2026-09-26T05:00:00-05:00|0038|Tester|PROBE "
                                        + label + "|checksum")
                    elif len(speech) > begin or time.monotonic() > deadline:
                        observed.extend(speech[begin:])
                        modal.accept()
                        timer.stop()

                timer.timeout.connect(inside_warning)
                try:
                    with patch.object(ac.QMessageBox, "warning", actual_warning):
                        timer.start(20)
                        change()
                finally:
                    timer.stop()
                    settings.rmdir()
                self.assertTrue(launched)
                self.assertEqual(observed, [expected])
                QTest.qWait(100)


class CaptureClockTests(unittest.TestCase):
    def test_clock_rollback_retains_the_latest_completed_captures_after_restart(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(pull_capture, "_KEEP_CAPTURES", 2), \
                patch.object(pull_capture, "datetime", wraps=datetime) as clock:
            root = Path(directory)
            for number, minute in enumerate((50, 51, 20, 21), 1):
                now = datetime(2026, 11, 1, 1, minute)
                clock.now.return_value = now
                capture = pull_capture.PullCapture(root)
                capture.context = lambda: ("Boss", "Arena")
                capture.set_recording(True)
                capture.on_raw_message(json.dumps({"pull": number}))
                capture.on_log_line("20|ts|40000001|Boss|1234|Cast|")
                capture.close()
                metadata = json.loads(capture._path.with_suffix(".meta.json").read_text())
                self.assertEqual(metadata["started"], now.isoformat(timespec="seconds"))
            files = sorted((root / "Boss").glob("*.jsonl"))
            self.assertEqual([json.loads(path.read_text())["pull"] for path in files], [3, 4])
            self.assertEqual(len(list((root / "Boss").glob("*.meta.json"))), 2)

    def test_repeated_wall_timestamp_never_overwrites_an_existing_capture(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(pull_capture, "datetime", wraps=datetime) as clock:
            clock.now.return_value = datetime(2026, 11, 1, 1, 20)
            root = Path(directory)
            for number in range(3):
                capture = pull_capture.PullCapture(root)
                capture.set_recording(True)
                capture.on_raw_message(json.dumps({"pull": number}))
                capture.on_log_line("20|ts|40000001|Boss|1234|Cast|")
                capture.close()
            files = sorted((root / "Unknown").glob("*.jsonl"))
            self.assertEqual([json.loads(path.read_text())["pull"] for path in files], [0, 1, 2])


    def test_exclusive_collision_preserves_the_other_capture_and_retries_next_event(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(pull_capture, "datetime", wraps=datetime) as clock, \
                patch.object(pull_capture.drop_log, "log_drop") as report:
            clock.now.return_value = datetime(2026, 11, 1, 1, 20)
            root = Path(directory)
            capture = pull_capture.PullCapture(root)
            self.addCleanup(capture.close)
            capture.set_recording(True)
            original_open = builtins.open
            collisions = []
            saved = '{"other_capture": true}\n'

            def raced_open(path, mode="r", *args, **kwargs):
                if mode == "x" and not collisions:
                    path.write_text(saved)
                    collisions.append(path)
                return original_open(path, mode, *args, **kwargs)

            first = "20|ts|40000001|Boss|1234|First|"
            second = "20|ts|40000001|Boss|1234|Second|"
            with patch("builtins.open", side_effect=raced_open):
                capture.on_raw_message(json.dumps({"type": "LogLine", "rawLine": first}))
                capture.on_log_line(first)
                self.assertFalse(capture._in_pull)
                self.assertEqual(collisions[0].read_text(), saved)
                capture.on_raw_message(json.dumps({"type": "LogLine", "rawLine": second}))
                capture.on_log_line(second)
                self.assertTrue(capture._in_pull)
                capture.close()
            self.assertEqual(collisions[0].read_text(), saved)
            self.assertNotEqual(capture._path, collisions[0])
            frames = [json.loads(line) for line in capture._path.read_text().splitlines()]
            self.assertEqual([frame["rawLine"] for frame in frames], [first, second])
            report.assert_called_once()


class CaptureWriteFailureTests(unittest.TestCase):
    def capture(self, root):
        capture = pull_capture.PullCapture(root)
        self.addCleanup(capture.close)
        capture.set_recording(True)
        for number in (1, 2):
            self.begin(capture, number)
            capture.on_in_combat(True, False)
        return capture

    @staticmethod
    def begin(capture, number):
        capture.on_raw_message(json.dumps({"pull": number}))
        capture.on_log_line("20|ts|40000001|Boss|1234|Cast|")

    def test_empty_write_failures_preserve_history_and_later_recording_recovers(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(pull_capture, "_KEEP_CAPTURES", 2), \
                patch.object(pull_capture.drop_log, "log_drop") as report:
            root = Path(directory)
            capture = self.capture(root)
            originals = {path: path.read_bytes() for path in root.rglob("*.jsonl")}
            original_open = builtins.open

            def failed_write(_line):
                raise OSError(errno.ENOSPC, "No space left on device")

            def failed_open(path, mode="r", *args, **kwargs):
                fh = original_open(path, mode, *args, **kwargs)
                if mode == "x":
                    fh.write = failed_write
                return fh

            with patch("builtins.open", side_effect=failed_open):
                for number in (3, 4, 5):
                    self.begin(capture, number)
                    self.assertFalse(capture._in_pull)
            self.assertEqual({path: path.read_bytes() for path in root.rglob("*.jsonl")}, originals)
            self.begin(capture, 6)
            capture.on_in_combat(True, False)
            frames = [json.loads(path.read_text()) for path in sorted(root.rglob("*.jsonl"))]
            self.assertEqual(frames, [{"pull": 2}, {"pull": 6}])
            report.assert_called_once()

    def test_partial_write_preserves_existing_history_and_the_partial_capture(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(pull_capture, "_KEEP_CAPTURES", 2), \
                patch.object(pull_capture.drop_log, "log_drop"):
            root = Path(directory)
            capture = self.capture(root)
            originals = {path: path.read_bytes() for path in root.rglob("*.jsonl")}
            self.begin(capture, 3)
            partial = capture._path
            original_write = capture._fh.write

            def partial_write(line):
                original_write(line[:8])
                raise OSError(errno.ENOSPC, "No space left on device")

            capture._fh.write = partial_write
            capture.on_raw_message('{"partial": true}')
            self.assertFalse(capture._in_pull)
            for path, contents in originals.items():
                self.assertEqual(path.read_bytes(), contents)
            self.assertEqual(partial.read_text(), '{"pull": 3}\n{"partia')
            self.assertEqual(json.loads(partial.with_suffix(".meta.json").read_text())["lines"], 1)
            self.begin(capture, 4)
            capture.on_in_combat(True, False)
            self.assertEqual(list(sorted(root.rglob("*.jsonl"))), [partial, capture._path])
            self.assertEqual(json.loads(capture._path.read_text()), {"pull": 4})


class ConnectingRetargetTests(unittest.TestCase):
    def test_new_url_replaces_a_handshake_that_never_completed(self):
        from PyQt6.QtNetwork import QAbstractSocket
        from tests.test_connection_recovery import FeedPeer, wait_for

        class NoHandshakePeer(FeedPeer):
            def handle(self, _connection):
                self.stop.wait(10)

        stalled, healthy = NoHandshakePeer(), FeedPeer()
        self.addCleanup(stalled.close)
        self.addCleanup(healthy.close)
        client = WSClient()
        self.addCleanup(client.disconnect_from)
        states = []
        client.status_changed.connect(lambda connected, _message: states.append(connected))
        client.connect_to(stalled.url)
        self.assertTrue(wait_for(lambda: stalled.connections == 1))
        self.assertEqual(client._ws.state(), QAbstractSocket.SocketState.ConnectingState)
        client.connect_to(healthy.url)
        self.assertTrue(wait_for(lambda: client._ws.isValid(), timeout=1),
                        (client._ws.state(), client._reconnect_timer.isActive()))
        self.assertTrue(wait_for(lambda: bool(healthy.messages)))
        self.assertEqual(healthy.messages[0]["call"], "subscribe")
        self.assertEqual(healthy.connections, 1)
        self.assertFalse(client._reopen_on_disconnect)
        self.assertFalse(client._reconnect_timer.isActive())
        self.assertEqual(states[-1], True)
        client.disconnect_from()
        self.assertEqual(states[-1], False)
        self.assertFalse(client._auto_reconnect)


class CactbotEndpointTests(unittest.TestCase):
    def setUp(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        from PyQt6 import QtCore
        from PyQt6.QtWidgets import QLineEdit
        from nyaatriggers.cactbot_reader import CactbotReader
        from nyaatriggers.ui.connection import ConnectionMixin
        from nyaatriggers.ui.engines import EnginesMixin

        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.pages = []

        def page(*_args):
            result = Mock()
            self.pages.append(result)
            return result

        engine = SimpleNamespace(QWebEnginePage=page, QWebEngineProfile=Mock(),
                                 QWebEngineScript=Mock(), QWebEngineSettings=Mock())
        self.stack.enter_context(patch.dict(sys.modules, {
            "PyQt6.QtWebEngineCore": engine,
            "PyQt6.QtWebChannel": SimpleNamespace(QWebChannel=Mock())}))
        file = self.stack.enter_context(patch.object(QtCore, "QFile"))
        file.return_value.readAll.return_value = b""

        class Host(ConnectionMixin, EnginesMixin):
            def _ensure_cactbot_reader(self):
                return self._cactbot_reader

        self.host = host = Host()
        host._connected = False
        host._settings = {}
        host._url_edit = QLineEdit("ws://localhost:10501/ws")
        host._cactbot_mode = False
        host._cactbot_teardown = False
        host._cactbot_disabled = {"muted"}
        host._cactbot_reader = CactbotReader()
        self.addCleanup(host._cactbot_reader.stop)
        host._cactbot_reader.status.connect(host._on_cactbot_status)
        host._ws = Mock()
        host._save_settings = Mock()
        host._set_cactbot_button = Mock()
        host._set_triggers_enabled = Mock()
        host._load_timeline_for_zone = Mock()
        host._match_zone = ""
        host._set_cactbot_enabled(True)

    def test_connect_moves_enabled_reader_and_rejects_old_page_output(self):
        host = self.host
        reader = host._cactbot_reader
        old_bridge = reader._bridge
        speech = []
        reader.tts.connect(speech.append)
        old_bridge.relay.emit("say", '{"text":"Original feed"}')
        host._url_edit.setText("ws://localhost:10502/ws")
        host._toggle_connection()
        self.assertEqual(len(self.pages), 2)
        self.assertEqual(reader.websocket_url(), "ws://localhost:10502/ws")
        self.assertTrue(host._cactbot_mode)
        self.assertEqual(reader._disabled, {"muted"})
        old_bridge.relay.emit("say", '{"text":"Old feed leak"}')
        reader._bridge.relay.emit("say", '{"text":"New feed"}')
        self.assertEqual(speech, ["Original feed", "New feed"])
        host._ws.connect_to.assert_called_once_with("ws://localhost:10502/ws")
        host._set_triggers_enabled.assert_not_called()

    def test_connect_retires_old_reader_before_settings_warning(self):
        from PyQt6 import sip
        from PyQt6.QtWidgets import QMessageBox

        host = self.host
        reader = host._cactbot_reader
        old_bridge = reader._bridge
        speech, observed = [], []
        reader.tts.connect(speech.append)
        host._url_edit.setText("ws://localhost:10502/ws")
        host._save_settings.reset_mock()

        def inside_warning():
            dialog = QApplication.activeModalWidget()
            if isinstance(dialog, QMessageBox):
                if not sip.isdeleted(old_bridge):
                    old_bridge.relay.emit("say", '{"text":"Old feed leak"}')
                reader._bridge.relay.emit("say", '{"text":"Selected feed"}')
                observed.append((reader.websocket_url(), host._ws.connect_to.call_count))
                dialog.accept()

        def save():
            QTimer.singleShot(0, inside_warning)
            QMessageBox.warning(None, "Save Failed", "Cannot save settings")

        host._save_settings.side_effect = save
        host._toggle_connection()
        self.assertEqual(observed, [("ws://localhost:10502/ws", 1)])
        self.assertEqual(speech, ["Selected feed"])
        host._save_settings.assert_called_once()

    def test_manual_disconnect_and_same_normalized_endpoint_keep_the_page(self):
        host = self.host
        reader = host._cactbot_reader
        page = reader._page
        host._connected = True
        host._toggle_connection()
        host._ws.disconnect_from.assert_called_once()
        self.assertIs(reader._page, page)
        host._connected = False
        host._url_edit.setText("  WS://LOCALHOST:10501/ws  ")
        host._toggle_connection()
        self.assertEqual(len(self.pages), 1)
        self.assertIs(reader._page, page)
        self.assertTrue(reader.is_active())

    def test_reader_stays_off_when_only_main_feed_endpoint_changes(self):
        host = self.host
        host._set_cactbot_enabled(False)
        host._url_edit.setText("ws://localhost:10502/ws")
        host._toggle_connection()
        self.assertFalse(host._cactbot_reader.is_active())
        self.assertEqual(host._cactbot_reader.websocket_url(), "")
        self.assertEqual(len(self.pages), 1)

    def test_failed_reader_retarget_restores_local_callouts(self):
        host = self.host
        host._url_edit.setText("ws://localhost:10502/ws")
        with patch.object(host._cactbot_reader, "start", side_effect=RuntimeError("load failed")):
            host._toggle_connection()
        self.assertFalse(host._cactbot_mode)
        self.assertFalse(host._cactbot_reader.is_active())
        host._set_triggers_enabled.assert_called_once_with(True)
        host._ws.connect_to.assert_called_once_with("ws://localhost:10502/ws")


if __name__ == "__main__":
    unittest.main()
