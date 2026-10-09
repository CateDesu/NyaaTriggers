from contextlib import ExitStack
import json
import unittest
from unittest.mock import patch

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QTreeWidgetItemIterator

from nyaatriggers import app_common as ac
from nyaatriggers.telesto_client import TelestoClient
from nyaatriggers.trigger_engine import Trigger
from nyaatriggers.ui.triggers_tab import TriggersTabMixin
from tests import test_session_ui as fixture


class TriggerFolderVisibilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture.SessionUiTests.setUpClass()

    def setUp(self):
        self.case = fixture.SessionUiTests()
        self.addCleanup(self.case.doCleanups)
        with patch.object(TelestoClient, "start"):
            self.case.setUp()
        self.window = self.case.window
        self.window._triggers = [Trigger(id="unassigned", name="Unassigned"),
                                 Trigger(id="named", name="Named", fight="General")]
        self.window._local_ids = {"unassigned", "named"}
        self.window._official_ids = set()
        self.window._official_triggers = {}
        self.window._deleted_ids = set()
        self.window._folders = []
        self.window._local_trigger_baseline = None
        self.window._src_collapsed = {}
        self.window._refresh_table()

    def items(self):
        iterator = QTreeWidgetItemIterator(self.window._tree)
        while iterator.value():
            item = iterator.value()
            if item.data(0, ac._ITEM_TYPE_ROLE) in ("custom_group", "folder"):
                yield item
            iterator += 1

    def visible(self, fight, kind):
        item = next((item for item in self.items()
                     if item.data(0, Qt.ItemDataRole.UserRole) == fight
                     and item.data(0, ac._ITEM_TYPE_ROLE) == kind), None)
        self.assertIsNotNone(item, f"Missing {kind} for {fight!r}")
        self.window._tree.setCurrentItem(item)
        self.window._apply_tab_filter(item)
        return {entry.data(Qt.ItemDataRole.UserRole)
                for row in range(self.window._table.rowCount())
                if not self.window._table.isRowHidden(row)
                and (entry := self.window._table.item(row, ac._C_EN)) is not None
                and entry.data(Qt.ItemDataRole.UserRole) in self.window._local_ids}

    def test_general_fight_tag_keeps_a_separate_unassigned_group(self):
        self.assertEqual(self.visible("", "custom_group"), {"unassigned"})
        self.assertEqual(self.visible("General", "custom_group"), {"named"})

    def test_creating_a_general_folder_keeps_unassigned_triggers_visible(self):
        with patch("nyaatriggers.ui.triggers_tab.QInputDialog.getText", return_value=("General", True)):
            self.window._create_folder(None)
        self.assertEqual(self.visible("", "custom_group"), {"unassigned"})
        self.assertEqual(self.visible("General", "folder"), {"named"})
        saved = json.loads(ac.TRIGGERS_LOCAL_FILE.read_text())
        self.assertEqual({row["id"] for row in saved["triggers"]}, {"unassigned", "named"})
        self.assertEqual(saved["folders"][0]["name"], "General")

    def test_renaming_a_folder_to_general_keeps_both_views_after_reload(self):
        self.window._folders = [{"id": "parent", "name": "Parent", "parent_id": None},
                                {"id": "child", "name": "Custom", "parent_id": "parent"}]
        self.window._triggers[1].fight = "Custom"
        self.window._refresh_table()
        with patch("nyaatriggers.ui.triggers_tab.QInputDialog.getText", return_value=("General", True)):
            self.window._rename_folder("child")
        self.assertEqual(self.visible("", "custom_group"), {"unassigned"})
        self.assertEqual(self.visible("General", "folder"), {"named"})
        with ExitStack() as stack:
            for name in ("TRIGGERS_FILE", "RETIRED_FILE", "_REPO_TRIGGERS_FILE",
                         "_REPO_RETIRED_FILE", "_REPO_TRIGGERS_VERSION"):
                stack.enter_context(patch.object(ac, name, self.case.temp / name))
            ac.TRIGGERS_FILE.write_text("[]")
            TriggersTabMixin._load_triggers(self.window)
            self.assertEqual(self.visible("", "custom_group"), {"unassigned"})
            self.assertEqual(self.visible("General", "folder"), {"named"})
            child = next(folder for folder in self.window._folders if folder["id"] == "child")
            self.assertEqual(child["parent_id"], "parent")


if __name__ == "__main__":
    unittest.main()
