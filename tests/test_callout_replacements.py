import json
from types import SimpleNamespace
import unittest

from PyQt6.QtCore import QCoreApplication

from nyaatriggers.cactbot_reader import CactbotReader
from nyaatriggers.trigger_engine import apply_replacements
from nyaatriggers.triggernometry_bridge import TriggernometryBridge
from nyaatriggers.triggevent_bridge import TriggeventBridge
from nyaatriggers.ui.engines import EnginesMixin


APP = QCoreApplication.instance() or QCoreApplication([])


class CalloutReplacementTests(unittest.TestCase):
    def test_plain_overrides_keep_backslashes_in_spoken_and_displayed_text(self):
        for source, bridge_type in (("cactbot", CactbotReader),
                                    ("triggevent", TriggeventBridge),
                                    ("triggernometry", TriggernometryBridge)):
            for replacement in (r"Celebrate \o/", r"Group \1", r"Literal \n"):
                with self.subTest(source=source, replacement=replacement):
                    bridge = bridge_type()
                    # Simulate engine output without starting a native process or browser.
                    bridge._active = True
                    bridge._gen = 1
                    host = SimpleNamespace(
                        _settings={}, _engine_text_overrides={source + ":test": {
                            "find": "Spread", "replace": replacement}},
                        _cactbot_reader=bridge, _triggevent=bridge, _triggernometry=bridge)
                    EnginesMixin._apply_engine_overrides(host, source)
                    spoken, displayed = [], []
                    bridge.tts.connect(lambda text, *args: spoken.append(text))
                    bridge.callout.connect(lambda text, *args: displayed.append(text))
                    if source == "cactbot":
                        for kind in ("say", "popup"):
                            bridge._on_message(kind, json.dumps({"text": "Spread"}))
                    else:
                        bridge._dispatch({"t": "callout", "tts": "Spread", "text": "Spread"}, gen=1)
                        APP.processEvents()
                    self.assertEqual(spoken, [replacement])
                    self.assertEqual(displayed, [replacement])

    def test_regex_rules_still_expand_capture_references(self):
        rules = [{"find": r"Group (\d+)", "replace": r"Party \1", "regex": True}]
        self.assertEqual(apply_replacements("Group 2", rules), "Party 2")

    def test_plain_rules_remain_case_insensitive_and_support_suppression(self):
        self.assertEqual(apply_replacements("SPREAD", [{"find": "Spread", "replace": ""}]), "")
        self.assertEqual(apply_replacements("A+B", [{"find": "a+b", "replace": "Stack"}]), "Stack")


if __name__ == "__main__":
    unittest.main()
