"""Challenge recovery and error handling after the first release audit fixes."""

import io
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
import zipfile
from unittest.mock import Mock, patch

from nyaatriggers import updater


REPO = Path(__file__).resolve().parent.parent


class ReleaseFollowupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.dest = self.root / "installed program"
        (self.dest / "_internal").mkdir(parents=True)
        (self.dest / "_internal/runtime").write_text("old")
        (self.dest / "NyaaTriggers").write_bytes(self.executable("old"))
        (self.dest / "NyaaTriggers").chmod(0o755)
        shutil.copy2(REPO / "NyaaTriggers.sh", self.dest / "NyaaTriggers.sh")

    @staticmethod
    def executable(generation):
        return (f'#!/bin/sh\nprintf "{generation} "\n'
                'cat "$(dirname "$0")/_internal/runtime"\n').encode()

    def archive(self, generation, launcher=True):
        archive = self.root / f"{generation}.tar.gz"
        files = {
            "NyaaTriggers/_internal/runtime": generation.encode(),
            "NyaaTriggers/NyaaTriggers": self.executable(generation),
        }
        if launcher is not None:
            files["NyaaTriggers/NyaaTriggers.sh"] = (
                (REPO / "NyaaTriggers.sh").read_bytes() if launcher is True else launcher)
        with tarfile.open(archive, "w:gz") as tar:
            for name, data in files.items():
                entry = tarfile.TarInfo(name)
                entry.size = len(data)
                tar.addfile(entry, io.BytesIO(data))
        return archive

    def launch(self, **kwargs):
        return subprocess.run(["sh", str(self.dest / "NyaaTriggers.sh")],
                              cwd=self.root, capture_output=True, text=True,
                              timeout=5, **kwargs)

    @unittest.skipUnless(sys.platform == "linux" and os.geteuid() != 0,
                         "Needs Linux file permissions")
    def test_readonly_healthy_install_launches(self):
        lock = self.dest / ".nyaa-update.lock"
        for existing_lock in (False, True):
            with self.subTest(existing_lock=existing_lock):
                if existing_lock:
                    lock.write_text("0")
                    lock.chmod(0o444)
                self.dest.chmod(0o555)
                try:
                    result = self.launch()
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout, "old old")
                finally:
                    self.dest.chmod(0o755)
                    if lock.exists():
                        lock.chmod(0o644)

    @unittest.skipUnless(sys.platform == "linux", "Linux launcher")
    def test_launcher_obeys_the_install_lock(self):
        with updater._update_lock(self.dest):
            result = self.launch()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertIn("update is running", result.stderr)
        self.assertEqual(self.launch().stdout, "old old")

    @unittest.skipUnless(sys.platform == "linux" and os.geteuid() != 0,
                         "Needs Linux file permissions")
    def test_readonly_pending_recovery_does_not_launch_or_change_files(self):
        pending = self.dest / ".nyaa-linux-update"
        pending.write_text("123\nNyaaTriggers\n")
        self.dest.chmod(0o555)
        try:
            result = self.launch()
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(result.stdout, "")
            self.assertEqual(pending.read_text(), "123\nNyaaTriggers\n")
            self.assertEqual((self.dest / "NyaaTriggers").read_bytes(), self.executable("old"))
            self.assertEqual((self.dest / "_internal/runtime").read_text(), "old")
        finally:
            self.dest.chmod(0o755)

    @unittest.skipUnless(sys.platform == "linux", "Linux launcher")
    def test_repeated_update_recovers_the_immediately_previous_pair(self):
        first = self.archive("first")
        second = self.archive("second")
        code = '''
import os, sys
from pathlib import Path
from nyaatriggers import updater
dest, first, second = map(Path, sys.argv[1:])
ok, message = updater.apply_frozen_linux(first, dest, "NyaaTriggers")
assert ok, message
real = os.replace
def replace(src, dst):
    real(src, dst)
    if Path(dst) == dest / ".nyaa-linux-update":
        os._exit(90)
updater.os.replace = replace
updater.apply_frozen_linux(second, dest, "NyaaTriggers")
'''
        result = subprocess.run([sys.executable, "-c", code, str(self.dest),
                                 str(first), str(second)], cwd=REPO, timeout=10)
        self.assertEqual(result.returncode, 90)
        launched = self.launch()
        self.assertEqual(launched.returncode, 0, launched.stderr)
        self.assertEqual(launched.stdout, "first first")

    @unittest.skipUnless(sys.platform == "linux", "Linux launcher")
    def test_launcher_finishes_an_interrupted_rollback(self):
        archive = self.archive("new")
        code = '''
import os, sys
from pathlib import Path
from nyaatriggers import updater
dest, archive = map(Path, sys.argv[1:3])
boundary = sys.argv[3]
real_unlink, real_replace = Path.unlink, os.replace
def unlink(path, *args, **kwargs):
    if path == dest / ".nyaa-linux-update":
        raise OSError("injected commit failure")
    return real_unlink(path, *args, **kwargs)
def replace(src, dst):
    real_replace(src, dst)
    if str(src).endswith(".nyaa-old") and Path(dst).name == boundary:
        os._exit(90)
Path.unlink, updater.os.replace = unlink, replace
updater.apply_frozen_linux(archive, dest, "NyaaTriggers")
'''
        for boundary in ("_internal", "NyaaTriggers"):
            with self.subTest(boundary=boundary):
                result = subprocess.run([sys.executable, "-c", code, str(self.dest),
                                         str(archive), boundary], cwd=REPO, timeout=10)
                self.assertEqual(result.returncode, 90)
                launched = self.launch()
                self.assertEqual(launched.returncode, 0, launched.stderr)
                self.assertEqual(launched.stdout, "old old")
                self.assertFalse((self.dest / ".nyaa-linux-update").exists())

    def test_deeply_nested_cache_is_ignored(self):
        cache = self.dest / "cache.json"
        cache.write_text("[" * 100000 + "0" + "]" * 100000)
        with patch.object(updater, "_release_cache_path", return_value=cache):
            self.assertIsNone(updater.read_cached_release())

    def test_rate_limit_fallback_failure_still_completes_manual_check(self):
        from nyaatriggers import updater_ui
        window = SimpleNamespace(
            _settings={}, _upd_available_signal=Mock(), _upd_checkmsg_signal=Mock())
        with patch.object(updater_ui.threading, "Thread") as worker, \
                patch.object(updater, "fetch_latest_release", side_effect=updater.RateLimited()), \
                patch.object(updater, "read_cached_release", side_effect=OSError("cache failed")):
            updater_ui.UpdaterUiMixin._start_update_check(window, manual=True)
            try:
                worker.call_args.kwargs["target"]()
            except OSError:
                pass
        window._upd_checkmsg_signal.emit.assert_called_once()
        self.assertTrue(window._upd_checkmsg_signal.emit.call_args.args[0])

    def test_failed_executable_rollback_names_the_surviving_backup(self):
        archive = self.archive("new")
        real_unlink, real_replace = Path.unlink, os.replace

        def unlink(path, *args, **kwargs):
            if path == self.dest / ".nyaa-linux-update":
                raise OSError("injected commit failure")
            return real_unlink(path, *args, **kwargs)

        def replace(src, dst):
            if str(src).endswith(".nyaa-old") and Path(dst) == self.dest / "NyaaTriggers":
                raise OSError("injected executable restore failure")
            return real_replace(src, dst)

        with patch.object(Path, "unlink", unlink), patch.object(updater.os, "replace", replace):
            ok, message = updater.apply_frozen_linux(archive, self.dest, "NyaaTriggers")
        self.assertFalse(ok, message)
        backup, = self.dest.glob("NyaaTriggers.*.nyaa-old")
        self.assertEqual(backup.read_bytes(), self.executable("old"))
        self.assertIn(backup.name, (self.dest / "RECOVER.txt").read_text())
        updater.cleanup_old_backups(self.dest)
        self.assertTrue(backup.exists())
        self.assertEqual(self.launch().stdout, "old old")

    def test_launcher_failure_stops_before_runtime_swap(self):
        archive = self.archive("new")
        for operation in ("copy", "chmod", "replace"):
            with self.subTest(operation=operation):
                real_copy, real_chmod, real_replace = shutil.copy2, os.chmod, os.replace

                def copy(src, dst, *args, **kwargs):
                    if operation == "copy" and Path(src).name == "NyaaTriggers.sh":
                        raise OSError("injected launcher copy failure")
                    return real_copy(src, dst, *args, **kwargs)

                def chmod(path, *args, **kwargs):
                    if operation == "chmod" and Path(path).name.startswith("NyaaTriggers.sh."):
                        raise OSError("injected launcher chmod failure")
                    return real_chmod(path, *args, **kwargs)

                def replace(src, dst, *args, **kwargs):
                    if operation == "replace" and Path(dst).name == "NyaaTriggers.sh":
                        raise OSError("injected launcher replace failure")
                    return real_replace(src, dst, *args, **kwargs)

                with patch.object(shutil, "copy2", copy), patch.object(os, "chmod", chmod), \
                        patch.object(os, "replace", replace):
                    ok, message = updater.apply_frozen_linux(archive, self.dest, "NyaaTriggers")
                self.assertFalse(ok, message)
                self.assertEqual(self.launch().stdout, "old old")
                self.assertFalse((self.dest / ".nyaa-linux-update").exists())
                self.assertFalse(list(self.dest.glob("*.nyaa-old")))

    def test_archive_requires_a_recovery_launcher(self):
        for launcher in (None, b'#!/bin/sh\nexec ./NyaaTriggers "$@"\n'):
            with self.subTest(launcher=launcher):
                archive = self.archive("new", launcher=launcher)
                ok, message = updater.apply_frozen_linux(archive, self.dest, "NyaaTriggers")
                self.assertFalse(ok, message)
                self.assertEqual(self.launch().stdout, "old old")
                self.assertFalse((self.dest / ".nyaa-linux-update").exists())

    def test_unreadable_boot_marker_does_not_override_process_state(self):
        for exit_code in (None, 1):
            with self.subTest(exit_code=exit_code):
                def launch(*args):
                    (self.dest / updater._BOOT_OK_MARKER).write_bytes(b"\xff")
                    return SimpleNamespace(pid=12345, poll=lambda: exit_code)

                with patch.object(updater, "_relaunch_installed", side_effect=launch):
                    self.assertEqual(updater._relaunch_and_verify(
                        self.dest / "NyaaTriggers", self.dest, grace=.05), exit_code is None)

    def test_oversized_boot_marker_cannot_confirm_a_dead_process(self):
        def launch(*args):
            (self.dest / updater._BOOT_OK_MARKER).write_bytes(b"12345" + b" " * 100000)
            return SimpleNamespace(pid=12345, poll=lambda: 1)

        with patch.object(updater, "_relaunch_installed", side_effect=launch), \
                patch.object(updater.time, "sleep"):
            self.assertFalse(updater._relaunch_and_verify(
                self.dest / "NyaaTriggers", self.dest, grace=.1))

    def test_voice_restore_keeps_the_complete_default_pair(self):
        from PyQt6.QtWidgets import QApplication, QComboBox
        from nyaatriggers import app_common as ac, paths
        from nyaatriggers.ui import voice_tab

        application = QApplication.instance() or QApplication([])
        stem = "en_US-arctic-medium"
        bundle = self.dest / "_internal"
        for directory in (self.dest / "voices", bundle / "voices"):
            directory.mkdir()
            (directory / f"{stem}.onnx").write_bytes(b"model")
        (bundle / "voices" / f"{stem}.onnx.json").write_text("{}")
        window = SimpleNamespace(_settings={"voice_model": stem}, _voice_combo=QComboBox())
        with patch.object(ac, "_USER_VOICES_DIR", self.dest / "voices"), \
                patch.object(ac, "_BUNDLE_DIR", bundle), \
                patch.object(paths, "data_root", return_value=self.dest), \
                patch.object(paths, "bundle_root", return_value=bundle), \
                patch.object(voice_tab, "set_model") as set_model:
            for repaired in (False, True):
                with self.subTest(repaired=repaired):
                    if repaired:
                        (self.dest / "voices" / f"{stem}.onnx.json").write_text("{}")
                    window._voice_combo.clear()
                    for name, path in voice_tab.VoiceTabMixin._scan_voices(window):
                        window._voice_combo.addItem(name, str(path))
                    voice_tab.VoiceTabMixin._restore_voice_model(window)
                    set_model.assert_called_with(paths.default_voice_dir() / f"{stem}.onnx")
        window._voice_combo.deleteLater()
        application.processEvents()

    def test_parent_refuses_handoff_when_the_install_lock_is_busy(self):
        exe = self.dest / "NyaaTriggers.exe"
        exe.write_bytes(b"old executable")
        archive = self.root / "update.zip"
        with zipfile.ZipFile(archive, "w") as zipped:
            zipped.writestr("NyaaTriggers/NyaaTriggers.exe", b"new executable")
            zipped.writestr("NyaaTriggers/_internal/runtime", b"new")
        children = []
        real_popen = subprocess.Popen

        def start(cmd, **kwargs):
            kwargs.pop("creationflags")
            child = real_popen([sys.executable, str(REPO / "main.py"), *cmd[1:]],
                               stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, **kwargs)
            children.append(child)
            return child

        with updater._update_lock(self.dest):
            try:
                with patch.object(subprocess, "Popen", side_effect=start):
                    ok, message = updater.apply_frozen_windows(
                        archive, self.dest, exe.name, version="1.4.0.100")
            finally:
                for child in children:
                    if child.poll() is None:
                        child.terminate()
                    child.communicate(timeout=3)
        self.assertFalse(ok, "The parent agreed to quit while the helper was waiting for the lock")
        self.assertIn("refused", message)
        self.assertEqual(exe.read_bytes(), b"old executable")
        self.assertEqual((self.dest / "_internal/runtime").read_text(), "old")


if __name__ == "__main__":
    unittest.main(verbosity=2)
