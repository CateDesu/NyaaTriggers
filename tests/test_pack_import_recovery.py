from contextlib import ExitStack
import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, call, patch
import uuid
import xml.etree.ElementTree as ET

from nyaatriggers import app_common as ac
from nyaatriggers.triggernometry_bridge import packs_dir
from nyaatriggers.ui import engines
from nyaatriggers.triggernometry_editor import PackDocument, validate_native, xml_bytes


class PackImportRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.packs = self.root / "packs"
        self.packs.mkdir()
        self.source = self.root / "callouts.xml"
        document = PackDocument(self.source)
        trigger = document.add_trigger()
        trigger.set("RegularExpression", r"PACK (?<label>\w+)")
        ET.SubElement(trigger.find("Actions"), "Action", OrderNumber="1", ActionType="UseTTS",
                      Asynchronous="False", UseTTSTextExpression="Original ${label}")
        self.original = xml_bytes(document.root)
        self.updated = self.original.replace(b"Original ${label}", b"Updated ${label}")
        self.source.write_bytes(self.original)
        self.host = SimpleNamespace(_triggers=[], _triggers_enabled=False, _local_ids=set())
        self.stack.enter_context(patch.object(ac.QFileDialog, "getOpenFileName",
                                             return_value=(str(self.source), "")))
        self.stack.enter_context(patch.object(engines, "_tn_packs_dir", return_value=self.packs))
        self.stack.enter_context(patch.object(engines, "TriggernometryBridge",
                                             SimpleNamespace(is_available=lambda: True)))
        self.success = self.stack.enter_context(patch.object(ac.QMessageBox, "information"))
        self.warning = self.stack.enter_context(patch.object(ac.QMessageBox, "warning"))
        self.error = self.stack.enter_context(patch.object(ac.QMessageBox, "critical"))
        self.confirm = self.stack.enter_context(patch.object(ac.QMessageBox, "question",
                                                return_value=ac.QMessageBox.StandardButton.Yes))
        self.validate = self.stack.enter_context(patch(
            "nyaatriggers.triggernometry_editor.validate_native"))

    def import_pack(self):
        engines.EnginesMixin._import_triggernometry(self.host)

    def test_updated_export_replaces_one_active_pack_and_keeps_backup(self):
        self.import_pack()
        self.source.write_bytes(self.updated)
        self.import_pack()
        self.assertEqual(list(self.packs.glob("*.xml")), [self.packs / "callouts.xml"])
        self.assertEqual((self.packs / "callouts.xml").read_bytes(), self.updated)
        self.assertEqual((self.packs / "callouts.xml.bak").read_bytes(), self.original)
        self.confirm.assert_called_once()

    def test_maximum_length_pack_names_can_be_replaced_with_a_backup(self):
        for stem in ("a" * 251, "猫" * 83 + "ab"):
            with self.subTest(stem=stem[:3]):
                for path in self.packs.iterdir():
                    path.unlink()
                self.error.reset_mock()
                self.warning.reset_mock()
                source = self.root / (stem + ".xml")
                source.write_bytes(self.original)
                with patch.object(ac.QFileDialog, "getOpenFileName", return_value=(str(source), "")):
                    self.import_pack()
                    source.write_bytes(self.updated)
                    self.import_pack()
                target = self.packs / source.name
                self.assertEqual(target.read_bytes(), self.updated)
                self.assertEqual(target.with_suffix(".bak").read_bytes(), self.original)
                self.error.assert_not_called()
                self.warning.assert_not_called()
                for path in self.packs.iterdir():
                    path.unlink()

    def test_identical_reimport_keeps_running_engine_and_pending_work(self):
        self.import_pack()
        self.host._triggers_enabled = True
        self.host._triggernometry_mode = True
        self.host._triggernometry = SimpleNamespace(
            is_active=lambda: True, pack_is_current=lambda _path: True)
        self.host._set_triggernometry_enabled = Mock()
        self.import_pack()
        self.assertEqual(list(self.packs.iterdir()), [self.packs / "callouts.xml"])
        self.host._set_triggernometry_enabled.assert_not_called()
        self.confirm.assert_not_called()

    def test_updated_export_keeps_local_fallback_without_native_runtime(self):
        document = PackDocument(self.source)
        self.host._is_duplicate = lambda trigger: None
        self.host._save_triggers = Mock(return_value=True)
        self.host._refresh_table = Mock()
        self.host._refresh_tree = Mock()
        self.validate.side_effect = validate_native
        with patch.object(engines.TriggernometryBridge, "is_available", return_value=False), \
                patch("nyaatriggers.triggernometry_bridge._find_mono", return_value=None), \
                patch("nyaatriggers.triggernometry_bridge._find_exe", return_value=None):
            for index, ability in enumerate(("ABCD", "ABCE"), 1):
                trigger = document.add_trigger()
                trigger.set("Name", f"Call {index}")
                trigger.set("RegularExpression", r"^21\|(?:[^|]*\|){3}" + ability + r"\|")
                ET.SubElement(trigger.find("Actions"), "Action", OrderNumber="1",
                              ActionType="UseTTS", UseTTSTextExpression=f"Call {index}")
                self.source.write_bytes(xml_bytes(document.root))
                self.import_pack()
                self.assertEqual(len(self.host._triggers), index)
                if index == 1:
                    staged = (self.packs / "callouts.xml").read_bytes()
            self.import_pack()
        self.assertEqual([trigger.ability_id for trigger in self.host._triggers], ["ABCD", "ABCE"])
        self.assertTrue(all(not trigger.enabled for trigger in self.host._triggers))
        self.assertEqual((self.packs / "callouts.xml").read_bytes(), staged)
        self.assertEqual(list(self.packs.iterdir()), [self.packs / "callouts.xml"])
        self.error.assert_not_called()
        self.confirm.assert_not_called()
        self.validate.assert_not_called()
        self.assertIn("already in your set", self.success.call_args.args[-1])

    def check_pack_discovery_fallback(self):
        document = PackDocument(self.source, self.original)
        trigger = document.root.find(".//Trigger")
        trigger.set("RegularExpression", r"^21\|(?:[^|]*\|){3}ABCD\|")
        trigger.find("Actions/Action").set("UseTTSTextExpression", "Simple call")
        self.source.write_bytes(xml_bytes(document.root))
        local = self.root / "triggers.local.json"
        self.host._is_duplicate = lambda trigger: None
        self.host._save_triggers = Mock(side_effect=lambda: ac._atomic_write_json(
            local, {"triggers": [trigger.to_dict() for trigger in self.host._triggers]}))
        self.host._refresh_table = Mock()
        self.host._refresh_tree = Mock()
        self.host._set_triggernometry_enabled = Mock()
        self.host._triggers_enabled = True
        for available in (False, True):
            with self.subTest(engine_available=available), \
                    patch.object(engines.TriggernometryBridge, "is_available", return_value=available):
                self.host._triggers.clear()
                self.host._local_ids.clear()
                local.unlink(missing_ok=True)
                self.warning.reset_mock()
                self.success.reset_mock()
                self.import_pack()
                saved = json.loads(local.read_text())["triggers"]
                self.assertEqual(len(saved), 1)
                self.assertEqual(saved[0]["ability_id"], "ABCD")
                self.assertEqual(saved[0]["tts_text"], "Simple call")
                self.assertFalse(saved[0]["enabled"])
                self.warning.assert_called_once()
                self.assertIn("imported into Local", self.success.call_args.args[-1])
                self.assertNotIn("is now running", self.success.call_args.args[-1])
        self.host._set_triggernometry_enabled.assert_not_called()
        self.error.assert_not_called()
        self.confirm.assert_not_called()
        self.validate.assert_not_called()

    def test_pack_directory_creation_failure_keeps_local_fallback(self):
        blocked = self.root / "blocked"
        blocked.write_bytes(b"Keep this file")
        with patch.dict(os.environ, {"NYAA_TRIGGERNOMETRY_PACKS": str(blocked / "packs")}), \
                patch.object(engines, "_tn_packs_dir", side_effect=packs_dir):
            self.check_pack_discovery_fallback()
        self.assertEqual(blocked.read_bytes(), b"Keep this file")

    def test_pack_directory_listing_failure_keeps_local_fallback(self):
        managed = self.packs / "existing.xml"
        managed.write_bytes(self.original)
        iterdir = Path.iterdir

        def unreadable(path):
            if path == self.packs:
                raise PermissionError("Pack directory is unreadable")
            return iterdir(path)

        with patch.object(Path, "iterdir", unreadable):
            self.check_pack_discovery_fallback()
        self.assertEqual(list(self.packs.iterdir()), [managed])
        self.assertEqual(managed.read_bytes(), self.original)

    def test_identical_reimport_restarts_an_enabled_but_stopped_engine(self):
        self.import_pack()
        self.host._triggers_enabled = True
        self.host._triggernometry_mode = True
        active = Mock(return_value=False)
        self.host._triggernometry = SimpleNamespace(is_active=active)

        def enable(enabled):
            self.host._triggernometry_mode = enabled
            active.return_value = enabled

        self.host._set_triggernometry_enabled = Mock(side_effect=enable)
        self.import_pack()
        self.assertEqual(self.host._set_triggernometry_enabled.call_args_list, [call(False), call(True)])
        self.assertTrue(active())
        self.assertIn("is now running", self.success.call_args.args[-1])
        self.confirm.assert_not_called()

    def test_failed_restart_does_not_report_the_pack_as_running(self):
        self.import_pack()
        self.host._triggers_enabled = True
        self.host._triggernometry_mode = True
        self.host._triggernometry = SimpleNamespace(is_active=lambda: False)
        self.host._set_triggernometry_enabled = Mock()
        self.import_pack()
        self.assertEqual(self.host._set_triggernometry_enabled.call_args_list, [call(False), call(True)])
        self.assertIn("the engine could not start", self.success.call_args.args[-1])
        self.assertNotIn("is now running", self.success.call_args.args[-1])

    def test_declined_update_preserves_both_source_and_managed_file(self):
        self.import_pack()
        self.source.write_bytes(self.updated)
        self.confirm.return_value = ac.QMessageBox.StandardButton.No
        self.import_pack()
        self.assertEqual(list(self.packs.iterdir()), [self.packs / "callouts.xml"])
        self.assertEqual((self.packs / "callouts.xml").read_bytes(), self.original)
        self.assertEqual(self.source.read_bytes(), self.updated)

    def test_external_edit_during_confirmation_is_preserved(self):
        self.import_pack()
        self.source.write_bytes(self.updated)
        external = self.original.replace(b"Original", b"External")
        def answer(*args):
            (self.packs / "callouts.xml").write_bytes(external)
            return ac.QMessageBox.StandardButton.Yes
        self.confirm.side_effect = answer
        self.import_pack()
        self.assertEqual((self.packs / "callouts.xml").read_bytes(), external)
        self.assertEqual(list(self.packs.iterdir()), [self.packs / "callouts.xml"])
        self.error.assert_called_once()

    def test_backup_failure_aborts_replacement(self):
        self.import_pack()
        self.source.write_bytes(self.updated)
        with patch.object(ac, "_atomic_write_bytes", side_effect=OSError("Disk full")):
            self.import_pack()
        self.assertEqual((self.packs / "callouts.xml").read_bytes(), self.original)
        self.assertEqual(list(self.packs.iterdir()), [self.packs / "callouts.xml"])
        self.assertTrue(self.error.called or self.warning.called)

    def test_existing_duplicate_identities_require_review_before_replacement(self):
        self.import_pack()
        (self.packs / "duplicate.xml").write_bytes(self.original)
        self.source.write_bytes(self.updated)
        self.import_pack()
        self.assertEqual({p.name for p in self.packs.iterdir()}, {"callouts.xml", "duplicate.xml"})
        self.assertTrue(all(p.read_bytes() == self.original for p in self.packs.iterdir()))
        self.error.assert_called_once()

    def test_overlapping_trigger_ids_under_another_folder_are_not_loaded_twice(self):
        self.import_pack()
        root = ET.fromstring(self.updated)
        root.find("ExportedFolder").set("Id", str(uuid.uuid4()))
        self.source.write_bytes(xml_bytes(root))
        self.import_pack()
        self.assertEqual(list(self.packs.iterdir()), [self.packs / "callouts.xml"])
        self.assertEqual((self.packs / "callouts.xml").read_bytes(), self.original)
        self.error.assert_called_once()

    def test_native_rejection_preserves_the_working_pack(self):
        self.import_pack()
        self.source.write_bytes(self.updated)
        self.validate.side_effect = ValueError("Invalid native regular expression")
        self.import_pack()
        self.assertEqual(list(self.packs.iterdir()), [self.packs / "callouts.xml"])
        self.assertEqual((self.packs / "callouts.xml").read_bytes(), self.original)
        self.error.assert_called_once()

    def test_failed_replacement_keeps_original_and_completed_backup(self):
        self.import_pack()
        self.source.write_bytes(self.updated)
        write = ac._atomic_write_bytes
        def fail_target(path, payload):
            if path.suffix == ".xml":
                raise OSError("Replacement failed")
            write(path, payload)
        with patch.object(ac, "_atomic_write_bytes", side_effect=fail_target):
            self.import_pack()
        self.assertEqual((self.packs / "callouts.xml").read_bytes(), self.original)
        self.assertEqual((self.packs / "callouts.xml.bak").read_bytes(), self.original)
        self.assertEqual(list(self.packs.glob("*.xml")), [self.packs / "callouts.xml"])

    def test_unrelated_valid_exports_with_the_same_filename_are_kept(self):
        self.import_pack()
        root = ET.fromstring(self.updated)
        root.find("ExportedFolder").set("Id", str(uuid.uuid4()))
        root.find(".//Trigger").set("Id", str(uuid.uuid4()))
        changed = xml_bytes(root)
        self.source.write_bytes(changed)
        self.import_pack()
        self.assertEqual((self.packs / "callouts.xml").read_bytes(), self.original)
        self.assertEqual((self.packs / "callouts_2.xml").read_bytes(), changed)
        self.confirm.assert_not_called()


if __name__ == "__main__":
    unittest.main()
