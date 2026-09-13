"""Load mixed case pack extensions with the vendored host."""

import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import unittest


CORE = Path(__file__).resolve().parents[1] / "triggernometry-core"


@unittest.skipUnless(os.name == "posix" and shutil.which("mono") and shutil.which("xvfb-run"),
                     "Requires Mono and Xvfb")
class TriggernometryHostTests(unittest.TestCase):
    def test_mixed_case_extensions_load_the_same_inventory(self):
        source = (CORE / "test" / "packs" / "spike-me-pack.xml").read_bytes()
        inventories = []
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for suffix in (".xml", ".XML", ".Xml"):
                with self.subTest(suffix=suffix):
                    pack = root / ("pack" + suffix)
                    pack.write_bytes(source)
                    proc = subprocess.Popen(
                        ["xvfb-run", "-a", "mono", str(CORE / "bin" / "triggernometry-core.exe"),
                         str(root / ("config" + suffix)), "--serve", str(pack)],
                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        text=True, start_new_session=True)
                    try:
                        out, err = proc.communicate("", timeout=25)
                    finally:
                        if proc.poll() is None:
                            os.killpg(proc.pid, signal.SIGTERM)
                            try:
                                proc.communicate(timeout=3)
                            except subprocess.TimeoutExpired:
                                os.killpg(proc.pid, signal.SIGKILL)
                                proc.communicate()
                    self.assertEqual(proc.returncode, 0, err)
                    frames = [json.loads(line) for line in out.splitlines() if line.startswith("{")]
                    inventory = next(frame["triggers"] for frame in frames if frame.get("t") == "inventory")
                    self.assertTrue(inventory)
                    inventories.append(inventory)
        self.assertEqual(inventories[0], inventories[1])
        self.assertEqual(inventories[0], inventories[2])


if __name__ == "__main__":
    unittest.main()
