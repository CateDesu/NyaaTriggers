import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest

import websockets
from websockets.sync.server import serve


REPO = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.name == "posix" and shutil.which("bash") and shutil.which("dirname"),
                     "Linux setup requires Bash and dirname")
class SourceSetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.program = self.root / "program with spaces"
        self.program.mkdir()
        shutil.copy2(REPO / "setup.sh", self.program / "setup.sh")
        shutil.copy2(REPO / "requirements.txt", self.program / "requirements.txt")
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.calls = self.root / "calls.jsonl"
        self.env = {**os.environ, "PATH": str(self.bin), "SETUP_CALLS": str(self.calls),
                    "SETUP_PYTHON": sys.executable, "SETUP_REPO": str(REPO),
                    "SETUP_WEBSOCKETS": str(Path(websockets.__file__).parent),
                    "NYAA_REPLAY_TEST": "1"}
        self.env.pop("PYTHONPATH", None)
        self.env.pop("PYTHONHOME", None)
        self.executable("id", "#!/bin/sh\necho 1000\n")
        (self.bin / "dirname").symlink_to(shutil.which("dirname"))
        self.executable("sudo", "#!/bin/sh\nexec \"$@\"\n")
        self.executable("python3", f"#!{sys.executable}\n" + '''import json, os, pathlib, subprocess, sys
with open(os.environ["SETUP_CALLS"], "a") as log:
    log.write(json.dumps(["python3", *sys.argv[1:]]) + "\\n")
if sys.argv[1:3] == ["-m", "venv"]:
    subprocess.run([os.environ["SETUP_PYTHON"], "-m", "venv", "--without-pip", *sys.argv[3:]], check=True)
    directory = pathlib.Path(sys.argv[-1])
    packages = next(directory.glob("lib/python*/site-packages"))
    # Package installation is simulated. The imported WebSocket implementation is real.
    (packages / "pip.py").write_text("""import json, os, pathlib, sys
with open(os.environ['SETUP_CALLS'], 'a') as log:
    log.write(json.dumps(['pip', *sys.argv[1:]]) + '\\\\n')
if os.environ.get('SETUP_PIP_FAIL'):
    raise SystemExit(1)
assert sys.argv[1:] == ['install', '-r', 'requirements.txt'], sys.argv
dependency = pathlib.Path(__file__).with_name('websockets')
if not dependency.exists():
    dependency.symlink_to(os.environ['SETUP_WEBSOCKETS'], target_is_directory=True)
""")
else:
    # Simulate the system package's missing sync API without changing installed dependencies.
    env = {**os.environ, "PYTHONPATH": os.environ["SETUP_OLD_MODULES"]}
    raise SystemExit(subprocess.run([os.environ["SETUP_PYTHON"], *sys.argv[1:]], env=env).returncode)
''')
        old = self.root / "old modules" / "websockets"
        old.mkdir(parents=True)
        (old / "__init__.py").write_text('__version__ = "10.4"\n')
        self.env["SETUP_OLD_MODULES"] = str(old.parent)

    def executable(self, name, text):
        path = self.bin / name
        marker = f"#!{sys.executable}\n"
        if text.startswith(marker):
            script = path.with_suffix(".py")
            script.write_text(text[len(marker):])
            text = "#!/bin/sh\nexec " + shlex.quote(sys.executable) + " " + shlex.quote(str(script)) + ' "$@"\n'
        path.write_text(text)
        path.chmod(0o755)

    def manager(self, name):
        self.executable(name, f"#!{sys.executable}\n" + '''import json, os, pathlib, sys
with open(os.environ["SETUP_CALLS"], "a") as log:
    log.write(json.dumps([pathlib.Path(sys.argv[0]).stem, *sys.argv[1:]]) + "\\n")
''')

    def run_setup(self):
        result = subprocess.run([shutil.which("bash"), str(self.program / "setup.sh")],
                                cwd=self.root, env=self.env, text=True, capture_output=True,
                                timeout=30)
        calls = [json.loads(line) for line in self.calls.read_text().splitlines()] if self.calls.exists() else []
        return result, calls

    def test_apt_setup_launches_with_the_compatible_overlay_dependency(self):
        self.manager("apt")

        def greeting(connection):
            connection.send(json.dumps({"protocol": 1, "plugin": "0.2.0"}))
            connection.recv()

        with serve(greeting, "127.0.0.1", 0) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.env["SETUP_OVERLAY_PORT"] = str(server.socket.getsockname()[1])
            self.program.joinpath("main.py").write_text('''import json, os, sys
sys.path.insert(0, os.environ["SETUP_REPO"])
from nyaatriggers.plugin_link import PluginLink, _HAVE_WS
assert _HAVE_WS, "the documented interpreter lacks websockets.sync"
connection, version, retention = PluginLink(port=int(os.environ["SETUP_OVERLAY_PORT"]))._connect()
connection.send("done")
connection.close()
print(json.dumps({"prefix": sys.prefix, "version": version}))
''')
            result, calls = self.run_setup()
            self.assertEqual(result.returncode, 0, result.stderr)
            command = re.search(r"Run the (?:app|program) with:\s+([^\n]+)", result.stdout)
            self.assertIsNotNone(command, result.stdout)
            launched = subprocess.run(shlex.split(command[1]), cwd=self.program, env=self.env,
                                      text=True, capture_output=True, timeout=10)
            self.assertEqual(launched.returncode, 0, launched.stderr)
            output = json.loads(launched.stdout)
            self.assertEqual(Path(output["prefix"]), self.program / ".venv")
            self.assertEqual(output["version"], "0.2.0")
            self.assertIn(["pip", "install", "-r", "requirements.txt"], calls)
            self.assertTrue(any(call[0] == "apt" and "python3-venv" in call for call in calls))
            server.shutdown()
            thread.join(5)
            self.assertFalse(thread.is_alive())

    def test_apt_dependency_failure_does_not_advertise_a_completed_setup(self):
        self.manager("apt")
        self.env["SETUP_PIP_FAIL"] = "1"
        result, calls = self.run_setup()
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertNotRegex(result.stdout, r"Run the (?:app|program) with:")
        self.env.pop("SETUP_PIP_FAIL")
        result, calls = self.run_setup()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(".venv/bin/python main.py", result.stdout)
        repaired = subprocess.run([str(self.program / ".venv/bin/python"), "-c",
                                   "from websockets.sync.client import connect; import sys; print(sys.prefix)"],
                                  env=self.env, text=True, capture_output=True, timeout=10)
        self.assertEqual(repaired.returncode, 0, repaired.stderr)
        self.assertEqual(Path(repaired.stdout.strip()), self.program / ".venv")

    def test_pacman_keeps_the_system_interpreter(self):
        self.manager("pacman")
        result, calls = self.run_setup()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("python3 main.py", result.stdout)
        self.assertEqual([call[0] for call in calls], ["pacman"])
        self.assertFalse(self.program.joinpath(".venv").exists())


if __name__ == "__main__":
    unittest.main()
