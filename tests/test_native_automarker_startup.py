from contextlib import ExitStack
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6 import sip
from PyQt6.QtCore import QCoreApplication, QEvent
from PyQt6.QtWidgets import QApplication, QCheckBox

from nyaatriggers import app_common as ac
from nyaatriggers import main_window as mw
from nyaatriggers import triggevent_bridge as tb
from nyaatriggers.triggevent_recovery import TriggeventRecovery
from nyaatriggers.ws_client import WSClient
from tests.test_native_automarker_settings import SettingsHost
from tests.test_native_automarkers_ui import native_inventory
from tests import test_session_ui as session_ui


APP = QApplication.instance() or QApplication([])


class NativeAutomarkerStartupTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        directory = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.cache = directory / "cache.json"
        self.seed = directory / "seed.json"
        self.stack.enter_context(patch.multiple(ac,
            _TRIGGEVENT_AUTOMARK_INVENTORY_CACHE=self.cache,
            _TRIGGEVENT_AUTOMARK_INVENTORY_SEED=self.seed))
        self.stack.enter_context(patch.object(tb, "record"))
        self.host = SettingsHost()
        self.host._save_settings = Mock()
        self.host._settings["telesto_enabled"] = False
        self.bridge = self.host._triggevent
        self.bridge.automark_inventory.disconnect(self.host._on_native_automark_inventory)
        self.bridge.automark_inventory.connect(self.host._receive_native_automark_inventory)
        self.panel = self.host._native_automarkers_panel
        self.inventory = native_inventory() | {"version": 1}
        self.seed.write_text(json.dumps(self.inventory))
        self.addCleanup(self.close_host)
        self.addCleanup(setattr, self.bridge, "_active", False)

    def close_host(self):
        self.host.close()
        self.host.deleteLater()
        APP.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.assertTrue(sip.isdeleted(self.host))

    def render_live(self, inventory=None, generation=4):
        self.bridge._dispatch(deepcopy(inventory or self.inventory), gen=generation)

    def test_startup_seed_restores_choices_without_saving_or_sending_settings(self):
        self.host._settings.update(native_umad_enabled=False,
                                  triggevent_automark_settings={"top.looper.enabled": True})
        settings = deepcopy(self.host._settings)
        self.host._load_cached_native_automark_inventory()
        self.assertEqual(len(self.panel._editors), 46)
        self.assertEqual(self.panel._tabs.count(), 4)
        self.assertTrue(self.panel.findChild(QCheckBox, "top.looper.enabled.value").isChecked())
        self.assertFalse(self.panel.findChild(QCheckBox, "native_umad.value").isChecked())
        self.assertTrue(self.panel.isEnabled())
        self.assertIsNone(self.host._native_automark_inventory)
        self.assertEqual(self.host._settings, settings)
        self.host._save_settings.assert_not_called()
        self.assertTrue(self.bridge._wq.empty())

    def test_invalid_or_empty_cache_falls_back_to_seed(self):
        for invalid in ("not JSON", "[]", '{"version":2}', '{"version":1,"settings":[]}'):
            with self.subTest(invalid=invalid):
                self.cache.write_text(invalid)
                self.host._load_cached_native_automark_inventory()
                self.assertEqual(len(self.panel._editors), 46)
                self.assertTrue(self.panel.isEnabled())

    def test_missing_cache_and_seed_leave_controls_unavailable(self):
        self.seed.unlink()
        self.host._load_cached_native_automark_inventory()
        self.assertFalse(self.panel.isEnabled())
        self.assertEqual(self.panel._editors, {})
        self.host._save_settings.assert_not_called()

    def test_malformed_optional_presets_do_not_abort_cached_controls(self):
        for invalid in (None, 17, "preset"):
            with self.subTest(invalid=invalid):
                cached = deepcopy(self.inventory)
                for setting in cached["settings"]:
                    if setting["type"] == "marker_map":
                        setting["presets"] = invalid
                self.cache.write_text(json.dumps(cached))
                settings = deepcopy(self.host._settings)
                self.host._load_cached_native_automark_inventory()
                self.assertEqual(len(self.panel._editors), 46)
                self.assertTrue(self.panel.isEnabled())
                self.assertEqual(self.host._settings, settings)
                self.host._save_settings.assert_not_called()
                self.assertTrue(self.bridge._wq.empty())

    def test_malformed_optional_priority_metadata_keeps_valid_cached_values(self):
        for changes in ({"override_enabled": {}}, {"parent": []},
                        {"parent": "absent", "effective_order": None},
                        {"override_enabled": "top.priority"}):
            with self.subTest(changes=changes):
                cached = deepcopy(self.inventory)
                setting = next(row for row in cached["settings"] if row["id"] == "top.ps.priority")
                setting.update(changes)
                self.cache.write_text(json.dumps(cached))
                settings = deepcopy(self.host._settings)
                self.host._load_cached_native_automark_inventory()
                self.assertEqual(len(self.panel._editors), 46)
                self.assertEqual(self.panel._values["top.ps.priority"], setting["value"])
                self.assertTrue(self.panel.isEnabled())
                self.assertEqual(self.host._settings, settings)
                self.host._save_settings.assert_not_called()
                self.assertTrue(self.bridge._wq.empty())

    def test_live_inventory_replaces_cache_and_enables_controls(self):
        cached = deepcopy(self.inventory)
        cached["settings"][0]["value"] = False
        self.cache.write_text(json.dumps(cached))
        self.host._load_cached_native_automark_inventory()
        self.assertFalse(self.panel.findChild(QCheckBox, "uwu.enabled.value").isChecked())
        self.render_live()
        self.assertTrue(self.panel.findChild(QCheckBox, "uwu.enabled.value").isChecked())
        self.assertTrue(self.panel.isEnabled())
        self.assertEqual(self.panel._engine_message, "")
        self.assertEqual(json.loads(self.cache.read_text()), self.inventory)
        self.host._check_native_automarkers_ready(4)
        self.assertTrue(self.panel.isEnabled())

    def test_legacy_engine_leaves_choices_visible_with_update_guidance(self):
        self.host._load_cached_native_automark_inventory()
        with patch("nyaatriggers.ui.engines.QTimer.singleShot") as schedule:
            self.host._on_native_automarkers_ready(4)
        self.assertEqual(schedule.call_args.args[0], 5000)
        schedule.call_args.args[1]()
        self.assertEqual(len(self.panel._editors), 46)
        self.assertTrue(self.panel.isEnabled())
        self.assertIn("Update the Triggevent engine", self.panel._engine_message)
        self.render_live()
        self.assertTrue(self.panel.isEnabled())
        self.assertEqual(self.panel._engine_message, "")

    def test_stale_inventory_and_ready_callbacks_do_not_replace_current_controls(self):
        self.render_live()
        with patch("nyaatriggers.ui.engines.QTimer.singleShot") as schedule:
            self.host._on_native_automarkers_ready(3)
        schedule.call_args.args[1]()
        stale = deepcopy(self.inventory)
        stale["settings"][0]["value"] = False
        self.host._receive_native_automark_inventory(json.dumps(stale), 3)
        self.assertTrue(self.panel.findChild(QCheckBox, "uwu.enabled.value").isChecked())
        self.assertTrue(self.panel.isEnabled())
        self.assertEqual(json.loads(self.cache.read_text()), self.inventory)

    def test_restarted_engine_keeps_choices_editable_until_current_inventory(self):
        self.host._engine_sidecar_state = {}
        self.host._update_engine_status_label = Mock()
        self.render_live()
        self.assertTrue(self.panel.isEnabled())
        settings = deepcopy(self.host._settings)
        self.bridge._gen = 5
        self.host._on_engine_sidecar_status("triggevent", False, "Off", 4)
        self.host._on_engine_sidecar_status("triggevent", True, "Starting Triggevent Engine...", 5)
        self.assertTrue(self.panel.isEnabled())
        self.assertIn("Loading native automarker controls", self.panel._engine_message)
        self.assertEqual(len(self.panel._editors), 46)
        self.render_live(generation=4)
        self.assertTrue(self.panel.isEnabled())
        self.assertEqual(self.host._settings, settings)
        self.host._save_settings.assert_not_called()
        self.render_live(generation=5)
        self.assertTrue(self.panel.isEnabled())
        self.assertEqual(self.panel._engine_message, "")
        self.host._on_engine_sidecar_status("triggevent", True, "Ready", 5)
        self.assertTrue(self.panel.isEnabled())

    def test_rejected_config_does_not_poison_next_startup_cache(self):
        self.render_live()
        rejected = dict(self.inventory, error="Rejected setting")
        self.render_live(rejected)
        self.assertEqual(json.loads(self.cache.read_text()), self.inventory)
        self.assertTrue(self.panel.isEnabled())
        self.host._save_settings.assert_not_called()

    def test_inventory_replay_restart_does_not_enable_or_cache_obsolete_controls(self):
        self.host._engine_sidecar_state = {}
        self.host._update_engine_status_label = Mock()
        self.bridge.status.connect(lambda active, msg, gen:
            self.host._on_engine_sidecar_status("triggevent", active, msg, gen))
        recovery = TriggeventRecovery(self.bridge, WSClient(), lambda: None, self.host)
        self.addCleanup(recovery._progress_timer.stop)
        recovery._on_status(True, "Starting", 4)
        self.host._settings.update(telesto_enabled=True,
                                  triggevent_automark_settings={"unsupported.setting": True})
        self.host._load_cached_native_automark_inventory()
        self.bridge._wq = tb._ByteQueue(1)
        self.bridge._wq.put_nowait("occupied")

        def restart():
            self.bridge._gen += 1
            self.bridge._active = True
            self.bridge._wq = tb._ByteQueue(10)
            self.bridge.status.emit(True, "Starting Triggevent Engine...", self.bridge.generation())

        with patch.object(self.bridge, "start", side_effect=restart) as start:
            self.render_live()
        start.assert_called_once()
        self.assertEqual(self.bridge.generation(), 6)
        self.assertEqual(self.host._native_automark_inventory_generation, 4)
        self.assertTrue(self.panel.isEnabled())
        self.assertFalse(self.cache.exists())
        self.render_live(generation=6)
        self.assertTrue(self.panel.isEnabled())
        self.assertEqual(self.host._native_automark_inventory_generation, 6)
        self.assertEqual(json.loads(self.cache.read_text()), self.inventory)

    def test_engine_exit_preserves_failure_and_rejects_queued_inventory(self):
        self.host._load_cached_native_automark_inventory()
        with patch("nyaatriggers.ui.engines.QTimer.singleShot") as schedule:
            self.host._on_native_automarkers_ready(4)
        self.bridge._active = False
        self.panel.set_engine_status("Sidecar exited")
        schedule.call_args.args[1]()
        self.host._receive_native_automark_inventory(json.dumps(self.inventory), 4)
        self.assertEqual(self.panel._engine_message, "Sidecar exited")
        self.assertTrue(self.panel.isEnabled())
        self.assertIsNone(self.host._native_automark_inventory)
        self.assertFalse(self.cache.exists())

    def test_engine_missing_keeps_saved_choices_editable(self):
        self.host._engine_sidecar_state = {}
        self.host._update_engine_status_label = Mock()
        self.host._load_cached_native_automark_inventory()
        self.bridge._active = False
        with patch.object(tb.TriggeventBridge, "is_available", return_value=False), \
                patch("nyaatriggers.ui.engines._te_has_jar", return_value=False):
            self.host._note_triggevent_unavailable()
        self.assertTrue(self.panel.isEnabled())
        self.assertIn("Engine not installed", self.panel._engine_message)
        self.panel.findChild(QCheckBox, "top.looper.enabled.value").click()
        self.assertTrue(self.host._settings["triggevent_automark_settings"]["top.looper.enabled"])
        self.host._save_settings.assert_called_once()
        self.assertTrue(self.bridge._wq.empty())


class NativeAutomarkerWindowStartupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        session_ui.SessionUiTests.setUpClass()
        cls.app = session_ui.SessionUiTests.app

    def setUp(self):
        with patch.object(tb.TriggeventBridge, "is_available", return_value=True), \
                patch.object(ac, "_TRIGGEVENT_AUTOMARK_INVENTORY_CACHE", Path("/missing-native-cache")):
            session_ui.SessionUiTests.setUp(self)

    def test_window_loads_native_choices_and_starts_engine_with_callouts_off(self):
        self.assertFalse(self.window._triggevent_mode)
        panel = self.window._native_automarkers_panel
        self.assertEqual(len(panel._editors), 46)
        self.assertEqual(panel._tabs.count(), 4)
        self.assertTrue(panel.isEnabled())
        callbacks = [call.args for call in mw.QTimer.singleShot.call_args_list]
        self.assertIn((900, self.window._reconcile_triggevent_engine), callbacks)


class NativeAutomarkerPackagingTests(unittest.TestCase):
    def test_release_harvest_ignores_startup_noise_and_preserves_both_seeds(self):
        root = Path(__file__).resolve().parents[1]
        workflow = (root / ".github/workflows/release.yml").read_text()
        harvest = workflow.split("- name: Harvest Triggevent inventory seed", 1)[1]
        block = harvest.split("          python3 - <<'PY'\n", 1)[1].split("          PY\n", 1)[0]
        script = "\n".join(line[10:] for line in block.splitlines())
        inventory = native_inventory() | {"version": 1}
        triggers = {"t": "inventory", "triggers": [{"id": "sample", "name": "Sample", "tts": "Sample speech"}]}
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            (directory / "assets").mkdir()
            noise = ['42', '[]', 'null', '"startup script"', 'not JSON']
            (directory / "engine.out").write_text("\n".join([*noise, json.dumps(triggers), json.dumps(inventory)]))
            result = subprocess.run([sys.executable, "-c", script], cwd=directory,
                                    text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            seed = json.loads((directory / "triggevent_inventory.seed.json").read_text())
            self.assertEqual(seed[0]["id"], "sample")
            self.assertEqual(seed[0]["tts"], "Sample speech")
            self.assertEqual(json.loads((directory / "assets/triggevent_automarkers.seed.json").read_text()), inventory)


if __name__ == "__main__":
    unittest.main()
