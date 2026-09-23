"""Replay a builder definition through the compiled Triggevent host."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
CORE = Path(__file__).resolve().parent
sys.path.insert(0, str(CORE.parent))
from tests.test_triggevent_custom import example, validation_cases


def main():
    jar = CORE / "target/triggevent-core.jar"
    with tempfile.TemporaryDirectory(prefix="nyaa-custom-test-") as temp:
        definition = example()
        path = Path(temp) / "definition.json"
        path.write_text(json.dumps(definition), encoding="utf-8")
        cases = Path(temp) / "validation.json"
        cases.write_text(json.dumps(validation_cases()), encoding="utf-8")
        subprocess.run(["javac", "-cp", str(jar), "-d", temp,
                        str(CORE / "src/test/java/gg/xp/nyaa/CustomTriggersVerification.java")], check=True)
        command = ["java", f"-Duser.home={temp}", "-cp", os.pathsep.join([temp, str(jar)]),
                   "gg.xp.nyaa.CustomTriggersVerification", str(path), str(cases)]
        if os.name != "nt":
            command = ["xvfb-run", "-a", *command]
        result = subprocess.run(command, cwd=temp, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, timeout=60)
        output = result.stdout
        if result.returncode or "RESULT PASS" not in output:
            print(output)
            raise SystemExit("FAIL custom Triggevent replay")
        messages = [json.loads(line) for line in output.splitlines() if line.startswith('{')]
        calls = [m for m in messages if m["t"] == "callout" and m.get("id") == definition["id"]]
        assert [m["tts"] for m in calls] == [
            "Player, go left", "Player, go right", "Player, changed",
            "Player, go right", "Player, go left", "Player, go left", "Player, go right", "Player, go left"], calls
        acknowledgements = [m for m in messages if m["t"] == "custom_triggers"]
        assert sum(not m["ok"] for m in acknowledgements) == 1, acknowledgements
        assert '"t":"callout"' in output.split("HISTORY_BOUNDARY", 1)[1]
        print("PASS compiled engine branches, current statuses, resets, edits, zone restrictions, timeout and recovery")
        print("PASS callouts retain their saved IDs and invalid replacements preserve working definitions")
        print("PASS Python and Java agree on Unicode, unused fields and timing boundaries")
        print("PASS raw status and cast log lines match IDs with pasted spacing")


if __name__ == "__main__":
    main()
