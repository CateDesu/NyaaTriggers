"""Exercise slow history loading through the real bridge and stdin protocol."""

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PyQt6.QtCore import QTimer
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication
from nyaatriggers.triggevent_bridge import TriggeventBridge
from nyaatriggers.triggevent_recovery import TriggeventRecovery
from nyaatriggers.ws_client import WSClient


def main():
    app = QApplication.instance() or QApplication([])
    diagnostics, progress = [], []
    with tempfile.TemporaryDirectory(prefix="nyaa-protocol-test-") as temp:
        stamp = datetime.now(timezone.utc) - timedelta(seconds=30)
        anchor = f"00|{(stamp + timedelta(seconds=3)).isoformat()}|0038|Player|Anchor|0"
        history = [
            f"01|{stamp.isoformat()}|553|Raid|0",
            f"03|{stamp.isoformat()}|10000001|Player|15|64|0|0|World|0|0|10000|10000|10000|10000|0|0|100|100|0|0|0",
            "00|invalid|0038|Player|Damaged line|0",
            f"00|{(stamp + timedelta(seconds=2)).isoformat()}|0038|Player|Later valid line|0",
        ]
        path = Path(temp) / "Network_test.log"
        path.write_text("\n".join(history) + "\n")
        bridge = TriggeventBridge()
        ws = WSClient()
        recovery = TriggeventRecovery(bridge, ws, lambda: temp)
        bridge.recovery_progress.connect(lambda message, generation: progress.append(message))
        backlog = json.dumps({"type": "LogLine", "rawLine":
                             f"00|{(stamp + timedelta(seconds=4)).isoformat()}|0038|Player|During restore|0"})

        def while_loading(generation):
            recovery.feed(backlog)
            assert not recovery._live
            QTimer.singleShot(750, lambda: path.write_text("\n".join(history + [anchor]) + "\n"))

        bridge.ready.connect(while_loading)
        with patch.dict(os.environ, {"NYAA_AUTOMARK": "0"}), \
                patch("nyaatriggers.triggevent_bridge._log", side_effect=diagnostics.append), \
                patch("nyaatriggers.triggevent_recovery._log", side_effect=diagnostics.append), \
                patch.object(bridge, "catch_up", wraps=bridge.catch_up) as catch_up:
            try:
                bridge.start()
                ws._on_message(json.dumps({"type": "ChangeZone", "zoneID": 0x553}))
                ws._on_message(json.dumps({"type": "ChangePrimaryPlayer", "charID": 0x10000001}))
                ws._on_message(json.dumps({"type": "LogLine", "rawLine": anchor}))
                for _ in range(1200):
                    if recovery._live:
                        break
                    QTest.qWait(25)
                assert recovery._live, "Recovery did not acknowledge the live handoff"
                assert any(backlog in call.args[0] for call in catch_up.call_args_list), "Input bypassed catch-up"
                assert [message["t"] for message in progress] == ["recovery_checkpoint", "recovery_checkpoint", "recovered"], progress
                assert [message["checkpoint"] for message in progress] == [1, 2, 3], progress
                assert progress[-1]["status"] == "degraded", progress
                assert progress[-1]["skipped"] == 1, progress
                assert any("restored 3 historical events, skipped 1" in line for line in diagnostics), diagnostics
                assert not any("command error:" in line or "feed error:" in line for line in diagnostics), diagnostics
            except BaseException:
                print("\n".join(diagnostics))
                raise
            finally:
                bridge.stop(wait=True)
    print("PASS real bridge catch-up during slow history loading and degraded history acknowledgement")


if __name__ == "__main__":
    main()
