import argparse
from contextlib import contextmanager
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile


MODULES = ":actimport,:xivsupport,:trigger-support,:triggers-general,:triggers-ew,:triggers-sb,:triggers-dt,:titan-jails,:easytriggers,:timelines,:telesto-core"
MAVEN_ARGS = ["-q", "-Dmaven.test.skip=true", "-pl", MODULES, "-am", "clean", "install"]


def git(source, *args, check=True):
    return subprocess.run(["git", *args], cwd=source, check=check,
                          text=True, encoding="utf-8", stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def install_modules(source):
    maven = shutil.which("mvn")
    if maven is None:
        raise RuntimeError("Maven was not found")
    if sys.platform == "win32" and Path(maven).suffix.lower() in (".bat", ".cmd"):
        environment = os.environ.copy()
        environment["NYAA_BUILD_MAVEN"] = maven
        # Quote the environment expansion so Maven paths remain literal.
        command = '""%NYAA_BUILD_MAVEN%" ' + " ".join(MAVEN_ARGS) + '"'
        interpreter = os.environ.get("COMSPEC", "cmd.exe")
        # cmd's command text needs literal quotes rather than CRT backslash escaping.
        command_line = subprocess.list2cmdline([interpreter, "/d", "/v:off", "/s", "/c"]) + " " + command
        subprocess.run(command_line, executable=interpreter, cwd=source, env=environment, check=True)
    else:
        subprocess.run([maven, *MAVEN_ARGS], cwd=source, check=True)


def remove_tree(path):
    def writable(function, filename, _error):
        os.chmod(filename, stat.S_IWRITE | stat.S_IREAD)
        function(filename)

    if path.exists():
        shutil.rmtree(path, onerror=writable)


def remove_worktree(source, worktree, common):
    git(source, "worktree", "remove", "--force", str(worktree), check=False)
    administrative = common / "worktrees"
    if administrative.is_dir():
        for entry in administrative.iterdir():
            try:
                destination = Path((entry / "gitdir").read_text(encoding="utf-8").strip())
            except (OSError, UnicodeError):
                continue
            if destination.resolve() == (worktree / ".git").resolve():
                # Git can leave this record after an interrupted checkout or removal.
                remove_tree(entry)


@contextmanager
def prepared_source(source, patch):
    source, patch = Path(source).resolve(), Path(patch).resolve()
    if not patch.is_file():
        raise RuntimeError(f"Patch file was not found: {patch}")
    root = Path(git(source, "rev-parse", "--show-toplevel").stdout.strip()).resolve()
    if root != source:
        raise RuntimeError("The engine source path must be its Git working tree root")
    if git(source, "status", "--porcelain", "--untracked-files=all").stdout:
        raise RuntimeError("The engine source contains local changes. Commit or move them before building")
    if git(source, "apply", "--check", "--", str(patch), check=False).returncode:
        if git(source, "apply", "--reverse", "--check", "--", str(patch), check=False).returncode:
            raise RuntimeError("The engine patch does not apply and the source does not already contain it")
        print(">> engine source already contains the patch", flush=True)
        yield source
        return
    head = git(source, "rev-parse", "HEAD").stdout.strip()
    common = Path(git(source, "rev-parse", "--git-common-dir").stdout.strip())
    if not common.is_absolute():
        common = source / common
    common = common.resolve()
    with tempfile.TemporaryDirectory(prefix="nyaa-engine-build-") as temporary:
        worktree = Path(temporary) / "source"
        try:
            git(source, "worktree", "add", "--detach", str(worktree), head)
            git(worktree, "apply", "--", str(patch))
            yield worktree
        finally:
            remove_worktree(source, worktree, common)


def build(source, patch):
    with prepared_source(source, patch) as prepared:
        if prepared != Path(source).resolve():
            print(">> installing patched Triggevent Engine modules", flush=True)
        install_modules(prepared)


def main():
    parser = argparse.ArgumentParser(description="Install engine modules without modifying the source clone")
    parser.add_argument("source", help="Clean engine source clone")
    parser.add_argument("patch", help="Engine compatibility patch")
    args = parser.parse_args()
    try:
        build(args.source, args.patch)
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        detail = exc.stderr.strip() if isinstance(exc, subprocess.CalledProcessError) and exc.stderr else str(exc)
        print(f"ERROR: {detail}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
