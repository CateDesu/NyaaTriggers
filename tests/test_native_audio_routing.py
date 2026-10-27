import os
from pathlib import Path
import shutil
import tempfile
import unittest
import wave
import xml.etree.ElementTree as ET

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from tests.test_triggernometry_host import replay


class NativeAudioRoutingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.name != "posix" or not shutil.which("mono") or not shutil.which("xvfb-run"):
            message = "Requires Mono and Xvfb"
            if os.environ.get("GITHUB_ACTIONS") == "true":
                raise RuntimeError(message)
            raise unittest.SkipTest(message)

    def test_imported_audio_uses_host_hooks_in_folders_and_loops(self):
        routes = (
            ("default", {}, {}),
            ("ACT", {"TTSRouting": "ACT"}, {"SoundRouting": "ACT"}),
            ("Triggernometry", {"TTSRouting": "Triggernometry"}, {"SoundRouting": "Triggernometry"}),
            ("legacy", {"PlaySpeechMyself": "True"}, {"PlaySoundMyself": "True"}),
        )
        for nested in (False, True):
            for name, speech_route, sound_route in routes:
                with self.subTest(route=name, loop=nested), tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    sound = root / "probe.wav"
                    with wave.open(str(sound), "wb") as output:
                        output.setnchannels(1)
                        output.setsampwidth(2)
                        output.setframerate(8000)
                        output.writeframes(b"\0\0" * 80)
                    document = ET.Element("TriggernometryExport", Version="1")
                    folder = ET.SubElement(document, "ExportedFolder",
                                           Id="57461118-fc98-493c-9d5e-344b830dc0e0",
                                           Name="Audio routing", Enabled="true")
                    child = ET.SubElement(ET.SubElement(folder, "Folders"), "Folder",
                                          Id="34872cce-c02f-4227-86af-029c790e488a",
                                          Name="Nested audio", Enabled="true")
                    trigger = ET.SubElement(ET.SubElement(child, "Triggers"), "Trigger",
                                            Id="27cd37d9-e1eb-48c1-badc-ae3398cd1a6d",
                                            Name="Routing probe", Enabled="true", Sequential="True",
                                            Source="FFXIVNetwork", RegularExpression=r"^00\|.*ROUTE")
                    actions = ET.SubElement(trigger, "Actions")
                    audio = actions
                    if nested:
                        loop = ET.SubElement(actions, "Action", ActionType="Loop",
                                             OrderNumber="1", Asynchronous="False")
                        condition = ET.SubElement(loop, "LoopCondition", Enabled="true", Grouping="And")
                        ET.SubElement(condition, "ConditionSingle", Enabled="true",
                                      ExpressionL="${_i}", ExpressionTypeL="Numeric",
                                      ExpressionR="1", ExpressionTypeR="Numeric", ConditionType="NumericLess")
                        audio = ET.SubElement(loop, "LoopActions")
                    ET.SubElement(audio, "Action", ActionType="UseTTS", OrderNumber="1",
                                  Asynchronous="False", UseTTSTextExpression="Routed speech", **speech_route)
                    ET.SubElement(audio, "Action", ActionType="PlaySound", OrderNumber="2",
                                  Asynchronous="False", PlaySoundFileExpression=str(sound),
                                  PlaySoundVolumeExpression="37", **sound_route)
                    ET.SubElement(actions, "Action", ActionType="Variable", OrderNumber="3",
                                  Asynchronous="False", VariableOp="SetString",
                                  VariableName="routing_witness", VariableExpression="finished")
                    ET.SubElement(actions, "Action", ActionType="UseTTS", OrderNumber="4",
                                  Asynchronous="False", ExecutionDelayExpression="50", TTSRouting="ACT",
                                  UseTTSTextExpression="Probe ${var:routing_witness}")
                    pack = root / "routing.xml"
                    ET.ElementTree(document).write(pack, encoding="utf-8")
                    original = pack.read_bytes()
                    with replay(pack) as host:
                        frames = []

                        def completed(frame):
                            if frame.get("t") in ("callout", "sound"):
                                frames.append(frame)
                            return frame.get("tts") == "Probe finished"

                        host.log("00", "0038", "Speaker", "ROUTE")
                        host.until(completed)
                        self.assertEqual(frames, [
                            {"t": "callout", "tts": "Routed speech", "text": "Routed speech"},
                            {"t": "sound", "file": str(sound), "volume": 37},
                            {"t": "callout", "tts": "Probe finished", "text": "Probe finished"},
                        ])
                    self.assertEqual(pack.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
