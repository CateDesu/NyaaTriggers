from contextlib import ExitStack
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt6.QtCore import QObject
from PyQt6.QtWidgets import QApplication

from nyaatriggers import app_common as ac
from nyaatriggers.timeline_engine import TimelineEngine
from nyaatriggers.triggevent_bridge import TriggeventBridge
from nyaatriggers.triggevent_recovery import TriggeventRecovery
from nyaatriggers.ui.engines import EnginesMixin
from nyaatriggers.ui.timeline_tab import TimelineTabMixin
from nyaatriggers.ws_client import WSClient

APP = QApplication.instance() or QApplication([])


class TimelineHost(EnginesMixin, TimelineTabMixin):
    def __init__(self):
        self._timeline = TimelineEngine()
        self._timeline.tts.connect(self._on_timeline_tts)
        self._cactbot_mode = False
        self._current_zone_id = 1
        self._match_zone = "Arena"
        self._local_enabled = self._global_local_on_flag = True
        self.schedules, self.calls, self.fetches = [], [], []
        self._plugin_link = SimpleNamespace(send_timeline=self.schedules.append)

    def _fight_tag_for_zone(self, zone):
        return "Local", zone

    def _fetch_cactbot_timeline(self, tag, relative):
        self.fetches.append((tag, relative))

    def _emit_guest_callout(self, text, severity):
        self.calls.append((text, severity))


