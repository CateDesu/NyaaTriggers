"""Callouts and saved choices across refreshes, toggles and duty changes."""

from contextlib import ExitStack
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from tests.test_callout_review_fixes import Host
from tests.test_data_safety import TriggerHost
from nyaatriggers import app_common as ac, tts
from nyaatriggers.main_window import MainWindow
from nyaatriggers.status_timer import StatusTimerRunner
from nyaatriggers.timeline_engine import TimelineEngine
from nyaatriggers.timeline_parser import parse
from nyaatriggers.trigger_dialog import TriggerDialog
from nyaatriggers.trigger_engine import Trigger
from nyaatriggers.ui.automarkers_tab import AutomarkersTabMixin
from nyaatriggers.ui.connection import ConnectionMixin
from nyaatriggers.ui.timeline_tab import TimelineTabMixin


class CalloutBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.host = Host([1000.0])
        self.host._fire = Mock()
        self.host._norm_hex = AutomarkersTabMixin._norm_hex
        self.host._automark_pairs.tracked = frozenset()
        self.host._umad_chain_line = lambda fields: None
        self.host._umad_gaze_line = lambda fields: None
        self.addCleanup(self.host._clear_status_timers)
        self.addCleanup(self.host._clear_seq_runners)

    def gain(self, duration="30", count="1", target="10000001"):
        fields = ["26", "ts", "ABC", "Status", duration,
                  "40000001", "Boss", target, "Player", count]
        self.host._dispatch_log_line(fields, "|".join(fields))

    def status_trigger(self, **kwargs):
        trigger = Trigger(log_type="26", ability_id="ABC", expiry_warn_s=5,
                          cooldown_s=0, **kwargs)
        self.host._triggers = [trigger]
        return trigger

    def test_disabled_status_warning_stays_cancelled_after_reenable(self):
        trigger = self.status_trigger()
        self.gain()
        runner, = self.host._status_timers
        self.host._set_trigger_enabled(trigger, False)
        self.host._set_trigger_enabled(trigger, True)
        self.assertFalse(self.host._status_timers)
        runner._fire()
        self.host._fire.assert_not_called()

    def test_filtered_status_refresh_cancels_the_previous_warning(self):
        for limits, duration, count in (({"duration_max": 40}, "60", "1"),
                                        ({"count_max": 1}, "30", "2"),
                                        ({}, "unknown", "1")):
            with self.subTest(limits=limits, duration=duration, count=count):
                self.host._clear_status_timers()
                self.status_trigger(**limits)
                self.gain()
                self.assertEqual(len(self.host._status_timers), 1)
                self.gain(duration, count)
                self.assertFalse(self.host._status_timers)
                self.host._fire.assert_not_called()

    def test_other_targets_do_not_cancel_a_status_warning(self):
        self.status_trigger(duration_max=40)
        self.gain()
        runner, = self.host._status_timers
        self.gain("60", target="10000002")
        self.assertEqual(self.host._status_timers, [runner])

    def test_reentering_the_same_zone_cancels_old_callouts(self):
        self.host.prepare_zone()
        self.status_trigger()
        self.gain()
        self.host._triggers.append(Trigger(ability_id="1234", delay_s=10))
        self.host.dispatch("1234", kind="20")
        self.assertEqual(len(self.host._seq_runners), 1)
        self.host.raw_zone("Old Arena", "64")
        self.assertFalse(self.host._status_timers)
        self.assertFalse(self.host._seq_runners)

    def test_repeated_zone_metadata_keeps_current_warnings(self):
        self.host.prepare_zone()
        self.status_trigger()
        self.gain()
        runner, = self.host._status_timers
        self.host._apply_zone("Old Arena", 100)
        self.assertEqual(self.host._status_timers, [runner])

    def test_primary_player_metadata_replaces_the_automarker_identity(self):
        host = SimpleNamespace(_me_id="10000001", _me_name="Player", _dps_meter=Mock())
        ConnectionMixin._on_ws_primary_player(host, 0x10000002, "Player")
        self.assertTrue(AutomarkersTabMixin._is_me_actor(host, "10000002", "Player"))
        self.assertFalse(AutomarkersTabMixin._is_me_actor(host, "10000001", "Player"))

    def test_local_controls_stop_local_timelines_and_preserve_cactbot_timelines(self):
        for cactbot in (False, True):
            for action in ("_toggle_global_local", "_reset_all_to_default"):
                with self.subTest(cactbot=cactbot, action=action):
                    host = self.host
                    host._cactbot_mode = cactbot
                    host._global_local_on_flag = True
                    host._official_ids = set()
                    host._engine_inventory = []
                    host._src_collapsed = {}
                    host._refresh_table = lambda: None
                    host._update_fight_controls = lambda: None
                    host._timeline = TimelineEngine(host)
                    host._timeline.load(parse('30 "Raidwide"'))
                    host._timeline.start()
                    self.addCleanup(host._timeline.reset)
                    host.frames.clear()
                    getattr(host, action)()
                    self.assertEqual(host._timeline.is_active(), cactbot)
                    if cactbot:
                        self.assertTrue(all(frame["c"] != "clear" for frame in host.frames))
                    else:
                        self.assertEqual(host.frames[-1]["c"], "clear")

    def test_status_warning_waits_for_its_deadline_and_fires_once(self):
        now = [100.0]
        done = Mock()
        with patch("time.monotonic", side_effect=lambda: now[0]):
            runner = StatusTimerRunner(Trigger(), {}, "A", "B", "C", 10000, done)
            self.addCleanup(runner.cancel)
            now[0] = 109.9
            runner._fire()
            done.assert_not_called()
            now[0] = 110.0
            runner._fire()
            runner._fire()
            done.assert_called_once()

    def test_guest_callouts_do_not_survive_empty_wipes_or_zone_boundaries(self):
        from PyQt6.QtTest import QTest

        host = self.host
        del host._clear_callout_dedup
        host._triggers_enabled = True
        host._localize_text = lambda text: text
        host._pending_guests = {}
        host._callout_claimed = {}
        host._guest_claim_sev = {}
        host._flush_guest = MainWindow._flush_guest.__get__(host)
        host._flush_guest_deferred = MainWindow._flush_guest_deferred.__get__(host)
        self.addCleanup(host._clear_callout_dedup)
        host._status_lbl = Mock()
        host._conn_btn = Mock()
        for boundary in ("zone", "same_zone", "wipe", "disconnect", "metadata"):
            with self.subTest(boundary=boundary), patch("nyaatriggers.main_window.speak") as speak:
                host.prepare_zone()
                host._clear_callout_dedup()
                host._emit_guest_callout("Move away")
                self.assertTrue(host._pending_guests)
                if boundary == "zone":
                    host.raw_zone()
                elif boundary == "same_zone":
                    host.raw_zone("Old Arena", "64")
                elif boundary == "wipe":
                    host.wipe()
                elif boundary == "disconnect":
                    host._on_status_changed(False, "Disconnected")
                else:
                    host._apply_zone("Old Arena", 100)
                QTest.qWait(ac._GUEST_CALLOUT_DEFER_MS + 60)
                self.assertEqual(speak.call_count, int(boundary == "metadata"))


class TriggerFileBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        for name in ("TRIGGERS_FILE", "TRIGGERS_LOCAL_FILE", "RETIRED_FILE",
                     "_REPO_TRIGGERS_FILE", "_REPO_RETIRED_FILE", "_REPO_TRIGGERS_VERSION"):
            self.stack.enter_context(patch.object(ac, name, root / (name + ".json")))
        self.stack.enter_context(patch.object(ac.QMessageBox, "warning"))
        ac.TRIGGERS_FILE.write_text('[{"id": "official", "name": "Official"}]')

    def test_invalid_local_shape_is_preserved_on_load_and_save(self):
        for data in ([{"id": "custom"}], {"triggers": {"custom": {"name": "Keep me"}}}):
            with self.subTest(data=data):
                content = json.dumps(data)
                ac.TRIGGERS_LOCAL_FILE.write_text(content)
                host = TriggerHost()
                host._load_triggers()
                self.assertTrue(host._local_corrupt)
                self.assertFalse(host._save_triggers())
                self.assertEqual(ac.TRIGGERS_LOCAL_FILE.read_text(), content)

    def test_invalid_local_shape_created_after_load_cannot_be_overwritten(self):
        host = TriggerHost()
        host._load_triggers()
        content = '[{"id": "custom", "tts_text": "Keep me"}]'
        ac.TRIGGERS_LOCAL_FILE.write_text(content)
        self.assertFalse(host._save_triggers())
        self.assertEqual(ac.TRIGGERS_LOCAL_FILE.read_text(), content)

    def test_slim_overrides_parse_false_like_full_trigger_records(self):
        for value in ("false", "0", "off", False):
            with self.subTest(value=value):
                ac.TRIGGERS_LOCAL_FILE.write_text(json.dumps({"triggers": [
                    {"id": "official", "enabled": value}]}))
                host = TriggerHost()
                host._load_triggers()
                self.assertFalse(host._triggers[0].enabled)

    def test_invalid_rows_in_a_local_list_are_preserved(self):
        content = json.dumps({"triggers": [{"id": "custom"}, ["another custom trigger"]]})
        ac.TRIGGERS_LOCAL_FILE.write_text(content)
        host = TriggerHost()
        host._load_triggers()
        self.assertTrue(host._local_corrupt)
        self.assertFalse(host._save_triggers())
        self.assertEqual(ac.TRIGGERS_LOCAL_FILE.read_text(), content)

    def test_import_uses_the_validated_file_even_if_the_source_changes(self):
        source = ac.TRIGGERS_LOCAL_FILE.with_name("import.json")
        original = b'{"triggers": [{"id": "imported", "tts_text": "Keep me"}]}'
        previous = b'{"triggers": [{"id": "previous"}]}'
        source.write_bytes(original)
        ac.TRIGGERS_LOCAL_FILE.write_bytes(previous)
        host = TriggerHost()
        host._load_triggers()

        def confirm(*args):
            source.write_text("interrupted write")
            return ac.QMessageBox.StandardButton.Yes

        with patch.object(ac.QFileDialog, "getOpenFileName", return_value=(str(source), "")), \
                patch.object(ac.QMessageBox, "question", side_effect=confirm), \
                patch.object(ac.QMessageBox, "information"), \
                patch.object(ac.QMessageBox, "critical") as failure:
            host._import_triggers()
        failure.assert_not_called()
        self.assertEqual(ac.TRIGGERS_LOCAL_FILE.read_bytes(), original)
        self.assertEqual(Path(str(ac.TRIGGERS_LOCAL_FILE) + ".bak").read_bytes(), previous)
        self.assertFalse(host._local_corrupt)
        self.assertIn("imported", {trigger.id for trigger in host._triggers})

    def test_import_rejects_encodings_the_local_reader_cannot_load(self):
        source = ac.TRIGGERS_LOCAL_FILE.with_name("import.json")
        previous = b'{"triggers": [{"id": "previous"}]}'
        ac.TRIGGERS_LOCAL_FILE.write_bytes(previous)
        for encoding in ("utf-16", "utf-8-sig"):
            with self.subTest(encoding=encoding), \
                    patch.object(ac.QFileDialog, "getOpenFileName", return_value=(str(source), "")), \
                    patch.object(ac.QMessageBox, "question") as confirm, \
                    patch.object(ac.QMessageBox, "critical") as failure:
                source.write_bytes('{"triggers": []}'.encode(encoding))
                TriggerHost()._import_triggers()
                failure.assert_called_once()
                confirm.assert_not_called()
                self.assertEqual(ac.TRIGGERS_LOCAL_FILE.read_bytes(), previous)


