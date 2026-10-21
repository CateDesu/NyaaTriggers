import json
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

from nyaatriggers import app_common as ac
from nyaatriggers import triggevent_custom as custom
from nyaatriggers.triggevent_bridge import TriggeventBridge
from nyaatriggers.triggernometry_bridge import TriggernometryBridge
from nyaatriggers.ui.settings_tab import SettingsTabMixin
from nyaatriggers.ui.triggernometry_editor import speech_rows
from tests import test_trigger_engine_moves as moves
from tests import test_trigger_text as text_ui
from tests.test_triggevent_custom import example
from tests.test_triggernometry_editor import example as pack_example


class CustomDisplayRecoveryTests(unittest.TestCase):
    setUpClass = classmethod(text_ui.TriggerTextTests.setUpClass.__func__)
    setUp = text_ui.TriggerTextTests.setUp

    def bridge(self):
        bridge = TriggeventBridge()
        bridge._active = True
        bridge._custom_gen = bridge.generation()
        self.window._triggevent = bridge
        self.window._triggevent_mode = True
        self.addCleanup(setattr, self.window, "_triggevent", None)
        self.addCleanup(setattr, bridge, "_active", False)
        return bridge

    def test_invalid_builder_display_edits_preserve_the_saved_working_template(self):
        definition = example()
        self.window._custom_triggevent = [definition]
        key = "triggevent:" + definition["id"]
        bridge = self.bridge()
        with patch.object(bridge, "_send_command") as send:
            self.window._set_display_text(key, "Watch {target} near {source}")
            self.assertEqual(send.call_args.args[0]["triggers"][0]["display_text"],
                             "Watch {target} near {source}")
            saved = ac._SETTINGS_FILE.read_bytes()
            for invalid, message in (("Dodge {boss}", "Unknown callout placeholder"),
                                     ("Dodge {target", "Close each callout placeholder"),
                                     ("🐈" * 1001, "Text must be at most 2000 characters")):
                with self.subTest(message=message), patch.object(ac.QMessageBox, "warning") as warning:
                    send.reset_mock()
                    self.window._set_display_text(key, invalid)
                    warning.assert_called_once()
                    self.assertIn(message, warning.call_args.args[2])
                    send.assert_not_called()
                    self.assertEqual(ac._SETTINGS_FILE.read_bytes(), saved)
                    self.assertEqual(self.window._trigger_display_edits[key], "Watch {target} near {source}")

    def test_saved_invalid_overlay_cannot_block_other_custom_rules_on_restart(self):
        first, second = example(), example()
        second["name"] = "Independent rule"
        invalid = "Dodge {boss}"
        edits = {"triggevent:" + first["id"]: invalid,
                 "triggevent:" + second["id"]: "Watch {target}"}
        with patch.object(custom, "CUSTOM_FILE", self.case.case.temp / "custom.json"):
            custom.save_triggers([first, second])
            self.window._settings["trigger_display_edits"] = edits
            self.assertTrue(self.window._save_settings())
            SettingsTabMixin._load_settings(self.window)
            self.window._init_engines()
            bridge = self.bridge()
            with patch.object(bridge, "_send_command") as send:
                self.window._sync_custom_triggevent()
            rows = send.call_args.args[0]["triggers"]
        self.assertEqual([row["id"] for row in rows], [first["id"], second["id"]])
        self.assertNotIn("display_text", rows[0])
        self.assertEqual(rows[1]["display_text"], "Watch {target}")
        self.assertEqual(rows[0]["steps"], first["steps"])
        self.assertEqual(self.window._trigger_display_edits["triggevent:" + first["id"]], invalid)


