"""Git update dependency refresh and recovery from old timeline download conflicts."""
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from nyaatriggers import updater

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
    stubbed so pull and pip return the scripted results.
    Returns ok, msg, pip calls and the temporary directory handle."""
    tmp = tempfile.TemporaryDirectory()
    repo = Path(tmp.name)
    if with_req:
        (repo / "requirements.txt").write_text("websockets==16.1.1\n",
                                               encoding="utf-8")
    calls = []
    def fake_run(argv, **kw):
        calls.append(list(argv))
        if "pull" in argv:
            return _R(pull_rc,
                      ("Updating old..new\nFast-forward\n" if head_moves else
                       "Already up to date.\n") if pull_rc == 0 else "",
                      "rejected: non-fast-forward" if pull_rc else "")
        if argv[0] == sys.executable and "pip" in argv:
            return _R(pip_rc, "", pip_err)
        raise AssertionError(f"unexpected argv: {argv}")

    orig = updater.subprocess.run
    updater.subprocess.run = fake_run
    try:
        with patch.object(updater, "_externally_managed_python", return_value=False):
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

# Retrying an update repairs dependencies even when the code is current.
ok, msg, pip_calls, tmp = run_case(head_moves=False)
check("unchanged HEAD still installs requirements", ok and len(pip_calls) == 1)
check("unchanged HEAD reports the dependency result", "dependencies are up to date" in msg)
tmp.cleanup()

# System-managed Python can use already installed packages without invoking pip.
with tempfile.TemporaryDirectory() as td:
    repo = Path(td)
    (repo / "requirements.txt").write_text(
        "required-example==1.2.3\nPyQt6-WebEngine==6.11.0\n", encoding="utf-8")
    with patch.object(updater, "_externally_managed_python", return_value=True), \
            patch.object(updater.metadata, "version", return_value="1.3.0"), \
            patch.object(updater.subprocess, "run", side_effect=AssertionError("pip ran")):
        managed_result = updater._install_requirements(repo)
    check("managed Python accepts newer required packages and absent optional WebEngine",
          managed_result == (True, ""))

    with patch.object(updater, "_externally_managed_python", return_value=True), \
            patch.object(updater.metadata, "version", return_value="1.0.0"), \
            patch.object(updater.subprocess, "run", side_effect=AssertionError("pip ran")):
        missing_result = updater._install_requirements(repo)
    check("managed Python reports an old required package without calling pip",
          missing_result is not None and not missing_result[0]
          and "required-example" in missing_result[1])
    with patch.object(updater, "_externally_managed_python", return_value=True), \
            patch.object(updater.metadata, "version",
                         side_effect=updater.metadata.PackageNotFoundError):
        absent_result = updater._install_requirements(repo)
    check("managed Python names an absent required package",
          absent_result is not None and not absent_result[0]
          and "required-example 1.2.3 is not installed" in absent_result[1])
    with patch.object(updater, "_externally_managed_python", return_value=True), \
            patch.object(updater, "_git_pull", return_value=_R(0, "Already up to date.")), \
            patch.object(updater.metadata, "version", return_value="1.0.0"), \
            patch.object(updater.subprocess, "run", side_effect=AssertionError("pip ran")):
        managed_ok, managed_message = updater.apply_git(repo)
    check("managed dependency failure gives a package manager repair path",
          not managed_ok and "distribution's package manager" in managed_message
          and "checking the Python dependencies failed" in managed_message
          and "pip install -r" not in managed_message)

for installed, expected in (
        ("6.11.0", True), ("6.11", True), ("6.11.0.0", True),
        ("6.11.0+dfsg1", True), ("6.11.0.post1", True),
        ("1!6.0", True), ("6.12.0", True),
        ("6.10.9", False), ("6.11.0rc1", False), ("6.11.0.dev1", False),
        ("not-a-version", False)):
    with tempfile.TemporaryDirectory() as td:
        repo = Path(td)
        (repo / "requirements.txt").write_text("PyQt6==6.11.0\n", encoding="utf-8")
        dist = repo / "PyQt6-6.11.0.dist-info"
        dist.mkdir()
        (dist / "METADATA").write_text(
            f"Metadata-Version: 2.1\nName: PyQt6\nVersion: {installed}\n",
            encoding="utf-8")
        sys.path.insert(0, td)
        try:
            with patch.object(updater, "_externally_managed_python", return_value=True), \
                    patch.object(updater, "_git_pull", return_value=_R(0, "Already up to date.")), \
                    patch.object(updater.subprocess, "run", side_effect=AssertionError("pip ran")):
                version_ok, version_message = updater.apply_git(repo)
        finally:
            sys.path.pop(0)
        check(f"managed Python handles installed version {installed}", version_ok == expected)
        if installed == "not-a-version":
            check("invalid package versions explain the comparison failure",
                  "Could not compare" in version_message)

with tempfile.TemporaryDirectory() as td:
    repo = Path(td)
    (repo / "requirements.txt").write_text("PyQt6==6.11.0\n", encoding="utf-8")
    with patch.dict(sys.modules, {"packaging": None, "packaging.version": None}), \
            patch.object(updater, "_externally_managed_python", return_value=True), \
            patch.object(updater, "_git_pull", return_value=_R(0, "Already up to date.")), \
            patch.object(updater.subprocess, "run", side_effect=AssertionError("pip ran")):
        parser_ok, parser_message = updater.apply_git(repo)
    check("missing version parser reports the required package",
          not parser_ok and "packaging" in parser_message
          and "distribution's package manager" in parser_message)

# A failed pull never touches pip either.
ok, msg, pip_calls, tmp = run_case(pull_rc=128)
check("failed pull reports failure", not ok and "git pull failed" in msg)
check("failed pull skips pip", pip_calls == [])
tmp.cleanup()

# A dependency failure must reach the UI before it offers a restart.
ok, msg, pip_calls, tmp = run_case(pip_rc=1, pip_err="ERROR: No matching distribution")
check("pip failure leaves the update incomplete", not ok)
check("pip failure shows pip's error", "No matching distribution" in msg)
check("pip failure points at the manual command",
      "pip install -r requirements.txt" in msg)

from nyaatriggers.updater_ui import UpdaterUiMixin
from nyaatriggers import app_common


class _Widget:
    def setVisible(self, value):
        pass

    def setText(self, value):
        pass


offers = []
release = updater.Release("v9.0.0", "9.0.0", "")
host = SimpleNamespace(
    _install_in_flight=True, _pending_release=release,
    _update_applied_version="", _upd_progress=_Widget(), _upd_msg=_Widget(),
    _on_update_available=lambda rel: offers.append(rel),
)
with patch.object(updater, "install_kind", return_value="git"), \
        patch.object(app_common.QMessageBox, "warning") as warning, \
        patch.object(app_common.QMessageBox, "question") as question:
    UpdaterUiMixin._handle_update_done(host, ok, msg)
    check("dependency failure opens a warning with the repair command",
          warning.call_count == 1 and warning.call_args.args[2] == msg)
    check("dependency failure never offers restart", not question.called)
check("dependency failure leaves Install available to retry",
      offers == [release] and host._update_applied_version == ""
      and not host._install_in_flight)
tmp.cleanup()

with tempfile.TemporaryDirectory() as td:
    repo = Path(td)
    attempts = []
    results = iter(((False, "offline"), (True, "")))

    def install_retry(path):
        attempts.append(path)
        return next(results)

    with patch.object(updater, "_git_pull", return_value=_R(0, "Already up to date.")), \
            patch.object(updater, "_install_requirements", install_retry):
        first_ok, _ = updater.apply_git(repo)
        second_ok, _ = updater.apply_git(repo)
    check("the same checkout retries dependencies after a failed install",
          not first_ok and second_ok and attempts == [repo, repo])

# A checkout without requirements.txt (shouldn't happen, but stay silent).
ok, msg, pip_calls, tmp = run_case(with_req=False)
check("missing requirements.txt skips pip without a word",
      ok and pip_calls == [] and "dependencies" not in msg)
tmp.cleanup()

# Fetch rolling tags even for commits already present locally.
seen = []
tmp = tempfile.TemporaryDirectory()

def record_run(argv, **kw):
    seen.append((list(argv), kw))
    return _R(0, "same\n")

orig = updater.subprocess.run
updater.subprocess.run = record_run
try:
    updater.apply_git(Path(tmp.name))
finally:
    updater.subprocess.run = orig
pulls = [argv for argv, _ in seen if "pull" in argv]
pull_kw = [kw for argv, kw in seen if "pull" in argv]
check("pull passes --tags", len(pulls) == 1 and "--tags" in pulls[0])
check("pull pins English output so the conflict parse survives any locale",
      pull_kw[0].get("env", {}).get("LC_ALL") == "C")
tmp.cleanup()

# Preserve conflicting old timeline downloads and retry the pull once.
CONFLICT_ERR = """\
error: The following untracked working tree files would be overwritten by merge:
	timelines/castrum_abania.cactbot.txt
	timelines/sirensong_sea.cactbot.txt
