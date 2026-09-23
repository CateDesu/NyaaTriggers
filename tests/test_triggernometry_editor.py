"""Native pack editing and execution through the bundled engine."""

from copy import deepcopy
import os
import shutil
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import xml.etree.ElementTree as ET

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt6.QtCore import Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QDialog

from nyaatriggers.triggernometry_editor import PackDocument, parse_xml, trigger_entries, validate_native, xml_bytes
from nyaatriggers.triggernometry_dialog import ActionDialog, TriggernometryDialog
from nyaatriggers.ui.triggernometry_editor import TriggernometryEditorMixin, speech_rows

APP = QApplication.instance() or QApplication([])


def example(path):
    document = PackDocument(path)
    folder = document.root.find("ExportedFolder")
    folder.attrib.update(Name="UMAD", FFXIVZoneFilterEnabled="True", FfxivZoneFilterRegularExpression="^1363$")
    first = document.add_trigger()
    first.attrib.update(Name="Remember and call", RegularExpression=r"^20\|[^|]+\|[^|]+\|[^|]+\|C403\|[^|]*\|[^|]*\|(?<target>[^|]*)\|")
    second = document.add_trigger()
    second.attrib.update(Name="Follow up", Source="None")
    actions = first.find("Actions")
    ET.SubElement(actions, "Action", OrderNumber="1", ActionType="Variable", VariableOp="SetString",
                  VariableName="nyaa_editor_target", VariableExpression="${target}", Asynchronous="False")
    ET.SubElement(actions, "Action", OrderNumber="2", ActionType="Placeholder", ExecutionDelayExpression="100", Asynchronous="False")
    speech = ET.SubElement(actions, "Action", OrderNumber="3", ActionType="UseTTS",
                          UseTTSTextExpression="First ${var:nyaa_editor_target}", Asynchronous="False")
    condition = ET.SubElement(speech, "Condition", Enabled="true", Grouping="And")
    ET.SubElement(condition, "ConditionSingle", Enabled="true", ExpressionL="${var:nyaa_editor_target}",
                  ExpressionR="Player", ExpressionTypeL="String", ExpressionTypeR="String", ConditionType="StringEqualCase")
    ET.SubElement(actions, "Action", OrderNumber="4", ActionType="Trigger", TriggerOp="FireTrigger",
                  TriggerId=second.get("Id"), TriggerForce="regexp", TriggerText="${_event}", Asynchronous="False")
    actions = second.find("Actions")
    ET.SubElement(actions, "Action", OrderNumber="1", ActionType="ExecuteScript", Asynchronous="False",
                  ExecScriptExpression='Triggernometry.Interpreter.StaticHelpers.SetScalarVariable(false, "nyaa_editor_result", (40+2).ToString());')
    ET.SubElement(actions, "Action", OrderNumber="2", ActionType="UseTTS", Asynchronous="False",
                  UseTTSTextExpression="Second ${var:nyaa_editor_result} ${var:nyaa_editor_target}")
    return document


class EditorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "pack.xml"

    def test_native_validation_backup_and_external_edit_protection(self):
        document = example(self.path)
        document.save()
        first = self.path.read_bytes()
        loaded = PackDocument.load(self.path)
        loaded.root.find("ExportedFolder").set("Name", "Renamed")
        loaded.save()
        self.assertEqual(self.path.with_suffix(".xml.bak").read_bytes(), first)
        self.path.write_bytes(first)
        with self.assertRaisesRegex(ValueError, "outside the editor"):
            loaded.save()
        self.assertEqual(self.path.read_bytes(), first)

    def test_invalid_regex_and_engine_enum_never_replace_a_saved_pack(self):
        document = example(self.path)
        document.save()
        original = self.path.read_bytes()
        trigger = trigger_entries(document.root)[0][1]
        pattern = trigger.get("RegularExpression")
        for key, value in (("RegularExpression", "["), ("Source", "NotAnEngineSource")):
            trigger.set("RegularExpression", pattern)
            trigger.set(key, value)
            with self.subTest(key=key), self.assertRaises(ValueError):
                document.save()
            self.assertEqual(self.path.read_bytes(), original)

    def test_dtd_deep_xml_and_duplicate_ids_are_rejected(self):
        with self.assertRaises(ValueError):
            parse_xml(b'<!DOCTYPE x [<!ENTITY y "x">]><TriggernometryExport/>')
        with self.assertRaises(ValueError):
            parse_xml("<TriggernometryExport>" + "<x>" * 65 + "</x>" * 65 + "</TriggernometryExport>")
        document = example(self.path)
        triggers = trigger_entries(document.root)
        triggers[1][1].set("Id", triggers[0][1].get("Id"))
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            document.save()
        self.assertFalse(self.path.exists())

    def test_imported_settings_actions_comments_and_nested_conditions_survive(self):
        document = example(self.path)
        trigger = trigger_entries(document.root)[0][1]
        trigger.set("CustomFutureOption", "keep")
        trigger.append(ET.Comment("retained comment"))
        actions = trigger.find("Actions")
        ET.SubElement(actions, "Action", ActionType="LogMessage", OrderNumber="7", LogMessageText="keep")
        before = ET.tostring(actions)
        dialog = TriggernometryDialog(document)
        self.addCleanup(dialog.deleteLater)
        dialog.name.setText("Edited")
        dialog.triggers.setCurrentRow(1)
        self.assertEqual(trigger.get("Name"), "Edited")
        self.assertEqual(trigger.get("CustomFutureOption"), "keep")
        self.assertEqual(ET.tostring(actions), before)
        self.assertIn(b"retained comment", xml_bytes(document.root))
        duplicate = document.add_trigger(trigger)
        self.assertNotEqual(duplicate.get("Id"), trigger.get("Id"))
        self.assertEqual(ET.tostring(duplicate.find("Actions")), before)

    def test_action_editor_preserves_engine_defaults_and_unknown_attributes(self):
        action = ET.Element("Action", ActionType="Variable", FutureOption="keep")
        dialog = ActionDialog(action, [])
        self.addCleanup(dialog.deleteLater)
        dialog.accept()
        self.assertEqual(dialog.element.get("Asynchronous"), "True")
        self.assertEqual(dialog.element.get("VariableOp"), "Unset")
        self.assertEqual(dialog.element.get("FutureOption"), "keep")
        self.assertEqual(action.attrib, {"ActionType": "Variable", "FutureOption": "keep"})

    def test_chain_action_requires_an_explicit_target(self):
        document = example(self.path)
        dialog = ActionDialog(ET.Element("Action", ActionType="Trigger"), trigger_entries(document.root))
        self.addCleanup(dialog.deleteLater)
        self.assertEqual(dialog.fields["TriggerId"].currentData(), "")
        with patch("nyaatriggers.triggernometry_dialog.QMessageBox.warning") as warning:
            dialog.accept()
        warning.assert_called_once()
        self.assertNotEqual(dialog.result(), QDialog.DialogCode.Accepted)
        dialog.fields["TriggerId"].setCurrentIndex(2)
        dialog.accept()
        self.assertEqual(dialog.element.get("TriggerId"), trigger_entries(document.root)[1][1].get("Id"))

    def test_validator_preserves_unicode_and_does_not_execute_scripts(self):
        document = example(self.path)
        trigger = trigger_entries(document.root)[1][1]
        trigger.set("Name", "日本語 🐈")
        marker = self.path.with_suffix(".executed")
        filename = str(marker).replace('"', '""')
        trigger.find("Actions/Action").set("ExecScriptExpression", 'System.IO.File.WriteAllText(@"' + filename + '", "executed");')
        document.save()
        self.assertFalse(marker.exists())
        self.assertEqual(trigger_entries(PackDocument.load(self.path).root)[1][1].get("Name"), "日本語 🐈")

    def test_fight_popup_keeps_the_selected_zone(self):
        for keyboard in (False, True):
            document = example(self.path)
            dialog = TriggernometryDialog(document)
            self.addCleanup(dialog.deleteLater)
            dialog.show()
            dialog.activateWindow()
            self.assertTrue(QTest.qWaitForWindowActive(dialog, 2000))
            dialog.fight.setFocus()
            dialog.fight.selectAll()
            QTest.keyClicks(dialog.fight, "fru")
            APP.processEvents()
            popup = dialog.fight.completer().popup()
            if keyboard:
                QTest.keyClick(popup, Qt.Key.Key_Down)
                QTest.keyClick(popup, Qt.Key.Key_Return)
            else:
                QTest.mouseClick(popup.viewport(), Qt.MouseButton.LeftButton,
                                 pos=popup.visualRect(popup.model().index(0, 0)).center())
            APP.processEvents()
            folder = document.root.find("ExportedFolder")
            self.assertEqual((folder.get("Name"), folder.get("FfxivZoneFilterRegularExpression")), ("FRU", "^1238$"))
            dialog.fight.setFocus()
            QTest.keyClick(dialog.fight, Qt.Key.Key_Backspace)
            self.assertEqual(folder.get("Name"), "Unsorted")
            self.assertEqual(folder.get("FFXIVZoneFilterEnabled"), "False")
            dialog.close()

    def test_save_failure_keeps_edits_open(self):
        dialog = TriggernometryDialog(example(self.path), save=Mock(side_effect=OSError("Read only")))
        self.addCleanup(dialog.deleteLater)
        dialog.name.setText("Keep these edits")
        with patch("nyaatriggers.triggernometry_dialog.QMessageBox.warning") as warning:
            dialog.accept()
        warning.assert_called_once()
        self.assertNotEqual(dialog.result(), QDialog.DialogCode.Accepted)
        self.assertEqual(dialog.name.text(), "Keep these edits")

    def test_referenced_trigger_cannot_be_deleted_from_a_saved_pack(self):
        document = example(self.path)
        document.save()
        before = self.path.read_bytes()
        _path, target, parent = trigger_entries(document.root)[1]
        parent.remove(target)
        with self.assertRaisesRegex(ValueError, "still referenced"):
            document.save()
        self.assertEqual(self.path.read_bytes(), before)

    def test_referenced_new_trigger_cannot_be_deleted_before_saving(self):
        for saved in (False, True):
            with self.subTest(saved=saved):
                path = self.path.with_name(f"new-{saved}.xml")
                document = example(path)
                if saved:
                    document.save()
                before = path.read_bytes() if saved else None
                target = document.add_trigger()
                target.attrib.update(Name="New target", Source="None")
                first = trigger_entries(document.root)[0][1]
                ET.SubElement(first.find("Actions"), "Action", ActionType="Trigger",
                              TriggerOp="FireTrigger", TriggerId=target.get("Id"))
                parent = next(parent for _path, item, parent in trigger_entries(document.root) if item is target)
                parent.remove(target)
                with self.assertRaisesRegex(ValueError, "still referenced"):
                    document.save()
                self.assertEqual(path.read_bytes() if path.exists() else None, before)

    def test_duplicate_remaps_equivalent_self_references(self):
        document = example(self.path)
        first = trigger_entries(document.root)[0][1]
        original_id = first.get("Id")
        first.set("Id", original_id.upper())
        action = ET.SubElement(first.find("Actions"), "Action", ActionType="Trigger",
                               TriggerOp="DisableTrigger", TriggerId=original_id)
        duplicate = document.add_trigger(first)
        copied = duplicate.findall("Actions/Action")[-1]
        self.assertEqual(copied.get("TriggerId"), duplicate.get("Id"))
        self.assertEqual(action.get("TriggerId"), original_id)

    def test_inventory_opens_equivalent_guid_and_selects_the_target(self):
        class Host(TriggernometryEditorMixin, SimpleNamespace):
            pass
        for compact in (False, True):
            with self.subTest(compact=compact):
                path = self.path.with_name(f"guid-{compact}.xml")
                document = example(path)
                target = trigger_entries(document.root)[1][1]
                ident = target.get("Id")
                target.set("Id", ident.replace("-", "").upper() if compact else "{" + ident.upper() + "}")
                document.save()
                host = Host(_open_triggernometry_document=Mock())
                with patch("nyaatriggers.ui.triggernometry_editor.pack_paths", return_value=[path]), \
                        patch("nyaatriggers.ui.triggernometry_editor.QMessageBox.warning") as warning:
                    host._edit_triggernometry_definition("triggernometry:" + ident + "#0")
                warning.assert_not_called()
                host._open_triggernometry_document.assert_called_once()
                selected_document, selected_id = host._open_triggernometry_document.call_args.args
                dialog = TriggernometryDialog(selected_document, selected_id=selected_id)
                self.addCleanup(dialog.deleteLater)
                self.assertEqual(dialog.name.text(), "Follow up")

    def test_equivalent_chain_target_keeps_its_name_in_the_editor(self):
        document = example(self.path)
        first, second = trigger_entries(document.root)
        action = first[1].find("Actions/Action[@ActionType='Trigger']")
        action.set("TriggerId", "{" + second[1].get("Id").upper() + "}")
        dialog = TriggernometryDialog(document)
        self.addCleanup(dialog.deleteLater)
        self.assertIn("Follow up", dialog.actions.item(3).text())
        action_dialog = ActionDialog(action, trigger_entries(document.root))
        self.addCleanup(action_dialog.deleteLater)
        self.assertIn("Follow up", action_dialog.fields["TriggerId"].currentText())
        self.assertEqual(action_dialog.fields["TriggerId"].count(), 3)
        action_dialog.accept()
        self.assertEqual(action_dialog.element.get("TriggerId"), second[1].get("Id"))

    def test_deleted_reference_check_accepts_equivalent_guid_spelling(self):
        document = example(self.path)
        document.save()
        first, second = trigger_entries(document.root)
        first[1].find("Actions/Action[@ActionType='Trigger']").set("TriggerId", "{" + second[1].get("Id").upper() + "}")
        second[2].remove(second[1])
        with self.assertRaisesRegex(ValueError, "still referenced"):
            document.save()

    def test_reordering_actions_uses_execution_order_and_preserves_conditions(self):
        document = example(self.path)
        trigger = trigger_entries(document.root)[0][1]
        actions = trigger.find("Actions")
        actions[:] = list(reversed(list(actions)))
        dialog = TriggernometryDialog(document)
        self.addCleanup(dialog.deleteLater)
        self.assertEqual([a.get("OrderNumber") for a in dialog.action_elements], ["1", "2", "3", "4"])
        speech = dialog.action_elements[2]
        condition = ET.tostring(speech.find("Condition"))
        dialog.actions.setCurrentRow(2)
        dialog.move_action(-1)
        self.assertEqual([a.get("ActionType") for a in trigger.findall("Actions/Action")],
                         ["Variable", "UseTTS", "Placeholder", "Trigger"])
        self.assertEqual(ET.tostring(speech.find("Condition")), condition)
        validate_native(xml_bytes(document.root))

    def test_window_save_shows_unsorted_and_defers_reload_without_losing_muting(self):
        from tests.test_session_ui import SessionUiTests
        case = SessionUiTests()
        case.setUp()
        try:
            from nyaatriggers import app_common as ac
            case.stack.enter_context(patch.object(ac, "_TRIGGERNOMETRY_INVENTORY_CACHE", case.temp / "inventory.json"))
            window = case.window
            case.connect()
            document = example(case.temp / "pack.xml")
            document.root.find("ExportedFolder").set("Name", "Unsorted")
            first = trigger_entries(document.root)[0][1]
            speech = ET.SubElement(first.find("Actions"), "Action", OrderNumber="5", ActionType="UseTTS", UseTTSTextExpression="Third")
            document.save()
            rows = speech_rows(document.root)
            first_id = first.get("Id") + "#0"
            second_id = first.get("Id") + "#1"
            window._triggernometry_disabled.add(first_id)
            window._triggernometry_callout_edits[first_id] = "Original override"
            window._in_game_combat = True
            with patch.object(window, "_set_triggernometry_enabled") as enabled:
                window._save_triggernometry_document(document)
                enabled.assert_not_called()
                self.assertIn(first_id, window._triggernometry_disabled)
                self.assertNotIn(second_id, window._triggernometry_disabled)
                self.assertEqual(window._triggernometry_callout_edits[first_id], "Original override")
                self.assertTrue(window._restore_tree_selection("", "custom_group"))
                self.assertTrue(any(row.get("source") == "triggernometry" for row in window._engine_inventory))
                self.assertEqual(window._engine_fight_tag(rows[0]), "")
                speech.set("UseTTSTextExpression", "Changed")
                window._save_triggernometry_document(document)
                self.assertNotIn(first_id, window._triggernometry_callout_edits)
                self.assertIn(second_id, window._triggernometry_disabled)
                window._on_in_combat(False, False)
                self.assertEqual([call.args[0] for call in enabled.call_args_list], [False, True])
        finally:
            case.doCleanups()

    @unittest.skipUnless(os.name == "posix" and shutil.which("mono") and shutil.which("xvfb-run"), "Requires Mono and Xvfb")
    def test_generated_pack_runs_variables_conditions_delays_chains_and_scripts(self):
        from tests.test_triggernometry_host import replay
        document = example(self.path)
        document.save()
        with replay(self.path) as host:
            host.send(t="zone", id=1363, name="Dancing Mad (Ultimate)")
            host.log("20", "40001234", "Kefka", "C403", "Cast", "10001234", "Player", "4.7")
            self.assertEqual(host.call(), "First Player")
            self.assertEqual(host.call(), "Second 42 Player")
            host.log("20", "40001234", "Kefka", "C403", "Cast", "10001235", "Other", "4.7")
            self.assertEqual(host.call(), "Second 42 Other")
            self.assertEqual(host.calls, ["First Player", "Second 42 Player", "Second 42 Other"])

    def test_reload_waits_for_combat_and_respects_cactbot(self):
        class Host(TriggernometryEditorMixin, SimpleNamespace):
            pass
        host = Host(_triggernometry_reload_pending=True, _in_game_combat=True,
                    _triggers_enabled=True, _cactbot_mode=False, _set_triggernometry_enabled=Mock())
        host._apply_pending_triggernometry_packs()
        host._set_triggernometry_enabled.assert_not_called()
        host._in_game_combat = False
        host._apply_pending_triggernometry_packs()
        self.assertEqual([call.args[0] for call in host._set_triggernometry_enabled.call_args_list], [False, True])
        host._set_triggernometry_enabled.reset_mock()
        host._triggernometry_reload_pending = True
        host._cactbot_mode = True
        host._apply_pending_triggernometry_packs()
        host._set_triggernometry_enabled.assert_called_once_with(False)

    def test_old_engine_inventory_cannot_replace_saved_edits(self):
        from nyaatriggers.triggernometry_bridge import TriggernometryBridge
        from nyaatriggers.ui.engines import EnginesMixin
        bridge = TriggernometryBridge()
        bridge._active = True
        bridge._gen = 2
        received = []
        bridge.inventory.connect(lambda payload, generation: received.append((payload, generation)))
        bridge._dispatch({"t": "inventory", "triggers": []}, 1)
        self.assertEqual(received, [])
        bridge._dispatch({"t": "inventory", "triggers": []}, 2)
        self.assertEqual(received, [("[]", 2)])
        state = SimpleNamespace(_triggernometry=bridge, _engine_inventory=[{"id": "keep"}])
        EnginesMixin._on_triggernometry_inventory(state, "[]", 1)
        self.assertEqual(state._engine_inventory, [{"id": "keep"}])
        state._triggernometry_reload_pending = True
        EnginesMixin._on_triggernometry_inventory(state, "[]", 2)
        self.assertEqual(state._engine_inventory, [{"id": "keep"}])
        bridge._active = False


if __name__ == "__main__":
    unittest.main()
