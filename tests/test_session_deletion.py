from copy import deepcopy
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

from PyQt6.QtCore import QCoreApplication, QEvent

from nyaatriggers import prog_session
from nyaatriggers.prog_session import ProgSessions
from nyaatriggers.record_store import RecordWriter, read_record, write_record
from nyaatriggers import app_common as ac
from nyaatriggers.telesto_client import TelestoClient
from tests import test_session_ui as fixture


class SessionDeletionTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.sessions = ProgSessions(self.directory, clock=lambda: 10, wall=lambda: 1000)

    def ended(self, name="Raid night"):
        session = self.sessions.start(name, 1, "Duty", False)
        self.sessions.end()
        return session

    def recap(self, session):
        return self.sessions.recaps.record(session["id"], str(uuid4()), {
            "id": str(uuid4()), "actor": 0x10000001, "name": "Player", "zone": "Duty",
            "when": 1000, "events": [], "statuses": []})

    def test_deletion_removes_owned_files_and_retries_without_affecting_active_session(self):
        deleted = self.ended()
        recap = self.recap(deleted)
        active = self.sessions.start("Current night", 1, "Duty", False)
        with patch.object(prog_session, "write_record", side_effect=OSError("Summary failed")):
            self.sessions.save(deleted)
        with patch("nyaatriggers.recap_store.write_record", side_effect=OSError("Recap failed")):
            self.sessions.recaps.save(recap)
        self.sessions.delete(deleted)
        self.sessions.flush_pending(wait=True)
        self.assertEqual(self.sessions.sessions, [active])
        self.assertIs(self.sessions.current, active)
        self.assertFalse((self.directory / (deleted["id"] + ".json")).exists())
        self.assertFalse((self.directory / "recaps" / deleted["id"]).exists())
        self.assertEqual(self.sessions.unsaved, {})
        self.assertEqual(self.sessions.recaps.unsaved, {})
        self.assertEqual(self.sessions.save_error, "")
        self.assertEqual(self.sessions.recaps.save_error, "")
        self.assertEqual(ProgSessions(self.directory).sessions[0]["id"], active["id"])

    def test_active_and_unowned_session_objects_cannot_be_deleted(self):
        active = self.sessions.start("Current night", 1, "Duty", False)
        with self.assertRaisesRegex(ValueError, "End"):
            self.sessions.delete(active)
        self.sessions.end()
        with self.assertRaises(ValueError):
            self.sessions.delete(deepcopy(active))
        self.assertTrue((self.directory / (active["id"] + ".json")).exists())

    def test_queued_summary_and_recap_saves_finish_before_deletion(self):
        writer = RecordWriter()
        self.addCleanup(writer.close)
        self.sessions = ProgSessions(self.directory, clock=lambda: 10, wall=lambda: 1000, writer=writer)
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def blocked(directory, data):
            entered.set()
            if not release.wait(3):
                raise OSError("Test writer was not released")
            write_record(directory, data)

        with patch.object(prog_session, "write_record", side_effect=blocked):
            session = self.ended()
            recap = self.recap(session)
            self.assertTrue(entered.wait(1))
            deleting = threading.Thread(target=lambda: (self.sessions.delete(session), finished.set()))
            deleting.start()
            try:
                self.assertFalse(finished.wait(.05))
            finally:
                release.set()
                deleting.join(3)
        self.assertFalse(deleting.is_alive())
        self.assertTrue(finished.is_set())
        self.sessions.flush_pending(wait=True)
        self.assertEqual(self.sessions.sessions, [])
        self.assertEqual(list(self.directory.glob("*.json")), [])
        self.assertFalse((self.directory / "recaps" / session["id"]).exists())
        self.assertNotIn(recap["id"], self.sessions.recaps.unsaved)

    def test_disk_failure_retains_session_and_all_in_memory_retry_state(self):
        session = self.ended()
        recap = self.recap(session)
        with patch.object(prog_session, "write_record", side_effect=OSError("Summary failed")):
            self.sessions.save(session)
        with patch("nyaatriggers.recap_store.write_record", side_effect=OSError("Recap failed")):
            self.sessions.recaps.save(recap)
        original_unlink = Path.unlink
        summary = self.directory / (session["id"] + ".json")

        def refuse_summary(path, *args, **kwargs):
            if path == summary:
                raise PermissionError("Summary is locked")
            return original_unlink(path, *args, **kwargs)

        with patch.object(Path, "unlink", refuse_summary):
            with self.assertRaisesRegex(OSError, "locked"):
                self.sessions.delete(session)
        self.assertIs(self.sessions.sessions[0], session)
        self.assertEqual(self.sessions.unsaved, {session["id"]: session})
        self.assertEqual(self.sessions.recaps.unsaved, {recap["id"]: recap})
        self.assertEqual(self.sessions.save_error, "Summary failed")
        self.assertEqual(self.sessions.recaps.save_error, "Recap failed")
        self.assertEqual(read_record(summary)["id"], session["id"])
        self.sessions.flush_pending(wait=True)
        self.assertEqual(self.sessions.recaps.load(session["id"], recap["pull_id"]), ([recap], []))
        self.sessions.delete(session)
        self.assertEqual(ProgSessions(self.directory).sessions, [])


class SessionDeletionUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture.SessionUiTests.setUpClass()

    def setUp(self):
        self.case = fixture.SessionUiTests()
        self.addCleanup(self.cleanup_case)
        with patch.object(TelestoClient, "start"):
            self.case.setUp()
        self.window = self.case.window
        self.tab = self.window._prog_tab
        self.case.connect()

    def cleanup_case(self):
        self.case.doCleanups()
        window = getattr(self.case, "window", None)
        if window is not None:
            window.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def ended(self):
        self.case.start_session()
        session = self.tab.session
        self.window._on_in_combat(True, True)
        self.case.line(fixture.ability())
        self.case.line(["25", "ts", fixture.PLAYER, "Player"])
        self.case.clock.value += 2
        self.window._on_in_combat(False, False)
        self.case.end_session()
        return session

    def delete(self, answer=ac.QMessageBox.StandardButton.Yes):
        with patch.object(ac.QMessageBox, "question", return_value=answer) as confirmation:
            self.tab.delete_button.click()
        return confirmation

    def test_cancel_keeps_notes_and_confirmed_delete_clears_last_session_details(self):
        session = self.ended()
        self.tab.note.setPlainText("Unsaved selected-pull note")
        confirmation = self.delete(ac.QMessageBox.StandardButton.No)
        self.assertIs(self.tab.session, session)
        self.assertEqual(self.tab.note.toPlainText(), "Unsaved selected-pull note")
        self.assertEqual(confirmation.call_args.args[-1], ac.QMessageBox.StandardButton.No)
        self.tab.open_recaps()
        self.assertIs(self.window._recap_context[0], session)
        self.tab.note.setPlainText("Latest note before deletion")
        self.delete()
        self.tab.flush()
        self.tab.tick()
        self.assertIsNone(self.tab.session)
        self.assertIsNone(self.tab.pull)
        self.assertIsNone(self.tab.dirty)
        self.assertIsNone(self.tab._hidden_selection)
        self.assertFalse(self.tab.save_timer.isActive())
        self.assertEqual(self.tab.table.rowCount(), 0)
        self.assertEqual(self.tab.note.toPlainText(), "")
        self.assertEqual(self.tab.phase_table.rowCount(), 0)
        self.assertEqual(self.tab.comparison.candidates, [])
        self.assertIsNone(self.window._recap_context)
        self.assertFalse(self.tab.delete_button.isEnabled())
        self.assertEqual(ProgSessions(self.case.temp / "prog_sessions").sessions, [])
        self.assertFalse((self.case.temp / "prog_sessions" / "recaps" / session["id"]).exists())

    def test_delete_archived_filtered_session_repairs_selection_and_preserves_active_capture(self):
        old = self.ended()
        self.window._prog_sessions.set_archived(old, True)
        self.tab.refresh()
        self.case.start_session()
        active = self.tab.session
        self.window._on_in_combat(True, True)
        self.case.line(fixture.ability())
        pending = self.window._prog_sessions.pending
        self.tab.show_archived.setChecked(True)
        self.tab.picker.setCurrentIndex(self.tab.picker.findData(old["id"]))
        self.tab.name.setText("Delete only this archived session")
        self.tab.rename()
        self.tab.session_search.setText("Delete only")
        self.delete()
        self.assertIsNone(self.tab.session)
        self.assertIsNone(self.tab._hidden_selection)
        self.assertIs(self.window._prog_sessions.current, active)
        self.assertEqual(self.window._prog_sessions.pending, pending)
        self.tab.session_search.clear()
        self.assertIs(self.tab.session, active)
        self.assertFalse(self.tab.delete_button.isEnabled())
        self.assertNotIn(old["id"], {session["id"] for session in self.tab.comparison.candidates})
        with patch.object(ac.QMessageBox, "question") as confirmation:
            self.tab.delete_session()
        confirmation.assert_not_called()
        self.case.clock.value += 3
        self.window._on_in_combat(False, False)
        self.assertEqual(len(active["pulls"]), 1)
        self.assertTrue(active["pulls"][0]["complete"])

    def test_failed_delete_keeps_selection_notes_and_reports_partial_removal(self):
        session = self.ended()
        self.tab.note.setPlainText("Keep retryable notes")
        self.tab.flush()
        self.window._prog_sessions.poll_saves(wait=True)
        directory = self.case.temp / "prog_sessions" / "recaps" / session["id"]
        recap = next(directory.glob("*/*.json"))

        def partial_removal(path):
            self.assertEqual(path, directory)
            recap.unlink()
            raise OSError("Recap is locked")

        with patch.object(prog_session.shutil, "rmtree", side_effect=partial_removal), \
                patch.object(ac.QMessageBox, "warning") as warning:
            self.delete()
        self.assertIs(self.tab.session, session)
        self.assertEqual(self.tab.note.toPlainText(), "Keep retryable notes")
        self.assertEqual(self.window._prog_sessions.sessions, [session])
        self.assertIn("Recap is locked", warning.call_args.args[2])
        self.assertIn("may already have been removed", warning.call_args.args[2])
        self.assertFalse(recap.exists())
        self.assertTrue((self.case.temp / "prog_sessions" / (session["id"] + ".json")).exists())
        self.delete()
        self.assertEqual(self.window._prog_sessions.sessions, [])


if __name__ == "__main__":
    unittest.main()
