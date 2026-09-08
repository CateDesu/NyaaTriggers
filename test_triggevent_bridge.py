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

Also covers the sidecar generation gate. start and stop bump a generation id
that reader threads carry in their args like proc and wq, every UI bound
emit is stamped with it and dropped at dispatch once the generation dies,
and the UI slots re-check the token since Qt queued delivery can land a pre
restart signal after the restart. The stderr chain watch is gated the same
way: a never started or stopped bridge does not emit, and neither does a
previous generation's still draining pipe.

Run directly:  python test_triggevent_bridge.py   (exit 0 = all pass)
"""
import io
import os
import queue
import signal
import subprocess
import sys
import threading
import time
import unittest.mock as mock
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
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


# ── the stderr chain watch is gated to the live sidecar generation ────────────
# The chain failure line only ever appears on the sidecar stderr stream, never
# in the callout stream. The watch must log_drop every line but fire
# chain_failure only for the live generation. Lines buffered in a dead proc's
# pipe, the JVM teardown flush during stop, and a previous generation's still
# draining stderr after a restart must not reach the badge.
drops = []
_o_drop = tb.log_drop
tb.log_drop = lambda site, detail, *a, **k: drops.append((site, detail))
try:
    tv3 = tb.TriggeventBridge()
    seen = []
    tv3.chain_failure.connect(lambda line, gen: seen.append((line, gen)))

    class _ErrProc:
        def __init__(self):
            self.stderr = io.StringIO(
                "some boot line\n"
                "18:00:00.000 [SequentialTrigger-3] ERROR gg.xp.SequentialTriggerController - "
                "Error in sequential trigger 'DMU.ttSq' while waiting for 'BuffApplied'\n")

    class _QuietProc:
        def __init__(self):
            self.stderr = io.StringIO("some boot line\nreading WS messages on stdin\n")

    # a never started bridge has no live generation, the drop is still logged
    # but the signal must not fire
    tv3._err_loop(_ErrProc(), 0)
    check("an Error in sequential trigger line logs an engine-chain drop",
          any(site == "engine-chain" and "DMU.ttSq" in d for site, d in drops))
    check("a never started bridge does not fire chain_failure", seen == [])

    # the live generation fires, stamped with its generation
    tv3._active = True
    tv3._gen = 1
    tv3._err_loop(_ErrProc(), 1)
    check("the live generation fires chain_failure with its generation",
          len(seen) == 1 and "DMU.ttSq" in seen[0][0] and seen[0][1] == 1)

    # a restart bumped the generation, the old reader's buffered stderr dies
    tv3._gen = 2
    tv3._err_loop(_ErrProc(), 1)
    check("a previous generation's stderr does not fire after a restart",
          len(seen) == 1)

    # stop left no live generation, the JVM teardown flush must not fire
    tv3._gen = 3
    tv3._active = False
    tv3._err_loop(_ErrProc(), 2)
    check("a stopped bridge does not fire chain_failure", len(seen) == 1)

    drops.clear()
    ready = []
    tv3.ready.connect(lambda: ready.append(True))
    tv3._active = True
    tv3._gen = 4
    tv3._err_loop(_QuietProc(), 4)
    check("benign stderr lines raise no engine-chain drop", drops == [])
    check("the live generation still fires ready", ready == [True])
    tv3._gen = 5
    tv3._err_loop(_QuietProc(), 4)
    check("a stale generation does not fire ready", ready == [True])
finally:
    tb.log_drop = _o_drop


# ── a previous generation's reader cannot fire into the live session ──────────
# stop+start swaps _proc and bumps the generation while the old reader is
# still draining its buffered stdout. The old reader's callouts must die at
# dispatch, and the generation token riding each emit must fail the UI slot's
# re-check when queued delivery lands after the restart.
fired = []
tv4 = tb.TriggeventBridge()
tv4.callout.connect(lambda text, sev, gen: fired.append(("callout", text, gen)))
tv4.tts.connect(lambda text, gen: fired.append(("tts", text, gen)))
tv4.status.connect(lambda active, msg, gen: fired.append(("status", msg, gen)))


class _OldProc:
    def __init__(self, stdout_text):
        self.pid = 4871
        self.stdin = None
        self.stdout = io.StringIO(stdout_text)
        self.stderr = io.StringIO("")

    def poll(self):
        return 0

    def wait(self, timeout=None):
        return 0


_CALLOUT_LINE = '{"t":"callout","seq":1,"tts":"old gen","text":"old gen","severity":"alert"}\n'

# generation 1 was live, then stop+start installed the replacement and bumped
# the generation past it while the old reader still held a buffered callout
old_proc = _OldProc(_CALLOUT_LINE)
tv4._active = True
tv4._proc = _OldProc("")        # the replacement generation's proc
tv4._gen = 3                    # stop bumped 1 -> 2, start bumped 2 -> 3
tv4._read_loop(old_proc, queue.Queue(), {"last": None}, 1)
check("a buffered callout from the old generation is not emitted", fired == [])

# the live generation's reader fires, stamped with its generation
live_proc = _OldProc(_CALLOUT_LINE)
tv4._proc = live_proc
tv4._read_loop(live_proc, queue.Queue(), {"last": None}, 3)
check("the live generation's callout is emitted with its generation",
      ("callout", "old gen", 3) in fired and ("tts", "old gen", 3) in fired)
check("the live generation's exit status is emitted with its generation",
      ("status", "Sidecar exited", 3) in fired)

# the slot half, a stale emit that slipped out before the restart must be
# dropped when queued delivery lands after it
from ui.engines import EnginesMixin


class _CalloutHost:
    _on_triggevent_callout = EnginesMixin._on_triggevent_callout

    def __init__(self, bridge):
        self._triggevent = bridge
        self._triggevent_mode = True
        self.shown = []

    def _localize_text(self, text):
        return text

    def _emit_alert(self, text, severity):
        self.shown.append((text, severity))


tv5 = tb.TriggeventBridge()
tv5._gen = 7
host = _CalloutHost(tv5)
host._on_triggevent_callout("pre restart", "info", 3)
check("the UI slot drops a stale generation's queued callout", host.shown == [])
host._on_triggevent_callout("live", "alert", 7)
check("the UI slot accepts the live generation's callout",
      host.shown == [("live", "alert")])
host._on_triggevent_callout("internal", "info")
check("a direct internal call without a token still lands",
      host.shown == [("live", "alert"), ("internal", "info")])

print()
if FAILS:
    print(f"{len(FAILS)} FAILED: {', '.join(FAILS)}")
    sys.exit(1)
print("all tests passed")
