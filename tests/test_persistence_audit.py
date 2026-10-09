from pathlib import Path
from datetime import datetime
from contextlib import ExitStack
import json
import errno
import os
import subprocess
import sys
import sysconfig
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from nyaatriggers import dps_store, updater


class ProfileWarningTests(unittest.TestCase):
    def exercise(self, failure, *, persistent=False, profile=True):
        from PyQt6.QtCore import QTimer
        from PyQt6.QtWidgets import QMessageBox
        from nyaatriggers import app_common as ac, main_window as mw
        from nyaatriggers.trigger_engine import Trigger
        from nyaatriggers.trigger_profiles import capture_profile
        from nyaatriggers.ui import settings_tab, triggers_tab
        from tests.test_profile_recovery_audit import fixture

        warning = QMessageBox.warning
        case = fixture()
        window = case.window
        trigger = Trigger(id="local", ability_id="ABCD", enabled=True,
                          tts_text="Old committed speech", cooldown_s=0)
        window._triggers = [trigger]
        window._local_ids.add(trigger.id)
        window._local_enabled = True
        window._save_settings()
        window._save_triggers()
        target = capture_profile(window, "Target")
        target["local"]["local"]["text"] = "Target speech"
        if failure in ("external-change", "corrupt"):
            external = json.loads(ac.TRIGGERS_LOCAL_FILE.read_text())
            external["triggers"][0]["tts_text"] = "External saved edit"
            ac.TRIGGERS_LOCAL_FILE.write_text(
                json.dumps(external) if failure == "external-change" else "corrupt external bytes")
        original_bytes = ac.TRIGGERS_LOCAL_FILE.read_bytes()
        speech, dialogs, flags, attempts = [], [], [], []
        timer = QTimer(window)
        timer.setInterval(1)

        def during_modal():
            box = case.app.activeModalWidget()
            if not isinstance(box, QMessageBox):
                return
            timer.stop()
            dialogs.append(box.windowTitle())
            flags.append(bool(getattr(window, "_local_conflict_dialog", False)))
            window._on_log_line(
                "20|2026-09-26T11:30:00.000|40000001|Boss|ABCD|Cast|10000001|Player|1|0|0|0")
            box.accept()

        timer.timeout.connect(during_modal)
        writer = settings_tab if failure == "settings" else triggers_tab
        original = writer._atomic_write_json

        def fail_write(*args, **kwargs):
            attempts.append(True)
            if failure in ("settings", "triggers") and (persistent or len(attempts) == 1):
                raise PermissionError("temporary disk failure")
            return original(*args, **kwargs)

        try:
            timer.start()
            with patch.object(ac.QMessageBox, "warning", warning), \
                    patch.object(writer, "_atomic_write_json", fail_write), \
                    patch.object(mw, "speak", side_effect=lambda text, **kwargs: speech.append(text)):
                applied = window._activate_profile(target) if profile else window._save_settings()
            self.assertFalse(applied)
            self.assertEqual(dialogs, [{"external-change": "Triggers Changed", "corrupt": "Triggers Unreadable"}.get(failure, "Save Failed")])
            self.assertEqual(speech, ["Old committed speech"])
            self.assertEqual(trigger.tts_text, "Old committed speech")
            self.assertEqual(ac.TRIGGERS_LOCAL_FILE.read_bytes(), original_bytes)
            self.assertEqual(flags, [failure == "external-change"])
            self.assertFalse(getattr(window, "_local_conflict_dialog", False))
            self.assertIsNone(window._deferred_persistence_warnings if profile else None)
        finally:
            timer.stop()
            case.doCleanups()

    def test_profile_failure_dialogs_observe_rolled_back_choices(self):
        for failure in ("settings", "triggers", "external-change", "corrupt"):
            with self.subTest(failure=failure):
                self.exercise(failure)

    def test_failed_rollback_keeps_first_warning_and_previous_runtime(self):
        self.exercise("triggers", persistent=True)

    def test_ordinary_save_still_warns_immediately(self):
        self.exercise("settings", profile=False)

    def test_nested_warning_scope_preserves_order_and_conflict_flag(self):
        from nyaatriggers import app_common as ac
        window = SimpleNamespace()
        shown = []
        def warning(parent, title, text):
            shown.append((title, text, getattr(parent, "_local_conflict_dialog", False)))
        with patch.object(ac.QMessageBox, "warning", warning):
            with ac.defer_persistence_warnings(window):
                ac.persistence_warning(window, "first", "one")
                with ac.defer_persistence_warnings(window):
                    ac.persistence_warning(window, "second", "two", conflict=True)
                self.assertEqual(shown, [])
            self.assertEqual(shown, [("first", "one", False), ("second", "two", True)])
            self.assertFalse(window._local_conflict_dialog)


class SessionFinalSaveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from tests.test_session_ui import SessionUiTests
        SessionUiTests.setUpClass()

    def fixture(self):
        from tests.test_session_ui import SessionUiTests
        case = SessionUiTests("test_window_icon_loads_from_bundled_assets")
        case.setUp()
        self.addCleanup(case.doCleanups)
        case.connect()
        case.start_session()
        case.window._prog_sessions.poll_saves(wait=True)
        case.clock.value += 12
        return case

    def test_close_restart_and_handoff_retry_the_final_session_write(self):
        from nyaatriggers import prog_session

        for operation in ("close", "_restart_for_update", "_quit_for_windows_handoff"):
            with self.subTest(operation=operation):
                case = self.fixture()
                sessions = case.window._prog_sessions
                session = sessions.current
                original = prog_session.write_record
                attempts = []

                def transient(directory, data):
                    if data.get("state") == "ended":
                        attempts.append(data["elapsed"])
                        if len(attempts) == 1:
                            raise PermissionError("temporary final replace failure")
                    return original(directory, data)

                with patch.object(prog_session, "write_record", side_effect=transient), \
                        patch.object(updater, "relaunch"):
                    getattr(case.window, operation)()
                saved = json.loads((Path(sessions.directory) / (session["id"] + ".json")).read_text())
                self.assertEqual(attempts, [12.0, 12.0])
                self.assertEqual((saved["state"], saved["elapsed"]), ("ended", 12.0))
                self.assertEqual(sessions.unsaved, {})
                case.doCleanups()

    def test_persistent_final_failure_is_bounded_and_preserves_the_previous_record(self):
        from nyaatriggers import prog_session

        case = self.fixture()
        sessions = case.window._prog_sessions
        session = sessions.current
        path = Path(sessions.directory) / (session["id"] + ".json")
        original = path.read_bytes()
        with patch.object(prog_session, "write_record", side_effect=PermissionError("still locked")) as write:
            started = time.monotonic()
            case.window.close()
            self.assertLess(time.monotonic() - started, 2)
            self.assertEqual(write.call_count, 2)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(len(sessions.unsaved), 1)


