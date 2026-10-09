import unittest
from unittest.mock import patch

from PyQt6 import sip
from PyQt6.QtCore import QCoreApplication, QEvent

from nyaatriggers import app_common as ac
from nyaatriggers import main_window as mw
from nyaatriggers.prog_session import ProgSessions
from nyaatriggers.record_store import read_record
from nyaatriggers.telesto_client import TelestoClient
from tests import test_session_ui as fixture


class OfflineProgTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture.SessionUiTests.setUpClass()

    def setUp(self):
        self.case = fixture.SessionUiTests()
        self.addCleanup(self.cleanup_case)
        self.identities = {}
        initialize_window = mw.MainWindow.__init__

        def initialize_saved_window(window, *args, **options):
            directory = ac._DATA_DIR / "prog_sessions"
            stamps = iter(range(1_700_000_000, 1_700_000_010))
            seed = ProgSessions(directory, clock=lambda: 100.0, wall=lambda: next(stamps))
            for name in ("Earlier night", "Later night"):
                session = seed.start(name, 1363, "The Dancing Mad", False)
                self.identities[name] = session["id"]
                seed.end()
            interrupted = seed.start("Interrupted night", 1363, "The Dancing Mad", False)
            self.identities["Interrupted night"] = interrupted["id"]
            seed.close()
            self.assertEqual(read_record(directory / (interrupted["id"] + ".json"))["state"], "active")
            initialize_window(window, *args, **options)

        with patch.object(mw.MainWindow, "__init__", initialize_saved_window), \
                patch.object(TelestoClient, "start"), \
                patch.object(mw.TriggeventBridge, "is_available", return_value=False), \
                patch.object(mw.MainWindow, "_load_cached_native_automark_inventory"), \
                patch.object(mw.MainWindow, "_load_cached_triggevent_inventory"), \
                patch.object(mw.MainWindow, "_load_cached_triggernometry_inventory"):
            self.case.setUp()
        self.window = self.case.window
        self.tab = self.window._prog_tab
        self.directory = self.case.temp / "prog_sessions"

    def cleanup_case(self):
        self.case.doCleanups()
        window = getattr(self.case, "window", None)
        if window is not None:
            window.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            self.assertTrue(sip.isdeleted(window))

    def select(self, name):
        index = self.tab.picker.findData(self.identities[name])
        self.assertGreaterEqual(index, 0)
        self.tab.picker.setCurrentIndex(index)
        self.assertEqual(self.tab.session["id"], self.identities[name])

    def reload(self):
        self.window._prog_sessions.flush_pending(wait=True)
        loaded = ProgSessions(self.directory)
        self.addCleanup(loaded.close)
        self.assertEqual(loaded.errors, [])
        return {session["id"]: session for session in loaded.sessions}

    def test_saved_and_interrupted_sessions_remain_manageable_before_game_connection(self):
        window, tab = self.window, self.tab
        self.assertFalse(window._connected)
        self.assertFalse(window._combat_known)
        self.assertFalse(window._in_game_combat)
        self.assertTrue(window._awaiting_zone_metadata)
        self.assertEqual(window._current_zone_id, 0)
        self.assertIsNone(window._prog_sessions.current)
        self.assertEqual(len(tab.sessions.sessions), 3)
        self.assertTrue(window._nav_buttons[4].isEnabled())
        window._nav_buttons[4].click()
        self.assertIs(window._stack.currentWidget(), tab)
        for control in ("start_button", "end_button", "start_session", "end_session"):
            self.assertFalse(hasattr(tab, control))

        self.select("Later night")
        self.assertTrue(tab.compare_button.isEnabled())
        self.assertTrue(tab.archive_button.isEnabled())
        self.assertTrue(tab.delete_button.isEnabled())
        tab.compare_button.click()
        self.assertFalse(tab.comparison.isHidden())
        compared = tab.comparison.picker.findData(self.identities["Earlier night"])
        self.assertGreater(compared, 0)
        tab.comparison.picker.setCurrentIndex(compared)
        self.assertEqual(tab.comparison.compared_id, self.identities["Earlier night"])
        self.assertTrue(tab.comparison.copy_button.isEnabled())

        self.select("Interrupted night")
        interrupted = self.identities["Interrupted night"]
        self.assertEqual(tab.session["state"], "interrupted")
        self.assertTrue(tab.compare_button.isEnabled())
        self.assertTrue(tab.archive_button.isEnabled())
        self.assertTrue(tab.delete_button.isEnabled())
        tab.archive_button.click()
        archived = self.reload()[interrupted]
        self.assertEqual(archived["state"], "interrupted")
        self.assertTrue(archived["archived"])

        tab.show_archived.setChecked(True)
        self.select("Interrupted night")
        tab.archive_button.click()
        self.assertFalse(self.reload()[interrupted]["archived"])

        with patch.object(ac.QMessageBox, "question", return_value=ac.QMessageBox.StandardButton.Yes) as confirm:
            tab.delete_button.click()
        confirm.assert_called_once()
        self.assertEqual(set(self.reload()), {self.identities["Earlier night"], self.identities["Later night"]})
        self.assertFalse((self.directory / (interrupted + ".json")).exists())
        self.assertFalse(window._connected)
        self.assertIsNone(window._prog_sessions.current)


if __name__ == "__main__":
    unittest.main()
