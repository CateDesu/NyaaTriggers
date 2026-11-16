from contextlib import ExitStack, contextmanager
from copy import deepcopy
import errno
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtTest import QSignalSpy
from PyQt6.QtWidgets import QApplication

from nyaatriggers import app_common as ac
from nyaatriggers.sequential import SequentialRunner
from nyaatriggers.ui.connection import ConnectionMixin
from nyaatriggers.ui.settings_tab import SettingsTabMixin
from tests.test_trigger_file_recovery import TriggerHost


@contextmanager
def deny(path):
    mode = path.stat().st_mode & 0o777
    try:
        path.chmod(0)
        yield
    finally:
        path.chmod(mode)


class SettingsHost(SettingsTabMixin, ConnectionMixin):
    def __init__(self):
        self._settings = {}
        self._settings_load_warning = None
        self._me_name = ""
        self._settings_save_timer = Mock()
        self._settings_save_timer.isActive.return_value = False
        self._warn_save_failed = Mock()


class AccessFailureRecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.storage = self.root / "storage"
        self.storage.mkdir()
        self.bundle = self.root / "bundle"
        self.bundle.mkdir()
        for name in ("_SETTINGS_FILE", "TRIGGERS_LOCAL_FILE", "_REPO_TRIGGERS_FILE",
                     "_REPO_RETIRED_FILE", "_REPO_TRIGGERS_VERSION"):
            self.enterContext(patch.object(ac, name, self.storage / name))
        for name in ("TRIGGERS_FILE", "RETIRED_FILE"):
            self.enterContext(patch.object(ac, name, self.bundle / name))
        self.enterContext(patch.object(ac, "persistence_warning"))
        self.enterContext(patch.object(ac, "log_drop"))
        self.settings = SettingsHost()
        self.saved = b'{"ui_language":"ja","char_name":"Saved","sentinel":"Keep","local_enabled":false}'
        ac._SETTINGS_FILE.write_bytes(self.saved)
        self.host = TriggerHost()
        self.addCleanup(self.host.deleteLater)
        self.addCleanup(self.host._clear_status_timers)
        self.addCleanup(self.host._clear_seq_runners)
        self.stamp = 1_700_000_000_000_000_000

    def write(self, path, value):
        path.write_text(json.dumps(value), encoding="utf-8")
        os.utime(path, ns=(self.stamp, self.stamp))

    def assert_settings_backup(self):
        self.assertEqual(ac._SETTINGS_FILE.with_name(ac._SETTINGS_FILE.name + ".bad").read_bytes(),
                         self.saved)

    @unittest.skipUnless(os.name == "posix" and os.geteuid() != 0, "Needs directory permissions")
    def test_settings_parent_denial_then_player_metadata_preserves_original(self):
        with deny(self.storage):
            with self.assertRaises(PermissionError):
                ac._SETTINGS_FILE.read_bytes()
            self.settings._load_settings()
        self.assertTrue(self.settings._settings_backup_pending)
        self.assertIsNotNone(self.settings._settings_load_warning)
        self.settings._on_ws_primary_player(0x10000001, "Live player")
        self.assert_settings_backup()
        self.assertEqual(json.loads(ac._SETTINGS_FILE.read_text()), {"char_name": "Live player"})

    @unittest.skipUnless(os.name == "posix" and os.geteuid() != 0, "Needs file permissions")
    def test_repeated_settings_denial_keeps_backup_pending_until_shutdown_retry(self):
        with deny(ac._SETTINGS_FILE):
            self.settings._load_settings()
        self.assertTrue(self.settings._settings_backup_pending)
        self.settings._settings["char_name"] = "Latest"
        with deny(self.storage):
            for _ in range(2):
                self.assertFalse(self.settings._save_settings())
                self.assertTrue(self.settings._settings_backup_pending)
                self.assertTrue(self.settings._settings_save_failed)
        self.assertEqual(ac._SETTINGS_FILE.read_bytes(), self.saved)
        self.settings._flush_pending_settings_save()
        self.assert_settings_backup()
        self.assertFalse(self.settings._settings_backup_pending)
        self.assertFalse(self.settings._settings_save_failed)
        self.assertEqual(json.loads(ac._SETTINGS_FILE.read_text()), {"char_name": "Latest"})

    def test_settings_open_io_failure_warns_and_preserves_original_on_retry(self):
        real_open = io.open

        def fail(path, *args, **kwargs):
            if os.fspath(path) == str(ac._SETTINGS_FILE):
                raise OSError(errno.EIO, "Storage unavailable", str(path))
            return real_open(path, *args, **kwargs)

        with patch.object(io, "open", fail):
            self.settings._load_settings()
        self.assertIsNotNone(self.settings._settings_load_warning)
        self.assertTrue(self.settings._save_settings())
        self.assert_settings_backup()

    def test_settings_pending_backup_stat_errors_block_replacement(self):
        for code in (errno.EACCES, errno.EPERM, errno.EIO):
            with self.subTest(code=code):
                self.settings._settings_backup_pending = True
                real_stat = os.stat

                def fail(path, *args, **kwargs):
                    if os.fspath(path) == str(ac._SETTINGS_FILE):
                        raise OSError(code, "Storage unavailable", str(path))
                    return real_stat(path, *args, **kwargs)

                with patch.object(os, "stat", fail):
                    self.assertFalse(self.settings._save_settings())
                self.assertTrue(self.settings._settings_backup_pending)
                self.assertEqual(ac._SETTINGS_FILE.read_bytes(), self.saved)
        self.settings._flush_pending_settings_save()
        self.assert_settings_backup()

    def test_settings_absence_and_deliberate_deletion_allow_saving(self):
        ac._SETTINGS_FILE.unlink()
        self.settings._load_settings()
        self.assertIsNone(self.settings._settings_load_warning)
        self.assertFalse(self.settings._settings_backup_pending)
        self.settings._on_ws_primary_player(0x10000001, "First player")
        self.assertEqual(json.loads(ac._SETTINGS_FILE.read_text()), {"char_name": "First player"})
        self.settings._settings_backup_pending = True
        ac._SETTINGS_FILE.unlink()
        self.assertTrue(self.settings._save_settings())
        self.assertFalse(self.settings._settings_backup_pending)
        self.assertEqual(list(self.storage.glob("*.bad*")), [])

    @unittest.skipUnless(os.name == "posix" and os.geteuid() != 0, "Needs directory permissions")
    def test_real_window_preserves_unread_settings_before_automatic_player_save(self):
        from nyaatriggers import main_window as mw
        from nyaatriggers.ui import engines

        with ExitStack() as stack:
            stack.enter_context(patch.object(ac, "_DATA_DIR", self.root))
            stack.enter_context(patch.object(mw, "_DATA_DIR", self.root))
            stack.enter_context(patch.object(ac, "_TRIGGERNOMETRY_INVENTORY_CACHE", self.root / "inventory.json"))
            stack.enter_context(patch.object(ac, "_TRIGGEVENT_INVENTORY_CACHE", self.root / "tv-cache.json"))
            stack.enter_context(patch.object(ac, "_TRIGGEVENT_INVENTORY_SEED", self.root / "tv-seed.json"))
            stack.enter_context(patch("nyaatriggers.ui.engines._tn_packs_dir", return_value=self.root / "packs"))

            def load(window):
                with deny(self.storage):
                    SettingsTabMixin._load_settings(window)

            stack.enter_context(patch.object(mw.MainWindow, "_load_settings", load))
            for name in ("_load_triggers", "_load_timeline_for_zone", "_refresh_telesto_party"):
                stack.enter_context(patch.object(mw.MainWindow, name))
            for target in ((mw.PluginLink, "start"), (mw.QTimer, "singleShot"),
                           (mw, "kokoro_ready"), (mw, "_load_piper"), (mw, "_ensure_worker"),
                           (mw, "set_engine"), (ac.QMessageBox, "warning")):
                stack.enter_context(patch.object(*target))
            # Native engines are deliberately unavailable in this storage fixture.
            stack.enter_context(patch.object(mw.TriggeventBridge, "is_available", return_value=False))
            if engines.TriggernometryBridge is not None:
                stack.enter_context(patch.object(engines.TriggernometryBridge, "is_available", return_value=False))
            stack.enter_context(patch("nyaatriggers.ui.recap_widgets.RecapIcons._pump"))
            window = mw.MainWindow()
            try:
                self.assertIsNotNone(window._settings_load_warning)
                self.assertTrue(window._settings_backup_pending)
                window._on_ws_primary_player(0x10000001, "Live player")
                self.assert_settings_backup()
                self.assertEqual(json.loads(ac._SETTINGS_FILE.read_text())["char_name"], "Live player")
                self.assertFalse(window._settings_save_failed)
            finally:
                window.close()
                window._prog_sessions.close()

    def test_backup_name_access_failure_is_contained_and_retried(self):
        ac._SETTINGS_FILE.write_bytes(b"invalid settings")
        real_stat = os.stat

        def fail(path, *args, **kwargs):
            if ".bad" in Path(path).name:
                raise PermissionError(errno.EACCES, "Backup unavailable", str(path))
            return real_stat(path, *args, **kwargs)

        self.write(ac.TRIGGERS_FILE, [])
        ac.TRIGGERS_LOCAL_FILE.write_text("invalid triggers")
        with patch.object(os, "stat", fail):
            self.settings._load_settings()
            self.host._load_triggers()
        self.assertTrue(self.settings._settings_backup_pending)
        self.assertTrue(self.host._local_corrupt)
        self.assertTrue(self.settings._save_settings())
        self.assertEqual(ac._SETTINGS_FILE.with_name(ac._SETTINGS_FILE.name + ".bad").read_bytes(),
                         b"invalid settings")

    @unittest.skipUnless(os.name == "posix" and os.geteuid() != 0, "Needs directory permissions")
    def test_backup_candidate_denial_preserves_existing_backup_until_retry(self):
        backup = ac._SETTINGS_FILE.with_name(ac._SETTINGS_FILE.name + ".bad")
        backup.write_bytes(b"previous backup")
        self.settings._settings_backup_pending = True
        real_stat = os.stat

        def fail_candidate(path, *args, **kwargs):
            if os.fspath(path) == str(backup):
                with deny(self.storage):
                    return real_stat(path, *args, **kwargs)
            return real_stat(path, *args, **kwargs)

        with patch.object(os, "stat", fail_candidate):
            self.assertFalse(self.settings._save_settings())
        self.assertTrue(self.settings._settings_backup_pending)
        self.assertEqual(backup.read_bytes(), b"previous backup")
        self.assertEqual(ac._SETTINGS_FILE.read_bytes(), self.saved)
        self.assertTrue(self.settings._save_settings())
        self.assertEqual(backup.read_bytes(), b"previous backup")
        self.assertEqual(backup.with_name(backup.name + ".1").read_bytes(), self.saved)

    def test_file_in_place_of_settings_directory_is_not_first_run_absence(self):
        blocker = self.root / "not-a-directory"
        blocker.write_bytes(b"keep")
        with patch.object(ac, "_SETTINGS_FILE", blocker / "settings.json"):
            self.settings._load_settings()
            self.assertIsNotNone(self.settings._settings_load_warning)
            self.assertTrue(self.settings._settings_backup_pending)
            self.assertFalse(self.settings._save_settings())
        self.assertEqual(blocker.read_bytes(), b"keep")

    def arm(self, local):
        rows = [
            {"id": "warning", "tts_text": "Move", "log_type": "26", "ability_id": "ABC",
             "expiry_warn_s": 5, "cooldown_s": 120},
            {"id": "sequence", "tts_text": "Away", "log_type": "20", "ability_id": "8F00",
             "sequence": [{"log_type": "21", "ability_id": "8F01"}]},
        ]
        self.write(ac.TRIGGERS_FILE, [{"id": "official"}] + ([] if local else rows))
        self.write(ac.TRIGGERS_LOCAL_FILE, {
            "triggers": [{"id": "official", "enabled": False}] + (rows if local else []),
            "folders": [{"id": "kept", "name": "Keep"}], "deleted": ["removed"]})
        self.host._load_triggers()
        warning, sequence = self.host._triggers[1:]
        warning._last_fired.clear()
        warning._last_fired["older-effect"] = 12
        self.host._arm_status_timer(warning, {}, [
            "26", "ts", "ABC", "Effect", "5.001", "40000001", "Boss", "10FF0001", "Player"])
        runner = SequentialRunner(sequence, {}, self.host._on_seq_complete,
                                  self.host._on_seq_expire, self.host)
        self.host._seq_runners.append(runner)
        return runner

    def complete(self, runner):
        self.assertTrue(runner.try_advance([
            "21", "ts", "40000001", "Boss", "8F01", "Ability", "10FF0001", "Player"]))

    @unittest.skipUnless(os.name == "posix" and os.geteuid() != 0, "Needs directory permissions")
    def test_local_and_bundled_denial_retain_live_state_for_direct_and_watcher_reload(self):
        for local in (True, False):
            for method in ("_load_triggers", "_maybe_reload_triggers"):
                with self.subTest(local=local, method=method):
                    self.host._clear_status_timers()
                    self.host._clear_seq_runners()
                    runner = self.arm(local)
                    before = list(self.host._triggers)
                    baseline = deepcopy(self.host._local_trigger_baseline)
                    folders = deepcopy(self.host._folders)
                    status = self.host._status_timers[0]
                    path = ac.TRIGGERS_LOCAL_FILE if local else ac.TRIGGERS_FILE
                    original = path.read_bytes()
                    stamp = self.host._trigger_files_stamp()
                    with deny(path.parent):
                        for _ in range(2):
                            getattr(self.host, method)()
                            self.assertEqual(self.host._triggers, before)
                            self.assertTrue(all(a is b for a, b in zip(self.host._triggers, before)))
                            self.assertEqual(self.host._local_trigger_baseline, baseline)
                            self.assertEqual(self.host._folders, folders)
                            self.assertFalse(self.host._triggers[0].enabled)
                            self.assertEqual(self.host._status_timers, [status])
                    self.assertEqual(self.host._trigger_files_stamp(), stamp)
                    self.assertEqual(path.read_bytes(), original)
                    self.assertEqual(self.host._triggers_mtime, ())
                    refreshes = self.host.refreshes
                    self.host._maybe_reload_triggers()
                    self.assertGreater(self.host.refreshes, refreshes)
                    self.assertTrue(all(a is b for a, b in zip(self.host._triggers, before)))
                    self.assertEqual(before[1]._last_fired, {"older-effect": 12})
                    self.host.fired.clear()
                    self.complete(runner)
                    self.assertEqual(self.host.fired, [("sequence", "Away")])
                    self.assertTrue(QSignalSpy(status._timer.timeout).wait(1000))
                    self.assertEqual(self.host.fired, [("sequence", "Away"), ("warning", "Move")])

    def test_watcher_retries_stat_io_failure_even_with_an_unchanged_stamp(self):
        runner = self.arm(local=True)
        original = list(self.host._triggers)
        stamp = self.host._trigger_files_stamp()
        real_stat = os.stat

        def fail(path, *args, **kwargs):
            if os.fspath(path) == str(ac.TRIGGERS_LOCAL_FILE):
                raise OSError(errno.EIO, "Storage unavailable", str(path))
            return real_stat(path, *args, **kwargs)

        with patch.object(os, "stat", fail):
            self.host._maybe_reload_triggers()
        self.assertTrue(all(a is b for a, b in zip(self.host._triggers, original)))
        self.assertEqual(self.host._triggers_mtime, ())
        self.assertEqual(self.host._trigger_files_stamp(), stamp)
        self.host._maybe_reload_triggers()
        self.complete(runner)
        self.assertEqual(self.host.fired, [("sequence", "Away")])

    @unittest.skipUnless(os.name == "posix" and os.geteuid() != 0, "Needs directory permissions")
    def test_inaccessible_startup_local_file_warns_blocks_save_and_retries(self):
        self.write(ac.TRIGGERS_FILE, [{"id": "official"}])
        self.write(ac.TRIGGERS_LOCAL_FILE, {"triggers": [{"id": "local", "tts_text": "Away"}]})
        with deny(self.storage):
            self.host._load_triggers()
            self.assertTrue(self.host._local_corrupt)
            self.assertFalse(self.host._save_triggers())
        ac.persistence_warning.assert_called_once()
        self.host._maybe_reload_triggers()
        self.assertFalse(self.host._local_corrupt)
        self.assertEqual([t.id for t in self.host._triggers], ["official", "local"])

    def test_true_local_deletion_removes_overrides_and_cancels_old_sequence(self):
        runner = self.arm(local=True)
        ac.TRIGGERS_LOCAL_FILE.unlink()
        self.host._maybe_reload_triggers()
        self.assertEqual([t.id for t in self.host._triggers], ["official"])
        self.assertTrue(self.host._triggers[0].enabled)
        self.assertIsNone(self.host._local_trigger_baseline)
        self.assertFalse(self.host._local_corrupt)
        self.complete(runner)
        self.assertEqual(self.host.fired, [])

    def test_true_bundled_deletion_removes_callouts(self):
        runner = self.arm(local=False)
        ac.TRIGGERS_FILE.unlink()
        self.host._maybe_reload_triggers()
        self.assertEqual(self.host._triggers, [])
        self.complete(runner)
        self.assertEqual(self.host.fired, [])

    @unittest.skipUnless(os.name == "posix" and os.geteuid() != 0, "Needs file permissions")
    def test_unreadable_download_still_uses_the_bundled_fallback(self):
        self.write(ac.TRIGGERS_FILE, [{"id": "bundled"}])
        self.write(ac._REPO_TRIGGERS_FILE, [{"id": "downloaded"}])
        self.write(ac._REPO_TRIGGERS_VERSION, ac._VERSION)
        self.host._load_triggers()
        self.assertEqual([t.id for t in self.host._triggers], ["downloaded"])
        with deny(ac._REPO_TRIGGERS_FILE):
            self.host._load_triggers()
        self.assertEqual([t.id for t in self.host._triggers], ["bundled"])
        self.assertEqual(self.host._triggers_mtime, ())
        self.host._maybe_reload_triggers()
        self.assertEqual([t.id for t in self.host._triggers], ["downloaded"])

    @unittest.skipUnless(os.name == "posix" and os.geteuid() != 0, "Needs directory permissions")
    def test_retry_observes_equal_size_equal_time_edit_and_invalidates_changed_work(self):
        runner = self.arm(local=True)
        warning, sequence = self.host._triggers[1:]
        status = self.host._status_timers[0]
        stamp = self.host._trigger_files_stamp()
        path = ac.TRIGGERS_LOCAL_FILE
        previous = path.read_bytes()
        with deny(self.storage):
            self.host._maybe_reload_triggers()
        updated = previous.replace(b"Move", b"Flee")
        self.assertNotEqual(updated, previous)
        self.assertEqual(len(updated), len(previous))
        path.write_bytes(updated)
        os.utime(path, ns=(self.stamp, self.stamp))
        self.assertEqual(self.host._trigger_files_stamp(), stamp)
        self.host._maybe_reload_triggers()
        self.assertIsNot(self.host._triggers[1], warning)
        self.assertEqual(self.host._triggers[1].tts_text, "Flee")
        self.assertIs(self.host._triggers[2], sequence)
        self.complete(runner)
        self.assertTrue(QSignalSpy(status._timer.timeout).wait(1000))
        self.assertEqual(self.host.fired, [("sequence", "Away")])

    def import_file(self):
        source = self.root / "import.json"
        source.write_bytes(b'{"triggers":[{"id":"imported"}]}')
        self.enterContext(patch.object(ac.QFileDialog, "getOpenFileName", return_value=(str(source), "")))
        self.enterContext(patch.object(ac.QMessageBox, "question", return_value=ac.QMessageBox.StandardButton.Yes))
        failure = self.enterContext(patch.object(ac.QMessageBox, "critical"))
        success = self.enterContext(patch.object(ac.QMessageBox, "information"))
        return source.read_bytes(), failure, success

    @unittest.skipUnless(os.name == "posix" and os.geteuid() != 0, "Needs directory permissions")
    def test_import_access_recovers_after_failed_inspection_without_skipping_backup(self):
        self.write(ac.TRIGGERS_FILE, [])
        self.write(ac.TRIGGERS_LOCAL_FILE, {"triggers": [{"id": "kept"}]})
        previous = ac.TRIGGERS_LOCAL_FILE.read_bytes()
        imported, failure, success = self.import_file()
        real_stat, real_open = os.stat, io.open
        failures = []

        def recover(call, path, *args, **kwargs):
            if isinstance(path, int) or os.fspath(path) != str(ac.TRIGGERS_LOCAL_FILE) or failures:
                return call(path, *args, **kwargs)
            try:
                return call(path, *args, **kwargs)
            except PermissionError:
                failures.append(path)
                self.storage.chmod(0o700)
                raise

        with deny(self.storage), \
                patch.object(os, "stat", lambda *a, **kw: recover(real_stat, *a, **kw)), \
                patch.object(io, "open", lambda *a, **kw: recover(real_open, *a, **kw)):
            self.host._import_triggers()
        self.assertEqual(len(failures), 1)
        failure.assert_called_once()
        success.assert_not_called()
        self.assertEqual(ac.TRIGGERS_LOCAL_FILE.read_bytes(), previous)
        self.assertFalse(ac.TRIGGERS_LOCAL_FILE.with_name(ac.TRIGGERS_LOCAL_FILE.name + ".bak").exists())
        self.host._import_triggers()
        self.assertEqual(ac.TRIGGERS_LOCAL_FILE.read_bytes(), imported)
        self.assertEqual(ac.TRIGGERS_LOCAL_FILE.with_name(ac.TRIGGERS_LOCAL_FILE.name + ".bak").read_bytes(), previous)
        self.assertEqual(list(self.storage.glob("*.tmp")), [])

    def test_import_into_absent_destination_needs_no_backup(self):
        self.write(ac.TRIGGERS_FILE, [])
        imported, failure, success = self.import_file()
        self.host._import_triggers()
        failure.assert_not_called()
        success.assert_called_once()
        self.assertEqual(ac.TRIGGERS_LOCAL_FILE.read_bytes(), imported)
        self.assertFalse(ac.TRIGGERS_LOCAL_FILE.with_name(ac.TRIGGERS_LOCAL_FILE.name + ".bak").exists())


if __name__ == "__main__":
    unittest.main()