Please move or remove them before you merge.
Aborting
"""

tmp = tempfile.TemporaryDirectory()
repo = Path(tmp.name)
(repo / "requirements.txt").write_text("websockets==16.1.1\n", encoding="utf-8")
(repo / "timelines").mkdir()
for name in ("castrum_abania", "sirensong_sea"):
    (repo / "timelines" / f"{name}.cactbot.txt").write_text("old download\n",
                                                            encoding="utf-8")
calls = []
rev_count = [0]

def conflict_then_ok(argv, **kw):
    calls.append(list(argv))
    if "rev-parse" in argv:
        rev_count[0] += 1
        return _R(0, ("old" if rev_count[0] == 1 else "new") + "\n")
    if "pull" in argv:
        if len([c for c in calls if "pull" in c]) == 1:
            return _R(1, "", CONFLICT_ERR)
        return _R(0, "Updating old..new\nFast-forward\n")
    if argv[0] == sys.executable and "pip" in argv:
        return _R(0)
    raise AssertionError(f"unexpected argv: {argv}")

orig = updater.subprocess.run
updater.subprocess.run = conflict_then_ok
try:
    with patch.object(updater, "_externally_managed_python", return_value=False):
        ok, msg = updater.apply_git(repo)
finally:
    updater.subprocess.run = orig
check("stale cactbot conflicts self heal and the pull retries",
      ok and len([c for c in calls if "pull" in c]) == 2)
check("the stale downloads are gone from the checkout",
      not any((repo / "timelines").glob("*.cactbot.txt")))
check("the message mentions the cleanup", "stale cactbot timeline" in msg)
check("healed pull still refreshes pip requirements",
      "dependencies are up to date" in msg)
tmp.cleanup()

# A conflict on any other path keeps the hands off failure: nothing deleted,
# no retry, the generic message.
MIXED_ERR = CONFLICT_ERR.replace("timelines/sirensong_sea.cactbot.txt",
                                 "triggers.local.json")
tmp = tempfile.TemporaryDirectory()
repo = Path(tmp.name)
(repo / "timelines").mkdir()
(repo / "timelines" / "castrum_abania.cactbot.txt").write_text("old download\n",
                                                               encoding="utf-8")
calls = []

def conflict_only(argv, **kw):
    calls.append(list(argv))
    if "rev-parse" in argv:
        return _R(0, "same\n")
    if "pull" in argv:
        return _R(1, "", MIXED_ERR)
    raise AssertionError(f"unexpected argv: {argv}")

orig = updater.subprocess.run
updater.subprocess.run = conflict_only
try:
    ok, msg = updater.apply_git(repo)
finally:
    updater.subprocess.run = orig
check("mixed conflicts keep the plain failure", not ok and "git pull failed" in msg)
check("mixed conflicts delete nothing",
      (repo / "timelines" / "castrum_abania.cactbot.txt").exists())
check("mixed conflicts never retry the pull",
      len([c for c in calls if "pull" in c]) == 1)
tmp.cleanup()

# Accept git advice prefixed with hint:.
HINT_ERR = CONFLICT_ERR.replace(
    "Please move or remove them before you merge.",
    "hint: Please move or remove them before you merge.")
paths = updater._stale_cactbot_conflicts(HINT_ERR)
check("hint style advice still parses both paths",
      paths == ["timelines/castrum_abania.cactbot.txt",
                "timelines/sirensong_sea.cactbot.txt"])
check("plain failure output parses to no conflicts",
      updater._stale_cactbot_conflicts("rejected: non-fast-forward") == [])


print()
if FAILS:
    print(f"{len(FAILS)} FAILED: {FAILS}")
    sys.exit(1)
print("all tests passed")
