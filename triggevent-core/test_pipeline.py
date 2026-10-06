"""Replay UMAD through the native engine, Qt speech delivery and Linux audio process."""

import argparse
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("NYAA_REPLAY_TEST", "1")
CORE = Path(__file__).resolve().parent
sys.path.insert(0, str(CORE.parent))

from PyQt6.QtCore import QObject
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication

from nyaatriggers import triggevent_bridge, tts
from nyaatriggers.ui.engines import EnginesMixin
from nyaatriggers.ui.voice_tab import VoiceTabMixin
from nyaatriggers.ws_client import WSClient


class CalloutHost(QObject, EnginesMixin, VoiceTabMixin):
    def __init__(self, bridge):
        super().__init__()
        self._triggevent = bridge
        self._connected = self._triggevent_mode = True
        self.shown = []
        bridge.callout.connect(self._on_triggevent_callout)
        bridge.tts.connect(self._on_triggevent_tts)

    def _localize_text(self, text):
        return text

    def _reading_for(self, text):
        return None

    def _emit_alert(self, text, severity):
        self.shown.append(text)


class TestVoice:
    def synthesize_wav(self, text, output, **kwargs):
        output.setnchannels(1)
        output.setsampwidth(1)
        output.setframerate(8000)
        output.writeframes(text.encode("utf-8"))


def wait_for(predicate, message, seconds=30):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        QTest.qWait(10)
    raise AssertionError(message)


