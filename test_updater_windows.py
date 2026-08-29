"""Regression test for finish_windows_update's swap/rollback state machine.

Mocks only the Windows-only pieces (_wait_for_pid_exit, the detached relaunch)
and drives the real os.replace/copytree/rollback paths with injected mid-swap
failures. The updater must never leave a half-swapped, un-launchable install,
must log one line per exit (plus a sentinel naming a boot-rejected build), and
must never raise, even when a removal hits a sharing violation.

The tail smoke-covers the Linux in-place swap, apply_frozen_linux: happy path,
rollback when the exe step fails after the _internal swap, and rejection of an
archive with a traversal member.

Run directly:  python test_updater_windows.py   (exit 0 = all pass)
Or via pytest: python -m pytest test_updater_windows.py
"""
import io
import os
import sys
import shutil
import subprocess
import tarfile
import tempfile
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import updater

# Keep a handle on the real pid-wait before the swap tests stub it out below.
_real_wait_for_pid_exit = updater._wait_for_pid_exit

# --- mock the Windows-only pieces -------------------------------------------
updater._wait_for_pid_exit = lambda pid, timeout=90.0: True
LAUNCHED = []
# Bail/rollback paths relaunch via _relaunch_installed. The happy path uses
# _relaunch_and_verify, mocked here as "booted OK". Both record into LAUNCHED.
updater._relaunch_installed = lambda exe_dst, dest_dir: LAUNCHED.append(Path(exe_dst))
updater._relaunch_and_verify = lambda exe_dst, dest_dir, grace=25.0: (
    LAUNCHED.append(Path(exe_dst)) or True)

EXE = "NyaaTriggers.exe"
OLD_INTERNAL = {"a.txt": "OLD", "b.txt": "OLD", "python3.dll": "OLD"}
NEW_INTERNAL = {"a.txt": "NEW", "c.txt": "NEW", "python3.dll": "NEW"}


def build(base, nested=False):
    """Fresh install dir + staging new_root (sibling .nyaa-update-* dir, as the
    real apply_frozen_windows makes). With nested=True the app root sits one
    folder inside the staging dir, as the wrapped release archive extracts.
    Returns (inst, new_root)."""
    inst = Path(base) / "Program" / "NyaaTriggers"
    (inst / "_internal").mkdir(parents=True)
    for n, c in OLD_INTERNAL.items():
        (inst / "_internal" / n).write_text(c)
    (inst / EXE).write_text("OLD-EXE")
    (inst / "settings.json").write_text("USERDATA")   # a user-data sibling

    staging = Path(tempfile.mkdtemp(prefix=".nyaa-update-", dir=str(inst.parent)))
    new_root = staging / "NyaaTriggers" if nested else staging
    (new_root / "_internal").mkdir(parents=True)
    for n, c in NEW_INTERNAL.items():
        (new_root / "_internal" / n).write_text(c)
    (new_root / EXE).write_text("NEW-EXE")
    return inst, new_root


def snap_internal(inst):
    d = inst / "_internal"
    return {p.name: p.read_text() for p in d.iterdir()} if d.is_dir() else None


def leftovers(inst):
    return sorted(p.name for p in inst.iterdir())


def check(label, cond):
    print(("  PASS  " if cond else "  FAIL  ") + label)
    if not cond:
        check.failed += 1
check.failed = 0


# === Test 1: happy path =====================================================
print("Test 1: happy path (full swap)")
with tempfile.TemporaryDirectory() as base:
    inst, new_root = build(base)
    LAUNCHED.clear()
    updater.finish_windows_update(inst, new_root, old_pid=1, exe_name=EXE)
    internal = snap_internal(inst)
    check("exe replaced with NEW", (inst / EXE).read_text() == "NEW-EXE")
    check("_internal is the NEW tree", internal == NEW_INTERNAL)
    check("stale OLD-only file b.txt is gone", "b.txt" not in internal)
    check("user data untouched", (inst / "settings.json").read_text() == "USERDATA")
    check("relaunched the installed exe", LAUNCHED == [inst / EXE])
    check("log records the successful boot",
          "booted OK" in (inst / updater._UPDATE_LOG_NAME).read_text())
    baks = [n for n in leftovers(inst) if n.endswith(updater._BACKUP_SUFFIX)]
    check("backups carry _BACKUP_SUFFIX (sweepable)",
          all(b.endswith(".nyaa-old") for b in baks))
    updater.cleanup_old_backups(inst)
    check("cleanup removed all .nyaa-old backups",
          not [n for n in leftovers(inst) if n.endswith(".nyaa-old")])
    check("no .new scratch left behind",
          not [n for n in leftovers(inst) if ".new" in n])


