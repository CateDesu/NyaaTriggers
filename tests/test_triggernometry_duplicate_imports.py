from copy import deepcopy
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

from nyaatriggers.trigger_engine import Trigger
from nyaatriggers.triggernometry_editor import parse_xml, xml_bytes
from nyaatriggers.ui import engines
from tests.test_triggernometry_duplicates import pack
from tests import test_triggernometry_sections as section_fixture


class DuplicateImportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        section_fixture.TriggernometrySectionTests.setUpClass()

    def setUp(self):
        self.case = section_fixture.TriggernometrySectionTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.window = self.case.window
        self.window._triggers_enabled = False

    def import_root(self, root):
        self.case.source.write_bytes(xml_bytes(root))
        self.window._import_triggernometry()
        self.case.critical.assert_not_called()

    def test_duplicate_folder_import_and_reimport_preserve_independent_choices(self):
        root = pack("Copied folders", ["Left", "Right"])
        self.import_root(root)
        rows = self.window._engine_inventory
        self.assertEqual(len(rows), 4)
        self.assertEqual(len({row["id"] for row in rows}), 4)
        left = next(row["id"] for row in rows if row["text"] == "Left ${var:duplicate_Left}")
        self.window._triggernometry_callout_edits[left] = "Custom left ${var:duplicate_Left}"
        self.window._toggle_engine_row("triggernometry:" + left, False)
        ids = {row["id"] for row in rows}
        folders = root.find("ExportedFolder/Folders")
        folders[:] = reversed(list(folders))
        for folder in folders:
            folder.set("Name", "Renamed " + folder.get("Name"))
        self.import_root(root)
        self.assertEqual({row["id"] for row in self.window._engine_inventory}, ids)
        self.assertEqual(self.window._triggernometry_callout_edits[left], "Custom left ${var:duplicate_Left}")
        self.assertEqual(self.window._triggernometry_disabled, {left})
        self.assertEqual(len(list(self.case.packs.glob("*.xml"))), 1)

    def test_distinct_packs_with_shared_ids_survive_update_and_peer_removal(self):
        self.import_root(pack("First pack", ["Left"]))
        first_ids = {row["id"] for row in self.window._engine_inventory}
        second = pack("Second pack", ["Right"])
        self.import_root(second)
        rows = self.window._engine_inventory
        second_ids = {row["id"] for row in rows if row["name"] == "Target Right"}
        self.assertEqual(len(rows), 4)
        self.assertEqual(len(second_ids), 2)
        self.assertTrue(first_ids.isdisjoint(second_ids))
        files = sorted(self.case.packs.glob("*.xml"))
        self.assertEqual(len(files), 2)
        first_file = next(path for path in files
                          if parse_xml(path.read_bytes()).find("ExportedFolder").get("Name") == "First pack")
        first_file.unlink()
        self.import_root(second)
        self.assertEqual({row["id"] for row in self.window._engine_inventory}, second_ids)

    def test_ambiguous_chain_does_not_install_a_partially_rewritten_pack(self):
        root = pack("Ambiguous", ["Left", "Right"])
        left, right = root.findall(".//Folder")
        left.find("Triggers").append(deepcopy(right.find("Triggers/Trigger[@Source='None']")))
        original = xml_bytes(root)
        self.case.source.write_bytes(original)
        self.window._import_triggernometry()
        self.case.critical.assert_called_once()
        self.assertIn("ambiguous", self.case.critical.call_args.args[-1])
        self.assertEqual(list(self.case.packs.iterdir()), [])
        self.assertEqual(self.window._engine_inventory, [])
        self.assertEqual(self.case.source.read_bytes(), original)

    def test_local_fallback_keeps_distinct_triggers_sharing_a_matcher(self):
        existing = Trigger(name="Existing", fight="UMAD", log_type="21", ability_id="ABCD")
        self.window._triggers = [existing]
        self.window._local_ids = {existing.id}
        root = pack("UMAD", ["Left", "Right"])
        for trigger in root.findall(".//Trigger"):
            trigger.set("Source", "FFXIVNetwork")
            trigger.set("RegularExpression", r"^21\|(?:[^|]*\|){3}ABCD\|")
            actions = trigger.find("Actions")
            actions[:] = [ET.Element("Action", ActionType="UseTTS", UseTTSTextExpression=trigger.get("Name"))]
        with patch.object(engines.TriggernometryBridge, "is_available", return_value=False):
            self.import_root(root)
            count = len(self.window._triggers)
            self.assertEqual(count, 5)
            self.assertEqual({trigger.name for trigger in self.window._triggers},
                             {"Existing", "Start Left", "Target Left", "Start Right", "Target Right"})
            self.assertEqual(len({trigger.id for trigger in self.window._triggers}), count)
            self.import_root(root)
        self.assertEqual(len(self.window._triggers), count)
        self.assertIs(self.window._triggers[0], existing)


if __name__ == "__main__":
    unittest.main()