def replay(recording, expected, temp, *, drop=None, removed=0):
    bridge = triggevent_bridge.TriggeventBridge()
    ws = WSClient()
    host = CalloutHost(bridge)
    ws.engine_message.connect(bridge.feed)
    spoken, progress, failures, diagnostics = [], [], [], []
    bridge.tts.connect(lambda text, gen: spoken.append(text))
    bridge.recovery_progress.connect(lambda message, gen: progress.append(message))
    bridge.chain_failure.connect(lambda message, gen: failures.append(message))
    captured = temp / (recording.stem + "-audio.jsonl")
    captured.unlink(missing_ok=True)
    popen = subprocess.Popen

    def audio_process(command, **kwargs):
        if command[0] != "aplay":
            return popen(command, **kwargs)
        return popen([sys.executable, "-c",
                      "import json,sys,wave; "
                      "w=wave.open(sys.argv[1]); text=w.readframes(w.getnframes()).decode('utf-8'); "
                      "open(sys.argv[2],'a',encoding='utf-8').write(json.dumps(text)+'\\n')",
                      command[-1], str(captured)], **kwargs)

    lines = [line for line in recording.read_text(encoding="utf-8").splitlines() if line.strip()]
    if drop is not None:
        filtered = [line for line in lines if not drop(line.split("|"))]
        if len(lines) - len(filtered) != removed:
            raise AssertionError("Fault injection did not remove the intended mechanic inputs")
        lines = filtered
    original = datetime.fromisoformat(lines[0].split("|", 2)[1])
    start = datetime.now(timezone.utc) + timedelta(seconds=120)
    shift = start - original
    frames = []
    for line in lines:
        kind, stamp, fields = line.split("|", 2)
        shifted = datetime.fromisoformat(stamp) + shift
        frames.append(json.dumps({"type": "LogLine",
                                  "rawLine": f"{kind}|{shifted.isoformat()}|{fields}"}))
    cut = next(index for index, line in enumerate(lines) if line.startswith("20|"))
    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, {
            "JAVA_TOOL_OPTIONS": f"-Duser.home={temp}", "NYAA_AUTOMARK": "0"}))
        stack.enter_context(patch.object(triggevent_bridge, "_log", side_effect=diagnostics.append))
        stack.enter_context(patch.object(tts.subprocess, "Popen", side_effect=audio_process))
        stack.enter_context(patch.multiple(tts, _engine="piper", _jp_auto=False,
                                           _jp_neural=False, _piper_voice=TestVoice(),
                                           _piper_failed=False, _master_volume=1.0,
                                           _speech_suspended=False))
        stack.enter_context(patch.object(tts, "log_drop"))
        try:
            bridge.start()
            wait_for(bridge.supports_catchup, "Native engine did not become ready")
            if not bridge.recover(frames[:cut], start.isoformat()):
                raise AssertionError("Native recovery setup was rejected")
            wait_for(lambda: any(m["t"] == "recovered" for m in progress),
                     "Native engine did not acknowledge setup")
            if spoken or host.shown:
                raise AssertionError("Historical state produced a live callout")
            for frame in frames[cut:]:
                ws._on_message(frame)
            if not bridge.catch_up([], 99):
                raise AssertionError("Native replay fence was rejected")
            wait_for(lambda: any(m.get("checkpoint") == 99 for m in progress),
                     "Native engine did not finish the recorded feed")
            if failures:
                raise AssertionError(f"Native sequence failures: {failures}")
            if spoken != expected:
                raise AssertionError(f"{recording.name} speech: {spoken!r}, expected {expected!r}")

            def audio():
                if not captured.exists():
                    return []
                return [json.loads(line) for line in captured.read_text(encoding="utf-8").splitlines()]

            wait_for(lambda: len(audio()) == len(spoken), "Speech did not reach the audio process")
            if not spoken or audio() != spoken:
                raise AssertionError("Audio process lost, changed or reordered native speech")
            wait_for(lambda: tts._queue.empty() and tts._current_proc is None,
                     "Audio process did not finish")
            if not host.shown:
                raise AssertionError("Recorded callouts did not reach the display slot")
            suffix = f", {removed} missing inputs" if drop is not None else ""
            print(f"PASS {recording.name}{suffix}: {len(spoken)} native calls through Qt, synthesis and audio process")
            return spoken
        except BaseException:
            print("\n".join(diagnostics[-40:]))
            raise
        finally:
            host._connected = False
            bridge.stop(wait=True)
            tts.interrupt(wait=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine-source", type=Path, default=CORE / "event-trigger")
    args = parser.parse_args()
    if sys.platform != "linux":
        raise SystemExit("FAIL audio subprocess verification requires Linux")
    if not triggevent_bridge.TriggeventBridge.is_available():
        raise SystemExit("FAIL pipeline verification requires the built native engine and Java")
    app = QApplication.instance() or QApplication([])
    resources = args.engine_source / "triggers/triggers-dt/src/test/resources"
    cases = {
        "dmu-arrows.log": ["Arrows", "Double East", "Confusion Tether",
                           "Spread for Confusion", "Fake Gaze", "Stack In Thunder, Look Towards"],
        "dmu-graven.log": ["Buster on Player 119", "Graven Image", "No Tether",
                           "Spread in Cones", "Line Spread", "Avoid Tower",
                           "Confetti on Player 116, Player 117", "Avoid Both", "Raidwide",
                           "Graven Image", "Avoid Ice, Stone", "Drop Stone",
                           "Buster on Player 120", "Dark", "Avoid Stone and Puddle", "Final Soaks"],
        "dmu-kefka.log": ["Kefka Says", "Out of Cones, In Lines", "Fake Cross", "Real Tsunami",
                          "Fake Short Accel", "Avoid Both", "Real Cross", "Fake Inferno",
                          "Real Water", "Stand in Both", "Real Cross", "Real Black + Allag",
                          "Stand in Black (Southwest)", "Motion and Stack",
                          "Real Thunder, Fake Gaze on YOU", "Stack for Donut", "Raidwide", "Stay",
                          "Stack In Ice (with Player 117, Player 120)", "Real Gaze",
                          "Donut, Fake Ice, Real Thunder", "Stay In Ice"],
    }
    with tempfile.TemporaryDirectory(prefix="nyaa-pipeline-test-") as directory:
        temp = Path(directory)
        for name, expected in cases.items():
            replay(resources / name, expected, temp)
        graven = cases["dmu-graven.log"]
        replay(resources / "dmu-graven.log", graven[:3] + graven[4:], temp,
               drop=lambda f: f[0] == "27" and f[6] == "02A3"
               and f[1] == "2026-09-15T21:09:48.7530000-05:00", removed=1)
        replay(resources / "dmu-graven.log", graven[:3] + graven[4:], temp,
               drop=lambda f: f[0] == "27" and f[6] == "0080"
               and f[1] == "2026-09-15T21:09:48.7530000-05:00", removed=2)
        replay(resources / "dmu-graven.log", graven[:7] + graven[8:], temp,
               drop=lambda f: f[0] == "27" and f[6] == "02A4"
               and f[1] == "2026-09-15T21:10:05.0210000-05:00", removed=1)
        arrows = cases["dmu-arrows.log"]
        replay(resources / "dmu-arrows.log", arrows[:1] + arrows[2:], temp,
               drop=lambda f: (f[0] == "26" and f[2] == "130E"
                               and f[7] == "10031A09" and f[5] == "400055B7")
               or (f[0] == "38" and f[2] == "10031A09" and "130E" in f), removed=12)
        replay(resources / "dmu-arrows.log", arrows[:4] + ["Stack In Thunder"], temp,
               drop=lambda f: f[0] == "273" and f[3] == "019D"
               and f[4] == "40" and f[5] == "80", removed=1)
        kefka = cases["dmu-kefka.log"]
        replay(resources / "dmu-kefka.log", kefka[:1] + kefka[2:], temp,
               drop=lambda f: f[0] == "27" and f[6] == "02A4"
               and f[1] == "2026-09-15T21:21:48.5060000-05:00", removed=1)


if __name__ == "__main__":
    main()