# === Test 2: failure on the FINAL exe rename (hardest rollback) =============
# _internal is already NEW and the old exe is in backup. Rollback must restore
# both to OLD for a launchable install.
print("Test 2: inject failure at the last step (exe_new -> exe_dst)")
with tempfile.TemporaryDirectory() as base:
    inst, new_root = build(base)
    LAUNCHED.clear()
    real_replace = os.replace
    def faulty_replace(src, dst, *a, **k):
        if str(src).endswith(".new" + updater._BACKUP_SUFFIX) and Path(src).name.startswith(EXE):
            raise OSError("injected: AV grabbed the new exe")
        return real_replace(src, dst, *a, **k)
    os.replace = faulty_replace
    try:
        updater.finish_windows_update(inst, new_root, old_pid=1, exe_name=EXE)
    finally:
        os.replace = real_replace
    internal = snap_internal(inst)
    check("exe rolled back to OLD", (inst / EXE).read_text() == "OLD-EXE")
    check("_internal rolled back to OLD (consistent)", internal == OLD_INTERNAL)
    check("user data untouched", (inst / "settings.json").read_text() == "USERDATA")
    check("install looks intact -> relaunched OLD", LAUNCHED == [inst / EXE])
    check("log records the swap failure",
          "swap failed" in (inst / updater._UPDATE_LOG_NAME).read_text())
    updater.cleanup_old_backups(inst)
    check("no .new/.nyaa-old leftovers after cleanup",
          not [n for n in leftovers(inst) if ".new" in n or n.endswith(".nyaa-old")])


# === Test 3: failure during the initial sibling copy (nothing live touched) =
print("Test 3: inject failure during copytree into the sibling")
with tempfile.TemporaryDirectory() as base:
    inst, new_root = build(base)
    LAUNCHED.clear()
    real_copytree = shutil.copytree
    def faulty_copytree(src, dst, *a, **k):
        raise OSError("injected: disk full while staging new _internal")
    shutil.copytree = faulty_copytree
    try:
        updater.finish_windows_update(inst, new_root, old_pid=1, exe_name=EXE)
    finally:
        shutil.copytree = real_copytree
    internal = snap_internal(inst)
    check("live _internal untouched (still OLD)", internal == OLD_INTERNAL)
    check("live exe untouched (still OLD)", (inst / EXE).read_text() == "OLD-EXE")
    check("relaunched the (untouched) install", LAUNCHED == [inst / EXE])
    check("no partial .new dir left in install",
          not [n for n in leftovers(inst) if ".new" in n])


# === Test 4: old process never exits -> bail, no swap =======================
print("Test 4: _wait_for_pid_exit returns False -> leave install untouched")
with tempfile.TemporaryDirectory() as base:
    inst, new_root = build(base)
    LAUNCHED.clear()
    updater._wait_for_pid_exit = lambda pid, timeout=90.0: False
    try:
        updater.finish_windows_update(inst, new_root, old_pid=1, exe_name=EXE)
    finally:
        updater._wait_for_pid_exit = lambda pid, timeout=90.0: True
    check("install left fully OLD",
          snap_internal(inst) == OLD_INTERNAL and (inst / EXE).read_text() == "OLD-EXE")
    check("relaunched old exe anyway", LAUNCHED == [inst / EXE])
    check("no backups/scratch created",
          not [n for n in leftovers(inst) if ".new" in n or n.endswith(".nyaa-old")])
    log_lines = (inst / updater._UPDATE_LOG_NAME).read_text().strip().splitlines()
    check("log records the pid-wait bail (one line)",
          len(log_lines) == 1 and "did not exit" in log_lines[0])


