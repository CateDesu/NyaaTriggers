"""Game status metadata needed by combat recording without Qt."""

from functools import lru_cache
import json
from zipfile import BadZipFile, ZipFile

from nyaatriggers.paths import bundle_root


@lru_cache(maxsize=1)
def _permanent_statuses():
    try:
        with ZipFile(bundle_root() / "assets" / "recap_icons.zip") as archive:
            if archive.getinfo("catalog.json").file_size > 16 << 20:
                return frozenset()
            rows = json.loads(archive.read("catalog.json"))["Status"]
        if not isinstance(rows, dict):
            return frozenset()
        return frozenset(int(ident) for ident, row in rows.items()
                         if ident.isascii() and ident.isdecimal() and len(ident) <= 10
                         and isinstance(row, dict) and row.get("is_permanent") is True)
    except (OSError, ValueError, KeyError, TypeError, RecursionError, BadZipFile):
        return frozenset()


def is_permanent(ident):
    return type(ident) is int and ident in _permanent_statuses()
