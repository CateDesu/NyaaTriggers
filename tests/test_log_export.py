import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QDialog

from nyaatriggers.ui.settings_tab import SettingsTabMixin


class LogExportTests(unittest.TestCase):
    def export(self, path):
        with patch("nyaatriggers.ui.settings_tab.ac.QFileDialog") as dialog, \
                patch("nyaatriggers.ui.settings_tab.ac.QMessageBox.information") as success, \
                patch("nyaatriggers.ui.settings_tab.ac.QMessageBox.critical") as failure:
            dialog.return_value.exec.return_value = QDialog.DialogCode.Accepted
            dialog.return_value.selectedFiles.return_value = [str(path)]
            SettingsTabMixin._save_raw_log(SimpleNamespace(_raw_capture=["戦闘|line", "next"]))
            return success.call_count, failure.call_count

    def test_export_preserves_existing_sibling_temporary_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "combat.txt"
            sibling = root / "combat.txt.tmp"
            path.write_bytes(b"previous export")
            sibling.write_bytes(b"unrelated user file")
            self.assertEqual(self.export(path), (1, 0))
            ending = "\r\n" if os.name == "nt" else "\n"
            self.assertEqual(path.read_bytes(), f"戦闘|line{ending}next{ending}".encode("utf-8"))
            self.assertEqual(sibling.read_bytes(), b"unrelated user file")
            self.assertEqual(set(root.iterdir()), {path, sibling})

    def test_failed_replacement_preserves_export_and_sibling(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "combat.txt"
            sibling = root / "combat.txt.tmp"
            path.write_bytes(b"previous export")
            sibling.write_bytes(b"unrelated user file")
            with patch("nyaatriggers.app_common.os.replace", side_effect=PermissionError("locked")):
                self.assertEqual(self.export(path), (0, 1))
            self.assertEqual(path.read_bytes(), b"previous export")
            self.assertEqual(sibling.read_bytes(), b"unrelated user file")
            self.assertEqual(set(root.iterdir()), {path, sibling})


if __name__ == "__main__":
    unittest.main()