# === Test 5: cleanup protects the only good _internal copy ==================
print("Test 5: cleanup keeps _internal backup when live _internal is missing")
with tempfile.TemporaryDirectory() as base:
    inst, _ = build(base)
    shutil.rmtree(inst / "_internal")               # live gone
    bak = inst / f"_internal.999{updater._BACKUP_SUFFIX}"
    bak.mkdir(); (bak / "python3.dll").write_text("OLD")
    updater.cleanup_old_backups(inst)
    check("backup _internal NOT deleted while live is missing", bak.is_dir())
    (inst / "_internal").mkdir(); (inst / "_internal" / "python3.dll").write_text("X")
    updater.cleanup_old_backups(inst)
    check("backup _internal swept once live is healthy", not bak.exists())


# === Test 6: helper sanity ==================================================
print("Test 6: helper sanity")
with tempfile.TemporaryDirectory() as base:
    inst, _ = build(base)
    check("_install_looks_intact True for healthy install",
          updater._install_looks_intact(inst, inst / EXE))
    check("_dir_writable True for a writable dir", updater._dir_writable(inst))
    shutil.rmtree(inst / "_internal")
    check("_install_looks_intact False when _internal gone",
          not updater._install_looks_intact(inst, inst / EXE))
    real_unlink = Path.unlink
    def faulty_unlink(self, *a, **k):
        raise PermissionError("injected: sharing violation (WinError 32)")
    Path.unlink = faulty_unlink
    try:
        updater._force_remove(inst / EXE)   # must not raise
        swallowed = True
    except PermissionError:
        swallowed = False
    finally:
        Path.unlink = real_unlink
    check("_force_remove swallows a sharing violation on a file", swallowed)


# === Test 7: new build swaps in but won't boot -> roll back to OLD ===========
# Swap succeeds on disk but the new build fails to come up (e.g. AV quarantined
# a DLL). Updater must restore and relaunch the previous version.
print("Test 7: swapped build fails to boot -> rollback + relaunch OLD")
with tempfile.TemporaryDirectory() as base:
    inst, new_root = build(base)
    # The rejected build's version is read from its staged source when present.
    (new_root / "app_common.py").write_text('_VERSION = "9.9.9"\n')
    LAUNCHED.clear()
    updater._relaunch_and_verify = lambda exe_dst, dest_dir, grace=25.0: (
        LAUNCHED.append(Path(exe_dst)) or False)   # new build does NOT boot
    try:
        updater.finish_windows_update(inst, new_root, old_pid=1, exe_name=EXE)
    finally:
        updater._relaunch_and_verify = lambda exe_dst, dest_dir, grace=25.0: (
            LAUNCHED.append(Path(exe_dst)) or True)
    internal = snap_internal(inst)
    check("exe rolled back to OLD", (inst / EXE).read_text() == "OLD-EXE")
    check("_internal rolled back to OLD (consistent)", internal == OLD_INTERNAL)
    check("user data untouched", (inst / "settings.json").read_text() == "USERDATA")
    check("tried the new build, then relaunched the rolled-back OLD",
          LAUNCHED == [inst / EXE, inst / EXE])
    check("install looks intact after rollback",
          updater._install_looks_intact(inst, inst / EXE))
    sentinel = inst / updater._REJECTED_NAME
    check("sentinel names the rejected version",
          sentinel.is_file() and "9.9.9" in sentinel.read_text())
    check("log records the boot-verify rollback",
          "rejected 9.9.9" in (inst / updater._UPDATE_LOG_NAME).read_text())
    updater.cleanup_old_backups(inst)
    check("no .new/.nyaa-old leftovers after cleanup",
          not [n for n in leftovers(inst) if ".new" in n or n.endswith(".nyaa-old")])


