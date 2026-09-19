"""Callouts and saved choices across refreshes, toggles and duty changes."""

from contextlib import ExitStack
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from tests.test_callout_review_fixes import Host
from tests.test_data_safety import TriggerHost
from nyaatriggers import app_common as ac, tts
from nyaatriggers.trigger_engine import Trigger
from nyaatriggers.ui.automarkers_tab import AutomarkersTabMixin
from nyaatriggers.ui.connection import ConnectionMixin


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
