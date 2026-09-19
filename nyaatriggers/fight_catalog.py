#!/usr/bin/env python3
"""Offline fight catalog and picker, extended by cached cactbot data. Each entry has
difficulty, expansion, name, folder_name and has_triggers fields. The folder name
matches trigger.fight.
"""

from __future__ import annotations

import json
import os
import re
import socket
import threading
import time
import urllib.request
from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QDialog, QDialogButtonBox, QLabel, QLineEdit, QTreeWidget, QTreeWidgetItem,
    QVBoxLayout,
)

from nyaatriggers.drop_log import log_drop
from nyaatriggers.http_fetch import open_response
from nyaatriggers.locale_util import _, N_

FightTree = list[tuple[str, list[tuple[str, list[str]]]]]

_DIFFICULTIES = (N_("Savage"), N_("Ultimate"), N_("Extreme"))

_EXPANSION = {
    "02-arr": N_("A Realm Reborn"), "03-hw": N_("Heavensward"),
    "04-sb": N_("Stormblood"), "05-shb": N_("Shadowbringers"),
    "06-ew": N_("Endwalker"), "07-dt": N_("Dawntrail"),
}

_ULTIMATE_INFO = {
    "UCoB": ("Stormblood",      "The Unending Coil of Bahamut"),
    "UwU":  ("Stormblood",      "The Weapon's Refrain"),
    "TEA":  ("Shadowbringers",  "The Epic of Alexander"),
    "DSR":  ("Endwalker",       "Dragonsong's Reprise"),
    "TOP":  ("Endwalker",       "The Omega Protocol"),
    "FRU":  ("Dawntrail",       "Futures Rewritten"),
    "UMAD": ("Dawntrail",       "Dancing Mad"),
}

# Use the local fight tags so cactbot entries merge with the offline catalog.
_ULTIMATE_STEM_TO_TAG = {
    "unending_coil_ultimate":       "UCoB",
    "ultima_weapon_ultimate":       "UwU",
    "the_epic_of_alexander":        "TEA",
    "dragonsongs_reprise_ultimate": "DSR",
    "the_omega_protocol":           "TOP",
    "futures_rewritten":            "FRU",
    "dancing_mad":                  "UMAD",
}

# Use the local fight tags to avoid duplicate picker entries.
_TRIAL_STEM_TO_TAG = {
    "queen-eternal-ex": "Queen EX",
    "ultima-ex":        "Ultima's Bane EX",
}

_CACTBOT_TREE_API = (
    "https://api.github.com/repos/OverlayPlugin/cactbot/git/trees/main?recursive=1"
)


def _entry(difficulty: str, expansion: str, name: str,
           folder_name: str, has_triggers: bool) -> dict:
    return {
        "difficulty": difficulty, "expansion": expansion or "Other",
        "name": name, "folder_name": folder_name, "has_triggers": bool(has_triggers),
    }


def build_offline(fight_tree: FightTree, known_tags: set[str]) -> list[dict]:
    """Build the offline catalog from a category, expansion and tag tree. known_tags
    identifies fights that have triggers.
    """
    out: list[dict] = []
    for category, exps in fight_tree:
        if category == "Ultimates":
            for expansion, tags in exps:
                for tag in tags:
                    exp, name = _ULTIMATE_INFO.get(tag, (expansion, tag))
                    out.append(_entry("Ultimate", exp, name, tag, tag in known_tags))
        elif category in ("Savage Raids", "Extreme Trials"):
            kind = "Savage" if category == "Savage Raids" else "Extreme"
            for expansion, tags in exps:
                for tag in tags:
                    out.append(_entry(kind, expansion, tag, tag, tag in known_tags))
    return out


def parse_cactbot_paths(paths: list[str]) -> list[dict]:
    """Derive fight entries from paths in the cactbot raidboss data tree."""
    out: list[dict] = []
    seen: set[tuple] = set()
    for p in paths:
        m = re.match(r"ui/raidboss/data/(0\d-\w+)/(raid|ultimate|trial)/([^/]+)\.ts$", p)
        if not m:
            continue
        prefix, kind, stem = m.group(1), m.group(2), m.group(3)
        expansion = _EXPANSION.get(prefix, N_("Other"))
        if kind == "ultimate":
            tag = _ULTIMATE_STEM_TO_TAG.get(stem)
            if tag is not None:
                difficulty, name = "Ultimate", _ULTIMATE_INFO[tag][1]
                folder = tag
            else:
                difficulty, name = "Ultimate", _titleize(stem)
                folder = name
        elif kind == "raid" and re.search(r"\d+s$", stem):
            # Cactbot uses R1S through R12S where the local fight tags use M1S through
            # M12S.
            name = re.sub(r"(?i)^r(\d+)s$", r"M\1S", stem.upper())
            difficulty = "Savage"
            folder = name
        elif kind == "trial" and stem.endswith("-ex"):
            tag = _TRIAL_STEM_TO_TAG.get(stem)
            if tag is not None:
                difficulty, name = "Extreme", tag
            else:
                difficulty, name = "Extreme", _titleize(stem[:-3]) + " EX"
            folder = name
        else:
            continue
        key = (difficulty, expansion, name)
        if key in seen:
            continue
        seen.add(key)
        out.append(_entry(difficulty, expansion, name, folder, False))
    return out


