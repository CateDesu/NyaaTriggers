from functools import lru_cache
import json

from nyaatriggers.locale_util import active_locale
from nyaatriggers.paths import bundle_root


@lru_cache(maxsize=1)
def _catalog():
    try:
        data = json.loads((bundle_root() / "assets" / "game_data_ja.json").read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, RecursionError):
        return {}


def game_text(text, section="names"):
    if active_locale() != "ja":
        return text
    values = _catalog().get(section, {})
    value = values.get(text) if isinstance(values, dict) else None
    return value if isinstance(value, str) and value else text


def fight_label(fight):
    translated = game_text(fight, "fights")
    if translated == fight:
        translated = game_text(fight)
    return f"{translated} [{fight}]" if translated != fight else fight


def localized_metadata(sheet, ident, metadata):
    if active_locale() != "ja":
        return metadata
    rows = _catalog().get(sheet, {})
    row = rows.get(str(ident), {}) if isinstance(rows, dict) else {}
    if not isinstance(row, dict):
        return metadata
    fields = {key: value for key, value in row.items()
              if key in ("name", "description") and isinstance(value, str) and value}
    return {**metadata, **fields}
