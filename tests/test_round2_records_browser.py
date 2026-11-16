import sys
import unittest
from unittest.mock import patch

from PyQt6.QtCore import QCoreApplication, QEvent
from PyQt6.QtWidgets import QDialog, QDialogButtonBox

from nyaatriggers import app_common as ac
from nyaatriggers.telesto_client import TelestoClient
from nyaatriggers.triggevent_bridge import TriggeventBridge
from nyaatriggers.triggernometry_bridge import TriggernometryBridge
from nyaatriggers.ui.recap_browser import SavedRecapDialog
from tests import test_session_ui as fixture
from tests.test_session_features import ability, PLAYER


class BrowserIdentityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture.SessionUiTests.setUpClass()

    def setUp(self):
        self.case = fixture.SessionUiTests()
        self.addCleanup(self.cleanup)
        with patch.object(TelestoClient, 'start'), \
                patch.object(TriggeventBridge, 'is_available', return_value=False), \
                patch.object(TriggernometryBridge, 'is_available', return_value=False):
            self.case.setUp()
        self.window = self.case.window
        self.case.connect()
        self.case.pull()
        self.session = self.window._prog_sessions.current
        self.completed = self.session['pulls'][0]
        self.window._on_in_combat(True, True)
        self.empty = self.session['pulls'][1]
        self.dialog = SavedRecapDialog(self.window._prog_sessions.sessions, self.window)
        self.assertIs(self.dialog.selection[1], self.empty)
        self.errors = []
        self.hook = patch.object(sys, 'excepthook', side_effect=lambda *args: self.errors.append(args))
        self.hook.start()
        self.addCleanup(self.hook.stop)

    def cleanup(self):
        if hasattr(self, 'dialog'):
            self.dialog.close()
            self.dialog.deleteLater()
        self.case.doCleanups()
        if hasattr(self.case, 'window'):
            self.case.window.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def remove_empty(self):
        self.window._on_in_combat(False, False)
        self.assertEqual(self.session['pulls'], [self.completed])

    def replacement(self):
        self.case.clock.value += 5
        self.window._on_in_combat(True, True)
        self.case.line(ability())
        self.case.line(['25', 'ts', PLAYER, 'Distinct replacement victim'])
        self.case.clock.value += 5
        self.window._on_in_combat(False, False)
        replacement = self.session['pulls'][1]
        self.assertNotEqual(replacement['id'], self.empty['id'])
        records, errors = self.window._prog_sessions.recaps.load(self.session['id'], replacement['id'])
        self.assertFalse(errors)
        self.assertEqual([r['name'] for r in records], ['Distinct replacement victim'])
        return replacement

    def assert_cleared(self):
        self.assertEqual(self.errors, [])
        self.assertIsNone(self.dialog.selection)
        self.assertFalse(self.dialog.delete_button.isEnabled())
        self.assertFalse(self.dialog.buttons.button(QDialogButtonBox.StandardButton.Open).isEnabled())
        self.assertNotEqual(self.dialog.result(), QDialog.DialogCode.Accepted)

    def test_removed_row_selection_clears_without_slot_exception(self):
        self.dialog.pulls.setCurrentRow(1)
        self.assertIs(self.dialog.selection[1], self.completed)
        self.remove_empty()
        self.dialog.pulls.setCurrentRow(0)
        self.assert_cleared()

    def test_removed_row_cannot_acquire_distinct_replacement(self):
        self.dialog.pulls.setCurrentRow(1)
        self.remove_empty()
        replacement = self.replacement()
        self.dialog.pulls.setCurrentRow(0)
        self.assert_cleared()
        self.dialog._refresh()
        self.assertIs(self.dialog.selection[1], replacement)
        self.assertTrue(self.dialog.delete_button.isEnabled())

    def test_open_revalidates_selected_removed_identity(self):
        self.remove_empty()
        self.replacement()
        self.dialog.buttons.button(QDialogButtonBox.StandardButton.Open).click()
        self.assert_cleared()
        self.dialog.pulls.setCurrentRow(1)
        self.dialog.buttons.button(QDialogButtonBox.StandardButton.Open).click()
        self.assertEqual(self.dialog.result(), QDialog.DialogCode.Accepted)
        self.assertIs(self.dialog.selection[1], self.completed)

    def test_delete_revalidates_selected_removed_identity_before_confirmation(self):
        self.remove_empty()
        removed = self.replacement()
        self.dialog._refresh()
        self.assertTrue(self.dialog.delete_button.isEnabled())
        self.window._prog_sessions.delete_pull(self.session, removed)
        replacement = self.replacement()
        with patch.object(ac.QMessageBox, 'question', return_value=ac.QMessageBox.StandardButton.Yes) as question, \
                patch.object(self.window._prog_tab, 'delete_pull', wraps=self.window._prog_tab.delete_pull) as delete:
            self.dialog.delete_button.click()
            delete.assert_not_called()
            question.assert_not_called()
            self.assert_cleared()
            self.dialog._refresh()
            self.assertIs(self.dialog.selection[1], replacement)
            self.dialog.delete_button.click()
            delete.assert_called_once()
            question.assert_called_once()
        self.assertEqual(self.session['pulls'], [self.completed])

    def test_open_resolves_current_ordinal_after_earlier_pull_deleted(self):
        self.remove_empty()
        replacement = self.replacement()
        self.dialog._refresh()
        self.assertEqual(self.dialog.selection, (self.session, replacement, 2))
        self.window._prog_sessions.delete_pull(self.session, self.completed)
        self.dialog.buttons.button(QDialogButtonBox.StandardButton.Open).click()
        self.assertEqual(self.dialog.result(), QDialog.DialogCode.Accepted)
        self.assertEqual(self.dialog.selection, (self.session, replacement, 1))


if __name__ == '__main__':
    unittest.main()
