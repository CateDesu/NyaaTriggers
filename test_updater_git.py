"""Git-install update refreshes pip requirements after a pull.

A git checkout updates with `git pull --ff-only --tags`, which brings new code but
never the new or re-pinned dependencies that code needs (the plugin link's
websockets only entered requirements.txt after many checkouts existed, and
those envs reported the overlay link broken through every later update).
After any pull that moves HEAD, apply_git runs
`pip install -r requirements.txt` with the running interpreter. A pip failure
never fails the update itself - the message asks for a manual install.

Run directly:  python3 test_updater_git.py   (exit 0 = all pass)
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import updater

FAILS = []


def check(name, cond):
    print(("PASS  " if cond else "FAIL  ") + name)
    if not cond:
        FAILS.append(name)


class _R:
    def __init__(self, rc, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


def run_case(pull_rc=0, head_moves=True, pip_rc=0, pip_err="", with_req=True):
    """Run apply_git against a temp checkout with updater.subprocess.run
    stubbed: rev-parse answers old->new (or old->old), pull and pip return
    the scripted results. Returns (ok, msg, calls, tmpdir_handle)."""
    tmp = tempfile.TemporaryDirectory()
    repo = Path(tmp.name)
    if with_req:
        (repo / "requirements.txt").write_text("websockets==16.1.1\n",
                                               encoding="utf-8")
    calls = []
    rev_count = [0]

    def fake_run(argv, **kw):
        calls.append(list(argv))
        if "rev-parse" in argv:
            rev_count[0] += 1
            head = "old" if rev_count[0] == 1 else ("new" if head_moves else "old")
            return _R(0, head + "\n")
        if "pull" in argv:
            return _R(pull_rc,
                      "Updating old..new\nFast-forward\n" if pull_rc == 0 else "",
                      "rejected: non-fast-forward" if pull_rc else "")
        if argv[0] == sys.executable and "pip" in argv:
            return _R(pip_rc, "", pip_err)
        raise AssertionError(f"unexpected argv: {argv}")

    orig = updater.subprocess.run
    updater.subprocess.run = fake_run
    try:
        ok, msg = updater.apply_git(repo)
    finally:
        updater.subprocess.run = orig
    pip_calls = [c for c in calls if c[0] == sys.executable and "pip" in c]
    return ok, msg, pip_calls, tmp


# A pull that moves HEAD installs the requirements with the running interpreter.
ok, msg, pip_calls, tmp = run_case()
check("moved HEAD runs pip install -r requirements.txt",
      len(pip_calls) == 1
      and pip_calls[0][1:4] == ["-m", "pip", "install"]
      and "--disable-pip-version-check" in pip_calls[0]
      and "-r" in pip_calls[0]
      and pip_calls[0][-1].endswith("requirements.txt"))
check("successful deps install is reported in the message",
      ok and "dependencies are up to date" in msg)
check("pull output survives in the message", "Fast-forward" in msg)
tmp.cleanup()

# An up-to-date pull never touches pip.
ok, msg, pip_calls, tmp = run_case(head_moves=False)
check("unchanged HEAD skips pip", ok and pip_calls == [])
check("unchanged HEAD message stays the plain pull output", "dependencies" not in msg)
tmp.cleanup()

# A failed pull never touches pip either.
ok, msg, pip_calls, tmp = run_case(pull_rc=128)
check("failed pull reports failure", not ok and "git pull failed" in msg)
check("failed pull skips pip", pip_calls == [])
tmp.cleanup()

# A pip failure does not fail the update; the message asks for a manual run.
ok, msg, pip_calls, tmp = run_case(pip_rc=1, pip_err="ERROR: No matching distribution")
check("pip failure keeps the update ok", ok)
check("pip failure shows pip's error", "No matching distribution" in msg)
check("pip failure points at the manual command",
      "pip install -r requirements.txt" in msg)
tmp.cleanup()

# A checkout without requirements.txt (shouldn't happen, but stay silent).
ok, msg, pip_calls, tmp = run_case(with_req=False)
check("missing requirements.txt skips pip without a word",
      ok and pip_calls == [] and "dependencies" not in msg)
tmp.cleanup()

# The pull asks for tags too, so a maintainer checkout's git describe
# version follows the rolling tags its own pushes are cut from. Plain
# pulls never fetch tags for commits the checkout already has.
seen = []
tmp = tempfile.TemporaryDirectory()

def record_run(argv, **kw):
    seen.append(list(argv))
    return _R(0, "same\n")

orig = updater.subprocess.run
updater.subprocess.run = record_run
try:
    updater.apply_git(Path(tmp.name))
finally:
    updater.subprocess.run = orig
pulls = [c for c in seen if "pull" in c]
check("pull passes --tags", len(pulls) == 1 and "--tags" in pulls[0])
tmp.cleanup()


print()
if FAILS:
    print(f"{len(FAILS)} FAILED: {FAILS}")
    sys.exit(1)
print("all tests passed")
