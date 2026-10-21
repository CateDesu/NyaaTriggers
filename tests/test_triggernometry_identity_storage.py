from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from nyaatriggers import app_common as ac
from nyaatriggers.triggernometry_editor import occupied_import_ids, prepare_import, prepared_packs, xml_bytes
from tests.test_triggernometry_duplicates import GUID_SCRIPT, identities, pack


class TriggernometryIdentityStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.history = self.directory / ".nyaatriggers-identities.json"

    def write_pack(self, name, label):
        path = self.directory / name
        path.write_bytes(xml_bytes(pack(name, [label])))
        os.utime(path, ns=(1_700_000_000_000_000_000, 1_700_000_000_000_000_000))
        return path

    def test_import_reserves_an_absent_peers_identity_before_persisting_script_references(self):
        previous = self.write_pack("previous.xml", "Left")
        prepared_packs([previous])
        previous.unlink()
        incoming = pack("Incoming", ["Right"])
        action = incoming.find(".//Trigger/Actions/Action")
        action.attrib.clear()
        action.attrib.update(ActionType="ExecuteScript", ExecScriptExpression=GUID_SCRIPT)
        with self.assertRaisesRegex(ValueError, "Script.*contains trigger ID"):
            prepare_import(xml_bytes(incoming), occupied_ids=occupied_import_ids(self.directory))

    def test_corrupt_history_fails_closed_and_retries_after_same_stamp_repair(self):
        paths = [self.write_pack("a.xml", "Left"), self.write_pack("b.xml", "Right")]
        expected = {path: identities(raw) for path, raw in prepared_packs(paths)}
        original = self.history.read_bytes()
        stamp = self.history.stat().st_mtime_ns
        self.history.write_bytes(b"!" * len(original))
        os.utime(self.history, ns=(stamp, stamp))
        errors = []
        self.assertEqual(prepared_packs(paths, on_error=lambda path, error: errors.append((path, error))), [])
        self.assertEqual({path for path, _error in errors}, set(paths))
        with self.assertRaisesRegex(ValueError, "identities"):
            occupied_import_ids(self.directory)
        self.history.write_bytes(original)
        os.utime(self.history, ns=(stamp, stamp))
        self.assertEqual({path: identities(raw) for path, raw in prepared_packs(paths)}, expected)

    def test_failed_identity_save_exposes_no_uncommitted_runtime_ids_and_can_retry(self):
        path = self.write_pack("only.xml", "Left")
        original = path.read_bytes()
        with patch.object(ac, "_atomic_write_bytes", side_effect=OSError("Disk full")):
            errors = []
            self.assertEqual(prepared_packs([path], on_error=lambda _path, error: errors.append(error)), [])
            self.assertEqual(len(errors), 1)
            with self.assertRaisesRegex(ValueError, "identities"):
                occupied_import_ids(self.directory)
        self.assertFalse(self.history.exists())
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(dict(prepared_packs([path]))[path], original)
        self.assertTrue(self.history.is_file())

    def test_concurrent_scans_agree_when_a_new_earlier_peer_appears(self):
        existing = self.write_pack("b.xml", "Right")
        original = existing.read_bytes()
        expected = identities(dict(prepared_packs([existing]))[existing])
        added = self.write_pack("a.xml", "Left")
        with ThreadPoolExecutor(max_workers=2) as workers:
            results = list(workers.map(prepared_packs, ([existing], [added, existing])))
        for result in results:
            self.assertEqual(identities(dict(result)[existing]), expected)
        complete = dict(prepared_packs([added, existing]))
        self.assertTrue(set(identities(complete[added]).values()).isdisjoint(expected.values()))
        self.assertEqual(existing.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
