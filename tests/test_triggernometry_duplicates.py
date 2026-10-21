from copy import deepcopy
import json
import os
from pathlib import Path
import shutil
import tempfile
import time
import unittest
from unittest.mock import patch
import uuid
import xml.etree.ElementTree as ET

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication

from nyaatriggers import triggernometry_bridge as bridge_module
from nyaatriggers.triggernometry_bridge import TriggernometryBridge
from nyaatriggers.triggernometry_editor import (
    PackDocument, matching_import, occupied_import_ids, parse_xml,
    prepare_import, prepared_packs, trigger_entries, validate_native, xml_bytes,
)
from nyaatriggers.ui.triggernometry_editor import pack_speech_rows


APP = QApplication.instance() or QApplication([])
START_ID = "11111111-1111-1111-1111-111111111111"
TARGET_ID = "22222222-2222-2222-2222-222222222222"
GUID_SCRIPT = ('var action = new Triggernometry.Action { ActionType = "Trigger", TriggerId = "' + TARGET_ID
               + '", TriggerForce = "regexp" }; TriggernometryHelpers.Plugin.QueueAction('
               'TriggernometryHelpers.CurrentContext, TriggernometryHelpers.CurrentContext.trig, null, action, '
               'System.DateTime.Now, false);')


def pack(name, labels):
    root = ET.Element("TriggernometryExport", Version="1")
    exported = ET.SubElement(root, "ExportedFolder", Id=str(uuid.uuid5(uuid.NAMESPACE_URL, name)),
                             Name=name, Enabled="true")
    folders = ET.SubElement(exported, "Folders")
    for label in labels:
        folder = ET.SubElement(folders, "Folder", Id=str(uuid.uuid5(uuid.NAMESPACE_URL, label)),
                               Name=label, Enabled="true")
        triggers = ET.SubElement(folder, "Triggers")
        start = ET.SubElement(triggers, "Trigger", Id=START_ID, Name="Start " + label,
                              Enabled="true", Source="FFXIVNetwork", RegularExpression="^TN_DUP_" + label + "$")
        actions = ET.SubElement(start, "Actions")
        ET.SubElement(actions, "Action", ActionType="Trigger", OrderNumber="1", TriggerId=TARGET_ID,
                      TriggerOp="FireTrigger", TriggerForce="regexp", Asynchronous="False")
        target = ET.SubElement(triggers, "Trigger", Id=TARGET_ID, Name="Target " + label,
                               Enabled="true", Source="None", Sequential="True", RegularExpression="")
        actions = ET.SubElement(target, "Actions")
        ET.SubElement(actions, "Action", ActionType="Variable", OrderNumber="1", VariableOp="Increment",
                      VariableName="duplicate_" + label, VariableExpression="1", Asynchronous="False")
        ET.SubElement(actions, "Action", ActionType="Placeholder", OrderNumber="2",
                      ExecutionDelayExpression="100", Asynchronous="False")
        expression = label + " ${var:duplicate_" + label + "}"
        ET.SubElement(actions, "Action", ActionType="UseTTS", OrderNumber="3",
                      UseTTSTextExpression=expression, Asynchronous="False")
        ET.SubElement(actions, "Action", ActionType="UseTTS", OrderNumber="4",
                      UseTTSTextExpression="Done " + expression, Asynchronous="False")
    return root


def identities(raw):
    return {trigger.get("Name"): trigger.get("Id")
            for _path, trigger, _parent in trigger_entries(parse_xml(raw))}


class DuplicateIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)

    def test_copied_folders_keep_all_actions_and_local_chains(self):
        original = pack("Copies", ["Left", "Right"])
        prepared = prepare_import(xml_bytes(original))
        root = parse_xml(prepared)
        ids = identities(prepared)
        self.assertEqual(len(ids), 4)
        self.assertEqual(len(set(ids.values())), 4)
        for original_folder, folder in zip(original.findall(".//Folder"), root.findall(".//Folder")):
            label = folder.get("Name")
            start, target = folder.findall("Triggers/Trigger")
            self.assertEqual(start.find("Actions/Action").get("TriggerId"), ids["Target " + label])
            self.assertEqual(start.get("NyaaSourceId"), START_ID)
            self.assertEqual(target.get("NyaaSourceId"), TARGET_ID)
            restored = deepcopy(folder)
            for old, current in zip(original_folder.findall("Triggers/Trigger"), restored.findall("Triggers/Trigger")):
                current.set("Id", old.get("Id"))
                current.attrib.pop("NyaaSourceId")
                for action in current.findall("Actions/Action[@ActionType='Trigger']"):
                    action.set("TriggerId", TARGET_ID)
            self.assertEqual(ET.tostring(restored), ET.tostring(original_folder))
        self.assertEqual(prepare_import(prepared), prepared)
        validate_native(prepared)

    def test_reordering_and_renaming_folders_preserves_duplicate_identities(self):
        original = pack("Stable", ["Left", "Right"])
        first = prepare_import(xml_bytes(original))
        folders = original.find("ExportedFolder/Folders")
        folders[:] = reversed(list(folders))
        for folder in folders:
            folder.set("Name", "Renamed " + folder.get("Name"))
        self.assertEqual(identities(prepare_import(xml_bytes(original), previous=first)), identities(first))

    def test_reordering_duplicate_triggers_in_one_folder_keeps_controls_with_their_names(self):
        root = pack("One folder", ["Left"])
        triggers = root.find(".//Folder/Triggers")
        triggers.remove(triggers[0])
        duplicate = deepcopy(triggers[0])
        duplicate.set("Name", "Different copy")
        triggers.append(duplicate)
        first = prepare_import(xml_bytes(root))
        triggers[:] = reversed(list(triggers))
        self.assertEqual(identities(prepare_import(xml_bytes(root), previous=first)), identities(first))

    def test_adding_and_removing_a_copy_preserves_existing_trigger_controls(self):
        root = pack("Add copy", ["Left"])
        triggers = root.find(".//Folder/Triggers")
        triggers.remove(triggers[0])
        first = xml_bytes(root)
        duplicate = deepcopy(triggers[0])
        duplicate.set("Name", "New copy")
        triggers.insert(0, duplicate)
        copied = prepare_import(xml_bytes(root), previous=first)
        self.assertEqual(identities(copied)["Target Left"], TARGET_ID)
        self.assertNotEqual(identities(copied)["New copy"], TARGET_ID)
        triggers.remove(duplicate)
        self.assertEqual(identities(prepare_import(xml_bytes(root), previous=copied)), identities(first))

    def test_cross_pack_collisions_and_upstream_updates_keep_separate_ids(self):
        first = self.directory / "first.xml"
        first.write_bytes(xml_bytes(pack("First", ["Left"])))
        original = pack("Second", ["Right"])
        raw = xml_bytes(original)
        self.assertIsNone(matching_import(self.directory, self.directory / "source.xml", raw))
        prepared = prepare_import(raw, occupied_ids=occupied_import_ids(self.directory))
        second = self.directory / "second.xml"
        second.write_bytes(prepared)
        self.assertTrue(set(identities(first.read_bytes()).values()).isdisjoint(identities(prepared).values()))
        self.assertEqual(matching_import(self.directory, self.directory / "source.xml", raw), (second, prepared))
        original.find(".//Trigger/Actions/Action").set("ExecutionDelayExpression", "50")
        updated = prepare_import(xml_bytes(original), occupied_ids=(), previous=prepared)
        self.assertEqual(identities(updated), identities(prepared))
        start = parse_xml(updated).find(".//Trigger")
        self.assertEqual(start.find("Actions/Action").get("TriggerId"), identities(updated)["Target Right"])

    def test_multiple_matching_imports_have_a_specific_recovery_message(self):
        raw = xml_bytes(pack("Copies", ["Left"]))
        for name in ("one.xml", "two.xml"):
            (self.directory / name).write_bytes(raw)
        with self.assertRaisesRegex(ValueError, "Multiple imported copies match"):
            matching_import(self.directory, self.directory / "source.xml", raw)

    def test_reimporting_original_unprepared_packs_keeps_runtime_identities(self):
        paths = [self.directory / "a.xml", self.directory / "b.xml", self.directory / "c.xml"]
        for path, labels in zip(paths, (["Left"], ["Right"], ["One", "Two"])):
            path.write_bytes(xml_bytes(pack(path.name, labels)))
        runtime = dict(prepared_packs(paths))
        for path in paths:
            existing = path, path.read_bytes()
            prepared = prepare_import(existing[1], occupied_ids=occupied_import_ids(self.directory, existing),
                                      previous=existing[1])
            self.assertEqual(identities(prepared), identities(runtime[path]))

    def test_ambiguous_duplicate_reference_is_reported_before_rewriting(self):
        root = pack("Ambiguous", ["Left", "Right"])
        first, second = root.findall(".//Folder")
        first.find("Triggers").append(deepcopy(second.find("Triggers/Trigger[@Source='None']")))
        original = xml_bytes(root)
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            prepare_import(original)
        self.assertEqual(xml_bytes(root), original)

    def test_scripts_containing_remapped_guids_are_reported_without_changing_unique_scripts(self):
        for labels in (["Left"], ["Left", "Right"]):
            root = pack("Script GUID", labels)
            action = root.find(".//Trigger/Actions/Action")
            action.attrib.clear()
            action.attrib.update(ActionType="ExecuteScript", OrderNumber="1", ExecScriptExpression=GUID_SCRIPT,
                                 Asynchronous="False")
            raw = xml_bytes(root)
            if len(labels) == 1:
                self.assertEqual(prepare_import(raw), raw)
            else:
                with self.assertRaisesRegex(ValueError, "Script.*contains trigger ID"):
                    prepare_import(raw)

    def test_unique_ids_and_guid_spellings_remain_unchanged(self):
        root = pack("Unique", ["Left"])
        ident = "abcde123-abcd-4abc-8abc-abcdef123456"
        root.find(".//Trigger").set("Id", "{" + ident.upper() + "}")
        raw = xml_bytes(root)
        self.assertEqual(prepare_import(raw), raw)
        self.assertEqual(identities(raw)["Start Left"], "{" + ident.upper() + "}")
        prepared = prepare_import(raw, occupied_ids=[ident])
        self.assertNotEqual(identities(prepared)["Start Left"], ident)
        self.assertEqual(prepared, prepare_import(raw, occupied_ids=["{" + ident.upper() + "}"]))

    def test_unrelated_action_fields_do_not_become_chain_references(self):
        root = pack("Copies", ["Left", "Right"])
        action = root.find(".//Trigger[@Source='None']/Actions/Action[@ActionType='UseTTS']")
        action.set("TriggerId", TARGET_ID)
        prepared = parse_xml(prepare_import(xml_bytes(root)))
        self.assertEqual(prepared.find(".//Trigger[@Source='None']/Actions/Action[@ActionType='UseTTS']").get("TriggerId"), TARGET_ID)

    def test_editor_and_inventory_use_the_same_cross_pack_runtime_ids(self):
        paths = [self.directory / "a.xml", self.directory / "b.xml"]
        for path, label in zip(paths, ("Left", "Right")):
            path.write_bytes(xml_bytes(pack(label, [label])))
        original = {path: path.read_bytes() for path in paths}
        prepared = dict(prepared_packs(paths))
        rows, incomplete = pack_speech_rows(self.directory)
        self.assertFalse(incomplete)
        self.assertEqual(len({row["id"] for row in rows}), 4)
        for path in paths:
            document = PackDocument.load(path)
            self.assertEqual(identities(xml_bytes(document.root)), identities(prepared[path]))
            self.assertEqual(document.original, original[path])
            self.assertEqual(path.read_bytes(), original[path])
        second = PackDocument.load(paths[1])
        second.save()
        self.assertEqual(identities(paths[1].read_bytes()), identities(prepared[paths[1]]))
        self.assertEqual(paths[1].with_suffix(".xml.bak").read_bytes(), original[paths[1]])

    def test_editor_save_normalizes_new_duplicate_triggers(self):
        path = self.directory / "editor.xml"
        document = PackDocument(path)
        document.root = pack("Editor", ["Left", "Right"])
        document.save()
        self.assertEqual(len(set(identities(path.read_bytes()).values())), 4)

    def test_editor_cannot_delete_a_normalized_target_still_used_by_a_chain(self):
        for labels in (["Left", "Right"], ["Other"]):
            path = self.directory / f"{len(labels)}.xml"
            path.write_bytes(xml_bytes(pack(path.name, labels)))
        for path in sorted(self.directory.glob("*.xml")):
            original = path.read_bytes()
            document = PackDocument.load(path)
            target = next((trigger, parent) for _folder, trigger, parent in trigger_entries(document.root)
                          if trigger.get("Source") == "None")
            target[1].remove(target[0])
            with self.assertRaisesRegex(ValueError, "still referenced"):
                document.save()
            self.assertEqual(path.read_bytes(), original)

    def test_editor_cannot_delete_a_target_still_used_by_a_script(self):
        path = self.directory / "script.xml"
        root = pack("Script GUID", ["Left"])
        action = root.find(".//Trigger/Actions/Action")
        action.attrib.clear()
        action.attrib.update(ActionType="ExecuteScript", OrderNumber="1", ExecScriptExpression=GUID_SCRIPT)
        path.write_bytes(xml_bytes(root))
        original = path.read_bytes()
        document = PackDocument.load(path)
        _path, trigger, parent = trigger_entries(document.root)[1]
        parent.remove(trigger)
        with self.assertRaisesRegex(ValueError, "still referenced by a script"):
            document.save()
        self.assertEqual(path.read_bytes(), original)


