import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QObject
from PyQt6.QtTest import QSignalSpy
from PyQt6.QtWidgets import QApplication

from nyaatriggers import app_common as ac
from nyaatriggers.sequential import SequentialRunner
from nyaatriggers.ui.instance_tab import InstanceTabMixin
from nyaatriggers.ui.timeline_tab import TimelineTabMixin
from nyaatriggers.ui.triggers_tab import TriggersTabMixin


class TriggerHost(QObject, TriggersTabMixin, InstanceTabMixin, TimelineTabMixin):
    def __init__(self):
        super().__init__()
        self._triggers = []
        self._status_timers = []
        self._seq_runners = []
        self._local_enabled = True
        self.fired = []
        self.refreshes = 0

    def _refresh_table(self):
        self.refreshes += 1

    def _fire(self, trigger, captured):
        self.fired.append((trigger.id, trigger.tts_text))


class TriggerFileRecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        for name in ("TRIGGERS_FILE", "TRIGGERS_LOCAL_FILE", "RETIRED_FILE",
                     "_REPO_TRIGGERS_FILE", "_REPO_RETIRED_FILE", "_REPO_TRIGGERS_VERSION"):
            self.enterContext(patch.object(ac, name, directory / name))
        self.stamp = 1_700_000_000_000_000_000
        self.write(ac.TRIGGERS_FILE, '[{"id": "bundled", "tts_text": "Move"}]')
        self.host = TriggerHost()
        self.addCleanup(self.host.deleteLater)
        self.addCleanup(self.host._clear_status_timers)
        self.addCleanup(self.host._clear_seq_runners)

    def write(self, path, content):
        path.write_text(content, encoding="utf-8")
        self.stamp += 1_000_000_000
        os.utime(path, ns=(self.stamp, self.stamp))

    def repair(self, path, content):
        failed = path.stat()
        path.write_text(content, encoding="utf-8")
        os.utime(path, ns=(failed.st_atime_ns, failed.st_mtime_ns))
        self.assertEqual(path.stat().st_size, failed.st_size)
        self.assertEqual(path.stat().st_mtime_ns, failed.st_mtime_ns)

    def assert_triggers(self, expected):
        self.assertEqual([(t.id, t.tts_text) for t in self.host._triggers], expected)

    def assert_settled(self, expected):
        self.assert_triggers(expected)
        refreshes = self.host.refreshes
        self.host._maybe_reload_triggers()
        self.assertEqual(self.host.refreshes, refreshes)
        self.assert_triggers(expected)

    def test_failed_bundled_startup_read_retries_an_unchanged_stamp(self):
        repaired = ac.TRIGGERS_FILE.read_text(encoding="utf-8")
        self.write(ac.TRIGGERS_FILE, "!" + repaired[1:])
        self.host._load_triggers()
        self.assertEqual(self.host._triggers, [])
        self.repair(ac.TRIGGERS_FILE, repaired)
        self.host._maybe_reload_triggers()
        self.assert_settled([("bundled", "Move")])

    def test_failed_download_read_retries_after_using_bundled_fallback(self):
        repaired = '[{"id": "updated", "tts_text": "Away"}]'
        self.write(ac._REPO_TRIGGERS_FILE, "!" + repaired[1:])
        self.write(ac._REPO_TRIGGERS_VERSION, json.dumps(ac._VERSION))
        self.host._load_triggers()
        self.assert_triggers([("bundled", "Move")])
        self.repair(ac._REPO_TRIGGERS_FILE, repaired)
        self.host._maybe_reload_triggers()
        self.assert_settled([("updated", "Away")])

    def check_invalid_update_preserves_live_work(self, invalid):
        rows = [
            {"id": "warning", "tts_text": "Move", "log_type": "26",
             "ability_id": "ABC", "expiry_warn_s": 5, "cooldown_s": 120},
            {"id": "sequence", "tts_text": "Away", "log_type": "20",
             "ability_id": "8F00", "sequence": [{"log_type": "21", "ability_id": "8F01"}]},
            {"id": "editable", "tts_text": "Wait"},
        ]
        self.write(ac.TRIGGERS_FILE, json.dumps(rows))
        self.host._load_triggers()
        warning, sequence, editable = self.host._triggers
        warning._last_fired["older-effect"] = 12
        self.host._arm_status_timer(warning, {}, [
            "26", "ts", "ABC", "Effect", "5.001", "40000001", "Boss", "10FF0001", "Player",
        ])
        self.assertEqual(len(self.host._status_timers), 1)
        status = self.host._status_timers[0]
        runner = SequentialRunner(sequence, {}, self.host._on_seq_complete,
                                  self.host._on_seq_expire, self.host)
        self.host._seq_runners.append(runner)

        rows[2]["tts_text"] = "Away"
        repaired = json.dumps(rows)
        self.assertEqual(len(repaired.encode("utf-8")), ac.TRIGGERS_FILE.stat().st_size)
        self.write(ac.TRIGGERS_FILE, invalid.ljust(len(repaired)))
        self.host._maybe_reload_triggers()
        self.assertEqual(self.host._triggers, [warning, sequence, editable])
        self.assertIs(self.host._triggers[0], warning)
        self.assertIs(self.host._triggers[1], sequence)
        self.assertEqual(warning._last_fired, {"older-effect": 12})

        self.repair(ac.TRIGGERS_FILE, repaired)
        self.host._maybe_reload_triggers()
        self.assert_settled([("warning", "Move"), ("sequence", "Away"), ("editable", "Away")])
        self.assertIs(self.host._triggers[0], warning)
        self.assertIs(self.host._triggers[1], sequence)
        self.assertTrue(runner.try_advance([
            "21", "ts", "40000001", "Boss", "8F01", "Ability", "10FF0001", "Player",
        ]))
        self.assertEqual(self.host.fired, [("sequence", "Away")])
        self.assertTrue(QSignalSpy(status._timer.timeout).wait(1000))
        self.assertEqual(self.host.fired, [("sequence", "Away"), ("warning", "Move")])
        self.assertEqual(warning._last_fired["older-effect"], 12)
        self.assertEqual(self.host._status_timers, [])
        self.assertEqual(self.host._seq_runners, [])

    def test_invalid_bundled_object_preserves_live_work_until_repaired(self):
        self.check_invalid_update_preserves_live_work("{}")

    def test_invalid_bundled_row_preserves_live_work_until_repaired(self):
        self.check_invalid_update_preserves_live_work("[0]")

    def check_change_during_load_is_retried(self, local):
        path = ac.TRIGGERS_LOCAL_FILE if local else ac.TRIGGERS_FILE
        original = [{"id": "bundled", "tts_text": "Before"}]
        updated = [{"id": "bundled", "tts_text": "After!"}]
        original_text = json.dumps({"triggers": original} if local else original)
        updated_text = json.dumps({"triggers": updated} if local else updated)
        self.assertEqual(len(original_text.encode("utf-8")), len(updated_text.encode("utf-8")))
        self.write(path, original_text)
        before = path.stat()
        read_text = Path.read_text
        changes = []

        def read_then_change(read_path, *args, **kwargs):
            data = read_text(read_path, *args, **kwargs)
            if read_path == path and not changes:
                # Simulate an editor write after the real read, before the loader returns.
                self.write(path, updated_text)
                changes.append(path)
            return data

        with patch.object(Path, "read_text", read_then_change):
            self.host._load_triggers()
        self.assertEqual(changes, [path])
        self.assertEqual(path.stat().st_size, before.st_size)
        self.assertNotEqual(path.stat().st_mtime_ns, before.st_mtime_ns)
        self.assert_triggers([("bundled", "Before")])
        self.host._maybe_reload_triggers()
        self.assert_settled([("bundled", "After!")])

    def test_official_change_during_load_stays_pending_for_the_next_poll(self):
        self.check_change_during_load_is_retried(local=False)

    def test_local_change_during_load_stays_pending_for_the_next_poll(self):
        self.check_change_during_load_is_retried(local=True)

    def check_incomplete_write_after_preflight_preserves_live_work(self, local):
        rows = [
            {"id": "warning", "tts_text": "Move", "log_type": "26",
             "ability_id": "ABC", "expiry_warn_s": 5, "cooldown_s": 120},
            {"id": "sequence", "tts_text": "Away", "log_type": "20",
             "ability_id": "8F00", "sequence": [{"log_type": "21", "ability_id": "8F01"}]},
        ]
        path = ac.TRIGGERS_LOCAL_FILE if local else ac.TRIGGERS_FILE
        if local:
            self.write(ac.TRIGGERS_FILE, "[]")
        complete = json.dumps({"triggers": rows} if local else rows)
        incomplete = "!" + complete[1:]
        self.write(path, complete)
        self.host._load_triggers()
        warning, sequence = self.host._triggers
        warning._last_fired["older-effect"] = 12
        self.host._arm_status_timer(warning, {}, [
            "26", "ts", "ABC", "Effect", "5.001", "40000001", "Boss", "10FF0001", "Player",
        ])
        status = self.host._status_timers[0]
        runner = SequentialRunner(sequence, {}, self.host._on_seq_complete,
                                  self.host._on_seq_expire, self.host)
        self.host._seq_runners.append(runner)
        self.write(path, complete)
        before = path.stat()
        read_text = Path.read_text
        changes = []

        def read_then_corrupt(read_path, *args, **kwargs):
            data = read_text(read_path, *args, **kwargs)
            if read_path == path and not changes:
                self.write(path, incomplete)
                changes.append(path)
            return data

        with patch.object(Path, "read_text", read_then_corrupt), patch.object(ac, "persistence_warning"):
            self.host._maybe_reload_triggers()
        self.assertEqual(changes, [path])
        self.assertEqual(path.stat().st_size, before.st_size)
        self.assertNotEqual(path.stat().st_mtime_ns, before.st_mtime_ns)
        self.assertEqual(self.host._triggers, [warning, sequence])
        self.assertIs(self.host._triggers[0], warning)
        self.assertIs(self.host._triggers[1], sequence)
        self.assertEqual(warning._last_fired, {"older-effect": 12})
        self.assertNotEqual(self.host._triggers_mtime, self.host._trigger_files_stamp())
        if local:
            with patch.object(ac, "persistence_warning"):
                self.assertFalse(self.host._save_triggers())
            self.assertTrue(self.host._local_corrupt)
            self.assertEqual(path.read_text(encoding="utf-8"), incomplete)
        self.repair(path, complete)
        self.host._maybe_reload_triggers()
        self.assert_settled([("warning", "Move"), ("sequence", "Away")])
        self.assertIs(self.host._triggers[0], warning)
        self.assertIs(self.host._triggers[1], sequence)
        self.assertFalse(self.host._local_corrupt)
        self.assertTrue(runner.try_advance([
            "21", "ts", "40000001", "Boss", "8F01", "Ability", "10FF0001", "Player",
        ]))
        self.assertTrue(QSignalSpy(status._timer.timeout).wait(1000))
        self.assertEqual(self.host.fired, [("sequence", "Away"), ("warning", "Move")])
        self.assertEqual(warning._last_fired["older-effect"], 12)

    def test_bundled_incomplete_write_after_preflight_preserves_live_work(self):
        self.check_incomplete_write_after_preflight_preserves_live_work(local=False)

    def test_local_incomplete_write_after_preflight_preserves_live_work(self):
        self.check_incomplete_write_after_preflight_preserves_live_work(local=True)

    def test_stale_download_cleanup_settles_after_a_bundled_update(self):
        self.host._load_triggers()
        self.write(ac._REPO_TRIGGERS_FILE, '[{"id": "stale", "tts_text": "Away"}]')
        self.write(ac._REPO_TRIGGERS_VERSION, json.dumps(ac._VERSION))
        self.write(ac.TRIGGERS_FILE, '[{"id": "fresh", "tts_text": "Move"}]')
        self.host._maybe_reload_triggers()
        self.assertFalse(ac._REPO_TRIGGERS_FILE.exists())
        self.assert_settled([("fresh", "Move")])


if __name__ == "__main__":
    unittest.main()