class VoiceEnvironmentRecoveryTests(unittest.TestCase):
    def old_environment(self, root, kokoro=True):
        root.mkdir()
        minor = max(0, sys.version_info.minor - 1)
        (root / "pyvenv.cfg").write_text(f"version = 3.{minor}.0\n")
        packages = root / "lib" / f"python3.{minor}" / "site-packages"
        (packages / "piper").mkdir(parents=True)
        if kokoro:
            (packages / "kokoro_onnx").mkdir()
        (root / "user-notes.txt").write_text("preserved 日本語")
        return {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}

    @staticmethod
    def fake_installer(root):
        def run(args, timeout):
            if args[1:3] == ["-m", "venv"]:
                root.mkdir()
                (root / "pyvenv.cfg").write_text(
                    f"version = {sys.version_info.major}.{sys.version_info.minor}.0\n")
            elif args[1] == "install":
                packages = Path(sysconfig.get_path("purelib", scheme="venv",
                                vars={"base": str(root), "platbase": str(root)}))
                (packages / "piper").mkdir(parents=True)
        return run

    def test_repair_preserves_optional_choice_and_the_whole_original(self):
        import install

        for kokoro in (False, True):
            with self.subTest(kokoro=kokoro), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary) / "voice"
                original = self.old_environment(root, kokoro)
                run = Mock(side_effect=self.fake_installer(root))
                install.prepare_voice_venv(root, run)
                packages = run.call_args_list[1].args[0]
                self.assertIn("piper-tts==1.4.2", packages)
                self.assertEqual("kokoro-onnx==0.4.7" in packages, kokoro)
                backup, = root.parent.glob("voice.backup-*")
                self.assertEqual({p.relative_to(backup): p.read_bytes()
                                  for p in backup.rglob("*") if p.is_file()}, original)
                self.assertFalse(install.voice_repair_pending(root))

    def test_compatible_uv_environment_keeps_program_packages_during_voice_setup(self):
        import install
        import main

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "runtime"
            scripts = root / ("Scripts" if os.name == "nt" else "bin")
            scripts.mkdir(parents=True)
            pip = scripts / ("pip.exe" if os.name == "nt" else "pip")
            pip.write_bytes(b"existing installer")
            config = root / "pyvenv.cfg"
            original = f"implementation = CPython\nversion_info = {sys.version_info.major}.{sys.version_info.minor}.8\n"
            config.write_text(original)
            packages = Path(sysconfig.get_path("purelib", scheme="venv",
                            vars={"base": str(root), "platbase": str(root)}))
            (packages / "piper").mkdir(parents=True)
            (packages / "PyQt6").mkdir()
            sentinel = packages / "PyQt6" / "program-dependency"
            sentinel.write_bytes(b"keep the shared runtime")
            run = Mock()
            with patch.object(install, "run_setup_command") as probe, \
                    patch.object(sys, "frozen", False, create=True):
                self.assertTrue(main._piper_installed(root))
                install.prepare_voice_venv(root, run)
            probe.assert_not_called()
            self.assertEqual(install.voice_venv_version(root), sys.version_info[:2])
            self.assertEqual(config.read_text(), original)
            self.assertEqual(sentinel.read_bytes(), b"keep the shared runtime")
            self.assertEqual(list(root.parent.glob("runtime.backup-*")), [])
            self.assertFalse(install.voice_setup_pending(root))
            self.assertEqual(run.call_count, 2)
            self.assertEqual(run.call_args_list[0].args[0][0], str(pip))
            self.assertFalse(any(call.args[0][1:3] == ["-m", "venv"]
                                 for call in run.call_args_list))

    def test_another_python_versions_uv_environment_is_repaired_and_preserved(self):
        import install
        import main

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "voice"
            self.old_environment(root, kokoro=False)
            config = root / "pyvenv.cfg"
            config.write_text(config.read_text().replace("version =", "version_info ="))
            packages = Path(sysconfig.get_path("purelib", scheme="venv",
                            vars={"base": str(root), "platbase": str(root)}))
            (packages / "piper").mkdir(parents=True)
            original = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
            with patch.object(sys, "frozen", False, create=True):
                self.assertFalse(main._piper_installed(root))
                install.prepare_voice_venv(root, self.fake_installer(root))
            backup, = root.parent.glob("voice.backup-*")
            self.assertEqual({p.relative_to(backup): p.read_bytes()
                              for p in backup.rglob("*") if p.is_file()}, original)
            self.assertEqual(install.voice_venv_version(root), sys.version_info[:2])
            self.assertTrue(main._piper_installed(root))
            self.assertFalse(install.voice_setup_pending(root))

    def test_failed_creation_install_and_validation_restore_the_original(self):
        import install

        for failed_call in (1, 2, 3):
            with self.subTest(failed_call=failed_call), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary) / "voice"
                original = self.old_environment(root)
                calls = []
                fake = self.fake_installer(root)

                def run(args, timeout):
                    calls.append(args)
                    fake(args, timeout)
                    if len(calls) == failed_call:
                        raise OSError("setup interrupted")

                with self.assertRaisesRegex(OSError, "setup interrupted"):
                    install.prepare_voice_venv(root, run)
                self.assertEqual({p.relative_to(root): p.read_bytes()
                                  for p in root.rglob("*") if p.is_file()}, original)
                self.assertFalse(install.voice_repair_pending(root))
                self.assertEqual(list(root.parent.glob("voice.backup-*")), [])
                install.prepare_voice_venv(root, self.fake_installer(root))
                self.assertFalse(install.voice_repair_pending(root))

    def test_failure_before_original_rename_never_removes_the_original(self):
        import install

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "voice"
            original = self.old_environment(root)
            replace = os.replace

            def fail_backup(source, destination):
                if Path(source) == root:
                    raise OSError("rename denied")
                return replace(source, destination)

            with patch.object(install.os, "replace", side_effect=fail_backup):
                with self.assertRaisesRegex(OSError, "rename denied"):
                    install.prepare_voice_venv(root, self.fake_installer(root))
            self.assertEqual({p.relative_to(root): p.read_bytes()
                              for p in root.rglob("*") if p.is_file()}, original)
            self.assertFalse(install.voice_repair_pending(root))

    def test_failed_rollback_keeps_the_backup_and_recovers_on_retry(self):
        import install

        for failure in ("remove", "rename"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary) / "voice"
                original = self.old_environment(root)
                replace = os.replace
                fake = self.fake_installer(root)

                def run(args, timeout):
                    fake(args, timeout)
                    raise OSError("installation failed")

                def fail_restore(source, destination):
                    if Path(destination) == root:
                        raise OSError("restore locked")
                    return replace(source, destination)

                target = "shutil.rmtree" if failure == "remove" else "os.replace"
                effect = OSError("cleanup locked") if failure == "remove" else fail_restore
                with patch("install." + target, side_effect=effect):
                    with self.assertRaisesRegex(RuntimeError, "original environment is preserved"):
                        install.prepare_voice_venv(root, run)
                backup, = root.parent.glob("voice.backup-*")
                self.assertEqual({p.relative_to(backup): p.read_bytes()
                                  for p in backup.rglob("*") if p.is_file()}, original)
                self.assertTrue(install.voice_repair_pending(root))
                install.prepare_voice_venv(root, self.fake_installer(root))
                self.assertFalse(install.voice_repair_pending(root))
                backup, = root.parent.glob("voice.backup-*")
                self.assertEqual({p.relative_to(backup): p.read_bytes()
                                  for p in backup.rglob("*") if p.is_file()}, original)

    def test_full_storage_before_the_recovery_marker_keeps_original_in_place(self):
        import install

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "voice"
            original = self.old_environment(root)
            with patch.object(install.os, "fsync", side_effect=OSError(errno.ENOSPC, "disk full")):
                with self.assertRaisesRegex(OSError, "disk full"):
                    install.prepare_voice_venv(root, Mock())
            self.assertEqual({p.relative_to(root): p.read_bytes()
                              for p in root.rglob("*") if p.is_file()}, original)
            self.assertFalse(install.voice_repair_pending(root))
            self.assertEqual(list(root.parent.iterdir()), [root])

    def test_recovery_rejects_paths_outside_the_owned_backup_name(self):
        import install

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "voice"
            original = self.old_environment(root)
            for name in ("../other", str(root), "another.backup-" + "0" * 32):
                with self.subTest(name=name):
                    install._voice_repair_marker(root).write_text(json.dumps({"backup": name}))
                    with self.assertRaisesRegex(RuntimeError, "Invalid voice environment recovery"):
                        install.prepare_voice_venv(root, Mock())
                    self.assertEqual({p.relative_to(root): p.read_bytes()
                                      for p in root.rglob("*") if p.is_file()}, original)

    def test_missing_backup_cannot_approve_a_partial_replacement(self):
        import install

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "voice"
            root.mkdir()
            (root / "pyvenv.cfg").write_text(
                f"version = {sys.version_info.major}.{sys.version_info.minor}.0\n")
            marker = install._voice_repair_marker(root)
            marker.write_text(json.dumps({"backup": "voice.backup-" + "0" * 32,
                                          "version": [3, sys.version_info.minor - 1]}))
            with self.assertRaisesRegex(RuntimeError, "backup is missing"):
                install.prepare_voice_venv(root, Mock())
            self.assertTrue(marker.exists())
            self.assertTrue((root / "pyvenv.cfg").exists())

    def test_invalid_backup_is_rejected_before_removing_any_environment(self):
        import install

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "voice"
            original = self.old_environment(root)
            backup = root.with_name("voice.backup-" + "0" * 32)
            backup.mkdir()
            marker = install._voice_repair_marker(root)
            marker.write_text(json.dumps({"backup": backup.name,
                                          "version": [3, sys.version_info.minor - 1]}))
            with self.assertRaisesRegex(RuntimeError, "does not match"):
                install.prepare_voice_venv(root, Mock())
            self.assertEqual({p.relative_to(root): p.read_bytes()
                              for p in root.rglob("*") if p.is_file()}, original)
            self.assertTrue(marker.exists())

    def test_process_death_at_repair_boundaries_recovers_without_losing_original(self):
        import install
        import main

        script = '''
from pathlib import Path
import os, sys, sysconfig
import install
root, phase = Path(sys.argv[1]), sys.argv[2]
replace = os.replace
def boundary_replace(source, destination):
    marker = Path(destination) == install._voice_repair_marker(root)
    backup = Path(source) == root
    if phase == "before-marker" and marker:
        os._exit(23)
    replace(source, destination)
    if (phase == "after-marker" and marker) or (phase == "after-backup" and backup):
        os._exit(23)
install.os.replace = boundary_replace
unlink = Path.unlink
def boundary_unlink(path, *args, **kwargs):
    result = unlink(path, *args, **kwargs)
    if phase == "committed" and path == install._voice_install_marker(root):
        os._exit(23)
    return result
Path.unlink = boundary_unlink
calls = 0
def run(args, timeout):
    global calls
    calls += 1
    if calls == 1:
        root.mkdir()
        (root / "pyvenv.cfg").write_text(f"version = {sys.version_info.major}.{sys.version_info.minor}.0\\n")
    elif calls == 2:
        packages = Path(sysconfig.get_path("purelib", scheme="venv",
                        vars={"base": str(root), "platbase": str(root)}))
        (packages / "piper").mkdir(parents=True)
    if phase == {1:"created", 2:"installed", 3:"validated"}[calls]:
        os._exit(23)
install.prepare_voice_venv(root, run)
'''
        for phase in ("before-marker", "after-marker", "after-backup", "created",
                      "installed", "validated", "committed"):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary) / "voice"
                original = self.old_environment(root)
                result = subprocess.run([sys.executable, "-c", script, str(root), phase],
                                        capture_output=True, text=True, timeout=5,
                                        cwd=Path(__file__).resolve().parents[1])
                self.assertEqual(result.returncode, 23, result.stdout + result.stderr)
                with patch.object(main, "_FFXIV_VENV", root), patch.object(sys, "frozen", False, create=True):
                    self.assertEqual(main._piper_installed(root), phase == "committed")
                if phase != "committed":
                    install.prepare_voice_venv(root, self.fake_installer(root))
                backup, = root.parent.glob("voice.backup-*")
                self.assertEqual({p.relative_to(backup): p.read_bytes()
                                  for p in backup.rglob("*") if p.is_file()}, original)
                self.assertFalse(install.voice_repair_pending(root))

    def test_startup_does_not_accept_another_python_versions_piper(self):
        import main

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            old_minor = max(0, sys.version_info.minor - 1)
            (root / "pyvenv.cfg").write_text(f"version = 3.{old_minor}.0\n")
            for packages in (root / "lib" / f"python3.{old_minor}" / "site-packages",
                             root / "Lib" / "site-packages"):
                (packages / "piper").mkdir(parents=True)
            with patch.object(main, "_FFXIV_VENV", root), patch.object(sys, "frozen", False, create=True):
                self.assertFalse(main._piper_installed(root))

    def test_repair_can_run_from_its_own_environment(self):
        import install

        script = '''
from pathlib import Path
import subprocess, sys
import install
root = Path(sys.prefix)
def run(args, timeout):
    if args[1:3] == ["-m", "venv"]:
        subprocess.run(args, check=True, capture_output=True, timeout=timeout)
install.prepare_voice_venv(root, run)
'''
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "voice"
            subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(root)],
                           check=True, capture_output=True, timeout=30)
            cfg = root / "pyvenv.cfg"
            cfg.write_text(cfg.read_text().replace(
                f"version = {sys.version_info.major}.{sys.version_info.minor}.",
                f"version = {sys.version_info.major}.{sys.version_info.minor - 1}."))
            python = root / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            result = subprocess.run([str(python), "-c", script], capture_output=True,
                                    text=True, timeout=45, cwd=Path(__file__).resolve().parents[1])
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertFalse(install.voice_repair_pending(root))