# === Test 8: removals hit a sharing violation -> still never raises ==========
# _force_remove's file branch used to catch only FileNotFoundError, so a locked
# file propagated out of the rollback tail, breaking the "never raises" promise
# and skipping the relaunch. Drive the whole boot-fail path with unlink broken.
print("Test 8: PermissionError on every unlink -> rollback completes anyway")
with tempfile.TemporaryDirectory() as base:
    inst, new_root = build(base)
    LAUNCHED.clear()
    updater._relaunch_and_verify = lambda exe_dst, dest_dir, grace=25.0: (
        LAUNCHED.append(Path(exe_dst)) or False)   # new build does NOT boot
    real_unlink = Path.unlink
    def faulty_unlink(self, *a, **k):
        raise PermissionError("injected: sharing violation (WinError 32)")
    Path.unlink = faulty_unlink
    try:
        updater.finish_windows_update(inst, new_root, old_pid=1, exe_name=EXE)
        raised = False
    except Exception:  # noqa: BLE001
        raised = True
    finally:
        Path.unlink = real_unlink
        updater._relaunch_and_verify = lambda exe_dst, dest_dir, grace=25.0: (
            LAUNCHED.append(Path(exe_dst)) or True)
    check("finish_windows_update never raised", not raised)
    check("exe rolled back to OLD", (inst / EXE).read_text() == "OLD-EXE")
    check("_internal rolled back to OLD (consistent)",
          snap_internal(inst) == OLD_INTERNAL)
    check("tried the new build, then relaunched the rolled-back OLD",
          LAUNCHED == [inst / EXE, inst / EXE])
    updater.cleanup_old_backups(inst)
    check("locked scratch swept once it unlocks",
          not [n for n in leftovers(inst) if ".new" in n or n.endswith(".nyaa-old")])


# === Test 9: --apply-update argv validation (H-2) ===========================
# finish_windows_update takes dest/staging/pid straight from sys.argv. It must
# refuse anything that doesn't look like a real apply_frozen_windows hand-off.
print("Test 9: --apply-update validation refuses alien dest/staging/pid")
with tempfile.TemporaryDirectory() as base:
    inst, new_root = build(base)
    LAUNCHED.clear()
    updater.finish_windows_update(inst, new_root, old_pid=0, exe_name=EXE)
    check("pid=0 refused, install untouched",
          snap_internal(inst) == OLD_INTERNAL and (inst / EXE).read_text() == "OLD-EXE")
    check("pid=0 refusal relaunches nothing", LAUNCHED == [])
    check("pid=0 refusal is logged",
          "refused" in (inst / updater._UPDATE_LOG_NAME).read_text())
with tempfile.TemporaryDirectory() as base:
    # Correctly-named staging, but not next to the install dir -> refuse.
    inst, _ = build(base)
    alien = Path(base) / "elsewhere" / ".nyaa-update-zz"
    (alien / "_internal").mkdir(parents=True)
    (alien / "_internal" / "a.txt").write_text("NEW")
    (alien / EXE).write_text("NEW-EXE")
    LAUNCHED.clear()
    updater.finish_windows_update(inst, alien, old_pid=1, exe_name=EXE)
    check("staging outside dest's parent refused, install untouched",
          snap_internal(inst) == OLD_INTERNAL and (inst / EXE).read_text() == "OLD-EXE")
    check("alien-staging refusal relaunches nothing", LAUNCHED == [])
with tempfile.TemporaryDirectory() as base:
    # A sibling of the install dir without the staging prefix -> refuse.
    inst, _ = build(base)
    bad = inst.parent / "NyaaTriggers-new"
    (bad / "_internal").mkdir(parents=True)
    (bad / "_internal" / "a.txt").write_text("NEW")
    (bad / EXE).write_text("NEW-EXE")
    LAUNCHED.clear()
    updater.finish_windows_update(inst, bad, old_pid=1, exe_name=EXE)
    check("wrong staging name refused, install untouched",
          snap_internal(inst) == OLD_INTERNAL and (inst / EXE).read_text() == "OLD-EXE")
    check("wrong-name refusal relaunches nothing", LAUNCHED == [])
with tempfile.TemporaryDirectory() as base:
    # The real hand-off: app root nested one folder inside the staging dir.
    inst, new_root = build(base, nested=True)
    LAUNCHED.clear()
    updater.finish_windows_update(inst, new_root, old_pid=1, exe_name=EXE)
    check("well-formed nested staging accepted: exe swapped",
          (inst / EXE).read_text() == "NEW-EXE")
    check("well-formed nested staging accepted: _internal swapped",
          snap_internal(inst) == NEW_INTERNAL)


