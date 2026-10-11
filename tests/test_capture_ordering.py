import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PyQt6.QtCore import QCoreApplication

from nyaatriggers import pull_capture
from nyaatriggers.ws_client import WSClient


APP = QCoreApplication.instance() or QCoreApplication([])


class CaptureOrderingTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.ws = WSClient()
        self.capture = pull_capture.PullCapture(
            self.directory, state_snapshot=self.ws.state_snapshot)
        self.addCleanup(self.capture.close)
        self.ws.raw_message.connect(self.capture.on_raw_message)
        self.ws.log_line.connect(self.capture.on_log_line)
        self.ws.zone_changed.connect(self.capture.on_zone_changed)
        self.seed = [
            {"type": "ChangePrimaryPlayer", "charID": 0x10000001, "charName": "Player"},
            {"type": "ChangeZone", "zoneID": 1234, "zoneName": "Duty"},
            self.party("10000001"),
            {"type": "InCombat", "inACTCombat": False, "inGameCombat": False},
        ]
        for frame in self.seed:
            self.send(frame)
        self.capture.set_recording(True)
        self.cast = {"type": "LogLine", "rawLine": "20|ts|40000001|Boss|1234|Cast|"}

    @staticmethod
    def party(actor):
        return {"type": "PartyChanged", "party": [{"id": actor, "inParty": True, "job": 19}]}

    def send(self, frame, now=0):
        with patch.object(pull_capture.time, "monotonic", return_value=now):
            self.ws._on_message(json.dumps(frame))

    def frames(self):
        self.capture.close()
        path, = self.directory.rglob("*.jsonl")
        return [json.loads(line) for line in path.read_text().splitlines()]

    def test_seed_precedes_complete_ordered_transitions_and_repeated_logs(self):
        status = {"type": "LogLine", "rawLine": "26|ts|1234|Status|10|40000001|Boss|10000001|Player|"}
        combat = {"type": "InCombat", "inACTCombat": True, "inGameCombat": True}
        received = [status, self.party("10000002"), status, combat, combat, self.cast]
        for frame in received:
            self.send(frame)
        self.assertEqual(self.frames(), self.seed + received)

    def test_message_eviction_uses_state_before_the_oldest_retained_frame(self):
        changed = self.party("10000002")
        retained = [{"type": "LogLine", "rawLine": "00|ts|Before party change"},
                    self.party("10000003"), self.cast]
        with patch.object(pull_capture, "_PRE_PULL_MAX_MESSAGES", 3):
            for frame in [changed, *retained]:
                self.send(frame)
        expected_seed = [*self.seed[:2], changed, self.seed[3]]
        self.assertEqual(self.frames(), expected_seed + retained)

    def test_age_eviction_preserves_transitions_after_the_new_seed(self):
        changed = self.party("10000002")
        retained = self.party("10000003")
        self.send(changed, now=0)
        self.send(retained, now=16)
        self.send(self.cast, now=17)
        expected_seed = [*self.seed[:2], changed, self.seed[3]]
        self.assertEqual(self.frames(), expected_seed + [retained, self.cast])

    def test_byte_eviction_preserves_transitions_after_the_new_seed(self):
        changed = self.party("10000002")
        retained = [self.party("10000003"), self.cast]
        limit = sum(len(json.dumps(frame).encode()) for frame in retained)
        with patch.object(pull_capture, "_PRE_PULL_MAX_BYTES", limit):
            for frame in [changed, *retained]:
                self.send(frame)
        expected_seed = [*self.seed[:2], changed, self.seed[3]]
        self.assertEqual(self.frames(), expected_seed + retained)


if __name__ == "__main__":
    unittest.main()
