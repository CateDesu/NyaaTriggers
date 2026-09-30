"""Japanese display text keeps protocol values and saved definitions intact."""

from collections import Counter
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication, QDialog, QDialogButtonBox, QTableWidget

from nyaatriggers import game_locale
from nyaatriggers.app_common import _compile_phrase_patterns, _phrase_template, _C_EN, _C_NAME, _C_FIGHT
from nyaatriggers.fight_catalog import FightPickerDialog
from nyaatriggers.locale_util import _, engine_status, set_locale
from nyaatriggers.plugin_link import timeline_frame
from nyaatriggers.triggernometry_dialog import combo, options
from nyaatriggers.ui.recap_widgets import status_name
from nyaatriggers.ui.settings_tab import SettingsTabMixin
from nyaatriggers.ui.engines import EnginesMixin

APP = QApplication.instance() or QApplication([])
ROOT = Path(__file__).resolve().parents[1]


class JapaneseDisplayTests(unittest.TestCase):
    def setUp(self):
        set_locale("ja")

    def tearDown(self):
        set_locale("en")

    def test_standard_buttons_follow_program_locale(self):
        box = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        self.assertEqual(box.button(QDialogButtonBox.StandardButton.Save).text(), "保存")
        self.assertEqual(box.button(QDialogButtonBox.StandardButton.Cancel).text(), "キャンセル")
        set_locale("en")
        APP.processEvents()
        self.assertEqual(box.button(QDialogButtonBox.StandardButton.Save).text(), "Save")

    def test_fight_search_accepts_japanese_and_keeps_saved_tag(self):
        picker = FightPickerDialog([{"difficulty": "Ultimate", "expansion": "Endwalker",
                                    "name": "Dragonsong's Reprise", "folder_name": "DSR",
                                    "has_triggers": True}])
        leaf = picker._tree.topLevelItem(1).child(0).child(0)
        picker._filter("竜詩")
        self.assertFalse(leaf.isHidden())
        self.assertEqual(leaf.data(0, Qt.ItemDataRole.UserRole), "DSR")
        picker._tree.setCurrentItem(leaf)
        picker._on_ok()
        self.assertEqual(picker.selected_folder(), "DSR")

    def test_native_editor_translates_only_display_values(self):
        widget = combo(options(("StringEqualNocase", "NumericGreater")), "NumericGreater")
        self.assertEqual(widget.currentText(), "より大きい")
        self.assertEqual(widget.currentData(), "NumericGreater")

    def test_recap_metadata_uses_japanese_with_english_fallback(self):
        original = {"name": "Weakness", "description": "Reduced attributes", "icon": 215010}
        translated = game_locale.localized_metadata("Status", 43, original)
        self.assertEqual(translated["name"], "衰弱")
        self.assertIn("低下", translated["description"])
        self.assertEqual(translated["icon"], original["icon"])
        self.assertEqual(status_name({"name": "Weakness"}, translated), "衰弱")
        self.assertEqual(original["name"], "Weakness")
        self.assertEqual(game_locale.localized_metadata("Status", 999999, original), original)
        set_locale("en")
        self.assertEqual(game_locale.localized_metadata("Status", 43, original), original)

    def test_timeline_translation_keeps_cue_type_and_timestamp(self):
        with patch.object(game_locale, "_catalog", return_value={"timelines": {"Tank Buster": "タンク強攻撃"}}):
            frame = timeline_frame([(12.5, "Tank Buster")])
        self.assertEqual(frame["v"], [[12.5, "タンク強攻撃", "tankbuster"]])

    def test_engine_status_preserves_error_details(self):
        self.assertEqual(engine_status("Failed to launch sidecar: missing.exe"),
                         "エンジンを起動できませんでした：missing.exe")
        self.assertEqual(engine_status("unrecognized engine detail"), "unrecognized engine detail")

    def test_saved_log_message_uses_locale_plural_forms(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "combat.txt"
            with patch("nyaatriggers.ui.settings_tab.ac.QFileDialog") as dialog, \
                    patch("nyaatriggers.ui.settings_tab.ac.QMessageBox.information") as notice:
                dialog.return_value.exec.return_value = QDialog.DialogCode.Accepted
                dialog.return_value.selectedFiles.return_value = [str(path)]
                for language, count, expected in (
                        ("ja", 1, "1行を保存しました："), ("ja", 2, "2行を保存しました："),
                        ("en", 1, "Saved 1 line to:"), ("en", 2, "Saved 2 lines to:")):
                    with self.subTest(language=language, count=count):
                        set_locale(language)
                        lines = [f"line {number}" for number in range(count)]
                        SettingsTabMixin._save_raw_log(SimpleNamespace(_raw_capture=lines))
                        self.assertEqual(path.read_text(), "\n".join(lines) + "\n")
                        self.assertEqual(notice.call_args.args[2], f"{expected}\n{path}")

    def test_dynamic_readings_keep_player_names(self):
        readings = {"{target}に強攻撃、その後{safe}": "{target}にきょうこうげき、そのあと{safe}", "北": "きた"}
        owner = SimpleNamespace(_callouts_readings=readings,
                                _callouts_reading_patterns=_compile_phrase_patterns(readings, minimum_literal=1))
        self.assertEqual(SettingsTabMixin._reading_for(owner, "Alice Exampleに強攻撃、その後北"),
                         "Alice Exampleにきょうこうげき、そのあときた")

    def test_engine_table_uses_name_translations_and_keeps_identifiers(self):
        owner = SimpleNamespace(
            _table=QTableWidget(0, 7), _settings={}, _engine_disabled={},
            _engine_text_overrides={}, _callouts_names_ja={},
            _callouts_names_text_ja={"Test Buster": "テスト強攻撃"},
            _callouts_phrases_ja={}, _callouts_phrases_ja_patterns=[],
            _callout_edits_for=lambda source: {}, _engine_fight_tag=lambda row: row["fight"])
        owner._localized_name = lambda trigger: SettingsTabMixin._localized_name(owner, trigger)
        owner._localize_text = lambda text: SettingsTabMixin._localize_text(owner, text)
        EnginesMixin._append_engine_row(owner, {"source": "triggevent", "id": "example",
                                                "name": "Test Buster", "fight": "DSR", "text": ""})
        self.assertEqual(owner._table.item(0, _C_NAME).text(), "テスト強攻撃")
        self.assertEqual(owner._table.item(0, _C_EN).data(Qt.ItemDataRole.UserRole), "triggevent:example")
        self.assertEqual(owner._table.item(0, _C_FIGHT).data(Qt.ItemDataRole.UserRole), "DSR")

    def test_callout_catalog_preserves_mechanic_details(self):
        catalog = json.loads((ROOT / "tools/callout_phrases_ja.json").read_text())
        for english, row in catalog.items():
            if not isinstance(row, dict):
                continue
            tokens = Counter(token for token in _phrase_template(english)[1]
                             if "duration" not in token.lower())
            with self.subTest(phrase=english):
                for field in ("display", "reading"):
                    self.assertEqual(tokens, Counter(token for token in _phrase_template(row[field])[1]
                                                     if "duration" not in token.lower()))
        phrases = json.loads((ROOT / "assets/callouts_ja.json").read_text())["phrases"]
        owner = SimpleNamespace(_settings={}, _callouts_phrases_ja=phrases,
                                _callouts_phrases_ja_patterns=_compile_phrase_patterns(phrases))
        for english, japanese in (("East then North", "東から北"),
                                  ("North or South", "北か南"),
                                  ("Move East", "東へ移動"),
                                  ("Stack on Alice Example", "Alice Exampleで頭割り"),
                                  ("Left then Right (North)", "左から右、北"),
                                  ("Right then Left (Alice Example)", "右から左、Alice Example"),
                                  ("Left then Right (5.0s)", "左→右"),
                                  ("Rotate Left", "左に回る"),
                                  ("Rotate Right", "右に回る"),
                                  ("Be On Blue Square", "青い床に乗る")):
            self.assertEqual(SettingsTabMixin._localize_text(owner, english), japanese)

    def test_callout_readings_use_game_pronunciation(self):
        catalog = json.loads((ROOT / "assets/callouts_ja.json").read_text())
        owner = SimpleNamespace(_callouts_readings=catalog["readings"],
                                _callouts_reading_patterns=_compile_phrase_patterns(
                                    catalog["readings"], minimum_literal=1, restrict_choices=False))
        for english, reading in (("Partner Stacks", "ふたりあたまわり"),
                                 ("Light Parties Cross", "じゅうじでよにんあたまわり"),
                                 ("Kill Adds", "ざこをしょり"),
                                 ("Stand In Front", "まえにたつ"),
                                 ("Emptiness", "むのぼうそう"),
                                 ("Front then North", "まえからきた"),
                                 ("Front or Left", "まえかひだり"),
                                 ("Corners and Spread", "かどでさんかい"),
                                 ("Go to third line", "さんぼんめのせんへ"),
                                 ("West and Face In", "にしでうちがわをむく"),
                                 ("1 set", "いっかいめ"),
                                 ("6 set", "ろっかいめ"),
                                 ("8 set", "はっかいめ"),
                                 ("Tiles", "ゆかのます"),
                                 ("Floor Spikes", "わな: とげ"),
                                 ("Twisters", "ついすたー")):
            with self.subTest(phrase=english):
                self.assertEqual(SettingsTabMixin._reading_for(owner, catalog["phrases"][english]), reading)

    def test_dynamic_tethers_keep_values_containing_spaces(self):
        catalog = json.loads((ROOT / "assets/callouts_ja.json").read_text())
        owner = SimpleNamespace(
            _settings={}, _callouts_phrases_ja=catalog["phrases"],
            _callouts_phrases_ja_patterns=_compile_phrase_patterns(catalog["phrases"]),
            _callouts_readings=catalog["readings"],
            _callouts_reading_patterns=_compile_phrase_patterns(
                catalog["readings"], minimum_literal=1, restrict_choices=False))
        for english, japanese, reading in (
                ("Alice Example Outer Tether, Cross", "Alice Exampleの外側の線、反対側へ",
                 "Alice Exampleのそとがわのせん、はんたいがわへ"),
                ("Alice Example Inner Tether, Stay", "Alice Exampleの内側の線、そのまま",
                 "Alice Exampleのうちがわのせん、そのまま")):
            with self.subTest(callout=english):
                localized = SettingsTabMixin._localize_text(owner, english)
                self.assertEqual(localized, japanese)
                self.assertEqual(SettingsTabMixin._reading_for(owner, localized), reading)
        unknown = "Alice Example Other Tether, Stay"
        self.assertEqual(SettingsTabMixin._localize_text(owner, unknown), unknown)

    def test_timeline_catalog_has_meaningful_labels(self):
        curated = json.loads((ROOT / "tools/game_text_ja.json").read_text())
        bundled = json.loads((ROOT / "assets/game_data_ja.json").read_text())
        self.assertEqual(bundled["timelines"], curated["timelines"])
        for english, japanese in curated["timelines"].items():
            with self.subTest(label=english):
                self.assertTrue(any(character.isalnum() for character in japanese))
                self.assertNotIn("(?", japanese)
        for english, japanese in (("Ultrasonic Amp", "アグリゲートソニック"),
                                  ("Ultrasonic Spread (DPS)", "スプレッドソニック (DPS)"),
                                  ("Morning Stars", "明けの連星"),
                                  ("(debuffs resolve)", "(デバフ発動)")):
            self.assertEqual(game_locale.game_text(english, "timelines"), japanese)

    def test_trigger_names_keep_roles_and_mechanic_terms(self):
        names = json.loads((ROOT / "assets/callouts_ja.json").read_text())["names_text"]
        for english, japanese in (
                ("Limitless Synergy as non-Tank: Give Away Tether", "連携プログラムLB：非タンクは線を渡す"),
                ("Graven Image", "神々の像"),
                ("Mooncleaver", "剛刃一閃"),
                ("Break IV", "ブレクジャ"),
                ("EnuoEx Dense Emptiness", "エヌオーEX 集束波動"),
                ("Fundamental Synergy - Numbers", "連携プログラムC - 数字"),
                ("Darklit Dragonsong: Initial", "光と闇の竜詩：開幕"),
                ("Chaos Thrust / Chaotic Spring (reapply warning)", "桜華狂咲 / 桜華繚乱（再付与警告）"),
                ("Enrage", "時間切れ"),
                ("Spread", "散開"),
                ("Third Art of Darkness: Partner Stacks", "闇の戦技：三重: 2人頭割り"),
                ("Kefka Says: Fake Dynamic Fluid (Second Set Applied)",
                 "ケフカの指示: 偽物の混沌の水 (2セット目 付与)")):
            with self.subTest(name=english):
                self.assertEqual(names[english], japanese)

    def test_bundled_ui_and_local_triggers_have_translations(self):
        catalog = json.loads((ROOT / "lang/ja.json").read_text())
        self.assertTrue(all(isinstance(value, str) and value for value in catalog.values()))
        callouts = json.loads((ROOT / "assets/callouts_ja.json").read_text())
        for row in json.loads((ROOT / "assets/triggers.json").read_text()):
            with self.subTest(trigger=row["name"]):
                self.assertTrue(callouts["callouts"].get(row["id"]))
                self.assertTrue(callouts["names"].get(row["id"]))


if __name__ == "__main__":
    unittest.main()
