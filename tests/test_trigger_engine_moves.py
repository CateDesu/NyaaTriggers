from contextlib import ExitStack
import json
from pathlib import Path
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QMenu

from nyaatriggers import app_common as ac
from nyaatriggers import triggevent_custom as custom
from nyaatriggers.telesto_client import TelestoClient
from nyaatriggers.trigger_engine import Trigger
from nyaatriggers.triggernometry_bridge import TriggernometryBridge
from nyaatriggers.triggernometry_editor import PackDocument, parse_xml, trigger_entries
from nyaatriggers.ui import trigger_conversion as moves
from tests import test_session_ui as fixture


class TriggerEngineMoveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture.SessionUiTests.setUpClass()

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.case = fixture.SessionUiTests()
        self.addCleanup(self.case.doCleanups)
        self.stack.enter_context(patch.object(TelestoClient, "start"))
        self.case.setUp()
        self.window = self.case.window
        self.stack.enter_context(patch.object(custom, "CUSTOM_FILE", self.case.temp / "custom.json"))
        self.packs = self.case.temp / "triggernometry-packs"
        self.stack.enter_context(patch.object(moves, "packs_dir", return_value=self.packs))
        self.stack.enter_context(patch("nyaatriggers.ui.triggernometry_editor.packs_dir", return_value=self.packs))
        self.warning = self.stack.enter_context(patch.object(moves.QMessageBox, "warning"))
        self.question = self.stack.enter_context(patch.object(moves.QMessageBox, "question",
                                                            return_value=moves.QMessageBox.StandardButton.Yes))
        self.window._triggers_enabled = False
        self.window._cactbot_mode = False
        self.window._engine_inventory = []
        self.window._custom_triggevent = []
        self.window._official_ids = set()
        self.window._official_triggers = {}
        self.window._deleted_ids = set()
        self.window._folders = []
        self.window._local_trigger_baseline = None
        self.trigger = Trigger(id="original", name="Move call", log_type="20", ability_id="B123",
                               tts_text="Boss {source}", fight="UMAD", cooldown_s=0)
        self.window._triggers = [self.trigger]
        self.window._local_ids = {self.trigger.id}
        self.assertTrue(self.window._save_triggers())
        custom.save_triggers([])
        self.assertTrue(self.window._save_settings())
        self.window._refresh_table()
        self.assertTrue(self.window._restore_tree_selection("UMAD"))

    def native_required(self):
        if not TriggernometryBridge.is_available():
            self.skipTest("Requires the native Triggernometry validator and runtime")

    def move(self, key, target):
        self.assertTrue(self.window._move_trigger_to_engine(key, target), self.warning.call_args)
        self.warning.assert_not_called()
        if target == "local":
            return self.window._triggers[0].id
        rows = [row for row in self.window._engine_inventory if row["source"] == target]
        self.assertEqual(len(rows), 1)
        return target + ":" + rows[0]["id"]

    def test_moves_between_all_three_engines_replace_the_owning_definition(self):
        self.native_required()
        key = self.trigger.id
        for target in ("triggevent", "local", "triggernometry", "triggevent", "triggernometry", "local"):
            with self.subTest(target=target):
                key = self.move(key, target)
                local = json.loads(ac.TRIGGERS_LOCAL_FILE.read_text())["triggers"]
                native = [trigger for path in self.packs.glob("*.xml")
                          for _folder, trigger, _parent in trigger_entries(parse_xml(path.read_bytes()))]
                self.assertEqual(len(local), int(target == "local"))
                self.assertEqual(len(custom.load_triggers()), int(target == "triggevent"))
                self.assertEqual(len(native), int(target == "triggernometry"))
                self.assertEqual(self.window._selected_row_key(), key)
                row = next(index for index in range(self.window._table.rowCount())
                           if self.window._table.item(index, ac._C_EN).data(Qt.ItemDataRole.UserRole) == key)
                self.assertEqual(self.window._table.item(row, ac._C_NAME).text(), "Move call")
        self.assertEqual(self.window._triggers[0].tts_text, "Boss {source}")
        self.assertEqual(self.window._triggers[0].ability_id, "B123")
        self.assertEqual(self.window._triggers[0].fight, "UMAD")
        self.question.assert_not_called()

    def test_disabled_callout_and_effective_custom_text_survive_engine_moves(self):
        self.native_required()
        self.trigger.enabled = False
        self.assertTrue(self.window._save_triggers())
        key = self.move(self.trigger.id, "triggevent")
        ident = key.split(":", 1)[1]
        self.assertIn(ident, self.window._engine_disabled["triggevent"])
        self.window._triggevent_callout_edits[ident] = "Edited {target}"
        self.window._settings["triggevent_callout_edits"] = self.window._triggevent_callout_edits
        self.assertTrue(self.window._save_settings())
        key = self.move(key, "triggernometry")
        native_id = key.split(":", 1)[1]
        self.assertIn(native_id, self.window._engine_disabled["triggernometry"])
        root = next(parse_xml(path.read_bytes()) for path in self.packs.glob("*.xml")
                    if trigger_entries(parse_xml(path.read_bytes())))
        native = trigger_entries(root)[0][1]
        self.assertEqual(native.get("Enabled"), "true")
        self.assertEqual(native.find("Actions/Action").get("UseTTSTextExpression"), "Edited ${target}")
        self.move(key, "local")
        self.assertFalse(self.window._triggers[0].enabled)
        self.assertEqual(self.window._triggers[0].tts_text, "Edited {target}")
        self.assertFalse(json.loads(ac.TRIGGERS_LOCAL_FILE.read_text())["triggers"][0]["enabled"])

    def test_unsupported_native_patterns_preserve_source_files_and_controls(self):
        self.native_required()
        self.trigger.ability_id = "B123|B124"
        self.assertTrue(self.window._save_triggers())
        key = self.move(self.trigger.id, "triggernometry")
        path = next(self.packs.glob("*.xml"))
        document = PackDocument.load(path)
        trigger = document.root.find(".//Trigger")
        pattern = trigger.get("RegularExpression")
        for expression in ("B123|B124", "(?<source>B123|B124)", "(?<id-source>B123|B124)"):
            with self.subTest(expression=expression):
                trigger.set("RegularExpression", pattern.replace("(?:B123|B124)", expression))
                self.window._save_triggernometry_document(document)
                paths = (path, ac.TRIGGERS_LOCAL_FILE, ac._SETTINGS_FILE)
                before = {path: path.read_bytes() for path in paths}
                self.assertFalse(self.window._move_trigger_to_engine(key, "local"))
                self.assertEqual({path: path.read_bytes() for path in paths}, before)
                self.assertEqual(self.window._triggers, [])
                self.assertIsNotNone(self.window._engine_entry_for_key(key))
                self.assertIn("pattern", self.warning.call_args.args[2].lower())

    def test_native_suffix_restrictions_leave_the_source_in_place(self):
        self.native_required()
        key = self.move(self.trigger.id, "triggernometry")
        path = next(self.packs.glob("*.xml"))
        document = PackDocument.load(path)
        trigger = document.root.find(".//Trigger")
        trigger.set("RegularExpression", trigger.get("RegularExpression") + r"[^|]*\|" * 20)
        self.window._save_triggernometry_document(document)
        paths = (path, ac.TRIGGERS_LOCAL_FILE, ac._SETTINGS_FILE)
        before = {path: path.read_bytes() for path in paths}
        self.assertFalse(self.window._move_trigger_to_engine(key, "local"))
        self.assertEqual({path: path.read_bytes() for path in paths}, before)
        self.assertEqual(self.window._triggers, [])
        self.assertIsNotNone(self.window._engine_entry_for_key(key))

    def test_moving_a_bundled_trigger_persists_its_source_deletion(self):
        self.window._official_ids = {self.trigger.id}
        self.window._official_triggers = {self.trigger.id: Trigger.from_dict(self.trigger.to_dict())}
        self.trigger.enabled = False
        self.assertTrue(self.window._save_triggers())
        self.move(self.trigger.id, "triggevent")
        local = json.loads(ac.TRIGGERS_LOCAL_FILE.read_text())
        self.assertEqual(local["triggers"], [])
        self.assertEqual(local["deleted"], [self.trigger.id])
        self.assertEqual(self.window._deleted_ids, {self.trigger.id})
        self.assertIn(custom.load_triggers()[0]["id"], self.window._engine_disabled["triggevent"])

    def test_approved_native_cooldown_adjustment_keeps_a_muted_source_muted(self):
        self.native_required()
        self.trigger.cooldown_s = 5
        self.trigger.enabled = False
        self.assertTrue(self.window._save_triggers())
        key = self.move(self.trigger.id, "triggernometry")
        self.question.assert_called_once()
        self.assertIn("cooldown", self.question.call_args.args[2].lower())
        self.assertIn(key.split(":", 1)[1], self.window._engine_disabled["triggernometry"])

    def test_destination_write_failure_preserves_the_source_and_live_model(self):
        before = ac.TRIGGERS_LOCAL_FILE.read_bytes()
        settings = ac._SETTINGS_FILE.read_bytes()
        with patch.object(custom, "save_triggers", side_effect=OSError("Destination is read only")):
            self.assertFalse(self.window._move_trigger_to_engine(self.trigger.id, "triggevent"))
        self.assertEqual(ac.TRIGGERS_LOCAL_FILE.read_bytes(), before)
        self.assertEqual(ac._SETTINGS_FILE.read_bytes(), settings)
        self.assertEqual(custom.load_triggers(), [])
        self.assertIs(self.window._triggers[0], self.trigger)
        self.assertTrue(self.trigger.enabled)

    def test_source_write_failure_rolls_back_the_already_saved_destination(self):
        before = {path: path.read_bytes() for path in (ac.TRIGGERS_LOCAL_FILE, custom.CUSTOM_FILE, ac._SETTINGS_FILE)}
        write = ac._atomic_write_bytes

        def fail_source(path, payload):
            if path == ac.TRIGGERS_LOCAL_FILE and payload != before[path]:
                raise OSError("Source is read only")
            write(path, payload)

        with patch.object(ac, "_atomic_write_bytes", side_effect=fail_source):
            self.assertFalse(self.window._move_trigger_to_engine(self.trigger.id, "triggevent"))
        self.assertEqual({path: path.read_bytes() for path in before}, before)
        self.assertIs(self.window._triggers[0], self.trigger)
        self.assertEqual(self.window._custom_triggevent, [])

    def test_external_source_edit_during_destination_save_is_preserved(self):
        original = ac.TRIGGERS_LOCAL_FILE.read_bytes()
        external = json.loads(original)
        external["triggers"][0]["tts_text"] = "Outside edit"
        payload = json.dumps(external).encode()
        save = custom.save_triggers

        def change_source(rows):
            save(rows)
            ac.TRIGGERS_LOCAL_FILE.write_bytes(payload)

        with patch.object(custom, "save_triggers", side_effect=change_source):
            self.assertFalse(self.window._move_trigger_to_engine(self.trigger.id, "triggevent"))
        self.assertEqual(ac.TRIGGERS_LOCAL_FILE.read_bytes(), payload)
        self.assertEqual(custom.load_triggers(), [])
        self.assertIs(self.window._triggers[0], self.trigger)

    def test_settings_write_failure_restores_both_trigger_files(self):
        before = {path: path.read_bytes() for path in (ac.TRIGGERS_LOCAL_FILE, custom.CUSTOM_FILE, ac._SETTINGS_FILE)}
        write = ac._atomic_write_bytes

        def fail_settings(path, payload):
            if path == ac._SETTINGS_FILE and payload != before[path]:
                raise OSError("Settings are read only")
            write(path, payload)

        with patch.object(ac, "_atomic_write_bytes", side_effect=fail_settings):
            self.assertFalse(self.window._move_trigger_to_engine(self.trigger.id, "triggevent"))
        self.assertEqual({path: path.read_bytes() for path in before}, before)
        self.assertIs(self.window._triggers[0], self.trigger)
        self.assertTrue(self.trigger.enabled)

    def test_rollback_does_not_overwrite_an_external_destination_change(self):
        external = custom.new_trigger("M1S")
        external.update(name="External callout", start={"event": "cast", "ids": "B124", "target": "any"})
        external["steps"][0]["text"] = "Outside edit"
        source = ac.TRIGGERS_LOCAL_FILE.read_bytes()
        write = ac._atomic_write_bytes

        def fail_source(path, payload):
            if path == ac.TRIGGERS_LOCAL_FILE and payload != source:
                custom.save_triggers([external])
                raise OSError("Source is read only")
            write(path, payload)

        with patch.object(ac, "_atomic_write_bytes", side_effect=fail_source):
            self.assertFalse(self.window._move_trigger_to_engine(self.trigger.id, "triggevent"))
        self.assertEqual(ac.TRIGGERS_LOCAL_FILE.read_bytes(), source)
        self.assertEqual(custom.load_triggers(), [external])
        self.assertIn("could not be restored", self.warning.call_args.args[2])
        self.assertIs(self.window._triggers[0], self.trigger)

    def test_destination_replaced_during_save_does_not_delete_the_source(self):
        source = ac.TRIGGERS_LOCAL_FILE.read_bytes()
        save = custom.save_triggers

        def replace_destination(rows):
            save(rows)
            save([])

        with patch.object(custom, "save_triggers", side_effect=replace_destination):
            self.assertFalse(self.window._move_trigger_to_engine(self.trigger.id, "triggevent"))
        self.assertEqual(ac.TRIGGERS_LOCAL_FILE.read_bytes(), source)
        self.assertEqual(custom.load_triggers(), [])
        self.assertIs(self.window._triggers[0], self.trigger)

    def test_unsupported_sequence_and_builtin_triggevent_keep_their_definitions(self):
        self.trigger.sequence = [{"log_type": "20", "ability_id": "B124"}]
        before = ac.TRIGGERS_LOCAL_FILE.read_bytes()
        self.assertFalse(self.window._move_trigger_to_engine(self.trigger.id, "triggevent"))
        self.assertEqual(ac.TRIGGERS_LOCAL_FILE.read_bytes(), before)
        self.assertEqual(custom.load_triggers(), [])
        self.window._engine_inventory = [{"source": "triggevent", "id": "builtin", "name": "Built in"}]
        self.assertFalse(self.window._move_trigger_to_engine("triggevent:builtin", "local"))
        self.assertEqual(ac.TRIGGERS_LOCAL_FILE.read_bytes(), before)
        self.assertEqual(self.window._engine_inventory[0]["id"], "builtin")

    def test_cooldown_adjustment_is_reviewed_before_the_source_changes(self):
        self.trigger.cooldown_s = 5
        self.assertTrue(self.window._save_triggers())
        before = ac.TRIGGERS_LOCAL_FILE.read_bytes()
        self.question.return_value = moves.QMessageBox.StandardButton.No
        self.assertFalse(self.window._move_trigger_to_engine(self.trigger.id, "triggevent"))
        self.assertIn("cooldown", self.question.call_args.args[2].lower())
        self.assertEqual(ac.TRIGGERS_LOCAL_FILE.read_bytes(), before)
        self.assertEqual(custom.load_triggers(), [])
        self.question.return_value = moves.QMessageBox.StandardButton.Yes
        self.move(self.trigger.id, "triggevent")
        self.assertEqual(self.window._triggers, [])

    def test_combat_start_during_review_prevents_the_conversion(self):
        self.trigger.cooldown_s = 5
        self.assertTrue(self.window._save_triggers())
        before = ac.TRIGGERS_LOCAL_FILE.read_bytes()

        def start_combat(*_args):
            self.window._in_game_combat = True
            return moves.QMessageBox.StandardButton.Yes

        self.question.side_effect = start_combat
        self.assertFalse(self.window._move_trigger_to_engine(self.trigger.id, "triggevent"))
        self.assertEqual(ac.TRIGGERS_LOCAL_FILE.read_bytes(), before)
        self.assertEqual(custom.load_triggers(), [])

    def test_actual_move_menu_action_converts_the_selected_local_trigger(self):
        menu = QMenu(self.window)
        self.window._add_engine_move_menu(menu, self.trigger.id)
        choices = menu.actions()[0].menu().actions()
        self.assertFalse(choices[0].isEnabled())
        self.assertEqual(choices[1].text(), "Triggevent")
        choices[1].trigger()
        self.warning.assert_not_called()
        self.assertEqual(self.window._triggers, [])
        self.assertEqual(custom.load_triggers()[0]["name"], "Move call")

    def test_complex_triggernometry_script_stays_in_its_pack(self):
        self.native_required()
        path = self.packs / "script.xml"
        path.write_bytes((Path(__file__).resolve().parents[1] / "triggernometry-core" / "test" /
                          "spike-script-pack.xml").read_bytes())
        document = PackDocument.load(path)
        ident = trigger_entries(document.root)[0][1].get("Id")
        original = path.read_bytes()
        self.window._load_cached_triggernometry_inventory()
        self.window._refresh_table()
        self.assertFalse(self.window._move_trigger_to_engine("triggernometry:" + ident + "#0", "local"))
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(self.window._triggers, [self.trigger])

    def test_native_validation_failure_keeps_the_local_source(self):
        self.native_required()
        source = ac.TRIGGERS_LOCAL_FILE.read_bytes()
        with patch.object(moves, "validate_native", side_effect=ValueError("Native validator rejected the pack")):
            self.assertFalse(self.window._move_trigger_to_engine(self.trigger.id, "triggernometry"))
        self.assertEqual(ac.TRIGGERS_LOCAL_FILE.read_bytes(), source)
        self.assertEqual(list(self.packs.glob("*.xml")), [])
        self.assertIs(self.window._triggers[0], self.trigger)

    def test_incoming_script_reference_prevents_moving_a_native_target(self):
        self.native_required()
        key = self.move(self.trigger.id, "triggernometry")
        ident = key.split(":", 1)[1].split("#", 1)[0]
        script = ET.parse(Path(__file__).resolve().parents[1] / "triggernometry-core" / "test" /
                          "spike-script-pack.xml")
        script.find(".//Action").set("ExecScriptExpression",
                                     'var action = new Triggernometry.Action { ActionType = Triggernometry.Action.ActionTypeEnum.Trigger, '
                                     'TriggerId = "' + ident + '" };')
        script.write(self.packs / "caller.xml", encoding="utf-8")
        before = {path: path.read_bytes() for path in self.packs.glob("*.xml")}
        self.assertFalse(self.window._move_trigger_to_engine(key, "local"))
        self.assertEqual({path: path.read_bytes() for path in before}, before)
        self.assertEqual(self.window._triggers, [])
        self.assertIn("script", self.warning.call_args.args[2].lower())

    def test_incoming_trigger_action_prevents_moving_a_native_target(self):
        self.native_required()
        key = self.move(self.trigger.id, "triggernometry")
        ident = key.split(":", 1)[1].split("#", 1)[0]
        caller = PackDocument(self.packs / "caller.xml")
        trigger = caller.add_trigger()
        trigger.set("RegularExpression", "CALLER")
        ET.SubElement(trigger.find("Actions"), "Action", ActionType="Trigger", TriggerId=ident)
        caller.path.write_bytes(ET.tostring(caller.root, encoding="utf-8"))
        before = {path: path.read_bytes() for path in self.packs.glob("*.xml")}
        self.assertFalse(self.window._move_trigger_to_engine(key, "local"))
        self.assertEqual({path: path.read_bytes() for path in before}, before)
        self.assertEqual(self.window._triggers, [])
        self.assertIn("references", self.warning.call_args.args[2].lower())

    def test_external_native_speech_edit_does_not_inherit_an_old_override(self):
        self.native_required()
        key = self.move(self.trigger.id, "triggernometry")
        ident = key.split(":", 1)[1]
        self.window._triggernometry_callout_edits[ident] = "Custom old"
        self.window._settings["triggernometry_callout_edits"] = self.window._triggernometry_callout_edits
        self.assertTrue(self.window._save_settings())
        path = next(self.packs.glob("*.xml"))
        tree = ET.parse(path)
        tree.find(".//Action").set("UseTTSTextExpression", "New mechanic")
        tree.write(path, encoding="utf-8")
        before = path.read_bytes()
        settings = ac._SETTINGS_FILE.read_bytes()
        self.assertFalse(self.window._move_trigger_to_engine(key, "local"))
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(ac._SETTINGS_FILE.read_bytes(), settings)
        self.assertEqual(self.window._triggers, [])
        self.assertEqual(self.window._triggernometry_callout_edits[ident], "Custom old")

    def test_removed_native_speech_row_does_not_move_another_action(self):
        self.native_required()
        key = self.move(self.trigger.id, "triggernometry")
        path = next(self.packs.glob("*.xml"))
        tree = ET.parse(path)
        ET.SubElement(tree.find(".//Actions"), "Action", OrderNumber="2", ActionType="UseTTS",
                      UseTTSTextExpression="Second call")
        tree.write(path, encoding="utf-8")
        self.window._load_cached_triggernometry_inventory()
        self.window._refresh_table()
        stale_key = key.rsplit("#", 1)[0] + "#1"
        self.assertIsNotNone(self.window._engine_entry_for_key(stale_key))
        tree.find(".//Actions").remove(tree.find(".//Actions")[1])
        tree.write(path, encoding="utf-8")
        before = path.read_bytes()
        self.assertFalse(self.window._move_trigger_to_engine(stale_key, "local"))
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(self.window._triggers, [])


if __name__ == "__main__":
    unittest.main()
