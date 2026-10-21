import os
import signal
from pathlib import Path
import subprocess
import tempfile

CORE = Path(__file__).resolve().parent


def main():
    jar = CORE / "target/triggevent-core.jar"
    with tempfile.TemporaryDirectory(prefix="nyaa-delay-test-") as temp:
        subprocess.run(["javac", "-cp", str(jar), "-d", temp,
                        str(CORE / "src/test/java/gg/xp/nyaa/DelayedSpeechVerification.java"),
                        str(CORE / "src/test/java/gg/xp/nyaa/DisplayTextVerification.java")], check=True, timeout=60)
        command = ["java", f"-Duser.home={temp}", "-cp", os.pathsep.join([temp, str(jar)]),
                   "gg.xp.nyaa.DelayedSpeechVerification"]
        if os.name != "nt":
            command = ["xvfb-run", "-a", *command]
        environment = os.environ.copy()
        environment["NYAA_AUTOMARK"] = "0"
        with subprocess.Popen(command, cwd=temp, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              text=True, env=environment, start_new_session=os.name != "nt") as process:
            try:
                output, _ = process.communicate(timeout=60)
            except BaseException:
                if os.name == "nt":
                    process.kill()
                else:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                process.communicate(timeout=5)
                raise
            if process.returncode or "RESULT PASS" not in output:
                print(output)
                raise SystemExit("FAIL native delayed speech")
        for line in output.splitlines():
            if line.startswith("VERIFIED "):
                print(line.replace("VERIFIED ", "PASS ", 1))


if __name__ == "__main__":
    main()
