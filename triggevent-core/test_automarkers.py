import os
from pathlib import Path
import subprocess
import tempfile

CORE = Path(__file__).resolve().parent


def main():
    jar = CORE / "target/triggevent-core.jar"
    with tempfile.TemporaryDirectory(prefix="nyaa-automark-test-") as temp:
        subprocess.run(["javac", "-cp", str(jar), "-d", temp,
                        str(CORE / "src/test/java/gg/xp/nyaa/AutomarkVerification.java")], check=True)
        command = ["java", f"-Duser.home={temp}", "-cp", os.pathsep.join([temp, str(jar)]),
                   "gg.xp.nyaa.AutomarkVerification"]
        if os.name != "nt":
            command = ["xvfb-run", "-a", *command]
        env = os.environ.copy()
        env["NYAA_AUTOMARK"] = "0"
        env["NYAA_TELESTO_URI"] = "http://127.0.0.1:1/"
        result = subprocess.run(command, cwd=temp, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, timeout=90)
        checks = [line for line in result.stdout.splitlines() if line.startswith("VERIFIED ")]
        if result.returncode or "RESULT PASS" not in result.stdout or len(checks) != 12:
            print(result.stdout)
            raise SystemExit("FAIL native automarker pipeline")
        for line in checks:
            print(line.replace("VERIFIED ", "PASS ", 1))


if __name__ == "__main__":
    main()
