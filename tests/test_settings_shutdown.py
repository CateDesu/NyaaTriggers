import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from nyaatriggers import app_common as ac
from nyaatriggers.main_window import MainWindow
from nyaatriggers.ui.settings_tab import SettingsTabMixin
from nyaatriggers.updater_ui import UpdaterUiMixin


class RestartHost(SettingsTabMixin, UpdaterUiMixin):
    _teardown_step = MainWindow._teardown_step

    def __init__(self):
        self._settings = {"ui_language": "en"}
        self._settings_save_timer = Mock()
        self._settings_save_timer.isActive.return_value = False
        self._ui_lang_combo = Mock()
        self._ui_lang_combo.currentData.return_value = "ja"
        self._warn_save_failed = Mock()
        for name in ("_stop_background_timers", "_clear_status_timers",
                     "_clear_seq_runners", "_finalize_live_encounter",
                     "_stop_cactbot_reader", "_stop_sidecar"):
            setattr(self, name, Mock())
        self._ws = Mock()
        self._pull_capture = Mock()


class SettingsShutdownTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "settings.json"
        self.path.write_text('{"ui_language": "en"}')
        settings = patch.object(ac, "_SETTINGS_FILE", self.path)
        settings.start()
        self.addCleanup(settings.stop)
        self.host = RestartHost()

    def test_failed_language_save_retries_before_relaunch_without_active_timer(self):
        with patch("nyaatriggers.ui.settings_tab._atomic_write_json",
                   side_effect=OSError("read only")), \
                patch.object(ac.QMessageBox, "question") as question:
            self.host._on_ui_language_changed(1)
        question.assert_not_called()
        self.assertTrue(self.host._settings_save_failed)

        at_relaunch = []

        def relaunch():
            at_relaunch.append((json.loads(self.path.read_text()), self.host._settings_save_failed))

        with patch("nyaatriggers.updater_ui.updater.relaunch", side_effect=relaunch) as restart:
            self.host._restart_for_update()
        restart.assert_called_once()
        self.assertEqual(at_relaunch, [({"ui_language": "ja"}, False)])
        self.host._settings_save_timer.stop.assert_called_once()
        self.host._stop_sidecar.assert_any_call("_triggernometry", wait=True)

    def test_pending_debounce_is_flushed_even_after_an_earlier_cleanup_failure(self):
        self.host._settings["ui_language"] = "ja"
        self.host._settings_save_timer.isActive.return_value = True
        self.host._clear_status_timers.side_effect = RuntimeError("cleanup failed")
        with patch("nyaatriggers.updater_ui.updater.relaunch") as restart, \
                patch("sys.stderr"):
            self.host._restart_for_update()
        self.assertEqual(json.loads(self.path.read_text()), {"ui_language": "ja"})
        self.host._settings_save_timer.stop.assert_called_once()
        self.host._pull_capture.close.assert_called_once()
        restart.assert_called_once()

    def test_repeated_flush_failure_preserves_disk_and_retry_state(self):
        self.host._settings["ui_language"] = "ja"
        self.host._settings_save_failed = True
        with patch("nyaatriggers.ui.settings_tab._atomic_write_json",
                   side_effect=OSError("read only")):
            self.host._flush_pending_settings_save()
        self.assertEqual(json.loads(self.path.read_text()), {"ui_language": "en"})
        self.assertTrue(self.host._settings_save_failed)
        self.host._flush_pending_settings_save()
        self.assertEqual(json.loads(self.path.read_text()), {"ui_language": "ja"})
        self.assertFalse(self.host._settings_save_failed)


if __name__ == "__main__":
    unittest.main()
