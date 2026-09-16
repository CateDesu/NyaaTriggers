"""Exercise update failures with real files, processes and HTTP peers."""

from contextlib import ExitStack
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
import urllib.request
from unittest.mock import Mock, patch

import install
from nyaatriggers import updater
from tests.test_transport_deadlines import HttpPeer, wait_for


REPO = Path(__file__).resolve().parent.parent


class ReleaseRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.dest = self.root / "NyaaTriggers"
        (self.dest / "_internal").mkdir(parents=True)
        (self.dest / "_internal" / "runtime").write_text("old")
        (self.dest / "NyaaTriggers").write_text("old executable")

    def test_fresh_staging_survives_another_launch(self):
        stage = self.root / ".nyaa-update-active"
        (stage / "NyaaTriggers" / "_internal").mkdir(parents=True)
        (stage / "NyaaTriggers" / "_internal" / "runtime").write_text("new")
        updater.cleanup_old_backups(self.dest)
        self.assertTrue(stage.exists(), "Startup removed an active update")

    def test_missing_runtime_preserves_both_halves_of_backup(self):
        shutil.rmtree(self.dest / "_internal")
        internal = self.dest / "_internal.123.nyaa-old"
        internal.mkdir()
        (internal / "runtime").write_text("old")
        exe = self.dest / "NyaaTriggers.123.nyaa-old"
        exe.write_text("old executable")
        updater.cleanup_old_backups(self.dest)
        self.assertTrue(exe.exists(), "Recovery lost the matching executable")
        self.assertTrue(internal.exists())

    def test_other_process_boot_marker_does_not_accept_dead_build(self):
        def launch(*args):
            (self.dest / updater._BOOT_OK_MARKER).write_text("99999")
            return SimpleNamespace(pid=12345, poll=lambda: 1)

        with patch.object(updater, "_relaunch_installed", side_effect=launch), \
                patch.object(updater.time, "sleep"):
            self.assertFalse(updater._relaunch_and_verify(
                self.dest / "NyaaTriggers", self.dest, grace=.1))

    def test_git_worktree_is_a_git_install(self):
        source = self.root / "source"
        source.mkdir()
        subprocess.run(["git", "init", "-q", str(source)], check=True)
        subprocess.run(["git", "-C", str(source), "-c", "user.name=Test",
                        "-c", "user.email=test@example.invalid", "commit",
                        "--allow-empty", "-qm", "initial"], check=True)
        worktree = self.root / "worktree"
        subprocess.run(["git", "-C", str(source), "worktree", "add", "--detach",
                        str(worktree)], check=True, capture_output=True)
        with patch.object(updater, "source_dir", return_value=worktree), \
                patch.object(updater, "is_frozen", return_value=False):
            self.assertEqual(updater.install_kind(), "git")

    def test_invalid_cached_version_is_not_offered(self):
        cache = self.dest / "latest_release.json"
        cache.write_text(json.dumps({"tag": "v9.0.0", "version": "².0"}))
        with patch.object(updater, "_release_cache_path", return_value=cache):
            release = updater.read_cached_release()
        self.assertTrue(release is None or release.version == "9.0.0")

    def test_new_source_bundle_expires_same_version_download(self):
        from nyaatriggers import app_common as ac
        bundle = self.dest / "triggers.json"
        stamp = self.dest / "triggers.repo.version"
        stamp.write_text(json.dumps(ac._VERSION))
        bundle.write_text("[]")
        os.utime(stamp, (100, 100))
        os.utime(bundle, (200, 200))
        with patch.object(ac, "TRIGGERS_FILE", bundle), \
                patch.object(ac, "RETIRED_FILE", self.dest / "retired.json"), \
                patch.object(ac, "_REPO_TRIGGERS_VERSION", stamp), \
                patch.object(updater, "is_frozen", return_value=False):
            self.assertIsNone(ac._repo_download_version())
            os.utime(stamp, (300, 300))
            self.assertEqual(ac._repo_download_version(), ac._VERSION)

    def test_stale_private_download_is_swept_but_active_download_survives(self):
        from nyaatriggers import app_common as ac
        stale = self.root / "nyaatriggers-download-stale"
        active = self.root / "nyaatriggers-download-active"
        for directory in (stale, active):
            directory.mkdir()
            part = directory / (updater.LINUX_ASSET + ".12.34.part")
            part.write_bytes(b"partial archive")
            os.utime(directory, (100, 100))
            if directory == stale:
                os.utime(part, (100, 100))
        ac._sweep_stale_update_parts(self.root)
        self.assertFalse(stale.exists())
        self.assertTrue(active.exists())

    def test_git_update_preserves_local_timeline_content(self):
        origin = self.root / "origin"
        clone = self.root / "clone"
        def git(folder, *args):
            return subprocess.run(["git", "-C", str(folder), "-c", "user.name=Test",
                                   "-c", "user.email=test@example.invalid", *args],
                                  check=True, capture_output=True, text=True, timeout=5)
        origin.mkdir()
        git(origin, "init", "-q", "-b", "main")
        git(origin, "commit", "--allow-empty", "-qm", "initial")
        git(self.root, "clone", "-q", str(origin), str(clone))
        name = "timelines/sample.cactbot.txt"
        for folder, content in ((origin, "upstream timeline"), (clone, "my edited timeline")):
            (folder / "timelines").mkdir()
            (folder / name).write_text(content)
        git(origin, "add", name)
        git(origin, "commit", "-qm", "add timeline")
        ok, message = updater.apply_git(clone)
        self.assertTrue(ok, message)
        self.assertEqual((clone / name).read_text(), "upstream timeline")
        copies = list(clone.rglob("sample.cactbot.txt"))
        self.assertTrue(any(path.read_text() == "my edited timeline" for path in copies),
                        "The automatic conflict cleanup discarded local content")

    def test_rejected_version_reads_both_legacy_source_layouts(self):
        for relative in ("app_common.py", "nyaatriggers/app_common.py"):
            with self.subTest(relative=relative), tempfile.TemporaryDirectory() as directory:
                stage = Path(directory)
                source = stage / relative
                source.parent.mkdir(parents=True, exist_ok=True)
                source.write_text('_VERSION = "9.2.1"\n')
                updater._mark_rejected(self.dest, stage)
                self.assertTrue(updater.is_rejected_update("v9.2.1", self.dest))
                self.assertFalse(updater.is_rejected_update("9.2.2", self.dest))

    def test_genuine_boot_marker_is_accepted(self):
        def launch(*args):
            (self.dest / updater._BOOT_OK_MARKER).write_text("12345")
            return SimpleNamespace(pid=12345, poll=lambda: None)
        with patch.object(updater, "_relaunch_installed", side_effect=launch):
            self.assertTrue(updater._relaunch_and_verify(
                self.dest / "NyaaTriggers", self.dest, grace=.1))

    def test_failed_tasklist_cannot_confirm_process_exit(self):
        with patch.object(updater.subprocess, "run", return_value=SimpleNamespace(
                returncode=1, stdout="INFO: No matching tasks")), \
                patch.object(updater.time, "sleep"):
            self.assertFalse(updater._wait_for_pid_exit(1234, timeout=.02))

    def test_repaired_voice_lives_outside_the_replaceable_bundle(self):
        from nyaatriggers import paths
        bundle = self.dest / "_internal"
        with patch.object(paths, "bundle_root", return_value=bundle), \
                patch.object(paths, "data_root", return_value=self.dest):
            with patch.object(sys, "excepthook"), patch.object(threading, "excepthook"):
                namespace = {"__name__": "bootstrap_fixture", "__file__": str(REPO / "main.py")}
                exec(compile((REPO / "main.py").read_text(), str(REPO / "main.py"), "exec"), namespace)
            self.assertEqual(namespace["_VOICES_DIR"], self.dest / "voices")
            self.assertEqual(paths.default_voice_dir(), self.dest / "voices")
            voice = bundle / "voices"
            voice.mkdir()
            (voice / "en_US-arctic-medium.onnx").write_bytes(b"model")
            self.assertEqual(paths.default_voice_dir(), self.dest / "voices")
            (voice / "en_US-arctic-medium.onnx.json").write_text("{}")
            self.assertEqual(paths.default_voice_dir(), voice)
            repaired = self.dest / "voices"
            shutil.copytree(voice, repaired)
            self.assertEqual(paths.default_voice_dir(), repaired)

    def test_active_swap_blocks_cleanup_and_a_second_update(self):
        archive = self.root / "update.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            for name, data in {"NyaaTriggers/_internal/runtime": b"new",
                               "NyaaTriggers/NyaaTriggers": b"new executable",
                               "NyaaTriggers/NyaaTriggers.sh": (REPO / "NyaaTriggers.sh").read_bytes()}.items():
                entry = tarfile.TarInfo(name)
                entry.size = len(data)
                tar.addfile(entry, io.BytesIO(data))
        ready = self.root / "ready"
        resume = self.root / "resume"
        code = '''
import sys, time
from pathlib import Path
from nyaatriggers import updater
dest, archive, ready, resume = map(Path, sys.argv[1:])
real = updater._safe_extract_tar
def extract(*args):
    real(*args)
    ready.touch()
    while not resume.exists():
        time.sleep(.01)
updater._safe_extract_tar = extract
ok, message = updater.apply_frozen_linux(archive, dest, "NyaaTriggers")
print(message)
sys.exit(0 if ok else 1)
'''
        process = subprocess.Popen([sys.executable, "-c", code, str(self.dest),
                                    str(archive), str(ready), str(resume)],
                                   cwd=REPO, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertTrue(wait_for(ready.exists, timeout=3))
            updater.cleanup_old_backups(self.dest)
            ok, _ = updater.apply_frozen_linux(archive, self.dest, "NyaaTriggers")
            self.assertFalse(ok, "Concurrent updates both entered the swap")
            self.assertEqual((self.dest / "_internal" / "runtime").read_text(), "old")
        finally:
            resume.touch()
            stdout, stderr = process.communicate(timeout=5)
        self.assertEqual(process.returncode, 0, stdout + stderr)
        self.assertEqual((self.dest / "_internal" / "runtime").read_text(), "new")

    def test_manual_check_reports_failure_after_lookup(self):
        from nyaatriggers import updater_ui
        window = SimpleNamespace(
            _settings={}, _upd_available_signal=Mock(), _upd_checkmsg_signal=Mock())
        release = updater.Release("v9.0.0", "9.0.0", "")
        with patch.object(updater_ui.threading, "Thread") as worker, \
                patch.object(updater, "fetch_latest_release", return_value=release), \
                patch.object(updater, "install_kind", return_value="source"), \
                patch.object(updater, "is_update_for_here", side_effect=ValueError("bad version")):
            updater_ui.UpdaterUiMixin._start_update_check(window, manual=True)
            try:
                worker.call_args.kwargs["target"]()
            except ValueError:
                pass
        self.assertTrue(window._upd_checkmsg_signal.emit.called,
                        "Manual check never released its button")

    def check_headers(self, operation, tls=False):
        peer = HttpPeer("headers", tls=tls)
        self.addCleanup(peer.close)
        if tls:
            import ssl
            context = ssl.create_default_context(cafile=peer.certificate)
            self.enterContext(patch.object(ssl, "_create_default_https_context", return_value=context))
            self.enterContext(patch.object(urllib.request, "_opener", None))
        errors = []
        done = threading.Event()

        def run():
            try:
                operation(peer.uri)
            except Exception as exc:
                errors.append(exc)
            finally:
                done.set()

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        try:
            self.assertTrue(peer.started.wait(.5), "The fixture never reached response headers")
            self.assertTrue(done.wait(.8), "The deadline did not cover HTTP headers")
            self.assertTrue(errors, "A failed response was reported as success")
            self.assertTrue(any("timed out" in str(error) or "stalled" in str(error) for error in errors),
                            repr(errors))
            self.assertTrue(wait_for(lambda: not any(
                t.name == "http-response-reader" for t in threading.enumerate()), timeout=.5))
        finally:
            peer.release.set()
            thread.join(3)

    def test_archive_header_deadline(self):
        destination = self.dest / "archive"
        with patch.object(updater, "_READ_STALL_S", .15), \
                patch.object(updater, "_DOWNLOAD_DEADLINE_S", .25):
            self.check_headers(lambda url: updater.download(url, destination, timeout=.5))
        self.assertFalse(destination.exists())
        self.assertEqual(list(self.dest.glob("*.part")), [])

    @unittest.skipUnless(shutil.which("openssl"), "TLS fixture needs openssl")
    def test_archive_https_header_deadline(self):
        with patch.object(updater, "_READ_STALL_S", .15), \
                patch.object(updater, "_DOWNLOAD_DEADLINE_S", .25):
            self.check_headers(lambda url: updater.download(
                url, self.dest / "archive", timeout=.5), tls=True)

    def test_release_header_deadline(self):
        with patch.object(updater, "_READ_STALL_S", .15), \
                patch.object(updater, "_RELEASE_DEADLINE_S", .25):
            def fetch(url):
                with patch.object(updater, "API_LATEST_URL", url):
                    updater.fetch_latest_release(timeout=.5)
            self.check_headers(fetch)

    def test_slow_body_with_continuous_progress_completes(self):
        peer = HttpPeer("headers")
        self.addCleanup(peer.close)
        body = b"slow but complete"
        def send(handler):
            handler.send_response(200)
            handler.send_header("Content-Length", str(len(body)))
            handler.end_headers()
            try:
                for byte in body:
                    handler.wfile.write(bytes([byte]))
                    handler.wfile.flush()
                    time.sleep(.025)
            except OSError:
                pass
        peer.server.RequestHandlerClass.do_GET = send
        destination = self.dest / "slow-archive"
        with patch.object(updater, "_READ_STALL_S", .15), \
                patch.object(updater, "_DOWNLOAD_DEADLINE_S", 2):
            updater.download(peer.uri, destination, timeout=1)
        self.assertEqual(destination.read_bytes(), body)

    def test_installer_header_deadline(self):
        with patch.object(install, "VOICES_DIR", self.dest), \
                patch.object(install, "VOICE_FILE", self.dest / "en_US-arctic-medium.onnx"), \
                patch.object(install, "_READ_STALL_S", .15), \
                patch.object(install, "_DOWNLOAD_DEADLINE_S", .25):
            def download(url):
                with patch.object(install, "VOICE_BASE", url):
                    install.download_voice()
            self.check_headers(download)

    def test_checksum_response_has_a_total_deadline(self):
        archive = self.dest / "archive"
        archive.write_bytes(b"archive")
        def verify(url):
            release = updater.Release("v9.0.0", "9.0.0", "", assets={"archive.sha256": url})
            ok, message = updater.verify_release_asset(release, "archive", archive, timeout=.2)
            if not ok:
                raise OSError(message)
        self.check_headers(verify)

    def test_installer_rejects_bad_model_before_publishing_it(self):
        peer = HttpPeer("headers")
        self.addCleanup(peer.close)
        def send(handler):
            handler.send_response(200)
            handler.send_header("Content-Length", "3")
            handler.end_headers()
            handler.wfile.write(b"bad")
        peer.server.RequestHandlerClass.do_GET = send
        destination = self.dest / "en_US-arctic-medium.onnx"
        published = []
        replace = os.replace
        def record(src, dst):
            if Path(dst) == destination:
                published.append(Path(src).read_bytes())
            replace(src, dst)
        with patch.object(install, "VOICES_DIR", self.dest), \
                patch.object(install, "VOICE_FILE", destination), \
                patch.object(install, "VOICE_BASE", peer.uri), \
                patch.object(install.os, "replace", side_effect=record):
            with self.assertRaises(SystemExit):
                install.download_voice()
        self.assertEqual(published, [], "A rejected model reached the live voice path")
        self.assertFalse(destination.exists())
        self.assertEqual(list(self.dest.glob("*.part")), [])


class LinuxInterruptedUpdateTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "linux", "Linux launcher")
    def test_launcher_recovers_each_interrupted_swap(self):
        for boundary in ("runtime_backup", "runtime_install", "executable_install"):
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                dest = root / "installed program"
                (dest / "_internal").mkdir(parents=True)
                (dest / "_internal" / "runtime").write_text("old")
                script = '#!/bin/sh\nprintf "old "\ncat "$(dirname "$0")/_internal/runtime"\n'
                (dest / "NyaaTriggers").write_text(script)
                (dest / "NyaaTriggers").chmod(0o755)
                shutil.copy2(REPO / "NyaaTriggers.sh", dest / "NyaaTriggers.sh")
                archive = root / "update.tar.gz"
                with tarfile.open(archive, "w:gz") as tar:
                    for name, data in {
                        "NyaaTriggers/_internal/runtime": b"new",
                        "NyaaTriggers/NyaaTriggers": script.replace("old ", "new ").encode(),
                        "NyaaTriggers/NyaaTriggers.sh": (REPO / "NyaaTriggers.sh").read_bytes(),
                    }.items():
                        entry = tarfile.TarInfo(name)
                        entry.size = len(data)
                        tar.addfile(entry, io.BytesIO(data))
                code = '''
import os, sys
from pathlib import Path
from nyaatriggers import updater
dest, archive, boundary = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
real = os.replace
def replace(src, dst):
    real(src, dst)
    src, dst = Path(src), Path(dst)
    hit = ((boundary == "runtime_backup" and src == dest / "_internal")
           or (boundary == "runtime_install" and dst == dest / "_internal")
           or (boundary == "executable_install" and dst == dest / "NyaaTriggers"))
    if hit:
        os._exit(90)
updater.os.replace = replace
updater.apply_frozen_linux(archive, dest, "NyaaTriggers")
'''
                result = subprocess.run([sys.executable, "-c", code, str(dest),
                                         str(archive), boundary], cwd=REPO, timeout=10)
                self.assertEqual(result.returncode, 90)
                launched = subprocess.run(["sh", str(dest / "NyaaTriggers.sh")],
                                          cwd=root, capture_output=True, text=True, timeout=10)
                self.assertEqual(launched.returncode, 0, launched.stderr)
                self.assertEqual(launched.stdout, "old old",
                                 "Recovery did not restore a matching executable and runtime")


if __name__ == "__main__":
    unittest.main(verbosity=2)
