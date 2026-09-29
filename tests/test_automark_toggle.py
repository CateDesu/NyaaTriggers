"""Automarker changes take effect before a storage warning opens its event loop."""

import unittest
from unittest.mock import patch

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication, QMessageBox

from nyaatriggers import app_common as ac
from nyaatriggers.telesto_client import TelestoClient
from tests import test_session_ui as fixture


ACTUAL_WARNING = QMessageBox.warning


class AutomarkToggleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture.SessionUiTests.setUpClass()

    def setUp(self):
        fixture.SessionUiTests.setUp(self)

    def test_disabling_blocks_marks_before_a_save_warning(self):
        window = self.window
        window._telesto_client.request_stop()
        window._telesto_client.join_stopped(2)
        client = TelestoClient(enabled=True)
        window._telesto_client = client
        window._settings["telesto_enabled"] = True
        window._automark_cb.blockSignals(True)
        window._automark_cb.setChecked(True)
        window._automark_cb.blockSignals(False)
        window._automark_active["me"] = "ABC"
        self.assertTrue(client.mark_self("attack1"))

        settings = ac._SETTINGS_FILE
        settings.unlink(missing_ok=True)
        settings.mkdir()
        observed = []
        timer = QTimer()

        def inspect_warning():
            dialog = QApplication.activeModalWidget()
            if isinstance(dialog, QMessageBox):
                observed.append((client.is_enabled(), client.mark_self("attack2")))
                timer.stop()
                dialog.accept()

        timer.timeout.connect(inspect_warning)
        try:
            with patch.object(ac.QMessageBox, "warning", ACTUAL_WARNING):
                timer.start(10)
                window._automark_cb.setChecked(False)
            self.assertEqual(observed, [(False, False)])
            commands = [item[0]["payload"].get("command", "") for item in client._queue.queue]
            self.assertFalse(any("attack" in command for command in commands))
            self.assertIn("/mk clear <me>", commands)
            self.assertFalse(window._automark_active)
        finally:
            timer.stop()
            settings.rmdir()
        window._automark_cb.setChecked(True)
        self.assertTrue(client.mark_self("attack1"))


if __name__ == "__main__":
    unittest.main()
