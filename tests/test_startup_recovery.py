"""Damaged saved data must not crash callbacks or replace unrelated choices."""

from contextlib import ExitStack
import json
import os
from pathlib import Path
import random
import re
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt6.QtWidgets import QApplication

from nyaatriggers import app_common as ac
from nyaatriggers.record_store import load_records
from nyaatriggers.ui.connection import ConnectionMixin
from nyaatriggers.ui.dps_tab import DpsTabMixin
from nyaatriggers.ui.settings_tab import SettingsTabMixin
from nyaatriggers.ui.triggers_tab import TriggersTabMixin

APP = QApplication.instance() or QApplication([])


class StartupRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))

    def test_unusable_translation_cache_keeps_the_bundled_callouts(self):
        cache, bundle = self.root / "cache.json", self.root / "bundle.json"
        bundle.write_text(json.dumps({"app_version": "1.0", "callouts": {"id": "翻訳"}}))
        for data in ({"app_version": "2.0", "callouts": {"broken": 123}},
                     {"app_version": "9" * 5000, "callouts": {"id": "bad"}}):
            with self.subTest(data=str(data)[:70]):
                cache.write_text(json.dumps(data))
                host = SimpleNamespace()
                with patch.object(ac, "_CALLOUTS_JA_CACHE", cache), \
                        patch.object(ac, "_CALLOUTS_JA_BUNDLE", bundle), \
                        patch("nyaatriggers.ui.triggers_tab.set_readings"):
                    TriggersTabMixin._load_cached_callouts_ja(host)
                self.assertEqual(host._callouts_ja, {"id": "翻訳"})

    def test_translation_cache_can_supply_only_phrase_translations(self):
        cache, bundle = self.root / "cache.json", self.root / "bundle.json"
        bundle.write_text(json.dumps({"app_version": "1.0", "callouts": {"id": "翻訳"}}))
        cache.write_text(json.dumps({"app_version": "2.0", "callouts": {}, "phrases": {"Spread": "散開"}}))
        host = SimpleNamespace()
        with patch.object(ac, "_CALLOUTS_JA_CACHE", cache), \
                patch.object(ac, "_CALLOUTS_JA_BUNDLE", bundle), \
                patch("nyaatriggers.ui.triggers_tab.set_readings"):
            TriggersTabMixin._load_cached_callouts_ja(host)
        self.assertEqual(host._callouts_phrases_ja, {"Spread": "散開"})

    def test_nested_lookup_files_keep_their_fallbacks(self):
        deep = "[" * 2000 + "]" * 2000
        for path_name, cache_name, call, fallback in (
                ("_REPO_TRIGGERS_VERSION", None, ac._repo_download_version, None),
                ("ZONE_NAMES_FILE", "_zone_names_cache", lambda: ac.canonical_zone_name(1), ""),
                ("CACTBOT_TIMELINES_FILE", "_cactbot_tl_cache", lambda: ac.cactbot_timeline_for_zone(1), ())):
            with self.subTest(file=path_name), ExitStack() as stack:
                path = self.root / path_name
                path.write_text(deep)
                stack.enter_context(patch.object(ac, path_name, path))
                if cache_name:
                    stack.enter_context(patch.object(ac, cache_name, None))
                self.assertEqual(call(), fallback)

    @unittest.skipIf(os.name == "nt", "Uses the Linux IINACT configuration location")
    def test_nested_iinact_config_keeps_the_default_log_directory(self):
        config = self.root / ".xlcore" / "pluginConfigs" / "IINACT.json"
        config.parent.mkdir(parents=True)
        config.write_text("[" * 2000 + "]" * 2000)
        logs = self.root / "Documents" / "IINACT"
        logs.mkdir(parents=True)
        with patch.object(Path, "home", return_value=self.root):
            self.assertEqual(ConnectionMixin._find_iinact_log_dir(), logs)

    def test_infinite_saved_idle_timeout_uses_the_default(self):
        for value in (float("inf"), float("-inf")):
            with self.subTest(value=value):
                host = SimpleNamespace(_settings={"dps_idle_timeout": value},
                                       _on_meter_encounter_end=Mock())
                DpsTabMixin._init_dps(host)
                self.assertEqual(host._dps_idle_timeout, 120)

    @unittest.skipUnless(os.name == "posix" and os.geteuid() != 0, "Needs directory permissions")
    def test_unreadable_record_directory_is_reported(self):
        directory = self.root / "profiles"
        directory.mkdir()
        directory.chmod(0)
        try:
            records, errors = load_records(directory, lambda data: None)
        finally:
            directory.chmod(0o700)
        self.assertEqual(records, [])
        self.assertEqual(len(errors), 1)

    def test_failed_settings_serialization_preserves_the_previous_file(self):
        path = self.root / "settings.json"
        path.write_text('{"char_name":"Previous"}')
        host = SimpleNamespace(_settings={"char_name": "\ud800"}, _warn_save_failed=Mock())
        with patch.object(ac, "_SETTINGS_FILE", path):
            self.assertFalse(SettingsTabMixin._save_settings(host))
        self.assertEqual(json.loads(path.read_text()), {"char_name": "Previous"})
        host._warn_save_failed.assert_called_once()
        self.assertEqual(list(self.root.iterdir()), [path])

    def test_translation_templates_do_not_stall_on_repeated_fragments(self):
        script = '''
from types import SimpleNamespace
from nyaatriggers.app_common import _compile_phrase_patterns
from nyaatriggers.ui.settings_tab import SettingsTabMixin
phrase = "Group {currentGroup}: {first} and {second} ({event.estimatedRemainingDuration})"
host = SimpleNamespace(_settings={"callouts_localized": True}, _callouts_phrases_ja={},
                       _callouts_phrases_ja_patterns=_compile_phrase_patterns({phrase: "グループ"}))
text = "Group " + ": x and y (" * 500 + "unfinished"
assert SettingsTabMixin._localize_text(host, text) == text
'''
        result = subprocess.run([sys.executable, "-c", script], capture_output=True,
                                text=True, timeout=3)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_fragment_matching_preserves_template_matching_semantics(self):
        rng = random.Random(1731)
        for _ in range(5000):
            parts = ["".join(rng.choices("ab\n.*界", k=rng.randrange(4)))
                     for _ in range(rng.randrange(2, 7))]
            reference = re.compile("^" + ".*?".join(map(re.escape, parts)) + "$", re.DOTALL)
            pattern = ac._PhrasePattern(parts)
            if rng.randrange(2):
                text = "".join(part + "".join(rng.choices("ab\n.*界", k=rng.randrange(4)))
                               for part in parts[:-1]) + parts[-1]
            else:
                text = "".join(rng.choices("ab\n.*界", k=rng.randrange(25)))
            if rng.randrange(3) == 0:
                text += "\n"
            self.assertEqual(pattern.match(text), bool(reference.match(text)), (parts, text))

    def test_absent_record_directory_remains_a_normal_first_run(self):
        self.assertEqual(load_records(self.root / "missing", lambda data: None), ([], []))

    def test_file_in_place_of_record_directory_is_reported(self):
        path = self.root / "profiles"
        path.write_text("Not a directory")
        records, errors = load_records(path, lambda data: None)
        self.assertEqual(records, [])
        self.assertEqual(len(errors), 1)
        self.assertEqual(path.read_text(), "Not a directory")

    def test_failed_settings_serialization_can_be_retried_after_repair(self):
        path = self.root / "settings.json"
        cycle = {}
        cycle["cycle"] = cycle
        host = SimpleNamespace(_settings=cycle, _warn_save_failed=Mock())
        with patch.object(ac, "_SETTINGS_FILE", path):
            self.assertFalse(SettingsTabMixin._save_settings(host))
            self.assertFalse(path.exists())
            host._settings = {"char_name": "Repaired"}
            self.assertTrue(SettingsTabMixin._save_settings(host))
        self.assertEqual(json.loads(path.read_text()), host._settings)
        self.assertEqual(list(self.root.iterdir()), [path])


if __name__ == "__main__":
    unittest.main()
