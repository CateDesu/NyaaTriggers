from contextlib import ExitStack
import json
import os
import shutil
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

from PyQt6.QtCore import Qt
from PyQt6.QtTest import QSignalSpy
from PyQt6.QtWidgets import QTreeWidgetItemIterator

from nyaatriggers import app_common as ac
from nyaatriggers.telesto_client import TelestoClient
from nyaatriggers.triggernometry_editor import PackDocument, xml_bytes
from nyaatriggers.ui import engines
from tests import test_session_ui as fixture


class TriggernometrySectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture.SessionUiTests.setUpClass()

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.case = fixture.SessionUiTests()
        self.addCleanup(self.case.doCleanups)
        with patch.object(TelestoClient, "start"):
            self.case.setUp()
        self.window = self.case.window
        self.window._triggers = []
        self.window._local_ids = set()
        self.window._official_ids = set()
        self.window._folders = []
        self.window._custom_triggevent = []
        self.window._engine_inventory = []
        self.window._src_collapsed = {"engine": True, "triggernometry": True}
        self.window._refresh_table()
        self.cache = self.case.temp / "triggernometry-inventory.json"
        self.stack.enter_context(patch.object(ac, "_TRIGGERNOMETRY_INVENTORY_CACHE", self.cache))
        self.packs = self.case.temp / "packs"
        self.packs.mkdir()
        self.source = self.case.temp / "import.xml"
        self.document = PackDocument(self.source)
        self.document.root.find("ExportedFolder").set("Name", "UMAD")
        self.trigger = self.document.add_trigger()
        self.trigger.set("Name", "Imported callout")
        self.trigger.set("RegularExpression", "TEST")
        ET.SubElement(self.trigger.find("Actions"), "Action", OrderNumber="1", ActionType="UseTTS",
                      UseTTSTextExpression="Spread ${_me.name}")
        self.key = "triggernometry:" + self.trigger.get("Id") + "#0"
        self.stack.enter_context(patch.object(ac.QFileDialog, "getOpenFileName",
                                             return_value=(str(self.source), "")))
        self.stack.enter_context(patch.object(engines, "_tn_packs_dir", return_value=self.packs))
        self.stack.enter_context(patch.object(engines.TriggernometryBridge, "is_available", return_value=True))
        self.stack.enter_context(patch("nyaatriggers.triggernometry_editor.validate_native"))
        self.stack.enter_context(patch.object(ac.QMessageBox, "question",
                                             return_value=ac.QMessageBox.StandardButton.Yes))
        self.stack.enter_context(patch.object(ac.QMessageBox, "information"))
        self.critical = self.stack.enter_context(patch.object(ac.QMessageBox, "critical"))
        self.enable = self.stack.enter_context(patch.object(self.window, "_set_triggernometry_enabled"))

    def import_pack(self):
        self.source.write_bytes(xml_bytes(self.document.root))
        self.window._import_triggernometry()
        self.critical.assert_not_called()

    def row(self, key):
        return next(row for row in range(self.window._table.rowCount())
                    if self.window._table.item(row, ac._C_EN).data(Qt.ItemDataRole.UserRole) == key)

    def visible_keys(self):
        return [self.window._table.item(row, ac._C_EN).data(Qt.ItemDataRole.UserRole)
                for row in range(self.window._table.rowCount())
                if not self.window._table.isRowHidden(row)]

    def select(self, fight, kind=None):
        self.assertTrue(self.window._restore_tree_selection(fight, kind))
        self.window._apply_tab_filter()

    def test_every_fight_has_matching_empty_sections(self):
        iterator = QTreeWidgetItemIterator(self.window._tree)
        checked = set()
        while iterator.value():
            item = iterator.value()
            fight = item.data(0, Qt.ItemDataRole.UserRole)
            if fight is not None and item.data(0, ac._ITEM_TYPE_ROLE) is None:
                self.window._tree.setCurrentItem(item)
                self.window._apply_tab_filter()
                with self.subTest(fight=fight):
                    self.assertEqual(self.visible_keys(), ["__hdr__:engine", "__hdr__:triggernometry"])
                    for key in self.visible_keys():
                        self.assertTrue(self.window._table.item(self.row(key), ac._C_NAME).text().startswith("▶"))
                checked.add(fight)
            iterator += 1
        self.assertTrue(ac._TREE_FIGHTS.issubset(checked))

    def test_import_populates_section_before_engine_inventory_and_survives_reload(self):
        for enabled in (False, True):
            with self.subTest(editable_callouts=enabled):
                self.window._triggers_enabled = enabled
                self.window._cactbot_mode = not enabled
                self.select("UMAD")
                self.import_pack()
                self.assertEqual(self.visible_keys(), ["__hdr__:engine", "__hdr__:triggernometry", self.key])
                self.assertEqual(self.window._triggers, [])
                cell = self.window._table.item(self.row(self.key), ac._C_EN)
                self.assertEqual(cell.data(ac._SECTION_ROLE), "triggernometry")
                self.assertEqual(cell.checkState(), Qt.CheckState.Checked)
                cached = json.loads(self.cache.read_text())
                self.assertEqual(cached[0]["id"], self.key.split(":", 1)[1])
                self.window._engine_inventory = []
                self.window._load_cached_triggernometry_inventory()
                self.window._refresh_table()
                self.assertIn(self.key, self.visible_keys())
                if not enabled:
                    self.enable.assert_not_called()

    def test_collapse_and_search_keep_sources_and_fights_separate(self):
        self.window._triggers_enabled = False
        self.window._engine_inventory = [{"source": "triggevent", "id": "other", "fight": "M1S",
                                          "name": "Another fight", "text": "Stack"}]
        self.select("UMAD")
        self.import_pack()
        header = self.row("__hdr__:triggernometry")
        self.window._table.cellClicked.emit(header, ac._C_NAME)
        self.assertNotIn(self.key, self.visible_keys())
        self.assertIn("__hdr__:triggernometry", self.visible_keys())
        filtered = QSignalSpy(self.window._tab_filter_timer.timeout)
        self.window._search_edit.setText("Imported callout")
        self.assertTrue(filtered.wait(1000))
        self.assertEqual(self.visible_keys(), [self.key])
        self.window._search_edit.clear()
        self.assertTrue(filtered.wait(1000))
        self.assertNotIn(self.key, self.visible_keys())
        self.window._table.cellClicked.emit(header, ac._C_NAME)
        self.assertIn(self.key, self.visible_keys())
        self.select("M1S")
        self.assertNotIn(self.key, self.visible_keys())

    def test_reimport_replaces_pack_rows_and_preserves_other_packs_and_choices(self):
        self.window._triggers_enabled = False
        second = self.document.add_trigger()
        second.set("RegularExpression", "SECOND")
        ET.SubElement(second.find("Actions"), "Action", ActionType="UseTTS", UseTTSTextExpression="Removed call")
        removed_key = "triggernometry:" + second.get("Id") + "#0"
        self.import_pack()
        other = PackDocument(self.packs / "other.xml")
        other.root.find("ExportedFolder").set("Name", "M1S")
        other_trigger = other.add_trigger()
        other_trigger.set("RegularExpression", "OTHER")
        ET.SubElement(other_trigger.find("Actions"), "Action", ActionType="UseTTS", UseTTSTextExpression="Keep")
        other.path.write_bytes(xml_bytes(other.root))
        other_id = other_trigger.get("Id") + "#0"
        self.window._engine_inventory.append({"source": "triggernometry", "id": other_id,
                                               "name": "Keep", "fight": "M1S", "text": "Keep"})
        ident = self.key.split(":", 1)[1]
        self.window._triggernometry_disabled.add(ident)
        self.window._triggernometry_callout_edits[ident] = "Custom wording"
        self.document.root.find("ExportedFolder/Triggers").remove(second)
        self.import_pack()
        self.select("UMAD")
        self.assertNotIn(removed_key, self.visible_keys())
        rows = {row["id"]: row for row in self.window._engine_inventory}
        self.assertEqual(set(rows), {ident, other_id})
        self.assertEqual(rows[ident]["text"], "Spread ${_me.name}")
        self.assertEqual(self.window._table.item(self.row(self.key), ac._C_TTS).text(), "Custom wording")
        self.assertEqual(self.window._table.item(self.row(self.key), ac._C_EN).checkState(), Qt.CheckState.Unchecked)

    def test_changed_speech_cannot_inherit_another_actions_wording_or_enable_a_muted_trigger(self):
        self.window._triggers_enabled = False
        self.import_pack()
        ident = self.key.split(":", 1)[1]
        self.window._triggernometry_disabled.add(ident)
        self.window._triggernometry_callout_edits[ident] = "Custom spread"
        actions = self.trigger.find("Actions")
        actions[0].set("OrderNumber", "2")
        actions.insert(0, ET.Element("Action", OrderNumber="1", ActionType="UseTTS", UseTTSTextExpression="Stack"))
        self.import_pack()
        self.assertNotIn(ident, self.window._triggernometry_callout_edits)
        expected = {self.trigger.get("Id") + "#0", self.trigger.get("Id") + "#1"}
        self.assertTrue(expected.issubset(self.window._triggernometry_disabled))
        saved = json.loads((self.case.temp / "settings.json").read_text())
        self.assertTrue(expected.issubset(saved["triggernometry_disabled_triggers"]))
        self.assertNotIn(ident, saved["triggernometry_callout_edits"])

    def test_reimport_after_external_deletion_removes_cached_speech_and_triggers(self):
        self.window._triggers_enabled = False
        ET.SubElement(self.trigger.find("Actions"), "Action", ActionType="UseTTS", UseTTSTextExpression="Later")
        removed = self.document.add_trigger()
        removed.set("RegularExpression", "REMOVED")
        ET.SubElement(removed.find("Actions"), "Action", ActionType="UseTTS", UseTTSTextExpression="Removed")
        self.import_pack()
        managed = self.packs / self.source.name
        self.trigger.find("Actions").remove(self.trigger.findall("Actions/Action")[1])
        self.document.root.find("ExportedFolder/Triggers").remove(removed)
        managed.write_bytes(xml_bytes(self.document.root))
        with patch.object(ac.QFileDialog, "getOpenFileName", return_value=(str(managed), "")):
            self.window._import_triggernometry()
        self.critical.assert_not_called()
        self.assertEqual([row["id"] for row in self.window._engine_inventory], [self.key.split(":", 1)[1]])
        self.window._engine_inventory = []
        self.window._load_cached_triggernometry_inventory()
        self.assertEqual([row["id"] for row in self.window._engine_inventory], [self.key.split(":", 1)[1]])

    def test_folder_names_with_path_separators_are_literal(self):
        self.window._triggers_enabled = False
        self.document.root.find("ExportedFolder").set("Name", "M1S / M2S")
        self.import_pack()
        self.assertEqual(self.window._engine_inventory[0]["fight"], "M1S / M2S")

    def test_bad_unrelated_pack_does_not_keep_deleted_triggers_or_erase_other_controls(self):
        self.window._triggers_enabled = False
        removed = self.document.add_trigger()
        removed.set("RegularExpression", "REMOVED")
        ET.SubElement(removed.find("Actions"), "Action", ActionType="UseTTS", UseTTSTextExpression="Removed")
        self.import_pack()
        other_id = "other-pack#0"
        self.window._engine_inventory.append({"source": "triggernometry", "id": other_id, "text": "Keep"})
        self.window._triggernometry_disabled.add(other_id)
        self.window._triggernometry_callout_edits[other_id] = "Custom keep"
        (self.packs / "bad.xml").write_bytes(b"<broken")
        self.document.root.find("ExportedFolder/Triggers").remove(removed)
        managed = self.packs / self.source.name
        managed.write_bytes(xml_bytes(self.document.root))
        with patch.object(ac.QFileDialog, "getOpenFileName", return_value=(str(managed), "")):
            self.window._import_triggernometry()
        self.critical.assert_not_called()
        self.assertEqual([row["id"] for row in self.window._engine_inventory], [self.key.split(":", 1)[1]])
        self.assertIn(other_id, self.window._triggernometry_disabled)
        self.assertEqual(self.window._triggernometry_callout_edits[other_id], "Custom keep")

    def test_cache_reload_observes_removed_packs_and_changed_action_positions(self):
        self.window._triggers_enabled = False
        self.import_pack()
        ident = self.key.split(":", 1)[1]
        self.window._triggernometry_disabled.add(ident)
        self.window._triggernometry_callout_edits[ident] = "Custom spread"
        actions = self.trigger.find("Actions")
        actions.insert(0, ET.Element("Action", ActionType="UseTTS", UseTTSTextExpression="Stack"))
        managed = self.packs / self.source.name
        managed.write_bytes(xml_bytes(self.document.root))
        self.window._engine_inventory = []
        self.window._load_cached_triggernometry_inventory()
        self.assertEqual([row["text"] for row in self.window._engine_inventory], ["Stack", "Spread ${_me.name}"])
        self.assertNotIn(ident, self.window._triggernometry_callout_edits)
        self.assertIn(self.trigger.get("Id") + "#1", self.window._triggernometry_disabled)
        self.window._triggernometry_callout_edits[ident] = "Custom stack"
        self.window._load_cached_triggernometry_inventory()
        self.assertEqual(self.window._triggernometry_callout_edits[ident], "Custom stack")
        managed.unlink()
        self.window._load_cached_triggernometry_inventory()
        self.assertEqual(self.window._engine_inventory, [])

    def test_live_inventory_reconciles_external_action_changes_before_replaying_controls(self):
        self.window._triggers_enabled = False
        self.import_pack()
        ident = self.key.split(":", 1)[1]
        self.window._triggernometry_disabled.add(ident)
        self.window._triggernometry_callout_edits[ident] = "Custom spread"
        changed = [{"id": ident, "text": "Stack"},
                   {"id": self.trigger.get("Id") + "#1", "text": "Spread ${_me.name}"}]
        with patch.object(self.window, "_apply_engine_disabled") as apply, \
                patch.object(self.window, "_replay_triggernometry_callout_edits") as replay:
            self.window._on_triggernometry_inventory(json.dumps(changed))
        apply.assert_called_once_with("triggernometry")
        replay.assert_called_once()
        self.assertNotIn(ident, self.window._triggernometry_callout_edits)
        self.assertIn(self.trigger.get("Id") + "#1", self.window._triggernometry_disabled)

    def test_repaired_packs_reconcile_controls_using_the_last_readable_baseline(self):
        self.window._triggers_enabled = False
        ident = self.key.split(":", 1)[1]
        for recovery in ("startup", "import"):
            with self.subTest(recovery=recovery):
                self.import_pack()
                self.window._triggernometry_disabled.add(ident)
                self.window._triggernometry_callout_edits[ident] = "Custom spread"
                managed = self.packs / self.source.name
                managed.write_bytes(b"<broken")
                self.window._load_cached_triggernometry_inventory()
                self.assertEqual(self.window._engine_inventory, [])
                self.assertEqual(self.window._triggernometry_callout_edits[ident], "Custom spread")
                actions = self.trigger.find("Actions")
                inserted = ET.Element("Action", ActionType="UseTTS", UseTTSTextExpression="Stack")
                actions.insert(0, inserted)
                managed.write_bytes(xml_bytes(self.document.root))
                if recovery == "startup":
                    self.window._load_cached_triggernometry_inventory()
                else:
                    with patch.object(ac.QFileDialog, "getOpenFileName", return_value=(str(managed), "")):
                        self.window._import_triggernometry()
                self.assertNotIn(ident, self.window._triggernometry_callout_edits)
                self.assertIn(self.trigger.get("Id") + "#1", self.window._triggernometry_disabled)
                actions.remove(inserted)
                self.window._triggernometry_disabled.clear()
                self.window._triggernometry_callout_edits.clear()

    @unittest.skipUnless(os.name == "posix" and shutil.which("mono") and shutil.which("xvfb-run"), "Requires Mono and Xvfb")
    def test_imported_listing_agrees_with_native_ids_and_literal_folder_names(self):
        from tests.test_triggernometry_host import replay
        self.window._triggers_enabled = False
        self.document.root.find("ExportedFolder").set("Name", "M1S / M2S")
        self.trigger.attrib.pop("Id")
        self.import_pack()
        managed = self.packs / self.source.name
        preview = self.window._engine_inventory
        self.assertEqual(len(preview), 1)
        self.assertEqual(preview[0]["fight"], "M1S / M2S")
        with replay(managed) as host:
            self.assertEqual(host.inventory[0]["id"], preview[0]["id"])
            self.assertEqual(host.inventory[0]["fight"], preview[0]["fight"])
        self.import_pack()
        self.assertEqual(list(self.packs.glob("*.xml")), [managed])
        self.assertEqual(self.window._engine_inventory, preview)

    def test_unassigned_import_appears_in_unsorted_triggernometry_section(self):
        self.window._triggers_enabled = False
        self.document.root.find("ExportedFolder").set("Name", "Unsorted")
        self.import_pack()
        self.select("", "custom_group")
        self.assertEqual(self.visible_keys(), ["__hdr__:triggernometry", self.key])
        self.select("", None)
        self.assertNotIn(self.key, self.visible_keys())


if __name__ == "__main__":
    unittest.main()
