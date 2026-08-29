#!/usr/bin/env python3
"""Catalog of FFXIV endgame fights, Savage / Ultimate / Extreme, and a picker
dialog, for the "Move to..." / "New folder for a fight" workflow.

Offline base comes from NyaaTriggers' own fight list, so the picker works
without a network. Cactbot's raidboss data tree augments it, fetched once via
the GitHub tree API, cached to disk, failures logged to the drop log.

A catalog entry is a dict {difficulty, expansion, name, folder_name, has_triggers}.
- difficulty - "Savage" | "Ultimate" | "Extreme"
- expansion - "Dawntrail" | "Endwalker" | ... | "Other"
- name - human label shown in the picker
- folder_name - folder to create / move into, matches trigger.fight. For known
               fights this is the NyaaTriggers tag so membership lines up
- has_triggers - True if NyaaTriggers already holds triggers for it
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

from drop_log import log_drop
from locale_util import _, N_

FightTree = list[tuple[str, list[tuple[str, list[str]]]]]

_DIFFICULTIES = (N_("Savage"), N_("Ultimate"), N_("Extreme"))

# cactbot data folder prefix -> expansion display name
_EXPANSION = {
    "02-arr": N_("A Realm Reborn"), "03-hw": N_("Heavensward"),
    "04-sb": N_("Stormblood"), "05-shb": N_("Shadowbringers"),
    "06-ew": N_("Endwalker"), "07-dt": N_("Dawntrail"),
}

# Ultimates aren't split by expansion in the fight tree. Map tag -> expansion and full name.
_ULTIMATE_INFO = {
    "UCoB": ("Stormblood",      "The Unending Coil of Bahamut"),
    "UwU":  ("Stormblood",      "The Weapon's Refrain"),
    "TEA":  ("Shadowbringers",  "The Epic of Alexander"),
    "DSR":  ("Endwalker",       "Dragonsong's Reprise"),
    "TOP":  ("Endwalker",       "The Omega Protocol"),
    "FRU":  ("Dawntrail",       "Futures Rewritten"),
    "UMAD": ("Dawntrail",       "Dancing Mad"),
}

# cactbot ultimate data file stem -> NyaaTriggers fight tag. Same role as the
# rNs -> MNS savage rename in parse_cactbot_paths. Without it the picker lists
# a second copy of the fight under a folder name no trigger's fight tag has.
_ULTIMATE_STEM_TO_TAG = {
    "unending_coil_ultimate":       "UCoB",
    "ultima_weapon_ultimate":       "UwU",
    "the_epic_of_alexander":        "TEA",
    "dragonsongs_reprise_ultimate": "DSR",
    "the_omega_protocol":           "TOP",
    "futures_rewritten":            "FRU",
    "dancing_mad":                  "UMAD",
}

# cactbot extreme trial data file stem to NyaaTriggers fight tag. Same role as
# _ULTIMATE_STEM_TO_TAG. Without it the picker lists a second copy of the fight
# under a folder name no trigger's fight tag has.
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
    """Network-free base built from NyaaTriggers' own fight tree.

    `fight_tree` is main_window._FIGHT_TREE, shaped category -> expansion -> tag lists.
    `known_tags` are fight tags that currently have triggers.
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
    """Derive Savage/Ultimate/Extreme entries from cactbot data file paths such as
    `ui/raidboss/data/07-dt/ultimate/futures_rewritten.ts`."""
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
                # Land on the offline entry's name and folder so the merge
                # dedupes instead of doubling the fight.
                difficulty, name = "Ultimate", _ULTIMATE_INFO[tag][1]
                folder = tag
            else:
                difficulty, name = "Ultimate", _titleize(stem)
                folder = name
        elif kind == "raid" and re.search(r"\d+s$", stem):     # r1s, p8s, e12s...
            # cactbot names the Arcadion tier r1s..r12s but NyaaTriggers' own
            # tree and its trigger.fight tags use M1S..M12S. Without the mapping
            # the picker shows both "M4S" and a duplicate "R4S" whose folder
            # never matches any trigger's fight tag.
            name = re.sub(r"(?i)^r(\d+)s$", r"M\1S", stem.upper())
            difficulty = "Savage"
            folder = name
        elif kind == "trial" and stem.endswith("-ex"):
            tag = _TRIAL_STEM_TO_TAG.get(stem)
            if tag is not None:
                # Land on the offline entry's name and folder so the merge
                # dedupes instead of doubling the fight.
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
    # Not plain str.title. That uppercases small words and mangles apostrophes,
    # "alexander's" -> "Alexander'S".
    words = re.sub(r"[_\-]+", " ", stem).strip().split()
    return " ".join(
        w.lower() if i and w.lower() in _SMALL_WORDS else w.capitalize()
        for i, w in enumerate(words)
    )