class DisplayMoveRecoveryTests(unittest.TestCase):
    setUpClass = classmethod(moves.TriggerEngineMoveTests.setUpClass.__func__)
    setUp = moves.TriggerEngineMoveTests.setUp
    native_required = moves.TriggerEngineMoveTests.native_required
    move = moves.TriggerEngineMoveTests.move

    def test_display_and_speech_tokens_survive_moves_between_all_engines_and_reload(self):
        self.native_required()
        key = self.trigger.id
        visual = "Look away {target}, cast by {source}"
        self.window._set_display_text(key, visual)
        for target in ("triggevent", "local", "triggernometry", "triggevent", "triggernometry", "local"):
            with self.subTest(target=target):
                key = self.move(key, target)
                expected = visual.replace("{target}", "${target}").replace("{source}", "${source}") if target == "triggernometry" else visual
                self.assertEqual(self.window._trigger_display_edits, {key: expected})
                self.assertEqual(json.loads(ac._SETTINGS_FILE.read_text())["trigger_display_edits"], {key: expected})
        with patch("nyaatriggers.main_window.speak") as speak, \
                patch.object(self.window._plugin_link, "send_alert") as display:
            self.window._fire(self.window._triggers[0], {"source": "Distinct Boss", "target": "Distinct Player"})
        self.assertEqual(speak.call_args.args[0], "Boss Distinct Boss")
        display.assert_called_once_with("Look away Distinct Player, cast by Distinct Boss", "info")
        SettingsTabMixin._load_settings(self.window)
        self.window._init_engines()
        self.assertEqual(self.window._trigger_display_edits, {key: visual})

    def test_unconvertible_overlay_expression_preserves_source_and_settings(self):
        self.native_required()
        key = self.move(self.trigger.id, "triggernometry")
        self.window._set_display_text(key, "Watch ${var:unportable}")
        paths = [ac._SETTINGS_FILE, ac.TRIGGERS_LOCAL_FILE, custom.CUSTOM_FILE, *self.packs.glob("*.xml")]
        before = {path: path.read_bytes() for path in paths}
        self.assertFalse(self.window._move_trigger_to_engine(key, "local"))
        self.assertIn("variables and expressions", self.warning.call_args.args[2])
        self.assertEqual({path: path.read_bytes() for path in paths}, before)
        self.assertEqual(self.window._trigger_display_edits, {key: "Watch ${var:unportable}"})
        self.assertEqual(self.window._triggers, [])

    def test_overlay_conversion_keeps_an_already_approved_cooldown_adjustment(self):
        self.native_required()
        self.trigger.cooldown_s = 5
        self.assertTrue(self.window._save_triggers())
        self.window._set_display_text(self.trigger.id, "Watch {target}")
        key = self.move(self.trigger.id, "triggernometry")
        self.question.assert_called_once()
        self.assertIn("cooldown", self.question.call_args.args[2])
        self.assertEqual(self.window._trigger_display_edits, {key: "Watch ${target}"})

    def test_failed_move_keeps_the_overlay_with_its_original_owner(self):
        self.window._set_display_text(self.trigger.id, "Watch {target}")
        paths = [ac._SETTINGS_FILE, ac.TRIGGERS_LOCAL_FILE, custom.CUSTOM_FILE]
        before = {path: path.read_bytes() for path in paths}
        write = ac._atomic_write_bytes

        def fail_settings(path, payload):
            if path == ac._SETTINGS_FILE:
                raise OSError("Simulated settings failure")
            return write(path, payload)

        with patch.object(ac, "_atomic_write_bytes", side_effect=fail_settings):
            self.assertFalse(self.window._move_trigger_to_engine(self.trigger.id, "triggevent"))
        self.assertIn("Simulated settings failure", self.warning.call_args.args[2])
        self.assertEqual({path: path.read_bytes() for path in paths}, before)
        self.assertEqual(self.window._trigger_display_edits, {self.trigger.id: "Watch {target}"})
        self.assertIs(self.window._triggers[0], self.trigger)

    def test_changed_native_action_positions_clear_unsafe_display_edits_only(self):
        self.native_required()
        document = pack_example(self.packs / "display.xml")
        first = document.root.find(".//Trigger")
        ET.SubElement(first.find("Actions"), "Action", OrderNumber="5", ActionType="UseTTS",
                      UseTTSTextExpression="Third ${target}")
        document.save()
        self.window._in_game_combat = True
        with patch.object(self.window, "_set_triggernometry_enabled") as enabled:
            self.window._save_triggernometry_document(document)
            rows = speech_rows(document.root)
            key = "triggernometry:" + rows[0]["id"]
            unrelated = "triggernometry:" + rows[-1]["id"]
            self.window._set_display_text(key, "First visual ${target}")
            self.window._set_display_text(unrelated, "Other trigger visual")
            self.window._save_triggernometry_document(document)
            self.assertEqual(self.window._trigger_display_edits[key], "First visual ${target}")
            speech = first.find("Actions/Action[@ActionType='UseTTS']")
            speech.set("OrderNumber", "6")
            actions = first.find("Actions")
            actions.remove(speech)
            actions.append(speech)
            self.window._save_triggernometry_document(document)
            enabled.assert_not_called()
        self.assertNotIn(key, self.window._trigger_display_edits)
        self.assertEqual(self.window._trigger_display_edits, {unrelated: "Other trigger visual"})
        self.assertEqual(json.loads(ac._SETTINGS_FILE.read_text())["trigger_display_edits"],
                         {unrelated: "Other trigger visual"})


class DisplayBridgeTests(unittest.TestCase):
    def test_explicit_empty_display_preserves_speech_and_missing_display_uses_legacy_fallback(self):
        bridge = TriggernometryBridge()
        bridge._active = True
        self.addCleanup(setattr, bridge, "_active", False)
        spoken, displayed = [], []
        bridge.tts.connect(lambda text, generation: spoken.append(text))
        bridge.callout.connect(lambda text, severity, generation: displayed.append(text))
        generation = bridge.generation()
        bridge._dispatch({"t": "callout", "tts": "Speech without popup", "text": ""}, generation)
        self.assertEqual(spoken, ["Speech without popup"])
        self.assertEqual(displayed, [])
        bridge._dispatch({"t": "callout", "tts": "Older host fallback"}, generation)
        self.assertEqual(spoken, ["Speech without popup", "Older host fallback"])
        self.assertEqual(displayed, ["Older host fallback"])
        bridge._dispatch({"t": "callout", "tts": "", "text": "Popup without speech"}, generation)
        self.assertEqual(spoken, ["Speech without popup", "Older host fallback"])
        self.assertEqual(displayed, ["Older host fallback", "Popup without speech"])


if __name__ == "__main__":
    unittest.main()
