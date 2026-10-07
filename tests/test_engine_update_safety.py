from contextlib import ExitStack
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from nyaatriggers import triggevent_bridge as bridge


@unittest.skipUnless(shutil.which("git") and shutil.which("bash"), "Requires Git and Bash")
class EngineUpdateTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.origin = self.root / "origin"
        self.origin.mkdir()
        self.git(self.origin, "init", "-b", "main")
        (self.origin / "engine.txt").write_text("committed source\n")
        self.commit(self.origin)
        self.checkout = self.root / "checkout"
        self.git(self.root, "clone", str(self.origin), str(self.checkout))
        (self.origin / "upstream.txt").write_text("upstream update\n")
        self.commit(self.origin)
        self.build = self.root / "build.sh"
        self.source = self.root / "src/main/java/TriggeventCore.java"
        self.source.parent.mkdir(parents=True)
        self.source.write_text("wrapper before native fixes\n")
        (self.root / "pom.xml").write_text("<project/>\n")
        (self.root / "build.bat").write_text("@echo off\n")
        self.jar = self.root / "target/triggevent-core.jar"
        self.jar.parent.mkdir()
        self.build_command = f"cp {shlex.quote(str(self.source))} {shlex.quote(str(self.jar))}\n"
        self.build.write_text(self.build_command)
        self.stamp = self.jar.with_name(self.jar.name + ".built-from")
        for key, value in {"_CORE_DIR": self.root, "_ET_DIR": self.checkout, "_ET_REPO_URL": str(self.origin),
                           "_BUILD_SCRIPT": self.build, "_JAR_STAMP": self.stamp}.items():
            self.stack.enter_context(patch.object(bridge, key, value))
        which = shutil.which
        self.stack.enter_context(patch.object(bridge.shutil, "which", side_effect=
            lambda name: which(name) or ("unused" if name in ("java", "mvn") else None)))
        self.stack.enter_context(patch.object(bridge, "_download_engine",
                                             side_effect=AssertionError("Unexpected download")))

    def git(self, path, *args):
        return subprocess.run(["git", "-C", str(path), "-c", "user.name=CateDesu",
                               "-c", "user.email=205492565+CateDesu@users.noreply.github.com", "-c", "commit.gpgsign=false",
                               *args], capture_output=True, text=True, check=True).stdout.strip()

    def commit(self, path):
        self.git(path, "add", ".")
        self.git(path, "commit", "-m", "Fixture")

    def test_dirty_checkouts_remain_untouched(self):
        for kind in ("unstaged", "staged", "untracked", "diverged"):
            with self.subTest(kind=kind):
                if kind == "diverged":
                    (self.checkout / "local.txt").write_text("local commit\n")
                    self.commit(self.checkout)
                file = self.checkout / ("new.txt" if kind == "untracked" else "engine.txt")
                file.write_text("keep my work\n")
                if kind == "staged":
                    self.git(self.checkout, "add", str(file))
                before = self.git(self.checkout, "status", "--porcelain")
                head = self.git(self.checkout, "rev-parse", "HEAD")
                with patch.object(bridge.subprocess, "Popen", wraps=subprocess.Popen) as spawn:
                    changed, message = bridge.update_engine(manual=True)
                self.assertFalse(changed)
                self.assertIn("uncommitted changes", message)
                self.assertEqual(file.read_text(), "keep my work\n")
                self.assertEqual(self.git(self.checkout, "status", "--porcelain"), before)
                self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), head)
                self.assertFalse(any(call.args[0][0] == "bash" for call in spawn.call_args_list))
                self.git(self.checkout, "reset", "--hard", "HEAD")
                if kind == "untracked":
                    file.unlink()

    def test_clean_update_builds_and_stamps_the_new_commit(self):
        changed, _message = bridge.update_engine()
        self.assertTrue(changed)
        self.assertEqual(json.loads(self.stamp.read_text())["engine"], self.git(self.origin, "rev-parse", "HEAD"))
        self.assertEqual(self.git(self.checkout, "status", "--porcelain"), "")

    def test_unpublished_commits_cannot_be_replaced_by_a_source_rebuild(self):
        self.git(self.checkout, "fetch", "origin", "main")
        self.git(self.checkout, "merge", "--ff-only", "origin/main")
        (self.checkout / "engine.txt").write_text("local engine fix\n")
        self.commit(self.checkout)
        local_head = self.git(self.checkout, "rev-parse", "HEAD")
        self.build.write_text(
            f'git -C {shlex.quote(str(self.checkout))} checkout "$EVENT_TRIGGER_REF"\n'
            f"cp {shlex.quote(str(self.checkout / 'engine.txt'))} {shlex.quote(str(self.jar))}\n")

        for manual in (False, True):
            with self.subTest(manual=manual):
                self.git(self.checkout, "checkout", "-B", "main", local_head)
                self.jar.write_text("local engine fix\n")
                self.stamp.write_text("local build stamp\n")
                changed, message = bridge.update_engine(manual=manual)
                actual = (changed, self.git(self.checkout, "rev-parse", "HEAD"),
                          self.jar.read_text(), self.stamp.read_text())
                self.assertEqual(actual, (False, local_head, "local engine fix\n", "local build stamp\n"),
                                 message)
                self.assertIn("unpublished commits", message)

    def test_clean_diverged_history_preserves_source_and_build(self):
        (self.checkout / "engine.txt").write_text("diverged local fix\n")
        self.commit(self.checkout)
        local_head = self.git(self.checkout, "rev-parse", "HEAD")
        self.jar.write_text("local jar\n")
        self.stamp.write_text("local stamp\n")
        for manual in (False, True):
            with self.subTest(manual=manual):
                changed, message = bridge.update_engine(manual=manual)
                self.assertFalse(changed)
                self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), local_head)
                self.assertEqual((self.checkout / "engine.txt").read_text(), "diverged local fix\n")
                self.assertEqual(self.jar.read_text(), "local jar\n")
                self.assertEqual(self.stamp.read_text(), "local stamp\n")

    def test_failed_build_can_retry_the_same_commit(self):
        self.build.write_text("exit 1\n")
        changed, message = bridge.update_engine()
        self.assertFalse(changed)
        self.assertIn("rebuild failed", message)
        self.assertFalse(self.stamp.exists())
        self.build.write_text(self.build_command)
        self.assertTrue(bridge.update_engine()[0])

    def test_failed_status_check_does_not_fetch_or_build(self):
        with patch.object(bridge.subprocess, "run", return_value=
                          subprocess.CompletedProcess([], 1, "", "status failed")) as run:
            changed, message = bridge.update_engine()
        self.assertFalse(changed)
        self.assertIn("could not check local changes", message)
        self.assertEqual(run.call_count, 1)

    def test_wrapper_edits_rebuild_without_an_engine_update(self):
        self.assertTrue(bridge.update_engine()[0])
        for manual in (False, True):
            with self.subTest(manual=manual):
                self.source.write_text(f"updated wrapper {manual}\n")
                changed, message = bridge.update_engine(manual=manual)
                self.assertTrue(changed, message)
                self.assertEqual(self.jar.read_bytes(), self.source.read_bytes())
                self.assertIn("rebuilt", message)

    def test_unchanged_wrapper_skips_another_build(self):
        self.assertTrue(bridge.update_engine()[0])
        modified = self.jar.stat().st_mtime_ns
        changed, message = bridge.update_engine()
        self.assertFalse(changed)
        self.assertIn("already up to date", message)
        self.assertEqual(self.jar.stat().st_mtime_ns, modified)

    def test_same_size_source_edits_still_rebuild(self):
        self.assertTrue(bridge.update_engine()[0])
        before = self.source.stat()
        self.source.write_bytes(self.source.read_bytes().upper())
        os.utime(self.source, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000_000))
        self.assertEqual(self.source.stat().st_size, before.st_size)
        self.assertTrue(bridge.update_engine()[0])
        self.assertEqual(self.jar.read_bytes(), self.source.read_bytes())

    def test_build_inputs_and_removed_resources_trigger_rebuilds(self):
        self.assertTrue(bridge.update_engine()[0])
        resource = self.root / "src/main/resources/new.xml"
        resource.parent.mkdir()
        patch_file = self.root / "patches" / "same-zone-history.patch"
        patch_file.parent.mkdir()
        for path in (self.root / "pom.xml", self.root / "build.bat",
                     self.root / "build_engine.py", patch_file, resource):
            with self.subTest(path=path.name):
                path.write_text("changed input\n")
                self.assertTrue(bridge.update_engine()[0])
                self.assertFalse(bridge.update_engine()[0])
        resource.unlink()
        self.assertTrue(bridge.update_engine()[0])
        (self.root / "README.md").write_text("Documentation change\n")
        self.assertFalse(bridge.update_engine()[0])

    def test_legacy_stamp_rebuilds_once(self):
        self.assertTrue(bridge.update_engine()[0])
        self.stamp.write_text(self.git(self.origin, "rev-parse", "HEAD") + "\n")
        self.source.write_text("new wrapper with an old stamp\n")
        self.assertTrue(bridge.update_engine()[0])
        self.assertEqual(self.jar.read_bytes(), self.source.read_bytes())
        self.assertFalse(bridge.update_engine()[0])

    def test_missing_jar_rebuilds_even_with_a_current_stamp(self):
        self.assertTrue(bridge.update_engine()[0])
        self.jar.unlink()
        self.assertTrue(bridge.update_engine()[0])
        self.assertEqual(self.jar.read_bytes(), self.source.read_bytes())

    def test_failed_missing_jar_rebuild_does_not_validate_partial_output(self):
        marker = self.root / "fail-build"
        self.build.write_text(
            f"if [ -f {shlex.quote(str(marker))} ]; then "
            f"echo broken > {shlex.quote(str(self.jar))}; exit 1; fi\n" + self.build_command)
        self.assertTrue(bridge.update_engine()[0])
        self.jar.unlink()
        marker.touch()
        changed, message = bridge.update_engine()
        self.assertFalse(changed)
        self.assertIn("rebuild failed", message)
        self.assertEqual(self.jar.read_text(), "broken\n")
        marker.unlink()
        self.assertTrue(bridge.update_engine()[0])
        self.assertEqual(self.jar.read_bytes(), self.source.read_bytes())

    def test_missing_build_input_preserves_the_existing_jar(self):
        self.assertTrue(bridge.update_engine()[0])
        previous = self.jar.read_bytes()
        (self.root / "pom.xml").unlink()
        changed, message = bridge.update_engine()
        self.assertFalse(changed)
        self.assertIn("could not prepare the build", message)
        self.assertEqual(self.jar.read_bytes(), previous)

    def test_failed_wrapper_build_keeps_the_change_retryable(self):
        self.assertTrue(bridge.update_engine()[0])
        self.source.write_text("wrapper awaiting a successful build\n")
        self.build.write_text("exit 1\n")
        changed, message = bridge.update_engine()
        self.assertFalse(changed)
        self.assertIn("rebuild failed", message)
        self.build.write_text(self.build_command)
        self.assertTrue(bridge.update_engine()[0])
        self.assertEqual(self.jar.read_bytes(), self.source.read_bytes())

    def test_edits_during_a_build_are_not_marked_current(self):
        marker = shlex.quote(str(self.root / "edited-once"))
        source = shlex.quote(str(self.source))
        self.build.write_text(self.build_command +
            f"if [ ! -f {marker} ]; then touch {marker}; echo changed >> {source}; fi\n")
        self.assertTrue(bridge.update_engine()[0])
        self.assertNotEqual(self.jar.read_bytes(), self.source.read_bytes())
        self.assertTrue(bridge.update_engine()[0])
        self.assertEqual(self.jar.read_bytes(), self.source.read_bytes())
        self.assertFalse(bridge.update_engine()[0])


if __name__ == "__main__":
    unittest.main()
