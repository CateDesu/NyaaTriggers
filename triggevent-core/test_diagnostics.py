import os
from pathlib import Path
import subprocess
import tempfile

CORE = Path(__file__).resolve().parent


def main():
    jar = CORE / "target/triggevent-core.jar"
    with tempfile.TemporaryDirectory(prefix="nyaa-diagnostic-test-") as temp:
        subprocess.run(["javac", "-cp", str(jar), "-d", temp,
                        str(CORE / "src/test/java/gg/xp/nyaa/DiagnosticsVerification.java")], check=True)
        command = ["java", f"-Duser.home={temp}", "-cp", os.pathsep.join([temp, str(jar)]),
                   "gg.xp.nyaa.DiagnosticsVerification"]
        if os.name != "nt":
            command = ["xvfb-run", "-a", *command]
        result = subprocess.run(command, cwd=temp, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, timeout=60)
        if result.returncode or "RESULT PASS" not in result.stdout:
            print(result.stdout)
            raise SystemExit("FAIL engine diagnostics")
        for line in result.stdout.splitlines():
            if line.startswith("VERIFIED "):
                print(line.replace("VERIFIED ", "PASS ", 1))


if __name__ == "__main__":
    main()
