from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication, QDialog, QInputDialog, QPlainTextEdit, QWidget

from nyaatriggers import app_common as ac, triggevent_custom
from nyaatriggers.sequential import SequentialRunner
from nyaatriggers.trigger_dialog import TriggerDialog
from nyaatriggers.trigger_engine import Trigger
from nyaatriggers.triggevent_bridge import TriggeventBridge
from nyaatriggers.triggernometry_bridge import TriggernometryBridge
from nyaatriggers.ui.engines import EnginesMixin
from nyaatriggers.ui.triggers_tab import TriggersTabMixin


APP = QApplication.instance() or QApplication([])
IDENT = "00000000-0000-4000-8000-000000000001#0"


class EditorHost(QWidget, EnginesMixin, TriggersTabMixin):
    def __init__(self, source, settings_path):
        super().__init__()
        self.source = source
        self.settings_path = settings_path
        self._settings = {}
        self._custom_triggevent = []
        self._triggers = []
        self._trigger_display_edits = {}
        self._engine_text_overrides = {}
        self._shipped_callout_defaults = {}
        self._triggevent_callout_edits = {}
        self._triggernometry_callout_edits = {}
        self._triggevent_disabled = set()
        self._triggernometry_disabled = set()
        self._engine_disabled = {"triggevent": self._triggevent_disabled,
                                 "triggernometry": self._triggernometry_disabled}
        self._engine_seen = {}
        self._engine_inventory = []
        self._triggevent = self._triggernometry = None
        # Simulate a negotiated process without consulting installed native engines.
        self.bridge = TriggeventBridge() if source == "triggevent" else TriggernometryBridge()
        self.bridge._active = True
        setattr(self, "_" + source, self.bridge)
        self.bridge.inventory.connect(getattr(self, "_on_" + source + "_inventory"))

    def receive_inventory(self, text):
        rows = [] if text is None else [
            {"id": IDENT, "name": "Council sequence", "text": text, "tts": text,
             "fight": "Arena", "group": ""}]
        self.bridge._dispatch({"t": "inventory", "triggers": rows}, gen=self.bridge.generation())
        return self.source + ":" + IDENT

    def _save_settings(self):
        ac._atomic_write_json(self.settings_path, self._settings)
        return True

    def _refresh_table(self):
        pass

    def _apply_automark_state(self):
        pass

    def _localize_text(self, text):
        return text


class FollowupEditorTests(unittest.TestCase):
    def dialog(self, kind, pattern):
        dialog = TriggerDialog(trigger=Trigger(
            name="Council sequence", ability_id="CAFE", sequence=[
                {"log_type": kind, "ability_id": "1234", "ability_regex": pattern}]))
        self.addCleanup(dialog.deleteLater)
        return dialog

    def runner(self, dialog):
        completed = []
        runner = SequentialRunner(dialog.get_trigger(), {},
                                  lambda _runner, captures: completed.append(captures),
                                  lambda _runner: None)
        self.addCleanup(runner.deleteLater)
        self.addCleanup(runner.cancel)
        return runner, completed

    def test_ignored_id_cannot_hide_an_invalid_effective_step_regex(self):
        for kind in ("00", "00|21", "21|00"):
            with self.subTest(kind=kind):
                dialog = self.dialog(kind, "[")
                with patch("nyaatriggers.trigger_dialog.QMessageBox.warning") as warning:
                    dialog.accept()
                self.assertNotEqual(dialog.result(), QDialog.DialogCode.Accepted)
                warning.assert_called_once()
                self.assertIn("regex for step 1 does not compile", warning.call_args.args[2])

    def test_indexed_steps_keep_ignoring_the_unused_regex(self):
        for kind, log_type in (("21", "21"), ("20|21", "21"), (" ", "20"),
                               ("21|", "21"), ("|21", "21"), ("", "20")):
            with self.subTest(kind=kind):
                dialog = self.dialog(kind, "[")
                with patch("nyaatriggers.trigger_dialog.QMessageBox.warning") as warning:
                    dialog.accept()
                warning.assert_not_called()
                self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)
                runner, completed = self.runner(dialog)
                self.assertFalse(runner.try_advance(
                    [log_type, "ts", "40000001", "Boss", "ABCD", "Wrong", "10000001", "Player"]))
                self.assertEqual(completed, [])
                self.assertTrue(runner.try_advance(
                    [log_type, "ts", "40000001", "Boss", "1234", "Correct", "10000001", "Player"]))
                self.assertEqual(completed, [{"source": "Boss", "target": "Player"}])

    def test_unindexed_steps_use_the_regex_even_with_a_saved_id(self):
        dialog = self.dialog("00|21", "Council ready")
        with patch("nyaatriggers.trigger_dialog.QMessageBox.warning") as warning:
            dialog.accept()
        warning.assert_not_called()
        self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)
        runner, completed = self.runner(dialog)
        self.assertFalse(runner.try_advance(["00", "ts", "0038", "Guide", "Other message"]))
        self.assertEqual(completed, [])
        self.assertTrue(runner.try_advance(["00", "ts", "0038", "Guide", "Council ready"]))
        self.assertEqual(completed, [{"source": "Guide"}])


class ModalCalloutEditorTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.hosts = []
        self.enterContext(patch.object(ac, "_TRIGGEVENT_INVENTORY_CACHE", self.root / "tv.json"))
        self.enterContext(patch.object(ac, "_TRIGGERNOMETRY_INVENTORY_CACHE", self.root / "tn.json"))
        self.addCleanup(APP.processEvents)

    def host(self, source):
        host = EditorHost(source, self.root / (str(len(self.hosts)) + ".json"))
        self.hosts.append(host)
        self.addCleanup(host.deleteLater)
        self.addCleanup(host.close)
        self.addCleanup(setattr, host.bridge, "_active", False)
        host.receive_inventory("Original mechanic")
        self.commands(host)
        return host

    def commands(self, host):
        commands = []
        while not host.bridge._wq.empty():
            for line in host.bridge._wq.get_nowait().splitlines():
                command = json.loads(line)
                if command.get("nyaa_cmd", command.get("t")) in (
                        "set_callout", "set_display_text", "reset_callout"):
                    commands.append(command)
        return commands

    def edit(self, host, key, display, update, wording="Stale editor wording"):
        errors, dialogs = [], []
        watchdog = QTimer()
        watchdog.setSingleShot(True)

        def expired():
            errors.append("The modal editor exceeded its deadline")
            dialog = APP.activeModalWidget()
            if dialog is not None:
                dialog.reject()

        watchdog.timeout.connect(expired)

        def finish():
            dialog = APP.activeModalWidget()
            try:
                self.assertIsInstance(dialog, QDialog)
                dialogs.append(dialog)
                update()
                if display:
                    self.assertIsInstance(dialog, QInputDialog)
                    dialog.setTextValue(wording)
                else:
                    editor = dialog.findChild(QPlainTextEdit)
                    self.assertIsNotNone(editor)
                    editor.setPlainText(wording)
                dialog.accept()
            except BaseException as exc:
                errors.append(repr(exc))
                if dialog is not None:
                    dialog.reject()

        QTimer.singleShot(0, finish)
        watchdog.start(2000)
        try:
            with patch.object(ac.QMessageBox, "warning") as warning:
                if display:
                    host._edit_display_text(key)
                else:
                    host._edit_engine_row(key)
        finally:
            watchdog.stop()
        self.assertEqual(errors, [])
        self.assertEqual(len(dialogs), 1)
        return warning

    def test_changed_and_removed_inventory_cannot_receive_a_stale_editor_override(self):
        for source in ("triggevent", "triggernometry"):
            for display in (False, True):
                for replacement in ("New mechanic", None):
                    with self.subTest(source=source, display=display, replacement=replacement):
                        host = self.host(source)
                        key = source + ":" + IDENT
                        before = host.settings_path.read_bytes()
                        warning = self.edit(host, key, display,
                                            lambda: host.receive_inventory(replacement))
                        self.assertEqual(host._callout_edits_for(source), {})
                        self.assertEqual(host._trigger_display_edits, {})
                        self.assertEqual(host.settings_path.read_bytes(), before)
                        self.assertEqual(self.commands(host), [])
                        warning.assert_called_once()
                        self.assertIn("changed outside the editor", warning.call_args.args[2])

    def test_equal_inventory_refresh_allows_the_edit_and_sends_it_to_the_engine(self):
        for source in ("triggevent", "triggernometry"):
            for display in (False, True):
                with self.subTest(source=source, display=display):
                    host = self.host(source)
                    key = source + ":" + IDENT
                    warning = self.edit(host, key, display,
                                        lambda: host.receive_inventory("Original mechanic"),
                                        wording="Accepted wording")
                    warning.assert_not_called()
                    commands = self.commands(host)
                    self.assertEqual(len(commands), 1)
                    self.assertEqual(commands[0]["id"], IDENT)
                    saved = json.loads(host.settings_path.read_text())
                    if display:
                        self.assertEqual(saved["trigger_display_edits"], {key: "Accepted wording"})
                        self.assertEqual(commands[0]["text"], "Accepted wording")
                    else:
                        self.assertEqual(saved[source + "_callout_edits"], {IDENT: "Accepted wording"})
                        self.assertEqual(commands[0].get("tts", commands[0].get("text")), "Accepted wording")

    def test_wording_changed_during_the_dialog_keeps_the_newer_choice(self):
        for source in ("triggevent", "triggernometry"):
            for display in (False, True):
                with self.subTest(source=source, display=display):
                    host = self.host(source)
                    key = source + ":" + IDENT

                    def update():
                        if display:
                            host._set_display_text(key, "Newer wording")
                        else:
                            host._apply_callout_edit(source, IDENT, "Newer wording")

                    warning = self.edit(host, key, display, update)
                    saved = json.loads(host.settings_path.read_text())
                    if display:
                        self.assertEqual(saved["trigger_display_edits"], {key: "Newer wording"})
                    else:
                        self.assertEqual(saved[source + "_callout_edits"], {IDENT: "Newer wording"})
                    commands = self.commands(host)
                    self.assertEqual(len(commands), 1)
                    self.assertEqual(commands[0].get("tts", commands[0].get("text")), "Newer wording")
                    warning.assert_called_once()

    def test_in_place_definition_changes_keep_existing_wording(self):
        for source in ("triggevent", "triggernometry"):
            for display in (False, True):
                with self.subTest(source=source, display=display):
                    host = self.host(source)
                    key = source + ":" + IDENT
                    host._apply_callout_edit(source, IDENT, "Pinned speech")
                    host._set_display_text(key, "Pinned overlay")
                    self.commands(host)
                    before = host.settings_path.read_bytes()
                    field = "tts" if source == "triggevent" else "text"

                    def update():
                        host._engine_entry_for_key(key)[field] = "Changed default speech"

                    warning = self.edit(host, key, display, update)
                    self.assertEqual(host._engine_text_for_key(key), "Pinned speech")
                    self.assertEqual(host._trigger_display_text(key), "Pinned overlay")
                    self.assertEqual(host._engine_entry_for_key(key)[field], "Changed default speech")
                    self.assertEqual(host.settings_path.read_bytes(), before)
                    self.assertEqual(self.commands(host), [])
                    warning.assert_called_once()

    def test_replaced_local_definition_cannot_receive_a_stale_overlay_edit(self):
        host = self.host("triggernometry")
        trigger = Trigger(id="council-local", ability_id="CAFE", tts_text="Original mechanic")
        host._triggers = [trigger]
        before = host.settings_path.read_bytes()

        def update():
            row = deepcopy(trigger.to_dict())
            row["ability_id"] = "ABCD"
            host._triggers = [Trigger.from_dict(row)]

        warning = self.edit(host, trigger.id, True, update)
        self.assertEqual(host._trigger_display_edits, {})
        self.assertEqual(host._triggers[0].ability_id, "ABCD")
        self.assertEqual(host.settings_path.read_bytes(), before)
        warning.assert_called_once()

    def test_nested_custom_definition_change_keeps_existing_overlay(self):
        host = self.host("triggevent")
        host._custom_triggevent_error = ""
        host._custom_triggevent_dialog = None
        definition = triggevent_custom.new_trigger("Fixture", 123)
        definition["name"] = "Fixture sequence"
        definition["start"]["ids"] = "CAFE"
        definition["steps"][0]["text"] = "Original speech"
        triggevent_custom.validate_trigger(definition)
        host._custom_triggevent = [definition]
        host._merge_custom_triggevent_inventory()
        key = "triggevent:" + definition["id"]
        host._set_display_text(key, "Pinned overlay")
        self.commands(host)
        before = host.settings_path.read_bytes()

        def update():
            definition["start"]["ids"] = "ABCD"

        warning = self.edit(host, key, True, update)
        self.assertEqual(definition["start"]["ids"], "ABCD")
        self.assertEqual(host._trigger_display_edits[key], "Pinned overlay")
        self.assertEqual(host.settings_path.read_bytes(), before)
        self.assertEqual(self.commands(host), [])
        warning.assert_called_once()


if __name__ == "__main__":
    unittest.main()