class TimelineDownloadBoundaryTests(unittest.TestCase):
    def test_invalid_refresh_preserves_the_cached_timeline_and_allows_retry(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(ac, "TIMELINES_DIR", Path(directory)), \
                patch("nyaatriggers.ui.timeline_tab.threading.Thread") as thread:
            thread.side_effect = lambda *, target, daemon: SimpleNamespace(start=target)
            host = SimpleNamespace(_cactbot_tl_lock=threading.Lock(),
                                   _cactbot_tl_fetching=set(), _cactbot_tl_signal=Mock())
            cache = Path(directory) / "Arena.cactbot.cache.txt"
            previous = b'30 "Raidwide"\n'
            replacement = b'\xef\xbb\xbf40 "Tankbuster"\n'
            for response in (b"", b"<html>Temporarily unavailable</html>", b"\xff", replacement):
                with self.subTest(response=response), \
                        patch("nyaatriggers.ui.timeline_tab.fetch_bytes", return_value=response):
                    cache.write_bytes(previous)
                    host._cactbot_tl_signal.reset_mock()
                    TimelineTabMixin._fetch_cactbot_timeline(host, "Arena", "raid/arena.txt")
                    valid = response == replacement
                    self.assertEqual(cache.read_bytes(), response if valid else previous)
                    self.assertEqual(host._cactbot_tl_signal.emit.call_count, int(valid))
                    self.assertFalse(host._cactbot_tl_fetching)
                    self.assertFalse(list(Path(directory).glob("*.tmp")))


class TriggerEditorBoundaryTests(unittest.TestCase):
    def test_extended_wire_types_can_be_edited_and_still_match(self):
        from PyQt6.QtWidgets import QDialog

        for kind in ("257", "260", "21|267"):
            with self.subTest(kind=kind):
                trigger = Trigger(log_type=kind, ability_regex="Ready")
                dlg = TriggerDialog(trigger)
                self.addCleanup(dlg.deleteLater)
                with patch("nyaatriggers.trigger_dialog.QMessageBox.warning") as warning:
                    dlg.accept()
                warning.assert_not_called()
                self.assertEqual(dlg.result(), QDialog.DialogCode.Accepted)
                saved = dlg.get_trigger(trigger.id)
                self.assertIsNotNone(saved.matches([kind.split("|")[-1], "ts", "Ready"]))


class SystemVoiceBoundaryTests(unittest.TestCase):
    def test_espeak_ng_installation_can_speak_japanese(self):
        with patch("platform.system", return_value="Linux"), \
                patch("shutil.which", side_effect=lambda name: "/usr/bin/espeak-ng" if name == "espeak-ng" else None), \
                patch.object(tts, "_jp_auto", True), \
                patch.object(tts, "_run_speak_proc", return_value=True) as speak:
            self.assertTrue(tts._system_speak("全体攻撃", reading="ぜんたいこうげき"))
            speak.assert_called_once()
            command, spoken = speak.call_args.args
            self.assertEqual(command[0], "espeak-ng")
            self.assertEqual(command[-2:], ["-v", "ja"])
            self.assertEqual(spoken, "ぜんたいこうげき")


if __name__ == "__main__":
    unittest.main()