_SMALL_WORDS = {"a", "an", "and", "in", "of", "on", "the", "to"}


def _titleize(stem: str) -> str:
    # Preserve small words and apostrophes that str.title would capitalize.
    words = re.sub(r"[_\-]+", " ", stem).strip().split()
    return " ".join(
        w.lower() if i and w.lower() in _SMALL_WORDS else w.capitalize()
        for i, w in enumerate(words)
    )


def load_catalog(fight_tree: FightTree, known_tags: set[str], cache_path: Path) -> list[dict]:
    """Merge cached cactbot fights with the offline base, falling back to the base on
    failure.
    """
    catalog = build_offline(fight_tree, known_tags)
    try:
        if cache_path.exists():
            extra = json.loads(cache_path.read_text(encoding="utf-8"))
            have = {(e["difficulty"], e["name"]) for e in catalog}
            for e in extra:
                # The picker requires all these fields, even in a hand edited cache.
                if not isinstance(e, dict) or not all(
                    k in e and isinstance(e[k], str) for k in
                    ("difficulty", "expansion", "name", "folder_name")
                ) or not isinstance(e.get("has_triggers"), bool):
                    continue
                # Drop obsolete cached R tags when the corresponding M tag is known.
                rn = (re.match(r"(?i)^R(\d+)S$", e["folder_name"])
                      or re.match(r"(?i)^R(\d+)S$", e["name"]))
                if rn and ("Savage", f"M{rn.group(1)}S") in have:
                    continue
                if (e.get("difficulty"), e.get("name")) not in have and e.get("difficulty") in _DIFFICULTIES:
                    catalog.append(e)
                    have.add((e["difficulty"], e["name"]))
    except Exception as e:  # noqa: BLE001
        log_drop("fight-catalog", f"cache merge skipped: {e!r}")
    return catalog


# Allow one refresh at a time and reuse the cache while it is fresh.
_REFRESH_RUNNING = threading.Event()
_CACHE_FRESH_S = 3600
# Limit the size of the GitHub tree response.
_TREE_MAX_BYTES = 16_000_000
# Enforce stall and total deadlines outside the read. Socket timeouts reset on each
# byte, so a trickling response could otherwise block refresh indefinitely.
_TREE_STALL_S = 15
_TREE_DEADLINE_S = 60


def _unblock_reader(resp) -> None:
    """Try to shut down the socket without waiting for the reader to release its buffer
    lock.
    """
    try:
        resp.fp.raw._sock.shutdown(socket.SHUT_RDWR)
    except Exception:  # noqa: BLE001
        pass


def refresh_from_cactbot_async(cache_path: Path) -> None:
    """Refresh the cached catalog in the background and log failures."""
    if _REFRESH_RUNNING.is_set():
        return
    try:
        if cache_path.exists() and time.time() - cache_path.stat().st_mtime < _CACHE_FRESH_S:
            return
    except OSError:
        pass
    _REFRESH_RUNNING.set()

    def _worker() -> None:
        try:
            req = urllib.request.Request(
                _CACTBOT_TREE_API, headers={"User-Agent": "NyaaTriggers"})
            deadline = time.monotonic() + _TREE_DEADLINE_S
            with open_response(req, 20, min(deadline, time.monotonic() + _TREE_STALL_S)) as resp:
                # Read in a helper so the watchdog can enforce both deadlines.
                done = threading.Event()
                progress = [0]
                reader_error = [None]
                raw = bytearray()

                def _reader() -> None:
                    try:
                        read_chunk = getattr(resp, "read1", resp.read)
                        while True:
                            chunk = read_chunk(1 << 16)
                            if not chunk:
                                break
                            raw.extend(chunk)
                            progress[0] = len(raw)
                            if len(raw) > _TREE_MAX_BYTES:
                                raise ValueError("tree response too large")
                    except BaseException as exc:
                        reader_error[0] = exc
                    finally:
                        done.set()

                threading.Thread(target=_reader, daemon=True).start()
                last_seen = progress[0]
                last_change = time.monotonic()
                while not done.wait(timeout=min(_TREE_STALL_S, max(0.0, deadline - time.monotonic()))):
                    now = time.monotonic()
                    if progress[0] == last_seen or now > deadline:
                        # Shut down the socket to wake the reader. Closing the response
                        # would wait for its read lock.
                        _unblock_reader(resp)
                        # Report a stall only after the full quiet window has elapsed.
                        if now - last_change >= _TREE_STALL_S:
                            raise TimeoutError(
                                f"cactbot tree fetch stalled, no new bytes for {_TREE_STALL_S} seconds")
                        raise TimeoutError("cactbot tree fetch timed out after 60 s")
                    last_seen = progress[0]
                    last_change = now
                if reader_error[0]:
                    raise reader_error[0]
            body = json.loads(raw)
            tree = body.get("tree") if isinstance(body, dict) else None
            if tree is None:
                # An API error can still have a valid JSON body.
                log_drop("fight-catalog",
                         "cactbot tree refresh: response holds no tree: "
                         f"{str(body)[:200]}")
                return
            if body.get("truncated"):
                log_drop("fight-catalog",
                         "cactbot tree refresh: GitHub truncated the tree, "
                         "cache may be incomplete")
            paths = [t.get("path", "") for t in tree if t.get("type") == "blob"]
            entries = parse_cactbot_paths(paths)
            if entries:
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                tmp = cache_path.with_suffix(".tmp")
                with open(tmp, "w", encoding="utf-8") as f:
                    f.write(json.dumps(entries, indent=2))
                    # Flush the data before replacing the cache file.
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, cache_path)
        except Exception as e:  # noqa: BLE001
            log_drop("fight-catalog", f"cactbot tree refresh failed: {e!r}")
        finally:
            _REFRESH_RUNNING.clear()
    try:
        threading.Thread(target=_worker, daemon=True).start()
    except Exception as e:  # noqa: BLE001
        # Allow another refresh if the thread fails to start.
        _REFRESH_RUNNING.clear()
        log_drop("fight-catalog", f"cactbot tree refresh could not start: {e!r}")