@unittest.skipUnless(os.name == "posix" and shutil.which("mono") and shutil.which("xvfb-run"),
                     "Requires Mono and Xvfb")
class NativeDuplicateTests(unittest.TestCase):
    def test_literal_guid_script_reaches_a_native_trigger_with_a_unique_id(self):
        from tests.test_triggernometry_host import replay
        root = pack("Script GUID", ["Left"])
        action = root.find(".//Trigger/Actions/Action")
        action.attrib.clear()
        action.attrib.update(ActionType="ExecuteScript", OrderNumber="1", ExecScriptExpression=GUID_SCRIPT,
                             Asynchronous="False")
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "script.xml"
            path.write_bytes(prepare_import(xml_bytes(root)))
            with replay(path) as host:
                host.send(t="log", line="TN_DUP_Left")
                self.assertEqual(host.call(), "Left 1")
                self.assertEqual(host.call(), "Done Left 1")

    def test_duplicate_chains_and_speech_controls_stay_independent_without_source_writes(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            packs = directory / "packs"
            packs.mkdir()
            roots = (pack("Copied folders", ["Left", "Right"]), pack("Other pack", ["Other"]),
                     pack("Another pack", ["Another"]))
            originals = {}
            for index, root in enumerate(roots):
                path = packs / f"{index}.xml"
                originals[path] = xml_bytes(root)
                path.write_bytes(originals[path])
            rows, incomplete = pack_speech_rows(packs)
            self.assertFalse(incomplete)
            with patch.object(bridge_module, "_packs_dir", return_value=packs), \
                    patch.object(bridge_module, "_rundata_dir", return_value=directory / "runtime"), \
                    patch.object(bridge_module, "_make_bundled_mono_executable"), \
                    patch.object(bridge_module, "_log"):
                engine = TriggernometryBridge()
                speech, inventory = [], []
                engine.tts.connect(lambda text, _generation: speech.append(text))
                engine.inventory.connect(lambda raw, _generation: inventory.extend(json.loads(raw)))

                def wait_for(predicate):
                    deadline = time.monotonic() + 15
                    while not predicate() and time.monotonic() < deadline:
                        QTest.qWait(10)
                    self.assertTrue(predicate(), speech)

                def fire(label, count, expected):
                    start = len(speech)
                    engine.feed_log("TN_DUP_" + label)
                    wait_for(lambda: f"Done {label} {count}" in speech[start:])
                    self.assertEqual(speech[start:], expected)

                try:
                    engine.start()
                    wait_for(lambda: len(inventory) == len(rows))
                    self.assertEqual({row["id"] for row in rows}, {row["id"] for row in inventory})
                    self.assertEqual(len(inventory), 8)
                    for label in ("Left", "Right", "Other", "Another"):
                        fire(label, 1, [label + " 1", "Done " + label + " 1"])
                    cid = next(row["id"] for row in inventory if row["text"] == "Left ${var:duplicate_Left}")
                    engine.set_callout(cid, tts="Custom ${var:duplicate_Left}")
                    engine.set_callout(cid, enable=False)
                    fire("Left", 2, ["Done Left 2"])
                    fire("Right", 2, ["Right 2", "Done Right 2"])
                    engine.set_callout(cid, enable=True)
                    fire("Left", 3, ["Custom 3", "Done Left 3"])
                    engine.reset_callout(cid)
                    fire("Left", 4, ["Left 4", "Done Left 4"])
                    cid = next(row["id"] for row in inventory if row["text"] == "Another ${var:duplicate_Another}")
                    engine.set_callout(cid, enable=False)
                    fire("Another", 2, ["Done Another 2"])
                    fire("Other", 2, ["Other 2", "Done Other 2"])
                    for path, raw in originals.items():
                        self.assertEqual(path.read_bytes(), raw)
                        self.assertTrue(engine.pack_is_current(path))
                    runtime = Path(engine._proc._nyaa_runtime_dir.name)
                    self.assertTrue(runtime.is_dir())
                finally:
                    engine.stop(wait=True)
                    for worker in (engine._reader, engine._writer, engine._errpump):
                        if worker is not None:
                            worker.join(timeout=8)
                            self.assertFalse(worker.is_alive())
                self.assertFalse(runtime.exists())


if __name__ == "__main__":
    unittest.main()
