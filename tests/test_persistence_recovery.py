"""Recovery from malformed files and restoration of saved preferences."""

import contextlib
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt6.QtWidgets import QComboBox

from tests.test_data_safety import TriggerHost, ability
from nyaatriggers import app_common as ac, locale_util
from nyaatriggers.dps_meter import DpsMeter
from nyaatriggers.ui.settings_tab import SettingsTabMixin
from nyaatriggers.ui.voice_tab import VoiceTabMixin


class VoiceHost(VoiceTabMixin):
    def __init__(self, settings=None):
        self._settings = settings or {}
        self._voice_combo = QComboBox()
        self._save_settings = Mock()
        self._on_kokoro_download = Mock()


class PersistenceRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.deep = "[" * 200000 + "0" + "]" * 200000

    def patch_paths(self):
        for name in ("_SETTINGS_FILE", "TRIGGERS_FILE", "TRIGGERS_LOCAL_FILE",
                     "_REPO_TRIGGERS_FILE", "_REPO_RETIRED_FILE", "_REPO_TRIGGERS_VERSION",
                     "RETIRED_FILE", "_CALLOUTS_JA_CACHE", "_CALLOUTS_JA_BUNDLE",
                     "CALLOUT_DEFAULTS_FILE"):
            self.stack.enter_context(patch.object(ac, name, self.root / (name + ".json")))
        self.stack.enter_context(patch("nyaatriggers.ui.triggers_tab._repo_download_version",
                                       return_value=ac._VERSION))
        self.stack.enter_context(patch("nyaatriggers.ui.triggers_tab.set_readings"))
        self.stack.enter_context(patch.object(ac, "log_drop"))
        self.stack.enter_context(patch.object(ac.QMessageBox, "warning"))
        ac.TRIGGERS_FILE.write_text('[{"id": "official", "name": "Official"}]')

    def test_deep_settings_preserve_original_and_rotated_backups(self):
        self.patch_paths()
        ac._SETTINGS_FILE.write_text(self.deep)
        backups = []
        for _ in range(2):
            host = SimpleNamespace(_settings={})
            SettingsTabMixin._load_settings(host)
            self.assertEqual(host._settings, {})
            _, backup = host._settings_load_warning
            backups.append(backup)
            self.assertEqual(backup.read_text(), self.deep)
        self.assertNotEqual(*backups)
        self.assertEqual(ac._SETTINGS_FILE.read_text(), self.deep)

    def test_deep_official_override_falls_back_to_bundle(self):
        self.patch_paths()
        ac._REPO_TRIGGERS_FILE.write_text(self.deep)
        host = TriggerHost()
        host._load_triggers()
        self.assertEqual(host._official_ids, {"official"})
        ac.TRIGGERS_FILE.write_text(self.deep)
        host._load_triggers()
        self.assertEqual(host._official_ids, set())

    def test_oversized_trigger_numbers_load_from_official_and_imported_files(self):
        self.patch_paths()
        huge = json.loads("9" * 400)
        row = {"id": "oversized", "log_type": "26", "ability_id": "ABC",
               "cooldown_s": huge, "duration_min": huge, "duration_max": huge,
               "speed": huge, "expiry_warn_s": huge}
        ac._REPO_TRIGGERS_FILE.write_text(json.dumps([row]))
        host = TriggerHost()
        host._load_triggers()
        trigger = host._triggers[0]
        self.assertEqual((trigger.cooldown_s, trigger.speed), (5.0, 1.0))
        self.assertEqual((trigger.duration_min, trigger.duration_max, trigger.expiry_warn_s), (0.0, 0.0, 0.0))
        pack = self.root / "import.json"
        pack.write_text(json.dumps({"triggers": [row | {"id": "imported"}]}))
        with patch.object(ac.QFileDialog, "getOpenFileName", return_value=(str(pack), "")), \
                patch.object(ac.QMessageBox, "question", return_value=ac.QMessageBox.StandardButton.Yes), \
                patch.object(ac.QMessageBox, "information"):
            host._import_triggers()
        self.assertEqual(ac.TRIGGERS_LOCAL_FILE.read_bytes(), pack.read_bytes())
        restarted = TriggerHost()
        restarted._load_triggers()
        self.assertFalse(restarted._local_corrupt)
        self.assertEqual({t.id for t in restarted._triggers}, {"oversized", "imported"})
        self.assertTrue(all(t.cooldown_s == 5.0 for t in restarted._triggers))

    def test_deep_local_file_blocks_saves_and_repaired_file_reloads(self):
        self.patch_paths()
        ac.TRIGGERS_LOCAL_FILE.write_text(self.deep)
        host = TriggerHost()
        host._load_triggers()
        self.assertTrue(host._local_corrupt)
        self.assertFalse(host._save_triggers())
        self.assertEqual(ac.TRIGGERS_LOCAL_FILE.read_text(), self.deep)
        self.assertEqual(Path(str(ac.TRIGGERS_LOCAL_FILE) + ".bad").read_text(), self.deep)
        host._maybe_reload_triggers()
        self.assertEqual(host._official_ids, {"official"})
        ac.TRIGGERS_LOCAL_FILE.write_text('{"triggers": [{"id": "custom", "name": "Custom"}]}')
        host._maybe_reload_triggers()
        self.assertFalse(host._local_corrupt)
        self.assertIn("custom", {t.id for t in host._triggers})
        self.assertTrue(host._save_triggers())
        json.loads(ac.TRIGGERS_LOCAL_FILE.read_text())

    def test_external_corruption_between_load_and_save_is_preserved(self):
        self.patch_paths()
        host = TriggerHost()
        host._load_triggers()
        ac.TRIGGERS_LOCAL_FILE.write_text(self.deep)
        self.assertFalse(host._save_triggers())
        self.assertTrue(host._local_corrupt)
        self.assertEqual(ac.TRIGGERS_LOCAL_FILE.read_text(), self.deep)

    def test_deep_optional_data_and_catalogs_use_fallbacks(self):
        self.patch_paths()
        host = TriggerHost()
        for file in (ac.RETIRED_FILE, ac.CALLOUT_DEFAULTS_FILE, ac._CALLOUTS_JA_CACHE):
            file.write_text(self.deep)
        host._load_triggers()
        self.assertEqual(host._official_ids, {"official"})
        self.assertEqual(host._retired_ids, set())
        self.assertEqual(host._load_callout_defaults(), {})
        ac._CALLOUTS_JA_BUNDLE.write_text('{"callouts": {"official": "訳"}}')
        host._load_cached_callouts_ja()
        self.assertEqual(host._callouts_ja, {"official": "訳"})
        ac._CALLOUTS_JA_BUNDLE.write_text(self.deep)
        host._load_cached_callouts_ja()
        self.assertEqual(host._callouts_ja, {})
        (self.root / "ja.json").write_text(self.deep)
        with patch.object(locale_util, "_LANG_DIR", self.root), \
             patch.object(locale_util, "_catalogs", {}):
            self.assertEqual(locale_util._load_catalog("ja"), {})

    def voices(self, settings=None, stems=None):
        stems = stems if stems is not None else ["en_GB-arctic-medium", "en_US-arctic-low",
                                                "en_US-arctic-medium"]
        for stem in stems:
            (self.root / (stem + ".onnx")).touch()
        host = VoiceHost(settings)
        self.stack.enter_context(patch.object(ac, "_USER_VOICES_DIR", self.root))
        self.stack.enter_context(patch.object(ac, "_BUNDLE_DIR", self.root))
        host._populate_voice_combo()
        return host

    def test_same_named_voice_round_trip_and_distinct_labels(self):
        host = self.voices()
        combo = host._voice_combo
        selected = self.root / "en_US-arctic-medium.onnx"
        combo.setCurrentIndex(combo.findData(str(selected)))
        with patch("nyaatriggers.ui.voice_tab.set_model") as model, \
             patch("nyaatriggers.ui.voice_tab.set_jp_neural"):
            host._on_voice_changed(combo.currentIndex())
            self.assertEqual(host._settings["voice_model"], selected.stem)
            self.assertEqual(len({combo.itemText(i) for i in range(3)}), 3)
            combo.setCurrentIndex(0)
            host._restore_voice_model()
            self.assertEqual(combo.currentData(), str(selected))
            model.assert_called_with(selected)
            host._on_kokoro_download.assert_not_called()

    def test_legacy_names_and_missing_models_migrate_without_setup(self):
        host = self.voices()
        fallback = self.root / "en_GB-arctic-medium.onnx"
        with patch("nyaatriggers.ui.voice_tab.set_model") as model:
            for saved in ("(ENG) Arctic", "deleted-model", None, 42):
                with self.subTest(saved=saved):
                    host._settings = {"voice_model": saved}
                    host._restore_voice_model()
                    self.assertEqual(host._settings["voice_model"], fallback.stem)
                    self.assertEqual(host._voice_combo.currentData(), str(fallback))
                    model.assert_called_with(fallback)
                    host._on_kokoro_download.assert_not_called()

    def test_saved_stem_wins_over_a_legacy_label_collision(self):
        host = self.voices({"voice_model": "(ENG) Arctic"},
                           stems=["en_US-arctic-medium", "(ENG) Arctic"])
        with patch("nyaatriggers.ui.voice_tab.set_model") as model:
            host._restore_voice_model()
            model.assert_called_once_with(self.root / "(ENG) Arctic.onnx")

    def test_japanese_selection_keeps_saved_english_model(self):
        host = self.voices({"voice_model": "en_US-arctic-medium", "jp_neural_enabled": True,
                            "jp_neural_voice": "jm_kumo"})
        with patch("nyaatriggers.ui.voice_tab.set_model") as model, \
             patch("nyaatriggers.ui.voice_tab.set_jp_neural") as neural:
            host._restore_voice_model()
            self.assertEqual(host._voice_combo.currentData(), "kokoro:jm_kumo")
            model.assert_called_with(self.root / "en_US-arctic-medium.onnx")
            self.assertEqual(host._settings["voice_model"], "en_US-arctic-medium")
            host._settings["jp_neural_voice"] = "removed"
            host._restore_voice_model()
            self.assertEqual(host._voice_combo.currentData(), "kokoro:jf_alpha")
            neural.assert_called_once_with(True, "jf_alpha")
            host._on_kokoro_download.assert_not_called()

    def test_no_piper_models_does_not_enable_japanese(self):
        host = self.voices(stems=[])
        with patch("nyaatriggers.ui.voice_tab.set_model") as model, \
             patch("nyaatriggers.ui.voice_tab.set_jp_neural") as neural:
            host._restore_voice_model()
            self.assertEqual(host._voice_combo.currentIndex(), -1)
            model.assert_not_called()
            neural.assert_not_called()
            host._on_kokoro_download.assert_not_called()

    def test_removing_a_voice_keeps_the_refresh_fallback_persisted(self):
        host = self.voices({"voice_model": "en_US-arctic-medium"})
        with patch("nyaatriggers.ui.voice_tab.set_model") as model, \
             patch("nyaatriggers.ui.voice_tab.set_jp_neural"):
            host._restore_voice_model()
            (self.root / "en_US-arctic-medium.onnx").unlink()
            host._refresh_voice_combo()
            self.assertEqual(host._settings["voice_model"], "en_GB-arctic-medium")
            model.assert_called_with(self.root / "en_GB-arctic-medium.onnx")
            host._save_settings.assert_called_once()

    def test_user_model_wins_over_the_bundled_copy(self):
        user = self.root / "user"
        bundle = self.root / "bundle"
        user.mkdir()
        (bundle / "voices").mkdir(parents=True)
        name = "en_US-arctic-medium.onnx"
        (user / name).touch()
        (bundle / "voices" / name).touch()
        host = VoiceHost({"voice_model": "en_US-arctic-medium"})
        with patch.object(ac, "_USER_VOICES_DIR", user), \
             patch.object(ac, "_BUNDLE_DIR", bundle), \
             patch("nyaatriggers.ui.voice_tab.set_model") as model:
            host._populate_voice_combo()
            host._restore_voice_model()
            model.assert_called_with(user / name)
            self.assertEqual(host._voice_combo.itemText(0), "(ENG) Arctic")
            self.assertEqual(host._voice_combo.count(), 1 + len(ac._JP_NEURAL_VOICES))

    def test_display_timeout_cannot_change_recorded_boundaries(self):
        for scenario in ("lazy", "second flag", "falling edge"):
            for gap in (60, 120, 121, 601):
                results = []
                for idle in (15, 30, 120, 600):
                    with self.subTest(scenario=scenario, gap=gap, idle=idle):
                        now = [1000.0]
                        meter = DpsMeter(clock=lambda: now[0])
                        ended = []
                        meter.on_encounter_end = ended.append
                        meter.set_idle_timeout(idle)
                        meter.process(["02", "ts", "10000001", "Player"])
                        if scenario != "lazy":
                            meter.set_in_combat(True, False)
                        meter.process(ability("750003", 100))
                        first = meter.current.pull_id
                        if scenario == "falling edge":
                            meter.set_in_combat(False, False)
                        now[0] += gap
                        meter.set_in_combat(True, True)
                        result = (len(ended), meter.current.pull_id == first)
                        results.append(result)
                        split = gap > 120 or scenario == "falling edge"
                        self.assertEqual(result, (int(split), not split))
                        if ended:
                            self.assertEqual(ended[0]["Encounter"]["damage"], 100)
                self.assertEqual(len(set(results)), 1)


if __name__ == "__main__":
    unittest.main()