class VoiceInstallCompletionTests(unittest.TestCase):
    @staticmethod
    def installer(root):
        def run(args, timeout):
            scripts = root / ("Scripts" if os.name == "nt" else "bin")
            if args[1:3] == ["-m", "venv"]:
                scripts.mkdir(parents=True, exist_ok=True)
                (scripts / ("pip.exe" if os.name == "nt" else "pip")).touch()
                (root / "pyvenv.cfg").write_text(
                    f"version = {sys.version_info.major}.{sys.version_info.minor}.0\n")
            elif args[1] == "install":
                packages = Path(sysconfig.get_path("purelib", scheme="venv",
                                vars={"base": str(root), "platbase": str(root)}))
                (packages / "piper").mkdir(parents=True, exist_ok=True)
        return run

    def test_failed_runtime_validation_remains_pending_and_retry_forces_install(self):
        import install
        import main

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "voice"
            fake = self.installer(root)

            def fail_validation(args, timeout):
                fake(args, timeout)
                if args[1] == "-c":
                    raise subprocess.CalledProcessError(1, args, stderr="missing piper.config")

            with self.assertRaises(subprocess.CalledProcessError):
                install.prepare_voice_venv(root, fail_validation)
            (root / "user-notes.txt").write_text("keep my notes")
            self.assertTrue(install.voice_setup_pending(root))
            self.assertFalse(main._piper_installed(root))
            retry = Mock(side_effect=fake)
            install.prepare_voice_venv(root, retry)
            self.assertIn("--force-reinstall", retry.call_args_list[0].args[0])
            self.assertTrue(main._piper_installed(root))
            self.assertFalse(install.voice_setup_pending(root))
            self.assertEqual((root / "user-notes.txt").read_text(), "keep my notes")
            clean = Mock(side_effect=fake)
            install.prepare_voice_venv(root, clean)
            self.assertNotIn("--force-reinstall", clean.call_args_list[0].args[0])

    def test_marker_failure_precedes_any_environment_mutation(self):
        import install

        for operation in ("fsync", "replace"):
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary) / "voice"
                run = Mock()
                with patch.object(install.os, operation, side_effect=OSError(errno.ENOSPC, "disk full")):
                    with self.assertRaisesRegex(OSError, "disk full"):
                        install.prepare_voice_venv(root, run)
                run.assert_not_called()
                self.assertFalse(root.exists())
                self.assertFalse(install.voice_setup_pending(root))
                self.assertEqual(list(root.parent.iterdir()), [])

    def test_cancelled_install_and_failed_completion_cleanup_keep_retry_state(self):
        import install
        import main

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "voice"
            with self.assertRaises(KeyboardInterrupt):
                install.prepare_voice_venv(root, Mock(side_effect=KeyboardInterrupt))
            self.assertTrue(install.voice_setup_pending(root))
            fake = self.installer(root)
            unlink = Path.unlink

            def fail_completion(path, *args, **kwargs):
                if path == install._voice_install_marker(root):
                    raise PermissionError("completion marker locked")
                return unlink(path, *args, **kwargs)

            with patch.object(Path, "unlink", fail_completion):
                with self.assertRaisesRegex(PermissionError, "completion marker locked"):
                    install.prepare_voice_venv(root, fake)
            self.assertFalse(main._piper_installed(root))
            install.prepare_voice_venv(root, fake)
            self.assertTrue(main._piper_installed(root))

    def test_process_cuts_remain_incomplete_until_validated_install_commits(self):
        import install
        import main

        script = '''
from pathlib import Path
import os, sys
import install
from tests.test_persistence_audit import VoiceInstallCompletionTests
root, phase = Path(sys.argv[1]), sys.argv[2]
replace = os.replace
def boundary(source, destination):
    replace(source, destination)
    if phase == "marked" and Path(destination) == install._voice_install_marker(root):
        os._exit(23)
install.os.replace = boundary
unlink = Path.unlink
def completion(path, *args, **kwargs):
    result = unlink(path, *args, **kwargs)
    if phase == "committed" and path == install._voice_install_marker(root):
        os._exit(23)
    return result
Path.unlink = completion
fake = VoiceInstallCompletionTests.installer(root)
calls = 0
def run(args, timeout):
    global calls
    calls += 1
    fake(args, timeout)
    if phase == {1: "created", 2: "installed", 3: "validated"}[calls]:
        os._exit(23)
install.prepare_voice_venv(root, run)
'''
        for phase in ("marked", "created", "installed", "validated", "committed"):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary) / "voice"
                result = subprocess.run([sys.executable, "-c", script, str(root), phase],
                                        capture_output=True, text=True, timeout=10,
                                        cwd=Path(__file__).resolve().parents[1])
                self.assertEqual(result.returncode, 23, result.stdout + result.stderr)
                self.assertEqual(main._piper_installed(root), phase == "committed")
                if phase != "committed":
                    install.prepare_voice_venv(root, self.installer(root))
                self.assertTrue(main._piper_installed(root))
                self.assertFalse(install.voice_setup_pending(root))


