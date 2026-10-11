import os
from pathlib import Path
import shutil
import sys
import tempfile
from unittest.mock import patch

TEST_DIR = Path(__file__).resolve().parent
CORE = TEST_DIR.parent
sys.path.insert(0, str(CORE.parent))

from PyQt6.QtCore import QCoreApplication, QTimer
from nyaatriggers import triggernometry_bridge as tb

EXPECTED = "my x is 55.5 hp 42000"


def main():
    app = QCoreApplication.instance() or QCoreApplication([])
    with tempfile.TemporaryDirectory(prefix="nyaa-tn-smoke-") as temporary:
        root = Path(temporary)
        packs = root / "packs"
        packs.mkdir()
        shutil.copyfile(TEST_DIR / "packs" / "spike-me-pack.xml", packs / "player.xml")
        with patch.dict(os.environ, {
                "NYAA_TRIGGERNOMETRY_PACKS": str(packs),
                "NYAA_TRIGGERNOMETRY_EXE": str(CORE / "bin" / "triggernometry-core.exe"),
                "XDG_CONFIG_HOME": str(root), "APPDATA": str(root)}):
            bridge = tb.TriggernometryBridge()
            speech = []

            def feed(_inventory, _generation):
                bridge.feed_combatants({"me": 268443700, "list": [
                    {"id": 268443700, "name": "BridgePlayer", "job": 24,
                     "hp": 42000, "maxhp": 50000, "x": 55.5, "y": 2.0,
                     "z": 0.0, "h": 1.0, "party": 1}]})
                bridge.feed_log("00|2026-06-27T12:00:00.0000000+00:00|0839|S|SPIKEME via bridge|x")

            def spoken(text, _generation):
                print("TTS:", text)
                speech.append(text)
                if text == EXPECTED:
                    app.quit()

            bridge.inventory.connect(feed)
            bridge.tts.connect(spoken)
            bridge.status.connect(lambda active, message, _generation:
                                  print("STATUS:", active, message))
            timeout = QTimer()
            timeout.setSingleShot(True)
            timeout.timeout.connect(app.quit)
            try:
                timeout.start(10000)
                bridge.start()
                app.exec()
            finally:
                timeout.stop()
                bridge.stop(wait=True)
            success = EXPECTED in speech
            print("RESULT:", "PASS" if success else "FAIL", speech)
            return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
