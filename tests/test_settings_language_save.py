import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from nyaatriggers import app_common as ac
from nyaatriggers.ui.settings_tab import SettingsTabMixin


class LanguageHost(SettingsTabMixin):
    def __init__(self):
        self._settings = {"ui_language": "en"}
        self._ui_lang_combo = Mock()
        self._ui_lang_combo.currentData.return_value = "ja"
        self._warn_save_failed = Mock()
        self._restart_for_update = Mock()


class LanguageSaveTests(unittest.TestCase):
    def test_failed_save_keeps_disk_language_and_does_not_offer_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.write_text('{"ui_language": "en"}')
            host = LanguageHost()
            with patch.object(ac, "_SETTINGS_FILE", path), \
                    patch("nyaatriggers.ui.settings_tab._atomic_write_json",
                          side_effect=OSError("read only")), \
                    patch.object(ac.QMessageBox, "question") as question:
                host._on_ui_language_changed(1)
            self.assertEqual(json.loads(path.read_text()), {"ui_language": "en"})
            self.assertEqual(host._settings["ui_language"], "ja")
            self.assertTrue(host._settings_save_failed)
            host._warn_save_failed.assert_called_once()
            question.assert_not_called()
            host._restart_for_update.assert_not_called()
            with patch.object(ac, "_SETTINGS_FILE", path):
                self.assertTrue(host._save_settings())
            self.assertEqual(json.loads(path.read_text()), {"ui_language": "ja"})
            self.assertFalse(host._settings_save_failed)

    def test_successful_save_persists_language_before_restart_prompt(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            host = LanguageHost()

            def accept(*args):
                self.assertEqual(json.loads(path.read_text()), {"ui_language": "ja"})
                return ac.QMessageBox.StandardButton.Yes

            with patch.object(ac, "_SETTINGS_FILE", path), \
                    patch.object(ac.QMessageBox, "question", side_effect=accept):
                host._on_ui_language_changed(1)
            host._restart_for_update.assert_called_once()
            host._warn_save_failed.assert_not_called()


if __name__ == "__main__":
    unittest.main()
