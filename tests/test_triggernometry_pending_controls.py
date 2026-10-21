from contextlib import ExitStack
from copy import deepcopy
import json
import os
from pathlib import Path
import shutil
import threading
import time
import unittest
from unittest.mock import patch
import uuid
import xml.etree.ElementTree as ET

from PyQt6.QtCore import Qt
from PyQt6.QtTest import QTest

from nyaatriggers import app_common as ac
from nyaatriggers.telesto_client import TelestoClient
from nyaatriggers.triggernometry_bridge import TriggernometryBridge
from nyaatriggers.triggernometry_editor import PackDocument, xml_bytes
from tests import test_session_ui as fixture


CORE = Path(__file__).resolve().parents[1] / "triggernometry-core"


class TriggernometryPendingControlTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.name != "posix" or not shutil.which("mono") or not shutil.which("xvfb-run"):
            message = "Requires Mono and Xvfb"
            if os.environ.get("GITHUB_ACTIONS") == "true":
                raise RuntimeError(message)
            raise unittest.SkipTest(message)
        fixture.SessionUiTests.setUpClass()

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.case = fixture.SessionUiTests()
        self.addCleanup(self.case.doCleanups)
        with patch.object(TelestoClient, "start"):
            self.case.setUp()
        self.window = self.case.window
        self.packs = self.case.temp / "triggernometry-packs"
        self.stack.enter_context(patch.dict(os.environ, {
            "XDG_CONFIG_HOME": str(self.case.temp / "runtime"),
            "NYAA_TRIGGERNOMETRY_PACKS": str(self.packs),
            "NYAA_TRIGGERNOMETRY_EXE": str(CORE / "bin" / "triggernometry-core.exe"),
        }))
        self.bridge = TriggernometryBridge()
        self.window._triggernometry = self.bridge
        self.window._triggernometry_mode = True
        self.window._triggers_enabled = True
        self.window._cactbot_mode = False
        self.window._engine_inventory = []
        self.spoken = []
        self.displayed = []
        self.inventories = []
        self.bridge.tts.connect(lambda text, generation: self.spoken.append(text))
        self.bridge.callout.connect(lambda text, severity, generation: self.displayed.append(text))
        self.bridge.inventory.connect(self.window._on_triggernometry_inventory)
        self.bridge.inventory.connect(lambda payload, generation: self.inventories.append(json.loads(payload)))
        self.addCleanup(self.stop_bridge)

    def stop_bridge(self):
        self.bridge.stop(wait=True)
        for worker in (self.bridge._reader, self.bridge._errpump, self.bridge._writer):
            if worker is not None:
                worker.join(timeout=8)
                self.assertFalse(worker.is_alive())
        fixture.SessionUiTests.app.processEvents()

    def wait_for(self, predicate):
        deadline = time.monotonic() + 20
        while not predicate() and time.monotonic() < deadline:
            QTest.qWait(10)
        self.assertTrue(predicate(), self.spoken)

    def start_bridge(self):
        count = len(self.inventories)
        self.bridge.start()
        self.wait_for(lambda: len(self.inventories) > count)

    def pack(self, labels):
        tree = ET.parse(CORE / "test" / "spike-script-pack.xml")
        container = tree.find(".//Triggers")
        original = container[0]
        container.clear()
        identifiers = {}
        for label in labels:
            trigger = deepcopy(original)
            trigger.set("Id", str(uuid.uuid4()))
            trigger.set("Name", label)
            trigger.set("RegularExpression", "PENDING_" + label)
            actions = trigger.find("Actions")
            actions[:] = [
                ET.Element("Action", OrderNumber="1", ActionType="UseTTS", Asynchronous="False",
                           UseTTSTextExpression="Old " + label),
                ET.Element("Action", OrderNumber="2", ActionType="UseTTS", Asynchronous="False",
                           UseTTSTextExpression="Done " + label),
            ]
            container.append(trigger)
            identifiers[label] = trigger.get("Id") + "#0"
        path = self.packs / "pending.xml"
        tree.write(path, encoding="utf-8")
        return path, identifiers

    def fire(self, label):
        start = len(self.spoken)
        self.bridge.feed_log("00|2026-10-04T12:00:00Z|0038|Player|PENDING_" + label + "|test")
        self.wait_for(lambda: "Done " + label in self.spoken[start:])
        return self.spoken[start:]

    def test_pending_pack_controls_wait_for_reload_and_then_apply_to_new_actions(self):
        labels = ("EDIT", "RESET", "REPLAY", "TOGGLE", "DISABLED")
        path, identifiers = self.pack(labels)
        self.start_bridge()
        self.bridge.set_callout(identifiers["RESET"], tts="Active old reset")
        self.window._triggernometry_callout_edits[identifiers["RESET"]] = "Active old reset"
        document = PackDocument.load(path)
        for trigger in document.root.iter("Trigger"):
            actions = trigger.find("Actions")
            for order, action in enumerate(actions, 2):
                action.set("OrderNumber", str(order))
            actions.insert(0, ET.Element("Action", OrderNumber="1", ActionType="UseTTS", Asynchronous="False",
                                         UseTTSTextExpression="New " + trigger.get("Name")))
        self.window._in_game_combat = True
        self.window._save_triggernometry_document(document)
        self.assertTrue(self.window._triggernometry_reload_pending)
        self.window._set_triggernometry_callout_edit(identifiers["EDIT"], "Staged edit")
        self.window._set_display_text("triggernometry:" + identifiers["EDIT"], "Staged visual")
        self.window._reset_triggernometry_callout_edit(identifiers["RESET"])
        self.window._triggernometry_callout_edits[identifiers["REPLAY"]] = "Staged replay"
        self.window._replay_triggernometry_callout_edits()
        self.window._toggle_engine_row("triggernometry:" + identifiers["TOGGLE"], False)
        self.window._triggernometry_disabled.add(identifiers["DISABLED"])
        self.window._apply_engine_disabled("triggernometry")
        self.window._persist_engine_disabled("triggernometry")
        for label in labels:
            with self.subTest(active_pack=label):
                text = "Active old reset" if label == "RESET" else "Old " + label
                display_start = len(self.displayed)
                self.assertEqual(self.fire(label), [text, "Done " + label])
                self.assertEqual(self.displayed[display_start:], [text, "Done " + label])
        settings = json.loads((self.case.temp / "settings.json").read_text())
        self.assertEqual(settings["triggernometry_callout_edits"][identifiers["EDIT"]], "Staged edit")
        self.assertEqual(settings["trigger_display_edits"]["triggernometry:" + identifiers["EDIT"]],
                         "Staged visual")
        self.assertIn(identifiers["TOGGLE"], settings["triggernometry_disabled_triggers"])
        self.assertIn(identifiers["DISABLED"], settings["triggernometry_disabled_triggers"])
        inventory_count = len(self.inventories)
        self.window._in_game_combat = False
        self.window._apply_pending_triggernometry_packs()
        self.wait_for(lambda: len(self.inventories) > inventory_count)
        self.assertFalse(self.window._triggernometry_reload_pending)
        for label in labels:
            with self.subTest(reloaded_pack=label):
                first = {"EDIT": "Staged edit", "REPLAY": "Staged replay"}.get(label, "New " + label)
                expected = ["Old " + label, "Done " + label]
                if label not in ("TOGGLE", "DISABLED"):
                    expected.insert(0, first)
                display_start = len(self.displayed)
                self.assertEqual(self.fire(label), expected)
                visual = ["Staged visual", *expected[1:]] if label == "EDIT" else expected
                self.assertEqual(self.displayed[display_start:], visual)

    def test_native_rejected_pack_retains_controls_until_repair(self):
        path, identifiers = self.pack(("REPAIR",))
        ident = identifiers["REPAIR"]
        self.start_bridge()
        self.window._set_triggernometry_callout_edit(ident, "Custom repaired speech")
        self.window._toggle_engine_row("triggernometry:" + ident, False)
        self.stop_bridge()
        original = path.read_bytes()
        broken = ET.fromstring(original)
        broken.find(".//Trigger").set("Source", "NotAnEngineSource")
        path.write_bytes(xml_bytes(broken))
        self.window._load_cached_triggernometry_inventory()
        self.start_bridge()
        self.assertEqual(self.inventories[-1], [])
        self.assertIn(ident, self.window._triggernometry_disabled)
        self.assertEqual(self.window._triggernometry_callout_edits[ident], "Custom repaired speech")
        cached = json.loads(ac._TRIGGERNOMETRY_INVENTORY_CACHE.read_text())
        self.assertTrue(any(row["id"] == ident for row in cached))
        self.stop_bridge()
        path.write_bytes(original)
        self.window._load_cached_triggernometry_inventory()
        self.start_bridge()
        self.assertIn(ident, self.window._triggernometry_disabled)
        self.assertEqual(self.window._triggernometry_callout_edits[ident], "Custom repaired speech")
        self.assertEqual(self.fire("REPAIR"), ["Done REPAIR"])
        self.stop_bridge()
        self.window._toggle_engine_row("triggernometry:" + ident, True)
        self.start_bridge()
        self.assertEqual(self.fire("REPAIR"), ["Custom repaired speech", "Done REPAIR"])

    def test_reconnect_consumes_pending_reload_only_after_a_new_successful_start(self):
        labels = ("RESTART_EDIT", "RESTART_MUTE")
        path, identifiers = self.pack(labels)
        self.start_bridge()
        document = PackDocument.load(path)
        for trigger in document.root.iter("Trigger"):
            actions = trigger.find("Actions")
            for order, action in enumerate(actions, 2):
                action.set("OrderNumber", str(order))
            actions.insert(0, ET.Element("Action", OrderNumber="1", ActionType="UseTTS", Asynchronous="False",
                                         UseTTSTextExpression="New " + trigger.get("Name")))
        self.window._in_game_combat = True
        self.window._save_triggernometry_document(document)
        self.window._set_triggernometry_callout_edit(identifiers["RESTART_EDIT"], "Custom staged restart")
        self.window._set_display_text("triggernometry:" + identifiers["RESTART_EDIT"], "Restart visual")
        self.window._toggle_engine_row("triggernometry:" + identifiers["RESTART_MUTE"], False)
        generation = self.bridge.generation()
        self.window._set_triggernometry_enabled(True)
        self.assertTrue(self.window._triggernometry_reload_pending)
        self.assertEqual(self.bridge.generation(), generation)
        self.assertFalse(self.bridge.pack_is_current(path))
        for label in labels:
            display_start = len(self.displayed)
            self.assertEqual(self.fire(label), ["Old " + label, "Done " + label])
            self.assertEqual(self.displayed[display_start:], ["Old " + label, "Done " + label])
        self.window._on_status_changed(False, "Disconnected")
        self.window._on_triggernometry_connection(False, "Disconnected")
        self.assertFalse(self.bridge.is_active())
        self.window._on_status_changed(True, "Connected")
        self.window._in_game_combat = True
        with patch("nyaatriggers.triggernometry_bridge.subprocess.Popen", side_effect=OSError("Spawn failed")):
            self.window._on_triggernometry_connection(True, "Connected")
        self.assertTrue(self.window._triggernometry_reload_pending)
        self.assertFalse(self.bridge.is_active())
        count = len(self.inventories)
        self.window._on_triggernometry_connection(True, "Connected")
        early_spoken = []
        early_displayed = []
        finished = set()
        done = threading.Event()

        def capture(text, generation):
            early_spoken.append(text)
            if text in {"Done " + label for label in labels}:
                finished.add(text)
                if len(finished) == len(labels):
                    done.set()

        self.bridge.tts.connect(capture, Qt.ConnectionType.DirectConnection)
        def capture_display(text, severity, generation):
            early_displayed.append(text)
        self.bridge.callout.connect(capture_display, Qt.ConnectionType.DirectConnection)
        try:
            for label in labels:
                self.bridge.feed_log("00|2026-10-04T12:00:00Z|0038|Player|PENDING_" + label + "|test")
            # Hold Qt inventory delivery while queued native input completes.
            self.assertTrue(done.wait(timeout=20), early_spoken)
        finally:
            self.bridge.tts.disconnect(capture)
            self.bridge.callout.disconnect(capture_display)
        self.wait_for(lambda: len(self.inventories) > count)
        with self.subTest(restart="pending status"):
            self.assertFalse(self.window._triggernometry_reload_pending)
            self.assertTrue(self.window._triggernometry_editor_status.isHidden())
        self.assertTrue(self.bridge.pack_is_current(path))
        self.assertGreater(self.bridge.generation(), generation)
        for label in labels:
            with self.subTest(restarted_pack=label):
                expected = ["Old " + label, "Done " + label]
                if label == "RESTART_EDIT":
                    expected.insert(0, "Custom staged restart")
                possible = {"New " + label, "Old " + label, "Done " + label}
                if label == "RESTART_EDIT":
                    possible.add("Custom staged restart")
                self.assertEqual([text for text in early_spoken if text in possible], expected)
        self.assertEqual(sorted(early_displayed), sorted([
            "Restart visual", "Old RESTART_EDIT", "Done RESTART_EDIT",
            "Old RESTART_MUTE", "Done RESTART_MUTE"]))


if __name__ == "__main__":
    unittest.main()
