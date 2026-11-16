from contextlib import ExitStack, contextmanager
import errno
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QCoreApplication, QEvent
from PyQt6.QtWidgets import QApplication

from nyaatriggers import app_common as ac, main_window as mw
from nyaatriggers.telesto_client import TelestoClient
from nyaatriggers.ui import engines, timeline_tab


TAG = "Zoraal Ja EX"
ZONE = "Everkeep (Extreme)"
STAMP = 1_700_000_000_000_000_000
USER = b'60 "User cue"\n'
BUNDLE = b'60 "Pack cue"\n'
ROOT = Path(__file__).resolve().parents[1]


@contextmanager
def deny(path):
    mode = path.stat().st_mode & 0o777
    try:
        path.chmod(0)
        yield
    finally:
        path.chmod(mode)


class TimelineAccessRecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.data = self.root / "data"
        self.local = self.data / "timelines"
        self.bundle = self.root / "bundle" / "timelines"
        self.local.mkdir(parents=True)
        self.bundle.mkdir(parents=True)
        self.path = self.local / f"{TAG}.txt"
        for name in ("TRIGGERS_FILE", "RETIRED_FILE", "TRIGGERS_LOCAL_FILE", "_REPO_TRIGGERS_FILE",
                     "_REPO_RETIRED_FILE", "_REPO_TRIGGERS_VERSION", "_SETTINGS_FILE",
                     "_TRIGGERNOMETRY_INVENTORY_CACHE", "_TRIGGEVENT_INVENTORY_CACHE",
                     "_TRIGGEVENT_INVENTORY_SEED", "_TRIGGEVENT_AUTOMARK_INVENTORY_CACHE",
                     "_TRIGGEVENT_AUTOMARK_INVENTORY_SEED", "CACTBOT_TIMELINES_FILE"):
            self.enterContext(patch.object(ac, name, self.data / name))
        self.enterContext(patch.multiple(ac, _DATA_DIR=self.data, TIMELINES_DIR=self.local,
                                        _BUNDLE_TIMELINES_DIR=self.bundle, _cactbot_tl_cache=None))
        self.enterContext(patch.object(mw, "_DATA_DIR", self.data))
        self.enterContext(patch.object(engines, "_tn_packs_dir", return_value=self.data / "packs"))
        rows = [row for row in json.loads((ROOT / "assets" / "triggers.json").read_text(encoding="utf-8"))
                if row.get("fight") == TAG]
        self.assertTrue(rows)
        self.write(ac.TRIGGERS_FILE, json.dumps(rows).encode())
        self.write(ac.RETIRED_FILE, b"[]")
        self.write(ac.TRIGGERS_LOCAL_FILE, b'{"triggers": []}')
        self.write(ac.CACTBOT_TIMELINES_FILE, b"{}")
        self.write(ac._SETTINGS_FILE, json.dumps({
            "tts_engine": "system", "auto_connect": False, "auto_check_updates": False,
            "local_enabled": True, "cactbot_enabled": False, "ui_language": "en",
            "callouts_localized": False,
        }).encode())
        for owner, name in ((mw.PluginLink, "start"), (mw.QTimer, "singleShot"), (mw, "kokoro_ready"),
                            (mw, "_ensure_worker"), (mw, "set_engine"), (TelestoClient, "start"),
                            (mw.MainWindow, "_refresh_telesto_party")):
            self.enterContext(patch.object(owner, name))
        # Native engines are deliberately unavailable in this timeline fixture.
        self.enterContext(patch.object(mw.TriggeventBridge, "is_available", return_value=False))
        self.enterContext(patch.object(engines.TriggernometryBridge, "is_available", return_value=False))
        self.enterContext(patch("nyaatriggers.ui.recap_widgets.RecapIcons._pump"))
        self.enterContext(patch.object(ac.QMessageBox, "warning"))
        self.enterContext(patch.object(ac.QMessageBox, "information"))
        self.window = mw.MainWindow()
        self.addCleanup(self.close_window)
        self.assertTrue(self.window._settings["local_enabled"])
        self.assertFalse(self.window._settings["cactbot_enabled"])
        self.assertTrue(self.window._local_enabled and self.window._global_local_on_flag)
        self.assertFalse(self.window._cactbot_mode)
        self.assertEqual(self.window._zone_redetect_timer.interval(), 30_000)
        self.assertTrue(self.window._zone_redetect_timer.isActive())
        self.loads = self.enterContext(patch.object(self.window, "_load_timeline_for_zone",
                                                   wraps=self.window._load_timeline_for_zone))
        self.logs = self.enterContext(patch.object(ac, "log_drop"))
        self.fetches = self.enterContext(patch.object(self.window, "_fetch_cactbot_timeline"))

    def close_window(self):
        self.window.close()
        self.window._prog_sessions.close()
        self.window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def write(self, path, data=USER):
        path.write_bytes(data)
        os.utime(path, ns=(STAMP, STAMP))

    def snapshot(self, path):
        info = path.stat()
        return path.read_bytes(), info.st_size, info.st_mtime_ns

    def zone(self):
        self.window._ws._on_message(json.dumps({"type": "ChangeZone", "zoneID": 0, "zoneName": ZONE}))
        self.assertEqual(self.window._match_zone, ZONE)
        self.assertEqual(self.window._fight_tag_for_zone(ZONE)[0], TAG)

    def combat(self, active=True):
        self.window._ws._on_message(json.dumps({
            "type": "InCombat", "inACTCombat": active, "inGameCombat": active,
        }))
        self.assertEqual(self.window._in_game_combat, active)

    def poll(self):
        self.window._zone_redetect_timer.timeout.emit()

    def assert_schedule(self, label="User cue"):
        self.assertEqual(self.window._timeline.upcoming(), [] if label is None else [(60, label)])

    def assert_pending(self, errors=1):
        self.assert_schedule(None)
        self.assertEqual(self.window._timeline_fight, "")
        diagnostics = [call.args for call in self.logs.call_args_list if call.args[0] == "timeline"]
        self.assertEqual(len(diagnostics), errors)
        self.assertTrue(all("load failed" in detail for _, detail in diagnostics))

    def assert_repaired_poll(self, before, loads=2):
        self.assertEqual(self.snapshot(self.path), before)
        for _ in range(3):
            self.poll()
        self.assert_schedule()
        self.assertEqual(self.window._timeline_fight, TAG)
        self.assertEqual(self.loads.call_count, loads)
        self.assertFalse(self.window._timeline_from_cactbot)
        self.fetches.assert_not_called()

    @unittest.skipUnless(os.name == "posix" and os.geteuid() != 0, "Needs directory permissions")
    def test_directory_denial_retries_before_competing_bundle(self):
        self.write(self.path)
        self.write(self.bundle / self.path.name, BUNDLE)
        self.assertEqual(len(USER), len(BUNDLE))
        before = self.snapshot(self.path)
        with deny(self.local):
            with self.assertRaises(PermissionError):
                self.path.stat()
            self.zone()
            self.assert_pending()
        self.assert_repaired_poll(before)

    @unittest.skipUnless(os.name == "posix" and os.geteuid() != 0, "Needs directory permissions")
    def test_repaired_timer_selects_unchanged_user_file_over_bundle(self):
        self.write(self.path)
        self.write(self.bundle / self.path.name, BUNDLE)
        before = self.snapshot(self.path)
        with deny(self.local):
            self.zone()
        self.assert_repaired_poll(before)

    @unittest.skipUnless(os.name == "posix" and os.geteuid() != 0, "Needs directory permissions")
    def test_same_directory_layout_retries_unchanged_file(self):
        self.enterContext(patch.object(ac, "_BUNDLE_TIMELINES_DIR", self.local))
        self.write(self.path)
        before = self.snapshot(self.path)
        with deny(self.local):
            with self.assertRaises(PermissionError):
                self.path.read_bytes()
            self.zone()
            self.assert_pending()
        self.assert_repaired_poll(before)

    @unittest.skipUnless(os.name == "posix" and os.geteuid() != 0, "Needs directory permissions")
    def test_same_pull_poll_recovers_after_combat_began_during_denial(self):
        self.write(self.path)
        before = self.snapshot(self.path)
        now = [1000.0]
        with patch("time.monotonic", side_effect=lambda: now[0]):
            with deny(self.local):
                self.zone()
                self.combat()
                self.assert_pending(errors=2)
            now[0] += 5
            self.assert_repaired_poll(before, loads=3)
            self.assertTrue(self.window._timeline.is_active())
            self.assertEqual(self.window._timeline.current_time(), 5)

    @unittest.skipUnless(os.name == "posix" and os.geteuid() != 0, "Needs directory permissions")
    def test_empty_schedule_can_recover_at_the_next_combat_edge(self):
        self.write(self.path)
        with deny(self.local):
            self.zone()
        self.combat()
        self.assert_schedule()
        self.assertEqual(self.loads.call_count, 2)
        self.assertTrue(self.window._timeline.is_active())

    def test_nonempty_bundle_does_not_reload_at_combat_or_watch_new_local_file(self):
        self.write(self.bundle / self.path.name, BUNDLE)
        self.zone()
        self.write(self.path)
        self.combat()
        for _ in range(3):
            self.poll()
        self.assert_schedule("Pack cue")
        self.assertEqual(self.loads.call_count, 1)
        self.assertEqual(self.window._timeline_fight, TAG)
        self.logs.assert_not_called()

    def test_genuine_absence_is_acknowledged_without_a_new_file_watcher(self):
        self.zone()
        self.assert_schedule(None)
        self.assertEqual(self.window._timeline_fight, TAG)
        self.write(self.path)
        self.poll()
        self.assert_schedule(None)
        self.assertEqual(self.loads.call_count, 1)
        self.logs.assert_not_called()
        self.fetches.assert_not_called()

    @unittest.skipUnless(os.name == "posix" and os.geteuid() != 0, "Needs directory permissions")
    def test_bundle_directory_denial_retries_when_local_is_absent(self):
        path = self.bundle / self.path.name
        self.write(path, BUNDLE)
        before = self.snapshot(path)
        with deny(self.bundle):
            self.zone()
            self.assert_pending()
        self.poll()
        self.assert_schedule("Pack cue")
        self.assertEqual(self.snapshot(path), before)
        self.assertEqual(self.loads.call_count, 2)

    @unittest.skipUnless(os.name == "posix" and os.geteuid() != 0, "Needs directory permissions")
    def test_final_selected_path_denial_remains_pending(self):
        self.write(self.path)
        before = self.snapshot(self.path)
        select = timeline_tab._local_timeline_path
        with ExitStack() as access:
            def selected_then_denied(fight):
                path = select(fight)
                self.assertEqual(path, self.path)
                access.enter_context(deny(self.local))
                return path

            with patch.object(timeline_tab, "_local_timeline_path", side_effect=selected_then_denied):
                self.zone()
                self.assert_pending()
        self.assert_repaired_poll(before)

    def test_selected_file_disappearing_is_still_genuine_absence(self):
        self.write(self.path)
        select = timeline_tab._local_timeline_path

        def selected_then_removed(fight):
            path = select(fight)
            path.unlink()
            return path

        with patch.object(timeline_tab, "_local_timeline_path", side_effect=selected_then_removed):
            self.zone()
        self.assert_schedule(None)
        self.assertEqual(self.window._timeline_fight, TAG)
        self.logs.assert_not_called()

    def test_operational_stat_error_is_not_absence(self):
        self.write(self.path)
        self.write(self.bundle / self.path.name, BUNDLE)
        before = self.snapshot(self.path)
        stat = os.stat

        def failed_stat(path, *args, **kwargs):
            if isinstance(path, (str, os.PathLike)) and Path(path) == self.path:
                raise OSError(errno.EIO, "Controlled timeline I/O failure")
            return stat(path, *args, **kwargs)

        with patch.object(os, "stat", failed_stat):
            self.zone()
            self.assert_pending()
        self.assert_repaired_poll(before)

    @unittest.skipUnless(os.name == "posix" and os.geteuid() != 0, "Needs file permissions")
    def test_file_read_denial_retries_without_selecting_bundle(self):
        self.write(self.path)
        self.write(self.bundle / self.path.name, BUNDLE)
        before = self.snapshot(self.path)
        with deny(self.path):
            with self.assertRaises(PermissionError):
                self.path.read_bytes()
            self.zone()
            self.assert_pending()
        self.assert_repaired_poll(before)

    def test_decode_failure_recovers_with_equal_size_and_timestamp(self):
        self.write(self.path, USER.replace(b"6", b"\xff", 1))
        self.write(self.bundle / self.path.name, BUNDLE)
        before = self.path.stat()
        self.zone()
        self.assert_pending()
        self.write(self.path)
        self.assertEqual(self.path.stat().st_size, before.st_size)
        self.assertEqual(self.path.stat().st_mtime_ns, before.st_mtime_ns)
        self.poll()
        self.assert_schedule()
        self.assertEqual(self.loads.call_count, 2)

    def test_no_entries_is_accepted_on_normal_load(self):
        self.write(self.path, b"# no cues\n")
        self.write(self.bundle / self.path.name, BUNDLE)
        self.zone()
        self.assert_schedule(None)
        self.assertEqual(self.window._timeline_fight, TAG)
        self.logs.assert_not_called()
        self.poll()
        self.assertEqual(self.loads.call_count, 1)

    @unittest.skipUnless(os.name == "posix" and os.geteuid() != 0, "Needs directory permissions")
    def test_preserve_time_denial_keeps_schedule_origin_and_source(self):
        self.write(self.path)
        self.write(self.bundle / self.path.name, BUNDLE)
        self.zone()
        now = [1000.0]
        with patch("time.monotonic", side_effect=lambda: now[0]):
            self.combat()
            now[0] += 5
            origin = self.window._timeline._t0
            with deny(self.local):
                self.window._load_timeline_for_zone(ZONE, preserve_time=True)
            self.assert_schedule()
            self.assertEqual(self.window._timeline._t0, origin)
            self.assertEqual(self.window._timeline.current_time(), 5)
            self.assertTrue(self.window._timeline.is_active())
            self.assertEqual(self.window._timeline_fight, TAG)
            self.assertFalse(self.window._timeline_from_cactbot)
        self.assertEqual(self.logs.call_count, 1)

    def test_preserve_time_missing_or_empty_replacement_keeps_schedule(self):
        self.write(self.path)
        self.zone()
        self.combat()
        origin = self.window._timeline._t0
        for empty in (False, True):
            with self.subTest(empty=empty):
                if empty:
                    self.write(self.path, b"# no cues\n")
                else:
                    self.path.unlink()
                self.window._load_timeline_for_zone(ZONE, preserve_time=True)
                self.assert_schedule()
                self.assertEqual(self.window._timeline._t0, origin)
                self.assertTrue(self.window._timeline.is_active())
                self.assertEqual(self.window._timeline_fight, TAG)

    def test_cactbot_off_ignores_index_files_downloads_and_completion(self):
        self.write(self.path)
        self.write(ac.CACTBOT_TIMELINES_FILE,
                   b'{"42": {"tag": "Indexed", "txt_path": "indexed.txt"}}')
        self.write(self.local / f"{TAG}.cactbot.cache.txt", BUNDLE)
        self.write(self.bundle / f"{TAG}.cactbot.txt", BUNDLE)
        self.zone()
        self.window._current_zone_id = 42
        self.assertEqual(self.window._cactbot_zone_entry(), ())
        self.window._cactbot_tl_signal.emit(TAG)
        self.assert_schedule()
        self.assertEqual(self.loads.call_count, 1)
        self.assertFalse(self.window._timeline_from_cactbot)
        self.fetches.assert_not_called()

    @unittest.skipUnless(os.name == "posix" and os.geteuid() != 0, "Needs directory permissions")
    def test_cactbot_cache_denial_retries_before_competing_bundle(self):
        self.window._cactbot_mode = True
        cache = self.local / f"{TAG}.cactbot.cache.txt"
        self.write(cache)
        self.write(self.bundle / f"{TAG}.cactbot.txt", BUNDLE)
        before = self.snapshot(cache)
        with deny(self.local):
            self.zone()
            self.assert_pending()
            self.fetches.assert_not_called()
        self.poll()
        self.assert_schedule()
        self.assertTrue(self.window._timeline_from_cactbot)
        self.assertEqual(self.window._timeline_fight, TAG)
        self.assertEqual(self.snapshot(cache), before)
        self.assertEqual(self.loads.call_count, 2)

    @unittest.skipUnless(os.name == "posix" and os.geteuid() != 0, "Needs directory permissions")
    def test_cactbot_bundle_denial_retries_before_local_fallback(self):
        self.window._cactbot_mode = True
        self.write(self.path)
        self.write(self.bundle / f"{TAG}.cactbot.txt", BUNDLE)
        with deny(self.bundle):
            self.zone()
            self.assert_pending()
            self.fetches.assert_not_called()
        self.poll()
        self.assert_schedule("Pack cue")
        self.assertTrue(self.window._timeline_from_cactbot)
        self.assertEqual(self.loads.call_count, 2)

    def test_cactbot_absence_falls_back_then_matching_completion_preserves_clock(self):
        self.window._cactbot_mode = True
        self.write(self.path)
        self.zone()
        self.assert_schedule()
        self.assertFalse(self.window._timeline_from_cactbot)
        self.assertEqual(self.fetches.call_count, 1)
        self.write(self.local / f"{TAG}.cactbot.cache.txt", BUNDLE)
        now = [1000.0]
        with patch("time.monotonic", side_effect=lambda: now[0]):
            self.combat()
            origin = self.window._timeline._t0
            now[0] += 5
            self.window._cactbot_tl_signal.emit("Other fight")
            self.assertEqual(self.loads.call_count, 1)
            self.window._cactbot_tl_signal.emit(TAG)
            self.assert_schedule("Pack cue")
            self.assertTrue(self.window._timeline_from_cactbot)
            self.assertEqual(self.window._timeline._t0, origin)
            self.assertEqual(self.window._timeline.current_time(), 5)
            self.assertEqual(self.loads.call_count, 2)


if __name__ == "__main__":
    unittest.main()
