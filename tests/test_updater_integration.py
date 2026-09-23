"""Exercise source updates with real Git repositories and offline pip installs."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import venv
import zipfile

from nyaatriggers import updater


REPO = Path(__file__).resolve().parent.parent


class GitRepoCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="nyaa-update-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.origin = self.root / "origin"
        self.clone = self.root / "installed program"
        self.origin.mkdir()
        self.git(self.origin, "init", "-q", "-b", "main")
        (self.origin / "program.txt").write_text("base", encoding="utf-8")
        self.commit("base")
        self.base = self.git(self.origin, "rev-parse", "HEAD")
        (self.origin / "program.txt").write_text("earlier push", encoding="utf-8")
        self.commit("earlier push")
        self.old = self.git(self.origin, "rev-parse", "HEAD")
        self.git(self.root, "clone", "-q", str(self.origin), str(self.clone))
        self.saved = self.clone / "settings.json"
        self.saved.write_text('{"user": "keep me"}', encoding="utf-8")

    def git(self, folder, *args):
        result = subprocess.run(
            ["git", "-C", str(folder), "-c", "user.name=Test",
             "-c", "user.email=test@example.invalid", *args],
            capture_output=True, text=True, check=True, timeout=15)
        return result.stdout.strip()

    def commit(self, message):
        self.git(self.origin, "add", ".")
        self.git(self.origin, "commit", "-qm", message)

    def rewrite(self):
        self.git(self.origin, "reset", "--soft", self.base)
        (self.origin / "program.txt").write_text("combined push", encoding="utf-8")
        self.commit("combined push")
        return self.git(self.origin, "rev-parse", "HEAD")

    def assert_saved_data(self):
        self.assertEqual(self.saved.read_text(encoding="utf-8"), '{"user": "keep me"}')


class GitUpdateTests(GitRepoCase):
    def test_combined_push_recovers_clean_published_checkout(self):
        target = self.rewrite()
        ok, message = updater.apply_git(self.clone)
        self.assertTrue(ok, message)
        self.assertEqual(self.git(self.clone, "rev-parse", "HEAD"), target)
        backups = self.git(self.clone, "for-each-ref", "--format=%(objectname)",
                           "refs/nyaa-update-backups/").splitlines()
        self.assertIn(self.old, backups)
        self.assertIn("refs/nyaa-update-backups/", message)
        self.assert_saved_data()

    def test_combined_push_can_retry_after_an_earlier_failed_pull(self):
        target = self.rewrite()
        self.assertNotEqual(updater._git_pull(self.clone).returncode, 0)
        ok, message = updater.apply_git(self.clone)
        self.assertTrue(ok, message)
        self.assertEqual(self.git(self.clone, "rev-parse", "HEAD"), target)
        self.assert_saved_data()

    def test_combined_push_keeps_local_commits(self):
        (self.clone / "program.txt").write_text("local work", encoding="utf-8")
        self.git(self.clone, "commit", "-qam", "local work")
        local = self.git(self.clone, "rev-parse", "HEAD")
        self.rewrite()
        ok, _ = updater.apply_git(self.clone)
        self.assertFalse(ok)
        self.assertEqual(self.git(self.clone, "rev-parse", "HEAD"), local)
        self.assertEqual((self.clone / "program.txt").read_text(), "local work")
        self.assert_saved_data()

    def test_combined_push_keeps_tracked_edits_and_staged_changes(self):
        self.rewrite()
        for staged in (False, True):
            with self.subTest(staged=staged):
                (self.clone / "program.txt").write_text("local edit", encoding="utf-8")
                if staged:
                    self.git(self.clone, "add", "program.txt")
                ok, _ = updater.apply_git(self.clone)
                self.assertFalse(ok)
                self.assertEqual(self.git(self.clone, "rev-parse", "HEAD"), self.old)
                self.assertEqual((self.clone / "program.txt").read_text(), "local edit")
                self.assert_saved_data()

    def test_combined_push_keeps_conflicting_untracked_files(self):
        self.rewrite()
        (self.origin / "settings.json").write_text("incoming file", encoding="utf-8")
        self.commit("add conflicting path")
        ok, _ = updater.apply_git(self.clone)
        self.assertFalse(ok)
        self.assertEqual(self.git(self.clone, "rev-parse", "HEAD"), self.old)
        self.assert_saved_data()

    def test_combined_push_requires_recorded_upstream_history(self):
        self.rewrite()
        self.assertNotEqual(updater._git_pull(self.clone).returncode, 0)
        self.git(self.clone, "reflog", "expire", "--expire=all", "refs/remotes/origin/main")
        ok, _ = updater.apply_git(self.clone)
        self.assertFalse(ok)
        self.assertEqual(self.git(self.clone, "rev-parse", "HEAD"), self.old)
        self.assert_saved_data()

    def test_another_update_holds_the_checkout_lock(self):
        (self.origin / "program.txt").write_text("new code", encoding="utf-8")
        self.commit("next update")
        with updater._update_lock(self.clone):
            ok, message = updater.apply_git(self.clone)
        self.assertFalse(ok)
        self.assertIn("lock", message)
        self.assertEqual(self.git(self.clone, "rev-parse", "HEAD"), self.old)

    def test_managed_python_pulls_and_checks_real_installed_requirements(self):
        (self.origin / "requirements.txt").write_bytes((REPO / "requirements.txt").read_bytes())
        self.commit("require current dependencies")
        with patch.object(updater, "_externally_managed_python", return_value=True):
            ok, message = updater.apply_git(self.clone)
        self.assertTrue(ok, message)
        self.assertIn("dependencies are up to date", message)
        self.assert_saved_data()


class PipUpdateTests(GitRepoCase):
    @classmethod
    def setUpClass(cls):
        cls.environment = tempfile.TemporaryDirectory(prefix="nyaa-update-venv-")
        cls.addClassCleanup(cls.environment.cleanup)
        cls.venv_root = Path(cls.environment.name)
        venv.EnvBuilder(with_pip=True).create(cls.venv_root)
        cls.python = cls.venv_root / ("Scripts/python.exe" if os.name == "nt" else "bin/python")

    def setUp(self):
        super().setUp()
        self.wheels = self.root / "wheels"
        self.wheels.mkdir()

    def wheel(self, version):
        name = "nyaa_update_probe"
        info = f"{name}-{version}.dist-info"
        files = {
            f"{name}.py": f"VERSION = {version!r}\n",
            f"{info}/METADATA": f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n",
            f"{info}/WHEEL": "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        }
        files[f"{info}/RECORD"] = "".join(f"{path},,\n" for path in [*files, f"{info}/RECORD"])
        with zipfile.ZipFile(self.wheels / f"{name}-{version}-py3-none-any.whl", "w") as wheel:
            for name, data in files.items():
                wheel.writestr(name, data)

    def run_update(self):
        result = subprocess.run(
            [str(self.python), "-c",
             "import json, sys; from pathlib import Path; from nyaatriggers import updater; "
             "print(json.dumps(updater.apply_git(Path(sys.argv[1]))))", str(self.clone)],
            cwd=REPO, env={**os.environ, "PYTHONPATH": str(REPO),
                           "PIP_NO_INDEX": "1", "PIP_FIND_LINKS": str(self.wheels),
                           "PIP_CONFIG_FILE": os.devnull},
            capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_real_pip_failure_retries_with_unchanged_git_head(self):
        version = "2.0"
        (self.origin / "requirements.txt").write_text(f"nyaa-update-probe=={version}\n")
        self.commit("require new package")
        ok, message = self.run_update()
        self.assertFalse(ok, message)
        self.assertIn("dependencies failed", message)
        head = self.git(self.clone, "rev-parse", "HEAD")
        self.wheel(version)
        ok, message = self.run_update()
        self.assertTrue(ok, message)
        self.assertEqual(self.git(self.clone, "rev-parse", "HEAD"), head)
        restarted = subprocess.run(
            [str(self.python), "-c", "import nyaa_update_probe; print(nyaa_update_probe.VERSION)"],
            check=True, capture_output=True, text=True, timeout=15)
        self.assertEqual(restarted.stdout.strip(), version)
        self.assert_saved_data()


if __name__ == "__main__":
    unittest.main()