class FightPickerDialog(QDialog):
    """Searchable fight picker. selected_folder is the chosen folder, an empty string for
    uncategorised, or None on cancellation.
    """

    _UNCATEGORISED = "\x00uncategorised"

    def __init__(self, catalog: list[dict], parent=None,
                 title: "str | None" = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(title or _("Choose a fight"))
        self.resize(460, 560)
        self._chosen: str | None = None

        lay = QVBoxLayout(self)
        lay.addWidget(QLabel(_("Pick a fight to make/use a folder for, or choose Uncategorised.")))
        self._search = QLineEdit()
        self._search.setPlaceholderText(_("Search fights..."))
        self._search.setClearButtonEnabled(True)
        self._search.textChanged.connect(self._filter)
        lay.addWidget(self._search)

        self._tree = QTreeWidget()
        self._tree.setHeaderHidden(True)
        self._tree.itemDoubleClicked.connect(lambda *_: self._accept_if_leaf())
        lay.addWidget(self._tree, 1)

        self._populate(catalog)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._on_ok)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

    def _populate(self, catalog: list[dict]) -> None:
        unc = QTreeWidgetItem([_("Uncategorised (no folder)")])
        unc.setData(0, Qt.ItemDataRole.UserRole, self._UNCATEGORISED)
        self._tree.addTopLevelItem(unc)

        for difficulty in _DIFFICULTIES:
            diff_entries = [e for e in catalog if e["difficulty"] == difficulty]
            if not diff_entries:
                continue
            diff_item = QTreeWidgetItem([f"▶ {_(difficulty)}"])
            diff_item.setData(0, Qt.ItemDataRole.UserRole, None)   # group rows are not pickable targets
            self._tree.addTopLevelItem(diff_item)
            by_exp: dict[str, list[dict]] = {}
            for e in diff_entries:
                by_exp.setdefault(e["expansion"], []).append(e)
            for expansion in sorted(by_exp):
                exp_item = QTreeWidgetItem([_(expansion)])
                exp_item.setData(0, Qt.ItemDataRole.UserRole, None)
                diff_item.addChild(exp_item)
                for e in sorted(by_exp[expansion], key=lambda x: x["name"]):
                    label = e["name"] + ("  ✓" if e["has_triggers"] else "")
                    leaf = QTreeWidgetItem([label])
                    leaf.setData(0, Qt.ItemDataRole.UserRole, e["folder_name"])
                    exp_item.addChild(leaf)

    def _filter(self, text: str) -> None:
        q = text.strip().lower()
        for i in range(self._tree.topLevelItemCount()):
            self._filter_item(self._tree.topLevelItem(i), q)

    def _filter_item(self, item: QTreeWidgetItem, q: str, force: bool = False) -> bool:
        target = item.data(0, Qt.ItemDataRole.UserRole)
        self_match = force or (not q) or (q in item.text(0).lower())
        any_child_visible = False
        for i in range(item.childCount()):
            any_child_visible = self._filter_item(item.child(i), q, force or self_match) or any_child_visible
        visible = self_match or any_child_visible
        if target == self._UNCATEGORISED:
            visible = True
        item.setHidden(not visible)
        if q and any_child_visible:
            item.setExpanded(True)
        return visible

    def _selected_target(self) -> str | None:
        item = self._tree.currentItem()
        return item.data(0, Qt.ItemDataRole.UserRole) if item else None

    def _accept_if_leaf(self) -> None:
        if self._selected_target() is not None:
            self._on_ok()

    def _on_ok(self) -> None:
        target = self._selected_target()
        if target is None:
            return
        self._chosen = "" if target == self._UNCATEGORISED else target
        self.accept()

    def selected_folder(self) -> str | None:
        return self._chosen
