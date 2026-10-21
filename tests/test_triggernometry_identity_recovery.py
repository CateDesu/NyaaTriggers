from contextlib import ExitStack
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import unittest
from unittest.mock import patch
import uuid
import xml.etree.ElementTree as ET

from PyQt6.QtTest import QTest

from nyaatriggers import triggernometry_bridge as bridge_module
from nyaatriggers.telesto_client import TelestoClient
from nyaatriggers.triggernometry_bridge import TriggernometryBridge
from nyaatriggers.triggernometry_editor import parse_xml, xml_bytes
from tests.test_triggernometry_duplicates import pack
from tests import test_session_ui as fixture


class IdentityRecoveryTests(unittest.TestCase):
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
        self.directory = self.case.temp / "triggernometry-packs"
        self.first, self.second = self.directory / "a.xml", self.directory / "b.xml"
        self.originals = {self.first: xml_bytes(pack("First", ["Left"])),
                          self.second: xml_bytes(pack("Second", ["Right"]))}
        self.stamp = 1_700_000_000_123_456_789
        for path, content in self.originals.items():
            path.write_bytes(content)
            os.utime(path, ns=(self.stamp, self.stamp))

    def scan(self):
        self.window._load_cached_triggernometry_inventory()
        self.window._refresh_table()
        return {row["text"]: row["id"] for row in self.window._engine_inventory
                if row.get("source") == "triggernometry"}

    def save_controls(self, ident):
        self.window._set_triggernometry_callout_edit(ident, "Custom ${var:duplicate_Right}")
        self.window._toggle_engine_row("triggernometry:" + ident, False)

    def assert_controls(self, rows, expected):
        self.assertEqual(rows["Right ${var:duplicate_Right}"], expected)
        self.assertIn(expected, self.window._triggernometry_disabled)
        self.assertEqual(self.window._triggernometry_callout_edits[expected], "Custom ${var:duplicate_Right}")
        self.assertNotIn(rows["Done Right ${var:duplicate_Right}"], self.window._triggernometry_disabled)
        self.assertEqual(self.second.read_bytes(), self.originals[self.second])

    def fresh_process_rows(self):
        script = (
            "import json,sys; from pathlib import Path; "
            "from nyaatriggers.ui.triggernometry_editor import pack_speech_rows; "
            "rows, incomplete=pack_speech_rows(Path(sys.argv[1])); "
            "print(json.dumps({'rows':rows,'incomplete':incomplete}))"
        )
        result = subprocess.run([sys.executable, "-c", script, str(self.directory)],
                                cwd=Path(__file__).resolve().parents[1], capture_output=True,
                                text=True, check=True, timeout=30)
        data = json.loads(result.stdout)
        return {row["text"]: row["id"] for row in data["rows"]}, data["incomplete"]

    def check_peer_failure(self, kind):
        original = self.scan()
        selected = original["Right ${var:duplicate_Right}"]
        self.save_controls(selected)
        if kind == "malformed":
            self.first.write_bytes(b"<bad" + b" " * (len(self.originals[self.first]) - 4))
            os.utime(self.first, ns=(self.stamp, self.stamp))
        elif kind == "missing":
            self.first.unlink()
        open_file = Path.open

        def unavailable(path, *args, **kwargs):
            if kind == "unreadable" and path == self.first:
                raise PermissionError("Simulated unreadable peer")
            return open_file(path, *args, **kwargs)

        with patch.object(Path, "open", unavailable):
            failed_rows = self.scan()
        self.assert_controls(failed_rows, selected)
        if kind != "unreadable":
            restarted, incomplete = self.fresh_process_rows()
            self.assertEqual(restarted["Right ${var:duplicate_Right}"], selected)
            self.assertEqual(incomplete, kind == "malformed")
        self.first.write_bytes(self.originals[self.first])
        os.utime(self.first, ns=(self.stamp, self.stamp))
        self.assert_controls(self.scan(), selected)
        restarted, incomplete = self.fresh_process_rows()
        self.assertFalse(incomplete)
        self.assertEqual(restarted, original)
        self.assertEqual(self.first.read_bytes(), self.originals[self.first])

    def test_malformed_peer_preserves_mute_and_override_through_repair_and_process_restart(self):
        self.check_peer_failure("malformed")

    def test_unreadable_peer_preserves_mute_and_override_on_the_next_retry(self):
        self.check_peer_failure("unreadable")

    def test_removed_peer_preserves_survivor_controls_through_process_restart_and_return(self):
        self.check_peer_failure("missing")

    def test_new_earlier_peer_cannot_take_an_existing_triggers_controls(self):
        self.first.unlink()
        original = self.scan()
        selected = original["Right ${var:duplicate_Right}"]
        self.save_controls(selected)
        self.first.write_bytes(self.originals[self.first])
        os.utime(self.first, ns=(self.stamp, self.stamp))
        added = self.scan()
        self.assert_controls(added, selected)
        self.assertNotEqual(added["Left ${var:duplicate_Left}"], selected)
        self.assertNotIn(added["Left ${var:duplicate_Left}"], self.window._triggernometry_disabled)
        restarted, incomplete = self.fresh_process_rows()
        self.assertFalse(incomplete)
        self.assertEqual(restarted, added)

    def test_moving_an_unambiguous_source_trigger_to_another_folder_keeps_controls(self):
        original = self.scan()
        selected = original["Right ${var:duplicate_Right}"]
        self.save_controls(selected)
        root = parse_xml(self.originals[self.second])
        container = root.find(".//Folder/Triggers")
        target = container.find("Trigger[@Source='None']")
        container.remove(target)
        moved = ET.SubElement(root.find("ExportedFolder/Folders"), "Folder", Id=str(uuid.uuid4()),
                              Name="Moved", Enabled="true")
        ET.SubElement(moved, "Triggers").append(target)
        self.originals[self.second] = xml_bytes(root)
        self.second.write_bytes(self.originals[self.second])
        current = self.scan()
        self.assert_controls(current, selected)
        restarted, incomplete = self.fresh_process_rows()
        self.assertFalse(incomplete)
        self.assertEqual(restarted, current)

    def test_renaming_a_pack_preserves_its_controls_and_source_bytes(self):
        original = self.scan()
        selected = original["Right ${var:duplicate_Right}"]
        self.save_controls(selected)
        renamed = self.directory / "c.xml"
        self.second.rename(renamed)
        self.originals[renamed] = self.originals.pop(self.second)
        self.second = renamed
        current = self.scan()
        self.assert_controls(current, selected)
        restarted, incomplete = self.fresh_process_rows()
        self.assertFalse(incomplete)
        self.assertEqual(restarted, current)

    def test_real_engine_retains_controls_after_malformed_peer_restart_and_repair(self):
        if not TriggernometryBridge.is_available() or not shutil.which("xvfb-run"):
            self.skipTest("Requires the real Mono engine and Xvfb")
        original = self.scan()
        selected = original["Right ${var:duplicate_Right}"]
        self.save_controls(selected)
        self.stack.enter_context(patch.object(bridge_module, "_packs_dir", return_value=self.directory))
        self.stack.enter_context(patch.object(bridge_module, "_rundata_dir", return_value=self.case.temp / "runtime"))
        engine = TriggernometryBridge()
        self.window._triggernometry = engine
        self.window._triggernometry_mode = True
        speech, inventories = [], []
        engine.tts.connect(lambda text, generation: speech.append(text))
        engine.inventory.connect(self.window._on_triggernometry_inventory)
        engine.inventory.connect(lambda payload, generation: inventories.append(json.loads(payload)))

        def stop():
            engine.stop(wait=True)
            for worker in (engine._reader, engine._writer, engine._errpump):
                if worker is not None:
                    worker.join(timeout=8)
                    self.assertFalse(worker.is_alive())

        self.addCleanup(stop)

        def wait_for(predicate):
            deadline = time.monotonic() + 20
            while not predicate() and time.monotonic() < deadline:
                QTest.qWait(10)
            self.assertTrue(predicate(), speech)

        def start():
            count = len(inventories)
            engine.start()
            wait_for(lambda: len(inventories) > count)
            current = next(row["id"] for row in inventories[-1]
                           if row["text"] == "Right ${var:duplicate_Right}")
            self.assertEqual(current, selected)

        def fire(number, expected):
            offset = len(speech)
            engine.feed_log("TN_DUP_Right")
            wait_for(lambda: f"Done Right {number}" in speech[offset:])
            self.assertEqual(speech[offset:], expected)

        start()
        fire(1, ["Done Right 1"])
        stop()
        self.first.write_bytes(b"<bad" + b" " * (len(self.originals[self.first]) - 4))
        os.utime(self.first, ns=(self.stamp, self.stamp))
        start()
        fire(1, ["Done Right 1"])
        self.window._toggle_engine_row("triggernometry:" + selected, True)
        fire(2, ["Custom 2", "Done Right 2"])
        stop()
        self.window._toggle_engine_row("triggernometry:" + selected, False)
        self.first.write_bytes(self.originals[self.first])
        os.utime(self.first, ns=(self.stamp, self.stamp))
        start()
        fire(1, ["Done Right 1"])
        self.window._toggle_engine_row("triggernometry:" + selected, True)
        fire(2, ["Custom 2", "Done Right 2"])
        self.assertEqual(self.second.read_bytes(), self.originals[self.second])
        self.assertEqual(self.first.read_bytes(), self.originals[self.first])


if __name__ == "__main__":
    unittest.main()