class TimelineContracts(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        index = self.root / "index.json"
        index.write_text(json.dumps({"1": {"tag": "Indexed", "txt_path": "indexed.txt"}}))
        self.stack.enter_context(patch.multiple(ac, TIMELINES_DIR=self.root,
                                               _BUNDLE_TIMELINES_DIR=self.root,
                                               CACTBOT_TIMELINES_FILE=index,
                                               _cactbot_tl_cache=None))
        self.stack.enter_context(patch("nyaatriggers.ui.timeline_tab.FIGHT_TO_CACTBOT_TXT",
                                      {"Local": "fallback.txt"}))
        (self.root / "Local.txt").write_text('1 "Local warning"')
        self.host = TimelineHost()
        self.addCleanup(self.host._timeline.reset)

    def test_cactbot_switch_alone_gates_index_and_fallback_files(self):
        for zone, tag in ((1, "Indexed"), (2, "Local")):
            for suffix in (".cactbot.cache.txt", ".cactbot.txt"):
                with self.subTest(zone=zone, suffix=suffix):
                    path = self.root / (tag + suffix)
                    path.write_text('1 "Cactbot warning"')
                    self.host._current_zone_id = zone
                    for enabled, expected in ((False, "Local warning"), (True, "Cactbot warning"),
                                              (False, "Local warning")):
                        self.host._cactbot_mode = enabled
                        self.host._load_timeline_for_zone("Arena")
                        self.assertEqual(self.host._timeline.upcoming(), [(1, expected)])
                        self.assertEqual(self.host.schedules[-1], [(1, expected)])
                        self.assertEqual(self.host._timeline_from_cactbot, enabled)
                    self.assertEqual(self.host.fetches, [])
                    path.unlink()

    def test_cactbot_switch_alone_gates_missing_timeline_downloads(self):
        for zone, tag, relative in ((1, "Indexed", "indexed.txt"), (2, "Local", "fallback.txt")):
            with self.subTest(zone=zone):
                self.host._current_zone_id = zone
                self.host.fetches.clear()
                self.host._cactbot_mode = False
                self.host._load_timeline_for_zone("Arena")
                self.assertEqual(self.host.fetches, [])
                self.host._cactbot_mode = True
                self.host._load_timeline_for_zone("Arena")
                self.assertEqual(self.host.fetches, [(tag, relative)])
                self.assertEqual(self.host._timeline.upcoming(), [(1, "Local warning")])

    def test_late_cactbot_download_cannot_replace_or_double_speak_local_timeline(self):
        now = [100.0]
        timed_calls = []
        self.host._timeline.tts.connect(timed_calls.append)
        self.host._current_zone_id = 2
        with patch("nyaatriggers.timeline_engine._time.monotonic", side_effect=lambda: now[0]):
            self.host._load_timeline_for_zone("Arena")
            self.host._timeline.start()
            now[0] += 0.5
            (self.root / "Local.cactbot.cache.txt").write_text('1 "Cactbot warning"')
            schedules = list(self.host.schedules)
            self.host._on_cactbot_timeline_ready("Local")
            self.assertEqual(self.host.schedules, schedules)
            self.assertEqual(self.host._timeline.current_time(), 0.5)
            now[0] += 0.5
            self.host._timeline._tick()
            self.assertEqual(self.host.calls, [("Local warning", "info")])
            self.host._cactbot_mode = True
            self.host._load_timeline_for_zone("Arena")
            self.host._timeline.start()
            now[0] += 1
            self.host._timeline._tick()
            self.assertEqual(timed_calls, ["Local warning", "Cactbot warning"])
            self.assertEqual(self.host.calls, [("Local warning", "info")])


class ModeHost(QObject, EnginesMixin):
    def __init__(self, bridge):
        super().__init__()
        self._triggevent = bridge
        self._triggevent_mode = True
        self._update_automark_status_label = lambda: None


class TriggeventContracts(unittest.TestCase):
    def setUp(self):
        self.bridge = TriggeventBridge()
        # Simulate negotiated capabilities without depending on a locally built jar.
        self.bridge._active = True
        self.bridge._gen = self.bridge._recovery_gen = self.bridge._catchup_gen = 1
        self.ws = WSClient()
        self.recovery = TriggeventRecovery(self.bridge, self.ws, lambda: None)
        self.addCleanup(self.recovery._progress_timer.stop)
        self.bridge.status.emit(True, "Starting", 1)

    def queued(self):
        rows = []
        while not self.bridge._wq.empty():
            rows.extend(json.loads(line) for line in self.bridge._wq.get_nowait().splitlines())
        return rows

    def test_state_seed_cannot_overwrite_buffered_transitions_or_deduplicate_events(self):
        player = {"type": "ChangePrimaryPlayer", "charID": 0x10000001, "charName": "Player"}
        zone = {"type": "ChangeZone", "zoneID": 1363, "zoneName": "Arena"}
        initial = {"type": "InCombat", "inACTCombat": False, "inGameCombat": False}
        for event in (zone, player, initial):
            self.ws._on_message(json.dumps(event))
        self.bridge._gen = self.bridge._recovery_gen = self.bridge._catchup_gen = 2
        self.bridge.status.emit(True, "Starting", 2)
        line = {"type": "LogLine", "rawLine": '00|2026-10-04T12:00:00Z|0038|Player|日本語 "nyaa_cmd"|'}
        active = {**initial, "inACTCombat": True, "inGameCombat": True}
        transitions = [active, line, line, initial, active]
        for event in transitions:
            self.ws._on_message(json.dumps(event))
        self.bridge.ready.emit(2)
        rows = self.queued()
        self.assertEqual(rows[0]["nyaa_cmd"], "recover_begin")
        self.assertEqual(rows[1:-1], [player, zone, initial, *transitions])
        self.assertEqual(rows[-1], {"nyaa_cmd": "recover_checkpoint", "checkpoint": 1})
        self.assertFalse(self.recovery._live)
        self.bridge._dispatch({"t": "recovery_checkpoint", "checkpoint": 1}, gen=2)
        self.assertEqual(self.queued(), [{"nyaa_cmd": "recover_end", "checkpoint": 2}])
        self.assertFalse(self.recovery._live)
        for generation, checkpoint in ((1, 2), (2, 1), (2, 3), (2, None)):
            with self.subTest(generation=generation, checkpoint=checkpoint):
                self.bridge._dispatch({"t": "recovered", "checkpoint": checkpoint}, gen=generation)
                self.assertFalse(self.recovery._live)
                self.assertTrue(self.recovery._ending)
                self.assertEqual(self.queued(), [])
        self.bridge._dispatch({"t": "recovered", "checkpoint": 2}, gen=2)
        self.assertTrue(self.recovery._live)
        self.ws._on_message(json.dumps(line))
        self.assertEqual(self.queued(), [line])

    def test_callout_mode_changes_preserve_engine_generation_and_pending_recovery(self):
        host = ModeHost(self.bridge)
        self.bridge._speech_cancel_all_gen = 1
        self.recovery._finish(1, None, "")
        self.queued()
        buffered = {"type": "LogLine", "rawLine": "00|2026-10-04T12:00:00Z|0038|Player|During recovery|"}
        self.ws._on_message(json.dumps(buffered))
        with patch.object(TriggeventBridge, "is_available", return_value=True), \
                patch("nyaatriggers.triggevent_bridge._find_java", return_value=None), \
                patch("nyaatriggers.triggevent_bridge._find_jar", return_value=None):
            for enabled in (False, False, True, True):
                host._set_triggevent_enabled(enabled)
                self.assertTrue(self.bridge.is_active())
                self.assertEqual(self.bridge.generation(), 1)
                self.assertEqual(self.recovery._pending, [json.dumps(buffered)])
                self.assertTrue(self.recovery._loading)
                self.assertFalse(self.recovery._live)
        commands = self.queued()
        self.assertEqual([row["nyaa_cmd"] for row in commands], ["cancel_speech", "cancel_speech"])
        self.assertTrue(all(row["all"] for row in commands))
        self.assertNotEqual(commands[0]["token"], commands[1]["token"])
        self.bridge._dispatch({"t": "recovery_checkpoint", "checkpoint": 1}, gen=1)
        self.assertEqual(self.queued(), [buffered, {"nyaa_cmd": "recover_checkpoint", "checkpoint": 2}])
        self.bridge._dispatch({"t": "recovery_checkpoint", "checkpoint": 2}, gen=1)
        self.assertEqual(self.queued(), [{"nyaa_cmd": "recover_end", "checkpoint": 3}])
        self.bridge._dispatch({"t": "recovered", "checkpoint": 3}, gen=1)
        self.assertTrue(self.recovery._live)


if __name__ == "__main__":
    unittest.main()
