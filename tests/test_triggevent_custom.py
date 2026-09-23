"""Builder definitions, safe saves, editing and engine delivery."""

from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt6.QtWidgets import QApplication, QDialog
from PyQt6.QtCore import Qt
from PyQt6.QtTest import QTest

from nyaatriggers import triggevent_custom as custom
from nyaatriggers.triggevent_bridge import TriggeventBridge
from nyaatriggers.triggevent_dialog import TriggeventDialog
from nyaatriggers.ui.custom_triggevent import CustomTriggeventMixin

APP = QApplication.instance() or QApplication([])


def example():
    data = custom.new_trigger("Example fight", 123)
    data["name"] = "Debuff direction"
    data["start"]["ids"] = "A123 | A124"
    data["steps"] = [
        {"kind": "wait", "event": "cast", "ids": "B123", "target": "any"},
        {"kind": "callout", "condition": "start_id", "ids": "A123",
         "text": "{start.target}, go left", "otherwise": "{start.target}, go right"},
    ]
    return data


def validation_cases():
    cases = []

    def add(label, valid, change):
        row = example()
        change(row)
        cases.append({"label": label, "valid": valid, "definition": row})

    for char in (" ", "\t", "\n", "\x1c", "\x85", "\u00a0", "\u2007", "\u202f", "\u3000"):
        add(f"ID spacing U+{ord(char):04X}", True,
            lambda row, char=char: row["start"].update(ids=f"A123{char}|{char}A124"))
    for length, valid in ((100, True), (101, False)):
        add(f"Emoji name length {length}", valid, lambda row, length=length: row.update(name="🐈" * length))
    for length, valid in ((1000, True), (1001, False)):
        add(f"Emoji speech length {length}", valid,
            lambda row, length=length: row["steps"][1].update(text="🐈" * length))
    for value in (None, {}, [], 123, "x" * 513):
        add(f"Unused IDs {type(value).__name__}", False,
            lambda row, value=value: row["steps"][1].update(condition="always", ids=value))
    for timeout, valid in ((4.2, False), (4.3, True)):
        add(f"Delay total with timeout {timeout}", valid,
            lambda row, timeout=timeout: row.update(timeout_s=timeout, steps=[
                {"kind": "delay", "seconds": 0.1}, {"kind": "delay", "seconds": 4.1}, row["steps"][1],
            ]))
    return cases


class Host(CustomTriggeventMixin, SimpleNamespace):
    pass


