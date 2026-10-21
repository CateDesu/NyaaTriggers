from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from PyQt6.QtCore import QPoint, Qt
from PyQt6.QtTest import QSignalSpy, QTest

from nyaatriggers import app_common as ac
from nyaatriggers.main_window import MainWindow
from nyaatriggers.ui.settings_tab import SettingsTabMixin
from tests import test_trigger_group_controls as groups


class TriggerTextTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        groups.TriggerGroupControlTests.setUpClass()

    def setUp(self):
        self.case = groups.TriggerGroupControlTests()
        self.addCleanup(self.case.doCleanups)
        self.case.setUp()
        self.window = self.case.window
        self.window.resize(1500, 800)
        self.window.show()
        self.case.case.app.processEvents()

    def cell(self, key, column=ac._C_TEXT):
        table = self.window._table
        row = next(row for row in range(table.rowCount())
                   if table.item(row, ac._C_EN).data(Qt.ItemDataRole.UserRole) == key)
        return table.item(row, column)

    def test_dragging_dividers_changes_space_and_saves_widths(self):
        table = self.window._table
        header = table.horizontalHeader()
        saved_signal = QSignalSpy(self.window._settings_save_timer.timeout)
        for column in (ac._C_NAME, ac._C_TTS):
            before = table.columnWidth(column)
            edge = header.sectionViewportPosition(column) + before - 1
            start = QPoint(edge, header.height() // 2)
            end = start - QPoint(80, 0)
            QTest.mousePress(header.viewport(), Qt.MouseButton.LeftButton, pos=start)
            QTest.mouseMove(header.viewport(), end)
            QTest.mouseRelease(header.viewport(), Qt.MouseButton.LeftButton, pos=end)
            self.assertLess(table.columnWidth(column), before - 60)
        if self.window._settings_save_timer.isActive():
            self.assertTrue(saved_signal.wait(2000), "Column widths must save after dragging")
        self.assertGreater(len(saved_signal), 0, "Dragging must schedule a settings save")
        saved = self.case.settings()["trigger_column_widths"]
        self.assertEqual(saved, [table.columnWidth(ac._C_NAME), table.columnWidth(ac._C_TTS)])
        self.window._refresh_table()
        self.assertEqual(saved, [table.columnWidth(ac._C_NAME), table.columnWidth(ac._C_TTS)])
        with patch.object(MainWindow, "_load_settings", SettingsTabMixin._load_settings):
            reopened = MainWindow()
        self.addCleanup(reopened._prog_sessions.close)
        self.addCleanup(reopened.close)
        self.assertEqual(saved, [reopened._table.columnWidth(ac._C_NAME),
                                 reopened._table.columnWidth(ac._C_TTS)])

    def test_local_text_reaches_plugin_without_changing_speech_and_reloads(self):
        trigger = self.window._triggers[0]
        trigger.tts_text = "Spoken {target}"
        self.window._refresh_table()
        self.assertEqual(self.cell(trigger.id).text(), "Spoken {target}")
        self.window._set_display_text(trigger.id, "Move away, {target}")
        with patch("nyaatriggers.main_window.speak") as speech, \
                patch.object(self.window._plugin_link, "send_alert") as display:
            self.window._fire(trigger, {"target": "Alice"})
        self.assertEqual(speech.call_args.args[0], "Spoken Alice")
        display.assert_called_once_with("Move away, Alice", "info")
        self.assertEqual(self.cell(trigger.id, ac._C_TTS).text(), "Spoken {target}")
        SettingsTabMixin._load_settings(self.window)
        self.window._init_engines()
        self.window._refresh_table()
        self.assertEqual(self.cell(trigger.id).text(), "Move away, {target}")
        self.window._set_display_text(trigger.id, "")
        self.assertEqual(self.cell(trigger.id).text(), "Spoken {target}")
        self.assertNotIn(trigger.id, self.case.settings()["trigger_display_edits"])

    def test_display_only_local_log_dispatch_respects_local_mode(self):
        trigger = self.window._triggers[0]
        trigger.ability_id = "C0DE"
        trigger.tts_text = ""
        trigger.sound_file = ""
        trigger.cooldown_s = 0
        self.window._set_display_text(trigger.id, "Overlay {target} from {source}")
        self.window._set_local_enabled(True)
        with patch("nyaatriggers.main_window.speak") as speech, \
                patch("nyaatriggers.main_window.play_sound") as sound, \
                patch.object(self.window._plugin_link, "send_alert") as display:
            self.window._ws.log_line.emit(
                "20|ts|40000001|Distinct Boss|C0DE|Overlay cast|10000001|Distinct Player")
            display.assert_called_once_with("Overlay Distinct Player from Distinct Boss", "info")
            self.window._set_local_enabled(False)
            self.window._ws.log_line.emit(
                "20|ts|40000001|Distinct Boss|C0DE|Overlay cast|10000002|Other Player")
            self.case.case.app.processEvents()
            display.assert_called_once_with("Overlay Distinct Player from Distinct Boss", "info")
            speech.assert_not_called()
            sound.assert_not_called()

    def test_text_double_click_edits_correct_row_and_search_matches_it(self):
        key = "triggernometry:tn-umad#0"
        self.window._triggernometry = SimpleNamespace(set_display_text=Mock())
        self.addCleanup(setattr, self.window, "_triggernometry", None)
        with patch("nyaatriggers.ui.triggers_tab.QInputDialog.getMultiLineText",
                   return_value=("Show ${_me.name}", True)) as editor:
            table = self.window._table
            item = self.cell(key)
            table.scrollToItem(item)
            self.case.case.app.processEvents()
            point = table.visualItemRect(item).center()
            QTest.mouseClick(table.viewport(), Qt.MouseButton.LeftButton, pos=point)
            QTest.mouseDClick(table.viewport(), Qt.MouseButton.LeftButton, pos=point)
        editor.assert_called_once()
        self.window._triggernometry.set_display_text.assert_called_once_with("tn-umad#0", "Show ${_me.name}")
        self.window._search_edit.setText("Show")
        self.window._apply_tab_filter()
        self.assertFalse(self.window._table.isRowHidden(self.cell(key).row()))
        self.assertTrue(self.window._table.isRowHidden(self.cell("local-umad").row()))
        self.window._triggernometry = None

    def test_engine_display_and_speech_are_separate_through_edit_reset_and_replay(self):
        bridge = Mock()
        self.window._triggevent = bridge
        self.addCleanup(setattr, self.window, "_triggevent", None)
        self.window._engine_inventory[0].update(tts="Spoken default", text="Visual default")
        self.window._refresh_table()
        key = "triggevent:tv-umad"
        self.assertEqual(self.cell(key).text(), "Visual default")
        self.assertEqual(self.cell(key, ac._C_TTS).text(), "Spoken default")
        self.window._set_display_text(key, "Visual {event.target}")
        bridge.set_callout.assert_called_with("tv-umad", text="Visual {event.target}")
        bridge.cancel_speech.assert_not_called()
        self.window._set_triggevent_callout_edit("tv-umad", "New speech")
        bridge.set_callout.assert_called_with("tv-umad", tts="New speech", text="Visual {event.target}")
        self.window._reset_triggevent_callout_edit("tv-umad")
        bridge.set_callout.assert_called_with("tv-umad", text="Visual {event.target}")
        bridge.reset_mock()
        self.window._replay_triggevent_callout_edits()
        bridge.set_callout.assert_called_with("tv-umad", text="Visual {event.target}")
        self.window._set_display_text(key, "")
        bridge.set_callout.assert_called_with("tv-umad", text="Visual default")
        self.window._triggevent = None


if __name__ == "__main__":
    unittest.main()
