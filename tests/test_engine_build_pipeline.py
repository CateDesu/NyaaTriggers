import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "triggevent-core" / "build_engine.py"
SPEC = importlib.util.spec_from_file_location("engine_build", HELPER)
engine_build = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(engine_build)

MODULES = ":actimport,:xivsupport,:trigger-support,:triggers-general,:triggers-ew,:triggers-sb,:triggers-dt,:titan-jails,:easytriggers,:timelines,:telesto-core"


@unittest.skipUnless(shutil.which("git"), "Needs Git for isolated build fixtures")
class EngineBuildPipelineTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.source = self.directory / "source & clone"
        self.source.mkdir()
        self.environment = os.environ.copy()
        self.environment.update(GIT_AUTHOR_NAME="CateDesu", GIT_COMMITTER_NAME="CateDesu",
                                GIT_AUTHOR_EMAIL="205492565+CateDesu@users.noreply.github.com",
                                GIT_COMMITTER_EMAIL="205492565+CateDesu@users.noreply.github.com")
        self.git("init", "-q", "-b", "main")
        (self.source / "mechanic.txt").write_text("old\n", encoding="utf-8")
        (self.source / ".gitignore").write_text("target/\n", encoding="utf-8")
        self.git("add", "mechanic.txt", ".gitignore")
        self.git("commit", "-q", "-m", "Fixture")
        self.patch = self.directory / "compatibility & patch.patch"
        self.patch.write_text("--- a/mechanic.txt\n+++ b/mechanic.txt\n@@ -1 +1 @@\n-old\n+new\n",
                              encoding="utf-8")
        self.before = self.git("rev-parse", "HEAD").stdout.strip()
        self.calls = self.directory / "calls.jsonl"
        executables = self.directory / "bin & fixtures"
        executables.mkdir()
        observer = executables / "observe.py"
        observer.write_text('''import json
import os
from pathlib import Path
import subprocess
import sys

source = Path.cwd()
branch = subprocess.run(["git", "symbolic-ref", "-q", "HEAD"], text=True, capture_output=True)
entry = {"source": str(source), "content": (source / "mechanic.txt").read_text(),
         "arguments": sys.argv[1:], "detached": branch.returncode != 0}
with open(os.environ["NYAA_BUILD_TEST_CALLS"], "a", encoding="utf-8") as output:
    output.write(json.dumps(entry) + "\\n")
(source / "target").mkdir(exist_ok=True)
(source / "target" / "generated").write_text("build product")
raise SystemExit(int(os.environ.get("NYAA_BUILD_TEST_FAIL", "0")))
''', encoding="utf-8")
        if os.name == "nt":
            maven = executables / "mvn.cmd"
            maven.write_text(f'@echo off\n"{sys.executable}" "%~dp0observe.py" %*\n', encoding="utf-8")
        else:
            maven = executables / "mvn"
            maven.write_text(f'#!{sys.executable}\n' + observer.read_text(encoding="utf-8"), encoding="utf-8")
            maven.chmod(0o700)
        self.environment["PATH"] = str(executables) + os.pathsep + self.environment.get("PATH", "")
        self.environment["NYAA_BUILD_TEST_CALLS"] = str(self.calls)

    def git(self, *arguments):
        return subprocess.run(["git", *arguments], cwd=self.source, env=self.environment,
                              text=True, encoding="utf-8", capture_output=True, check=True)

    def run_build(self):
        return subprocess.run([sys.executable, str(HELPER), str(self.source), str(self.patch)],
                              cwd=self.directory, env=self.environment,
                              text=True, encoding="utf-8", capture_output=True)

    def observations(self):
        return [json.loads(line) for line in self.calls.read_text(encoding="utf-8").splitlines()]

    def assert_clone_preserved(self, content="old\n", head=None):
        self.assertEqual((self.source / "mechanic.txt").read_text(encoding="utf-8"), content)
        self.assertEqual(self.git("rev-parse", "HEAD").stdout.strip(), head or self.before)
        self.assertEqual(self.git("status", "--porcelain").stdout, "")
        self.assertEqual(self.git("symbolic-ref", "--short", "HEAD").stdout.strip(), "main")
        self.assertEqual(self.git("worktree", "list", "--porcelain").stdout.count("worktree "), 1)
        administrative = self.source / ".git" / "worktrees"
        self.assertFalse(administrative.exists() and any(administrative.iterdir()))

    def test_patch_build_uses_a_private_detached_worktree_and_preserves_clone(self):
        result = self.run_build()
        self.assertEqual(result.returncode, 0, result.stderr)
        call, = self.observations()
        self.assertEqual(call["content"], "new\n")
        self.assertTrue(call["detached"])
        self.assertNotEqual(Path(call["source"]), self.source)
        self.assertFalse(Path(call["source"]).exists())
        self.assertEqual(call["arguments"], ["-q", "-Dmaven.test.skip=true", "-pl", MODULES,
                                             "-am", "clean", "install"])
        self.assert_clone_preserved()

    def test_maven_failure_removes_patched_worktree_and_administrative_record(self):
        self.environment["NYAA_BUILD_TEST_FAIL"] = "9"
        result = self.run_build()
        self.assertEqual(result.returncode, 1)
        call, = self.observations()
        self.assertEqual(call["content"], "new\n")
        self.assertFalse(Path(call["source"]).exists())
        self.assert_clone_preserved()

    def test_consumer_failure_removes_prepared_source_and_administrative_record(self):
        prepared = None
        with self.assertRaisesRegex(RuntimeError, "verification failed"):
            with engine_build.prepared_source(self.source, self.patch) as prepared:
                self.assertEqual((prepared / "mechanic.txt").read_text(encoding="utf-8"), "new\n")
                self.assertNotEqual(prepared, self.source)
                branch = subprocess.run(["git", "symbolic-ref", "-q", "HEAD"], cwd=prepared,
                                        text=True, capture_output=True)
                self.assertNotEqual(branch.returncode, 0)
                (prepared / "target").mkdir()
                (prepared / "target" / "generated").write_text("test report", encoding="utf-8")
                raise RuntimeError("verification failed")
        self.assertIsNotNone(prepared)
        self.assertFalse(prepared.exists())
        self.assertFalse(self.calls.exists())
        self.assert_clone_preserved()

    def test_already_fixed_source_builds_directly_without_a_worktree(self):
        (self.source / "mechanic.txt").write_text("new\n", encoding="utf-8")
        self.git("add", "mechanic.txt")
        self.git("commit", "-q", "-m", "Fixed fixture")
        head = self.git("rev-parse", "HEAD").stdout.strip()
        result = self.run_build()
        self.assertEqual(result.returncode, 0, result.stderr)
        call, = self.observations()
        self.assertEqual(Path(call["source"]), self.source)
        self.assertFalse(call["detached"])
        self.assertEqual(call["content"], "new\n")
        self.assert_clone_preserved("new\n", head)

    def test_tracked_and_untracked_local_changes_are_refused_without_writes(self):
        for name, content in (("mechanic.txt", "local edit\n"), ("untracked.txt", "local file\n")):
            with self.subTest(name=name):
                path = self.source / name
                original = path.read_bytes() if path.exists() else None
                path.write_text(content, encoding="utf-8")
                before = self.git("status", "--porcelain").stdout
                result = self.run_build()
                self.assertEqual(result.returncode, 1)
                self.assertIn("local changes", result.stderr)
                self.assertFalse(self.calls.exists())
                self.assertEqual(path.read_text(encoding="utf-8"), content)
                self.assertEqual(self.git("status", "--porcelain").stdout, before)
                if original is None:
                    path.unlink()
                else:
                    path.write_bytes(original)
        self.assert_clone_preserved()

    def test_incompatible_patch_refuses_the_build_without_changing_clone(self):
        self.patch.write_text("--- a/mechanic.txt\n+++ b/mechanic.txt\n@@ -1 +1 @@\n-other\n+fixed\n",
                              encoding="utf-8")
        result = self.run_build()
        self.assertEqual(result.returncode, 1)
        self.assertIn("does not apply", result.stderr)
        self.assertFalse(self.calls.exists())
        self.assert_clone_preserved()

    def test_removal_fallback_preserves_an_unrelated_stale_worktree_record(self):
        unrelated = self.directory / "unrelated"
        self.git("worktree", "add", "--detach", str(unrelated), "HEAD")
        shutil.rmtree(unrelated)
        before = self.git("worktree", "list", "--porcelain").stdout
        original = engine_build.git

        def interrupted(source, *arguments, **kwargs):
            if arguments[:2] == ("worktree", "remove"):
                return subprocess.CompletedProcess(arguments, 1, "", "interrupted removal")
            return original(source, *arguments, **kwargs)

        with patch.dict(os.environ, self.environment), patch.object(engine_build, "git", side_effect=interrupted):
            engine_build.build(self.source, self.patch)
        call, = self.observations()
        self.assertFalse(Path(call["source"]).exists())
        self.assertEqual(self.git("worktree", "list", "--porcelain").stdout, before)
        self.assertEqual((self.source / "mechanic.txt").read_text(encoding="utf-8"), "old\n")
        self.assertEqual(self.git("status", "--porcelain").stdout, "")

    def test_windows_maven_batch_path_is_passed_as_a_quoted_environment_value(self):
        maven = r"C:\Maven & tools\mvn.cmd"
        interpreter = r"C:\Windows folder\System32\cmd.exe"
        with patch.object(engine_build.sys, "platform", "win32"), \
                patch.dict(os.environ, {"COMSPEC": interpreter}), \
                patch.object(engine_build.shutil, "which", return_value=maven), \
                patch.object(engine_build.subprocess, "run") as run:
            engine_build.install_modules(self.source)
        command = run.call_args.args[0]
        self.assertEqual(command, '"' + interpreter + '" /d /v:off /s /c ""%NYAA_BUILD_MAVEN%" '
                         '-q -Dmaven.test.skip=true -pl ' + MODULES + ' -am clean install"')
        self.assertNotIn(maven, command)
        self.assertNotIn('\\"', command)
        self.assertEqual(run.call_args.kwargs["executable"], interpreter)
        self.assertEqual(run.call_args.kwargs["env"]["NYAA_BUILD_MAVEN"], maven)
        self.assertEqual(run.call_args.kwargs["cwd"], self.source)


if __name__ == "__main__":
    unittest.main()
