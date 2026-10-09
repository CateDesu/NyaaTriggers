from copy import deepcopy
import unittest
from unittest.mock import patch

from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import Qt
from PyQt6.QtTest import QTest

from nyaatriggers import theme
from nyaatriggers.prog_session import ProgSessions
from nyaatriggers.record_store import read_record, write_record
from nyaatriggers.ui.recap_browser import SavedRecapDialog
from tests.test_prog_comparison import pull, session
from tests import test_session_ui as session_ui


class PullUiTests(unittest.TestCase):
    setUp = session_ui.SessionUiTests.setUp
    connect = session_ui.SessionUiTests.connect
    line = session_ui.SessionUiTests.line
    start_session = session_ui.SessionUiTests.start_session
    end_session = session_ui.SessionUiTests.end_session

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setStyleSheet(theme.STYLESHEET)

    def load(self, records):
        for record in records:
            write_record(self.temp / "prog_sessions", record)
        self.window._prog_sessions = ProgSessions(self.temp / "prog_sessions", clock=self.clock)
        tab = self.window._prog_tab
        tab.sessions = self.window._prog_sessions
        tab.refresh()
        return tab

    def marked_session(self, started=1):
        pulls = [pull("p3") for _ in range(7)]
        for index in (0, 2, 5):
            pulls[index]["bookmark"] = True
        return session(pulls, started=started)

    def test_navigation_uses_original_pull_order_and_does_not_wrap(self):
        tab = self.load((self.marked_session(),))
        self.assertFalse(tab.next_bookmark.isEnabled())
        for number in (6, 3, 1):
            tab.previous_bookmark.click()
            self.assertEqual(tab.session["pulls"].index(tab.pull) + 1, number)
            self.assertEqual(tab.table.item(tab.table.currentRow(), 0).text(), str(number))
        self.assertFalse(tab.previous_bookmark.isEnabled())
        for number in (3, 6):
            tab.next_bookmark.click()
            self.assertEqual(tab.session["pulls"].index(tab.pull) + 1, number)
        self.assertFalse(tab.next_bookmark.isEnabled())

    def test_navigation_flushes_notes_before_selecting_a_different_pull(self):
        tab = self.load((self.marked_session(),))
        edited = tab.pull
        tab.note.setPlainText("Towers\nReview positioning")
        tab.previous_bookmark.click()
        saved = read_record(self.temp / "prog_sessions" / (tab.session["id"] + ".json"))
        self.assertEqual(saved["pulls"][-1]["note"], "Towers\nReview positioning")
        self.assertIsNot(tab.pull, edited)
        self.assertEqual(tab.note.toPlainText(), "")

    def test_navigation_and_copy_retain_unsaved_notes_until_storage_recovers(self):
        self.addCleanup(self.app.clipboard().clear)
        tab = self.load((self.marked_session(),))
        path = self.temp / "prog_sessions" / (tab.session["id"] + ".json")
        backup = path.with_suffix(".saved")
        path.rename(backup)
        path.mkdir()
        tab.note.setPlainText("Last pull note")
        tab.previous_bookmark.click()
        self.assertTrue(tab.sessions.save_error)
        tab.note.setPlainText("Bookmarked pull note")
        tab.copy_pull_button.click()
        self.assertIn("Bookmarked pull note", self.app.clipboard().text())
        self.assertIn("could not be saved", tab.status.text())
        path.rmdir()
        backup.rename(path)
        tab.flush()
        saved = read_record(path)
        self.assertEqual(saved["pulls"][-1]["note"], "Last pull note")
        self.assertEqual(saved["pulls"][5]["note"], "Bookmarked pull note")
        self.assertFalse(tab.sessions.save_error)

    def test_bookmark_changes_and_session_switches_update_navigation(self):
        first, other = self.marked_session(started=2), session([pull("p2")])
        tab = self.load((first, other))
        tab.previous_bookmark.click()
        tab.bookmark.setChecked(False)
        tab.previous_bookmark.click()
        self.assertEqual(tab.session["pulls"].index(tab.pull), 2)
        self.assertFalse(tab.next_bookmark.isEnabled())
        tab.picker.setCurrentIndex(tab.picker.findData(other["id"]))
        self.assertFalse(tab.previous_bookmark.isEnabled())
        self.assertFalse(tab.next_bookmark.isEnabled())

    def test_copy_includes_observations_without_inventing_earlier_times(self):
        self.addCleanup(self.app.clipboard().clear)
        tab = self.load((self.marked_session(),))
        tab.previous_bookmark.click()
        tab.note.setPlainText("Towers\nReview positioning")
        selected = tab.pull
        cursor = tab.note.textCursor()
        cursor.setPosition(4)
        tab.note.setTextCursor(cursor)
        tab.copy_pull_button.click()
        copied = self.app.clipboard().text()
        self.assertIn("Session: ", copied)
        self.assertIn("Duty: Duty", copied)
        self.assertIn("Pull 6 · ", copied)
        self.assertIn("Duration: 00:00:10 · Ending: Wipe", copied)
        self.assertIn("Deaths: 0\nDeath recaps: 0\nBookmarked", copied)
        self.assertIn("P3\t00:00:05\tConfirmed", copied)
        self.assertIn("P1\t—\tEstablished by a later phase. Confirmation time unavailable.", copied)
        self.assertIn("first observed confirmation", copied)
        self.assertIn("Notes:\nTowers\nReview positioning", copied)
        self.assertIs(tab.pull, selected)
        self.assertEqual(tab.note.textCursor().position(), 4)
        saved = read_record(self.temp / "prog_sessions" / (tab.session["id"] + ".json"))
        self.assertEqual(saved["pulls"][5]["note"], "Towers\nReview positioning")

    def test_copy_preserves_unreadable_optional_phase_data_and_legacy_counts(self):
        self.addCleanup(self.app.clipboard().clear)
        block = {"version": 999, "future": {"observations": ["preserve"]}}
        unreadable = {**pull(), "phase_tracking": deepcopy(block)}
        legacy = pull(tracked=False)
        del legacy["recap_count"]
        tab = self.load((session([legacy, unreadable]),))
        tab.note.setPlainText("Still reviewable")
        tab.copy_pull_button.click()
        copied = self.app.clipboard().text()
        self.assertIn("Phase: Unavailable", copied)
        self.assertIn("Notes and recaps remain available", copied)
        saved = read_record(self.temp / "prog_sessions" / (tab.session["id"] + ".json"))
        self.assertEqual(saved["pulls"][1]["phase_tracking"], block)
        tab.table.selectRow(1)
        tab.copy_pull_button.click()
        copied = self.app.clipboard().text()
        self.assertIn("Phase: Not recorded", copied)
        self.assertNotIn("Death recaps:", copied)

    def test_empty_sessions_disable_navigation_and_copy_without_changing_clipboard(self):
        self.addCleanup(self.app.clipboard().clear)
        self.app.clipboard().setText("Keep this")
        tab = self.load((session([]),))
        for button in (tab.previous_bookmark, tab.next_bookmark, tab.copy_pull_button):
            self.assertFalse(button.isEnabled())
            button.click()
        self.assertEqual(self.app.clipboard().text(), "Keep this")

    def test_copy_live_pull_uses_latest_observed_meter_duration(self):
        self.addCleanup(self.app.clipboard().clear)
        self.connect()
        tab = self.window._prog_tab
        self.start_session()
        self.window._on_in_combat(True, True)
        self.line(session_ui.ability())
        self.clock.value += 12
        self.line(session_ui.ability())
        tab.note.setPlainText("Review this pull")
        tab.copy_pull_button.click()
        copied = self.app.clipboard().text()
        self.assertIn("Duration: 00:00:12 · Ending: In progress", copied)
        self.assertIn("Phase: Not supported", copied)
        self.assertIn("Notes:\nReview this pull", copied)
        self.assertIs(tab.session, tab.sessions.current)
        self.assertFalse(tab.pull["complete"])

    def test_live_pull_boundaries_keep_historical_navigation_and_note_cursor(self):
        historical = self.marked_session()
        tab = self.load((historical,))
        self.connect()
        self.start_session()
        tab.picker.setCurrentIndex(tab.picker.findData(historical["id"]))
        tab.previous_bookmark.click()
        selected = tab.pull
        tab.note.setPlainText("Review towers")
        cursor = tab.note.textCursor()
        cursor.setPosition(4)
        tab.note.setTextCursor(cursor)
        self.window._on_in_combat(True, True)
        self.line(session_ui.ability())
        self.window._on_in_combat(False, False)
        self.assertIs(tab.pull, selected)
        self.assertEqual(tab.note.textCursor().position(), 4)
        tab.previous_bookmark.click()
        self.assertEqual(tab.session["pulls"].index(tab.pull), 2)
        self.assertTrue(tab.next_bookmark.isEnabled())

    def test_search_matches_name_and_duty_words_without_changing_saved_data(self):
        first, other = self.marked_session(started=2), self.marked_session()
        first.update(name="Tuesday Towers", zone="UMAD")
        other.update(name="Monday", zone="別のコンテンツ")
        tab = self.load((first, other))
        original = deepcopy(tab.sessions.sessions)
        tab.session_search.setText("  TUESDAY  umad ")
        self.assertEqual(tab.picker.count(), 1)
        self.assertEqual(tab.session["id"], first["id"])
        self.assertEqual(tab.session_count.text(), "1 of 2 sessions")
        tab.session_search.setText("別の")
        self.assertEqual(tab.picker.count(), 1)
        self.assertEqual(tab.session["id"], other["id"])
        self.assertEqual(tab.sessions.sessions, original)

    def test_empty_search_results_keep_notes_and_restore_the_previous_selection(self):
        first, other = self.marked_session(started=2), self.marked_session()
        tab = self.load((first, other))
        tab.picker.setCurrentIndex(tab.picker.findData(other["id"]))
        tab.previous_bookmark.click()
        tab.note.setPlainText("Keep this saved note")
        selected = tab.pull
        tab.session_search.setText("No matching session")
        self.assertIsNone(tab.session)
        self.assertFalse(tab.picker.isEnabled())
        self.assertFalse(tab.copy_pull_button.isEnabled())
        self.assertIn("No sessions match", tab.stats.text())
        tab.session_search.clear()
        self.assertEqual(tab.session["id"], other["id"])
        self.assertEqual(tab.pull["id"], selected["id"])
        self.assertEqual(tab.note.toPlainText(), "Keep this saved note")
        saved = read_record(self.temp / "prog_sessions" / (other["id"] + ".json"))
        self.assertEqual(saved["pulls"][5]["note"], "Keep this saved note")

    def test_search_and_historical_selection_survive_background_pull_updates(self):
        historical = self.marked_session()
        historical.update(name="Tuesday towers", zone="UMAD")
        tab = self.load((historical,))
        self.connect()
        self.start_session()
        tab.session_search.setText("Tuesday UMAD")
        tab.note.setPlainText("Review towers")
        cursor = tab.note.textCursor()
        cursor.setPosition(4)
        tab.note.setTextCursor(cursor)
        selected = tab.pull
        self.window._on_in_combat(True, True)
        self.line(session_ui.ability())
        self.window._on_in_combat(False, False)
        self.assertEqual(tab.session_search.text(), "Tuesday UMAD")
        self.assertEqual(tab.picker.count(), 1)
        self.assertIs(tab.pull, selected)
        self.assertEqual(tab.note.textCursor().position(), 4)

    def test_search_does_not_stop_automatic_capture_or_reset_the_search(self):
        tab = self.load((self.marked_session(),))
        historical = tab.session
        self.connect()
        tab.session_search.setText("No matching session")
        self.assertIsNone(tab.sessions.current)
        self.window._on_in_combat(True, True)
        self.line(session_ui.ability())
        active = tab.sessions.current
        self.assertIsNotNone(active)
        self.clock.value += 12
        self.line(session_ui.ability())
        self.window._on_in_combat(False, False)
        self.assertEqual(tab.session_search.text(), "No matching session")
        self.assertIsNone(tab.session)
        self.assertIs(tab.sessions.current, active)
        self.assertEqual(len(active["pulls"]), 1)
        self.assertTrue(active["pulls"][0]["complete"])
        tab.session_search.clear()
        self.assertIs(tab.session, historical)
        self.assertIs(tab.sessions.current, active)

    def test_switching_to_an_identical_note_does_not_inherit_another_pulls_undo(self):
        record = self.marked_session()
        record["pulls"][5]["note"] = "Review towers"
        tab = self.load((record,))
        tab.note.insertPlainText("Review towers")
        tab.previous_bookmark.click()
        tab.note.undo()
        self.assertEqual(tab.note.toPlainText(), "Review towers")
        tab.flush()
        saved = read_record(self.temp / "prog_sessions" / (record["id"] + ".json"))
        self.assertEqual(saved["pulls"][5]["note"], "Review towers")
        self.assertEqual(saved["pulls"][6]["note"], "Review towers")

    def test_typing_at_the_note_limit_keeps_existing_text_and_cursor(self):
        tab = self.load((self.marked_session(),))
        for start in ("START ", "🙂START "):
            with self.subTest(start=start):
                note = start + "a" * (3996 - len(start)) + " END"
                tab.note.setPlainText(note)
                QTest.keyClick(tab.note, Qt.Key.Key_End, Qt.KeyboardModifier.ControlModifier)
                QTest.keyClicks(tab.note, "xy")
                self.assertEqual(tab.note.toPlainText(), note)
                self.assertEqual(tab.note.textCursor().position(),
                                 tab.note.document().characterCount() - 1)
                tab.flush()
                saved = read_record(self.temp / "prog_sessions" / (tab.session["id"] + ".json"))
                self.assertEqual(saved["pulls"][-1]["note"], note)

    def test_pasting_over_the_note_limit_keeps_the_insertion_position(self):
        tab = self.load((self.marked_session(),))
        note = "🙂" + "a" * 3999
        tab.note.setPlainText(note)
        cursor = tab.note.textCursor()
        cursor.setPosition(1001)
        tab.note.setTextCursor(cursor)
        tab.note.insertPlainText("Review")
        expected = (note[:1000] + "Review" + note[1000:])[:4000]
        self.assertEqual(tab.note.toPlainText(), expected)
        self.assertEqual(tab.note.textCursor().position(), 1007)
        QTest.keyClicks(tab.note, "x")
        self.assertEqual(tab.note.toPlainText(), (note[:1000] + "Reviewx" + note[1000:])[:4000])
        tab.flush()
        saved = read_record(self.temp / "prog_sessions" / (tab.session["id"] + ".json"))
        self.assertEqual(saved["pulls"][-1]["note"], tab.note.toPlainText())

    def test_switching_to_an_identical_session_name_does_not_inherit_rename_undo(self):
        current, other = self.marked_session(started=2), self.marked_session()
        other["name"] = "Renamed"
        tab = self.load((current, other))
        tab.name.selectAll()
        tab.name.insert("Renamed")
        tab.picker.setCurrentIndex(tab.picker.findData(other["id"]))
        tab.name.undo()
        self.assertEqual(tab.name.text(), "Renamed")
        tab.flush()
        for ident in (current["id"], other["id"]):
            saved = read_record(self.temp / "prog_sessions" / (ident + ".json"))
            self.assertEqual(saved["name"], "Renamed")

    def test_live_boundaries_do_not_normalize_a_session_name_while_it_is_being_typed(self):
        self.connect()
        tab = self.window._prog_tab
        self.start_session()
        self.window._nav_buttons[4].click()
        self.window.show()
        self.window.activateWindow()
        self.app.processEvents()
        for text in ("Tuesday ", ""):
            with self.subTest(text=text):
                tab.name.setFocus()
                tab.name.selectAll()
                QTest.keyClick(tab.name, Qt.Key.Key_Backspace)
                QTest.keyClicks(tab.name, text)
                tab.name.setCursorPosition(min(4, len(text)))
                self.assertTrue(tab.name.hasFocus())
                self.window._on_in_combat(True, True)
                self.line(session_ui.ability())
                self.window._on_in_combat(False, False)
                self.assertEqual(tab.name.text(), text)
                self.assertEqual(tab.name.cursorPosition(), min(4, len(text)))
                QTest.keyClick(tab.name, Qt.Key.Key_Return)
                self.assertEqual(tab.name.text(), text.strip() or tab.session["zone"])

    def test_archive_and_restore_keep_saved_notes_reviewable(self):
        self.addCleanup(self.app.clipboard().clear)
        first, other = self.marked_session(started=2), self.marked_session()
        first["pulls"][-1]["note"] = "Keep this archived note"
        tab = self.load((first, other))
        tab.archive_button.click()
        self.assertEqual(tab.picker.count(), 1)
        self.assertEqual(tab.session["id"], other["id"])
        path = self.temp / "prog_sessions" / (first["id"] + ".json")
        self.assertTrue(read_record(path)["archived"])
        tab.show_archived.setChecked(True)
        tab.picker.setCurrentIndex(tab.picker.findData(first["id"]))
        self.assertIn("Archived", tab.picker.currentText())
        self.assertEqual(tab.archive_button.text(), "Restore session")
        self.assertEqual(tab.note.toPlainText(), "Keep this archived note")
        self.assertTrue(tab.recap_button.isEnabled())
        tab.copy_pull_button.click()
        self.assertIn("Archived", self.app.clipboard().text())
        self.assertIn("Keep this archived note", self.app.clipboard().text())
        tab.archive_button.click()
        self.assertFalse(read_record(path)["archived"])
        self.assertNotIn("Archived", tab.picker.currentText())
        tab.show_archived.setChecked(False)
        self.assertEqual(tab.picker.count(), 2)
        self.assertEqual(tab.session["id"], first["id"])

    def test_archived_sessions_are_excluded_from_default_comparison_choices(self):
        selected, previous = self.marked_session(started=2), self.marked_session()
        previous["archived"] = True
        tab = self.load((selected, previous))
        tab.compare_button.click()
        self.assertEqual(tab.comparison.picker.count(), 1)
        self.assertFalse(tab.comparison.picker.isEnabled())
        tab.show_archived.setChecked(True)
        self.assertEqual(tab.comparison.picker.count(), 2)
        tab.comparison.picker.setCurrentIndex(tab.comparison.picker.findData(previous["id"]))
        self.assertIn("Archived", tab.comparison.identity.text())
        tab.show_archived.setChecked(False)
        self.assertIsNone(tab.comparison.picker.currentData())
        self.assertFalse(tab.comparison.copy_button.isEnabled())

    def test_archiving_the_last_session_explains_how_to_restore_it(self):
        record = self.marked_session()
        tab = self.load((record,))
        tab.archive_button.click()
        self.assertIsNone(tab.session)
        self.assertIn("Show archived", tab.stats.text())
        self.assertFalse(tab.archive_button.isEnabled())
        tab.show_archived.setChecked(True)
        self.assertEqual(tab.session["id"], record["id"])
        self.assertEqual(tab.archive_button.text(), "Restore session")

    def test_active_capture_cannot_be_archived(self):
        self.connect()
        tab = self.window._prog_tab
        self.start_session()
        active = tab.sessions.current
        self.assertFalse(tab.archive_button.isEnabled())
        tab.archive_button.click()
        tab.toggle_archived()
        self.assertIs(tab.sessions.current, active)
        self.assertNotIn("archived", active)

    def test_archived_pull_opens_its_saved_death_recap_after_restart(self):
        self.connect()
        tab = self.window._prog_tab
        self.start_session()
        self.window._on_in_combat(True, True)
        self.line(session_ui.ability())
        self.clock.value += 3
        self.line(["25", "ts", session_ui.PLAYER, "Player"])
        self.window._on_in_combat(False, False)
        self.end_session()
        ident, pull_id = tab.session["id"], tab.pull["id"]
        saved, errors = tab.sessions.recaps.load(ident, pull_id)
        self.assertFalse(errors)
        self.assertEqual(len(saved), 1)
        tab.archive_button.click()
        self.window._prog_sessions.close()
        self.window._prog_sessions = ProgSessions(self.temp / "prog_sessions", clock=self.clock)
        tab.sessions = self.window._prog_sessions
        tab.refresh()
        self.assertIsNone(tab.session)
        tab.show_archived.setChecked(True)
        self.assertEqual(tab.session["id"], ident)
        tab.recap_button.click()
        self.assertIn("Pull 1", self.window._recap_scope.text())
        self.assertEqual(self.window._recap_records[0]["id"], saved[0]["id"])

    def test_failed_archive_write_keeps_the_selected_session_visible(self):
        tab = self.load((self.marked_session(),))
        target = tab.session
        path = self.temp / "prog_sessions" / (target["id"] + ".json")
        backup = path.with_suffix(".saved")
        path.rename(backup)
        path.mkdir()
        tab.note.setPlainText("Latest note")
        tab.archive_button.click()
        self.assertIs(tab.session, target)
        self.assertNotIn("archived", target)
        self.assertEqual(tab.archive_button.text(), "Archive session")
        self.assertIn("could not be saved", tab.status.text())
        path.rmdir()
        backup.rename(path)
        tab.flush()
        saved = read_record(path)
        self.assertNotIn("archived", saved)
        self.assertEqual(saved["pulls"][-1]["note"], "Latest note")

    def test_archive_size_failure_reports_an_error_and_can_retry(self):
        for original in (None, True):
            with self.subTest(original=original):
                record = session([pull("p3")], started=2 if original is None else 3)
                if original is not None:
                    record["archived"] = original
                tab = self.load((record,))
                tab.show_archived.setChecked(True)
                tab.picker.setCurrentIndex(tab.picker.findData(record["id"]))
                target, selected = tab.session, tab.pull
                tab.note.setPlainText("Latest note")
                tab.flush()
                path = self.temp / "prog_sessions" / (target["id"] + ".json")
                saved = path.read_bytes()
                with patch("nyaatriggers.record_store.MAX_BYTES", len(saved)):
                    tab.archive_button.click()
                    tab.tick()
                    self.assertIs(tab.session, target)
                    self.assertIs(tab.pull, selected)
                    self.assertEqual(target.get("archived"), original)
                    self.assertEqual(path.read_bytes(), saved)
                    self.assertIn("Record is too large", tab.sessions.save_error)
                    self.assertIn("could not be saved", tab.status.text())
                tab.archive_button.click()
                restored = read_record(path)
                self.assertEqual(restored["archived"], original is not True)
                self.assertEqual(restored["pulls"][0]["note"], "Latest note")
                self.assertFalse(tab.sessions.save_error)

    def open_saved_pull(self, ident, number):
        dialog = SavedRecapDialog(self.window._prog_sessions.sessions, self.window)
        self.addCleanup(dialog.close)
        index = next(i for i, record in enumerate(dialog.sessions) if record["id"] == ident)
        dialog.session.setCurrentIndex(index)
        dialog.pulls.setCurrentRow(len(dialog.sessions[index]["pulls"]) - number)
        self.window._show_pull_recaps(*dialog.selection)
        return dialog.selection

    def test_back_from_saved_recap_reveals_the_session_hidden_by_search(self):
        target = session([pull("p1"), pull("p2"), pull("p3")])
        other = self.marked_session(started=2)
        other["name"] = "Other night"
        tab = self.load((target, other))
        tab.session_search.setText("Other night")
        tab.note.setPlainText("Preserve this pending note")
        saved_session, saved_pull, _ = self.open_saved_pull(target["id"], 2)
        self.assertIsNot(tab.session, saved_session)
        self.window._recap_back.click()
        self.assertIs(tab.session, saved_session)
        self.assertIs(tab.pull, saved_pull)
        self.assertEqual(tab.session_search.text(), "")
        self.assertFalse(tab.show_archived.isChecked())
        record = read_record(self.temp / "prog_sessions" / (other["id"] + ".json"))
        self.assertEqual(record["pulls"][-1]["note"], "Preserve this pending note")

    def test_back_from_saved_recap_reveals_the_archived_session_and_exact_pull(self):
        target = session([pull("p1"), pull("p2"), pull("p3")])
        target["archived"] = True
        other = self.marked_session(started=2)
        tab = self.load((target, other))
        tab.session_search.setText("No matching session")
        saved_session, saved_pull, _ = self.open_saved_pull(target["id"], 2)
        self.assertIsNone(tab.session)
        self.window._recap_back.click()
        self.assertIs(tab.session, saved_session)
        self.assertIs(tab.pull, saved_pull)
        self.assertEqual(tab.session_search.text(), "")
        self.assertTrue(tab.show_archived.isChecked())
        self.assertTrue(tab.session["archived"])
        self.assertEqual(self.window._stack.currentWidget(), tab)


if __name__ == "__main__":
    unittest.main()
