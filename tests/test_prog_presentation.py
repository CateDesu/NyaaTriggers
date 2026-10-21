from dataclasses import replace
from datetime import datetime
import json
import unittest
from unittest.mock import patch

from PyQt6.QtCore import Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QTableWidget

from nyaatriggers.time_format import format_datetime, format_time, set_clock_format
from nyaatriggers.ui.prog_tab import PHASE_PERCENT_COLUMN
from nyaatriggers.ui.recap_browser import SavedRecapDialog
from nyaatriggers.ui.settings_tab import SettingsTabMixin
from nyaatriggers.main_window import MainWindow
from nyaatriggers.prog_session import ProgSessions
from tests import test_prog_progress_ui as fixture
from tests.test_session_features import BOSS, PLAYER, ability


class ClockTests(unittest.TestCase):
    def tearDown(self):
        set_clock_format("12")

    def test_midnight_noon_and_afternoon_in_both_clock_formats(self):
        for hour, expected in ((0, "12:05:09 AM"), (12, "12:05:09 PM"), (21, "9:05:09 PM")):
            stamp = datetime(2026, 10, 7, hour, 5, 9).timestamp()
            set_clock_format("12")
            self.assertEqual(format_time(stamp), expected)
            self.assertEqual(format_datetime(stamp), "10/7/2026 " + expected)
            set_clock_format("24")
            self.assertEqual(format_time(stamp), f"{hour:02d}:05:09")
            self.assertEqual(format_time(stamp, seconds=False), f"{hour:02d}:05")

    def test_invalid_settings_fall_back_to_twelve_hours(self):
        stamp = datetime(2026, 10, 7, 21, 5, 9).timestamp()
        for value in (None, 24, "invalid", [], {}):
            set_clock_format(value)
            self.assertEqual(format_time(stamp), "9:05:09 PM")


