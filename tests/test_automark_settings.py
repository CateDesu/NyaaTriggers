import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from nyaatriggers import app_common as ac
from nyaatriggers.telesto_client import MARKER_TOKENS
from nyaatriggers.ui.automarkers_tab import AutomarkersTabMixin
from nyaatriggers.ui.settings_tab import SettingsTabMixin
from tests import test_session_ui as fixture


DEFAULTS = {
    "umad_chain_marker_dps": "attack1",
    "umad_chain_marker_support": "attack2",
    "umad_chain_marker_accretion": "attack3",
    "umad_gaze_marker_away1": "ignore1",
    "umad_gaze_marker_away2": "ignore2",
    "umad_gaze_marker_look1": "bind1",
    "umad_gaze_marker_look2": "bind2",
}


class AutomarkSettingsTests(unittest.TestCase):
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
        case = fixture.SessionUiTests()
        case.setUpClass()
        self.addCleanup(case.doCleanups)
        case.setUp()
        saved = {**case.window._settings, **dict.fromkeys(DEFAULTS, [])}
        ac._SETTINGS_FILE.write_text(json.dumps(saved), encoding="utf-8")
        with patch.object(fixture.mw.MainWindow, "_load_settings", SettingsTabMixin._load_settings):
            window = fixture.mw.MainWindow()
        self.addCleanup(window.close)
        choices = {"umad_chain_marker_" + key: combo.currentData()
                   for key, combo in window._umad_chain_combos.items()}
        choices.update({"umad_gaze_marker_" + key: combo.currentData()
                        for key, combo in window._umad_gaze_combos.items()})
        self.assertEqual(choices, DEFAULTS)


if __name__ == "__main__":
    unittest.main()
