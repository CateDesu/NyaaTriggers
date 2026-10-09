from contextlib import ExitStack
from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6 import sip
from PyQt6.QtCore import QCoreApplication, QEvent
from PyQt6.QtWidgets import QApplication, QCheckBox, QComboBox, QListWidget, QPushButton, QSpinBox, QWidget

from nyaatriggers import app_common as ac
from nyaatriggers import triggevent_bridge as tb
from nyaatriggers.ui.automarkers_tab import AutomarkersTabMixin
from nyaatriggers.ui.engines import EnginesMixin
from nyaatriggers.ui.native_automarkers import NativeAutomarkersPanel
from nyaatriggers.ui.settings_tab import SettingsTabMixin
from tests.test_native_automarkers_ui import native_inventory


_QT_APP = None
TEST_URI = "http://127.0.0.1:1/"


class SettingsHost(AutomarkersTabMixin, EnginesMixin, SettingsTabMixin, QWidget):
    def __init__(self):
        super().__init__()
        self._settings = {"telesto_enabled": True, "telesto_uri": TEST_URI,
                          "unrelated": "preserve"}
        self._settings_backup_pending = False
        self._save_warned = False
        self._automark_rules = []
        self._automark_active = {}
        self._automark_pending = []
        self._automark_owners = {}
        self._native_automark_inventory = None
        self._engine_inventory = []
        self._triggevent_mode = False
        self._triggevent = tb.TriggeventBridge(self)
        self._triggevent._active = True
        self._triggevent._gen = 4
        self._triggevent.inventory.connect(self._on_triggevent_inventory)
        self._triggevent.automark_inventory.connect(self._on_native_automark_inventory)
        self._native_automarkers_panel = NativeAutomarkersPanel(self)
        self._native_automarkers_panel.changed.connect(self._on_native_automark_setting_changed)
        self._telesto_client = Mock()
        self._telesto_client.last_status.return_value = None
        self._umad_chain_reset = Mock()
        self._umad_gaze_reset = Mock()
        self._update_automark_status_label = Mock()
        self._save_triggevent_inventory_cache = Mock()
        self._record_engine_seen = Mock()
        self._refresh_table = Mock()
        self._replay_triggevent_callout_edits = Mock()


class NativeAutomarkerSettingsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        global _QT_APP
        _QT_APP = QApplication.instance() or QApplication([])

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.directory = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.settings_path = self.directory / "settings.json"
        self.stack.enter_context(patch.object(ac, "_SETTINGS_FILE", self.settings_path))
        self.stack.enter_context(patch.object(tb, "record"))
        self.stack.enter_context(patch.object(tb, "is_available", return_value=False))
        self.host = SettingsHost()
        self.bridge = self.host._triggevent
        self.saved = self.stack.enter_context(patch.object(self.host, "_save_settings", wraps=self.host._save_settings))
        self.inventory = native_inventory()
        self.inventory["version"] = 1

    def tearDown(self):
        self.bridge._active = False
        self.host.close()
        self.host.deleteLater()
        _QT_APP.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.assertTrue(sip.isdeleted(self.host))

    def commands(self):
        result = []
        while not self.bridge._wq.empty():
            result.append(json.loads(self.bridge._wq.get_nowait()))
        return result

    def full_command(self, settings, enabled=True, native_umad=True):
        return {"nyaa_cmd": "set_automark", "enable": enabled, "uri": TEST_URI,
                "native_umad": native_umad, "settings": settings}

    def render(self):
        self.bridge._dispatch(deepcopy(self.inventory), gen=self.bridge.generation())

    def control(self, cls, name):
        widget = self.host._native_automarkers_panel.findChild(cls, name)
        self.assertIsNotNone(widget, name)
        return widget

    def load_offline_controls(self):
        seed = self.directory / "automarkers.seed.json"
        seed.write_text(json.dumps(self.inventory))
        with patch.multiple(ac, _TRIGGEVENT_AUTOMARK_INVENTORY_CACHE=self.directory / "missing-cache.json",
                            _TRIGGEVENT_AUTOMARK_INVENTORY_SEED=seed):
            self.host._load_cached_native_automark_inventory()

    def test_offline_edits_persist_reload_and_replay_on_engine_start_and_restart(self):
        self.bridge._active = False
        self.load_offline_controls()
        self.assertTrue(self.host._native_automarkers_panel.isEnabled())
        self.assertIsNone(self.host._native_automark_inventory)
        self.control(QCheckBox, "top.looper.enabled.value").click()
        self.control(QSpinBox, "top.sigma.delay_seconds.value").setValue(17)
        jobs = self.control(QListWidget, "top.priority.value")
        jobs.setCurrentRow(1)
        self.control(QPushButton, "top.priority.up").click()
        marker = self.control(QComboBox, "top.delta.markers.NearWorld.marker")
        marker.setCurrentIndex(marker.findData("IGNORE1"))
        self.control(QCheckBox, "native_umad.value").click()
        expected = {"top.looper.enabled": True, "top.sigma.delay_seconds": 17,
                    "top.priority": ["WAR", "DRG", "SCH"],
                    "top.delta.markers": {"NearWorld": {"enabled": True, "marker": "IGNORE1"},
                                          "DistantWorld": {"enabled": True, "marker": "ATTACK2"}}}
        self.assertEqual(self.commands(), [])
        self.assertEqual(json.loads(self.settings_path.read_text())["triggevent_automark_settings"], expected)
        self.assertEqual(self.saved.call_count, 5)
        self.host._settings = {}
        self.host._load_settings()
        self.load_offline_controls()
        self.assertEqual(self.host._native_automarkers_panel._values["top.delta.markers"], expected["top.delta.markers"])
        self.assertFalse(self.control(QCheckBox, "native_umad.value").isChecked())
        self.assertEqual(self.commands(), [])
        self.saved.reset_mock()
        self.bridge._active = True
        for generation in (4, 5):
            with self.subTest(generation=generation):
                self.bridge._gen = generation
                self.bridge._dispatch({"t": "inventory", "triggers": []}, gen=generation)
                self.assertEqual(self.commands(), [{"nyaa_cmd": "set_automark", "native_umad": False},
                                                   self.full_command(expected, native_umad=False)])
                self.render()
                self.assertEqual(self.commands(), [])
                self.assertEqual(self.host._native_automarkers_panel._values["top.priority"], expected["top.priority"])
                self.assertTrue(self.control(QCheckBox, "top.looper.enabled.value").isChecked())
        self.saved.assert_not_called()

    def test_before_bridge_creation_umad_preference_and_local_ownership_stay_current(self):
        self.host._triggevent = None
        self.load_offline_controls()
        self.host._settings["umad_gaze_enabled"] = True
        self.host._apply_native_automark_state()
        self.assertFalse(self.host._native_automarkers_panel._umad_note.isHidden())
        self.control(QCheckBox, "native_umad.value").click()
        self.assertFalse(json.loads(self.settings_path.read_text())["native_umad_enabled"])
        self.assertFalse(self.control(QCheckBox, "native_umad.value").isChecked())
        self.host._settings["umad_gaze_enabled"] = False
        self.host._apply_native_automark_state()
        self.assertTrue(self.host._native_automarkers_panel._umad_note.isHidden())
        self.assertTrue(self.host._native_automarkers_panel.isEnabled())
        self.assertEqual(self.commands(), [])

    def test_typed_panel_changes_persist_and_share_the_enable_command(self):
        self.render()
        self.control(QCheckBox, "top.looper.enabled.value").click()
        self.control(QSpinBox, "top.sigma.delay_seconds.value").setValue(17)
        jobs = self.control(QListWidget, "top.priority.value")
        jobs.setCurrentRow(1)
        self.control(QPushButton, "top.priority.up").click()
        marker = self.control(QComboBox, "top.delta.markers.NearWorld.marker")
        marker.setCurrentIndex(marker.findData("CLEAR"))
        expected = {"top.looper.enabled": True, "top.sigma.delay_seconds": 17,
                    "top.priority": ["WAR", "DRG", "SCH"],
                    "top.delta.markers": {"NearWorld": {"enabled": True, "marker": "CLEAR"},
                                          "DistantWorld": {"enabled": True, "marker": "ATTACK2"}}}
        commands = self.commands()
        snapshots = [command["settings"] for command in commands]
        self.assertEqual([len(snapshot) for snapshot in snapshots], [1, 2, 3, 4])
        self.assertEqual(snapshots[-1], expected)
        self.assertEqual(commands, [self.full_command(snapshot) for snapshot in snapshots])
        saved = json.loads(self.settings_path.read_text())
        self.assertEqual(saved["triggevent_automark_settings"], expected)
        self.assertEqual(saved["unrelated"], "preserve")
        self.assertEqual(self.saved.call_count, 4)
        self.host._load_settings()
        self.assertEqual(self.host._settings["triggevent_automark_settings"], expected)
        self.host._apply_native_automark_state()
        self.assertEqual(self.commands(), [self.full_command(expected)])
        self.host._umad_chain_reset.assert_not_called()
        self.host._umad_gaze_reset.assert_not_called()

    def test_setting_callback_copies_mutable_values(self):
        value = {"NearWorld": {"enabled": True, "marker": "IGNORE1"},
                 "DistantWorld": {"enabled": False, "marker": "IGNORE2"}}
        expected = deepcopy(value)
        self.host._on_native_automark_setting_changed("top.delta.markers", value)
        value["NearWorld"]["marker"] = "CLEAR"
        self.assertEqual(self.host._settings["triggevent_automark_settings"], {"top.delta.markers": expected})
        self.assertEqual(self.commands(), [self.full_command({"top.delta.markers": expected})])
        self.assertEqual(json.loads(self.settings_path.read_text())["triggevent_automark_settings"], {"top.delta.markers": expected})

    def test_enabled_priority_order_is_saved_from_the_native_acknowledgement(self):
        self.render()
        self.control(QCheckBox, "top.ps.priority_override.value").click()
        self.commands()
        self.saved.reset_mock()
        self.render()
        self.saved.assert_not_called()
        descriptors = {setting["id"]: setting for setting in self.inventory["settings"]}
        descriptors["top.ps.priority_override"]["value"] = True
        order = ["WAR", "SCH", "DRG"]
        descriptors["top.ps.priority"]["value"] = order
        self.render()
        saved = json.loads(self.settings_path.read_text())["triggevent_automark_settings"]
        self.assertEqual(saved["top.ps.priority"], order)
        self.saved.assert_called_once()
        self.assertEqual(self.commands(), [])
        self.render()
        self.saved.assert_called_once()
        self.host._on_native_automark_setting_changed("top.priority", list(reversed(order)))
        self.assertEqual(self.commands()[-1]["settings"]["top.ps.priority"], order)
        self.host._load_settings()
        self.bridge._gen = 5
        self.host._apply_native_automark_state()
        self.assertEqual(self.commands()[-1]["settings"]["top.ps.priority"], order)

    def test_late_enabled_inventory_preserves_order_after_a_quick_disable(self):
        self.render()
        control = self.control(QCheckBox, "top.ps.priority_override.value")
        control.click()
        control.click()
        descriptors = {setting["id"]: setting for setting in self.inventory["settings"]}
        descriptors["top.ps.priority_override"]["value"] = True
        order = descriptors["top.ps.priority"]["value"]
        self.render()
        saved = json.loads(self.settings_path.read_text())["triggevent_automark_settings"]
        self.assertFalse(saved["top.ps.priority_override"])
        self.assertEqual(saved["top.ps.priority"], order)

    def test_reset_replaces_invalid_saved_preferences_before_unrelated_edits(self):
        defaults = {setting["id"]: setting["default"] for setting in self.inventory["settings"]}
        invalid_map = deepcopy(defaults["top.delta.markers"])
        invalid_map["NearWorld"]["marker"] = "UNKNOWN"
        for ident, invalid in (("uwu.enabled", 1), ("top.sigma.delay_seconds", 51),
                               ("top.priority", ["DRG", "DRG", "SCH"]),
                               ("top.delta.markers", invalid_map)):
            with self.subTest(ident=ident):
                self.host._settings["triggevent_automark_settings"] = {ident: invalid}
                self.render()
                self.assertEqual(self.commands(), [])
                self.control(QPushButton, ident + ".reset").click()
                corrected = {ident: defaults[ident]}
                self.assertEqual(self.host._settings["triggevent_automark_settings"], corrected)
                self.assertEqual(self.commands(), [self.full_command(corrected)])
                self.assertEqual(json.loads(self.settings_path.read_text())["triggevent_automark_settings"], corrected)
                self.control(QPushButton, ident + ".reset").click()
                self.assertEqual(self.commands(), [])
                self.control(QCheckBox, "top.looper.enabled.value").click()
                corrected["top.looper.enabled"] = True
                self.assertEqual(self.commands(), [self.full_command(corrected)])

    def test_unknown_saved_preferences_do_not_block_supported_edits_or_get_deleted(self):
        saved = {"future.unknown": True, "native_umad": False, "uwu.enabled": False}
        self.host._settings["triggevent_automark_settings"] = deepcopy(saved)
        self.render()
        self.assertEqual(self.commands(), [self.full_command({"uwu.enabled": False})])
        self.assertEqual(self.host._settings["triggevent_automark_settings"], saved)
        self.saved.assert_not_called()
        self.render()
        self.assertEqual(self.commands(), [])
        self.control(QCheckBox, "top.looper.enabled.value").click()
        saved["top.looper.enabled"] = True
        self.assertEqual(self.commands(), [self.full_command({"uwu.enabled": False,
                                                             "top.looper.enabled": True})])
        self.assertEqual(json.loads(self.settings_path.read_text())["triggevent_automark_settings"], saved)
        self.bridge._gen = 5
        self.host._apply_native_automark_state()
        self.assertEqual(self.commands(), [self.full_command(saved)])
        self.inventory["settings"].append({"id": "future.unknown", "type": "boolean",
                                           "default": False, "value": False})
        self.render()
        self.assertEqual(self.commands(), [self.full_command({key: value for key, value in saved.items()
                                                             if key != "native_umad"})])
        self.assertEqual(self.host._settings["triggevent_automark_settings"], saved)

    def test_callout_inventory_replays_saved_settings_on_start_and_restart(self):
        settings = {"uwu.enabled": False, "top.looper.enabled": True,
                    "top.priority": ["SCH", "WAR", "DRG"], "top.sigma.delay_seconds": 22}
        self.host._settings["triggevent_automark_settings"] = settings
        for generation in (4, 5):
            with self.subTest(generation=generation):
                self.bridge._gen = generation
                self.bridge._dispatch({"t": "inventory", "triggers": []}, gen=generation)
                self.assertEqual(self.commands(), [self.full_command(settings)])
        self.assertEqual(self.saved.call_count, 0)
        self.assertFalse(self.host._triggevent_mode)

    def test_current_repeated_and_stale_metadata_only_render(self):
        settings = {"top.looper.enabled": True}
        self.host._settings["triggevent_automark_settings"] = settings
        self.render()
        self.render()
        self.assertTrue(self.control(QCheckBox, "top.looper.enabled.value").isChecked())
        stale = deepcopy(self.inventory)
        stale["error"] = "old generation"
        self.bridge._dispatch(stale, gen=3)
        self.host._on_native_automark_inventory(json.dumps(stale), 3)
        self.assertNotIn("error", self.host._native_automark_inventory)
        self.assertEqual(self.commands(), [])
        self.saved.assert_not_called()
        self.assertFalse(self.settings_path.exists())

    def test_queued_old_generation_metadata_cannot_replace_new_controls(self):
        thread = threading.Thread(target=self.bridge._dispatch, args=(deepcopy(self.inventory),), kwargs={"gen": 4})
        thread.start()
        thread.join()
        self.assertIsNone(self.host._native_automark_inventory)
        self.bridge._gen = 5
        _QT_APP.processEvents()
        self.assertIsNone(self.host._native_automark_inventory)
        self.assertEqual(self.commands(), [])
        self.saved.assert_not_called()

    def test_bad_metadata_does_not_emit_or_replace_current_inventory(self):
        self.render()
        original = self.host._native_automark_inventory
        for change in ({"version": 2}, {"settings": {}}, {"mechanics": None}, {"jobs": "bad"}):
            with self.subTest(change=change):
                self.bridge._dispatch(dict(self.inventory, **change), gen=4)
                self.assertIs(self.host._native_automark_inventory, original)
        for payload in ("not JSON", "[]", '{"version":2}'):
            self.host._on_native_automark_inventory(payload, 4)
            self.assertIs(self.host._native_automark_inventory, original)
        self.assertEqual(self.commands(), [])
        self.saved.assert_not_called()

    def test_umad_requested_preference_is_separate_from_generic_settings(self):
        self.host._settings["triggevent_automark_settings"] = {"uwu.enabled": False}
        self.render()
        self.control(QCheckBox, "native_umad.value").click()
        self.assertFalse(self.host._settings["native_umad_enabled"])
        self.assertNotIn("native_umad", self.host._settings["triggevent_automark_settings"])
        self.assertEqual(self.commands(), [{"nyaa_cmd": "set_automark", "native_umad": False},
                                           self.full_command({"uwu.enabled": False}, native_umad=False)])
        saved = json.loads(self.settings_path.read_text())
        self.assertFalse(saved["native_umad_enabled"])
        self.assertNotIn("native_umad", saved["triggevent_automark_settings"])

    def test_local_p4_owners_keep_native_output_suppressed_when_requested(self):
        settings = {"uwu.enabled": False}
        self.render()
        for gaze, rules in ((True, []),
                            (False, [{"fight": "UMAD", "status": "15A8", "marker": "attack1"}]),
                            (False, [{"fight": "", "status": "0x15a7+0x15aa", "marker": "square"}])):
            with self.subTest(gaze=gaze, rules=rules):
                self.host._settings.update(triggevent_automark_settings=settings, umad_gaze_enabled=gaze,
                                           native_umad_enabled=False)
                self.host._automark_rules = rules
                self.host._on_native_automark_setting_changed("native_umad", True)
                self.assertTrue(self.host._settings["native_umad_enabled"])
                self.assertEqual(self.commands(), [{"nyaa_cmd": "set_automark", "native_umad": False},
                                                   self.full_command(settings, native_umad=False)])
                self.assertTrue(self.control(QCheckBox, "native_umad.value").isChecked())
        self.host._settings["umad_gaze_enabled"] = False
        self.host._automark_rules = [{"fight": "UMAD", "status": "644+BBC", "marker": "attack1"}]
        self.host._apply_native_automark_state()
        self.assertEqual(self.commands(), [self.full_command(settings)])

    def test_bad_saved_controls_cannot_block_master_disable(self):
        for settings in (None, {}, [], "not an object", {"future.unknown": True},
                         {"top.sigma.delay_seconds": "bad"}, {"native_umad": True}):
            with self.subTest(settings=settings):
                self.host._settings.update(telesto_enabled=False, telesto_uri="invalid URI")
                if settings is None:
                    self.host._settings.pop("triggevent_automark_settings", None)
                else:
                    self.host._settings["triggevent_automark_settings"] = settings
                self.host._apply_native_automark_state()
                full = {"nyaa_cmd": "set_automark", "enable": False,
                        "uri": "invalid URI", "native_umad": True}
                if isinstance(settings, dict) and settings:
                    full["settings"] = settings
                self.assertEqual(self.commands(), [{"nyaa_cmd": "set_automark", "enable": False,
                                                    "native_umad": True}, full])
        self.saved.assert_not_called()

    def test_bad_saved_controls_cannot_block_local_ownership(self):
        for gaze, requested, rules in ((True, True, []),
                                       (False, True, [{"fight": "UMAD", "status": "15A8", "marker": "attack1"}]),
                                       (False, False, [])):
            for settings in (None, {}, [], "not an object", {"future.unknown": True},
                             {"top.sigma.delay_seconds": "bad"}, {"native_umad": True}):
                with self.subTest(gaze=gaze, requested=requested, rules=rules, settings=settings):
                    self.host._settings.update(umad_gaze_enabled=gaze, native_umad_enabled=requested,
                                               telesto_uri="invalid URI")
                    self.host._automark_rules = rules
                    if settings is None:
                        self.host._settings.pop("triggevent_automark_settings", None)
                    else:
                        self.host._settings["triggevent_automark_settings"] = settings
                    self.host._apply_native_automark_state()
                    full = {"nyaa_cmd": "set_automark", "enable": True, "uri": "invalid URI", "native_umad": False}
                    if isinstance(settings, dict) and settings:
                        full["settings"] = settings
                    self.assertEqual(self.commands(), [{"nyaa_cmd": "set_automark", "native_umad": False}, full])
        self.saved.assert_not_called()

    def test_absent_preferences_keep_native_defaults_and_one_enable_command(self):
        self.host._apply_native_automark_state()
        self.assertEqual(self.commands(), [{"nyaa_cmd": "set_automark", "enable": True,
                                           "uri": TEST_URI, "native_umad": True}])
        self.assertNotIn("triggevent_automark_settings", self.host._settings)
        self.assertNotIn("native_umad_enabled", self.host._settings)
        self.saved.assert_not_called()

    def test_main_window_contains_panel_and_reloads_preferences_with_master_off(self):
        from tests import test_session_ui as fixture

        case = fixture.SessionUiTests()
        case.setUpClass()
        self.addCleanup(case.doCleanups)
        case.setUp()
        window = case.window
        self.assertIsInstance(window._native_automarkers_panel, NativeAutomarkersPanel)
        window._on_native_automark_inventory(json.dumps(self.inventory))
        window._native_automarkers_panel.findChild(QCheckBox, "top.looper.enabled.value").click()
        self.assertFalse(window._settings.get("telesto_enabled", False))
        self.assertTrue(window._settings["triggevent_automark_settings"]["top.looper.enabled"])
        with patch.object(fixture.mw.MainWindow, "_load_settings", SettingsTabMixin._load_settings):
            reloaded = fixture.mw.MainWindow()
        self.addCleanup(reloaded._prog_sessions.close)
        self.addCleanup(reloaded.close)
        reloaded._on_native_automark_inventory(json.dumps(self.inventory))
        self.assertTrue(reloaded._native_automarkers_panel.findChild(QCheckBox, "top.looper.enabled.value").isChecked())
        self.assertFalse(reloaded._settings.get("telesto_enabled", False))


if __name__ == "__main__":
    unittest.main()
