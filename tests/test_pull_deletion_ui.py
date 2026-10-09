import unittest
from unittest.mock import patch
from uuid import uuid4

from PyQt6 import sip
from PyQt6.QtCore import QCoreApplication, QEvent
from PyQt6.QtWidgets import QDialogButtonBox

from nyaatriggers import app_common as ac
from nyaatriggers.locale_util import set_locale
from nyaatriggers.prog_session import ProgSessions
from nyaatriggers.telesto_client import TelestoClient
from nyaatriggers.triggevent_bridge import TriggeventBridge
from nyaatriggers.ui.recap_browser import SavedRecapDialog
from tests import test_session_ui as fixture
from tests.test_prog_comparison import pull as saved_pull


class PullDeletionUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture.SessionUiTests.setUpClass()

    def setUp(self):
        self.case = fixture.SessionUiTests()
        self.addCleanup(self.cleanup_case)
        with patch.object(TelestoClient, "start"), \
                patch.object(TriggeventBridge, "is_available", return_value=False):
            self.case.setUp()
        self.window = self.case.window
        self.tab = self.window._prog_tab
        self.sessions = self.window._prog_sessions

    def cleanup_case(self):
        self.case.doCleanups()
        window = getattr(self.case, "window", None)
        if window is not None:
            window.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            self.assertTrue(sip.isdeleted(window))

    def history(self, count=2):
        session = self.sessions.start("Saved night", 1, "Duty", False)
        session["pulls"].extend(saved_pull(tracked=False) for _ in range(count))
        self.sessions.end()
        self.tab.refresh()
        return session

    def recap(self, session, pull):
        return self.sessions.recaps.record(session["id"], pull["id"], {
            "id": str(uuid4()), "actor": 0x10000001, "name": "Player", "zone": "Duty",
            "when": 1000, "events": [], "statuses": []})

    def confirm(self, answer=ac.QMessageBox.StandardButton.Yes):
        return patch.object(ac.QMessageBox, "question", return_value=answer)

    def reload(self):
        self.sessions.flush_pending(wait=True)
        loaded = ProgSessions(self.case.temp / "prog_sessions")
        self.addCleanup(loaded.close)
        return loaded.sessions

    def test_prog_cancel_keeps_pull_and_confirmed_delete_removes_its_owned_data(self):
        session = self.history()
        selected = self.tab.pull
        other = session["pulls"][0]
        recap = self.recap(session, selected)
        self.tab.note.setPlainText("Selected note before removal")
        with self.confirm(ac.QMessageBox.StandardButton.No) as question:
            self.tab.delete_pull_button.click()
        question.assert_called_once()
        self.assertIs(self.tab.pull, selected)
        self.assertEqual(self.tab.note.toPlainText(), "Selected note before removal")
        with self.confirm():
            self.tab.delete_pull_button.click()
        self.assertEqual(session["pulls"], [other])
        self.assertEqual(self.tab.table.rowCount(), 1)
        self.assertIs(self.tab.pull, other)
        self.assertEqual([pull["id"] for pull in self.reload()[0]["pulls"]], [other["id"]])
        self.assertFalse((self.case.temp / "prog_sessions" / "recaps" / session["id"] / selected["id"]).exists())
        self.assertNotIn(recap["id"], self.sessions.recaps.unsaved)

    def test_death_recap_deletion_returns_to_recent_and_clears_last_pull_details_offline(self):
        session = self.history(1)
        pull = session["pulls"][0]
        self.recap(session, pull)
        self.tab.open_recaps()
        self.assertFalse(self.window._connected)
        self.assertTrue(self.window._recap_delete_pull.isEnabled())
        with self.confirm():
            self.window._recap_delete_pull.click()
        self.assertIsNone(self.window._recap_context)
        self.assertTrue(self.window._recap_delete_pull.isHidden())
        self.assertEqual(session["pulls"], [])
        self.assertEqual(self.tab.table.rowCount(), 0)
        self.assertEqual(self.tab.note.toPlainText(), "")
        self.assertFalse(self.tab.delete_pull_button.isEnabled())
        self.assertEqual(self.reload()[0]["pulls"], [])

    def test_saved_browser_deletes_selected_pull_and_keeps_remaining_pull_openable(self):
        session = self.history()
        removed, remaining = session["pulls"][1], session["pulls"][0]
        dialog = SavedRecapDialog(self.sessions.sessions, self.window)
        self.addCleanup(dialog.deleteLater)
        self.assertEqual(dialog.selection[1]["id"], removed["id"])
        with self.confirm():
            dialog.delete_button.click()
        self.assertEqual(dialog.selection[1]["id"], remaining["id"])
        self.assertTrue(dialog.buttons.button(QDialogButtonBox.StandardButton.Open).isEnabled())
        self.assertEqual(session["pulls"], [remaining])
        with self.confirm():
            dialog.delete_button.click()
        self.assertIsNone(dialog.selection)
        self.assertFalse(dialog.delete_button.isEnabled())
        self.assertFalse(dialog.buttons.button(QDialogButtonBox.StandardButton.Open).isEnabled())

    def test_prior_pull_can_delete_while_automatic_recording_keeps_the_current_pull(self):
        self.case.connect()
        self.case.pull()
        session = self.sessions.current
        completed = session["pulls"][0]
        self.window._on_in_combat(True, True)
        self.case.line(fixture.ability())
        active = session["pulls"][1]
        self.tab.table.selectRow(1)
        self.assertTrue(self.tab.delete_pull_button.isEnabled())
        with self.confirm():
            self.tab.delete_pull_button.click()
        self.assertEqual(session["pulls"], [active])
        self.assertEqual(self.sessions.pending, active["id"])
        self.assertIs(self.sessions.current, session)
        self.assertFalse(self.tab.delete_pull_button.isEnabled())
        self.assertNotIn(completed, session["pulls"])
        self.window._on_in_combat(False, False)
        self.assertTrue(active["complete"])
        self.assertEqual(len(self.reload()[0]["pulls"]), 1)

    def test_deleting_an_earlier_pull_renumbers_saved_recap_without_changing_the_page(self):
        session = self.history(3)
        earlier, shown = session["pulls"][0], session["pulls"][2]
        self.window._show_pull_recaps(session, shown, 3)
        self.window._nav_buttons[4].click()
        self.tab.table.selectRow(2)
        self.assertIs(self.tab.pull, earlier)
        with self.confirm():
            self.tab.delete_pull_button.click()
        self.assertEqual(self.window._recap_context, (session, shown, 2))
        self.assertIn("Pull 2", self.window._recap_scope.text())
        self.assertIs(self.window._stack.currentWidget(), self.tab)
        self.window._step_recap_pull(-1)
        self.assertIs(self.window._recap_context[1], session["pulls"][0])

    def test_failed_commit_keeps_the_selection_and_reports_failure(self):
        session = self.history(1)
        selected = self.tab.pull
        with self.confirm(), patch.object(self.sessions, "delete_pull", side_effect=OSError("Storage locked")), \
                patch.object(ac.QMessageBox, "warning") as warning:
            self.tab.delete_pull_button.click()
        self.assertIs(self.tab.pull, selected)
        self.assertEqual(session["pulls"], [selected])
        self.assertIn("Could not delete the pull", warning.call_args.args[2])

    def test_committed_deletion_reports_recap_cleanup_warning_and_keeps_pull_removed(self):
        session = self.history(1)
        with self.confirm(), patch("nyaatriggers.prog_session.shutil.rmtree", side_effect=OSError("Storage locked")), \
                patch.object(ac.QMessageBox, "warning") as warning:
            self.tab.delete_pull_button.click()
        self.assertEqual(session["pulls"], [])
        self.assertIsNone(self.tab.pull)
        self.assertEqual(self.reload()[0]["pulls"], [])
        self.assertIn("The pull was deleted", warning.call_args.args[2])

    def test_saved_recap_controls_fit_the_minimum_window_in_both_languages(self):
        self.addCleanup(set_locale, "en")
        session = self.history()
        for locale in ("en", "ja"):
            with self.subTest(locale=locale):
                set_locale(locale)
                old = self.window._death_recap_tab
                index = self.window._stack.indexOf(old)
                page = self.window._build_death_recap_tab()
                self.window._stack.removeWidget(old)
                self.window._stack.insertWidget(index, page)
                self.window._death_recap_tab = page
                old.deleteLater()
                self.window._show_pull_recaps(session, session["pulls"][1], 2)
                self.window.resize(900, 600)
                self.window.show()
                self.case.app.processEvents()
                self.case.app.processEvents()
                self.assertEqual((self.window.width(), self.window.height()), (900, 600))
                for button in (self.window._recap_previous, self.window._recap_next,
                               self.window._recap_delete_pull, self.window._recap_recent,
                               self.window._recap_back):
                    position = button.mapTo(page, button.rect().center())
                    self.assertTrue(page.rect().contains(position))


if __name__ == "__main__":
    unittest.main()
