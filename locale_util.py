"""Locale and translation, the single source of truth for the UI language.

`_` is the translation function, a bare cheap call from anywhere, gettext
convention. Keys ARE the English source strings, so English is a pure
pass-through and every missing translation, or a whole missing or corrupt
catalog, falls back to English per key. Partial coverage always ships.

No Qt gets imported at module load, so tts.py and the tests can import this
without a QApplication. The only Qt touchpoint is reading the system locale for
`auto`, and that import is lazy and guarded.

Japanese `ja` is the first additive locale, but nothing here is JP specific.
The plumbing is locale parameterised by a 2 letter code, so a second language
never forces a rewrite. See JAPANESE_L10N_PLAN.md.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# Bundled read-only data lives under _MEIPASS when frozen, else beside this file.
# Mirrors main_window._BUNDLE_DIR. UI string maps ship as lang/<code>.json.
_BUNDLE_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
_LANG_DIR = _BUNDLE_DIR / "lang"

# English is the base locale and the universal fallback. Everything else is
# additive. Add a code here and ship a lang/<code>.json to grow the set.
DEFAULT_LOCALE = "en"
SUPPORTED_LOCALES = ("en", "ja")

# Active locale for _. Starts English. main_window sets it once at startup from
# effective_locale over ui_language. Module-global by design so _ needs no context.
_active_locale: str = DEFAULT_LOCALE

# english_key -> translated string, per locale, loaded once on first use. A locale
# with no catalog, or a corrupt one, caches {} so every key falls back to English
# and the disk is never re-read on a hot _ path.
_catalogs: dict[str, dict[str, str]] = {}


# ─────────────────────────── locale resolution ───────────────────────────

def normalize_locale(loc: str | None) -> str:
    """Coerce any locale name to a supported 2-letter code, else DEFAULT_LOCALE.

    Accepts the shapes a system hands back, "ja", "ja_JP", "ja-JP",
    "en_US.UTF-8", by taking the leading subtag. Unknown or empty -> "en"."""
    if not loc:
        return DEFAULT_LOCALE
    code = loc.strip().lower().replace("-", "_").split("_", 1)[0].split(".", 1)[0]
    return code if code in SUPPORTED_LOCALES else DEFAULT_LOCALE


def _system_locale_name() -> str:
    """Best-effort system locale name, e.g. "ja_JP". Prefers Qt's QLocale, which
    matches the running client, and falls back to the standard env vars so it
    stays usable headless and in tests. Never raises. "" if nothing is set."""
    try:
        from PyQt6.QtCore import QLocale  # lazy, keeps Qt off the module-load path
        name = QLocale.system().name()
        if name:
            return name
    except Exception:
        pass
    # GNU gettext precedence puts LANGUAGE first. A LANGUAGE=ja LANG=en_US
    # box is Japanese, not English.
    for var in ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"):
        val = os.environ.get(var)
        if val:
            return val.split(":", 1)[0]   # LANGUAGE may be colon-separated
    return ""


def effective_locale(setting: str, *, system_name: str | None = None) -> str:
    """Resolve a `ui_language` setting, "auto"|"en"|"ja", to a concrete "en"/"ja".

    "auto" follows the system locale, QLocale else env. An explicit code wins
    outright and detection never hard-forces JP on someone. Anything unrecognised
    resolves to English. `system_name` is an injection seam for tests. Production
    omits it and the system is queried."""
    # A hand edited settings value can be a truthy non-string. Treat it as empty.
    s = (setting if isinstance(setting, str) else "").strip().lower()
    if s == "auto":
        name = system_name if system_name is not None else _system_locale_name()
        return normalize_locale(name)
    return normalize_locale(s)


def set_locale(loc: str) -> None:
    """Set the active locale for _, coerced to a supported code with "en" as fallback."""
    global _active_locale
    _active_locale = normalize_locale(loc)


def active_locale() -> str:
    """The locale _ currently translates into."""
    return _active_locale


# ─────────────────────────────── translation ───────────────────────────────

def _load_catalog(loc: str) -> dict[str, str]:
    """Load lang/<loc>.json, english_key -> translation, cached. Any problem,
    missing file, bad JSON, wrong shape, caches and returns {}, so callers fall
    back to English per key and never re-hit the disk. Never raises. Non-string
    entries are dropped so one bad value can't poison an unrelated lookup."""
    # Every current caller passes an already normalized code, but `loc` names a
    # file under _LANG_DIR, so enforce the invariant here too. An unvalidated
    # value like "../../etc/passwd" must never reach the path join.
    if loc not in SUPPORTED_LOCALES:
        return {}
    cached = _catalogs.get(loc)
    if cached is not None:
        return cached
    catalog: dict[str, str] = {}
    if loc != DEFAULT_LOCALE:   # English keys are themselves. There is no file
        try:
            # utf-8-sig tolerates a BOM a hand edited catalog may carry. Plain
            # utf-8 fails the parse and caches {}, silencing every translation.
            raw = json.loads((_LANG_DIR / f"{loc}.json").read_text(encoding="utf-8-sig"))
            if isinstance(raw, dict):
                catalog = {k: v for k, v in raw.items()
                           if isinstance(k, str) and isinstance(v, str)}
        except (OSError, ValueError):
            catalog = {}
    _catalogs[loc] = catalog
    return catalog


