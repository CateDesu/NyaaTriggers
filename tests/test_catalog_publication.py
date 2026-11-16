from contextlib import ExitStack
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QCoreApplication, QEvent, QObject, QTimer, pyqtSignal
from PyQt6.QtTest import QSignalSpy
from PyQt6.QtWidgets import QApplication

from nyaatriggers import app_common as ac, main_window as mw, updater_ui
from nyaatriggers.telesto_client import TelestoClient
from nyaatriggers.ui import engines


OLD = "1d837552-e81a-5351-8a74-266290ad62a6"
CURRENT = "dd5a2525-2fb3-4977-8207-0d8dff0f47d8"
OLD_ROW = {"id": OLD, "name": "Duty's Edge", "fight": "Zoraal Ja EX", "enabled": False,
           "log_type": "20", "ability_id": "9374", "tts_text": "Stack", "cooldown_s": 5,
           "zone_regex": r"^Everkeep \(Extreme\)$"}
CURRENT_ROW = {**OLD_ROW, "id": CURRENT, "tts_text": "Stack - Multiple Hits"}
CATALOGS = {
    "A": ([OLD_ROW], {"retired": []}),
    "B": ([CURRENT_ROW], {"retired": [{"id": OLD, "replaced_by": [CURRENT]}]}),
}


class PublicationEvents(QObject):
    ready = pyqtSignal(int, str)


class CatalogPublicationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.root = (self.fixture_root if hasattr(self, "fixture_root")
                     else Path(self.enterContext(tempfile.TemporaryDirectory())))
        for name in ("TRIGGERS_FILE", "RETIRED_FILE", "TRIGGERS_LOCAL_FILE", "_REPO_TRIGGERS_FILE",
                     "_REPO_RETIRED_FILE", "_REPO_TRIGGERS_VERSION", "_SETTINGS_FILE",
                     "_TRIGGERNOMETRY_INVENTORY_CACHE", "_TRIGGEVENT_INVENTORY_CACHE",
                     "_TRIGGEVENT_INVENTORY_SEED"):
            self.enterContext(patch.object(ac, name, self.root / name))
        self.enterContext(patch.object(ac, "_DATA_DIR", self.root))
        self.enterContext(patch.object(mw, "_DATA_DIR", self.root))
        self.enterContext(patch.object(ac.updater, "is_frozen", return_value=False))
        self.enterContext(patch.object(engines, "_tn_packs_dir", return_value=self.root / "packs"))
        self.information = self.enterContext(patch.object(ac.QMessageBox, "information"))
        self.warning = self.enterContext(patch.object(ac.QMessageBox, "warning"))

        def settings(window):
            window._settings.update(tts_engine="system", auto_connect=False, auto_check_updates=False,
                                    local_enabled=True, ui_language="en", callouts_localized=False)

        self.enterContext(patch.object(mw.MainWindow, "_load_settings", settings))
        for name in ("_load_timeline_for_zone", "_refresh_telesto_party"):
            self.enterContext(patch.object(mw.MainWindow, name))
        for owner, name in ((mw.PluginLink, "start"), (mw.QTimer, "singleShot"), (mw, "kokoro_ready"),
                            (mw, "_ensure_worker"), (mw, "set_engine"), (TelestoClient, "start")):
            self.enterContext(patch.object(owner, name))
        # Native engines are deliberately unavailable in this catalog fixture.
        self.enterContext(patch.object(mw.TriggeventBridge, "is_available", return_value=False))
        self.enterContext(patch.object(engines.TriggernometryBridge, "is_available", return_value=False))
        self.enterContext(patch("nyaatriggers.ui.recap_widgets.RecapIcons._pump"))
        if not getattr(self, "reuse_files", False):
            self.write(ac.TRIGGERS_FILE, CATALOGS["A"][0])
            self.write(ac.RETIRED_FILE, CATALOGS["A"][1])
            self.write(ac.TRIGGERS_LOCAL_FILE, {
                "triggers": [{**OLD_ROW, "enabled": True, "tts_text": "Saved historical wording"},
                             {"id": CURRENT, "enabled": True}]})
        self.local = ac.TRIGGERS_LOCAL_FILE.read_bytes()
        self.windows = [mw.MainWindow()]
        self.window = self.windows[0]
        self.buttons = (self.window._update_trig_btn, self.window._restore_trig_btn)
        self.labels = [button.text() for button in self.buttons]
        self.events = PublicationEvents()
        self.phases = QSignalSpy(self.events.ready)
        self.results = QSignalSpy(self.window._trig_update_signal)
        self.workers = []
        self.gates = []
        self.timeouts = []
        self.publications = []
        self.worker_id = threading.local()
        self.catalogs = ("A", "B")
        self.failures = {}
        self.enterContext(patch.object(updater_ui, "threading", SimpleNamespace(Thread=self.capture)))
        self.enterContext(patch.object(updater_ui, "fetch_bytes", self.fetch))
        self.enterContext(patch.object(updater_ui, "_atomic_write_json", self.publish))
        self.addCleanup(self.cleanup_owned)

    def write(self, path, value):
        path.write_text(json.dumps(value), encoding="utf-8")
        os.utime(path, ns=(1_700_000_000_000_000_000,) * 2)

    def cleanup_owned(self):
        for gates in self.gates:
            for gate in gates.values():
                gate.set()
        for worker in self.workers:
            worker.join(5)
        self.app.processEvents()
        for window in self.windows:
            window.close()
            window._prog_sessions.close()
            window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.assertFalse(any(worker.is_alive() for worker in self.workers))
        self.assertEqual(self.timeouts, [])

    def capture(self, *, target, **kwargs):
        index = len(self.workers)
        self.gates.append({phase: threading.Event() for phase in ("triggers", "retired", "stamp")})

        def run():
            self.worker_id.index = index
            target()

        worker = threading.Thread(target=run, **kwargs)
        self.workers.append(worker)
        return worker

    def pause(self, phase):
        index = self.worker_id.index
        self.events.ready.emit(index, phase)
        if not self.gates[index][phase].wait(5):
            self.timeouts.append((index, phase))
            raise TimeoutError(f"Unreleased {phase} response")
        if self.failures.get(index) == phase:
            raise OSError(f"Controlled {phase} failure")

    def fetch(self, request, limit):
        phase = "triggers" if request.full_url.endswith("triggers.json") else "retired"
        self.pause(phase)
        triggers, retired = CATALOGS[self.catalogs[self.worker_id.index]]
        return json.dumps(triggers if phase == "triggers" else retired).encode()

    def publish(self, path, value, **kwargs):
        if path == ac._REPO_TRIGGERS_VERSION:
            self.pause("stamp")
        ac._atomic_write_json(path, value, **kwargs)
        self.publications.append((self.worker_id.index, path))

    def wait_for(self, spy, predicate):
        deadline = time.monotonic() + 5
        while not predicate():
            remaining = int((deadline - time.monotonic()) * 1000)
            self.assertGreater(remaining, 0, "Worker did not reach the expected phase")
            spy.wait(min(remaining, 20))

    def wait_phase(self, index, phase):
        self.wait_for(self.phases, lambda: [index, phase] in [list(row) for row in self.phases])

    def snapshot(self):
        return list(self.window._triggers), deepcopy([t.to_dict() for t in self.window._triggers])

    def assert_deferred(self, before, *, closed=False):
        self.assertTrue(self.window._trig_dl_in_flight)
        self.assertTrue(all(not button.isEnabled() for button in self.buttons))
        self.assertEqual(self.window._zone_redetect_timer.isActive(), not closed)
        self.window._zone_redetect_timer.timeout.emit()
        self.window._load_triggers()
        self.assertEqual(self.window._triggers_mtime, ())
        identities, values = before
        self.assertEqual([t.to_dict() for t in self.window._triggers], values)
        for current, previous in zip(self.window._triggers, identities):
            self.assertIs(current, previous)

    def start(self, button_index):
        self.assertTrue(self.buttons[button_index].isEnabled())
        index = len(self.workers)
        self.buttons[button_index].click()
        self.wait_phase(index, "triggers")
        self.assertTrue(self.workers[index].is_alive())
        return index

    def finish(self, index, *, closed=False):
        before = self.snapshot()
        for phase, following in (("triggers", "retired"), ("retired", "stamp")):
            self.assert_deferred(before, closed=closed)
            self.gates[index][phase].set()
            self.wait_phase(index, following)
        self.assert_deferred(before, closed=closed)
        self.gates[index]["stamp"].set()
        self.wait_result(index)

    def wait_result(self, index):
        self.wait_for(self.results, lambda: len(self.results) > index)
        self.app.processEvents()
        self.workers[index].join(5)
        self.assertFalse(self.workers[index].is_alive())
        self.assertFalse(self.window._trig_dl_in_flight)
        self.assertTrue(all(button.isEnabled() for button in self.buttons))
        self.assertEqual([button.text() for button in self.buttons], self.labels)

    def assert_catalog(self, name, *, window=None):
        window = window or self.window
        self.assertEqual([json.loads(path.read_text()) for path in
                          (ac._REPO_TRIGGERS_FILE, ac._REPO_RETIRED_FILE)], list(CATALOGS[name]))
        self.assertEqual(json.loads(ac._REPO_TRIGGERS_VERSION.read_text()), ac._VERSION)
        expected_id = OLD if name == "A" else CURRENT
        for reload in (window._poll_zone_and_triggers, window._load_triggers):
            reload()
            self.assertEqual([(t.id, t.enabled, t.tts_text) for t in window._triggers],
                             [(expected_id, True, "Saved historical wording")])
            self.assertEqual(window._triggers_mtime, window._trigger_files_stamp())
            self.assert_speech(window)
        self.assertEqual(ac.TRIGGERS_LOCAL_FILE.read_bytes(), self.local)

    def speech_texts(self, window=None):
        window = window or self.window
        window._connected = window._local_enabled = True
        window._awaiting_zone_metadata = False
        window._zone_aliases = ("Everkeep (Extreme)",)
        for trigger in window._triggers:
            trigger._last_fired.clear()
        fields = ["20", "ts", "40000001", "Boss", "9374", "Duty's Edge", "10FF0001", "Player"]
        with patch.object(mw, "speak") as speech:
            window._dispatch_log_line(fields, "|".join(fields))
        return [call.args[0] for call in speech.call_args_list]

    def assert_speech(self, window=None):
        self.assertEqual(self.speech_texts(window), ["Saved historical wording"])

    def check_buttons(self, first, catalogs):
        self.catalogs = catalogs
        before = self.snapshot()
        index = self.start(first)
        other = 1 - first
        self.buttons[other].click()
        self.buttons[first].click()
        self.window._update_triggers_data()
        self.window._restore_triggers_from_repo()
        self.assertEqual(len(self.workers), 1)
        self.window._on_trig_update_result(self.buttons[other], "ok:1")
        self.assertEqual(len(self.window._trig_dl_in_flight), 1)
        self.assert_deferred(before)
        self.finish(index)
        self.assert_catalog(catalogs[0])
        index = self.start(other)
        self.window._on_trig_update_result(self.buttons[first], "ok:1")
        self.assertEqual(len(self.window._trig_dl_in_flight), 1)
        self.finish(index)
        self.assert_catalog(catalogs[1])
        self.assertEqual([row[1] for row in self.results], ["ok:1", "ok:1"])
        self.assertEqual(self.publications, [(index, path) for index in range(2) for path in
                         (ac._REPO_TRIGGERS_FILE, ac._REPO_RETIRED_FILE, ac._REPO_TRIGGERS_VERSION)])
        self.assertEqual(self.information.call_count, 2)
        self.warning.assert_not_called()
        reopened = mw.MainWindow()
        self.windows.append(reopened)
        self.assert_catalog(catalogs[1], window=reopened)

    def test_update_then_restore_preserves_distinct_catalogs(self):
        self.check_buttons(0, ("A", "B"))

    def test_restore_then_update_preserves_distinct_catalogs(self):
        self.check_buttons(1, ("A", "B"))

    def test_update_then_restore_preserves_reverse_catalogs(self):
        self.check_buttons(0, ("B", "A"))

    def test_restore_then_update_preserves_reverse_catalogs(self):
        self.check_buttons(1, ("B", "A"))

    def check_preserved_failure(self, failure):
        self.finish(self.start(0))
        self.assert_catalog("A")
        paths = (ac._REPO_TRIGGERS_FILE, ac._REPO_RETIRED_FILE, ac._REPO_TRIGGERS_VERSION)
        files = {path: path.read_bytes() for path in paths}
        publications = list(self.publications)
        real_unlink = Path.unlink

        def unlink(path, *args, **kwargs):
            if path == ac._REPO_TRIGGERS_VERSION:
                raise PermissionError("Controlled stamp invalidation failure")
            return real_unlink(path, *args, **kwargs)

        def invalid(request, limit):
            raw = self.fetch(request, limit)
            return b'{"invalid":[]}' if request.full_url.endswith("triggers.json") else raw

        with ExitStack() as stack:
            if failure == "invalidation":
                stack.enter_context(patch.object(Path, "unlink", unlink))
            elif failure == "validation":
                stack.enter_context(patch.object(updater_ui, "fetch_bytes", invalid))
            else:
                self.failures[1] = "triggers"
            index = self.start(1)
            for gate in self.gates[index].values():
                gate.set()
            self.wait_result(index)
        self.assertTrue(self.results[index][1].startswith("err:"))
        self.assertEqual(self.publications, publications)
        self.assertEqual({path: path.read_bytes() for path in paths}, files)
        self.assert_catalog("A")
        self.assertEqual(self.warning.call_count, 1)

    def test_fetch_failure_preserves_complete_prior_catalog(self):
        self.check_preserved_failure("fetch")

    def test_invalid_triggers_preserve_complete_prior_catalog(self):
        self.check_preserved_failure("validation")

    def test_failed_stamp_invalidation_aborts_before_active_writes(self):
        self.check_preserved_failure("invalidation")

    def test_failed_first_active_write_falls_back_and_allows_retry(self):
        self.catalogs = ("A", "B", "B")
        self.finish(self.start(0))
        self.assert_catalog("A")

        def fail(path, value, **kwargs):
            if path == ac._REPO_TRIGGERS_FILE:
                self.assertFalse(ac._REPO_TRIGGERS_VERSION.exists())
                raise OSError("Controlled trigger write failure")
            self.publish(path, value, **kwargs)

        with patch.object(updater_ui, "_atomic_write_json", fail):
            index = self.start(1)
            self.gates[index]["triggers"].set()
            self.wait_result(index)
        self.assertTrue(self.results[index][1].startswith("err:Controlled"))
        self.assertFalse(ac._REPO_TRIGGERS_VERSION.exists())
        self.assertFalse(ac._REPO_TRIGGERS_FILE.exists())
        self.assertFalse(ac._REPO_RETIRED_FILE.exists())
        self.assertEqual([t.id for t in self.window._triggers], [OLD])
        self.assert_speech()
        self.finish(self.start(0))
        self.assert_catalog("B")

    def check_failure_retry(self, phase):
        self.catalogs = ("B", "B")
        self.failures[0] = phase
        index = self.start(0)
        self.buttons[1].click()
        self.assertEqual(len(self.workers), 1)
        before = self.snapshot()
        if phase == "stamp":
            for current, following in (("triggers", "retired"), ("retired", "stamp")):
                self.gates[index][current].set()
                self.wait_phase(index, following)
        self.assert_deferred(before)
        self.gates[index][phase].set()
        self.wait_result(index)
        self.assertTrue(self.results[0][1].startswith("err:Controlled"))
        self.assertEqual([t.id for t in self.window._triggers], [OLD])
        self.assert_speech()
        self.assertFalse(ac._REPO_TRIGGERS_FILE.exists())
        self.assertEqual(self.window._triggers_mtime, self.window._trigger_files_stamp())
        index = self.start(1)
        self.finish(index)
        self.assert_catalog("B")
        self.assertEqual(self.results[1][1], "ok:1")
        self.assertEqual(self.warning.call_count, 1)
        self.assertEqual(self.information.call_count, 1)

    def test_async_fetch_failure_restores_both_buttons_and_allows_retry(self):
        self.check_failure_retry("triggers")

    def test_async_publication_failure_restores_both_buttons_and_allows_retry(self):
        self.check_failure_retry("stamp")

    def test_start_failure_preserves_disabled_peer_and_allows_retry(self):
        self.buttons[1].setEnabled(False)
        with patch.object(updater_ui.threading, "Thread", return_value=Mock(start=Mock(side_effect=OSError))):
            self.buttons[0].click()
        self.assertFalse(self.window._trig_dl_in_flight)
        self.assertTrue(self.buttons[0].isEnabled())
        self.assertFalse(self.buttons[1].isEnabled())
        self.assertEqual([button.text() for button in self.buttons], self.labels)
        self.assertEqual(self.window._triggers_mtime, ())
        self.window._poll_zone_and_triggers()
        self.assertEqual(self.window._triggers_mtime, self.window._trigger_files_stamp())
        self.buttons[1].setEnabled(True)
        self.finish(self.start(1))
        self.assert_catalog("A")

    def test_close_keeps_download_owned_until_worker_completion(self):
        self.catalogs = ("B",)
        index = self.start(1)
        self.gates[index]["triggers"].set()
        self.wait_phase(index, "retired")
        self.window.close()
        self.assertTrue(self.workers[index].is_alive())
        before = self.snapshot()
        self.assert_deferred(before, closed=True)
        self.gates[index]["retired"].set()
        self.wait_phase(index, "stamp")
        self.assert_deferred(before, closed=True)
        self.gates[index]["stamp"].set()
        self.wait_result(index)
        self.assert_catalog("B")


class CatalogWaitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_recorded_boundary_signal_satisfies_predicate_without_another_signal(self):
        events = PublicationEvents()
        spy = QSignalSpy(events.ready)
        allow, emitted = threading.Event(), threading.Event()

        def emit():
            if allow.wait(2):
                events.ready.emit(1, "retired")
                emitted.set()

        worker = threading.Thread(target=emit)
        worker.start()
        waits = []
        case = self

        class Boundary:
            def wait(self, remaining):
                allow.set()
                case.assertTrue(emitted.wait(2))
                case.assertEqual([list(row) for row in spy], [[1, "retired"]])
                result = spy.wait(20)
                waits.append(result)
                return result

        try:
            CatalogPublicationTests.wait_for(self, Boundary(), lambda: len(spy) == 1)
            self.assertEqual(waits, [False])
        finally:
            allow.set()
            worker.join(2)
            self.assertFalse(worker.is_alive())

    def test_missing_target_still_expires_after_a_false_wait(self):
        events = PublicationEvents()
        spy = QSignalSpy(events.ready)
        case = self
        waits = []

        class NoTarget:
            def wait(self, remaining):
                case.assertFalse(spy.wait(20))
                waits.append(remaining)
                return False

        with patch.object(time, "monotonic", side_effect=[10, 10, 16]):
            with self.assertRaisesRegex(AssertionError, "Worker did not reach"):
                CatalogPublicationTests.wait_for(self, NoTarget(), lambda: len(spy) > 0)
        self.assertEqual(waits, [20])