class UnknownVoiceEnvironmentTests(unittest.TestCase):
    def environment(self, root):
        subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(root)],
                       check=True, capture_output=True, timeout=30)
        config = root / "pyvenv.cfg"
        config.write_text("\n".join(config.read_text().splitlines()[:2]) + "\n")
        (root / "user-notes.txt").write_text("preserve the original environment")
        return config.read_bytes()

    def test_verified_same_abi_environment_is_recreated_with_complete_metadata(self):
        import install

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "voice"
            original = self.environment(root)
            self.assertIsNone(install.voice_venv_version(root))
            self.assertEqual(install._verified_voice_venv_version(root), sys.version_info[:2])
            install.prepare_voice_venv(root, VoiceEnvironmentRecoveryTests.fake_installer(root))
            self.assertEqual(install.voice_venv_version(root), sys.version_info[:2])
            backup, = root.parent.glob("voice.backup-*")
            self.assertEqual((backup / "pyvenv.cfg").read_bytes(), original)
            self.assertEqual((backup / "user-notes.txt").read_text(), "preserve the original environment")

    def test_failed_install_verifies_renamed_original_before_restoring_it(self):
        import install

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "voice"
            original = self.environment(root)
            fake = VoiceEnvironmentRecoveryTests.fake_installer(root)

            def run(args, timeout):
                fake(args, timeout)
                raise OSError("interrupted install")

            with self.assertRaisesRegex(OSError, "interrupted install"):
                install.prepare_voice_venv(root, run)
            self.assertEqual((root / "pyvenv.cfg").read_bytes(), original)
            self.assertTrue((root / "user-notes.txt").is_file())
            self.assertFalse(install.voice_repair_pending(root))
            self.assertEqual(install._verified_voice_venv_version(root), sys.version_info[:2])

    def test_missing_and_broken_interpreters_leave_unknown_environment_untouched(self):
        import install

        for broken in (False, True):
            with self.subTest(broken=broken), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary) / "voice"
                original = self.environment(root)
                python = root / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
                python.unlink()
                if broken:
                    python.write_bytes(b"interrupted executable")
                    python.chmod(0o700)
                run = Mock()
                with self.assertRaisesRegex(RuntimeError, "Cannot verify the voice environment"):
                    install.prepare_voice_venv(root, run)
                run.assert_not_called()
                self.assertEqual((root / "pyvenv.cfg").read_bytes(), original)
                self.assertTrue((root / "user-notes.txt").is_file())
                self.assertFalse(install.voice_repair_pending(root))
                self.assertEqual(list(root.parent.glob("voice.backup-*")), [])

    def test_non_environment_prefix_is_rejected_before_mutation(self):
        import install

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "voice"
            original = self.environment(root)
            for prefix, base in ((str(root), str(root)), (str(root.parent), sys.base_prefix)):
                with self.subTest(prefix=prefix, base=base):
                    output = json.dumps([list(sys.version_info[:2]), prefix, base])
                    with patch.object(install, "run_setup_command", return_value=
                                      subprocess.CompletedProcess([], 0, output, "")):
                        with self.assertRaisesRegex(RuntimeError, "Cannot verify"):
                            install.prepare_voice_venv(root, Mock())
                    self.assertEqual((root / "pyvenv.cfg").read_bytes(), original)
                    self.assertFalse(install.voice_repair_pending(root))

    def test_process_death_recovery_verifies_truncated_original_in_each_location(self):
        import install

        script = '''
from pathlib import Path
import os, sys
import install
root, phase = Path(sys.argv[1]), sys.argv[2]
replace = os.replace
def boundary(source, destination):
    replace(source, destination)
    if ((phase == "marker" and Path(destination) == install._voice_repair_marker(root))
            or (phase == "backup" and Path(source) == root)):
        os._exit(23)
install.os.replace = boundary
def run(args, timeout):
    root.mkdir()
    (root / "partial.txt").write_text("incomplete replacement")
    os._exit(23)
install.prepare_voice_venv(root, run)
'''
        for phase in ("marker", "backup", "created"):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary) / "voice"
                original = self.environment(root)
                result = subprocess.run([sys.executable, "-c", script, str(root), phase],
                                        capture_output=True, text=True, timeout=15,
                                        cwd=Path(__file__).resolve().parents[1])
                self.assertEqual(result.returncode, 23, result.stdout + result.stderr)
                self.assertTrue(install.voice_repair_pending(root))
                install.prepare_voice_venv(root, VoiceEnvironmentRecoveryTests.fake_installer(root))
                backup, = root.parent.glob("voice.backup-*")
                self.assertEqual((backup / "pyvenv.cfg").read_bytes(), original)
                self.assertTrue((backup / "user-notes.txt").is_file())
                self.assertFalse((root / "partial.txt").exists())
                self.assertFalse(install.voice_repair_pending(root))


class SetupProcessRecoveryTests(unittest.TestCase):
    def test_source_dependencies_exclude_voice_repair_until_the_installer_finishes(self):
        import install

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "requirements.txt").write_text("fixture==1.0\n")

            def pip(args, **kwargs):
                with self.assertRaisesRegex(RuntimeError, "already running"):
                    with install.setup_lock():
                        self.fail("voice repair entered during source dependency installation")
                return subprocess.CompletedProcess(args, 0, "", "")

            with patch.object(install, "_SETUP_LOCK", root / "setup.lock"), \
                    patch.object(install, "_SETUP_WAIT_S", 0), \
                    patch.object(updater, "_externally_managed_python", return_value=False), \
                    patch.object(install, "run_setup_command", side_effect=pip):
                self.assertEqual(updater._install_requirements(root), (True, ""))
                with install.setup_lock():
                    pass

    def test_source_update_timeout_stops_writes_before_a_retry(self):
        import install

        child = '''
from pathlib import Path
import sys, time
root = Path(sys.argv[1])
(root / "child-started").touch()
time.sleep(float(sys.argv[2]))
(root / "late-package-write").touch()
'''
        parent = '''
from pathlib import Path
import subprocess, sys, time
root = Path(sys.argv[1])
subprocess.Popen([sys.executable, "-c", sys.argv[2], str(root), sys.argv[3]],
                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
while not (root / "child-started").exists():
    time.sleep(.01)
time.sleep(10)
'''
        deadline = 1.2 if os.name == "nt" else .2
        child_delay = 2 if os.name == "nt" else .6
        popen = subprocess.Popen
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "requirements.txt").write_text("fixture==1.0\n")

            def launch(args, **kwargs):
                if args[1:4] == ["-m", "pip", "install"]:
                    args = [sys.executable, "-c", parent, str(root), child, str(child_delay)]
                elif "--run-setup-command" in args:
                    offset = args.index("--run-setup-command") + 1
                    args = [*args[:offset], sys.executable, "-c", parent, str(root), child, str(child_delay)]
                process = popen(args, **kwargs)
                communicate = process.communicate
                process.communicate = lambda input=None, timeout=None: communicate(input, timeout=deadline)
                return process

            with patch.object(updater, "_externally_managed_python", return_value=False), \
                    patch.object(install, "_SETUP_LOCK", root / "setup.lock"), \
                    patch.object(updater, "_git_pull", return_value=subprocess.CompletedProcess([], 0, "current", "")), \
                    patch.object(subprocess, "Popen", side_effect=launch):
                ok, _ = updater.apply_git(root)
            self.assertFalse(ok)
            self.assertTrue((root / "child-started").exists())
            with updater._update_lock(root):
                time.sleep(child_delay + .1)
                self.assertFalse((root / "late-package-write").exists())

    def test_windows_job_failure_never_starts_the_installer(self):
        import ctypes
        import install

        for failed in ("CreateJobObjectW", "SetInformationJobObject", "AssignProcessToJobObject"):
            with self.subTest(failed=failed), ExitStack() as stack:
                kernel = SimpleNamespace(**{name: Mock(return_value=1) for name in (
                    "CreateJobObjectW", "SetInformationJobObject", "AssignProcessToJobObject",
                    "GetCurrentProcess", "CloseHandle")})
                getattr(kernel, failed).return_value = 0
                stack.enter_context(patch.object(ctypes, "WinDLL", return_value=kernel, create=True))
                stack.enter_context(patch.object(ctypes, "get_last_error", return_value=5, create=True))
                stack.enter_context(patch.object(ctypes, "WinError", return_value=OSError("job denied"), create=True))
                run = stack.enter_context(patch.object(install.subprocess, "run"))
                with self.assertRaisesRegex(OSError, "job denied"):
                    install._windows_setup_command(["installer"])
                run.assert_not_called()
                self.assertEqual(kernel.CloseHandle.call_count, failed != "CreateJobObjectW")

    def test_runner_captures_utf8_and_replaces_invalid_output(self):
        import install

        script = ("import os; assert os.environ['NYAA_SETUP_TEST'] == 'preserved'; "
                  "os.write(1, b'\\xff' + '日本語'.encode()); os.write(2, b'\\xfe'); raise SystemExit(7)")
        with self.assertRaises(subprocess.CalledProcessError) as raised:
            install.run_setup_command([sys.executable, "-c", script], 5, capture_output=True,
                                      env={**os.environ, "NYAA_SETUP_TEST": "preserved"})
        self.assertEqual(raised.exception.returncode, 7)
        self.assertEqual(raised.exception.stdout, "\ufffd日本語")
        self.assertEqual(raised.exception.stderr, "\ufffd")

    def test_timeout_stops_descendants_before_restoring_original_environment(self):
        import install

        child = '''
from pathlib import Path
import sys, time
root = Path(sys.argv[1])
(root.parent / "child-started").touch()
time.sleep(float(sys.argv[2]))
(root / "user-notes.txt").write_text("surviving child")
'''
        parent = '''
from pathlib import Path
import subprocess, sys, time
root = Path(sys.argv[1])
subprocess.Popen([sys.executable, "-c", sys.argv[2], str(root), sys.argv[4]])
while not (root.parent / "child-started").exists():
    time.sleep(.01)
if sys.argv[3] == "running":
    time.sleep(10)
'''
        deadline = 1.2 if os.name == "nt" else .2
        child_delay = 2 if os.name == "nt" else .6
        for state in (("running",) if os.name == "nt" else ("running", "exited")):
            with self.subTest(state=state), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary) / "voice"
                root.mkdir()
                (root / "pyvenv.cfg").write_text(f"version = 3.{sys.version_info.minor - 1}.0\n")
                (root / "user-notes.txt").write_text("original")

                def run(args, timeout):
                    if args[1:3] == ["-m", "venv"]:
                        root.mkdir()
                        (root / "pyvenv.cfg").write_text(
                            f"version = 3.{sys.version_info.minor}.0\n")
                    else:
                        install.run_setup_command(
                            [sys.executable, "-c", parent, str(root), child, state, str(child_delay)],
                            deadline, capture_output=True)

                started = time.monotonic()
                with self.assertRaises(subprocess.TimeoutExpired):
                    install.prepare_voice_venv(root, run)
                self.assertLess(time.monotonic() - started, deadline + 2)
                self.assertTrue((root.parent / "child-started").exists())
                self.assertEqual((root / "user-notes.txt").read_text(), "original")
                time.sleep(child_delay + .1)
                self.assertEqual((root / "user-notes.txt").read_text(), "original")


