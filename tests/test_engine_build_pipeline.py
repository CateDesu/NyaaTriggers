import importlib.util
import json
import os
from pathlib import Path
import shlex
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
            maven.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} '
                             f'{shlex.quote(str(observer))} "$@"\n', encoding="utf-8")
            maven.chmod(0o700)
        self.environment["PATH"] = str(executables) + os.pathsep + self.environment.get("PATH", "")
        self.environment["NYAA_BUILD_TEST_CALLS"] = str(self.calls)

    def git(self, *arguments):
        return subprocess.run(["git", "-c", "commit.gpgsign=false", *arguments],
                              cwd=self.source, env=self.environment,
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

    def test_windows_build_output_property_keeps_path_characters_literal(self):
        maven = r"C:\Maven & tools\mvn.cmd"
        output = r"C:\Program & files\50% ready\target"
        with patch.object(engine_build.sys, "platform", "win32"), \
                patch.dict(os.environ, {"COMSPEC": "cmd.exe"}), \
                patch.object(engine_build.shutil, "which", return_value=maven), \
                patch.object(engine_build.subprocess, "run") as run:
            engine_build.run_maven(self.source, ["clean", "package"], {"nyaa.build.directory": output})
        self.assertEqual(run.call_args.args[0],
                         'cmd.exe /d /v:off /s /c ""%NYAA_BUILD_MAVEN%" clean package '
                         '"-Dnyaa.build.directory=%NYAA_BUILD_PROPERTY_0%""')
        self.assertEqual(run.call_args.kwargs["env"]["NYAA_BUILD_PROPERTY_0"], output)

    def test_failed_publication_preserves_the_installed_engine(self):
        target = self.directory / "target"
        target.mkdir()
        jar = target / "triggevent-core.jar"
        jar.write_bytes(b"working engine")

        def package(source, arguments, properties):
            output = Path(properties["nyaa.build.directory"])
            output.mkdir()
            (output / jar.name).write_bytes(b"complete engine")

        with patch.object(engine_build, "run_maven", side_effect=package), \
                patch.object(engine_build.os, "replace", side_effect=PermissionError("locked")):
            with self.assertRaises(PermissionError):
                engine_build.package_wrapper(self.directory, self.before)
        self.assertEqual(jar.read_bytes(), b"working engine")
        self.assertEqual(list(target.iterdir()), [jar])

    def wrapper_build(self, *, fail=False, existing=True):
        wrapper = self.directory / "wrapper & tools"
        wrapper.mkdir()
        for name in ("build.sh", "build_engine.py", "pom.xml"):
            shutil.copy2(ROOT / "triggevent-core" / name, wrapper / name)
        (wrapper / "patches").mkdir()
        shutil.copy2(self.patch, wrapper / "patches/same-zone-history.patch")
        target = wrapper / "target"
        target.mkdir()
        jar = target / "triggevent-core.jar"
        if existing:
            jar.write_bytes(b"working engine")
        binaries = self.directory / "bin & fixtures"
        observer = binaries / "observe.py"
        original = observer.read_text(encoding="utf-8")
        observer.write_text('''import json
import os
from pathlib import Path
import shutil
import sys

source = Path.cwd()
if (source / "pom.xml").is_file():
    arguments = sys.argv[1:]
    output = source / "target"
    for argument in arguments:
        if argument.startswith("-Dnyaa.build.directory="):
            output = Path(argument.split("=", 1)[1])
    shutil.rmtree(output, ignore_errors=True)
    output.mkdir(parents=True)
    (output / "triggevent-core.jar").write_bytes(b"partial engine")
    installed = source / "target/triggevent-core.jar"
    with Path(os.environ["NYAA_BUILD_TEST_CALLS"]).open("a") as log:
        log.write(json.dumps({"arguments": arguments, "output": str(output),
                              "installed": installed.read_text() if installed.exists() else None}) + "\\n")
    if os.environ.get("NYAA_WRAPPER_TEST_FAIL"):
        raise SystemExit(9)
    (output / "triggevent-core.jar").write_bytes(b"complete engine")
    raise SystemExit(0)
''' + original, encoding="utf-8")
        (binaries / "python3").symlink_to(sys.executable)
        java = binaries / "java"
        java.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        java.chmod(0o700)
        environment = self.environment | {"EVENT_TRIGGER_DIR": str(self.source),
                                           "EVENT_TRIGGER_REPO": str(self.source),
                                           "EVENT_TRIGGER_REF": self.before}
        if fail:
            environment["NYAA_WRAPPER_TEST_FAIL"] = "1"
        result = subprocess.run(["bash", str(wrapper / "build.sh")], cwd=self.directory,
                                env=environment, text=True, capture_output=True, timeout=30)
        return result, jar

    @unittest.skipUnless(os.name == "posix" and shutil.which("bash"),
                         "Requires the POSIX build launcher")
    def test_failed_wrapper_package_preserves_the_installed_engine(self):
        result, jar = self.wrapper_build(fail=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.observations()[-1]["installed"], "working engine")
        self.assertEqual(jar.read_bytes(), b"working engine", result.stderr)
        self.assertEqual(list(jar.parent.iterdir()), [jar])

    @unittest.skipUnless(os.name == "posix" and shutil.which("bash"),
                         "Requires the POSIX build launcher")
    def test_wrapper_package_is_published_only_after_success(self):
        result, jar = self.wrapper_build()
        self.assertEqual(result.returncode, 0, result.stderr)
        observed = self.observations()[-1]
        self.assertEqual(observed["installed"], "working engine")
        self.assertEqual(jar.read_bytes(), b"complete engine")
        self.assertIn("-Dnyaa.engine.commit=" + self.before, observed["arguments"])
        self.assertFalse(Path(observed["output"]).exists())
        self.assertEqual(list(jar.parent.iterdir()), [jar])

    @unittest.skipUnless(os.name == "posix" and shutil.which("bash"),
                         "Requires the POSIX build launcher")
    def test_failed_first_wrapper_package_leaves_no_partial_engine(self):
        result, jar = self.wrapper_build(fail=True, existing=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIsNone(self.observations()[-1]["installed"])
        self.assertFalse(jar.exists())
        self.assertEqual(list(jar.parent.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
