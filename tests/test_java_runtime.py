"""Check that packaging preserves distrust entries and rejects incomplete mitigation."""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools.java_runtime import EMUDHRA_CERTIFICATES, mitigate, read_blocklist


class JavaRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.runtime = Path(self.directory.name) / "jre"
        self.target = self.runtime / "lib/security/blocked.certs"
        self.target.parent.mkdir(parents=True)
        self.original = b"# Custom distrust caf\xe9\r\nAlgorithm=SHA-256\r\n" + b"A" * 64 + b"=custom\r\n"
        self.target.write_bytes(self.original)
        self.official = Path(self.directory.name) / "official.certs"
        # Simulated official data exercises merging without network access.
        self.official.write_text("Algorithm=SHA-256\n" + "\n".join(sorted(EMUDHRA_CERTIFICATES)) + "\n")

    def test_preserves_existing_entries_and_backup_on_repeated_application(self):
        mitigate(self.runtime, self.official)
        merged = self.target.read_bytes()
        self.assertTrue(merged.startswith(self.original))
        self.assertEqual(read_blocklist(merged), EMUDHRA_CERTIFICATES | {"A" * 64})
        self.assertEqual(self.target.with_suffix(".certs.bak").read_bytes(), self.original)
        mitigate(self.runtime, self.official)
        self.assertEqual(self.target.read_bytes(), merged)
        self.assertEqual(self.target.with_suffix(".certs.bak").read_bytes(), self.original)

    def test_pinned_mitigation_needs_no_download_and_preserves_existing_entries(self):
        mitigate(self.runtime)
        merged = self.target.read_bytes()
        self.assertTrue(merged.startswith(self.original))
        self.assertEqual(read_blocklist(merged), EMUDHRA_CERTIFICATES | {"A" * 64})
        self.assertEqual(self.target.with_suffix(".certs.bak").read_bytes(), self.original)
        mitigate(self.runtime)
        self.assertEqual(self.target.read_bytes(), merged)

    def test_rejects_incomplete_or_invalid_download_without_changing_runtime(self):
        for data in (b"<html>Download failed</html>", b"Algorithm=SHA-256\n" + b"B" * 64,
                     self.official.read_bytes().replace(b"SHA-256", b"SHA-1")):
            with self.subTest(data=data[:40]):
                self.official.write_bytes(data)
                with self.assertRaises(ValueError):
                    mitigate(self.runtime, self.official)
                self.assertEqual(self.target.read_bytes(), self.original)
                self.assertFalse(self.target.with_suffix(".certs.bak").exists())

    def test_rejects_incompatible_existing_algorithm(self):
        original = self.original.replace(b"SHA-256", b"SHA-1")
        self.target.write_bytes(original)
        with self.assertRaises(ValueError):
            mitigate(self.runtime, self.official)
        self.assertEqual(self.target.read_bytes(), original)

    def test_requires_every_compromised_certificate_variant(self):
        complete = self.official.read_text()
        for certificate in EMUDHRA_CERTIFICATES:
            with self.subTest(certificate=certificate):
                self.official.write_text(complete.replace(certificate + "\n", ""))
                with self.assertRaises(ValueError):
                    mitigate(self.runtime, self.official)
                self.assertEqual(self.target.read_bytes(), self.original)

    def test_preserves_additional_official_distrust(self):
        self.official.write_bytes(self.official.read_bytes() + b"B" * 64 + b"\n")
        mitigate(self.runtime, self.official)
        self.assertIn("B" * 64, read_blocklist(self.target.read_bytes()))

    def test_failed_write_leaves_runtime_blocklist_intact(self):
        write_bytes = Path.write_bytes

        def interrupted_write(path, data):
            write_bytes(path, data[:10])
            raise OSError("Write interrupted")

        with patch.object(Path, "write_bytes", interrupted_write):
            with self.assertRaises(OSError):
                mitigate(self.runtime, self.official)
        self.assertEqual(self.target.read_bytes(), self.original)
        self.assertEqual({path.name for path in self.target.parent.iterdir()},
                         {"blocked.certs", "blocked.certs.bak"})

    def test_preserves_unrelated_temporary_file_and_blocklist_permissions(self):
        unrelated = self.target.with_suffix(".certs.tmp")
        unrelated.write_bytes(b"Unrelated work")
        self.target.chmod(0o640)
        mode = self.target.stat().st_mode
        mitigate(self.runtime, self.official)
        self.assertEqual(unrelated.read_bytes(), b"Unrelated work")
        self.assertEqual(self.target.stat().st_mode, mode)


if __name__ == "__main__":
    unittest.main()