class SetupLockRecoveryTests(unittest.TestCase):
    def test_marker_write_failure_keeps_exclusion_and_releases_normally(self):
        import install

        with tempfile.TemporaryDirectory() as temporary, ExitStack() as stack:
            stack.enter_context(patch.object(install, "_SETUP_LOCK", Path(temporary) / "setup.lock"))
            stack.enter_context(patch.object(install, "_SETUP_WAIT_S", 0))
            with patch.object(install.os, "write", side_effect=OSError(errno.ENOSPC, "disk full")):
                with install.setup_lock():
                    with self.assertRaisesRegex(RuntimeError, "already running"):
                        with install.setup_lock():
                            self.fail("entered while another setup held the lock")
            with install.setup_lock():
                pass

    def test_unexpected_setup_exception_releases_the_lock_repeatedly(self):
        import install

        with tempfile.TemporaryDirectory() as temporary, \
                patch.object(install, "_SETUP_LOCK", Path(temporary) / "setup.lock"), \
                patch.object(install, "_SETUP_WAIT_S", 0):
            for _ in range(10):
                with self.assertRaisesRegex(RuntimeError, "installer failed"):
                    with install.setup_lock():
                        raise RuntimeError("installer failed")
            with install.setup_lock():
                pass

    def test_killed_installer_leaves_the_marker_while_its_child_finishes(self):
        import install

        script = '''
from pathlib import Path
import subprocess, sys, time
import install
install._SETUP_LOCK = Path(sys.argv[1])
root = install._SETUP_LOCK.parent
with install.setup_lock():
    child = subprocess.Popen([sys.executable, "-c", sys.argv[2], str(root)],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + 3
    while not (root / "child-active").exists():
        if time.monotonic() > deadline:
            raise RuntimeError("child never started")
        time.sleep(.01)
    print("held", flush=True)
    time.sleep(30)
'''
        child_script = '''
from pathlib import Path
import sys, time
root = Path(sys.argv[1])
(root / "child-active").touch()
deadline = time.monotonic() + 10
while not (root / "release-child").exists() and time.monotonic() < deadline:
    time.sleep(.01)
(root / "child-finished").touch()
'''
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            lock = root / "setup.lock"
            process = subprocess.Popen([sys.executable, "-c", script, str(lock), child_script],
                                       cwd=Path(__file__).resolve().parents[1],
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                self.assertEqual(process.stdout.readline().strip(), "held")
                process.kill()
                process.communicate(timeout=5)
                self.assertTrue(lock.exists())
                with patch.object(install, "_SETUP_LOCK", lock), patch.object(install, "_SETUP_WAIT_S", 0):
                    with self.assertRaisesRegex(RuntimeError, "already running"):
                        with install.setup_lock():
                            self.fail("setup overlapped the orphan child")
                self.assertFalse((root / "child-finished").exists())
            finally:
                if process.poll() is None:
                    process.kill()
                process.communicate(timeout=5)
                (root / "release-child").touch()
                deadline = time.monotonic() + 3
                while not (root / "child-finished").exists() and time.monotonic() < deadline:
                    time.sleep(.01)
            self.assertTrue((root / "child-finished").exists())
            os.utime(lock, (0, 0))
            with patch.object(install, "_SETUP_LOCK", lock), patch.object(install, "_SETUP_WAIT_S", 0):
                with install.setup_lock():
                    pass
            self.assertFalse(lock.exists())

    def test_stale_marker_waiters_cannot_overlap_in_separate_processes(self):
        script = '''
from pathlib import Path
import os, sys, time
import install
root, role = Path(sys.argv[1]), sys.argv[2]
install._SETUP_LOCK = root / "setup.lock"
install._SETUP_WAIT_S = 3
stat = Path.stat
def wait(path, timeout=3, required=True):
    deadline = time.monotonic() + timeout
    while not path.exists():
        if time.monotonic() >= deadline:
            if required:
                raise RuntimeError("fixture barrier timed out")
            return False
        time.sleep(.005)
    return True
def delayed_stat(path, *args, **kwargs):
    result = stat(path, *args, **kwargs)
    if path == install._SETUP_LOCK and result.st_mtime == 0:
        (root / ("observed-" + role)).touch()
        paired = wait(root / ("observed-fast" if role == "slow" else "observed-slow"), .3, False)
        if role == "slow" and paired:
            wait(root / "entered-fast")
    return result
Path.stat = delayed_stat
(root / ("ready-" + role)).touch()
wait(root / "go")
with install.setup_lock():
    with (root / "events").open("a") as events:
        events.write("enter " + role + "\\n")
    (root / ("entered-" + role)).touch()
    time.sleep(.2)
    with (root / "events").open("a") as events:
        events.write("leave " + role + "\\n")
'''
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            lock = root / "setup.lock"
            lock.write_text("abandoned")
            os.utime(lock, (0, 0))
            processes = [subprocess.Popen([sys.executable, "-c", script, str(root), role],
                                          cwd=Path(__file__).resolve().parents[1],
                                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                         for role in ("slow", "fast")]
            try:
                deadline = time.monotonic() + 3
                while not all((root / ("ready-" + role)).exists() for role in ("slow", "fast")):
                    self.assertLess(time.monotonic(), deadline)
                    time.sleep(.005)
                (root / "go").touch()
                for process in processes:
                    stdout, stderr = process.communicate(timeout=5)
                    self.assertEqual(process.returncode, 0, stdout + stderr)
            finally:
                for process in processes:
                    if process.poll() is None:
                        process.kill()
                    process.communicate(timeout=5)
            events = (root / "events").read_text().splitlines()
            self.assertEqual([event.split()[0] for event in events], ["enter", "leave", "enter", "leave"])


class DpsRetentionTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.directory = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(patch.object(dps_store, "_last_written", None))
        self.stack.enter_context(patch.object(dps_store, "MAX_PULLS_PER_LOG", 1))
        self.stack.enter_context(patch.object(dps_store, "MAX_LOGS", 1))

    def save(self, number, minute):
        when = datetime(2026, 11, 1, 1, minute)
        return dps_store.write_pull(self.directory,
                                    {"title": "Boss", "pull": number, "started": when.isoformat()}, when)

    def retained(self):
        return sorted(json.loads(path.read_text())["pull"]
                      for path in self.directory.glob("*.jsonl"))

    def test_clock_rollback_retains_recent_pulls_after_restarting(self):
        for number, minute in enumerate((50, 51, 20, 21), 1):
            if number >= 3:
                dps_store._last_written = None
            path = self.save(number, minute)
            self.assertEqual(json.loads(path.read_text())["started"],
                             datetime(2026, 11, 1, 1, minute).isoformat())
        self.assertEqual(self.retained(), [3, 4])

    def test_same_second_rollovers_do_not_reuse_pruned_names(self):
        names = [self.save(number, 20).name for number in range(1, 9)]
        self.assertEqual(self.retained(), [7, 8])
        self.assertEqual(len(set(names)), 8)
        self.assertEqual(names, sorted(names))

    def test_full_suffix_range_advances_the_filename_without_losing_order(self):
        previous = self.directory / "2026-11-01_01-20-00_999.jsonl"
        previous.write_text(json.dumps({"title": "Boss", "pull": 1}))
        self.assertEqual(self.save(2, 20).name, "2026-11-01_01-20-01.jsonl")
        dps_store._last_written = None
        self.assertEqual(self.save(3, 20).name, "2026-11-01_01-20-01_001.jsonl")
        self.assertEqual(self.retained(), [2, 3])

    def test_rotation_preserves_other_files_and_nested_logs(self):
        files = [self.directory / name for name in
                 ("notes.jsonl", "9999-99-99_01-00-00.jsonl", "٢٠٢٦-11-01_01-20-00.jsonl", "notes.txt")]
        nested = self.directory / "2026-11-01_23-59-59.jsonl"
        nested.mkdir()
        files.append(nested / "2026-11-01_01-00-00.jsonl")
        for path in files:
            path.write_text("saved separately")
        for number in range(5):
            self.save(number, 20)
        for path in files:
            self.assertEqual(path.read_text(), "saved separately")


class SettingsRecoveryTests(unittest.TestCase):
    def test_failed_backup_writes_never_replace_the_only_original(self):
        from nyaatriggers import app_common as ac
        from nyaatriggers.ui.settings_tab import SettingsTabMixin

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "settings.json"
            original = '{"char_name": "Previous", "cactbot_enabled": true'
            path.write_text(original)
            window = SimpleNamespace(_settings={}, _warn_save_failed=Mock())

            def partial_backup(source, destination):
                destination.write_text(original[:10])
                raise OSError("disk full")

            with patch.object(ac, "_SETTINGS_FILE", path):
                with patch("nyaatriggers.ui.settings_tab.shutil.copy2", side_effect=partial_backup):
                    SettingsTabMixin._load_settings(window)
                    window._settings["char_name"] = "Current"
                    self.assertFalse(SettingsTabMixin._save_settings(window))
                self.assertEqual(path.read_text(), original)
                self.assertTrue(SettingsTabMixin._save_settings(window))
            self.assertEqual(json.loads(path.read_text()), {"char_name": "Current"})
            self.assertIn(original, [backup.read_text() for backup in path.parent.glob("*.bad*")])

    @unittest.skipUnless(os.name == "posix" and os.geteuid() != 0, "Needs file permissions")
    def test_unreadable_settings_survive_a_failed_backup_until_permissions_recover(self):
        from nyaatriggers import app_common as ac
        from nyaatriggers.ui.settings_tab import SettingsTabMixin

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "settings.json"
            original = '{"char_name": "Previous", "cactbot_enabled": true}'
            path.write_text(original)
            path.chmod(0)
            window = SimpleNamespace(_settings={}, _warn_save_failed=Mock())
            with patch.object(ac, "_SETTINGS_FILE", path):
                try:
                    SettingsTabMixin._load_settings(window)
                    self.assertIsNone(window._settings_load_warning[1])
                    window._settings["char_name"] = "Current"
                    self.assertFalse(SettingsTabMixin._save_settings(window))
                finally:
                    path.chmod(0o600)
                self.assertEqual(path.read_text(), original)
                self.assertTrue(SettingsTabMixin._save_settings(window))
            self.assertEqual(json.loads(path.read_text()), {"char_name": "Current"})
            self.assertIn(original, [backup.read_text() for backup in path.parent.glob("*.bad*")])

    def test_shutdown_retries_a_failed_save_after_storage_recovers(self):
        from nyaatriggers import app_common as ac
        from nyaatriggers.ui.settings_tab import SettingsTabMixin

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "settings.json"
            path.write_text('{"char_name": "Previous"}')
            timer = SimpleNamespace(isActive=lambda: False, stop=Mock())
            window = SimpleNamespace(_settings={"char_name": "Current"},
                                     _settings_save_timer=timer, _warn_save_failed=Mock())
            window._save_settings = lambda: SettingsTabMixin._save_settings(window)
            with patch.object(ac, "_SETTINGS_FILE", path):
                with patch("nyaatriggers.ui.settings_tab._atomic_write_json",
                           side_effect=OSError("disk full")):
                    self.assertFalse(window._save_settings())
                    SettingsTabMixin._flush_pending_settings_save(window)
                self.assertEqual(json.loads(path.read_text()), {"char_name": "Previous"})
                SettingsTabMixin._flush_pending_settings_save(window)
                self.assertEqual(json.loads(path.read_text()), {"char_name": "Current"})
                with patch("nyaatriggers.ui.settings_tab._atomic_write_json") as write:
                    SettingsTabMixin._flush_pending_settings_save(window)
                    write.assert_not_called()
            self.assertEqual(json.loads(path.read_text()), {"char_name": "Current"})
            self.assertEqual(window._warn_save_failed.call_count, 2)


class CustomVoiceStartupTests(unittest.TestCase):
    def setUp(self):
        import install
        import main
        from PyQt6.QtWidgets import QApplication

        self.app = QApplication.instance() or QApplication([])
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.default = self.root / "default"
        self.custom = self.root / "custom voice"
        self.settings = self.root / "nyaatriggers_settings.json"
        self.stack.enter_context(patch.object(main, "data_root", return_value=self.root))
        self.stack.enter_context(patch.object(main, "_FFXIV_VENV", self.default))
        self.stack.enter_context(patch.object(main, "_voice_present", return_value=True))
        self.stack.enter_context(patch.object(sys, "frozen", False, create=True))
        self.stack.enter_context(patch.object(install, "_SETUP_LOCK", self.root / "setup.lock"))
        self.main, self.install = main, install

    @staticmethod
    def healthy(directory):
        directory.mkdir(parents=True)
        (directory / "pyvenv.cfg").write_text(
            f"version = {sys.version_info.major}.{sys.version_info.minor}.0\n")
        packages = Path(sysconfig.get_path("purelib", scheme="venv",
                        vars={"base": str(directory), "platbase": str(directory)}))
        (packages / "piper").mkdir(parents=True)

    def select(self, value):
        self.settings.write_text(json.dumps({"venv_path": value}))

    def run_setup(self, runner, sleep=lambda _seconds: None):
        worker = self.main._SetupWorker()
        done = []
        worker.done.connect(lambda success, error: done.append((success, error)))
        with patch.object(self.main.time, "sleep", side_effect=sleep), \
                patch.object(self.install, "run_setup_command", side_effect=runner):
            worker.run()
        self.assertEqual(len(done), 1)
        return done[0]

    def test_healthy_custom_environment_does_not_install_missing_default(self):
        self.healthy(self.custom)
        self.select(str(self.custom))
        self.assertFalse(self.main._needs_setup())
        runner = Mock()
        self.assertEqual(self.run_setup(runner), (True, ""))
        runner.assert_not_called()
        self.assertFalse(self.default.exists())

    def test_old_custom_environment_is_repaired_without_touching_healthy_default(self):
        self.healthy(self.default)
        original = VoiceEnvironmentRecoveryTests.old_environment(self, self.custom, kokoro=False)
        self.select(str(self.custom))
        self.assertTrue(self.main._needs_setup())
        fake = VoiceEnvironmentRecoveryTests.fake_installer(self.custom)
        self.assertEqual(self.run_setup(lambda args, *_args, **_kwargs: fake(args, 0)), (True, ""))
        backup, = self.root.glob("custom voice.backup-*")
        self.assertEqual({p.relative_to(backup): p.read_bytes() for p in backup.rglob("*") if p.is_file()},
                         original)
        self.assertFalse(self.main._needs_setup())
        self.assertFalse(list(self.root.glob("default.backup-*")))

    def test_failed_custom_repair_preserves_original_and_retries_the_same_choice(self):
        self.healthy(self.default)
        original = VoiceEnvironmentRecoveryTests.old_environment(self, self.custom, kokoro=False)
        self.select(str(self.custom))
        fake = VoiceEnvironmentRecoveryTests.fake_installer(self.custom)

        def fail(args, *_args, **_kwargs):
            if args[1] == "install":
                raise subprocess.CalledProcessError(1, args, stderr="Offline installer")
            fake(args, 0)

        success, error = self.run_setup(fail)
        self.assertFalse(success)
        self.assertIn("Offline installer", error)
        self.assertEqual({p.relative_to(self.custom): p.read_bytes()
                          for p in self.custom.rglob("*") if p.is_file()}, original)
        self.assertTrue(self.main._needs_setup())
        self.assertEqual(json.loads(self.settings.read_text())["venv_path"], str(self.custom))

    def test_one_attempt_keeps_its_resolved_destination_when_settings_change(self):
        self.healthy(self.default)
        VoiceEnvironmentRecoveryTests.old_environment(self, self.custom, kokoro=False)
        self.select(str(self.custom))
        other = self.root / "later choice"
        fake = VoiceEnvironmentRecoveryTests.fake_installer(self.custom)

        def changed(_seconds):
            self.select(str(other))

        self.assertEqual(self.run_setup(lambda args, *_args, **_kwargs: fake(args, 0), changed), (True, ""))
        self.assertEqual(self.install.voice_venv_version(self.custom), sys.version_info[:2])
        self.assertFalse(other.exists())

    def test_saved_path_semantics_defaults_and_frozen_check(self):
        for value in (None, 1, [], "", "   "):
            with self.subTest(value=value):
                self.select(value)
                self.assertEqual(self.main._configured_voice_venv(), self.default)
        self.select("  " + str(self.custom) + "  ")
        self.assertEqual(self.main._configured_voice_venv(), self.custom)
        self.select("~/selected voice")
        self.assertEqual(self.main._configured_voice_venv(), Path.home() / "selected voice")
        for raw in (b"[]", b'{"venv_path":', b"[" * 2000, b" " * ((4 << 20) + 1)):
            with self.subTest(length=len(raw)):
                self.settings.write_bytes(raw)
                self.assertEqual(self.main._configured_voice_venv(), self.default)
                self.assertEqual(self.settings.read_bytes(), raw)
        self.select(str(self.custom))
        with patch.object(sys, "frozen", True, create=True):
            self.assertTrue(self.main._piper_installed())

    def test_interrupted_custom_repair_is_recovered_even_if_new_packages_exist(self):
        self.healthy(self.default)
        original = VoiceEnvironmentRecoveryTests.old_environment(self, self.custom, kokoro=False)
        backup = self.custom.with_name(self.custom.name + ".backup-" + "a" * 32)
        self.install._start_voice_repair(self.custom, backup)
        self.healthy(self.custom)
        (self.custom / "unfinished.txt").write_text("discard incomplete replacement")
        self.select(str(self.custom))
        self.assertTrue(self.main._needs_setup())
        fake = VoiceEnvironmentRecoveryTests.fake_installer(self.custom)
        self.assertEqual(self.run_setup(lambda args, *_args, **_kwargs: fake(args, 0)), (True, ""))
        self.assertFalse(self.install.voice_repair_pending(self.custom))
        self.assertFalse((self.custom / "unfinished.txt").exists())
        retained, = self.root.glob("custom voice.backup-*")
        self.assertEqual({p.relative_to(retained): p.read_bytes()
                          for p in retained.rglob("*") if p.is_file()}, original)
        self.assertFalse(self.main._needs_setup())

    def test_unavailable_saved_home_uses_default_without_rewriting_settings(self):
        self.healthy(self.default)
        self.select("~unavailable-home/voice")
        previous = self.settings.read_bytes()
        with patch.object(self.main, "resolve_voice_venv", side_effect=RuntimeError("Home unavailable")):
            self.assertFalse(self.main._needs_setup())
        self.assertEqual(self.settings.read_bytes(), previous)


class VoicePathEditTests(unittest.TestCase):
    def setUp(self):
        from nyaatriggers import app_common as ac
        from nyaatriggers.ui import voice_tab

        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.previous = str(self.root / "previous")
        self.edit = SimpleNamespace(value="")
        self.edit.text = lambda: self.edit.value
        self.edit.setText = lambda value: setattr(self.edit, "value", value)
        self.host = SimpleNamespace(_settings={"venv_path": self.previous},
                                    _venv_edit=self.edit, _save_settings=Mock())
        self.warning = self.stack.enter_context(patch.object(ac.QMessageBox, "warning"))
        self.apply = self.stack.enter_context(patch.object(voice_tab, "set_venv_path"))
        self.voice_tab = voice_tab

    def test_bad_home_and_runtime_failure_preserve_saved_and_active_choice(self):
        for failure in ("home", "runtime"):
            with self.subTest(failure=failure):
                self.edit.value = str(self.root / "new voice")
                self.apply.reset_mock()
                if failure == "home":
                    failure_patch = patch.object(self.voice_tab, "resolve_voice_venv",
                                                 side_effect=RuntimeError("Home unavailable"))
                else:
                    failure_patch = patch.object(self.voice_tab, "set_venv_path",
                                                 side_effect=OSError("Environment unreadable"))
                with failure_patch:
                    self.voice_tab.VoiceTabMixin._on_venv_changed(self.host)
                self.assertEqual(self.host._settings["venv_path"], self.previous)
                self.assertEqual(self.edit.value, self.previous)
                self.host._save_settings.assert_not_called()
                self.apply.assert_not_called()
                self.assertTrue(self.warning.called)

    def test_valid_new_paths_and_whitespace_are_applied_before_saving(self):
        for value in ("  " + str(self.root / "not created") + "  ", "   ", "~/new voice"):
            with self.subTest(value=value):
                self.edit.value = value
                order = []
                self.apply.side_effect = lambda path: order.append(("apply", path))
                self.host._save_settings.side_effect = lambda: order.append(("save", None))
                self.voice_tab.VoiceTabMixin._on_venv_changed(self.host)
                self.assertEqual(order, [("apply", value.strip()), ("save", None)])
                self.assertEqual(self.host._settings["venv_path"], value.strip())
        self.warning.assert_not_called()
        self.assertEqual(list(self.root.iterdir()), [])


class VoiceConfigSelectionTests(unittest.TestCase):
    def setUp(self):
        from nyaatriggers import paths
        from nyaatriggers.ui import voice_tab
        from nyaatriggers import app_common as ac

        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.user = self.root / "voices"
        self.bundle = self.root / "_internal" / "voices"
        self.stem = "en_US-arctic-medium"
        self.valid = (Path(__file__).resolve().parents[1] / "voices" /
                      f"{self.stem}.onnx.json").read_bytes()
        for directory in (self.user, self.bundle):
            directory.mkdir(parents=True)
            (directory / f"{self.stem}.onnx").write_bytes(b"model")
        self.stack.enter_context(patch.object(paths, "data_root", return_value=self.root))
        self.stack.enter_context(patch.object(paths, "bundle_root", return_value=self.bundle.parent))
        self.stack.enter_context(patch.object(ac, "_USER_VOICES_DIR", self.user))
        self.stack.enter_context(patch.object(ac, "_BUNDLE_DIR", self.bundle.parent))
        self.paths, self.voice_tab = paths, voice_tab

    @unittest.skipUnless(hasattr(os, "mkfifo"), "Named pipes require POSIX")
    def test_special_config_files_cannot_block_startup(self):
        config = self.user / f"{self.stem}.onnx.json"
        os.mkfifo(config)
        result = subprocess.run(
            [sys.executable, "-c", "from pathlib import Path; import sys; "
             "from nyaatriggers.voice_config import voice_config_ok; "
             "print(voice_config_ok(Path(sys.argv[1])))", str(config)],
            cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "False")
        self.assertEqual(self.paths.default_voice_dir(), self.user)
        os.mkfifo(self.user / "pyvenv.cfg")
        result = subprocess.run(
            [sys.executable, "-c", "from pathlib import Path; import sys, install; "
             "print(install.voice_venv_version(Path(sys.argv[1])), flush=True); "
             "install.prepare_voice_venv(Path(sys.argv[1]))", str(self.user)],
            cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=5)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), "None")
        self.assertIn("Cannot verify the voice environment", result.stderr)
        self.assertEqual(list(self.root.glob("voices.backup-*")), [])

    def test_invalid_user_config_uses_valid_bundle_without_repairing_user_bytes(self):
        import main

        config = self.user / f"{self.stem}.onnx.json"
        config.write_bytes(b"")
        (self.bundle / config.name).write_bytes(self.valid)
        self.assertEqual(self.paths.default_voice_dir(), self.bundle)
        self.assertEqual(self.voice_tab.VoiceTabMixin._scan_voices(None),
                         [(self.stem, self.bundle / f"{self.stem}.onnx")])
        with patch.object(main, "_VOICE_FILE", self.paths.default_voice_dir() / f"{self.stem}.onnx"), \
                patch.object(main, "_VOICE_CONFIG", self.paths.default_voice_dir() / config.name), \
                patch.object(sys, "frozen", True, create=True):
            self.assertFalse(main._needs_setup())
        self.assertEqual(config.read_bytes(), b"")

    def test_valid_user_config_keeps_precedence_and_invalid_all_remains_repairable(self):
        import main

        for directory in (self.user, self.bundle):
            (directory / f"{self.stem}.onnx.json").write_bytes(self.valid)
        self.assertEqual(self.paths.default_voice_dir(), self.user)
        self.assertEqual(self.voice_tab.VoiceTabMixin._scan_voices(None),
                         [(self.stem, self.user / f"{self.stem}.onnx")])
        for directory in (self.user, self.bundle):
            (directory / f"{self.stem}.onnx.json").write_bytes(b"{}")
        self.assertEqual(self.paths.default_voice_dir(), self.user)
        with patch.object(main, "_VOICE_FILE", self.user / f"{self.stem}.onnx"), \
                patch.object(main, "_VOICE_CONFIG", self.user / f"{self.stem}.onnx.json"), \
                patch.object(sys, "frozen", True, create=True):
            self.assertTrue(main._needs_setup())
        custom = self.user / "custom.onnx"
        custom.write_bytes(b"custom")
        custom.with_suffix(".onnx.json").write_bytes(b"{}")
        self.assertIn(("custom", custom), self.voice_tab.VoiceTabMixin._scan_voices(None))