def reload_catalogs() -> None:
    """Drop cached catalogs so the next lookup re-reads from disk. Used after a
    downloaded translation update, or when _LANG_DIR is repointed under test."""
    _catalogs.clear()


def _(key: str) -> str:
    """Translate `key` into the active locale, else return it unchanged.

    Keys are the English source strings, so English is a pass-through and any
    missing translation, or a missing catalog, transparently falls back to
    English. An EMPTY value falls back too. extract_strings.py emits "" stubs
    for untranslated keys, which must read as English, not as a blank callout.
    Cheap. The catalog is loaded once and cached."""
    if not isinstance(key, str):
        return key
    if _active_locale == DEFAULT_LOCALE:
        return key
    return _load_catalog(_active_locale).get(key) or key


def N_(text: str) -> str:
    """Mark a string literal for extraction without translating it now.

    For strings defined in module-level data, table headers, marker labels, that
    are translated later at render time with `_`. extract_strings.py records
    the literal so the catalog keeps the key. N_ itself is a pass-through, so the
    data still holds the English source that `_` looks up. gettext's noop mark."""
    return text


# ──────────────────────────────── detection ────────────────────────────────

def has_japanese(text: str) -> bool:
    """True if `text` holds any Hiragana, U+3040-309F, Katakana, U+30A0-30FF,
    Katakana Phonetic Extensions U+31F0-31FF, Kana Supplement U+1B000-1B0FF,
    half-width forms U+FF65-FF9F incl. the voiced marks U+FF9E/FF9F, or kanji:
    CJK Unified Ideographs U+4E00-9FFF, Extension A U+3400-4DBF, Extension B
    U+20000-2A6DF, Extensions C through E U+2A700-2CEAF, Compatibility
    Ideographs U+F900-FAFF, and the iteration mark U+3005. Routes TTS to a
    Japanese voice and serves as a general "is this JP" signal."""
    for ch in text:
        o = ord(ch)
        if (0x3040 <= o <= 0x309F or 0x30A0 <= o <= 0x30FF or 0xFF65 <= o <= 0xFF9F
                or 0x31F0 <= o <= 0x31FF or 0x1B000 <= o <= 0x1B0FF
                or 0x3400 <= o <= 0x4DBF or 0x4E00 <= o <= 0x9FFF
                or 0x20000 <= o <= 0x2A6DF or 0x2A700 <= o <= 0x2CEAF
                or 0xF900 <= o <= 0xFAFF or o == 0x3005):
            return True
    return False
