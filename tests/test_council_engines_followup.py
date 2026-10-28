import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
import xml.etree.ElementTree as ET

from tests.test_triggernometry_host import replay
from tests.test_triggernometry_telesto import FakeTelesto, envelope
from nyaatriggers.triggernometry_telesto import TriggernometryTelesto


class NestedHostActions(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.name != "posix" or not shutil.which("mono") or not shutil.which("xvfb-run"):
            raise unittest.SkipTest("Requires Mono and Xvfb")

    def pack(self):
        document = ET.Element("TriggernometryExport", Version="1")
        folder = ET.SubElement(document, "ExportedFolder",
                               Id="56cc288f-c554-4f78-af74-c6d9df935d63",
                               Name="Nested actions", Enabled="true")
        trigger = ET.SubElement(ET.SubElement(folder, "Triggers"), "Trigger",
                                Id="80e356d4-ac95-48c1-aace-aed6c455507d",
                                Name="Nested actions", Enabled="true", Sequential="True",
                                Source="FFXIVNetwork", RegularExpression=r"^00\|.*NESTED")
        actions = ET.SubElement(trigger, "Actions")
        return document, actions

    def action(self, actions, kind, **values):
        return ET.SubElement(actions, "Action", ActionType=kind,
                             OrderNumber=str(len(actions) + 1), Asynchronous="False", **values)

    def loop(self, actions):
        loop = self.action(actions, "Loop")
        condition = ET.SubElement(loop, "LoopCondition", Enabled="true", Grouping="And")
        ET.SubElement(condition, "ConditionSingle", Enabled="true",
                      ExpressionL="${_i}", ExpressionTypeL="Numeric", ExpressionR="1",
                      ExpressionTypeR="Numeric", ConditionType="NumericLess")
        return ET.SubElement(loop, "LoopActions")

    def test_nested_scripts_receive_the_same_assembly_fallback_as_top_level_scripts(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            document, actions = self.pack()
            script = ('Triggernometry.Interpreter.StaticHelpers.SetScalarVariable(false, '
                      '"{name}", "ready");')
            self.action(actions, "ExecuteScript", ExecScriptExpression=script.format(name="outer"))
            self.action(actions, "UseTTS", UseTTSTextExpression="Outer ${var:outer}")
            nested = self.loop(actions)
            self.action(nested, "ExecuteScript", ExecScriptExpression=script.format(name="inner"))
            self.action(nested, "UseTTS", UseTTSTextExpression="Inner ${var:inner}")
            self.action(actions, "UseTTS", UseTTSTextExpression="Finished ${var:inner}")
            path = root / "nested.xml"
            ET.ElementTree(document).write(path, encoding="utf-8")
            original = path.read_bytes()
            with replay(path) as host:
                host.log("00", "0038", "Tester", "NESTED")
                host.until(lambda frame: str(frame.get("tts", "")).startswith("Finished"))
                self.assertEqual(host.calls, ["Outer ready", "Inner ready", "Finished ready"],
                                 host.errors())
            self.assertEqual(path.read_bytes(), original)

    def test_nested_telesto_actions_use_owned_resources_and_shutdown_cleanup(self):
        with tempfile.TemporaryDirectory() as temporary, FakeTelesto() as peer:
            document, actions = self.pack()
            endpoint = "http://${_const[TelestoEndpoint]}:" + str(peer.server.server_port) + "/"

            def draw(target, name, destination):
                self.action(target, "GenericJson", JsonEndpointExpression=destination,
                            JsonPayloadExpression=json.dumps(envelope(
                                "EnableDoodle", name=name, type="circle", radius="5")))

            draw(actions, "outer", endpoint)
            nested = self.loop(actions)
            draw(nested, "inner", endpoint)
            draw(nested, "independent", peer.url)
            peer.drawings["unrelated"] = {"name": "unrelated"}
            self.action(actions, "UseTTS", UseTTSTextExpression="Finished drawings")
            path = Path(temporary) / "nested.xml"
            ET.ElementTree(document).write(path, encoding="utf-8")
            original = path.read_bytes()
            errors = []
            relay = TriggernometryTelesto(lambda _body: None, errors.append, peer.url)
            relay.start()
            try:
                with replay(path, relay) as host:
                    host.log("00", "0038", "Tester", "NESTED")
                    host.until(lambda frame: frame.get("tts") == "Finished drawings")
                    messages = [peer.messages.get(timeout=3) for _ in range(3)]
                    self.assertEqual({message["type"] for message in messages}, {"EnableDoodle"})
                    self.assertEqual({message["payload"]["name"] for message in messages},
                                     {relay.prefix + "outer", relay.prefix + "inner", "independent"})
                    self.assertEqual(set(peer.drawings),
                                     set(relay._owned_drawings) | {"unrelated", "independent"})
            finally:
                relay.close(wait=True)
            self.assertTrue(relay.is_finished())
            self.assertEqual(set(peer.drawings), {"unrelated", "independent"})
            self.assertEqual(errors, [])
            self.assertEqual(path.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
