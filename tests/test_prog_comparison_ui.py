from copy import deepcopy
from dataclasses import replace

from PyQt6.QtWidgets import QApplication
import unittest

from nyaatriggers import theme
from nyaatriggers.locale_util import set_locale
from nyaatriggers.prog_session import ProgSessions
from nyaatriggers.prog_phases import PhaseDefinition, PhaseRule, UMAD
from nyaatriggers.record_store import read_record, write_record
from nyaatriggers.ui.prog_tab import ProgTab
from tests.test_prog_comparison import pull, session
from tests import test_session_ui as session_ui


class ComparisonUiTests(unittest.TestCase):
    setUp = session_ui.SessionUiTests.setUp
    connect = session_ui.SessionUiTests.connect
    line = session_ui.SessionUiTests.line
    phase_pull = session_ui.SessionUiTests.phase_pull

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setStyleSheet(theme.STYLESHEET)

    def load(self, records):
        for record in records:
            write_record(self.temp / "prog_sessions", record)
        self.window._prog_sessions = ProgSessions(self.temp / "prog_sessions")
        tab = self.window._prog_tab
        tab.sessions = self.window._prog_sessions
        tab.refresh()
        return tab

    def records(self):
        selected = session([pull("p3") for _ in range(6)] + [pull() for _ in range(14)] +
                           [pull("p4", "feed-lost") for _ in range(3)], started=2)
        previous = session([pull("p3") for _ in range(3)] + [pull() for _ in range(12)])
        return selected, previous

    def test_comparison_shows_samples_changes_and_exclusions(self):
        selected, previous = self.records()
        tab = self.load((selected, previous))
        self.assertIn("P3 confirmed on 6 of 20", tab.phase_progress.text())
        self.assertIn("Interrupted: 3", tab.phase_progress.text())
        tab.compare_button.click()
        comparison = tab.comparison
        self.assertEqual(comparison.picker.currentData(), previous["id"])
        self.assertEqual(comparison.table.item(6, 1).text(), "6/20 · 30.0%")
        self.assertEqual(comparison.table.item(6, 2).text(), "3/15 · 20.0%")
        self.assertEqual(comparison.table.item(6, 3).text(), "+10.0 points")
        self.assertEqual(comparison.table.item(6, 3).toolTip(), "+10.0 percentage points")
        self.assertEqual(comparison.table.item(2, 1).text(), "P3")
        self.assertIn("Interrupted: 3", comparison.notice.text())

    def test_opening_and_closing_keep_selected_pull_and_notes(self):
        tab = self.load(self.records())
        tab.table.selectRow(4)
        selected = tab.pull
        tab.note.setPlainText("Review towers")
        cursor = tab.note.textCursor()
        cursor.setPosition(4)
        tab.note.setTextCursor(cursor)
        tab.compare_button.click()
        tab.tick()
        tab.compare_button.click()
        self.assertIs(tab.pull, selected)
        self.assertEqual(tab.note.toPlainText(), "Review towers")
        self.assertEqual(tab.note.textCursor().position(), 4)
        saved = read_record(self.temp / "prog_sessions" / (tab.session["id"] + ".json"))
        self.assertEqual(saved["pulls"][-5]["note"], "Review towers")

    def test_copy_comparison_includes_identities_samples_full_units_and_exclusions(self):
        self.addCleanup(self.app.clipboard().clear)
        tab = self.load(self.records())
        self.assertFalse(tab.comparison.copy_button.isEnabled())
        tab.compare_button.click()
        tab.comparison.copy_button.click()
        copied = self.app.clipboard().text()
        self.assertIn("Selected: ", copied)
        self.assertIn("Compared: ", copied)
        self.assertIn("P3\t6/20 · 30.0%\t3/15 · 20.0%\t+10.0 percentage points", copied)
        self.assertIn("Interrupted: 3", copied)
        self.assertIn("Missing feed events", copied)
        tab.comparison.picker.setCurrentIndex(0)
        self.assertFalse(tab.comparison.copy_button.isEnabled())
        self.assertEqual(self.app.clipboard().text(), copied)

    def test_comparison_and_note_saves_preserve_an_unknown_phase_block(self):
        self.addCleanup(self.app.clipboard().clear)
        block = {"version": 999, "future": {"evidence": ["keep this", 3]}}
        selected = session([{**pull(), "phase_tracking": deepcopy(block)}], started=2)
        previous = session([pull("p3")])
        tab = self.load((selected, previous))
        tab.compare_button.click()
        tab.comparison.picker.setCurrentIndex(tab.comparison.picker.findData(previous["id"]))
        tab.note.setPlainText("Keep this note")
        tab.comparison.copy_button.click()
        saved = read_record(self.temp / "prog_sessions" / (selected["id"] + ".json"))
        self.assertEqual(saved["pulls"][0]["phase_tracking"], block)
        self.assertEqual(saved["pulls"][0]["note"], "Keep this note")
        self.assertIn("saved tracking could not be read", tab.comparison.notice.text())

    def test_readable_different_revisions_keep_counts_and_explain_unavailable_rates(self):
        revision = replace(UMAD, revision=2)
        selected = session([pull("p3")], started=2)
        previous = session([pull("p2", definition=revision)])
        tab = self.load((selected, previous))
        tab.sessions.definitions = UMAD, revision
        tab.tick()
        tab.compare_button.click()
        self.assertIsNone(tab.comparison.picker.currentData())
        tab.comparison.picker.setCurrentIndex(tab.comparison.picker.findData(previous["id"]))
        self.assertEqual(tab.comparison.table.rowCount(), 4)
        self.assertEqual(tab.comparison.table.item(0, 1).text(), "1")
        self.assertEqual(tab.comparison.table.item(0, 2).text(), "1")
        self.assertEqual(tab.comparison.table.item(3, 1).text(), "Unavailable")
        self.assertIn("tracking differs", tab.comparison.notice.text())

    def test_new_sessions_do_not_replace_a_comparison_or_an_explicit_empty_choice(self):
        selected, previous = self.records()
        tab = self.load((selected, previous))
        tab.compare_button.click()
        newer = session([pull("p4")], started=3)
        tab.sessions.sessions.insert(0, newer)
        tab.refresh()
        self.assertEqual(tab.comparison.picker.currentData(), previous["id"])
        tab.comparison.picker.setCurrentIndex(0)
        tab.tick()
        tab.compare_button.click()
        tab.compare_button.click()
        self.assertIsNone(tab.comparison.picker.currentData())
        self.assertEqual(tab.comparison.table.rowCount(), 0)
        self.assertEqual(tab.session["id"], selected["id"])

    def test_first_open_prefers_matching_phase_rules_over_a_recent_unrecorded_session(self):
        selected, previous = self.records()
        selected["started"] = 3
        legacy = session([pull(tracked=False)], started=2)
        tab = self.load((selected, previous, legacy))
        tab.compare_button.click()
        self.assertEqual(tab.comparison.picker.currentData(), previous["id"])

    def test_first_open_keeps_an_empty_choice_without_an_earlier_compatible_session(self):
        selected, _previous = self.records()
        newer = session([pull("p3")], started=3)
        legacy = session([pull(tracked=False)])
        tab = self.load((selected, newer, legacy))
        tab.picker.setCurrentIndex(tab.picker.findData(selected["id"]))
        tab.compare_button.click()
        self.assertIsNone(tab.comparison.picker.currentData())

    def test_live_pull_boundaries_preserve_a_historical_note_cursor_and_comparison(self):
        selected, previous = self.records()
        tab = self.load((selected, previous))
        self.connect()
        self.window._on_ws_zone_changed(1363, "UMAD")
        tab.start_button.click()
        tab.picker.setCurrentIndex(tab.picker.findData(selected["id"]))
        tab.table.selectRow(4)
        selected_pull = tab.pull
        tab.compare_button.click()
        tab.comparison.picker.setCurrentIndex(tab.comparison.picker.findData(previous["id"]))
        tab.note.setPlainText("Review towers")
        cursor = tab.note.textCursor()
        cursor.setPosition(4)
        tab.note.setTextCursor(cursor)
        self.window._on_in_combat(True, True)
        self.line(["20", "ts", "40001234", "Boss", "C403", "Action"])
        self.assertIs(tab.pull, selected_pull)
        self.assertEqual(tab.note.textCursor().position(), 4)
        self.assertEqual(tab.note.toPlainText(), "Review towers")
        self.window._on_in_combat(False, False)
        self.assertEqual(tab.note.textCursor().position(), 4)
        self.assertEqual(tab.comparison.picker.currentData(), previous["id"])

    def test_duty_change_clears_comparison_and_filters_active_candidates(self):
        selected, previous = self.records()
        active = session([pull("p2", "active")], started=0, state="active")
        other = session([pull(tracked=False)], zone_id=1, started=3)
        tab = self.load((selected, previous, other))
        tab.sessions.sessions.append(active)
        tab.picker.setCurrentIndex(tab.picker.findData(selected["id"]))
        tab.compare_button.click()
        self.assertEqual(tab.comparison.picker.count(), 2)
        self.assertEqual(tab.comparison.picker.currentData(), previous["id"])
        tab.picker.setCurrentIndex(tab.picker.findData(other["id"]))
        self.assertIsNone(tab.comparison.picker.currentData())
        self.assertFalse(tab.comparison.picker.isEnabled())
        self.assertIn("No other sessions", tab.comparison.notice.text())

    def test_empty_legacy_and_unreadable_phase_data_do_not_display_false_zero_rates(self):
        previous = session([pull("p3")])
        for selected in (session([pull("p4", "feed-lost")], started=2),
                         session([pull(tracked=False)], started=3),
                         session([{**pull(), "phase_tracking": {"version": 999}}], started=4)):
            with self.subTest(selected=selected["started"]):
                tab = self.load((selected, previous))
                tab.picker.setCurrentIndex(tab.picker.findData(selected["id"]))
                tab.compare_button.setChecked(True)
                tab.comparison.picker.setCurrentIndex(tab.comparison.picker.findData(previous["id"]))
                if selected["started"] == 2:
                    self.assertIn("No eligible pulls", tab.phase_progress.text())
                    self.assertEqual(tab.comparison.table.item(6, 1).text(), "No eligible pulls")
                    self.assertEqual(tab.comparison.table.item(6, 3).text(), "—")
                else:
                    self.assertEqual(tab.comparison.table.rowCount(), 4)
                    self.assertNotIn("0.0%", tab.phase_progress.text())
                    self.assertIn("unavailable" if selected["started"] == 4 else "not recorded",
                                  tab.comparison.notice.text())
                self.assertTrue(tab.note.isEnabled())
                self.assertTrue(tab.recap_button.isEnabled())

    def test_minimum_window_keeps_notes_and_comparison_reachable_in_both_languages(self):
        self.addCleanup(set_locale, "en")
        selected, previous = self.records()
        selected["name"] = "W" * 200
        previous["name"] = "前のセッション" * 20
        for locale in ("en", "ja"):
            with self.subTest(locale=locale):
                set_locale(locale)
                tab = self.load((selected, previous))
                old = tab
                tab = ProgTab(self.window, old.sessions)
                self.window._stack.removeWidget(old)
                self.window._stack.insertWidget(4, tab)
                self.window._prog_tab = tab
                old.deleteLater()
                self.window._nav_buttons[4].click()
                tab.session_search.setText("WW")
                tab.compare_button.setChecked(True)
                self.window.resize(900, 600)
                self.window.show()
                self.app.processEvents()
                self.app.processEvents()
                self.assertEqual((self.window.width(), self.window.height()), (900, 600))
                tab.content_scroll.ensureWidgetVisible(tab.note)
                self.app.processEvents()
                position = tab.note.mapTo(tab.content_scroll.viewport(), tab.note.rect().center())
                self.assertTrue(tab.content_scroll.viewport().rect().contains(position))
                for control in (tab.session_search, tab.picker, tab.compare_button,
                                tab.start_button, tab.end_button):
                    position = control.mapTo(self.window, control.rect().center())
                    self.assertTrue(self.window.rect().contains(position))
                self.assertEqual(tab.content_scroll.horizontalScrollBar().maximum(), 0)
                self.assertFalse(tab.comparison.table.isHidden())
                self.assertEqual(tab.comparison.table.verticalScrollBar().maximum(), 0)
                self.assertEqual(tab.phase_table.verticalScrollBar().maximum(), 0)
                self.assertEqual(tab.comparison.picker.count(), 2)
                self.window.grab().save(f"/tmp/nyaatriggers-comparison-{locale}.png")

    def test_long_definition_uses_page_scrolling_without_hiding_phase_rows(self):
        phases = tuple(f"p{number}" for number in range(1, 17))
        definition = PhaseDefinition("long-duty", 1, UMAD.zone_id, phases,
            tuple(PhaseRule(f"rule-{phase}", "20", 0x1000 + index, phase)
                  for index, phase in enumerate(phases)), verified=True, continuous_combat=True)
        tab = self.load((session([pull("p16", definition=definition)], started=2),
                         session([pull("p12", definition=definition)])))
        tab.sessions.definitions = (definition,)
        tab.tick()
        self.window._nav_buttons[4].click()
        tab.compare_button.click()
        self.window.resize(900, 600)
        self.window.show()
        self.app.processEvents()
        self.app.processEvents()
        self.assertEqual(tab.comparison.table.rowCount(), 20)
        self.assertEqual(tab.comparison.table.item(19, 0).text(), "P16")
        self.assertEqual(tab.phase_table.rowCount(), 16)
        self.assertEqual(tab.phase_table.item(15, 0).text(), "P16")
        self.assertEqual(tab.comparison.table.verticalScrollBar().maximum(), 0)
        self.assertEqual(tab.phase_table.verticalScrollBar().maximum(), 0)
        tab.content_scroll.ensureWidgetVisible(tab.note)
        self.app.processEvents()
        position = tab.note.mapTo(tab.content_scroll.viewport(), tab.note.rect().center())
        self.assertTrue(tab.content_scroll.viewport().rect().contains(position))


if __name__ == "__main__":
    unittest.main()