def load_catalog(fight_tree: FightTree, known_tags: set[str], cache_path: Path) -> list[dict]:
    """Offline base merged with any cached cactbot-derived fights. Always hands
    back a usable list. Never raises."""
    catalog = build_offline(fight_tree, known_tags)
    try:
        if cache_path.exists():
            extra = json.loads(cache_path.read_text(encoding="utf-8"))
            have = {(e["difficulty"], e["name"]) for e in catalog}
            for e in extra:
                # Structural guard. _populate later subscripts expansion/name/
                # folder_name/has_triggers directly, so a hand-edited or partial
                # cache entry missing a field would otherwise crash the
                # FightPickerDialog __init__ with a KeyError.
                if not isinstance(e, dict) or not all(
                    k in e and isinstance(e[k], str) for k in
                    ("difficulty", "expansion", "name", "folder_name")
                ) or not isinstance(e.get("has_triggers"), bool):
                    continue
                # A cache written before the rNs -> MNS rename can hold R1S
                # to R12S rows. Next to the M entries they show as phantom R
                # folders, so drop a cached R row when the M entry is known.
                rn = (re.match(r"(?i)^R(\d+)S$", e["folder_name"])
                      or re.match(r"(?i)^R(\d+)S$", e["name"]))
                if rn and ("Savage", f"M{rn.group(1)}S") in have:
                    continue
                if (e.get("difficulty"), e.get("name")) not in have and e.get("difficulty") in _DIFFICULTIES:
                    catalog.append(e)
    except Exception as e:  # noqa: BLE001
        log_drop("fight-catalog", f"cache merge skipped: {e!r}")
    return catalog


# One in-flight refresh at a time. Repeat calls, the picker fires one per
# dialog open, must not stack threads or re-hit the API while the cache is warm.
_REFRESH_RUNNING = threading.Event()
_CACHE_FRESH_S = 3600
# Read cap for the GitHub tree fetch. The recursive cactbot tree is a few MB.
# Anything past this is a substituted or hostile body, not the real API.
_TREE_MAX_BYTES = 16_000_000
# Watchdog timing for the tree fetch. The read loop runs on a daemon helper
# while the worker enforces a stall window and a total deadline from outside
# the read. The socket timeout is per recv and resets on every received byte,
# so a trickling peer would otherwise hold the read open forever and latch
# refresh off for the session.
_TREE_STALL_S = 15
_TREE_DEADLINE_S = 60


def _unblock_reader(resp) -> None:
    """Shut the underlying socket down so a read parked in another thread
    wakes at once. A plain resp.close from this side would block on the
    buffer lock the parked read still holds. Best effort, the reader is a
    daemon thread either way."""
    try:
        resp.fp.raw._sock.shutdown(socket.SHUT_RDWR)
    except Exception:  # noqa: BLE001
        pass


def refresh_from_cactbot_async(cache_path: Path) -> None:
    """Fetch cactbot's data tree in the background and cache the derived
    entries for next time. Any failure lands in the drop log, never raised."""
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
            with urllib.request.urlopen(req, timeout=20) as resp:
                # Read loop on a daemon helper, watchdog here. The stall
                # window and the total deadline are enforced from outside the
                # read because a trickling peer can hold one resp.read open
                # forever. Same guard main.py runs for its downloads.
                done = threading.Event()
                progress = [0]
                reader_error = [None]
                raw = bytearray()

                def _reader() -> None:
                    try:
                        while True:
                            chunk = resp.read(1 << 16)
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
                deadline = time.monotonic() + _TREE_DEADLINE_S
                last_seen = progress[0]
                last_change = time.monotonic()
                while not done.wait(timeout=min(_TREE_STALL_S, max(0.0, deadline - time.monotonic()))):
                    now = time.monotonic()
                    if progress[0] == last_seen or now > deadline:
                        # Shut the connection down so the parked reader wakes
                        # instead of leaking. A plain resp.close here would
                        # block on the lock the parked read still holds.
                        _unblock_reader(resp)
                        # Same label rule as updater and install: the stall
                        # line only fits when the whole stall window really
                        # passed with no byte. A wake near the deadline after
                        # less than a full window of quiet is the deadline.
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
                # An error body, say a rate-limit 403, is still valid JSON.
                # Without a warning this wrote nothing and logged nothing.
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
                    # Same power-loss hole as _atomic_write_json: a rename can
                    # commit before the data does.
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
        # A failed start, say a shutting-down interpreter, must not leave
        # refresh latched off for the rest of the session.
        _REFRESH_RUNNING.clear()
        log_drop("fight-catalog", f"cactbot tree refresh could not start: {e!r}")


class FightPickerDialog(QDialog):
    """Searchable fight picker, difficulty -> expansion -> boss name, plus an
    "Uncategorised" option. selected_folder after exec returns the chosen
    folder name, "" for uncategorised, or None if cancelled."""

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

    # ------------------------------------------------------------------
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
            return   # a group header is selected, demand a fight or Uncategorised
        self._chosen = "" if target == self._UNCATEGORISED else target
        self.accept()

    def selected_folder(self) -> str | None:
        return self._chosen
