from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

from nyaatriggers.prog_session import ProgSessions
from nyaatriggers.recap_store import RecapStore
from nyaatriggers.record_store import RecordWriter, read_record


class RecordWriterRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.writer = RecordWriter()
        self.addCleanup(self.writer.close)

    def assert_rejected_job_released(self):
        self.assertEqual(len(self.writer._pending), 0)
        self.assertEqual(self.writer._latest, {})
        self.assertFalse(self.writer._running)

    def test_failed_first_worker_keeps_the_active_session_available_for_retry(self):
        sessions = ProgSessions(self.directory, clock=lambda: 10, wall=lambda: 1000, writer=self.writer)
        with patch.object(threading.Thread, "start", side_effect=RuntimeError("No writer thread")):
            session = sessions.start("Raid night", 1, "Duty", False)
        self.assertIs(sessions.current, session)
        self.assertEqual(sessions.unsaved, {session["id"]: session})
        self.assertEqual(sessions._saving, {})
        self.assertIn("No writer thread", sessions.save_error)
        self.assert_rejected_job_released()
        self.assertEqual(list(self.directory.glob("*.json")), [])
        session["name"] = "Recovered night"
        sessions.flush_pending(wait=True)
        saved = read_record(self.directory / (session["id"] + ".json"))
        self.assertEqual(saved["name"], "Recovered night")
        self.assertEqual(sessions.unsaved, {})
        self.assertEqual(sessions.save_error, "")

    def test_failed_first_recap_worker_keeps_the_death_and_retries_once(self):
        store = RecapStore(self.directory, self.writer)
        session_id, pull_id = str(uuid4()), str(uuid4())
        death = {"id": str(uuid4()), "actor": 0x10000001, "name": "Player", "zone": "Duty",
                 "when": 1000, "events": [], "statuses": []}
        with patch.object(threading.Thread, "start", side_effect=RuntimeError("No recap thread")):
            recorded = store.record(session_id, pull_id, death)
        self.assertEqual(store.unsaved, {death["id"]: recorded})
        self.assertEqual(store._saving, {})
        self.assertIn("No recap thread", store.save_error)
        self.assert_rejected_job_released()
        self.assertEqual(store.load(session_id, pull_id), ([recorded], []))
        store.flush_pending()
        self.writer.poll(wait=True)
        self.assertEqual(store.unsaved, {})
        self.assertEqual(store.save_error, "")
        self.assertEqual(store.load(session_id, pull_id), ([recorded], []))
        self.assertEqual(len(list(self.directory.glob("*/*/*.json"))), 1)


if __name__ == "__main__":
    unittest.main()
