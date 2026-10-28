from contextlib import ExitStack
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QInputDialog

from nyaatriggers import app_common as ac
from nyaatriggers import main_window as mw
from nyaatriggers.telesto_client import TelestoClient
from nyaatriggers.triggernometry_bridge import TriggernometryBridge
from nyaatriggers.triggernometry_editor import PackDocument, prepared_packs, xml_bytes
from nyaatriggers.ui.triggernometry_editor import pack_speech_rows, speech_rows
from tests.test_triggernometry_host import replay
from tests.test_triggernometry_telesto import FakeTelesto, envelope


CORE = Path(__file__).resolve().parents[1] / "triggernometry-core"
IDENT = "80e356d4-ac95-48c1-aace-aed6c455507d"
WITNESS = "b082a9d5-592d-4978-95bf-547c8423400f#0"
NESTED = IDENT + "#loop:1.1"
DEEP = IDENT + "#loop:1.2.0"
SECOND = IDENT + "#loop:3.0"


def action(parent, kind, order, **values):
    return ET.SubElement(parent, "Action", ActionType=kind, OrderNumber=str(order),
                         Asynchronous="False", **values)


def loop(parent, order):
    value = action(parent, "Loop", order, LoopInitExpression="0", LoopIncrExpression="1")
    condition = ET.SubElement(value, "LoopCondition", Enabled="true", Grouping="And")
    ET.SubElement(condition, "ConditionSingle", Enabled="true", ExpressionL="${_i}",
                  ExpressionTypeL="Numeric", ExpressionR="1", ExpressionTypeR="Numeric",
                  ConditionType="NumericLess")
    return ET.SubElement(value, "LoopActions")


def pack(witness_url):
    root = ET.Element("TriggernometryExport", Version="1")
    folder = ET.SubElement(root, "ExportedFolder", Id="56cc288f-c554-4f78-af74-c6d9df935d63",
                           Name="UMAD", Enabled="true")
    triggers = ET.SubElement(folder, "Triggers")
    trigger = ET.SubElement(triggers, "Trigger", Id=IDENT.upper(), Name="Loop speech",
                            Enabled="true", Sequential="True", Source="FFXIVNetwork",
                            RegularExpression=r"LOOP_SPEECH_FIRE (?<turn>[0-9]+)")
    actions = ET.SubElement(trigger, "Actions")
    action(actions, "UseTTS", 10, UseTTSTextExpression="Top before ${turn}")
    nested = loop(actions, 20)
    action(nested, "ExecuteScript", 10, ExecScriptExpression=(
        'int count; int.TryParse(Triggernometry.Interpreter.StaticHelpers.GetScalarVariable(false, '
        '"loopcount"), out count); Triggernometry.Interpreter.StaticHelpers.SetScalarVariable(false, '
        '"loopcount", (count + 1).ToString());'))
    action(nested, "UseTTS", 20, UseTTSTextExpression="Nested ${var:loopcount}")
    deep = loop(nested, 30)
    # These bodies test inventory depth without assuming nested loop execution.
    nested[-1].find("LoopCondition").set("Enabled", "false")
    action(deep, "UseTTS", 10, UseTTSTextExpression="Deep ${var:loopcount}")
    action(actions, "UseTTS", 30, UseTTSTextExpression="Top after ${turn}")
    second = loop(actions, 40)
    actions[-1].find("LoopCondition").set("Enabled", "false")
    action(second, "UseTTS", 10, UseTTSTextExpression="Second ${var:loopcount}")
    action(actions, "GenericJson", 50, JsonEndpointExpression=witness_url,
           JsonPayloadExpression=json.dumps(envelope("Witness", count="${var:loopcount}")))
    other = ET.SubElement(triggers, "Trigger", Id=WITNESS.split("#")[0], Name="Witness",
                          Enabled="true", Sequential="True", Source="FFXIVNetwork",
                          RegularExpression="LOOP_SPEECH_WITNESS")
    action(ET.SubElement(other, "Actions"), "UseTTS", 10, UseTTSTextExpression="Witness ${var:loopcount}")
    return root


def expected_rows():
    return {"Top before ${turn}": IDENT + "#0", "Top after ${turn}": IDENT + "#1",
            "Nested ${var:loopcount}": NESTED, "Deep ${var:loopcount}": DEEP,
            "Second ${var:loopcount}": SECOND, "Witness ${var:loopcount}": WITNESS}


