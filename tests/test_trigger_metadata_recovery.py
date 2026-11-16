from contextlib import ExitStack, contextmanager
from copy import deepcopy
import errno
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtTest import QSignalSpy
from PyQt6.QtWidgets import QApplication, QPushButton

from nyaatriggers import app_common as ac, updater_ui
from nyaatriggers.sequential import SequentialRunner
from nyaatriggers.trigger_profiles import apply_choices
from tests.test_trigger_file_recovery import TriggerHost


OLD = "1d837552-e81a-5351-8a74-266290ad62a6"
CURRENT = "dd5a2525-2fb3-4977-8207-0d8dff0f47d8"
PERMISSIONS = os.name == "posix" and os.geteuid() != 0


@contextmanager
def deny(path):
    mode = path.stat().st_mode & 0o777
    try:
        path.chmod(0)
        yield
    finally:
        try:
            path.chmod(mode)
        except FileNotFoundError:
            pass


class MetadataHost(TriggerHost, updater_ui.UpdaterUiMixin):
    _trig_update_signal = pyqtSignal(object, str)

    def __init__(self):
        super().__init__()
        self._trig_dl_in_flight = {}
        self._trig_update_signal.connect(self._on_trig_update_result)


class TriggerMetadataRecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.bundle = self.root / "bundle"
        self.storage = self.root / "storage"
        self.bundle.mkdir()
        self.storage.mkdir()
        for name in ("TRIGGERS_LOCAL_FILE", "_REPO_TRIGGERS_FILE", "_REPO_RETIRED_FILE",
                     "_REPO_TRIGGERS_VERSION", "_SETTINGS_FILE"):
            self.enterContext(patch.object(ac, name, self.storage / name))
        for name in ("TRIGGERS_FILE", "RETIRED_FILE"):
            self.enterContext(patch.object(ac, name, self.bundle / name))
        self.enterContext(patch.object(ac, "log_drop"))
        self.enterContext(patch.object(ac.QMessageBox, "information"))
        self.enterContext(patch.object(ac.QMessageBox, "warning"))
        self.enterContext(patch.object(ac.updater, "is_frozen", return_value=False))
        self.host = MetadataHost()
        self.host._active_profile_id = "saved"
        self.addCleanup(self.host.deleteLater)
        self.addCleanup(self.host._clear_status_timers)
        self.addCleanup(self.host._clear_seq_runners)
        self.stamp = 1_700_000_000_000_000_000

    def write(self, path, value, *, stamp=None):
        path.write_text(json.dumps(value), encoding="utf-8")
        stamp = self.stamp if stamp is None else stamp
        os.utime(path, ns=(stamp, stamp))

    def seed(self, *, download=False):
        rows = [{"id": CURRENT, "tts_text": "Current wording", "enabled": False,
                 "log_type": "20", "ability_id": "9374", "cooldown_s": 0},
                {"id": "warning", "tts_text": "Move", "log_type": "26",
                 "ability_id": "ABC", "expiry_warn_s": 5, "cooldown_s": 120},
                {"id": "sequence", "tts_text": "Away", "log_type": "20", "ability_id": "8F00",
                 "sequence": [{"log_type": "21", "ability_id": "8F01"}]}]
        self.write(ac.TRIGGERS_FILE, rows)
        self.write(ac.RETIRED_FILE, {"retired": [{"id": OLD, "replaced_by": [CURRENT]}]})
        self.write(ac.TRIGGERS_LOCAL_FILE, {
            "triggers": [{"id": OLD, "enabled": True, "tts_text": "Saved wording",
                          "log_type": "20", "ability_id": "9374", "cooldown_s": 0},
                         {"id": CURRENT, "enabled": True}],
            "folders": [{"id": "saved", "name": "Saved"}], "deleted": ["removed"]})
        if download:
            self.write(ac._REPO_TRIGGERS_FILE, [{**rows[0], "tts_text": "Download wording"}])
            self.write(ac._REPO_RETIRED_FILE, {"retired": [{"id": OLD, "replaced_by": [CURRENT]},
                                                         "newer-retirement"]})
            self.write(ac._REPO_TRIGGERS_VERSION, ac._VERSION, stamp=self.stamp + 1_000_000_000)

    def watched_change(self):
        st = ac.TRIGGERS_LOCAL_FILE.stat()
        os.utime(ac.TRIGGERS_LOCAL_FILE, ns=(st.st_atime_ns, st.st_mtime_ns + 1))

    def wait_for_signals(self, spy, count):
        deadline = time.monotonic() + 5
        while len(spy) < count:
            remaining = int((deadline - time.monotonic()) * 1000)
            self.assertGreater(remaining, 0, "Expected signal was not emitted")
            spy.wait(min(remaining, 20))

    def snapshot(self):
        names = ("_triggers", "_retired_ids", "_trigger_replacements", "_official_triggers",
                 "_official_ids", "_local_ids", "_deleted_ids", "_folders", "_local_trigger_baseline",
                 "_active_profile_id", "_local_corrupt", "_local_conflict_warned")
        identities = {name: getattr(self.host, name) for name in names}
        values = {name: deepcopy(identities[name]) for name in ("_trigger_replacements", "_folders")}
        live = deepcopy([trigger.to_dict() for trigger in self.host._triggers])
        official = deepcopy({ident: trigger.to_dict()
                             for ident, trigger in self.host._official_triggers.items()})
        return identities, values, live, official

    def assert_retained(self, before):
        identities, values, live, official = before
        for name, value in identities.items():
            self.assertIs(getattr(self.host, name), value, name)
        for name, value in values.items():
            self.assertEqual(getattr(self.host, name), value, name)
        self.assertEqual([trigger.to_dict() for trigger in self.host._triggers], live)
        self.assertEqual({ident: trigger.to_dict()
                          for ident, trigger in self.host._official_triggers.items()}, official)
        self.assertEqual(self.host._triggers_mtime, ())

    def assert_settled(self):
        self.assertEqual(self.host._triggers_mtime, self.host._trigger_files_stamp())
        count = self.host.refreshes
        self.host._maybe_reload_triggers()
        self.assertEqual(self.host.refreshes, count)

    @unittest.skipUnless(PERMISSIONS, "Needs file permissions")
    def test_retirement_denial_retains_live_state_and_retries_unchanged_repair(self):
        for downloaded in (False, True):
            for method in ("_load_triggers", "_maybe_reload_triggers"):
                with self.subTest(downloaded=downloaded, method=method):
                    self.seed(download=downloaded)
                    self.host._load_triggers()
                    before = self.snapshot()
                    local = ac.TRIGGERS_LOCAL_FILE.read_bytes()
                    self.watched_change()
                    stamp = self.host._trigger_files_stamp()
                    path = ac._REPO_RETIRED_FILE if downloaded else ac.RETIRED_FILE
                    with deny(path):
                        with self.assertRaises(PermissionError):
                            path.read_bytes()
                        for _ in range(2):
                            getattr(self.host, method)()
                            self.assert_retained(before)
                    self.assertEqual(self.host._trigger_files_stamp(), stamp)
                    self.host._maybe_reload_triggers()
                    self.assertNotIn(OLD, {t.id for t in self.host._triggers})
                    self.assertEqual(self.host._trigger_replacements, {OLD: [CURRENT]})
                    self.assertEqual(ac.TRIGGERS_LOCAL_FILE.read_bytes(), local)
                    if downloaded:
                        self.assertIn("newer-retirement", self.host._retired_ids)
                    self.assert_settled()

    @unittest.skipUnless(PERMISSIONS, "Needs file permissions")
    def test_retirement_denial_preserves_cooldown_armed_sequence_and_profile_mapping(self):
        self.seed()
        self.host._load_triggers()
        current, warning, sequence = self.host._triggers
        warning._last_fired["older-effect"] = 12
        self.host._arm_status_timer(warning, {}, [
            "26", "ts", "ABC", "Effect", "5.001", "40000001", "Boss", "10FF0001", "Player"])
        status = self.host._status_timers[0]
        runner = SequentialRunner(sequence, {}, self.host._on_seq_complete,
                                  self.host._on_seq_expire, self.host)
        self.host._seq_runners.append(runner)
        with deny(ac.RETIRED_FILE):
            self.host._load_triggers()
            self.host._engine_disabled = {source: set() for source in ("cactbot", "triggevent", "triggernometry")}
            self.host._settings = {}
            apply_choices(self.host, {"name": "Saved", "local": {OLD: {"enabled": True, "text": "Profile wording"}},
                                      "engines": {}})
            self.assertEqual(current.tts_text, "Profile wording")
        self.assertIs(self.host._triggers[0], current)
        self.assertEqual(warning._last_fired, {"older-effect": 12})
        self.assertIs(self.host._status_timers[0], status)
        self.assertIs(self.host._seq_runners[0], runner)
        self.host._maybe_reload_triggers()
        self.assertTrue(runner.try_advance([
            "21", "ts", "40000001", "Boss", "8F01", "Ability", "10FF0001", "Player"]))
        self.assertTrue(QSignalSpy(status._timer.timeout).wait(1000))
        self.assertEqual(self.host.fired, [("sequence", "Away"), ("warning", "Move")])

    @unittest.skipUnless(PERMISSIONS, "Needs file permissions")
    def test_startup_retirement_fallback_retries_without_definition_changes(self):
        for downloaded in (False, True):
            with self.subTest(downloaded=downloaded):
                self.seed(download=downloaded)
                host = MetadataHost()
                self.addCleanup(host.deleteLater)
                path = ac._REPO_RETIRED_FILE if downloaded else ac.RETIRED_FILE
                with deny(path):
                    host._load_triggers()
                self.assertTrue(host._triggers)
                self.assertEqual(host._triggers_mtime, ())
                host._maybe_reload_triggers()
                self.assertNotIn(OLD, {t.id for t in host._triggers})
                self.assertEqual(host._trigger_replacements, {OLD: [CURRENT]})
                if downloaded:
                    self.assertIn("newer-retirement", host._retired_ids)
                self.assertEqual(host._triggers_mtime, host._trigger_files_stamp())

    @unittest.skipUnless(PERMISSIONS, "Needs file permissions")
    def test_version_read_denial_preserves_downloads_live_state_and_retry(self):
        for frozen in (False, True):
            for method in ("_load_triggers", "_maybe_reload_triggers"):
                with self.subTest(frozen=frozen, method=method), \
                        patch.object(ac.updater, "is_frozen", return_value=frozen):
                    self.seed(download=True)
                    self.host._load_triggers()
                    before = self.snapshot()
                    files = {p: p.read_bytes() for p in (ac._REPO_TRIGGERS_FILE,
                             ac._REPO_RETIRED_FILE, ac._REPO_TRIGGERS_VERSION)}
                    self.watched_change()
                    stamp = self.host._trigger_files_stamp()
                    with deny(ac._REPO_TRIGGERS_VERSION):
                        getattr(self.host, method)()
                        self.assert_retained(before)
                    for path, content in files.items():
                        self.assertEqual(path.read_bytes(), content)
                    self.assertEqual(self.host._trigger_files_stamp(), stamp)
                    self.host._maybe_reload_triggers()
                    self.assertEqual(self.host._official_triggers[CURRENT].tts_text, "Download wording")
                    self.assert_settled()

    def test_operational_version_stat_errors_are_not_staleness_or_absence(self):
        self.seed(download=True)
        self.host._load_triggers()
        real_stat = Path.stat
        for path in (ac._REPO_TRIGGERS_VERSION, ac.TRIGGERS_FILE, ac.RETIRED_FILE):
            for code in (errno.EIO, errno.EACCES):
                with self.subTest(path=path.name, code=code):
                    before = self.snapshot()

                    def fail(target, *args, **kwargs):
                        if target == path:
                            raise OSError(code, "Storage unavailable", str(target))
                        return real_stat(target, *args, **kwargs)

                    with patch.object(Path, "stat", fail):
                        with self.assertRaises(OSError):
                            ac._repo_download_version()
                        self.host._load_triggers()
                        self.assert_retained(before)
                    self.assertTrue(ac._REPO_TRIGGERS_FILE.is_file())
                    self.host._maybe_reload_triggers()
                    self.assert_settled()

    @unittest.skipUnless(PERMISSIONS, "Needs file permissions")
    def test_metadata_retry_applies_equal_size_equal_time_edits_after_repair(self):
        for downloaded in (False, True):
            with self.subTest(downloaded=downloaded):
                self.seed(download=downloaded)
                self.host._load_triggers()
                before = self.snapshot()
                original = ac.TRIGGERS_LOCAL_FILE.read_bytes()
                stamp = self.host._trigger_files_stamp()
                path = ac._REPO_TRIGGERS_VERSION if downloaded else ac.RETIRED_FILE
                with deny(path):
                    self.host._load_triggers()
                    self.assert_retained(before)
                updated = original.replace(b"Saved wording", b"Later wording")
                self.assertNotEqual(updated, original)
                self.assertEqual(len(updated), len(original))
                ac.TRIGGERS_LOCAL_FILE.write_bytes(updated)
                os.utime(ac.TRIGGERS_LOCAL_FILE, ns=(self.stamp, self.stamp))
                self.assertEqual(self.host._trigger_files_stamp(), stamp)
                self.host._maybe_reload_triggers()
                self.assertEqual(self.host._triggers[0].tts_text, "Later wording")
                self.assertNotIn(OLD, {t.id for t in self.host._triggers})
                self.assertEqual(ac.TRIGGERS_LOCAL_FILE.read_bytes(), updated)
                self.assert_settled()

    def test_absent_invalid_and_incompatible_versions_keep_cleanup_policy(self):
        for value in (None, "{invalid", "12", '"0.0.0"'):
            with self.subTest(value=value):
                self.seed(download=True)
                if value is None:
                    ac._REPO_TRIGGERS_VERSION.unlink()
                else:
                    ac._REPO_TRIGGERS_VERSION.write_text(value)
                self.host._load_triggers()
                for path in (ac._REPO_TRIGGERS_FILE, ac._REPO_RETIRED_FILE, ac._REPO_TRIGGERS_VERSION):
                    self.assertFalse(path.exists())
                self.assertEqual(self.host._official_triggers[CURRENT].tts_text, "Current wording")
                self.assert_settled()

    def test_source_bundle_staleness_and_frozen_matching_policy(self):
        for newer in (ac.TRIGGERS_FILE, ac.RETIRED_FILE):
            for frozen in (False, True):
                with self.subTest(newer=newer.name, frozen=frozen):
                    self.seed(download=True)
                    os.utime(newer, ns=(self.stamp + 2_000_000_000,) * 2)
                    with patch.object(ac.updater, "is_frozen", return_value=frozen):
                        self.host._load_triggers()
                    self.assertEqual(ac._REPO_TRIGGERS_FILE.exists(), frozen)
                    self.assertEqual(self.host._official_triggers[CURRENT].tts_text,
                                     "Download wording" if frozen else "Current wording")

    def test_optional_retirement_absence_and_invalid_content_still_clear_prior_rules(self):
        for value in (None, "{invalid", '{"retired":{}}', '"invalid"', '{"retired":[]}'):
            with self.subTest(value=value):
                self.seed()
                self.host._load_triggers()
                if value is None:
                    ac.RETIRED_FILE.unlink()
                else:
                    ac.RETIRED_FILE.write_text(value)
                self.host._load_triggers()
                self.assertEqual(self.host._retired_ids, set())
                self.assertEqual(self.host._trigger_replacements, {})
                self.assertIn(OLD, {t.id for t in self.host._triggers})
                self.assert_settled()

    def test_missing_downloaded_retirements_use_bundle_but_invalid_catalog_remains_optional(self):
        self.seed(download=True)
        ac._REPO_RETIRED_FILE.unlink()
        self.host._load_triggers()
        self.assertEqual(self.host._retired_ids, {OLD})
        ac._REPO_RETIRED_FILE.write_text("{invalid")
        self.host._load_triggers()
        self.assertEqual(self.host._retired_ids, set())
        self.assertEqual(self.host._trigger_replacements, {})

    def run_download(self, *, previous=None, retirement_failure=False,
                     retirement_write_failure=False, stamp_failure=False):
        self.seed()
        self.host._load_triggers()
        before = self.snapshot()
        local = ac.TRIGGERS_LOCAL_FILE.read_bytes()
        prior_retirements = {"retired": ["prior-retirement"]}
        if retirement_failure or retirement_write_failure:
            self.write(ac._REPO_RETIRED_FILE, prior_retirements)
        if previous is not None:
            self.write(ac._REPO_TRIGGERS_VERSION, previous, stamp=self.stamp + 1_000_000_000)
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        fresh = [{"id": "fresh", "tts_text": "Fresh download"}]

        def fetch(request, limit):
            if request.full_url.endswith("triggers.json"):
                return json.dumps(fresh).encode()
            entered.set()
            if not release.wait(5):
                raise TimeoutError("Download was not released")
            if retirement_failure:
                raise OSError("Retirement endpoint unavailable")
            return b'{"retired":[]}'

        threads = []
        actual_thread, actual_write = threading.Thread, ac._atomic_write_json

        def thread(**kwargs):
            worker = actual_thread(**kwargs)
            threads.append(worker)
            return worker

        def write(path, value, **kwargs):
            if retirement_write_failure and path == ac._REPO_RETIRED_FILE:
                raise OSError("Retirement publication failed")
            if stamp_failure and path == ac._REPO_TRIGGERS_VERSION:
                raise OSError("Stamp publication failed")
            actual_write(path, value, **kwargs)

        with patch.object(updater_ui, "fetch_bytes", fetch), \
                patch.object(updater_ui.threading, "Thread", thread), \
                patch.object(updater_ui, "_atomic_write_json", write):
            button = QPushButton("Update Triggers")
            self.addCleanup(button.deleteLater)
            spy = QSignalSpy(self.host._trig_update_signal)
            try:
                self.host._download_repo_triggers(button, "Update Triggers")
                self.assertTrue(entered.wait(5))
                for method in ("_maybe_reload_triggers", "_load_triggers"):
                    getattr(self.host, method)()
                    self.assertTrue(ac._REPO_TRIGGERS_FILE.is_file())
                    self.assertEqual(json.loads(ac._REPO_TRIGGERS_FILE.read_text()), fresh)
                    self.assert_retained(before)
                release.set()
                self.wait_for_signals(spy, 1)
                self.app.processEvents()
            finally:
                release.set()
                for worker in threads:
                    worker.join(5)
                    self.assertFalse(worker.is_alive())
                self.app.processEvents()
        self.assertFalse(self.host._trig_dl_in_flight)
        self.assertTrue(button.isEnabled())
        self.assertEqual(button.text(), "Update Triggers")
        self.assertEqual(ac.TRIGGERS_LOCAL_FILE.read_bytes(), local)
        if stamp_failure:
            self.assertTrue(spy[0][1].startswith("err:"))
            self.assertFalse(ac._REPO_TRIGGERS_FILE.exists())
            self.assertEqual(self.host._official_triggers[CURRENT].tts_text, "Current wording")
        else:
            self.assertTrue(spy[0][1].startswith("ok:"))
            self.assertIn("fresh", self.host._official_triggers)
            self.assertEqual(self.host._official_triggers["fresh"].tts_text, "Fresh download")
            self.assertTrue(ac._REPO_TRIGGERS_FILE.is_file())
            self.assertEqual(json.loads(ac._REPO_TRIGGERS_VERSION.read_text()), ac._VERSION)
            if retirement_failure or retirement_write_failure:
                self.assertEqual(json.loads(ac._REPO_RETIRED_FILE.read_text()), prior_retirements)
        self.assert_settled()

    def test_publication_interleaving_without_stamp(self):
        self.run_download()

    def test_publication_interleaving_with_incompatible_stamp(self):
        self.run_download(previous="0.0.0")

    def test_publication_interleaving_with_compatible_stamp(self):
        self.run_download(previous=ac._VERSION)

    def test_optional_retirement_fetch_failure_still_completes_download(self):
        self.run_download(previous=ac._VERSION, retirement_failure=True)

    def test_optional_retirement_write_failure_still_completes_download(self):
        self.run_download(previous=ac._VERSION, retirement_write_failure=True)

    def test_failed_final_publication_releases_deferral_and_cleans_incomplete_download(self):
        self.run_download(stamp_failure=True)

    def test_failed_final_publication_still_rejects_incompatible_prior_stamp(self):
        self.run_download(previous="0.0.0", stamp_failure=True)

    def test_failed_final_publication_rejects_compatible_prior_stamp(self):
        self.run_download(previous=ac._VERSION, stamp_failure=True)

    def test_buttons_serialize_and_failed_retry_releases_deferral(self):
        self.seed()
        self.host._load_triggers()
        before = self.snapshot()
        buttons = [QPushButton("Update"), QPushButton("Restore")]
        self.host._update_trig_btn, self.host._restore_trig_btn = buttons
        workers = []
        entered = [threading.Event(), threading.Event()]
        release = [threading.Event(), threading.Event()]
        assigned = []
        worker_id = threading.local()
        lock = threading.Lock()
        actual_thread, actual_write = threading.Thread, ac._atomic_write_json
        for button in buttons:
            self.addCleanup(button.deleteLater)

        def capture(**kwargs):
            worker = actual_thread(**kwargs)
            workers.append(worker)
            return worker

        def fetch(request, limit):
            if request.full_url.endswith("triggers.json"):
                with lock:
                    worker_id.index = len(assigned)
                    assigned.append(worker_id.index)
                return b'[{"id":"fresh"}]'
            index = worker_id.index
            entered[index].set()
            if not release[index].wait(5):
                raise TimeoutError("Download was not released")
            return b'{"retired":[]}'

        def write(path, value, **kwargs):
            if path == ac._REPO_TRIGGERS_VERSION and worker_id.index == 1:
                raise OSError("Second stamp publication failed")
            actual_write(path, value, **kwargs)

        with patch.object(updater_ui.threading, "Thread", capture), \
                patch.object(updater_ui, "fetch_bytes", fetch), \
                patch.object(updater_ui, "_atomic_write_json", write):
            spy = QSignalSpy(self.host._trig_update_signal)
            try:
                self.host._download_repo_triggers(buttons[0], "Update")
                self.assertTrue(entered[0].wait(5))
                self.host._download_repo_triggers(buttons[1], "Restore")
                self.host._download_repo_triggers(buttons[0], "Duplicate")
                self.assertEqual(len(workers), 1)
                self.assertFalse(entered[1].is_set())
                self.assertTrue(all(not button.isEnabled() for button in buttons))
                self.host._load_triggers()
                self.assert_retained(before)
                release[0].set()
                self.wait_for_signals(spy, 1)
                self.app.processEvents()
                self.assertTrue(all(button.isEnabled() for button in buttons))
                before = self.snapshot()
                self.host._download_repo_triggers(buttons[1], "Restore")
                self.assertTrue(entered[1].wait(5))
                self.host._load_triggers()
                self.assert_retained(before)
                self.assertTrue(all(not button.isEnabled() for button in buttons))
                release[1].set()
                self.wait_for_signals(spy, 2)
                self.app.processEvents()
            finally:
                for event in release:
                    event.set()
                for worker in workers:
                    worker.join(5)
                    self.assertFalse(worker.is_alive())
                self.app.processEvents()
        self.assertTrue(spy[0][1].startswith("ok:"))
        self.assertTrue(spy[1][1].startswith("err:"))
        self.assertFalse(self.host._trig_dl_in_flight)
        self.assertTrue(all(button.isEnabled() for button in buttons))
        self.assertEqual([button.text() for button in buttons], ["Update", "Restore"])
        self.assertEqual(self.host._official_ids, {CURRENT, "warning", "sequence"})
        self.assert_settled()

    def test_thread_start_failure_releases_deferral(self):
        self.seed()
        self.host._load_triggers()
        button = QPushButton("Update")
        self.addCleanup(button.deleteLater)
        with patch.object(updater_ui.threading, "Thread", return_value=Mock(start=Mock(side_effect=OSError))):
            self.host._download_repo_triggers(button, "Update")
        self.assertFalse(self.host._trig_dl_in_flight)
        self.assertTrue(button.isEnabled())
        self.watched_change()
        self.host._maybe_reload_triggers()
        self.assert_settled()

    @unittest.skipUnless(PERMISSIONS, "Needs file permissions")
    def test_actual_constructor_and_dispatch_recover_metadata_without_duplicate_speech(self):
        from nyaatriggers import main_window as mw
        from nyaatriggers.telesto_client import TelestoClient
        from nyaatriggers.ui import engines

        for kind in ("bundled_retirement", "downloaded_retirement", "version"):
            with self.subTest(kind=kind), ExitStack() as stack:
                self.seed(download=kind != "bundled_retirement")
                for module, name, value in (
                    (ac, "_DATA_DIR", self.root), (mw, "_DATA_DIR", self.root),
                    (ac, "_TRIGGERNOMETRY_INVENTORY_CACHE", self.root / "tn.json"),
                    (ac, "_TRIGGEVENT_INVENTORY_CACHE", self.root / "tv.json"),
                    (ac, "_TRIGGEVENT_INVENTORY_SEED", self.root / "seed.json")):
                    stack.enter_context(patch.object(module, name, value))
                stack.enter_context(patch("nyaatriggers.ui.engines._tn_packs_dir", return_value=self.root / "packs"))

                def settings(window):
                    window._settings.update(tts_engine="system", auto_connect=False,
                                            auto_check_updates=False, local_enabled=True, ui_language="en")

                stack.enter_context(patch.object(mw.MainWindow, "_load_settings", settings))
                for name in ("_load_timeline_for_zone", "_refresh_telesto_party"):
                    stack.enter_context(patch.object(mw.MainWindow, name))
                for target in ((mw.PluginLink, "start"), (mw.QTimer, "singleShot"),
                               (mw, "kokoro_ready"), (mw, "_ensure_worker"), (mw, "set_engine"),
                               (TelestoClient, "start")):
                    stack.enter_context(patch.object(*target))
                # Native availability is deliberately absent in this storage fixture.
                stack.enter_context(patch.object(mw.TriggeventBridge, "is_available", return_value=False))
                if engines.TriggernometryBridge is not None:
                    stack.enter_context(patch.object(engines.TriggernometryBridge, "is_available", return_value=False))
                stack.enter_context(patch("nyaatriggers.ui.recap_widgets.RecapIcons._pump"))
                path = {"bundled_retirement": ac.RETIRED_FILE,
                        "downloaded_retirement": ac._REPO_RETIRED_FILE,
                        "version": ac._REPO_TRIGGERS_VERSION}[kind]
                local = ac.TRIGGERS_LOCAL_FILE.read_bytes()
                with deny(path):
                    window = mw.MainWindow()
                try:
                    self.assertEqual(window._triggers_mtime, ())
                    if kind == "version":
                        self.assertTrue(ac._REPO_TRIGGERS_FILE.is_file())
                        self.assertEqual(window._official_triggers[CURRENT].tts_text, "Current wording")
                    window._maybe_reload_triggers()
                    self.assertNotIn(OLD, {t.id for t in window._triggers})
                    self.assertEqual(window._trigger_replacements, {OLD: [CURRENT]})
                    self.assertEqual(window._official_triggers[CURRENT].tts_text,
                                     "Current wording" if kind == "bundled_retirement" else "Download wording")
                    window._connected = window._local_enabled = True
                    window._awaiting_zone_metadata = False
                    window._zone_aliases = ("Everkeep (Extreme)",)
                    fields = ["20", "ts", "40000001", "Boss", "9374", "Duty's Edge", "10FF0001", "Player"]
                    with patch.object(mw, "speak") as speech:
                        window._dispatch_log_line(fields, "|".join(fields))
                        self.assertEqual(speech.call_count, 1)
                    with deny(path):
                        window._load_triggers()
                        with patch.object(mw, "speak") as speech:
                            window._dispatch_log_line(fields, "|".join(fields))
                            self.assertEqual(speech.call_count, 1)
                    window._maybe_reload_triggers()
                    self.assertEqual(window._triggers_mtime, window._trigger_files_stamp())
                    self.assertEqual(ac.TRIGGERS_LOCAL_FILE.read_bytes(), local)
                finally:
                    window.close()
                    window._prog_sessions.close()


if __name__ == "__main__":
    unittest.main()