class VoiceConfigRecoveryTests(unittest.TestCase):
    def setUp(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        import threading
        import install
        import main
        from PyQt6.QtWidgets import QApplication

        self.app = QApplication.instance() or QApplication([])
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.model = self.root / "en_US-arctic-medium.onnx"
        self.config = self.root / "en_US-arctic-medium.onnx.json"
        self.model.write_bytes(b"existing pinned model")
        self.valid = (Path(__file__).resolve().parents[1] / "voices" / self.config.name).read_bytes()
        self.payload = self.valid
        self.requests = []
        case = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                case.requests.append(self.path)
                self.send_response(200)
                self.end_headers()
                self.wfile.write(case.payload)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        thread.start()

        def close():
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        self.addCleanup(close)
        self.url = f"http://127.0.0.1:{server.server_port}"
        for module, values in ((main, {"_VOICES_DIR": self.root, "_VOICE_FILE": self.model,
                                       "_VOICE_CONFIG": self.config, "_VOICE_BASE": self.url}),
                               (install, {"VOICES_DIR": self.root, "VOICE_FILE": self.model,
                                          "VOICE_BASE": self.url,
                                          "VOICE_ONNX_SHA256": install._sha256(self.model)})):
            for key, value in values.items():
                self.stack.enter_context(patch.object(module, key, value))
        self.stack.enter_context(patch.object(main, "_piper_installed", return_value=True))
        self.main, self.install = main, install

    def run_setup(self):
        worker = self.main._SetupWorker()
        done = []
        worker.done.connect(lambda success, error: done.append((success, error)))
        with patch.object(self.main.time, "sleep"):
            worker.run()
        self.assertEqual(len(done), 1)
        return done[0]

    def test_empty_successful_response_stays_retryable_through_gui_restart(self):
        self.payload = b""
        success, error = self.run_setup()
        self.assertFalse(success)
        self.assertTrue(error)
        self.assertFalse(self.config.exists())
        self.assertTrue(self.main._needs_setup())
        self.payload = self.valid
        self.assertEqual(self.run_setup(), (True, ""))
        self.assertEqual(self.config.read_bytes(), self.valid)
        self.assertFalse(self.main._needs_setup())
        self.assertEqual(len(self.requests), 2)
        self.assertFalse(list(self.root.glob("*.part")))

    def test_invalid_downloads_preserve_a_working_config(self):
        self.config.write_bytes(self.valid)
        for payload in (b"", b'{"audio":', b"<html>Unavailable</html>", b"[]", b"{}"):
            with self.subTest(payload=payload):
                self.payload = payload
                with self.assertRaises((OSError, ValueError)):
                    self.main._download(self.url, self.config)
                self.assertEqual(self.config.read_bytes(), self.valid)
                self.assertFalse(list(self.root.glob("*.part")))

    def test_cli_repairs_existing_bad_config_only_after_a_valid_download(self):
        for previous in (b"", b'{"audio":', b"[]"):
            with self.subTest(previous=previous):
                self.config.write_bytes(previous)
                self.payload = b"<html>Unavailable</html>"
                self.assertTrue(self.main._needs_setup())
                with self.assertRaises((OSError, ValueError)):
                    self.install.download_voice()
                self.assertEqual(self.config.read_bytes(), previous)
                self.assertFalse(list(self.root.glob("*.part")))
                self.payload = self.valid
                self.install.download_voice()
                self.assertEqual(self.config.read_bytes(), self.valid)
                self.assertFalse(self.main._needs_setup())
        self.assertEqual(self.model.read_bytes(), b"existing pinned model")
        self.assertTrue(all(path.endswith(".onnx.json") for path in self.requests))

    def test_supported_optional_config_variants_skip_unnecessary_downloads(self):
        for phonemes in (None, "espeak", "text", "pinyin"):
            with self.subTest(phonemes=phonemes):
                config = json.loads(self.valid)
                for key in ("inference", "speaker_id_map", "piper_version", "phoneme_type"):
                    config.pop(key, None)
                if phonemes is not None:
                    config["phoneme_type"] = phonemes
                if phonemes == "text":
                    config["espeak"]["voice"] = ""
                raw = json.dumps(config).encode()
                self.config.write_bytes(raw)
                self.assertFalse(self.main._needs_setup())
                self.install.download_voice()
                self.assertEqual(self.requests, [])
                self.assertEqual(self.config.read_bytes(), raw)

    def test_oversized_and_deep_configs_are_rejected_without_replacing_previous_bytes(self):
        for payload in (b"[" * 2000 + b"]" * 2000, b" " * ((1 << 20) + 1)):
            with self.subTest(size=len(payload)):
                self.config.write_bytes(payload)
                self.assertTrue(self.main._needs_setup())
                self.config.write_bytes(self.valid)
                self.payload = payload
                with self.assertRaises((OSError, ValueError, RecursionError)):
                    self.main._download(self.url, self.config)
                self.assertEqual(self.config.read_bytes(), self.valid)
                self.assertFalse(list(self.root.glob("*.part")))

    def test_working_numeric_metadata_is_preserved(self):
        for variant in ("floats", "unused_zero_symbols"):
            with self.subTest(variant=variant):
                config = json.loads(self.valid)
                if variant == "floats":
                    config["num_symbols"] = float(config["num_symbols"])
                    config["num_speakers"] = float(config["num_speakers"])
                    config["audio"]["sample_rate"] = float(config["audio"]["sample_rate"])
                else:
                    config["num_symbols"] = 0
                raw = json.dumps(config).encode()
                self.config.write_bytes(raw)
                self.assertFalse(self.main._needs_setup())
                self.install.download_voice()
                self.assertEqual(self.requests, [])
                self.assertEqual(self.config.read_bytes(), raw)

    def test_partial_write_and_failed_replace_leave_the_previous_config_retryable(self):
        opened = Path.open

        class PartialWrite:
            def __init__(self, stream):
                self.stream = stream

            def __enter__(self):
                self.stream.__enter__()
                return self

            def __exit__(self, *args):
                return self.stream.__exit__(*args)

            def write(self, payload):
                self.stream.write(payload[:8])
                self.stream.flush()
                raise OSError(errno.ENOSPC, "disk full")

        def open_part(path, mode="r", *args, **kwargs):
            stream = opened(path, mode, *args, **kwargs)
            return PartialWrite(stream) if path.suffix == ".part" and mode == "wb" else stream

        for failure in ("write", "replace"):
            with self.subTest(failure=failure):
                self.config.write_bytes(b'{"unfinished":')
                failing = (patch.object(Path, "open", open_part) if failure == "write" else
                           patch.object(updater.os, "replace", side_effect=PermissionError("locked")))
                with failing, self.assertRaises(OSError):
                    self.main._download(self.url, self.config)
                self.assertEqual(self.config.read_bytes(), b'{"unfinished":')
                self.assertFalse(list(self.root.glob("*.part")))
                self.assertTrue(self.main._needs_setup())
                self.main._download(self.url, self.config)
                self.assertFalse(self.main._needs_setup())
                self.assertEqual(self.config.read_bytes(), self.valid)


class UpdateRecoveryTests(unittest.TestCase):
    def test_windows_update_does_not_duplicate_a_process_that_has_not_exited(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            installed = root / "NyaaTriggers"
            staged = root / ".nyaa-update-test"
            for directory, version in ((installed, "old"), (staged, "new")):
                (directory / "_internal").mkdir(parents=True)
                (directory / "_internal" / "runtime.dll").write_text(version)
                (directory / "NyaaTriggers.exe").write_text(version)
            with patch.object(updater, "_wait_for_pid_exit", return_value=False) as wait, \
                    patch.object(updater, "_relaunch_installed") as relaunch:
                updater.finish_windows_update(installed, staged, os.getpid(), "NyaaTriggers.exe")
            wait.assert_called_once_with(os.getpid())
            relaunch.assert_not_called()
            self.assertEqual((installed / "NyaaTriggers.exe").read_text(), "old")
            self.assertEqual((installed / "_internal" / "runtime.dll").read_text(), "old")
            self.assertEqual((staged / "NyaaTriggers.exe").read_text(), "new")
            self.assertFalse(list(installed.glob("*.nyaa-old")))
            self.assertIn("swap skipped", (installed / updater._UPDATE_LOG_NAME).read_text())


if __name__ == "__main__":
    unittest.main()