def catalog_process(mode, root):
    CatalogPublicationTests.setUpClass()
    case = CatalogPublicationTests("test_update_then_restore_preserves_distinct_catalogs")
    case.fixture_root = root
    case.reuse_files = mode == "consume"
    case.setUp()
    if mode == "consume":
        try:
            expected = CURRENT if (root / "completed").exists() else OLD
            observations = []
            for reload in (lambda: None, case.window._zone_redetect_timer.timeout.emit,
                           case.window._load_triggers):
                reload()
                ids = [t.id for t in case.window._triggers]
                speech = case.speech_texts()
                print(json.dumps({"mode": mode, "ids": ids, "speech": speech}), flush=True)
                case.assertEqual(ids, [expected])
                case.assertEqual(speech, ["Saved historical wording"])
                case.assertEqual(ac.TRIGGERS_LOCAL_FILE.read_bytes(), (root / "local.before").read_bytes())
                observations.append(ids)
            print(json.dumps({"mode": mode, "ids": observations, "speech": "Saved historical wording"}), flush=True)
        finally:
            case.doCleanups()
        return

    case.finish(case.start(0))
    case.assert_catalog("A")
    (root / "prior.version").write_bytes(ac._REPO_TRIGGERS_VERSION.read_bytes())
    (root / "local.before").write_bytes(case.local)
    index = case.start(1)
    if mode == "complete":
        case.finish(index)
        case.assert_catalog("B")
        (root / "completed").touch()
    else:
        case.gates[index]["triggers"].set()
        case.wait_phase(index, "retired")
        case.assertEqual(json.loads(ac._REPO_TRIGGERS_FILE.read_text()), CATALOGS["B"][0])
        case.assertEqual(json.loads(ac._REPO_RETIRED_FILE.read_text()), CATALOGS["A"][1])
        case.assert_deferred(case.snapshot())
        case.assertTrue(case.workers[index].daemon and case.workers[index].is_alive())

    if mode != "abrupt":
        import main

        timer = QTimer()
        timer.setSingleShot(True)
        timer.timeout.connect(case.window.close)
        timer.start(0)
        with patch.object(main, "QApplication", return_value=case.app), \
                patch.object(main, "data_root", return_value=root), \
                patch.object(main, "_needs_setup", return_value=False), \
                patch.object(mw, "MainWindow", return_value=case.window), \
                patch.object(ac.updater, "mark_boot_ok"), \
                patch.object(ac.updater, "cleanup_old_backups"):
            with case.assertRaises(SystemExit) as exit:
                main.main()
            case.assertEqual(exit.exception.code, 0)
        case.assertFalse(case.window.isVisible())
    if mode != "complete":
        case.assertTrue(case.workers[index].is_alive())
        case.assertEqual(len(case.results), 1)
    print(json.dumps({"mode": mode, "worker_alive": case.workers[index].is_alive(),
                      "completions": len(case.results), "stamp": ac._repo_download_version()}), flush=True)
    if mode == "abrupt":
        os._exit(0)
    # Releasing the blocked worker would erase the exit boundary being tested.


