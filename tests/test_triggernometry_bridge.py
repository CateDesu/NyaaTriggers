"""Regression tests for triggernometry_bridge.py.

Covers the sidecar generation gate. start and stop bump a generation id that
the reader thread carries in its args like proc and wq. Every UI bound emit
is stamped with it and dropped at dispatch once the generation dies, so a
previous generation's reader still draining its buffered stdout after
stop+start swapped _proc cannot fire callouts, sounds or status into the
live session. The token also rides each emitted payload and the UI slots
re-check it, Qt queued delivery can land a pre restart signal at the slot
after the restart.

Run directly:  python -m tests.test_triggernometry_bridge   (exit 0 = all pass)
"""
import io
import os
import queue
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nyaatriggers import triggernometry_bridge as tb

FAILS = []


def check(name, cond):
    print(("PASS  " if cond else "FAIL  ") + name)
    if not cond:
        FAILS.append(name)


class _OldProc:
    def __init__(self, stdout_text):
        self.pid = 5117
        self.stdin = None
        self.stdout = io.StringIO(stdout_text)
        self.stderr = io.StringIO("")

    def poll(self):
        return 0

    def wait(self, timeout=None):
        return 0


# ── start and stop bump the generation ────────────────────────────────────────
br = tb.TriggernometryBridge()
g0 = br.generation()
br._active = True          # a "running" bridge with no proc, stop must clean up
br.stop()
check("stop retires the generation", br.generation() == g0 + 1)
check("a stopped bridge has no live generation",
      not br._gen_live(g0) and not br._gen_live(br.generation()))


# ── a previous generation's reader cannot fire into the live session ──────────
# stop+start swaps _proc and bumps the generation while the old reader is
# still draining its buffered stdout. Every frame it held must die at
# dispatch.
_BUF = (
    '{"t":"callout","tts":"old gen","text":"old gen","severity":"alarm"}\n'
    '{"t":"sound","file":"/tmp/nyaa-tn-gentest.wav","volume":42}\n'
    '{"t":"status","active":true,"msg":"ready"}\n'
)

fired = []
br2 = tb.TriggernometryBridge()
br2.callout.connect(lambda text, sev, gen: fired.append(("callout", text, gen)))
br2.tts.connect(lambda text, gen: fired.append(("tts", text, gen)))
br2.sound.connect(lambda f, vol, gen: fired.append(("sound", f, vol, gen)))
br2.status.connect(lambda active, msg, gen: fired.append(("status", msg, gen)))

old_proc = _OldProc(_BUF)
br2._active = True
br2._proc = _OldProc("")       # the replacement generation's proc
br2._gen = 3                   # stop bumped 1 -> 2, start bumped 2 -> 3
br2._read_loop(old_proc, queue.Queue(), 1)
check("a buffered callout from the old generation is not emitted", fired == [])

# the live generation's reader fires every frame, stamped with its generation
live_proc = _OldProc(_BUF)
br2._proc = live_proc
br2._read_loop(live_proc, queue.Queue(), 3)
check("the live generation's callout is emitted with its generation",
      ("callout", "old gen", 3) in fired and ("tts", "old gen", 3) in fired)
check("the live generation's sound is emitted with its generation",
      ("sound", "/tmp/nyaa-tn-gentest.wav", 42, 3) in fired)
check("the live generation's status is emitted with its generation",
      ("status", "ready", 3) in fired)
check("the live generation's exit status is emitted with its generation",
      ("status", "Sidecar exited", 3) in fired)


# ── the UI slots re-check the generation token riding the payload ─────────────
# A stale emit that slipped out before the restart must be dropped when queued
# delivery lands after it. Drives the real slots unbound on duck windows, the
# test_umad_chain_wiring.py idiom.
from nyaatriggers.ui.engines import EnginesMixin
import nyaatriggers.ui.voice_tab as vt


class _CalloutHost:
    _on_triggernometry_callout = EnginesMixin._on_triggernometry_callout

    def __init__(self, bridge):
        self._triggernometry = bridge
        self._triggernometry_mode = True
        self.shown = []

    def _localize_text(self, text):
        return text

    def _emit_alert(self, text, severity):
        self.shown.append((text, severity))


class _TtsHost:
    _on_triggernometry_tts = vt.VoiceTabMixin._on_triggernometry_tts
    _on_triggernometry_sound = vt.VoiceTabMixin._on_triggernometry_sound

    def __init__(self, bridge):
        self._triggernometry = bridge
        self._triggernometry_mode = True
        self.spoken = []

    def _triggernometry_speak(self, text):
        self.spoken.append(text)


br3 = tb.TriggernometryBridge()
br3._gen = 7
host = _CalloutHost(br3)
host._on_triggernometry_callout("pre restart", "info", 3)
check("the callout slot drops a stale generation's queued callout",
      host.shown == [])
host._on_triggernometry_callout("live", "alert", 7)
check("the callout slot accepts the live generation's callout",
      host.shown == [("live", "alert")])
host._on_triggernometry_callout("internal", "info")
check("a direct internal call without a token still lands",
      host.shown == [("live", "alert"), ("internal", "info")])

thost = _TtsHost(br3)
thost._on_triggernometry_tts("pre restart", 3)
check("the tts slot drops a stale generation's queued tts", thost.spoken == [])
thost._on_triggernometry_tts("live", 7)
check("the tts slot accepts the live generation's tts", thost.spoken == ["live"])

played = []
_o_play = vt.play_sound
vt.play_sound = lambda f, v=1.0: played.append((f, v))
try:
    thost._on_triggernometry_sound(__file__, 50, 3)
    check("the sound slot drops a stale generation's queued sound", played == [])
    thost._on_triggernometry_sound(__file__, 50, 7)
    check("the sound slot accepts the live generation's sound",
          played == [(__file__, 0.5)])
finally:
    vt.play_sound = _o_play

br4 = tb.TriggernometryBridge()
br4._active = True
br4._gen = 9
br4._feed_endpoint('{"notificationid":"partysynergy"}', 8)
check("a retired Telesto callback cannot enter the new engine", br4._wq.empty())
br4._feed_endpoint('{"notificationid":"partysynergy"}', 9)
check("a current Telesto callback reaches the endpoint source",
      '"t": "endpoint"' in br4._wq.get_nowait())


class _Relay:
    def __init__(self):
        self.closed = []

    def close(self, wait=False):
        self.closed.append(wait)

    def is_finished(self):
        return False


retired = _Relay()
br4._telesto = retired
br4.stop()
br4.stop(wait=True)
check("shutdown waits for cleanup from an earlier stop", retired.closed == [False, True])

print()
if FAILS:
    print(f"{len(FAILS)} FAILED: {', '.join(FAILS)}")
    sys.exit(1)
print("all tests passed")
