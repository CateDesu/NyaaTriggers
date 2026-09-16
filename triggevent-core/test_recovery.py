"""Exercise the built engine's recovery clock and recorded pull handoff."""

import argparse
import os
from pathlib import Path
import subprocess
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--recording", type=Path)
    parser.add_argument("--cut", type=int, default=10000)
    parser.add_argument("--variant", type=Path)
    parser.add_argument("--expect", action="append", default=[])
    args = parser.parse_args()
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
                count = output.count("RECOVERED_CALL ")
                print(f"PASS {name}: {count} subsequent calls, no chain failures or historical output")
            else:
                print(f"PASS {name}")


if __name__ == "__main__":
    main()