# === Test 10: cleanup only sweeps real staging dirs (L-8) ===================
print("Test 10: cleanup_old_backups leaves non-staging .nyaa-update-* dirs alone")
with tempfile.TemporaryDirectory() as base:
    inst, leftover = build(base)   # leftover is a real (flat) staging dir
    user_dir = inst.parent / ".nyaa-update-notes"
    user_dir.mkdir()
    (user_dir / "readme.txt").write_text("mine")
    wrapped = Path(tempfile.mkdtemp(prefix=".nyaa-update-", dir=str(inst.parent)))
    (wrapped / "NyaaTriggers" / "_internal").mkdir(parents=True)
    (wrapped / "NyaaTriggers" / "_internal" / "x.dll").write_text("X")
    (wrapped / "NyaaTriggers" / EXE).write_text("X")
    empty = Path(tempfile.mkdtemp(prefix=".nyaa-update-", dir=str(inst.parent)))
    updater.cleanup_old_backups(inst)
    check("user folder matching the name survives", user_dir.is_dir())
    check("real flat staging swept", not leftover.exists())
    check("real wrapped staging swept", not wrapped.exists())
    check("staging without the marker left alone", empty.is_dir())


# === Test 11: tasklist fallback only trusts a clean "no tasks" (L-10) =======
print("Test 11: _wait_for_pid_exit fallback classifies tasklist output safely")

def fake_tasklist(stdout, returncode):
    class _R:
        pass
    r = _R()
    r.stdout, r.returncode = stdout, returncode
    return lambda *a, **k: r

real_run, real_sleep = subprocess.run, time.sleep
try:
    time.sleep = lambda s: None   # poll/deadline timing, instantly
    cases = [
        # (tasklist stdout, returncode, expect "old process exited")
        ('"NyaaTriggers.exe","1234","Console","1","100,000 K"', 0, False),   # live row
        ("INFO: No tasks are running which match the specified criteria.", 0, True),
        # tasklist localizes the no-tasks line. Only the INFO prefix is stable.
        ("INFO: Es sind keine Aufgaben vorhanden, die den Kriterien entsprechen.", 0, True),
        # French puts a space before the colon.
        ("INFO : Aucune tâche ne correspond aux critères spécifiés.", 0, True),
        ("", 0, True),                 # clean empty stdout
        ("", 1, False),                # empty but the probe failed
        ("ERROR: The RPC server is unavailable.", 0, False),
        ("ERROR: The RPC server is unavailable.", 1, False),
    ]
    for out, rc, expected in cases:
        subprocess.run = fake_tasklist(out, rc)
        got = _real_wait_for_pid_exit(1234, timeout=0.05)
        check(f"tasklist rc={rc} stdout={out[:24]!r} -> exited={expected}",
              got == expected)
finally:
    subprocess.run, time.sleep = real_run, real_sleep


# === Test 12: download() enforces a hard byte cap (L-23) ====================
print("Test 12: download() caps runaway streams with missing/lying Content-Length")

class _FakeResp:
    """urlopen stand-in: serves the given chunks, or the same chunk forever
    when chunks is None (a server that never stops sending)."""
    def __init__(self, total, chunks):
        self.headers = {"Content-Length": str(total)} if total else {}
        self._chunks = chunks
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False
    def read(self, n):
        if self._chunks is None:
            return b"x" * 262144
        return self._chunks.pop(0) if self._chunks else b""

with tempfile.TemporaryDirectory() as base:
    dest = Path(base) / "update.zip"
    real_urlopen = updater.urllib.request.urlopen
    real_cap = updater._MAX_DOWNLOAD_BYTES
    updater._MAX_DOWNLOAD_BYTES = 1 << 20   # 1 MB, so the test moves 2 MB not 2 GB
    try:
        for total in (0, 10 << 20):   # no Content-Length; and one lying about 10 MB
            updater.urllib.request.urlopen = lambda *a, **k: _FakeResp(total, None)
            try:
                updater.download("https://x/update.zip", dest)
                raised = False
            except OSError:
                raised = True
            check(f"cap raises past the limit (Content-Length={total})", raised)
            check(f".part cleaned up, no dest left (Content-Length={total})",
                  not dest.exists() and not list(Path(base).glob("*.part")))
        # A normal download under the cap still succeeds.
        body = [b"y" * 262144] * 3
        updater.urllib.request.urlopen = (
            lambda *a, **k: _FakeResp(len(b"".join(body)), list(body)))
        updater.download("https://x/update.zip", dest)
        check("download under the cap succeeds", dest.read_bytes() == b"".join(body))
    finally:
        updater.urllib.request.urlopen = real_urlopen
        updater._MAX_DOWNLOAD_BYTES = real_cap