class LoopSpeechInventoryTests(unittest.TestCase):
    def test_static_inventory_keeps_legacy_ids_and_distinguishes_nested_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            root = pack("http://127.0.0.1:1/")
            # Execution order differs from storage order without changing row identities.
            root.find(".//Trigger/Actions/Action").set("OrderNumber", "99")
            path = directory / "loop.xml"
            path.write_bytes(xml_bytes(root))
            original = path.read_bytes()
            rows, incomplete = pack_speech_rows(directory)
            self.assertFalse(incomplete)
            self.assertEqual({row["text"]: row["id"] for row in rows}, expected_rows())
            self.assertEqual(len({row["id"] for row in rows}), len(rows))
            script = ('import json,sys; from pathlib import Path; '
                      'from nyaatriggers.ui.triggernometry_editor import pack_speech_rows; '
                      'print(json.dumps(pack_speech_rows(Path(sys.argv[1]))))')
            result = subprocess.run([sys.executable, "-c", script, str(directory)],
                                    cwd=CORE.parent, capture_output=True, text=True, timeout=30, check=True)
            self.assertEqual(json.loads(result.stdout), [rows, False])
            self.assertEqual(path.read_bytes(), original)

    @unittest.skipUnless(os.name == "posix" and shutil.which("mono") and shutil.which("xvfb-run"),
                         "Requires Mono and Xvfb")
    def test_prepared_pack_order_and_duplicate_ids_agree_with_native_inventory(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            managed = directory / "packs"
            managed.mkdir()
            paths = [managed / "b.xml", managed / "a.xml"]
            for path in paths:
                root = pack("http://127.0.0.1:1/")
                actions = root.find(".//Trigger/Actions")
                actions[0].set("OrderNumber", "99")
                action(actions, "UseTTS", 1, UseTTSTextExpression="")
                path.write_bytes(xml_bytes(root))
            originals = {path: path.read_bytes() for path in paths}
            rows, incomplete = pack_speech_rows(managed)
            self.assertFalse(incomplete)
            identifiers = {row["id"] for row in rows}
            self.assertEqual(len(identifiers), 12)
            self.assertEqual({row["id"] for row in rows if row["text"] == "Top after ${turn}"},
                             {ident.split("#")[0] + "#1" for ident in identifiers if "#loop:" in ident})
            prepared = prepared_packs(paths)
            self.assertEqual([path for path, _raw in prepared], sorted(paths))
            runtime = []
            for index, (_source, raw) in enumerate(prepared):
                target = directory / f"runtime-{index}.xml"
                target.write_bytes(raw)
                runtime.append(target)
            with replay(runtime[0], extra_packs=runtime[1:]) as host:
                self.assertEqual({row["id"]: row["text"] for row in host.inventory},
                                 {row["id"]: row["text"] for row in rows})
                self.assertEqual(len(host.inventory), len(rows))
            self.assertEqual(pack_speech_rows(managed), (rows, False))
            self.assertEqual({path: path.read_bytes() for path in paths}, originals)

    @unittest.skipUnless(os.name == "posix" and shutil.which("mono") and shutil.which("xvfb-run"),
                         "Requires Mono and Xvfb")
    def test_native_inventory_covers_deep_paths_and_controls_executing_loop(self):
        with tempfile.TemporaryDirectory() as temporary, FakeTelesto() as peer:
            directory = Path(temporary)
            root = pack(peer.url)
            path = directory / "loop.xml"
            path.write_bytes(xml_bytes(root))
            original = path.read_bytes()
            with replay(path) as host:
                native = {row["text"]: row["id"] for row in host.inventory}
                self.assertEqual(native, expected_rows())
                self.assertEqual(native, {row["text"]: row["id"] for row in speech_rows(root)})

                def fire(number, expected):
                    start = len(host.calls)
                    host.log("00", "0038", "Tester", f"LOOP_SPEECH_FIRE {number}")
                    message = peer.messages.get(timeout=20)
                    self.assertEqual(message["type"], "Witness")
                    self.assertEqual(message["payload"]["count"], str(number), host.errors())
                    host.log("00", "0038", "Tester", "LOOP_SPEECH_WITNESS")
                    host.until(lambda frame: frame.get("tts") == f"Witness {number}")
                    self.assertEqual(host.calls[start:], [*expected, f"Witness {number}"], host.errors())

                fire(1, ["Top before 1", "Nested 1", "Top after 1"])
                host.send(t="set_callout", id=NESTED, text="Custom ${var:loopcount}")
                host.send(t="set_display_text", id=NESTED, text="Visual ${var:loopcount}")
                host.send(t="set_disabled", ids=[NESTED, DEEP, SECOND])
                fire(2, ["Top before 2", "Top after 2"])
                host.send(t="set_disabled", ids=[])
                fire(3, ["Top before 3", "Custom 3", "Top after 3"])
                host.send(t="set_callout", id=NESTED, text=None)
                host.send(t="set_display_text", id=NESTED, text=None)
                fire(4, ["Top before 4", "Nested 4", "Top after 4"])
            self.assertEqual(path.read_bytes(), original)


class LoopSpeechUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.name != "posix" or not shutil.which("mono") or not shutil.which("xvfb-run"):
            raise unittest.SkipTest("Requires Mono and Xvfb")
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        # Triggevent is deliberately unavailable in this Triggernometry replay.
        self.stack.enter_context(patch("nyaatriggers.triggevent_bridge.TriggeventBridge.is_available", return_value=False))
        self.temp = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.packs = self.temp / "triggernometry-packs"
        self.packs.mkdir()
        for key, value in {
            "_DATA_DIR": self.temp, "_SETTINGS_FILE": self.temp / "settings.json",
            "_TRIGGERNOMETRY_INVENTORY_CACHE": self.temp / "inventory.json",
            "TRIGGERS_LOCAL_FILE": self.temp / "triggers.local.json",
            "TRIGGERS_FILE": self.temp / "triggers.json",
            "_REPO_TRIGGERS_FILE": self.temp / "downloaded.json",
            "_REPO_RETIRED_FILE": self.temp / "retired.json",
            "_REPO_TRIGGERS_VERSION": self.temp / "downloaded.version",
            "_USER_VOICES_DIR": self.temp / "voices",
        }.items():
            self.stack.enter_context(patch.object(ac, key, value))
        self.stack.enter_context(patch.object(mw, "_DATA_DIR", self.temp))
        self.stack.enter_context(patch("nyaatriggers.drop_log._LOG_FILE", self.temp / "program.log"))
        self.stack.enter_context(patch.dict(os.environ, {
            "XDG_CONFIG_HOME": str(self.temp / "runtime"),
            "NYAA_TRIGGERNOMETRY_PACKS": str(self.packs),
            "NYAA_TRIGGERNOMETRY_EXE": str(CORE / "bin" / "triggernometry-core.exe"),
        }))
        for target in ((TelestoClient, "start"), (mw.PluginLink, "start"),
                       (mw.QTimer, "singleShot"), (mw, "kokoro_ready"), (mw, "_ensure_worker"),
                       (ac.QMessageBox, "warning"), (ac.QMessageBox, "information")):
            self.stack.enter_context(patch.object(*target))
        self.critical = self.stack.enter_context(patch.object(ac.QMessageBox, "critical"))
        self.peer = self.stack.enter_context(FakeTelesto())
        ac.TRIGGERS_FILE.write_text("[]")
        ac._SETTINGS_FILE.write_text(json.dumps({
            "tts_engine": "system", "auto_connect": False, "auto_check_updates": False,
            "local_enabled": False, "ui_language": "en", "telesto_url": self.peer.url,
        }))
        self.window = self.new_window()
        self.path = self.packs / "loop.xml"
        source = self.temp / "loop.xml"
        source.write_bytes(xml_bytes(pack(self.peer.url)))
        self.original = source.read_bytes()
        self.window._triggers_enabled = False
        with patch.object(ac.QFileDialog, "getOpenFileName", return_value=(str(source), "")):
            self.window._import_triggernometry()
        self.critical.assert_not_called()
        self.assertEqual(self.path.read_bytes(), self.original)
        self.spoken, self.displayed, self.inventories = [], [], []
        self.attach(self.window)
        self.addCleanup(self.stop)

    def new_window(self):
        window = mw.MainWindow()
        self.addCleanup(window._prog_sessions.close)
        self.addCleanup(window.close)
        return window

    def attach(self, window):
        self.window = window
        self.bridge = TriggernometryBridge()
        self.window._triggernometry = self.bridge
        self.window._triggernometry_mode = True
        self.window._triggers_enabled = True
        self.window._cactbot_mode = False
        self.bridge.tts.connect(lambda text, generation: self.spoken.append(text))
        self.bridge.callout.connect(lambda text, severity, generation: self.displayed.append(text))
        self.bridge.inventory.connect(window._on_triggernometry_inventory)
        self.bridge.inventory.connect(lambda payload, generation: self.inventories.append(json.loads(payload)))

    def stop(self):
        self.bridge.stop(wait=True)
        for worker in (self.bridge._reader, self.bridge._errpump, self.bridge._writer):
            if worker is not None:
                worker.join(timeout=8)
                self.assertFalse(worker.is_alive())
        self.app.processEvents()

    def wait_for(self, predicate):
        deadline = time.monotonic() + 20
        while not predicate() and time.monotonic() < deadline:
            QTest.qWait(10)
        self.assertTrue(predicate(), self.spoken)

    def start(self):
        count = len(self.inventories)
        self.bridge.start()
        self.wait_for(lambda: len(self.inventories) > count)

    def fire(self, number, expected, visual=None):
        start, display_start = len(self.spoken), len(self.displayed)
        self.bridge.feed_log(f"00|2026-10-04T12:00:00Z|0038|Tester|LOOP_SPEECH_FIRE {number}|test")
        messages = []

        def witnessed():
            try:
                messages.append(self.peer.messages.get_nowait())
            except queue.Empty:
                pass
            return bool(messages)

        self.wait_for(witnessed)
        self.assertEqual(messages, [envelope("Witness", count=str(number))])
        self.window._toggle_engine_row("triggernometry:" + WITNESS, True)
        self.bridge.feed_log("00|2026-10-04T12:00:00Z|0038|Tester|LOOP_SPEECH_WITNESS|test")
        self.wait_for(lambda: f"Witness {number}" in self.spoken[start:])
        self.assertEqual(self.spoken[start:], [*expected, f"Witness {number}"])
        self.assertEqual(self.displayed[display_start:], [*(expected if visual is None else visual), f"Witness {number}"])

    def test_global_fight_profile_restart_edit_and_reset_preserve_nested_speech_controls(self):
        self.start()
        self.fire(1, ["Top before 1", "Nested 1", "Top after 1"])
        self.window._set_triggernometry_callout_edit(NESTED, "Custom ${var:loopcount}")
        self.window._set_display_text("triggernometry:" + NESTED, "Visual ${var:loopcount}")
        self.window._global_tn_on_flag = True
        self.window._toggle_global_tn()
        self.fire(2, [])
        self.window._toggle_global_tn()
        self.fire(3, ["Top before 3", "Custom 3", "Top after 3"],
                  ["Top before 3", "Visual 3", "Top after 3"])
        self.window._set_fight_tn("UMAD", False)
        self.fire(4, [])
        with patch.object(QInputDialog, "getText", return_value=("Loop mute", True)):
            self.window._save_new_profile()
        profile = self.window._profiles[-1]
        self.assertFalse(profile["engines"]["triggernometry"][NESTED]["enabled"])
        self.assertEqual(profile["engines"]["triggernometry"][NESTED]["text"], "Custom ${var:loopcount}")
        self.stop()
        self.window.close()
        restarted = self.new_window()
        self.attach(restarted)
        self.assertEqual({row["text"]: row["id"] for row in restarted._engine_inventory
                          if row.get("source") == "triggernometry"}, expected_rows())
        self.assertIn(NESTED, restarted._triggernometry_disabled)
        self.assertEqual(restarted._triggernometry_callout_edits[NESTED], "Custom ${var:loopcount}")
        self.start()
        self.fire(1, [])
        restarted._set_fight_tn("UMAD", True)
        self.fire(2, ["Top before 2", "Custom 2", "Top after 2"],
                  ["Top before 2", "Visual 2", "Top after 2"])
        restored = next(item for item in restarted._profiles if item["id"] == profile["id"])
        self.assertTrue(restarted._activate_profile(restored))
        self.fire(3, [])
        restarted._set_fight_tn("UMAD", True)
        restarted._reset_triggernometry_callout_edit(NESTED)
        restarted._set_display_text("triggernometry:" + NESTED, "")
        self.fire(4, ["Top before 4", "Nested 4", "Top after 4"])
        self.assertEqual(self.path.read_bytes(), self.original)
        restarted._toggle_engine_row("triggernometry:" + NESTED, False)
        restarted._set_triggernometry_callout_edit(NESTED, "Before edit")
        document = PackDocument.load(self.path)
        document.root.find(".//LoopActions/Action[@ActionType='UseTTS']").set("UseTTSTextExpression", "Edited ${var:loopcount}")
        restarted._in_game_combat = True
        restarted._save_triggernometry_document(document)
        self.assertNotIn(NESTED, restarted._triggernometry_callout_edits)
        self.assertNotIn("triggernometry:" + NESTED, restarted._trigger_display_edits)
        self.assertTrue({IDENT + "#0", IDENT + "#1", NESTED, DEEP, SECOND}.issubset(restarted._triggernometry_disabled))
        count = len(self.inventories)
        restarted._in_game_combat = False
        restarted._apply_pending_triggernometry_packs()
        self.wait_for(lambda: len(self.inventories) > count)
        self.fire(1, [])
        restarted._set_fight_tn("UMAD", True)
        self.fire(2, ["Top before 2", "Edited 2", "Top after 2"])


if __name__ == "__main__":
    unittest.main()
