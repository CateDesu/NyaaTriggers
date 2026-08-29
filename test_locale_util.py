"""Regression tests for locale_util: the _() translation function, effective_locale
resolution, and has_japanese detection.

Run directly:  python test_locale_util.py   (exit 0 = all pass)
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import locale_util
from locale_util import _, effective_locale, has_japanese, set_locale

FAILS = []


def check(name, cond):
    print(("PASS  " if cond else "FAIL  ") + name)
    if not cond:
        FAILS.append(name)


# ── has_japanese: the three plan ranges, plus clear negatives ──
check("kana + kanji is Japanese", has_japanese("フレア来ます"))
check("hiragana alone is Japanese", has_japanese("たすけて"))
check("katakana alone is Japanese", has_japanese("スタック"))
check("a lone kanji is Japanese", has_japanese("北"))
check("one kana inside English is Japanese", has_japanese("go 東 now"))
check("plain ASCII is not Japanese", not has_japanese("stack"))
check("empty string is not Japanese", not has_japanese(""))
check("digits/punctuation/tokens are not Japanese", not has_japanese("123 !? {target}"))

# ── effective_locale: explicit wins, auto follows the (injected) system ──
check("explicit en stays en", effective_locale("en") == "en")
check("explicit ja stays ja", effective_locale("ja") == "ja")
check("auto under ja_JP -> ja", effective_locale("auto", system_name="ja_JP") == "ja")
check("auto under en_US.UTF-8 -> en", effective_locale("auto", system_name="en_US.UTF-8") == "en")
check("auto under bare 'ja' -> ja", effective_locale("auto", system_name="ja") == "ja")
check("auto under ja-JP (hyphen) -> ja", effective_locale("auto", system_name="ja-JP") == "ja")
check("auto under empty system -> en", effective_locale("auto", system_name="") == "en")
check("auto under C locale -> en", effective_locale("auto", system_name="C") == "en")
check("unknown explicit locale -> en", effective_locale("de") == "en")
check("truthy non-str setting -> en, never raises", effective_locale(5) == "en")

# ── _(): English passes through, ja translates, misses fall back per key ──
set_locale("en")
check("en is a pass-through", _("Settings") == "Settings")
set_locale("ja")
check("ja translates a known key", _("Settings") == "設定")
check("ja translates another known key", _("Automarkers") == "オートマーカー")
check("ja falls back on an unknown key", _("__no_such_ui_key__") == "__no_such_ui_key__")
set_locale("en")

# an empty stub value (untranslated key, as extract_strings.py emits) reads English
_orig = locale_util._LANG_DIR
_tmp0 = Path(tempfile.mkdtemp())
(_tmp0 / "ja.json").write_text('{"Settings": "設定", "Clear": ""}', encoding="utf-8")
locale_util._LANG_DIR = _tmp0
locale_util.reload_catalogs()
set_locale("ja")
check("empty stub value falls back to English", _("Clear") == "Clear")
check("filled sibling still translates", _("Settings") == "設定")
locale_util._LANG_DIR = _orig
locale_util.reload_catalogs()
set_locale("en")
check("set_locale('en') restores pass-through", _("Settings") == "Settings")
set_locale("xx")
check("unsupported set_locale coerces to en", _("Settings") == "Settings")

# ── resilience: a corrupt / missing catalog never crashes, degrades to English ──
_orig_dir = locale_util._LANG_DIR
try:
    tmp = Path(tempfile.mkdtemp())
    (tmp / "ja.json").write_text("{ this is not valid json", encoding="utf-8")
    locale_util._LANG_DIR = tmp
    locale_util.reload_catalogs()
    set_locale("ja")
    check("corrupt ja.json degrades to English", _("Settings") == "Settings")
    (tmp / "ja.json").unlink()
    locale_util.reload_catalogs()
    check("missing ja.json degrades to English", _("Settings") == "Settings")

    # wrong-shape catalog (list, and a non-string value) must not crash a lookup
    (tmp / "ja.json").write_text('{"Settings": 5, "Clear": "クリア"}', encoding="utf-8")
    locale_util.reload_catalogs()
    check("non-string value is dropped, valid sibling still works",
          _("Settings") == "Settings" and _("Clear") == "クリア")

    # a BOM'd catalog, a hand edited file saved with a BOM, must still load
    (tmp / "ja.json").write_bytes(b"\xef\xbb\xbf" + '{"Settings": "設定"}'.encode("utf-8"))
    locale_util.reload_catalogs()
    check("BOM'd ja.json still translates", _("Settings") == "設定")
finally:
    locale_util._LANG_DIR = _orig_dir
    locale_util.reload_catalogs()
    set_locale("en")

print()
if FAILS:
    print(f"{len(FAILS)} FAILED: {FAILS}")
    sys.exit(1)
print("all tests passed")
