from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

from nyaatriggers.death_recap import DeathRecap
from nyaatriggers.recap_store import MAX_PENDING_RECAPS, RecapStore
from nyaatriggers.record_store import RecordWriter


class RecapRetryRetentionTests(unittest.TestCase):
    def test_full_writer_retains_the_latest_failed_recaps_and_saved_history(self):
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        writer = RecordWriter()
        self.addCleanup(writer.close)
        store = RecapStore(directory, writer)
        session_id, pull_id = str(uuid4()), str(uuid4())
        recap = DeathRecap()

        def record(index):
            recap.process(["25", "ts", "10000001", "Player"], now=index * 2)
            return store.record(session_id, pull_id, recap.deaths[0])

        durable = record(0)
        writer._future.result(timeout=10)
        writer.poll()
        self.assertEqual(store.unsaved, {})
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def unavailable(_source, _destination):
            entered.set()
            if not release.wait(10):
                raise OSError("Test writer was not released")
            raise OSError("Storage unavailable")

        failed = []
        with patch("nyaatriggers.record_store.os.replace", side_effect=unavailable):
            try:
                failed.append(record(1))
                self.assertTrue(entered.wait(5))
                for index in range(2, MAX_PENDING_RECAPS + 4):
                    failed.append(record(index))
                self.assertFalse(release.is_set())
            finally:
                release.set()
            writer._future.result(timeout=10)
            writer.poll()

        expected = {row["id"] for row in failed[-MAX_PENDING_RECAPS:]}
        self.assertEqual(set(store.unsaved), expected)
        self.assertEqual(set(store.errors), expected)
        self.assertEqual(store.dropped, 3)
        records, errors = store.load(session_id, pull_id)
        self.assertEqual(errors, [])
        self.assertEqual({row["id"] for row in records}, expected | {durable["id"]})
        self.assertEqual(len(list(directory.glob("*/*/*.json"))), 1)
        self.assertEqual(list(directory.glob("*/*/.record-*.tmp")), [])

        store.flush_pending()
        writer._future.result(timeout=10)
        writer.poll()
        self.assertEqual(store.unsaved, {})
        self.assertEqual(store.save_error, "")
        restarted = RecapStore(directory)
        records, errors = restarted.load(session_id, pull_id)
        self.assertEqual(errors, [])
        self.assertEqual({row["id"] for row in records}, expected | {durable["id"]})
        self.assertEqual(list(directory.glob("*/*/.record-*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