# === Test 13: failed _internal restore logs + drops RECOVER.txt (M-3) =======
# The rollback restore rename already retries via _retry_locked; when it still
# fails the install is left without a working _internal. That must be loud:
# a log line plus a RECOVER.txt naming the backup to rename back by hand.
print("Test 13: rollback that cannot restore _internal leaves RECOVER.txt")
with tempfile.TemporaryDirectory() as base:
    inst, new_root = build(base)
    LAUNCHED.clear()
    updater._relaunch_and_verify = lambda exe_dst, dest_dir, grace=25.0: (
        LAUNCHED.append(Path(exe_dst)) or False)   # new build does NOT boot
    real_replace = os.replace
    real_sleep = time.sleep
    time.sleep = lambda s: None          # _retry_locked's retry budget, instantly
    def stuck_restore(src, dst, *a, **k):
        # Only the rollback's backup -> _internal restore rename never succeeds.
        if (Path(src).name.startswith("_internal.") and ".new" not in Path(src).name
                and Path(dst).name == "_internal"):
            raise PermissionError("injected: restore target stays locked")
        return real_replace(src, dst, *a, **k)
    os.replace = stuck_restore
    try:
        updater.finish_windows_update(inst, new_root, old_pid=1, exe_name=EXE)
        raised = False
    except Exception:  # noqa: BLE001
        raised = True
    finally:
        os.replace = real_replace
        time.sleep = real_sleep
        updater._relaunch_and_verify = lambda exe_dst, dest_dir, grace=25.0: (
            LAUNCHED.append(Path(exe_dst)) or True)
    check("finish_windows_update never raised", not raised)
    check("exe still rolled back to OLD", (inst / EXE).read_text() == "OLD-EXE")
    recover = inst / "RECOVER.txt"
    check("RECOVER.txt dropped", recover.is_file())
    bak = [n for n in leftovers(inst)
           if n.startswith("_internal.") and n.endswith(".nyaa-old")]
    check("RECOVER.txt names the exact backup folder",
          recover.is_file() and bak and bak[0] in recover.read_text())
    check("log records the failed restore",
          "rollback failed to restore" in (inst / updater._UPDATE_LOG_NAME).read_text())


# === Test 17: failed exe restore logs + drops RECOVER.txt ===================
# The exe restore gets the same loud treatment as the _internal restore. When
# the backup will not rename back the install has no exe at all, so the log
# line and RECOVER.txt naming the exe backup are the only recovery pointer.
print("Test 17: rollback that cannot restore the exe leaves RECOVER.txt")
with tempfile.TemporaryDirectory() as base:
    inst, new_root = build(base)
    LAUNCHED.clear()
    updater._relaunch_and_verify = lambda exe_dst, dest_dir, grace=25.0: (
        LAUNCHED.append(Path(exe_dst)) or False)   # new build does NOT boot
    real_replace = os.replace
    real_sleep = time.sleep
    time.sleep = lambda s: None          # _retry_locked's retry budget, instantly
    def stuck_exe_restore(src, dst, *a, **k):
        # Only the rollback's backup -> exe restore rename never succeeds.
        if (Path(src).name.startswith(EXE + ".") and ".new" not in Path(src).name
                and Path(dst).name == EXE):
            raise PermissionError("injected: exe restore target stays locked")
        return real_replace(src, dst, *a, **k)
    os.replace = stuck_exe_restore
    try:
        updater.finish_windows_update(inst, new_root, old_pid=1, exe_name=EXE)
        raised = False
    except Exception:  # noqa: BLE001
        raised = True
    finally:
        os.replace = real_replace
        time.sleep = real_sleep
        updater._relaunch_and_verify = lambda exe_dst, dest_dir, grace=25.0: (
            LAUNCHED.append(Path(exe_dst)) or True)
    check("finish_windows_update never raised", not raised)
    check("_internal still rolled back to OLD", snap_internal(inst) == OLD_INTERNAL)
    check("no exe left after the failed restore", not (inst / EXE).exists())
    recover = inst / "RECOVER.txt"
    check("RECOVER.txt dropped", recover.is_file())
    bak = [n for n in leftovers(inst)
           if n.startswith(EXE + ".") and ".new" not in n
           and n.endswith(updater._BACKUP_SUFFIX)]
    check("RECOVER.txt names the exact exe backup",
          recover.is_file() and bak and bak[0] in recover.read_text())
    check("RECOVER.txt names the exe to rename back to",
          recover.is_file() and f"    {EXE}\n" in recover.read_text())
    check("log records the failed exe restore",
          "rollback failed to restore" in (inst / updater._UPDATE_LOG_NAME).read_text())