class PresentationTests(unittest.TestCase):
    setUpClass = classmethod(fixture.ProgressUiTests.setUpClass.__func__)
    setUp = fixture.ProgressUiTests.setUp
    cleanup_case = fixture.ProgressUiTests.cleanup_case
    history = fixture.ProgressUiTests.history

    def test_each_pull_uses_its_own_furthest_phase_hp_and_capture_time(self):
        self.history()
        self.assertEqual(self.tab.table.item(0, PHASE_PERCENT_COLUMN).text(), "Not recorded")
        self.assertEqual(self.tab.table.item(1, PHASE_PERCENT_COLUMN).text(), "60.00%")
        self.tab.table.selectRow(1)
        self.assertEqual(self.tab.phase_table.item(0, 3).text(), format_datetime(1700100007))
        self.assertEqual(self.tab.phase_table.item(1, 3).text(), "Not recorded")
        self.assertEqual(self.tab.milestone_table.item(0, 2).text(), "35.00%")

    def test_clock_setting_refreshes_saved_dates_and_persists_without_changing_durations(self):
        self.history()
        selected = self.tab.pull
        duration = self.tab.table.item(0, 2).text()
        self.assertEqual(self.window._clock_format_combo.currentData(), "12")
        self.assertRegex(self.tab.table.item(0, 1).text(), r" (AM|PM)$")
        self.window._clock_format_combo.setCurrentIndex(1)
        self.assertIs(self.tab.pull, selected)
        self.assertEqual(self.tab.table.item(0, 1).text(),
                         datetime.fromtimestamp(selected["started"]).strftime("%H:%M:%S"))
        self.assertEqual(self.tab.table.item(0, 2).text(), duration)
        self.assertEqual(self.tab.milestone_table.item(0, 1).text(), format_datetime(1700000005))
        self.assertEqual(json.loads((self.case.temp / "settings.json").read_text())["clock_format"], "24")
        self.window._clock_format_combo.setCurrentIndex(0)
        self.assertRegex(self.tab.table.item(0, 1).text(), r" (AM|PM)$")
        self.assertEqual(json.loads((self.case.temp / "settings.json").read_text())["clock_format"], "12")

    def test_clock_setting_keeps_the_selected_dps_pull_and_its_details(self):
        self.case.connect()
        for minute, seconds, damage in ((28, 12, "3E80000"), (29, 24, "BB80000")):
            stamp = datetime(2026, 10, 7, 21, minute, 14).timestamp()
            with patch("nyaatriggers.ui.dps_tab.time.time", return_value=stamp):
                self.window._on_in_combat(True, True)
                self.case.line(ability(PLAYER, BOSS, [("03", damage)]))
                self.case.clock.value += seconds
                self.case.line(ability(PLAYER, BOSS, [("03", damage)]))
                self.window._on_in_combat(False, False)
        history = self.window._dps_history
        self.assertEqual([entry["snapshot"]["Encounter"]["damage"] for entry in history], [6000, 2000])
        self.window.resize(1500, 900)
        self.window.show()
        self.window._nav_buttons[2].click()
        self.case.app.processEvents()
        listing = self.window._dps_history_list
        position = listing.visualItemRect(listing.item(1)).center()
        QTest.mouseClick(listing.viewport(), Qt.MouseButton.LeftButton, pos=position)
        self.assertEqual(listing.currentRow(), 1)
        self.assertEqual(self.window._dps_selected_idx, 1)
        self.assertEqual(self.window._dps_live_title.text(), "Reviewing: Test duty (00:12, 9:28:14 PM)")
        self.assertEqual(self.window._dps_live_table.item(0, 2).text(), "167")
        for choice, expected in ((1, "21:28:14"), (0, "9:28:14 PM")):
            with self.subTest(choice=choice):
                self.window._clock_format_combo.setCurrentIndex(choice)
                self.assertEqual(listing.currentRow(), 1)
                self.assertEqual(self.window._dps_selected_idx, 1)
                self.assertIs(self.window._dps_history, history)
                self.assertIn(expected, listing.currentItem().text())
                self.assertEqual(self.window._dps_live_title.text(), f"Reviewing: Test duty (00:12, {expected})")
                self.assertEqual(self.window._dps_live_table.item(0, 2).text(), "167")

    def test_furthest_phase_hp_and_dates_remain_specific_to_the_saved_pull(self):
        self.history()
        session = self.tab.session
        pull = session["pulls"][0]
        definition = replace(self.sessions.definitions[0],
                             bosses=(("p1", (1234,)), ("p3", (5678,))))
        stamp = datetime(2026, 10, 7, 21, 25).timestamp()
        pull["started"] = stamp
        tracking = pull["phase_tracking"]
        tracking["observations"][0]["observed_at"] = stamp + 5
        tracking["observations"].append(
            {"phase": "p3", "at": 9, "rule": "phase-2", "observed_at": stamp + 139})
        tracking["boss_hp"] = {
            "p1": {"npc_id": 1234, "current": 125, "maximum": 1000},
            "p3": {"npc_id": 5678, "current": 437, "maximum": 1000}}
        self.assertTrue(self.sessions.save(session))
        self.sessions.close()
        self.sessions = ProgSessions(self.case.temp / "prog_sessions", definitions=(definition,))
        self.addCleanup(self.sessions.close)
        self.window._prog_sessions = self.sessions
        self.tab.sessions = self.sessions
        self.tab.refresh()
        self.assertEqual(self.tab.table.item(0, PHASE_PERCENT_COLUMN).text(), "Not recorded")
        self.assertEqual(self.tab.table.item(1, PHASE_PERCENT_COLUMN).text(), "43.70%")
        self.assertEqual(self.tab.milestone_table.item(0, 2).text(), "12.50%")
        self.assertEqual(self.tab.milestone_table.item(2, 2).text(), "43.70%")
        self.tab.table.selectRow(1)
        self.assertEqual(self.tab.phase_table.item(0, 3).text(), "10/7/2026 9:25:05 PM")
        self.assertEqual(self.tab.phase_table.item(2, 3).text(), "10/7/2026 9:27:19 PM")
        self.assertEqual(self.tab.phase_table.item(1, 3).text(), "Not recorded")
        self.tab.pull["phase_tracking"]["boss_hp"].pop("p3")
        self.tab.tick()
        self.assertEqual(self.tab.table.item(1, PHASE_PERCENT_COLUMN).text(), "Not recorded")

    def test_gridless_tables_and_compact_columns_with_room_for_timestamps(self):
        self.history()
        self.window._nav_buttons[4].click()
        self.window.resize(1500, 900)
        self.window.show()
        self.case.app.processEvents()
        for table in self.window.findChildren(QTableWidget):
            table.ensurePolished()
            self.assertFalse(table.showGrid(), table.objectName())
        for column in (1, 2):
            self.assertGreaterEqual(self.tab.table.columnWidth(column), 190)
        self.assertLess(self.tab.table.columnWidth(5), 250)
        self.assertLess(self.tab.phase_table.columnWidth(2), 250)
        self.assertGreaterEqual(self.tab.phase_table.columnWidth(1),
                                self.tab.phase_table.horizontalHeader().sectionSizeHint(1))
        self.assertEqual(self.tab.phase_table.verticalScrollBar().maximum(), 0)

    def test_clock_choice_loads_on_restart_and_updates_other_timestamp_displays(self):
        self.history()
        self.tab.show_archived.setChecked(True)
        stamp = datetime(2026, 10, 7, 21, 28, 14).timestamp()
        self.window._dps_history = [{"snapshot": self.window._dps_meter.snapshot(), "when": stamp}]
        self.window._recap_visible = [{"when": stamp, "name": "Player", "zone": "Duty"}]
        self.window._recap_list.clear()
        self.window._recap_list.addItem("")
        self.window._clock_format_combo.setCurrentIndex(1)
        self.assertIn("21:28:14", self.window._dps_history_list.item(0).text())
        self.assertIn("21:28:14", self.window._recap_list.item(0).toolTip())
        self.assertNotRegex(self.tab.comparison.picker.itemText(1), r" (AM|PM)")
        with patch.object(MainWindow, "_load_settings", SettingsTabMixin._load_settings):
            restored = MainWindow()
        self.addCleanup(restored._prog_sessions.close)
        self.addCleanup(restored.close)
        self.assertEqual(restored._clock_format_combo.currentData(), "24")
        self.window._clock_format_combo.setCurrentIndex(0)
        self.assertIn("9:28:14 PM", self.window._dps_history_list.item(0).text())
        self.assertIn("9:28:14 PM", self.window._recap_list.item(0).toolTip())
        self.assertRegex(self.tab.comparison.picker.itemText(1), r" (AM|PM)")
        dialog = SavedRecapDialog(self.sessions.sessions, self.window)
        self.addCleanup(dialog.close)
        self.assertRegex(dialog.session.itemText(0), r" (AM|PM)")
        self.assertRegex(dialog.pulls.item(0).text(), r" (AM|PM)")


if __name__ == "__main__":
    unittest.main()
