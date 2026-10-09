from contextlib import ExitStack
import json
from pathlib import Path
import queue
import tempfile
from types import SimpleNamespace
import unittest
import urllib.error
from unittest.mock import patch
from PyQt6 import sip
from PyQt6.QtCore import QCoreApplication, QEvent
from PyQt6.QtWidgets import QLabel, QPushButton

from nyaatriggers import app_common as ac
from nyaatriggers.telesto_client import MARKER_TOKENS, TelestoClient
from nyaatriggers.triggevent_bridge import TriggeventBridge
from nyaatriggers.ui.automarkers_tab import AutomarkersTabMixin
from nyaatriggers.ui.settings_tab import SettingsTabMixin
from tests import test_session_ui as fixture


DEFAULTS = {
    "umad_chain_marker_dps": "attack1",
    "umad_chain_marker_support": "attack2",
    "umad_accretion_marker_first": "ignore1",
    "umad_accretion_marker_second": "ignore2",
    "umad_gaze_marker_away1": "ignore1",
    "umad_gaze_marker_away2": "ignore2",
    "umad_gaze_marker_look1": "bind1",
    "umad_gaze_marker_look2": "bind2",
}


class AutomarkSettingsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture.SessionUiTests.setUpClass()

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(TelestoClient, "start"))
        self.stack.enter_context(patch.object(TriggeventBridge, "is_available", return_value=False))

    def dispose_window(self, window):
        window.close()
        window._prog_sessions.close()
        window.deleteLater()
        fixture.SessionUiTests.app.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.assertTrue(sip.isdeleted(window))

    def create_case(self):
        case = fixture.SessionUiTests()
        case.setUp()
        def cleanup():
            case.doCleanups()
            case.window.deleteLater()
            fixture.SessionUiTests.app.processEvents()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            self.assertTrue(sip.isdeleted(case.window))
        self.addCleanup(cleanup)
        return case

    def loaded_window(self, saved):
        ac._SETTINGS_FILE.write_text(json.dumps(saved), encoding="utf-8")
        with patch.object(fixture.mw.MainWindow, "_load_settings", SettingsTabMixin._load_settings):
            window = fixture.mw.MainWindow()
        self.addCleanup(self.dispose_window, window)
        return window

    def load_choices(self, saved):
        with tempfile.TemporaryDirectory() as directory:
            settings = Path(directory) / "settings.json"
            settings.write_text(json.dumps(saved), encoding="utf-8")
            window = SimpleNamespace(_settings={})
            with patch.object(ac, "_SETTINGS_FILE", settings):
                SettingsTabMixin._load_settings(window)
        choices = {}
        for prefix, method in (
            ("umad_chain_marker_", AutomarkersTabMixin._umad_chain_markers_from_settings),
            ("umad_accretion_marker_", AutomarkersTabMixin._umad_accretion_markers_from_settings),
            ("umad_gaze_marker_", AutomarkersTabMixin._umad_gaze_markers_from_settings),
        ):
            choices.update({prefix + key: value for key, value in method(window).items()})
        self.assertEqual(window._settings["unrelated"], "keep")
        return choices

    def test_invalid_saved_choice_keeps_defaults_and_other_assignments(self):
        self.assertEqual(self.load_choices({"unrelated": "keep"}), DEFAULTS)
        for key, default in DEFAULTS.items():
            for invalid in (None, [], {}, ["attack1"], {"marker": "attack1"}, True, 1, "", "unknown"):
                with self.subTest(key=key, value=invalid):
                    saved = {**dict.fromkeys(DEFAULTS, "square"), key: invalid, "unrelated": "keep"}
                    expected = {**dict.fromkeys(DEFAULTS, "square"), key: default}
                    self.assertEqual(self.load_choices(saved), expected)

    def test_every_supported_marker_survives_settings_reload(self):
        for marker in sorted(MARKER_TOKENS):
            with self.subTest(marker=marker):
                saved = {**dict.fromkeys(DEFAULTS, marker), "unrelated": "keep"}
                self.assertEqual(self.load_choices(saved), dict.fromkeys(DEFAULTS, marker))

    def test_invalid_marker_arrays_do_not_prevent_window_construction(self):
        case = self.create_case()
        saved = {**case.window._settings, **dict.fromkeys(DEFAULTS, [])}
        window = self.loaded_window(saved)
        choices = {"umad_chain_marker_" + key: combo.currentData()
                   for key, combo in window._umad_chain_combos.items()}
        choices.update({"umad_accretion_marker_" + key: combo.currentData()
                        for key, combo in window._umad_accretion_combos.items()})
        choices.update({"umad_gaze_marker_" + key: combo.currentData()
                        for key, combo in window._umad_gaze_combos.items()})
        self.assertEqual(choices, DEFAULTS)

    def test_generic_debuff_section_and_preset_are_removed_from_the_five_umad_tabs(self):
        case = self.create_case()
        window = case.window
        tabs = window._umad_automarker_tabs
        self.assertEqual([tabs.tabText(index) for index in range(tabs.count())],
                         ["Black-hole chains", "Accretion", "Cursed Shriek",
                          "Acceleration Bomb", "Forked Lightning"])
        self.assertEqual(set(window._umad_chain_combos), {"dps", "support"})
        self.assertEqual({key: combo.currentData() for key, combo
                          in window._umad_accretion_combos.items()},
                         {"first": "ignore1", "second": "ignore2"})
        self.assertFalse(hasattr(window, "_automark_rules_list"))
        self.assertFalse(hasattr(window, "_automark_assign_combo"))
        self.assertNotIn("Debuff assignments", [label.text() for label in window.findChildren(QLabel)])
        self.assertNotIn("Load UMAD preset", [button.text() for button in window.findChildren(QPushButton)])

    def test_saved_generic_rules_stay_dormant_and_do_not_block_native_p4_markers(self):
        case = self.create_case()
        rules = [{"fight": "UMAD", "status": "15AA", "marker": "circle", "scope": "party"},
                 {"status": "Forked Lightning", "marker": "square", "scope": "party"},
                 {"fight": "UMAD", "status": "644+BBC", "marker": "attack3", "scope": "party"}]
        window = self.loaded_window({**case.window._settings, "automark_rules": rules,
                                     "umad_accretion_enabled": True, "native_umad_enabled": True})
        self.assertEqual(window._settings["automark_rules"], rules)
        self.assertEqual(window._automark_rules, [])
        self.assertFalse(window._native_umad_owned_locally())
        self.assertTrue(window._native_umad_preference())
        window._current_fight_tag = "UMAD"
        window._automark_cb.setChecked(True)
        with patch.object(window, "_mark_player") as mark:
            for status in ("15AA", "15A8", "644", "BBC"):
                window._on_log_line("|".join(["26", "ts", status, "Status", "30",
                                             "40000001", "Boss", "10000001", "Player"]))
        mark.assert_not_called()
        self.assertEqual(window._automark_pending, [])
        self.assertEqual(json.loads(ac._SETTINGS_FILE.read_text())["automark_rules"], rules)

    def test_accretion_enable_migration_inherits_chains_only_when_own_preference_is_missing(self):
        case = self.create_case()
        for chains, own, expected in ((False, None, False), (True, None, True),
                                     (True, False, False), (False, True, True)):
            with self.subTest(chains=chains, own=own):
                saved = {**case.window._settings, "umad_chain_enabled": chains,
                         "umad_chain_marker_accretion": "attack8"}
                saved.pop("umad_accretion_enabled", None)
                if own is not None:
                    saved["umad_accretion_enabled"] = own
                window = self.loaded_window(saved)
                self.assertEqual(window._umad_accretion_enabled, expected)
                self.assertEqual(window._settings["umad_accretion_enabled"], expected)
                self.assertEqual(window._umad_accretion_cb.isChecked(), expected)
                self.assertEqual(window._umad_chain_cb.isChecked(), chains)
                self.assertEqual({key: combo.currentData() for key, combo
                                 in window._umad_accretion_combos.items()},
                                 {"first": "ignore1", "second": "ignore2"})
                if own is None:
                    window._umad_chain_cb.setChecked(not chains)
                    migrated = json.loads(ac._SETTINGS_FILE.read_text())
                    self.assertEqual(migrated["umad_accretion_enabled"], expected)
                    restored = self.loaded_window(migrated)
                    self.assertEqual(restored._umad_chain_cb.isChecked(), not chains)
                    self.assertEqual(restored._umad_accretion_enabled, expected)
                    self.assertEqual(restored._umad_accretion_cb.isChecked(), expected)

    def test_accretion_markers_persist_and_its_toggle_is_independent_of_chains(self):
        case = self.create_case()
        window = case.window
        window._umad_chain_cb.setChecked(True)
        self.assertFalse(window._umad_accretion_enabled)
        window._umad_accretion_cb.setChecked(True)
        window._umad_chain_cb.setChecked(False)
        self.assertTrue(window._umad_accretion_enabled)
        self.assertFalse(window._umad_chain_enabled)
        for key, marker in (("first", "bind1"), ("second", "triangle")):
            combo = window._umad_accretion_combos[key]
            combo.setCurrentIndex(combo.findData(marker))
        saved = json.loads(ac._SETTINGS_FILE.read_text())
        self.assertEqual({key: saved[key] for key in ("umad_accretion_enabled", "umad_chain_enabled",
                                                    "umad_accretion_marker_first", "umad_accretion_marker_second")},
                         {"umad_accretion_enabled": True, "umad_chain_enabled": False,
                          "umad_accretion_marker_first": "bind1", "umad_accretion_marker_second": "triangle"})
        restored = self.loaded_window(saved)
        self.assertTrue(restored._umad_accretion_cb.isChecked())
        self.assertFalse(restored._umad_chain_cb.isChecked())
        self.assertEqual({key: combo.currentData() for key, combo
                          in restored._umad_accretion_combos.items()},
                         {"first": "bind1", "second": "triangle"})
        restored._umad_accretion_cb.setChecked(False)
        self.assertFalse(restored._umad_accretion_enabled)
        self.assertFalse(restored._umad_chain_enabled)
        self.assertFalse(json.loads(ac._SETTINGS_FILE.read_text())["umad_accretion_enabled"])

    def test_bomb_and_lightning_assignments_allow_unassigned_and_reject_invalid_values(self):
        for name, count in (("bomb", 4), ("lightning", 2)):
            expected = {f"{kind}{number}": "" for kind in ("real", "fake")
                        for number in range(1, count + 1)}
            window = SimpleNamespace(_settings={})
            self.assertEqual(AutomarkersTabMixin._umad_pair_markers_from_settings(window, name, count), expected)
            for invalid in (None, [], {}, True, "unknown"):
                window._settings[f"umad_{name}_marker_real1"] = invalid
                self.assertEqual(AutomarkersTabMixin._umad_pair_markers_from_settings(window, name, count), expected)
            window._settings[f"umad_{name}_marker_real1"] = "attack1"
            self.assertEqual(AutomarkersTabMixin._umad_pair_markers_from_settings(window, name, count),
                             {**expected, "real1": "attack1"})

    def test_enabling_local_controllers_cancels_overlapping_queued_rules(self):
        case = self.create_case()
        window = case.window
        client = window._telesto_client
        window._settings["telesto_enabled"] = True
        window._current_fight_tag = "UMAD"
        client.configure(enabled=True, delay_base_ms=0, delay_plus_ms=0)
        actors = ("10000001", "10000002", "10000003")
        commands = []

        def response(request, _timeout):
            message = json.loads(request.data)
            if message["type"] == "ExecuteCommand":
                commands.append(message["payload"]["command"])
            return 200, b"null"

        def drain():
            with patch.object(client, "_read_response", side_effect=response):
                while True:
                    try:
                        item = client._queue.get_nowait()
                    except queue.Empty:
                        break
                    try:
                        client._send_queued(item, client._stopping)
                    finally:
                        client._finish_delivery(item)
                        client._queue.task_done()

        def gain(actor, status):
            window._match_automark_rules(["26", "ts", status, "Status", "30",
                                         "40000001", "Boss", actor, "Player"])

        for status, checkbox in (
                ("15AA", window._umad_pair_checkboxes["bomb"]),
                ("15A8", window._umad_pair_checkboxes["lightning"]),
                ("15A7", window._umad_gaze_cb),
                ("644", window._umad_chain_cb),
                ("644", window._umad_accretion_cb)):
            for fight in ("UMAD", "Other duty"):
                with self.subTest(status=status, fight=fight):
                    checkbox.setChecked(False)
                    window._clear_actor_state()
                    window._current_fight_tag = fight
                    client._update_party_slots(json.dumps([
                        {"actor": actor, "order": index}
                        for index, actor in enumerate(actors, 1)]).encode())
                    window._automark_rules = [
                        {"status": status, "marker": "circle", "scope": "party"},
                        {"status": "ABC", "marker": "square", "scope": "party"},
                    ]
                    gain(actors[0], status)
                    drain()
                    delivered = window._automark_deliveries[actors[0]][0].delivery
                    gain(actors[1], status)
                    queued = window._automark_deliveries[actors[1]][0].delivery
                    gain(actors[2], "ABC")
                    commands.clear()
                    checkbox.setChecked(True)
                    self.assertTrue(delivered.current)
                    self.assertFalse(delivered.cancelled)
                    self.assertEqual(queued.cancelled, fight == "UMAD")
                    if fight == "UMAD":
                        self.assertNotIn(actors[1], window._automark_active)
                        self.assertNotIn(actors[1], window._automark_owners)
                    drain()
                    expected = [] if fight == "UMAD" else ["/mk circle <2>"]
                    self.assertEqual(commands, [*expected, "/mk square <3>"])
                    checkbox.setChecked(False)
                    drain()

    def test_assignment_edits_cancel_queued_marks_and_keep_other_rules(self):
        case = self.create_case()
        window = case.window
        client = window._telesto_client
        window._settings["telesto_enabled"] = True
        client.set_enabled(True)
        actors = ("10000001", "10000002")
        commands = []

        def response(request, _timeout):
            message = json.loads(request.data)
            if message["type"] == "ExecuteCommand":
                commands.append(message["payload"]["command"])
            return 200, b"null"

        def drain():
            with patch.object(client, "_read_response", side_effect=response), \
                    patch.object(client, "_sleep_command_delay"):
                while True:
                    try:
                        item = client._queue.get_nowait()
                    except queue.Empty:
                        break
                    client._send_queued(item, client._stopping)
                    client._finish_delivery(item)
                    client._queue.task_done()

        def gain(actor, status):
            window._match_automark_rules(["26", "ts", status, "Status", "30",
                                         "40000001", "Boss", actor, "Player"])

        for action in ("clear", "reassign"):
            for restore in (False, True):
                with self.subTest(action=action, restore=restore):
                    window._clear_actor_state()
                    client._update_party_slots(b'{"response":[{"actor":"10000001","order":1},'
                                               b'{"actor":"10000002","order":2}]}')
                    window._automark_rules = [
                        {"status": "ABC", "marker": "attack1", "scope": "party"},
                        {"status": "DEF", "marker": "attack2", "scope": "party"},
                    ]
                    gain(actors[0], "ABC")
                    gain(actors[1], "DEF")
                    old = window._automark_deliveries[actors[0]][0].delivery
                    window._automark_rules[0]["marker"] = "" if action == "clear" else "attack3"
                    window._cancel_changed_rule_marks()
                    self.assertTrue(old.cancelled)
                    self.assertFalse(old.pending or old.current)
                    self.assertNotIn(actors[0], window._automark_active)
                    self.assertNotIn(actors[0], window._automark_owners)
                    if restore:
                        window._automark_rules[0]["marker"] = "attack1"
                        window._cancel_changed_rule_marks()
                    commands.clear()
                    drain()
                    self.assertEqual(commands, ["/mk attack2 <2>"])
                    marker = window._automark_rules[0]["marker"]
                    if marker:
                        gain(actors[0], "ABC")
                        drain()
                        self.assertEqual(commands, ["/mk attack2 <2>", f"/mk {marker} <1>"])
                    window._automark_rules[1]["marker"] = ""
                    window._cancel_changed_rule_marks()
                    self.assertTrue(window._automark_deliveries[actors[1]][0].delivery.current)
                    window._match_automark_unmark(["30", "ts", "DEF", "Status", "0",
                                                  "40000001", "Boss", actors[1], "Player"])
                    drain()
                    self.assertEqual(commands[-1], "/mk clear <2>")

    def test_canceled_replacement_preserves_old_cleanup_and_future_rule_retry(self):
        case = self.create_case()
        window = case.window
        client = window._telesto_client
        window._settings["telesto_enabled"] = True
        client.configure(enabled=True, delay_base_ms=0, delay_plus_ms=0)
        actor = "10000001"
        commands = []

        def response(request, _timeout):
            message = json.loads(request.data)
            if message["type"] == "ExecuteCommand":
                commands.append(message["payload"]["command"])
            return 200, b"null"

        def drain():
            with patch.object(client, "_read_response", side_effect=response):
                while True:
                    try:
                        item = client._queue.get_nowait()
                    except queue.Empty:
                        break
                    try:
                        client._send_queued(item, client._stopping)
                    finally:
                        client._finish_delivery(item)
                        client._queue.task_done()

        def status(effect, lost=False):
            case.line(["30" if lost else "26", "ts", effect, "Status", "30",
                       "40000001", "Boss", actor, "Player"])

        client._update_party_slots(b'{"response":[{"actor":"10000001","order":1}]}')
        window._automark_rules = [
            {"status": status_id, "marker": marker, "scope": "party"}
            for status_id, marker in (("ABC", "triangle"), ("DEF", "circle"), ("123", "square"))]
        status("ABC")
        drain()
        original = window._automark_deliveries[actor][0].delivery
        status("DEF")
        replacement = window._automark_deliveries[actor][-1].delivery
        window._automark_rules[1]["marker"] = ""
        window._cancel_changed_rule_marks()
        self.assertTrue(replacement.cancelled)
        self.assertTrue(original.current)
        self.assertEqual(window._automark_owners[actor], ("rule", "triangle"))
        self.assertEqual(window._automark_active, {actor: "ABC"})
        status("ABC", lost=True)
        drain()
        self.assertEqual(commands, ["/mk triangle <1>", "/mk clear <1>"])
        self.assertEqual(window._automark_active, {})
        client._update_party_slots(b'{"response":[]}')
        status("123")
        self.assertEqual(len(window._automark_pending), 1)
        client._update_party_slots(b'{"response":[{"actor":"10000001","order":1}]}')
        window._retry_automark_pending()
        drain()
        self.assertEqual(commands, ["/mk triangle <1>", "/mk clear <1>", "/mk square <1>"])
        self.assertEqual(window._automark_pending, [])

    def test_editing_a_rejected_assignment_releases_future_rule_retry(self):
        case = self.create_case()
        window = case.window
        client = window._telesto_client
        window._settings["telesto_enabled"] = True
        client.configure(enabled=True, delay_base_ms=0, delay_plus_ms=0)
        actor = "10000001"
        commands = []

        def response(request, _timeout):
            message = json.loads(request.data)
            if message["type"] == "ExecuteCommand":
                command = message["payload"]["command"]
                commands.append(command)
                if command == "/mk triangle <1>":
                    raise urllib.error.HTTPError(request.full_url, 503, "Rejected", {}, None)
            return 200, b"null"

        def drain():
            with patch.object(client, "_read_response", side_effect=response):
                while True:
                    try:
                        item = client._queue.get_nowait()
                    except queue.Empty:
                        break
                    try:
                        client._send_queued(item, client._stopping)
                    finally:
                        client._finish_delivery(item)
                        client._queue.task_done()

        def gain(effect):
            case.line(["26", "ts", effect, "Status", "30", "40000001", "Boss", actor, "Player"])

        client._update_party_slots(b'{"response":[{"actor":"10000001","order":1}]}')
        window._automark_rules = [
            {"status": "ABC", "marker": "triangle", "scope": "party"},
            {"status": "123", "marker": "square", "scope": "party"},
        ]
        gain("ABC")
        drain()
        rejected = window._automark_deliveries[actor][0].delivery
        self.assertFalse(rejected.pending or rejected.current)
        window._automark_rules[0]["marker"] = ""
        window._cancel_changed_rule_marks()
        self.assertFalse(rejected.cancelled)
        self.assertEqual(window._automark_active, {})
        self.assertEqual(window._automark_owners, {})
        client._update_party_slots(b'{"response":[]}')
        gain("123")
        self.assertEqual(len(window._automark_pending), 1)
        client._update_party_slots(b'{"response":[{"actor":"10000001","order":1}]}')
        window._retry_automark_pending()
        drain()
        self.assertEqual(commands, ["/mk triangle <1>", "/mk square <1>"])
        self.assertEqual(window._automark_pending, [])


if __name__ == "__main__":
    unittest.main()
