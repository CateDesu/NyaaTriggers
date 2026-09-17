"""UI locale selection and translation. English source strings are lookup keys and
fallbacks for missing translations. Qt is imported only when detecting the system
locale.
"""
from __future__ import annotations

import json
import os

from nyaatriggers.paths import bundle_root

_BUNDLE_DIR = bundle_root()
_LANG_DIR = _BUNDLE_DIR / "lang"

# Add a supported code and its lang catalog to offer another locale.
DEFAULT_LOCALE = "en"
SUPPORTED_LOCALES = ("en", "ja")

# Set once at startup so translation calls need no context.
_active_locale: str = DEFAULT_LOCALE

# Cache missing or invalid catalogs too so lookups do not repeatedly read disk.
_catalogs: dict[str, dict[str, str]] = {}



def normalize_locale(loc: str | None) -> str:
    """Normalize a locale name to a supported code, falling back to English."""
    if not loc:
        return DEFAULT_LOCALE
    code = loc.strip().lower().replace("-", "_").split("_", 1)[0].split(".", 1)[0]
    return code if code in SUPPORTED_LOCALES else DEFAULT_LOCALE


def _system_locale_name() -> str:
    """Read the system locale from Qt, then environment variables. Return an empty string
    if unavailable.
    """
    try:
        from PyQt6.QtCore import QLocale  # lazy, keeps Qt off the module-load path
        name = QLocale.system().name()
        if name:
            return name
    except Exception:
        pass
    # GNU gettext gives LANGUAGE precedence over LANG.
    for var in ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"):
        val = os.environ.get(var)
        if val:
            return val.split(":", 1)[0]   # LANGUAGE may be colon-separated
    return ""


def effective_locale(setting: str, *, system_name: str | None = None) -> str:
    """Resolve an explicit UI language or detect the system locale for auto. system_name
    can supply a locale for tests.
    """
    s = (setting if isinstance(setting, str) else "").strip().lower()
    if s == "auto":
        name = system_name if system_name is not None else _system_locale_name()
        return normalize_locale(name)
    return normalize_locale(s)


def set_locale(loc: str) -> None:
    """Set the active supported locale, falling back to English."""
    global _active_locale
    _active_locale = normalize_locale(loc)


def active_locale() -> str:
    """Return the active locale."""
    return _active_locale



def _load_catalog(loc: str) -> dict[str, str]:
    """Load and cache a locale catalog. Ignore nonstring entries and return an empty
    catalog on failure.
    """
    # Validate the locale before using it in a file path.
    if loc not in SUPPORTED_LOCALES:
        return {}
    cached = _catalogs.get(loc)
    if cached is not None:
        return cached
    catalog: dict[str, str] = {}
    if loc != DEFAULT_LOCALE:
        try:
            # Accept a BOM in hand edited catalogs.
            raw = json.loads((_LANG_DIR / f"{loc}.json").read_text(encoding="utf-8-sig"))
            if isinstance(raw, dict):
                catalog = {k: v for k, v in raw.items()
                           if isinstance(k, str) and isinstance(v, str)}
        except (OSError, ValueError, RecursionError):
            catalog = {}
    _catalogs[loc] = catalog
    return catalog


def reload_catalogs() -> None:
    """Clear cached catalogs so subsequent lookups read them again."""
    _catalogs.clear()


def _(key: str) -> str:
    """Translate a source string using the cached catalog. Missing or empty translations
    fall back to the source.
    """
    if not isinstance(key, str):
        return key
    if _active_locale == DEFAULT_LOCALE:
        return key
    return _load_catalog(_active_locale).get(key) or key


def N_(text: str) -> str:
    """Mark a literal for catalog extraction while leaving translation until render time.
    """
    return text



def has_japanese(text: str) -> bool:
    """Detect kana and kanji characters used to select Japanese speech."""
    for ch in text:
        o = ord(ch)
        if (0x3040 <= o <= 0x309F or 0x30A0 <= o <= 0x30FF or 0xFF65 <= o <= 0xFF9F
                or 0x31F0 <= o <= 0x31FF or 0x1B000 <= o <= 0x1B0FF
                or 0x3400 <= o <= 0x4DBF or 0x4E00 <= o <= 0x9FFF
                or 0x20000 <= o <= 0x2A6DF or 0x2A700 <= o <= 0x2CEAF
                or 0xF900 <= o <= 0xFAFF or o == 0x3005):
            return True
    return False