class CustomTriggerTests(unittest.TestCase):
    def test_focused_fight_popup_keeps_mouse_and_keyboard_selections(self):
        for keyboard in (False, True):
            with self.subTest(keyboard=keyboard):
                dialog = TriggeventDialog(trigger=example())
                self.addCleanup(dialog.deleteLater)
                dialog.show()
                dialog.activateWindow()
                self.assertTrue(QTest.qWaitForWindowActive(dialog, 2000))
                dialog.fight.setFocus()
                dialog.fight.selectAll()
                QTest.keyClicks(dialog.fight, "fru")
                APP.processEvents()
                popup = dialog.fight.completer().popup()
                index = popup.model().index(0, 0)
                self.assertTrue(index.isValid())
                if keyboard:
                    QTest.keyClick(popup, Qt.Key.Key_Down)
                    QTest.keyClick(popup, Qt.Key.Key_Return)
                else:
                    QTest.mouseClick(popup.viewport(), Qt.MouseButton.LeftButton,
                                     pos=popup.visualRect(index).center())
                APP.processEvents()
                self.assertEqual((dialog.get_trigger()["fight"], dialog.get_trigger()["zone_id"]),
                                 ("FRU", 1238))
                dialog.fight.setFocus()
                QTest.keyClick(dialog.fight, Qt.Key.Key_Backspace)
                self.assertEqual((dialog.get_trigger()["fight"], dialog.get_trigger()["zone_id"]), ("", 0))
                dialog.close()

    def test_validation_boundaries_shared_with_the_engine(self):
        for case in validation_cases():
            with self.subTest(case=case["label"]):
                if case["valid"]:
                    custom.validate_trigger(case["definition"])
                    dialog = TriggeventDialog(trigger=case["definition"])
                    self.addCleanup(dialog.deleteLater)
                    self.assertEqual(dialog.get_trigger(), case["definition"])
                else:
                    with self.assertRaises(ValueError):
                        custom.validate_trigger(case["definition"])

    def test_saved_sequence_roundtrip_and_corrupt_file_protection(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "custom.json"
            row = example()
            custom.save_triggers([row], path)
            self.assertEqual(custom.load_triggers(path), [row])
            path.write_text('{"version":2,"triggers":[]}')
            with self.assertRaises(ValueError):
                custom.save_triggers([row], path)
            self.assertEqual(json.loads(path.read_text())["version"], 2)

    def test_reject_unusable_conditions_ids_timeouts_and_steps(self):
        changes = [
            lambda row: row["start"].update(ids="A1|"),
            lambda row: row["start"].update(target="start_target"),
            lambda row: row.update(timeout_s=float("nan")),
            lambda row: row.update(timeout_s=10 ** 400),
            lambda row: row.update(zone_id=-1),
            lambda row: row["steps"][1].update(condition="script"),
            lambda row: row["steps"][1].update(text="{bad}"),
            lambda row: row["steps"][1].update(text="{target"),
            lambda row: row.update(steps=row["steps"][:1]),
            lambda row: row["steps"].insert(0, {"kind": "delay", "seconds": 120}),
        ]
        for change in changes:
            row = example()
            change(row)
            with self.subTest(row=row), self.assertRaises(ValueError):
                custom.validate_trigger(row)

    def test_oversized_save_preserves_a_readable_collection(self):
        rows = []
        for index in range(63):
            row = custom.new_trigger()
            row["name"] = f"Test {index}"
            row["start"]["ids"] = "1"
            row["steps"] = [{"kind": "callout", "condition": "always", "ids": "",
                             "text": "x" * 1950, "otherwise": ""} for _ in range(32)]
            rows.append(row)
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "custom.json"
            custom.save_triggers(rows[:62], path)
            before = path.read_bytes()
            self.assertLessEqual(len(before), custom.MAX_BYTES)
            with self.assertRaisesRegex(ValueError, "too large"):
                custom.save_triggers(rows, path)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(custom.load_triggers(path), rows[:62])
            custom.save_triggers(rows[:61], path)
            self.assertEqual(len(custom.load_triggers(path)), 61)

    def test_size_limit_counts_written_unicode_bytes(self):
        row = example()
        row["steps"][1]["text"] = "右" * 2000
        with tempfile.TemporaryDirectory() as temp, patch.object(custom, "MAX_BYTES", 7500):
            path = Path(temp) / "custom.json"
            custom.save_triggers([row], path)
            self.assertLessEqual(path.stat().st_size, custom.MAX_BYTES)
            self.assertEqual(custom.load_triggers(path), [row])

    def test_equal_delay_total_is_rejected_before_saving(self):
        row = example()
        row["timeout_s"] = 4.2
        row["steps"] = [{"kind": "delay", "seconds": value} for value in (0.1, 4.1)] + [row["steps"][1]]
        saved = Mock()
        dialog = TriggeventDialog(trigger=row, save=saved)
        self.addCleanup(dialog.deleteLater)
        with patch("nyaatriggers.triggevent_dialog.QMessageBox.warning") as warning:
            dialog.accept()
        warning.assert_called_once()
        saved.assert_not_called()
        self.assertNotEqual(dialog.result(), QDialog.DialogCode.Accepted)
        dialog.timeout.setValue(4.3)
        dialog.accept()
        saved.assert_called_once()
        self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)

    def test_dialog_edits_reorders_and_preserves_branch_text(self):
        row = example()
        dialog = TriggeventDialog(trigger=row)
        self.addCleanup(dialog.deleteLater)
        self.assertEqual(dialog.get_trigger(), row)
        dialog.add_step({"kind": "delay", "seconds": 2.5})
        delay = dialog.steps[-1]
        dialog.move_step(delay, -1)
        self.assertEqual([s["kind"] for s in dialog.get_trigger()["steps"]], ["wait", "delay", "callout"])
        dialog.remove_step(delay)
        dialog.steps[1].text.setText("{player}, stack")
        self.assertEqual(dialog.get_trigger()["steps"][1]["otherwise"], row["steps"][1]["otherwise"])
        self.assertEqual(row, example_with_id(row["id"]))

    def test_save_failure_keeps_editor_and_unsaved_text_open(self):
        dialog = TriggeventDialog(trigger=example(), save=Mock(side_effect=OSError("Read only")))
        self.addCleanup(dialog.deleteLater)
        with patch("nyaatriggers.triggevent_dialog.QMessageBox.warning") as warning:
            dialog.accept()
        warning.assert_called_once()
        self.assertNotEqual(dialog.result(), QDialog.DialogCode.Accepted)
        self.assertEqual(dialog.steps[1].text.text(), "{start.target}, go left")

    def test_fight_search_accepts_names_and_tags_and_fills_the_zone(self):
        dialog = TriggeventDialog()
        self.addCleanup(dialog.deleteLater)
        self.assertEqual((dialog.get_trigger()["fight"], dialog.zone.text()), ("", "0"))
        completer = dialog.fight.completer()
        for query, tag, zone in (("fru", "FRU", 1238), ("omega protocol", "TOP", 1122)):
            completer.setCompletionPrefix(query)
            matches = completer.completionModel()
            self.assertEqual(matches.rowCount(), 1)
            completer.activated[str].emit(matches.index(0, 0).data())
            data = dialog.get_trigger()
            self.assertEqual((data["fight"], data["zone_id"]), (tag, zone))
        dialog.fight.clear()
        self.assertEqual((dialog.get_trigger()["fight"], dialog.zone.text()), ("", "0"))
        dialog.fight.setText("An unfinished search")
        self.assertEqual(dialog.get_trigger()["fight"], "")

    def test_fights_with_multiple_zones_have_distinct_results(self):
        choices = [row for row in custom.fight_choices() if row["fight"] == "Eureka Orthos"]
        self.assertGreater(len(choices), 1)
        self.assertEqual(len({row["label"] for row in choices}), len(choices))
        dialog = TriggeventDialog(trigger=example())
        self.addCleanup(dialog.deleteLater)
        dialog.fight.completer().activated[str].emit(choices[-1]["label"])
        self.assertEqual(dialog.get_trigger()["zone_id"], choices[-1]["zone_id"])

    def test_saving_preserves_other_definitions_and_detects_external_edit(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(custom, "CUSTOM_FILE", Path(temp) / "custom.json"):
            original, other = example(), example()
            custom.save_triggers([original, other])
            host = Host(_custom_triggevent=[], _src_collapsed={}, _sync_custom_triggevent=Mock(),
                        _refresh_table=Mock(), _show_custom_triggevent_status=Mock())
            edited = deepcopy(original)
            edited["name"] = "Edited"
            host._save_custom_triggevent(edited, original)
            self.assertEqual(custom.load_triggers(), [edited, other])
            with self.assertRaises(ValueError):
                host._save_custom_triggevent(original, original)
            self.assertEqual(custom.load_triggers(), [edited, other])

    def test_inventory_merges_once_and_deleted_rows_do_not_return_from_cache(self):
        row = example()
        host = Host(_custom_triggevent=[row], _engine_inventory=[
            {"source": "triggevent", "id": "built-in"},
            {"source": "triggernometry", "id": "pack"},
            {"source": "triggevent", "id": "nyaa:deleted"}])
        host._merge_custom_triggevent_inventory()
        host._merge_custom_triggevent_inventory()
        self.assertEqual([r["id"] for r in host._engine_inventory], ["built-in", "pack", row["id"]])

    def test_disabled_definitions_and_cactbot_mode_are_not_sent(self):
        enabled, disabled = example(), example()
        bridge = Mock()
        bridge.supports_custom_triggers.return_value = True
        host = Host(_custom_triggevent=[enabled, disabled], _triggevent=bridge,
                    _triggevent_mode=True, _cactbot_mode=False,
                    _engine_disabled={"triggevent": {disabled["id"]}})
        host._sync_custom_triggevent()
        bridge.set_custom_triggers.assert_called_with([enabled])
        host._cactbot_mode = True
        host._sync_custom_triggevent()
        bridge.set_custom_triggers.assert_called_with([])

    def test_bridge_capability_and_stale_acknowledgements(self):
        bridge = TriggeventBridge()
        bridge._gen = 4
        bridge._active = True
        bridge._custom_gen = 3
        self.assertFalse(bridge.supports_custom_triggers())
        bridge._custom_gen = 4
        bridge.set_custom_triggers([example()])
        self.assertEqual(json.loads(bridge._wq.get_nowait())["nyaa_cmd"], "custom_triggers")
        seen = []
        bridge.custom_status.connect(lambda *args: seen.append(args))
        bridge._dispatch({"t": "custom_triggers", "ok": False, "message": "bad"}, gen=3)
        bridge._dispatch({"t": "custom_triggers", "ok": True}, gen=4)
        self.assertEqual(seen, [(True, "", 4)])

    def test_main_window_add_edit_duplicate_delete_and_fight_selection(self):
        from tests.test_session_ui import SessionUiTests
        from nyaatriggers.app_common import _C_EN
        from PyQt6.QtCore import Qt
        from PyQt6.QtWidgets import QMessageBox

        with tempfile.TemporaryDirectory() as temp, patch.object(custom, "CUSTOM_FILE", Path(temp) / "custom.json"):
            fixture = SessionUiTests()
            fixture.setUpClass()
            try:
                fixture.setUp()
                window = fixture.window
                window._current_fight_tag = "TOP"
                window._current_zone_id = 1122
                self.assertIn("Triggevent callout...", [a.text() for a in window._add_btn.menu().actions()])

                def fill(dialog):
                    definition = example()
                    self.assertEqual((dialog.get_trigger()["fight"], dialog.zone.text()), ("", "0"))
                    dialog.name.setText(definition["name"])
                    label = next(label for label, choice in dialog._fight_choices.items() if choice["fight"] == "FRU")
                    dialog.fight.completer().activated[str].emit(label)
                    dialog.start.ids.setText(definition["start"]["ids"])
                    for row in list(dialog.steps):
                        dialog.remove_step(row)
                    for step in definition["steps"]:
                        dialog.add_step(step)
                    dialog.accept()
                    return dialog.result()

                with patch.object(TriggeventDialog, "exec", fill):
                    window._add_custom_triggevent()
                saved = custom.load_triggers()[0]
                key = "triggevent:" + saved["id"]
                self.assertEqual(window._tree.currentItem().data(0, Qt.ItemDataRole.UserRole), "FRU")
                self.assertEqual(saved["zone_id"], 1238)
                self.assertEqual(window._selected_row_key(), key)
                row = window._table.currentRow()
                self.assertFalse(window._table.isRowHidden(row))
                self.assertEqual(window._table.item(row, _C_EN).data(Qt.ItemDataRole.UserRole), key)

                def rename(dialog):
                    dialog.name.setText("Changed in editor")
                    dialog.accept()
                    return dialog.result()

                with patch.object(TriggeventDialog, "exec", rename):
                    window._edit_trigger()
                self.assertEqual(custom.load_triggers()[0]["name"], "Changed in editor")
                with patch.object(TriggeventDialog, "exec", lambda dialog: dialog.accept() or dialog.result()):
                    window._duplicate_trigger()
                rows = custom.load_triggers()
                self.assertEqual(len(rows), 2)
                self.assertNotEqual(rows[0]["id"], rows[1]["id"])
                with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes):
                    window._delete_trigger()
                self.assertEqual(len(custom.load_triggers()), 1)

                unassigned = custom.load_triggers()[0]
                window._save_custom_triggevent({**unassigned, "fight": "", "zone_id": 0}, unassigned)
                from nyaatriggers.app_common import _ITEM_TYPE_ROLE
                current = window._tree.currentItem()
                self.assertEqual(current.data(0, _ITEM_TYPE_ROLE), "custom_group")
                self.assertEqual(current.parent().data(0, _ITEM_TYPE_ROLE), "custom_hdr")
                row = window._table.currentRow()
                self.assertFalse(window._table.isRowHidden(row))
                window._tree.setCurrentItem(window._tree.topLevelItem(0))
                self.assertTrue(window._table.isRowHidden(row))
                window._search_edit.setText("Changed in editor")
                window._apply_tab_filter()
                self.assertFalse(window._table.isRowHidden(row))
            finally:
                fixture.doCleanups()

    def test_general_controls_do_not_change_unsorted_builder_callouts(self):
        from tests.test_session_ui import SessionUiTests
        from nyaatriggers.app_common import _C_EN
        from PyQt6.QtCore import Qt

        with tempfile.TemporaryDirectory() as temp, patch.object(custom, "CUSTOM_FILE", Path(temp) / "custom.json"):
            unassigned, assigned = example(), example()
            unassigned.update(fight="", zone_id=0)
            assigned.update(fight="FRU", zone_id=1238)
            custom.save_triggers([unassigned, assigned])
            fixture = SessionUiTests()
            fixture.setUpClass()
            try:
                fixture.setUp()
                window = fixture.window
                window._engine_inventory = [{"source": "triggevent", "id": "builtin-general",
                                             "name": "Built in General", "fight": "", "group": ""}]
                window._triggevent = Mock()
                window._triggevent_mode = True
                window._refresh_table()
                window._restore_tree_selection("")
                window._update_fight_controls()
                key = "triggevent:" + unassigned["id"]
                index = next(i for i in range(window._table.rowCount())
                             if window._table.item(i, _C_EN).data(Qt.ItemDataRole.UserRole) == key)
                self.assertTrue(window._table.isRowHidden(index))
                self.assertEqual(window._fight_tv_ids(""), ["builtin-general"])
                self.assertEqual(window._fight_tv_ids("FRU"), [assigned["id"]])
                window._cb_tv.click()
                self.assertEqual(window._engine_disabled["triggevent"], {"builtin-general"})
                window._triggevent.set_custom_triggers.assert_called_with([unassigned, assigned])
                window._cb_tv.click()
                window._engine_disabled["triggevent"].add(unassigned["id"])
                window._update_fight_controls()
                self.assertTrue(window._cb_tv.isChecked())
                window._set_fight_tv("FRU", False)
                window._triggevent.set_custom_triggers.assert_called_with([])
            finally:
                fixture.doCleanups()


def example_with_id(ident):
    data = example()
    data["id"] = ident
    return data


if __name__ == "__main__":
    unittest.main()
