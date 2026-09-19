"""Large valid inputs and interrupted edits should remain usable."""

import os
import random
import re
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt6.QtWidgets import QApplication, QTreeWidgetItem

from nyaatriggers import app_common as ac
from nyaatriggers.ui import triggers_tab
from nyaatriggers.ui.triggers_tab import TriggersTabMixin

APP = QApplication.instance() or QApplication([])


class FolderHost(SimpleNamespace, TriggersTabMixin):
    pass


class InputGrowthTests(unittest.TestCase):
    def run_script(self, script):
        result = subprocess.run([sys.executable, "-c", script], capture_output=True,
                                text=True, timeout=3)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_long_timeline_identifiers_finish_parsing(self):
        for suffix in ('"A" * 50000', '\'Ability { \' + "A" * 50000 + " }"'):
            with self.subTest(suffix=suffix):
                self.run_script("from nyaatriggers.timeline_parser import parse\n"
                                + 'assert len(parse(\'1 "Callout" \' + ' + suffix + ")) == 1")

    def test_unclosed_translation_tokens_finish_compiling(self):
        self.run_script('from nyaatriggers.app_common import _compile_phrase_patterns\n'
                        'assert _compile_phrase_patterns({"{" * 500000: "Translation"}) == []')

    @staticmethod
    def folder_host(count=1500):
        return FolderHost(
            _folders=[{"id": str(i), "name": "Folder " + str(i),
                       "parent_id": str(i - 1) if i else None} for i in range(count)],
            _triggers=[], _local_ids=set(), _official_ids=set(),
            _save_triggers=Mock(), _refresh_tree=Mock(), _refresh_table=Mock())

    def test_deep_folder_tree_can_be_rendered(self):
        host = self.folder_host()
        root = parent = QTreeWidgetItem(["Root"])
        host._add_folder_node(host._folders[0], parent)
        count = 0
        while parent.childCount():
            parent = parent.child(0)
            count += 1
        self.assertEqual(count, 1500)

    def test_deep_folder_tree_can_be_collapsed(self):
        host = self.folder_host()
        root = QTreeWidgetItem(["Root"])
        host._add_folder_node(host._folders[0], root)
        host._collapse_tree_descendants(root)

    def test_folder_traversal_preserves_order_and_stops_at_cycles(self):
        host = self.folder_host(4)
        host._folders[2]["parent_id"] = "0"
        host._folders.append({"id": "0", "name": "Duplicate", "parent_id": "3"})
        root = QTreeWidgetItem(["Root"])
        host._add_folder_node(host._folders[0], root)
        first = root.child(0)
        self.assertEqual([first.child(i).text(0) for i in range(first.childCount())],
                         ["▶ Folder 1", "▶ Folder 2"])
        self.assertEqual(first.child(1).child(0).childCount(), 0)
        with patch.object(ac.QMessageBox, "question", return_value=ac.QMessageBox.StandardButton.Yes):
            host._delete_folder("0")
        self.assertEqual(host._folders, [])

    def test_malformed_parent_ids_do_not_break_other_folders(self):
        for parent_id in ([], {}, ["0"]):
            with self.subTest(parent_id=parent_id):
                host = self.folder_host(2)
                host._folders[1]["parent_id"] = parent_id
                root = QTreeWidgetItem(["Root"])
                host._add_folder_node(host._folders[0], root)
                self.assertEqual(root.child(0).childCount(), 0)
                with patch.object(ac.QMessageBox, "question", return_value=ac.QMessageBox.StandardButton.Yes):
                    host._delete_folder("0")
                self.assertEqual([folder["id"] for folder in host._folders], ["1"])

    def test_translation_token_split_keeps_previous_semantics(self):
        rng = random.Random(4928)
        for _ in range(5000):
            text = "".join(rng.choices("a{}\n界", k=rng.randrange(80)))
            self.assertEqual(ac._split_phrase_tokens(text), re.split(r"\{[^}]*\}", text), text)

    def test_deep_folder_tree_can_be_deleted(self):
        host = self.folder_host()
        with patch.object(ac.QMessageBox, "question", return_value=ac.QMessageBox.StandardButton.Yes):
            host._delete_folder("0")
        self.assertEqual(host._folders, [])

    def test_new_folder_stays_visible_if_its_parent_disappears(self):
        host = self.folder_host(1)
        def answer(*args, **kwargs):
            host._folders.clear()
            return "New folder", True
        with patch.object(triggers_tab.QInputDialog, "getText", side_effect=answer):
            host._create_folder("0")
        self.assertEqual(len(host._folders), 1)
        self.assertIsNone(host._folders[0]["parent_id"])


if __name__ == "__main__":
    unittest.main()