# === Tests 14-16: apply_frozen_linux in-place swap ==========================
# The Linux path needs no mocks: the running exe and _internal are not locked,
# so the swap is plain renames in a temp install dir. Drive a real tar.gz
# shaped like the release archive, one top-level NyaaTriggers/ folder.

def build_linux(base):
    """Fresh Linux install dir: exe, _internal, one user-data sibling."""
    inst = Path(base) / "NyaaTriggers"
    (inst / "_internal").mkdir(parents=True)
    for n, c in OLD_INTERNAL.items():
        (inst / "_internal" / n).write_text(c)
    (inst / "NyaaTriggers").write_text("OLD-EXE")
    (inst / "settings.json").write_text("USERDATA")
    return inst


def make_linux_tar(base):
    """A release-shaped tar.gz holding the NEW build. Returns the tar path."""
    new_root = Path(base) / "pkgsrc" / "NyaaTriggers"
    (new_root / "_internal").mkdir(parents=True)
    for n, c in NEW_INTERNAL.items():
        (new_root / "_internal" / n).write_text(c)
    (new_root / "NyaaTriggers").write_text("NEW-EXE")
    tar_path = Path(base) / "update.tar.gz"
    with tarfile.open(tar_path, "w:gz") as tf:
        tf.add(new_root, arcname="NyaaTriggers")
    return tar_path


print("Test 14: Linux happy path (in-place swap)")
with tempfile.TemporaryDirectory() as base:
    inst = build_linux(base)
    tar_path = make_linux_tar(base)
    ok, msg = updater.apply_frozen_linux(tar_path, dest_dir=inst, exe_name="NyaaTriggers")
    check("linux swap ok", ok)
    check("linux exe replaced with NEW", (inst / "NyaaTriggers").read_text() == "NEW-EXE")
    check("linux _internal is the NEW tree", snap_internal(inst) == NEW_INTERNAL)
    check("linux user data untouched", (inst / "settings.json").read_text() == "USERDATA")
    baks = [n for n in leftovers(inst) if n.endswith(updater._BACKUP_SUFFIX)]
    check("linux old exe + _internal parked as sweepable backups",
          f"_internal.{os.getpid()}{updater._BACKUP_SUFFIX}" in baks
          and f"NyaaTriggers.{os.getpid()}{updater._BACKUP_SUFFIX}" in baks)
    check("linux staging dir swept",
          not list(Path(base).glob(f"{updater._STAGING_PREFIX}*")))
    updater.cleanup_old_backups(inst)
    check("linux cleanup removed the backups",
          not [n for n in leftovers(inst) if n.endswith(updater._BACKUP_SUFFIX)])


print("Test 15: Linux exe rename fails after the _internal swap -> rollback")
with tempfile.TemporaryDirectory() as base:
    inst = build_linux(base)
    tar_path = make_linux_tar(base)
    exe_dst = inst / "NyaaTriggers"
    real_replace = os.replace
    def faulty_replace(src, dst, *a, **k):
        if Path(dst) == exe_dst:
            raise OSError("injected: exe replace failed")
        return real_replace(src, dst, *a, **k)
    os.replace = faulty_replace
    try:
        ok, msg = updater.apply_frozen_linux(tar_path, dest_dir=inst, exe_name="NyaaTriggers")
    finally:
        os.replace = real_replace
    check("linux swap reports failure", not ok)
    check("linux exe still OLD", exe_dst.read_text() == "OLD-EXE")
    check("linux _internal rolled back to OLD (consistent)",
          snap_internal(inst) == OLD_INTERNAL)
    check("linux user data untouched", (inst / "settings.json").read_text() == "USERDATA")
    updater.cleanup_old_backups(inst)
    check("linux cleanup sweeps the failure leftovers",
          not [n for n in leftovers(inst) if n.endswith(updater._BACKUP_SUFFIX)])


