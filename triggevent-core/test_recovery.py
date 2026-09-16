"""Exercise the built engine's recovery clock and recorded pull handoff."""

import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile
import sys


def delivered_trace(output):
    shift = next(int(line.split()[1]) for line in output.splitlines() if line.startswith("TIME_SHIFT "))
    messages = [json.loads(line) for line in output.splitlines() if line.startswith('{"t":"callout"')]
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from PyQt6.QtWidgets import QApplication
    from nyaatriggers.triggevent_bridge import TriggeventBridge
    app = QApplication.instance() or QApplication([])
    bridge = TriggeventBridge()
    bridge._active = True
    bridge._gen = 1
    spoken, shown = [], []
    bridge.tts.connect(lambda text, generation: spoken.append(text))
    bridge.callout.connect(lambda text, severity, generation: shown.append(text))
    state = {}
    for index, message in enumerate(messages, 1):
        if message.get("seq") != index:
            raise SystemExit("FAIL delivered callout sequence is incomplete")
        bridge._dispatch(message, state, 1)
    expected_spoken = [(m.get("tts") or "").strip() for m in messages if (m.get("tts") or "").strip()]
    expected_shown = [((m.get("text") or "").strip() or (m.get("tts") or "").strip()) for m in messages]
    if spoken != expected_spoken or shown != [text for text in expected_shown if text]:
        raise SystemExit("FAIL callouts were lost in the Python bridge")
    return [{"id": m.get("id") or "", "tts": m.get("tts") or "", "text": m.get("text") or "",
             "at": m["at"] - shift} for m in messages]


def compare_trace(name, actual, expected):
    if len(actual) != len(expected):
        raise SystemExit(f"FAIL {name}: {len(actual)} delivered calls versus {len(expected)} expected")
    for index, (got, want) in enumerate(zip(actual, expected)):
        if any(got[key] != want[key] for key in ("id", "tts", "text")) or abs(got["at"] - want["at"]) > 100:
            raise SystemExit(f"FAIL {name}: call {index}: {got!r} versus {want!r}")


def internal_trace(raw):
    return [json.loads(line.removeprefix("CALL_TRACE ")) for line in raw.splitlines()
            if line.startswith("CALL_TRACE ")]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--recording", type=Path)
    parser.add_argument("--cut", type=int, default=10000)
    parser.add_argument("--variant", type=Path)
    parser.add_argument("--expect", action="append", default=[])
    parser.add_argument("--history-folder", type=Path)
    parser.add_argument("--zone")
    parser.add_argument("--player")
    parser.add_argument("--compare", action="store_true",
                        help="Compare resolved calls with uninterrupted replay")
    args = parser.parse_args()
    if args.history_folder and not (args.recording and args.zone and args.player):
        parser.error("History verification requires a recording, zone and player")
    core = Path(__file__).resolve().parent
    jar = core / "target/triggevent-core.jar"
    resource = core / "event-trigger/triggers/triggers-dt/src/test/resources"
    cases = [("shared state and timers", None, 0)]
    if args.recording:
        cases.append((args.recording.name, args.recording.resolve(), args.cut))
    else:
        cases.extend((name, resource / name, 10000) for name in ("m1s_anon.log", "m2s_anon.log"))
    with tempfile.TemporaryDirectory(prefix="nyaa-recovery-tests-") as temp:
        subprocess.run(["javac", "-cp", str(jar), "-d", temp,
                        str(core / "src/test/java/gg/xp/nyaa/RecoveryVerification.java")], check=True)
        paths = [temp, str(jar)]
        if args.variant:
            paths.insert(0, str(args.variant.resolve()))
        for name, recording, cut in cases:
            command = ["java", "-cp", os.pathsep.join(paths), "gg.xp.nyaa.RecoveryVerification"]
            if os.name != "nt":
                command = ["xvfb-run", "-a", "-s", "-screen 0 1024x768x24", *command]
            if recording:
                command.extend([str(recording), str(cut)])
                if args.history_folder:
                    command.extend([str(args.history_folder.resolve()), args.zone, args.player])
            result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, timeout=120, cwd=core)
            output = result.stdout
            if result.returncode or "RESULT PASS" not in output:
                print(output)
                raise SystemExit(f"FAIL {name}")
            if recording:
                before, after = output.split("RECOVERY_BOUNDARY", 1)
                if '"t":"callout"' in before:
                    raise SystemExit(f"FAIL {name}: historical callout escaped recovery")
                if '"t":"callout"' not in after:
                    raise SystemExit(f"FAIL {name}: no live callouts after recovery")
                for expected in args.expect:
                    if "RECOVERED_CALL " + expected + "\n" not in output:
                        raise SystemExit(f"FAIL {name}: missing {expected}")
                actual = delivered_trace(output)
                compare_trace(name, actual, internal_trace(output))
                print(f"PASS {name}: {len(actual)} delivered calls, no chain failures or historical output")
                if args.compare:
                    reference_command = list(command)
                    reference_command.insert(reference_command.index("java") + 1, "-Drecovery.reference=true")
                    reference = subprocess.run(reference_command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                               text=True, timeout=120, cwd=core)
                    if reference.returncode or "RESULT PASS" not in reference.stdout:
                        print(reference.stdout)
                        raise SystemExit(f"FAIL {name}: uninterrupted reference")
                    compare_trace(name, actual, internal_trace(reference.stdout))
                    print(f"PASS {name}: delivered IDs, text, order and timing match uninterrupted replay")
            else:
                print(f"PASS {name}")


if __name__ == "__main__":
    main()
