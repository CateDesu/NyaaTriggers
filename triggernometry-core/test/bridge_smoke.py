import os, sys
TEST_DIR = os.path.dirname(os.path.abspath(__file__))      # .../triggernometry-core/test
CORE = os.path.dirname(TEST_DIR)                            # .../triggernometry-core
NYAA = os.path.dirname(CORE)                                # .../NyaaTriggers
os.environ["NYAA_TRIGGERNOMETRY_PACKS"] = os.path.join(TEST_DIR, "packs")
os.environ["NYAA_TRIGGERNOMETRY_EXE"] = os.path.join(CORE, "bin", "triggernometry-core.exe")
sys.path.insert(0, NYAA)
from PyQt6.QtCore import QCoreApplication, QTimer
import triggernometry_bridge as tb

print("is_available:", tb.is_available(), "| exe:", bool(tb._find_exe()), "| mono:", tb._find_mono(), "| packs:", len(tb._find_packs()))
app = QCoreApplication([])
br = tb.TriggernometryBridge()
got = {"n": 0}
br.tts.connect(lambda s: (print("TTS:", s), got.__setitem__("n", got["n"]+1)))
br.callout.connect(lambda t, s: print("CALLOUT:", repr(t), s))
br.status.connect(lambda a, m: print("STATUS:", a, m))
br.start()

def feed():
    print(">> feeding combatants + log")
    br.feed_combatants({"me": 268443700, "list": [
        {"id": 268443700, "name": "BridgePlayer", "job": 24, "hp": 42000, "maxhp": 50000,
         "x": 55.5, "y": 2.0, "z": 0.0, "h": 1.0, "party": 1}]})
    br.feed_log("00|2026-06-27T12:00:00.0000000+00:00|0839|S|SPIKEME via bridge|x")

QTimer.singleShot(4000, feed)
QTimer.singleShot(10000, lambda: (br.stop(), QTimer.singleShot(800, app.quit)))
app.exec()
print("RESULT callouts:", got["n"], "->", "PASS" if got["n"] > 0 else "FAIL")
