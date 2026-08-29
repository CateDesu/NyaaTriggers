"""Regression tests for triggevent_bridge.py.

Covers the engine build timeout kill: a timed out Maven build must die with
its whole tree. On POSIX the build runs in its own process group and killpg
reaches everything. On Windows build.bat runs through cmd.exe, and a plain
proc.kill is TerminateProcess on that wrapper only, which orphaned Maven and
the java compilers on the shared event-trigger tree. The Windows branch runs
taskkill /T.

Also covers the callout seq gap mark being scoped to one sidecar generation:
the jar numbers callouts from 1 each generation, so a high-water mark shared
across generations, which survived a spontaneous exit plus the reconcile
restart because stop() early-returns once the reader-exit path cleared the
state, muted real gap reports below the old mark. The mark now rides the
generation via the reader thread args, same as proc and wq.

Run directly:  python test_triggevent_bridge.py   (exit 0 = all pass)
"""
import io
import os
import signal
import subprocess
import sys
import threading
import time
import unittest.mock as mock
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import triggevent_bridge as tb

FAILS = []


def check(name, cond):
    print(("PASS  " if cond else "FAIL  ") + name)
    if not cond:
        FAILS.append(name)


class FakeProc:
    def __init__(self, pid=4242):
        self.pid = pid
        self.killed = False

    def kill(self):
        self.killed = True


# POSIX: the kill goes to the process group, not the bare child.
calls = []
_orig = (os.killpg, os.getpgid)
os.killpg = lambda pgid, sig: calls.append((pgid, sig))
os.getpgid = lambda pid: pid + 1000
try:
    p = FakeProc()
    tb._kill_build_tree(p)
finally:
    os.killpg, os.getpgid = _orig
check("posix kill hits the process group with SIGKILL",
      calls == [(5242, signal.SIGKILL)] and not p.killed)

# POSIX, group already gone: fall back to a direct kill on the proc.
def _gone(*_a):
    raise ProcessLookupError()


_orig = (os.killpg, os.getpgid)
os.killpg = _gone
os.getpgid = lambda pid: pid
try:
    p = FakeProc()
    tb._kill_build_tree(p)
finally:
    os.killpg, os.getpgid = _orig
check("a gone process group falls back to a direct kill", p.killed)

# Windows: taskkill /T takes the tree below the cmd.exe wrapper.
runs = []
_orig = (os.name, tb.subprocess.run)
os.name = "nt"
tb.subprocess.run = lambda *a, **k: runs.append(a[0]) or subprocess.CompletedProcess(a[0], 0)
try:
    p = FakeProc()
    tb._kill_build_tree(p)
finally:
    os.name, tb.subprocess.run = _orig
check("windows kill taskkills the whole tree",
      runs == [["taskkill", "/F", "/T", "/PID", "4242"]] and not p.killed)


# ── the callout seq gap mark is scoped to one sidecar generation ──────────────
# Two generations, each with its own seq_state the way start() binds them via
# the reader thread args. Gaps inside a generation must report. A new
# generation numbering from 1 must not trip over the old generation's mark,
# and a late write from the old generation's still draining reader must not
# poison the new one.
drops = []
_o_drop = tb.log_drop
tb.log_drop = lambda site, detail, *a, **k: drops.append((site, detail))
try:
    tv = tb.TriggeventBridge()
    gen_a: dict = {"last": None}
    tv._dispatch({"t": "callout", "seq": 1, "tts": "a"}, gen_a)
    tv._dispatch({"t": "callout", "seq": 4, "tts": "a"}, gen_a)
    check("a seq gap inside one generation is reported",
          any(site == "engine-seq" and "gap 1 -> 4" in d for site, d in drops))

    drops.clear()
    gen_b: dict = {"last": None}
    tv._dispatch({"t": "callout", "seq": 1, "tts": "b"}, gen_b)
    check("a fresh generation numbering from 1 reports no gap", drops == [])

    # the old generation's reader still draining after the restart
    tv._dispatch({"t": "callout", "seq": 500, "tts": "late a"}, gen_a)
    check("a late old generation write reports its own gap, not the new one's",
          any("gap 4 -> 500" in d for _, d in drops))
    drops.clear()
    tv._dispatch({"t": "callout", "seq": 1, "tts": "b again"}, gen_b)
    check("the late old write did not poison the new generation", drops == [])
    tv._dispatch({"t": "callout", "seq": 3, "tts": "b gap"}, gen_b)
    check("gaps in the new generation still report after the late write",
          any(site == "engine-seq" and "gap 1 -> 3" in d for site, d in drops))
finally:
    tb.log_drop = _o_drop

src = Path("triggevent_bridge.py").read_text(encoding="utf-8")
import ast
_attrs = {n.attr for n in ast.walk(ast.parse(src))
          if isinstance(n, ast.Attribute) and n.attr == "_last_callout_seq"}
check("no shared seq mark field remains on the bridge", not _attrs)
check("start binds a fresh seq state to the reader thread args",
      'seq_state: dict = {"last": None}' in src.split("def start", 1)[1].split("def stop", 1)[0])


# ── a full boot with the fake sidecar: the reader must run to its EOF exit ────
# Catches an arity mistake in the reader thread args, which would die silently
# inside the daemon thread and leave _active stuck on.
class _FakeSidecar:
    def __init__(self):
        self.pid = 4711
        self.stdin = io.StringIO()
        self.stdout = io.StringIO("")     # immediate EOF
        self.stderr = io.StringIO("")

    def poll(self):
        return 0

    def wait(self, timeout=None):
        return 0


_fake = _FakeSidecar()
with mock.patch.object(tb, "_find_java", return_value="/usr/bin/java"), \
     mock.patch.object(tb, "_find_jar", return_value=Path("/tmp/x.jar")), \
     mock.patch.object(tb.shutil, "which", return_value=None), \
     mock.patch.object(tb.subprocess, "Popen", return_value=_fake), \
     mock.patch.object(tb.proc_env, "child_env", return_value={}), \
     mock.patch.object(tb, "_bundled_jre_dir", return_value=None):
    tv2 = tb.TriggeventBridge()
    tv2.start()
    _t0 = time.monotonic()
    while tv2.is_active() and time.monotonic() - _t0 < 3:
        time.sleep(0.01)
    check("the reader reaches its EOF exit on a real boot shape", not tv2.is_active())
    tv2.stop()
check("stop after a spontaneous exit is a clean no-op", tv2._proc is None)

print()
if FAILS:
    print(f"{len(FAILS)} FAILED: {', '.join(FAILS)}")
    sys.exit(1)
print("all tests passed")