print("Test 16: Linux tar with a traversal member is rejected, install untouched")
with tempfile.TemporaryDirectory() as base:
    inst = build_linux(base)
    tar_path = Path(base) / "evil.tar.gz"
    with tarfile.open(tar_path, "w:gz") as tf:
        data = b"evil"
        info = tarfile.TarInfo("../evil.txt")
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
    ok, msg = updater.apply_frozen_linux(tar_path, dest_dir=inst, exe_name="NyaaTriggers")
    check("traversal tar rejected", not ok and "unsafe path" in msg)
    check("nothing written outside staging", not (Path(base) / "evil.txt").exists())
    check("linux install untouched",
          snap_internal(inst) == OLD_INTERNAL
          and (inst / "NyaaTriggers").read_text() == "OLD-EXE")


# === Test 18: RECOVER.txt shields the backups it names =======================
# A failed rollback drops RECOVER.txt naming the *.nyaa-old backup to rename
# back by hand. The next-launch sweep must not delete that backup, even with a
# non-empty _internal making internal_ok True.
print("Test 18: cleanup keeps every backup while RECOVER.txt is present")
with tempfile.TemporaryDirectory() as base:
    inst, _ = build(base)
    internal_bak = inst / f"_internal.999{updater._BACKUP_SUFFIX}"
    internal_bak.mkdir(); (internal_bak / "python3.dll").write_text("OLD")
    exe_bak = inst / f"{EXE}.999{updater._BACKUP_SUFFIX}"
    exe_bak.write_text("OLD-EXE")
    recover = inst / "RECOVER.txt"
    recover.write_text(f"rename {internal_bak.name} back by hand")
    updater.cleanup_old_backups(inst)
    check("_internal backup kept while RECOVER.txt is present", internal_bak.is_dir())
    check("exe backup kept while RECOVER.txt is present", exe_bak.is_file())
    recover.unlink()
    updater.cleanup_old_backups(inst)
    check("backups swept once RECOVER.txt is gone",
          not internal_bak.exists() and not exe_bak.exists())


# === Test 19: stale launcher .part sweep respects the age guard ==============
# apply_frozen_linux copies the launcher via a NyaaTriggers.sh.<pid>.<tid>.part
# temp in the install dir. A kill mid copy leaks it, so the launch sweep removes
# aged ones but leaves a concurrent update's fresh copy alone.
print("Test 19: cleanup sweeps aged launcher .part, keeps the in-flight copy")
with tempfile.TemporaryDirectory() as base:
    inst, _ = build(base)
    stale = inst / "NyaaTriggers.sh.111.222.part"
    stale.write_text("half a launcher")
    old = time.time() - 7200
    os.utime(stale, (old, old))
    fresh = inst / f"NyaaTriggers.sh.{os.getpid()}.333.part"
    fresh.write_text("copying now")
    updater.cleanup_old_backups(inst)
    check("aged launcher .part swept", not stale.exists())
    check("fresh launcher .part untouched", fresh.is_file())


# === Test 20: RECOVER.txt tells the user to delete it ========================
# The backup sweep stays off while RECOVER.txt exists, so the note must say to
# remove it once the app starts or backups pile up for the life of the install.
print("Test 20: RECOVER.txt tells the user to delete it after recovery")
with tempfile.TemporaryDirectory() as base:
    inst, _ = build(base)
    bak = inst / f"{EXE}.999{updater._BACKUP_SUFFIX}"
    updater._drop_recover_note(inst, bak, EXE)
    note = (inst / "RECOVER.txt").read_text()
    check("note names the backup", bak.name in note)
    check("note says to delete RECOVER.txt once the app starts",
          "delete this RECOVER.txt" in note)


print()
print("RESULT:", "ALL PASS" if check.failed == 0 else f"{check.failed} CHECK(S) FAILED")


def test_module_suite():
    """pytest entry: the checks above run at import; report them as one test."""
    assert check.failed == 0


if __name__ == "__main__":
    sys.exit(1 if check.failed else 0)
