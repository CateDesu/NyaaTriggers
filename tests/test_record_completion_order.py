from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

from nyaatriggers.prog_session import ProgSessions
from nyaatriggers.record_store import RecordWriter, read_record, write_record


class RecordCompletionOrderTests(unittest.TestCase):
    def test_stale_success_preserves_newer_failure_and_other_record_completion(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            writer = RecordWriter()
            entered, release = threading.Event(), threading.Event()
            self.addCleanup(writer.close)
            self.addCleanup(release.set)
            sessions = ProgSessions(directory, clock=lambda: 10, wall=lambda: 1000,
                                    writer=writer)
            writes = []

            def blocked(path, data):
                entered.set()
                if not release.wait(5):
                    raise OSError("Writer was not released")
                writes.append(data["name"])
                write_record(path, data)

            with patch("nyaatriggers.prog_session.write_record", side_effect=blocked):
                session = sessions.start("Original", 1, "Duty", False)
                self.assertTrue(entered.wait(5))
            recap = sessions.recaps.record(session["id"], str(uuid4()), {
                "id": str(uuid4()), "actor": 0x10000001, "name": "Player",
                "zone": "Duty", "when": 1000, "events": [], "statuses": []})
            session["name"] = "Latest"
            with patch("nyaatriggers.prog_session.write_record",
                       side_effect=OSError("Disk unavailable")):
                self.assertTrue(sessions.save(session))
                release.set()
                sessions.poll_saves(wait=True)
            self.assertEqual(writes, ["Original"])
            self.assertEqual(read_record(directory / (session["id"] + ".json"))["name"],
                             "Original")
            self.assertIs(sessions.unsaved[session["id"]], session)
            self.assertIn("Disk unavailable", sessions.save_error)
            self.assertEqual(sessions._saving, {})
            self.assertEqual(sessions.recaps.unsaved, {})
            self.assertEqual(sessions.recaps.load(session["id"], recap["pull_id"]),
                             ([recap], []))
            sessions.flush_pending(wait=True)
            self.assertEqual(sessions.unsaved, {})
            self.assertEqual(sessions.save_error, "")
            self.assertEqual(read_record(directory / (session["id"] + ".json"))["name"],
                             "Latest")
            self.assertEqual(ProgSessions(directory).sessions[0]["name"], "Latest")


if __name__ == "__main__":
    unittest.main()
