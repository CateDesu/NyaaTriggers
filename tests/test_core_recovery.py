"""Regression checks for compilation, timeline refresh and recorded state."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt6.QtCore import QCoreApplication

from nyaatriggers import app_common as ac
from nyaatriggers import pull_capture
from nyaatriggers import timeline_engine
from nyaatriggers.timeline_parser import parse
from nyaatriggers.ui.timeline_tab import TimelineTabMixin
from nyaatriggers.ws_client import WSClient

APP = QCoreApplication.instance() or QCoreApplication([])


class RegexLimits(unittest.TestCase):
    def test_expensive_compilation_and_dialog_diagnostics_are_bounded(self):
        script = r'''
import sys
if sys.platform != "win32":
    import resource
    resource.setrlimit(resource.RLIMIT_AS, (384 * 1024**2, 384 * 1024**2))
from nyaatriggers.trigger_engine import compile_user_regex
from nyaatriggers.trigger_dialog import _regex_syntax_error
patterns = ["a{1000000}", "(?:ab){1000000000}", "a{1,1000000}",
            "a{1000000,}", "a{,1000000}", "(?:a{100}){100}",
            "(?x)a{1 000 000}", "(?x)a{1# split\n000000}",
            r"(?x)\#a{1 000 000}", "(?x)[#]a{1# }\n000000}",
            "#(?x)a{1# split\n000000}"]
for pattern in patterns:
    assert compile_user_regex(pattern) is None, pattern
    assert not _regex_syntax_error(pattern), pattern
assert _regex_syntax_error("(")
assert compile_user_regex(r"\d{4}-\d{2}-\d{2}") is not None
assert compile_user_regex(r"\{1000000\}") is not None
assert compile_user_regex("Old") is compile_user_regex("Old")
assert compile_user_regex("New").fullmatch("New")
print("bounded compilation and diagnostics passed")
'''
        result = subprocess.run([sys.executable, "-c", script], capture_output=True,
                                text=True, timeout=8)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class TimelineRecovery(unittest.TestCase):
    def setUp(self):
        self.clock = [100.0]
        self.clock_patch = patch.object(
            timeline_engine, "_time", SimpleNamespace(monotonic=lambda: self.clock[0]))
        self.clock_patch.start()
        self.addCleanup(self.clock_patch.stop)
        self.engine = timeline_engine.TimelineEngine()
        self.addCleanup(self.engine.reset)
        self.spoken = []
        self.engine.tts.connect(self.spoken.append)

    def test_refresh_preserves_clock_and_fired_entries_after_reordering(self):
        entries = parse('10 "Past"\n95 "Early sync" StartsUsing { id: "ABCD" } window 10,10\n120 "Future"')
        self.engine.load(entries)
        self.engine.start()
        self.clock[0] += 90
        self.engine._tick()
        self.engine._fire(1, entries[1])
        origin = self.engine._t0
        refreshed = parse('5 "New past"\n10 "Past"\n95 "Early sync" StartsUsing { id: "ABCD" } window 10,10\n110 "New future"\n120 "Future"')
        self.engine.load(refreshed, preserve_time=True)
        self.assertTrue(self.engine.is_active())
        self.assertEqual(self.engine._t0, origin)
        self.clock[0] += 31
        self.engine._tick()
        self.assertEqual(self.spoken, ["Past", "Early sync", "New future", "Future"])
        self.engine.process_line(["33", "", "0", "4000000F"])
        self.assertFalse(self.engine.is_active())
        self.engine.process_line(["260", "", "1", "1"])
        self.assertEqual(self.engine.current_time(), 0)

    def test_refresh_callback_keeps_active_clock_and_rejects_bad_replacement(self):
        class Window(TimelineTabMixin):
            def _fight_tag_for_zone(self, zone):
                return "Audit", "Zone"

            def _cactbot_zone_entry(self):
                return ("Audit", "audit.txt") if self._cactbot_mode else ()

            def _fetch_cactbot_timeline(self, *args):
                raise AssertionError("Fresh fixture must not fetch")

        window = Window()
        window._timeline = self.engine
        window._cactbot_mode = True
        window._match_zone = "Zone"
        pushes = []
        window._plugin_link = SimpleNamespace(send_timeline=pushes.append)
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            path = folder / "Audit.cactbot.cache.txt"
            path.write_text('10 "Past"\n120 "Future"')
            with patch.object(ac, "TIMELINES_DIR", folder), patch.object(ac, "_BUNDLE_TIMELINES_DIR", folder):
                window._load_timeline_for_zone("Zone")
                self.engine.start()
                self.clock[0] += 90
                window._on_cactbot_timeline_ready("Other")
                self.assertEqual(len(pushes), 1)
                path.write_text('10 "Past"\n120 "Changed future"')
                window._on_cactbot_timeline_ready("Audit")
                self.assertTrue(self.engine.is_active())
                self.assertEqual(self.engine.current_time(), 90)
                self.assertEqual(self.engine.upcoming()[-1], (120, "Changed future"))
                window._cactbot_mode = False
                window._on_cactbot_timeline_ready("Audit")
                self.assertEqual(len(pushes), 2)
                window._cactbot_mode = True
                for invalid in ("invalid", ""):
                    path.write_text(invalid)
                    window._on_cactbot_timeline_ready("Audit")
                    self.assertEqual(self.engine.current_time(), 90)
                    self.assertTrue(self.engine.is_active())
                path.unlink()
                window._fetch_cactbot_timeline = lambda *args: None
                window._on_cactbot_timeline_ready("Audit")
                self.assertEqual(self.engine.current_time(), 90)
                path.write_text('20 "Next pull"')
                self.engine.reset()
                window._on_cactbot_timeline_ready("Audit")
                self.assertFalse(self.engine.is_active())
                self.assertEqual(self.engine.upcoming(), [(20, "Next pull")])

    def test_unusable_arrays_never_jump_on_an_unrelated_cast(self):
        for body in ('[]', '[1234]', '["1234", 5678]', '["1234" "BEEF"]', '[,"BEEF"]',
                     '[["BEEF"]]', '[[]]', '["BEEF"'):
            with self.subTest(body=body):
                self.engine.load(parse(f'10 "Phase" StartsUsing {{ id: {body} }} jump 100'))
                self.engine.start()
                self.clock[0] += 10
                self.engine.process_line(["20", "", "40000001", "Boss", "BEEF"])
                self.assertEqual(self.engine.current_time(), 10)
        self.engine.load(parse('10 "Phase" StartsUsing { id: ["BEEF", "ABCD",] } jump 100'))
        self.engine.start()
        self.clock[0] += 10
        self.engine.process_line(["20", "", "40000001", "Boss", "BEEF"])
        self.assertEqual(self.engine.current_time(), 100)

    def test_quoted_field_text_cannot_be_rewritten_or_invent_keys(self):
        entry = parse('10 "x" GameLog { line: "id: [\'ABCD\']" }')[0]
        self.assertEqual(entry.event_fields, {"line": "id: ['ABCD']"})
        entry = parse('10 "x" GameLog { line: ["id: 1234", "a]b}c"] }')[0]
        self.assertEqual(entry.event_fields, {"line": "(?:id: 1234|a]b}c)"})


class CaptureRecovery(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.folder = Path(self.directory.name)
        self.clock = [100.0]
        self.clock_patch = patch.object(
            pull_capture, "time", SimpleNamespace(monotonic=lambda: self.clock[0]))
        self.clock_patch.start()
        self.addCleanup(self.clock_patch.stop)
        self.client = WSClient()
        self.capture = pull_capture.PullCapture(
            self.folder, state_snapshot=self.client.state_snapshot)
        self.capture.context = lambda: ("M1S", "Zone")
        self.client.raw_message.connect(self.capture.on_raw_message)
        self.client.log_line.connect(self.capture.on_log_line)
        self.client.zone_changed.connect(self.capture.on_zone_changed)
        self.client.status_changed.connect(self.capture.on_status_changed)
        self.client.in_combat.connect(self.capture.on_in_combat)
        self.addCleanup(self.capture.close)

    def send(self, **data):
        self.client._on_message(json.dumps(data))

    def state(self):
        self.send(type="ChangeZone", zoneID=1226, zoneName="Zone")
        self.send(type="ChangePrimaryPlayer", charID=0x10000001, charName="Player")
        self.send(type="PartyChanged", party=[{"id": "10000001", "job": 24}])
        self.send(type="InCombat", inACTCombat=False, inGameCombat=False)

    def cast(self):
        fields = ["20", "2026-09-14T12:00:00.000-05:00", "40000001", "Black Cat",
                  "9494", "Bloody Scratch", "10000001", "Player", "5.0", "100", "100", "0", "0", ""]
        self.send(type="LogLine", line=fields, rawLine="|".join(fields))

    def read_capture(self):
        files = sorted(self.folder.rglob("*.jsonl"))
        return [json.loads(line) for line in files[-1].read_text().splitlines()]

    def test_state_survives_age_and_recording_enabled_after_subscription(self):
        self.state()
        self.clock[0] += 31
        self.capture.set_recording(True)
        self.cast()
        self.capture.close()
        types = [frame["type"] for frame in self.read_capture()]
        self.assertEqual(types, ["ChangePrimaryPlayer", "ChangeZone", "PartyChanged", "InCombat", "LogLine"])
        self.capture.set_recording(True)
        self.clock[0] += 31
        self.cast()
        self.capture.close()
        self.assertEqual([frame["type"] for frame in self.read_capture()], types)
        self.assertEqual(len(list(self.folder.rglob("*.jsonl"))), 2)

    def test_snapshot_is_not_undone_by_older_buffered_roster(self):
        self.capture.set_recording(True)
        self.state()
        self.send(type="PartyChanged", party=[{"id": "10000002", "job": 24}])
        self.cast()
        self.capture.close()
        rosters = [frame["party"] for frame in self.read_capture() if frame["type"] == "PartyChanged"]
        self.assertEqual(rosters, [[{"id": "10000002", "job": 24}]])

    def test_disconnect_and_zone_change_discard_old_prepull_lines(self):
        self.capture.set_recording(True)
        self.state()
        self.send(type="LogLine", rawLine="26|old zone status")
        self.client._on_disconnected()
        self.send(type="ChangeZone", zoneID=1228, zoneName="Other")
        self.cast()
        self.capture.close()
        frames = self.read_capture()
        self.assertEqual([frame["type"] for frame in frames], ["ChangeZone", "LogLine"])
        self.assertEqual(frames[0]["zoneID"], 1228)
        self.capture.set_recording(True)
        self.send(type="LogLine", rawLine="26|another stale status")
        self.send(type="ChangeZone", zoneID=1226, zoneName="Zone")
        self.cast()
        self.capture.close()
        self.assertEqual([frame["type"] for frame in self.read_capture()], ["ChangeZone", "LogLine"])


if __name__ == "__main__":
    unittest.main()