class CatalogRestartTests(unittest.TestCase):
    def run_process(self, mode, root):
        environment = os.environ.copy()
        profile = root.parent / (mode + "-profile")
        for key in ("HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME", "APPDATA", "TMPDIR"):
            path = profile / key
            path.mkdir(parents=True, exist_ok=True)
            environment[key] = str(path)
        # Keep installed dependencies visible when HOME changes for the disposable profile.
        environment["PYTHONPATH"] = os.pathsep.join(str(Path(path).resolve()) for path in sys.path if path)
        environment.update(QT_QPA_PLATFORM="offscreen", NYAA_REPLAY_TEST="1", PYTHONDONTWRITEBYTECODE="1")
        result = subprocess.run(
            [sys.executable, "-m", "tests.test_catalog_publication", "--catalog-process", mode, str(root)],
            cwd=Path(__file__).resolve().parents[1], env=environment,
            capture_output=True, text=True, timeout=25)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        print(result.stdout.strip(), flush=True)

    def test_exit_and_separate_restart_reject_partial_catalog_and_accept_completion(self):
        for mode in ("normal", "abrupt", "complete"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                root = Path(directory) / "catalog"
                root.mkdir()
                self.run_process(mode, root)
                self.run_process("consume", root)

    def test_missing_and_incompatible_stamp_restart_controls(self):
        for stamp in (None, "0.0.0"):
            with self.subTest(stamp=stamp), tempfile.TemporaryDirectory() as directory:
                root = Path(directory) / "catalog"
                root.mkdir()
                self.run_process("normal", root)
                path = root / "_REPO_TRIGGERS_VERSION"
                path.write_bytes((root / "prior.version").read_bytes())
                self.assertEqual(json.loads(path.read_text()), ac._VERSION)
                if stamp is None:
                    path.unlink(missing_ok=True)
                else:
                    path.write_text(json.dumps(stamp), encoding="utf-8")
                self.run_process("consume", root)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--catalog-process":
        catalog_process(sys.argv[2], Path(sys.argv[3]))
    else:
        unittest.main()
