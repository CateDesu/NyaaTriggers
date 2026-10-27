"""Temporary record fixtures without native engines or live endpoints."""

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from nyaatriggers import recap_store
from nyaatriggers.recap_store import RecapStore


class CouncilRecordRetryTests(unittest.TestCase):
    def test_failed_reads_and_equal_size_replacements_are_retried_at_the_same_mtime(self):
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        store = RecapStore(directory)
        session_id, pull_id, ident = (str(uuid4()) for _ in range(3))
        folder = store._directory(session_id, pull_id)
        folder.mkdir(parents=True)
        path = folder / (ident + ".json")
        row = {"version": 1, "id": ident, "session_id": session_id, "pull_id": pull_id,
               "actor": 0x10000001, "name": "First", "zone": "Fixture", "when": 1000,
               "events": [], "statuses": []}
        valid = json.dumps(row).encode()
        stamp = 1700000000000000000

        def replace(raw):
            self.assertEqual(len(raw), len(valid))
            path.write_bytes(raw)
            os.utime(path, ns=(stamp, stamp))
            self.assertEqual(path.stat().st_mtime_ns, stamp)

        with patch.object(recap_store, "read_record", wraps=recap_store.read_record) as reads:
            damaged = b"!" + valid[1:]
            replace(damaged)
            for _attempt in range(2):
                records, errors = store.load(session_id, pull_id)
                self.assertEqual(records, [])
                self.assertEqual(len(errors), 1)
                self.assertEqual(path.read_bytes(), damaged)
            replace(valid)
            self.assertEqual(store.load(session_id, pull_id), ([row], []))
            row["name"] = "Other"
            replace(json.dumps(row).encode())
            self.assertEqual(store.load(session_id, pull_id), ([row], []))
            self.assertEqual(reads.call_count, 4)


if __name__ == "__main__":
    unittest.main()
