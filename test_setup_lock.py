"""Tests for the cross process first-run setup lock, install.setup_lock.

Two app instances running first-run setup at once both passed the pip gate
and then ran venv create plus pip install into the same venv concurrently,
which can corrupt it. The lock serializes them, breaks the file a killed
holder leaves behind once it outlives the longest legitimate hold, and
fails a waiter with a clear message when the lock cannot be taken.

Each test_* function is both a pytest case and a step of the direct-run
script.

Run directly:  python test_setup_lock.py   (exit 0 = all pass)
        or:    python -m pytest test_setup_lock.py -q
"""
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import install

REPO_DIR = Path(__file__).parent
FAILS = []


def check(name, cond):
    print(("PASS  " if cond else "FAIL  ") + name)
    if not cond:
        FAILS.append(name)
        raise AssertionError(name)


def _patched_lock(td):
    """Point the lock at a temp dir. Returns the saved module state."""
    saved = install._SETUP_LOCK
    install._SETUP_LOCK = Path(td) / "ffxiv.setup.lock"
    return saved


# ── acquire and release ───────────────────────────────────────────────────
def test_setup_lock_acquire_release():
    with tempfile.TemporaryDirectory() as td:
        saved = _patched_lock(td)
        try:
            with install.setup_lock():
                check("held lock file exists", install._SETUP_LOCK.exists())
                check("lock names its holder",
                      install._SETUP_LOCK.read_text() == str(os.getpid()))
            check("release removes the lock", not install._SETUP_LOCK.exists())
        finally:
            install._SETUP_LOCK = saved


# ── a live holder serializes a waiter ──────────────────────────────────────
def test_setup_lock_serializes_waiter():
    with tempfile.TemporaryDirectory() as td:
        saved = _patched_lock(td)
        try:
            held = threading.Event()
            release = threading.Event()
            order = []

            def holder():
                with install.setup_lock():
                    held.set()
                    release.wait(10)
                    order.append("holder-out")

            def waiter():
                with install.setup_lock():
                    order.append("waiter-in")

            t = threading.Thread(target=holder)
            t.start()
            check("holder takes the lock", held.wait(10))
            w = threading.Thread(target=waiter)
            w.start()
            # The waiter polls once a second, so after this it must still be
            # blocked if the lock really excludes it.
            time.sleep(2.5)
            check("waiter stays blocked while held", not order)
            release.set()
            w.join(10)
            t.join(10)
            check("waiter enters after the release",
                  order == ["holder-out", "waiter-in"])
        finally:
            install._SETUP_LOCK = saved


# ── a killed holder's leftover lock is broken by age ──────────────────────
def test_setup_lock_breaks_stale():
    with tempfile.TemporaryDirectory() as td:
        saved = _patched_lock(td)
        try:
            install._SETUP_LOCK.write_text("0")
            old = time.time() - install._SETUP_LOCK_S - 10
            os.utime(install._SETUP_LOCK, (old, old))
            start = time.monotonic()
            with install.setup_lock():
                held = True
            check("stale lock broken right away",
                  held and time.monotonic() - start < 5)
            check("stale file replaced then released",
                  not install._SETUP_LOCK.exists())
        finally:
            install._SETUP_LOCK = saved


# ── a waiter that cannot take the lock bows out with a clear message ──────
def test_setup_lock_bows_out():
    with tempfile.TemporaryDirectory() as td:
        saved = (install._SETUP_LOCK, install._SETUP_WAIT_S)
        install._SETUP_LOCK = Path(td) / "ffxiv.setup.lock"
        # Shrink the wait so the test does not sit out the real deadline. The
        # held lock stays fresh, so only the deadline can end the wait.
        install._SETUP_WAIT_S = 2
        try:
            with install.setup_lock():
                raised = None
                try:
                    with install.setup_lock():
                        pass
                except RuntimeError as e:
                    raised = e
            check("live lock outlasts the wait and raises", raised is not None)
            check("message says another setup is running",
                  raised is not None and "already running" in str(raised))
        finally:
            install._SETUP_LOCK, install._SETUP_WAIT_S = saved


# ── both installers hold the lock around the venv build ────────────────────
def test_setup_lock_used_by_both_installers():
    install_src = (REPO_DIR / "install.py").read_text(encoding="utf-8")
    main_src = (REPO_DIR / "main.py").read_text(encoding="utf-8")
    venv_body = install_src.split("def setup_venv", 1)[1]
    check("install.py locks the venv build", "with setup_lock():" in venv_body)
    worker = main_src.split("class _SetupWorker", 1)[1].split("class _SetupDialog", 1)[0]
    check("main.py locks the venv build",
          "with install.setup_lock():" in worker)


# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            try:
                _fn()
            except AssertionError:
                pass            # check() already recorded the failed step
            except Exception as exc:
                print(f"FAIL  {_name}: {exc!r}")
                FAILS.append(_name)
    print()
    if FAILS:
        print(f"{len(FAILS)} FAILED: {', '.join(FAILS)}")
        sys.exit(1)
    print("all passed")
