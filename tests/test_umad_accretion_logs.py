from datetime import datetime
from pathlib import Path
import unittest

from nyaatriggers.umad_chains import AccretionQueue


FIRST, SECOND = "10FF0001", "10FF0002"
FIXTURE = Path(__file__).parent / "fixtures" / "umad_accretion_sequence.log"


class AccretionLogTests(unittest.TestCase):
    def events(self):
        first = None
        for line in FIXTURE.read_text().splitlines():
            if not line or line.startswith("#"):
                continue
            fields = line.split("|")
            timestamp = datetime.fromisoformat(fields[1])
            if first is None:
                first = timestamp
            yield fields, (timestamp - first).total_seconds()

    def test_synthetic_primary_sequence_keeps_each_sign_until_completion(self):
        controller = AccretionQueue()
        observed = []
        for fields, now in self.events():
            callback = controller.on_gain if fields[0] == "26" else controller.on_loss
            actions = callback(fields[2], fields[7], now)
            if actions:
                observed.append((fields[0], fields[2], fields[7], actions))
            if 0.03 <= now < 30.01:
                self.assertEqual(controller.outstanding(), [FIRST])
            elif 30.01 <= now < 60:
                self.assertEqual(controller.outstanding(), [SECOND])
        self.assertEqual(observed, [
            ("26", "644", FIRST, [("mark", FIRST, "ignore1")]),
            ("30", "154E", FIRST, [("clear", FIRST), ("mark", SECOND, "ignore2")]),
            ("30", "154E", SECOND, [("clear", SECOND)]),
        ])
        self.assertEqual(controller.outstanding(), [])

    def test_authoritative_completion_still_works_if_intermediate_packets_are_missing(self):
        controller = AccretionQueue()
        observed = []
        for fields, now in self.events():
            if fields[2] in {"154C", "154D"}:
                continue
            callback = controller.on_gain if fields[0] == "26" else controller.on_loss
            observed += callback(fields[2], fields[7], now)
        self.assertEqual(observed, [("mark", FIRST, "ignore1"), ("clear", FIRST),
                                    ("mark", SECOND, "ignore2"), ("clear", SECOND)])


if __name__ == "__main__":
    unittest.main()
