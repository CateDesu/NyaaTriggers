"""Triggers tab. Fight tree, trigger table, per-fight toggles and
callout edits. Mixin for MainWindow, all state rides on self.
"""

from pathlib import Path
import json
import os
import re
import shutil
import threading
import time
import urllib.request
import uuid

from PyQt6.QtCore import Qt, QSize, QTimer
from PyQt6.QtGui import QBrush, QColor
from PyQt6.QtWidgets import (
    QDialog, QInputDialog, QMenu, QTableWidgetItem, QTreeWidgetItem, QTreeWidgetItemIterator,
)

from trigger_engine import Trigger
from trigger_dialog import TriggerDialog
from tts import speak, set_readings
from locale_util import _, active_locale
from status_timer import StatusTimerRunner
from triggevent_bridge import TriggeventBridge
try:
    from triggernometry_bridge import TriggernometryBridge, has_packs as _tn_has_packs, \
        packs_dir as _tn_packs_dir, _log as _tn_log
except Exception:  # noqa: BLE001 - never block app load on the optional sidecar bridge
    TriggernometryBridge = None  # type: ignore
    _tn_has_packs = lambda: False  # noqa: E731
    _tn_packs_dir = None  # type: ignore
    _tn_log = lambda msg: None
import fight_catalog
import updater

import app_common as ac
from app_common import (
    _CALLOUTS_JA_MAX_BYTES, _CALLOUT_CLAIM_S, _C_EN, _C_FIGHT, _C_NAME, _C_RE, _C_TTS, _C_TYPE, _C_ZONE, _FIGHT_TREE, _GENERAL_TAB, _GUEST_CALLOUT_DEFER_MS, _GUEST_SEVERITY_RANK, _ITEM_ID_ROLE, _ITEM_TYPE_ROLE, _SECTION_ROLE, _TREE_FIGHTS, _VERSION, _as_strset, _atomic_write_json, _compile_phrase_patterns, _fsync_file, _next_bad_name, _repo_download_version, _watched_trigger_files,
)


class TriggersTabMixin:
    def _load_retired_ids(self) -> set[str]:
        """Ids we withdrew from triggers.json and want gone from clients too.

        Tolerates bare id strings as well as dict rows carrying an id and a
        reason, so the file can keep the why without a schema bump.
        """
        src = ac._REPO_RETIRED_FILE
        # Same version gate as the trigger override in _load_triggers. A stale
        # or stamp-less download must not shadow the bundled retired.json.
        if not (src.exists() and src != ac.RETIRED_FILE
                and _repo_download_version() == _VERSION):
            src = ac.RETIRED_FILE
        if not src.exists():
            return set()
        try:
            raw = json.loads(src.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return set()
        rows = raw.get("retired", []) if isinstance(raw, dict) else raw
        if not isinstance(rows, list):
            return set()
        out: set[str] = set()
        for row in rows:
            if isinstance(row, str):
                out.add(row)
            elif isinstance(row, dict) and isinstance(row.get("id"), str):
                out.add(row["id"])
        return out

    def _load_triggers(self) -> None:
        # Cleared on every reload. _handle_local_corrupt sets it True when the
        # file is unreadable, and while True _save_triggers refuses to write so
        # the backed-up original isn't atomically replaced with empty data.
        self._local_corrupt = False
        official: list[Trigger] = []
        # Prefer the downloaded repo set over the bundled one, but only while it
        # was fetched by THIS app version. A stale download must not shadow the
        # newer bundled set a self-update just installed. Purge it so a re-click
        # of Update Triggers starts clean. Keyed on the stamp alone, not the
        # triggers file, so partial crash states like a leftover retired file or
        # stamp clean up too. With no download the unlinks are no-ops.
        _override = ac._REPO_TRIGGERS_FILE
        if _repo_download_version() != _VERSION:
            for _p in (ac._REPO_TRIGGERS_FILE, ac._REPO_RETIRED_FILE, ac._REPO_TRIGGERS_VERSION):
                try:
                    _p.unlink(missing_ok=True)
                except OSError:
                    pass
        # Recheck the stamp instead of trusting the unlinks. A failed delete,
        # locked file, AV scan, would otherwise load the stale download as the
        # official set. Same direct check the retired gate above makes.
        _trig_src = (_override if (_override.exists() and _override != ac.TRIGGERS_FILE
                                   and _repo_download_version() == _VERSION)
                     else ac.TRIGGERS_FILE)
        # A corrupt downloaded override must not silently blank the official
        # set. Fall back to the pristine bundled file beside it before giving up.
        for _src in dict.fromkeys((_trig_src, ac.TRIGGERS_FILE)):
            if not _src.exists():
                continue
            try:
                data = json.loads(_src.read_text(encoding="utf-8"))
                # The override could be valid JSON but still not a list, a
                # hand edited {} or null. Treat that like a failed parse so
                # the bundled fallback above still runs, the whole point of
                # this loop.
                if not isinstance(data, list):
                    raise ValueError("not a trigger list")
                official = [Trigger.from_dict(d) for d in data if isinstance(d, dict)]
            except (OSError, ValueError, KeyError, TypeError) as exc:
                ac.log_drop("triggers", f"{_src.name} unreadable: {exc!r}")
                continue
            break
        self._retired_ids = self._load_retired_ids()
        # a retired id should already be gone from triggers.json but a stale
        # downloaded copy in _DATA_DIR could still carry it
        official = [t for t in official if t.id not in self._retired_ids]
        self._official_ids      = {t.id for t in official}
        # Pristine snapshot of the bundled state. It must NOT share objects with
        # the `official` list. The merge below mutates those objects, t.enabled
        # and so on, and puts them into self._triggers, so a shared reference
        # would make _save_triggers compare a trigger against itself. The
        # override diff always reads equal, toggles never persist, and a saved
        # {id, enabled} override is destroyed on the next save.
        self._official_triggers = {t.id: Trigger.from_dict(t.to_dict()) for t in official}

        local_triggers: list[Trigger] = []
        enabled_overrides: dict[str, bool] = {}
        self._deleted_ids = set()
        # Read and parse the local file once per load. An earlier shape read
        # it twice, triggers and deleted here, folders below, and a write
        # landing between the passes, an external editor or a save racing
        # the 30 s poll, mixed state from two different file versions.
        raw = None
        if ac.TRIGGERS_LOCAL_FILE.exists():
            try:
                raw = json.loads(ac.TRIGGERS_LOCAL_FILE.read_text(encoding="utf-8"))
            except (OSError, ValueError, KeyError, TypeError):
                self._handle_local_corrupt()
        # A file that's valid JSON but not a dict, say a bare list, would
        # make raw.get raise AttributeError out of __init__.
        if isinstance(raw, dict):
            trigs = raw.get("triggers")
            if not isinstance(trigs, list):
                # Valid JSON but a hand edited non-list, say {"triggers": 42}.
                # Treat it as empty, like the folders isinstance guard below.
                trigs = []
            for d in trigs:
                if not isinstance(d, dict):
                    continue
                # A slim {id, enabled} record is an on/off override of a
                # bundled trigger, written by _save_triggers when only
                # the toggle differs. It must not go through from_dict,
                # which would fabricate a default-bodied trigger.
                if d.get("id") and set(d.keys()) <= {"id", "enabled"}:
                    enabled_overrides[str(d["id"])] = bool(d.get("enabled", True))
                else:
                    local_triggers.append(Trigger.from_dict(d))
            # Tolerate null and non-str junk. A bare TypeError here
            # quarantines the whole file as corrupt, and a mixed-type
            # set makes sorted raise on every save. Ids are strings.
            self._deleted_ids = {x for x in _as_strset(raw.get("deleted"))
                                 if isinstance(x, str)}
        # Drop retired ids from the local set before anything else sees it. This keeps
        # them out of the merge below, where a local-only id would otherwise be
        # re-appended and survive its own removal, and because _save_triggers writes
        # only _local_ids it lets the next save rewrite the file without them. A slim
        # {id, enabled} override needs no filtering of its own. It only counts while the
        # id is still official, and retired ids are already gone from there. A stale
        # tombstone for a retired id can never match again, so clear those too.
        local_triggers = [t for t in local_triggers if t.id not in self._retired_ids]
        self._deleted_ids -= self._retired_ids
        self._local_ids = {t.id for t in local_triggers} | (
            set(enabled_overrides) & {t.id for t in official})

        local_by_id = {t.id: t for t in local_triggers}
        merged: list[Trigger] = []
        for t in official:
            if t.id in self._deleted_ids:
                continue
            loc = local_by_id.get(t.id)
            if loc is not None:
                # Legacy full local copies whose content still matches the
                # bundled trigger, differing only by 'enabled', collapse to a
                # toggle on the bundled content, so repo-side callout fixes
                # reach them. A copy that already diverges from the CURRENT
                # bundled content is kept verbatim. We can't tell a real user
                # edit from a bundled trigger that changed after the copy was
                # made, so we preserve it rather than risk discarding an edit.
                # New toggles never create a full copy since they persist as a
                # slim {id, enabled} record, so this only affects pre-existing
                # local files.
                ld, od = loc.to_dict(), t.to_dict()
                ld.pop("enabled", None)
                od.pop("enabled", None)
                if ld == od:
                    t.enabled = loc.enabled
                    merged.append(t)
                else:
                    merged.append(loc)   # genuinely edited, the local copy wins
                continue
            if t.id in enabled_overrides:
                t.enabled = enabled_overrides[t.id]
            merged.append(t)
        for t in local_triggers:
            if t.id not in self._official_ids:
                merged.append(t)
        self._triggers = merged

        self._folders = []
        if isinstance(raw, dict):
            folders = raw.get("folders", [])
            if isinstance(folders, list):
                # Keep only well-formed entries with str ids and names. The
                # tree build subscripts fo["name"]/fo["id"] during __init__,
                # so a bad entry from a hand-edit or an imported pack would
                # brick every launch, and a non-string name raises later in
                # the rename dialog and the Move to Folder menu.
                self._folders = [fo for fo in folders
                                 if isinstance(fo, dict)
                                 and isinstance(fo.get("id"), str)
                                 and isinstance(fo.get("name"), str)]

        # Re-baseline the hot-reload snapshot so every load path, startup,
        # import, repo update, the 30 s tick, leaves _triggers_mtime matching
        # what was just read from disk.
        self._triggers_mtime = self._trigger_files_stamp()
        self._refresh_table()

    def _handle_local_corrupt(self) -> None:
        """triggers.local.json could not be parsed. Back it up as a rotated .bad
        copy so the next save doesn't destroy the recoverable original and a
        second corruption doesn't overwrite the first, then warn the user. Sets
        _local_corrupt so _save_triggers pauses writes until the file is
        readable again. Mirrors _load_settings' corruption handling."""
        self._local_corrupt = True
        backup = _next_bad_name(ac.TRIGGERS_LOCAL_FILE)
        where = ""
        try:
            shutil.copy2(ac.TRIGGERS_LOCAL_FILE, backup)
            where = "\n\n" + _("A copy was kept at:\n{path}").format(path=backup)
        except OSError:
            pass
        ac.QMessageBox.warning(
            self, _("Triggers Unreadable"),
            _("Your local triggers file could not be read, so only the bundled "
              "triggers are shown. Edits are paused until the file is fixed or "
              "removed.") + where)

    def _save_triggers(self) -> None:
        if getattr(self, "_local_corrupt", False):
            # The local file was unreadable at load. Writing now would atomically
            # replace the backed-up corrupt original with empty data and lose any
            # recoverable content. Skip. The user was warned at load, and a fixed
            # file hot-reloads and clears the flag.
            return
        to_save = [t for t in self._triggers if t.id in self._local_ids]
        records: list[dict] = []
        for t in to_save:
            off = self._official_triggers.get(t.id)
            if off is not None:
                td, od = t.to_dict(), off.to_dict()
                td.pop("enabled", None)
                od.pop("enabled", None)
                if td == od:
                    # Content matches the bundled trigger. Persist only the
                    # toggle, or nothing at all if that matches too, so future
                    # bundled-trigger updates aren't masked by a stale copy.
                    if t.enabled != off.enabled:
                        records.append({"id": t.id, "enabled": t.enabled})
                    continue
            records.append(t.to_dict())
        try:
            # Built inside the try. sorted raises TypeError on a mixed-type
            # tombstone set, and that must degrade to the warning below rather
            # than raise out of every save for the rest of the session.
            data = {
                "triggers": records,
                "deleted":  sorted(self._deleted_ids),
                "folders":  self._folders,
            }
            _atomic_write_json(ac.TRIGGERS_LOCAL_FILE, data, indent=2)
        except (OSError, TypeError, ValueError) as exc:
            # A read-only install dir, say onedir under Program Files, must not
            # crash on every edit. Degrade to an in-memory-only change.
            self._warn_save_failed(_("triggers"), exc)

    @staticmethod
    def _tree_item_path(item) -> tuple:
        """Root-to-item chain of arrow-stripped labels. Expansion state keys
        on the path rather than the bare label because expansion headers
        repeat across categories. Dawntrail sits under four of them, and
        label keys re-expanded every namesake on each rebuild."""
        parts = []
        node = item
        while node is not None:
            parts.append(node.text(0).lstrip("▶▼ "))
            node = node.parent()
        return tuple(reversed(parts))

    def _refresh_tree(self) -> None:
        """Rebuild the left-hand fight tree, keeping selection and expansion."""
        cur = self._tree.currentItem()
        cur_fight     = cur.data(0, Qt.ItemDataRole.UserRole) if cur else ""
        cur_item_type = cur.data(0, _ITEM_TYPE_ROLE) if cur else None

        expanded: set[tuple] = set()
        it = QTreeWidgetItemIterator(self._tree)
        while it.value():
            item = it.value()
            if item.isExpanded():
                expanded.add(self._tree_item_path(item))
            it += 1

        # Repaint atomically. A bare clear and rebuild paints the empty tree for
        # one frame, a visible split-second collapse on every refresh.
        self._tree.setUpdatesEnabled(False)
        self._tree.blockSignals(True)
        self._tree.clear()

        # ── General, top-level leaf ──
        gen = QTreeWidgetItem([_(_GENERAL_TAB)])
        gen.setData(0, Qt.ItemDataRole.UserRole, "")
        bold = gen.font(0); bold.setBold(True); gen.setFont(0, bold)
        self._tree.addTopLevelItem(gen)

        # ── Predefined hierarchy, category -> expansion -> fight ──
        for cat, exps in _FIGHT_TREE:
            ci = QTreeWidgetItem([f"▶ {_(cat)}"])
            ci.setData(0, Qt.ItemDataRole.UserRole, None)
            ci.setFlags(Qt.ItemFlag.ItemIsEnabled)   # not selectable
            f = ci.font(0); f.setBold(True); ci.setFont(0, f)
            ci.setSizeHint(0, QSize(0, 28))
            self._tree.addTopLevelItem(ci)

            for exp, fights in exps:
                ei = QTreeWidgetItem([f"▶ {_(exp)}"])
                ei.setData(0, Qt.ItemDataRole.UserRole, None)
                ei.setFlags(Qt.ItemFlag.ItemIsEnabled)   # not selectable
                f = ei.font(0); f.setItalic(True); ei.setFont(0, f)
                ei.setSizeHint(0, QSize(0, 26))
                ci.addChild(ei)

                for fight in fights:
                    fi = QTreeWidgetItem([fight])
                    fi.setData(0, Qt.ItemDataRole.UserRole, fight)
                    fi.setSizeHint(0, QSize(0, 22))
                    ei.addChild(fi)

        # ── TBD, official fights with no slot in the curated tree, sort later ──
        self._build_tbd_tree_section()

        # ── Unsorted section, bottom, visually detached ──
        self._build_custom_tree_section()

        # Restore expansion. Headers carry a manual ▶/▼ arrow in their text, so save
        # and match on the arrow-stripped label path. A rebuild resets arrows to ▶, so
        # matching with the arrow would always fail. Re-point re-expanded arrows.
        # No animation for this: it is a programmatic replay after every rebuild,
        # and animating it would slide every section back open at once.
        self._tree.setAnimated(False)
        it = QTreeWidgetItemIterator(self._tree)
        while it.value():
            item = it.value()
            if self._tree_item_path(item) in expanded:
                item.setExpanded(True)
                txt = item.text(0)
                if txt.startswith("▶ "):
                    item.setText(0, "▼ " + txt[2:])
            it += 1
        self._tree.setAnimated(True)

        # Restore previous selection
        self._tree.blockSignals(False)
        if not self._restore_tree_selection(cur_fight, cur_item_type):
            self._tree.setCurrentItem(gen)
        self._tree.setUpdatesEnabled(True)   # single repaint, fully built + expanded

    def _restore_tree_selection(self, fight: str | None, item_type: str | None = None) -> bool:
        """Select the tree leaf whose UserRole and item_type match. Returns True on success."""
        if fight is None:
            return False
        it = QTreeWidgetItemIterator(self._tree)
        while it.value():
            item = it.value()
            if item.data(0, Qt.ItemDataRole.UserRole) == fight:
                if item_type is None or item.data(0, _ITEM_TYPE_ROLE) == item_type:
                    self._tree.setCurrentItem(item)
                    return True
            it += 1
        return False

    def _build_tbd_tree_section(self) -> None:
        """Group triggers whose fight tag has no slot in the curated tree under
        one TBD node so nothing is orphaned. Zone-locked customs sort to their
        zone here rather than Unsorted. No data moves. Each leaf filters to its
        real fight tag."""
        tbd: dict[str, int] = {}
        for t in self._triggers:
            f = t.fight or ""
            if not f or f in _TREE_FIGHTS:
                continue
            is_official     = t.id in self._official_ids
            is_zoned_custom = (t.id in self._local_ids
                               and t.id not in self._official_ids
                               and bool(t.zone_regex.strip()))
            if is_official or is_zoned_custom:
                tbd[f] = tbd.get(f, 0) + 1
        if not tbd:
            return
        th = QTreeWidgetItem([f"▶ {_('TBD')}"])
        th.setData(0, Qt.ItemDataRole.UserRole, None)
        th.setFlags(Qt.ItemFlag.ItemIsEnabled)   # not selectable. Click toggles expand
        f0 = th.font(0); f0.setBold(True); th.setFont(0, f0)
        th.setSizeHint(0, QSize(0, 28))
        self._tree.addTopLevelItem(th)
        for fight in sorted(tbd):
            fi = QTreeWidgetItem([f"{fight}  ({tbd[fight]})"])
            fi.setData(0, Qt.ItemDataRole.UserRole, fight)
            fi.setSizeHint(0, QSize(0, 22))
            th.addChild(fi)

    def _build_custom_tree_section(self) -> None:
        """Append the Unsorted block at the bottom of the fight tree. Holds
        custom triggers with no zone lock. A zone-locked custom with a fight tag
        auto-sorts to its zone, curated fight or TBD, never here. A zone-locked
        custom with NO fight tag sorts nowhere since every leaf keys on the tag,
        so it lands here too. Filtered out, it would be invisible in the tree.
        An unzoned custom whose tag the curated tree covers already shows under
        that fight's leaf, so it is left out here instead of listed twice."""
        custom = [t for t in self._triggers
                  if t.id in self._local_ids and t.id not in self._official_ids
                  and (not t.zone_regex.strip() or not t.fight)
                  # Unzoned with a curated tag lives under the fight's leaf.
                  and (t.zone_regex.strip() or t.fight not in _TREE_FIGHTS)]

        # Spacer between the official tree and Unsorted
        sep = QTreeWidgetItem([""])
        sep.setFlags(Qt.ItemFlag.NoItemFlags)
        sep.setSizeHint(0, QSize(0, 14))
        sep.setData(0, _ITEM_TYPE_ROLE, "sep")
        self._tree.addTopLevelItem(sep)

        # Custom section header. Starts collapsed. _refresh_tree's restore loop
        # re-expands it if it was open before the rebuild.
        ci = QTreeWidgetItem([f"▶ {_('Unsorted')}"])
        ci.setData(0, Qt.ItemDataRole.UserRole, None)
        ci.setData(0, _ITEM_TYPE_ROLE, "custom_hdr")
        ci.setFlags(Qt.ItemFlag.ItemIsEnabled)
        f = ci.font(0); f.setBold(True); ci.setFont(0, f)
        ci.setSizeHint(0, QSize(0, 28))
        self._tree.addTopLevelItem(ci)

        # Auto-groups, fight tags and General, used by pure-custom triggers
        folder_names = {fo["name"] for fo in self._folders}
        grouped: dict[str, list] = {}
        for t in custom:
            key = t.fight if t.fight else _GENERAL_TAB
            if key not in folder_names:
                grouped.setdefault(key, [])

        for fight_key in sorted(grouped):
            fi = QTreeWidgetItem([_(fight_key) if fight_key == _GENERAL_TAB else fight_key])
            fi.setData(0, Qt.ItemDataRole.UserRole, "" if fight_key == _GENERAL_TAB else fight_key)
            fi.setData(0, _ITEM_TYPE_ROLE, "custom_group")
            # Default item flags include ItemIsEditable and the tree has no
            # NoEditTriggers, so a double-click opened an in-place editor
            # whose "rename" the next refresh silently discarded.
            fi.setFlags(fi.flags() & ~Qt.ItemFlag.ItemIsEditable)
            fi.setSizeHint(0, QSize(0, 22))
            ci.addChild(fi)

        # User folders, top-level first, then nested
        for fo in self._folders:
            if fo.get("parent_id") is None:
                self._add_folder_node(fo, ci)

    def _add_folder_node(self, folder: dict, parent: QTreeWidgetItem,
                         visited: "set[str] | None" = None) -> None:
        fi = QTreeWidgetItem([f"▶ {folder['name']}"])
        fi.setData(0, Qt.ItemDataRole.UserRole, folder["name"])
        fi.setData(0, _ITEM_TYPE_ROLE, "folder")
        fi.setData(0, _ITEM_ID_ROLE, folder["id"])
        fi.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
        fi.setSizeHint(0, QSize(0, 22))
        parent.addChild(fi)
        # triggers.local.json is hand editable. Two folders sharing one id,
        # with a folder's parent_id naming that id, would recurse forever.
        # Mark each id as entered and skip a child already seen. First
        # occurrence wins.
        if visited is None:
            visited = set()
        visited.add(folder["id"])
        for child in self._folders:
            if child.get("parent_id") == folder["id"] \
                    and child.get("id") not in visited:
                self._add_folder_node(child, fi, visited)

    def _on_tree_context_menu(self, pos) -> None:
        item = self._tree.itemAt(pos)
        item_type = item.data(0, _ITEM_TYPE_ROLE) if item else None

        menu = QMenu(self._tree)

        if item_type == "custom_hdr":
            new_folder = menu.addAction(_("New Folder"))
            new_fight  = menu.addAction(_("New folder for a fight..."))
            chosen = menu.exec(self._tree.viewport().mapToGlobal(pos))
            if chosen is new_folder:
                self._create_folder(parent_id=None)
            elif chosen is new_fight:
                self._create_fight_folder()

        elif item_type == "folder":
            folder_id = item.data(0, _ITEM_ID_ROLE)
            new_sub   = menu.addAction(_("New Subfolder"))
            menu.addSeparator()
            rename    = menu.addAction(_("Rename"))
            delete    = menu.addAction(_("Delete Folder"))
            chosen = menu.exec(self._tree.viewport().mapToGlobal(pos))
            if chosen is new_sub:
                self._create_folder(parent_id=folder_id)
            elif chosen is rename:
                self._rename_folder(folder_id)
            elif chosen is delete:
                self._delete_folder(folder_id)

        elif item_type == "custom_group":
            new_sub   = menu.addAction(_("New Subfolder in Unsorted"))
            new_fight = menu.addAction(_("New folder for a fight..."))
            chosen = menu.exec(self._tree.viewport().mapToGlobal(pos))
            if chosen is new_sub:
                self._create_folder(parent_id=None)
            elif chosen is new_fight:
                self._create_fight_folder()

        else:
            # Official tree node. Offer to create a folder in Custom
            new_folder = menu.addAction(_("New Unsorted Folder"))
            new_fight  = menu.addAction(_("New folder for a fight..."))
            chosen = menu.exec(self._tree.viewport().mapToGlobal(pos))
            if chosen is new_folder:
                self._create_folder(parent_id=None)
            elif chosen is new_fight:
                self._create_fight_folder()

    def _create_folder(self, parent_id: str | None) -> None:
        name, ok = QInputDialog.getText(self, _("New Folder"), _("Folder name:"))
        if not ok or not name.strip():
            return
        name = name.strip()
        # Membership and delete matching are keyed on the name alone, see
        # _delete_folder, so a duplicate name would couple the two folders.
        if any(f["name"] == name for f in self._folders):
            ac.QMessageBox.information(
                self, _("New Folder"),
                _("A folder named '{name}' already exists.").format(name=name))
            return
        self._folders.append({"id": str(uuid.uuid4()), "name": name, "parent_id": parent_id})
        self._save_triggers()
        self._refresh_tree()

    def _create_fight_folder(self) -> None:
        """Pick a fight and create a top-level folder named after it. Same picker
        as "Move to..."."""
        folder = self._pick_fight_folder()
        if not folder:   # cancelled or "Uncategorised"
            return
        if any(f["name"] == folder for f in self._folders):
            ac.QMessageBox.information(
                self, _("New Folder"),
                _("A folder named '{name}' already exists.").format(name=folder))
            return       # already exists, see _create_folder for the name coupling
        self._folders.append({"id": str(uuid.uuid4()), "name": folder, "parent_id": None})
        self._save_triggers()
        self._refresh_tree()

    def _rename_folder(self, folder_id: str) -> None:
        folder = next((f for f in self._folders if f["id"] == folder_id), None)
        if folder is None:
            return
        name, ok = QInputDialog.getText(self, _("Rename Folder"), _("New name:"), text=folder["name"])
        if not ok or not name.strip():
            return
        name = name.strip()
        # Same name coupling as _create_folder, renaming onto an existing
        # folder would merge their membership for _delete_folder.
        if name != folder["name"] and any(f["name"] == name for f in self._folders):
            ac.QMessageBox.information(
                self, _("Rename Folder"),
                _("A folder named '{name}' already exists.").format(name=name))
            return
        old_name = folder["name"]
        folder["name"] = name
        # Membership is keyed on trigger.fight == folder name, see
        # _delete_folder, so a rename would orphan every trigger inside.
        # Retag the local-only ones with it.
        for t in self._triggers:
            if t.fight == old_name and t.id in self._local_ids \
                    and t.id not in self._official_ids:
                t.fight = folder["name"]
        self._save_triggers()
        self._refresh_tree()
        self._refresh_table()

    def _delete_folder(self, folder_id: str) -> None:
        folder = next((f for f in self._folders if f["id"] == folder_id), None)
        if folder is None:
            return
        # Collect folder + all descendants. seen guards a duplicated folder
        # id, the same hand edited file hazard as _add_folder_node.
        def _collect(fid: str, seen: "set[str]") -> list[str]:
            ids = [fid]
            seen.add(fid)
            for ch in self._folders:
                cid = ch.get("id")
                if ch.get("parent_id") == fid and cid not in seen:
                    ids.extend(_collect(cid, seen))
            return ids
        to_remove = set(_collect(folder_id, set()))
        # Folder membership is keyed on trigger.fight == folder.name, so delete
        # the local-only triggers tagged with any removed folder name. Official
        # triggers are never destroyed by a folder delete.
        names_to_remove = {f["name"] for f in self._folders if f["id"] in to_remove}
        victims = [
            t for t in self._triggers
            if t.fight in names_to_remove
            and t.id in self._local_ids and t.id not in self._official_ids
        ]
        n = len(victims)
        answer = ac.QMessageBox.question(
            self, _("Delete Folder"),
            (_('Delete "{name}" and all its subfolders?\nThis will also delete {count} trigger(s) inside.')
             .format(name=folder["name"], count=n))
            if n else
            (_('Delete "{name}" and all its subfolders?\nThere are no triggers inside.')
             .format(name=folder["name"])),
        )
        if answer != ac.QMessageBox.StandardButton.Yes:
            return
        victim_ids = {t.id for t in victims}
        self._folders = [f for f in self._folders if f["id"] not in to_remove]
        self._triggers = [t for t in self._triggers if t.id not in victim_ids]
        self._local_ids -= victim_ids
        self._save_triggers()
        self._refresh_tree()
        self._refresh_table()

    def _on_tree_item_clicked(self, item: QTreeWidgetItem, _col: int) -> None:
        """Toggle expand/collapse on header items with a single click."""
        if not (item.flags() & Qt.ItemFlag.ItemIsSelectable):
            expanding = not item.isExpanded()
            item.setExpanded(expanding)
            self._set_tree_arrow(item, expanding)
            if not expanding:
                # Also collapse nested sub-sections, so re-expanding shows them closed.
                self._collapse_tree_descendants(item)

    @staticmethod
    def _set_tree_arrow(item: QTreeWidgetItem, expanded: bool) -> None:
        text = item.text(0)
        if expanded and text.startswith("▶ "):
            item.setText(0, "▼ " + text[2:])
        elif not expanded and text.startswith("▼ "):
            item.setText(0, "▶ " + text[2:])

    def _collapse_tree_descendants(self, item: QTreeWidgetItem) -> None:
        for i in range(item.childCount()):
            child = item.child(i)
            child.setExpanded(False)
            self._set_tree_arrow(child, False)
            self._collapse_tree_descendants(child)

    def _apply_tab_filter(self, item: QTreeWidgetItem | None = None) -> None:
        if item is None:
            item = self._tree.currentItem()
        fight      = item.data(0, Qt.ItemDataRole.UserRole) if item else ""
        item_type  = item.data(0, _ITEM_TYPE_ROLE) if item else None
        local_only = item_type in ("folder", "custom_group")
        query = self._search_edit.text().strip().lower()
        # Cells display the localized name/callout. The English originals stay
        # searchable too, so either language finds a native trigger.
        trigger_map = {t.id: t for t in self._triggers} if query else {}
        matches = 0
        header_rows: list[tuple[int, str]] = []
        section_has = {"general": False, "dot": False, "local": False,
                       "engine": False, "triggernometry": False}
        # Batch the per-row visibility flips. Hundreds of setRowHidden calls
        # each queue a repaint, the visible stutter on every tree click.
        # One repaint at the end instead, same trick as _refresh_tree.
        self._table.setUpdatesEnabled(False)
        for row in range(self._table.rowCount()):
            en  = self._table.item(row, _C_EN)
            tid = en.data(Qt.ItemDataRole.UserRole) if en else None
            if isinstance(tid, str) and tid.startswith("__hdr__:"):
                header_rows.append((row, tid.split(":", 1)[1]))
                continue
            fi  = self._table.item(row, _C_FIGHT)
            fv  = fi.text() if fi else ""
            section = en.data(_SECTION_ROLE) if en else None
            if section not in section_has:
                section = "engine" if self._is_engine_key(tid) else "local"
            if query:
                name    = self._table.item(row, _C_NAME).text() if self._table.item(row, _C_NAME) else ""
                typ     = self._table.item(row, _C_TYPE).text() if self._table.item(row, _C_TYPE) else ""
                ability = self._table.item(row, _C_RE).text()   if self._table.item(row, _C_RE)   else ""
                tts     = self._table.item(row, _C_TTS).text()  if self._table.item(row, _C_TTS)  else ""
                hidden  = not any(query in c.lower() for c in (name, fv, typ, ability, tts))
                if hidden:
                    t = trigger_map.get(tid)
                    if t is not None:
                        hidden = not any(query in c.lower() for c in (t.name, t.tts_text))
                if not hidden:
                    matches += 1
            else:
                hidden = fv != (fight or "")
                if not hidden and local_only:
                    hidden = tid not in self._local_ids or tid in self._official_ids
                elif not hidden and not local_only and fight == "":
                    hidden = tid in self._local_ids and tid not in self._official_ids
            if not hidden:
                section_has[section] = True
                if not query and self._src_collapsed.get(section):
                    hidden = True   # collapsed, non-search view only. Hide rows, keep header
            self._table.setRowHidden(row, hidden)
        # Section headers show only when their section has rows and no search is
        # active. Search flattens everything into one result list.
        for row, key in header_rows:
            show = (not query) and section_has.get(key, False)
            self._table.setRowHidden(row, not show)
            if show:
                self._set_group_header_arrow(row, key)
        self._table.setUpdatesEnabled(True)   # single repaint, fully filtered
        if query:
            self._search_count_lbl.setText(_("{matches} of {total}").format(
                matches=matches, total=self._table.rowCount() - len(header_rows)))
        else:
            self._search_count_lbl.setText("")

    def _refresh_table(self) -> None:
        # Disable repaints for the whole rebuild. The engine inventory can be
        # thousands of rows. try/finally guarantees updates resume.
        prev = self._table.blockSignals(True)
        self._table.setUpdatesEnabled(False)
        try:
            self._table.setRowCount(0)
            # Local triggers split into collapsible source sections. General-tab
            # triggers, no fight tag, divide into General and DoT, the DoT ones
            # being expiry_warn_s > 0. Every other fight groups under Local.
            generals = [t for t in self._triggers if not (t.fight or "") and not self._is_dot(t)]
            dots     = [t for t in self._triggers if not (t.fight or "") and self._is_dot(t)]
            others   = [t for t in self._triggers if (t.fight or "")]
            self._append_group_header("general", _("General"))
            for t in generals:
                self._append_row(t)
            self._append_group_header("dot", _("DoT"))
            for t in dots:
                self._append_row(t)
            self._append_group_header("local", _("Local"))
            for t in others:
                self._append_row(t)
            # Engine triggers are read-only and never enter self._triggers, so the
            # save path is safe. Triggernometry gets its own section so imports
            # aren't lumped in with Triggevent or Local. Empty-section headers get
            # hidden by _apply_tab_filter.
            eng = sorted(self._engine_inventory,
                         key=lambda e: (self._engine_fight_tag(e) or "~",
                                        e.get("source") or "", (e.get("name") or "").lower()))
            self._append_group_header("engine", _("Triggevent"))
            for e in eng:
                if e.get("source") != "triggernometry":
                    self._append_engine_row(e)
            self._append_group_header("triggernometry", _("Triggernometry"))
            for e in eng:
                if e.get("source") == "triggernometry":
                    self._append_engine_row(e)
        finally:
            self._table.setUpdatesEnabled(True)
            self._table.blockSignals(prev)
        self._refresh_tree()

    def _append_row(self, t: Trigger) -> None:
        prev = self._table.blockSignals(True)
        row = self._table.rowCount()
        self._table.insertRow(row)

        cb = QTableWidgetItem()
        cb.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsSelectable)
        cb.setCheckState(Qt.CheckState.Checked if t.enabled else Qt.CheckState.Unchecked)
        cb.setData(Qt.ItemDataRole.UserRole, t.id)
        if not (t.fight or ""):
            cb.setData(_SECTION_ROLE, "dot" if self._is_dot(t) else "general")
        else:
            cb.setData(_SECTION_ROLE, "local")
        self._table.setItem(row, _C_EN, cb)

        def _ro(text: str) -> QTableWidgetItem:
            item = QTableWidgetItem(text)
            item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            return item

        ability_display = t.ability_id if t.ability_id else t.ability_regex
        # Display the localized name/callout under a Japanese UI, same gate as
        # the spoken callouts. Both fall back to English. The stored trigger
        # keeps its English name/tts_text. Only the visible cells swap.
        self._table.setItem(row, _C_NAME,  _ro(self._localized_name(t)))
        self._table.setItem(row, _C_FIGHT, _ro(t.fight))
        self._table.setItem(row, _C_TYPE,  _ro(t.log_type))
        self._table.setItem(row, _C_RE,    _ro(ability_display))
        self._table.setItem(row, _C_TTS,   _ro(self._localized_callout(t)))

        dot, color = self._zone_dot(t)
        zone_item = _ro(dot)
        zone_item.setForeground(QBrush(QColor(color)))
        zone_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        self._table.setItem(row, _C_ZONE, zone_item)

        self._table.blockSignals(prev)

    def _selected_trigger(self) -> tuple[Trigger | None, int]:
        items = self._table.selectedItems()
        if not items:
            return None, -1
        row = items[0].row()
        trigger_id = self._table.item(row, _C_EN).data(Qt.ItemDataRole.UserRole)
        for i, t in enumerate(self._triggers):
            if t.id == trigger_id:
                return t, i
        return None, -1

    def _add_trigger(self) -> None:
        dlg = TriggerDialog(parent=self, current_zone=self._match_zone,
                            fight_picker=self._pick_fight_folder,
                            current_fight=self._current_fight_tag)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._commit_new_trigger(dlg.get_trigger())
        # exec keeps the dialog a live C++ child of this immortal window, so
        # defer-destroy it or every opened editor accumulates for the session.
        dlg.deleteLater()

    @staticmethod
    def _matcher_key(t: Trigger) -> tuple[str, str]:
        """The identifying matcher for a trigger, lowercased. ability_id takes
        precedence over ability_regex, matching the engine's matches logic."""
        if t.ability_id:
            return ("id", t.ability_id.casefold())
        return ("regex", t.ability_regex.casefold())

    @staticmethod
    def _pipe_parts(value: str) -> set[str]:
        """The concrete parts of a pipe joined field, split, stripped and
        uppercased. A log_type of 21|22 listens on both types, an
        ability_id of A55D|A55E pins both ids."""
        return {p.strip().upper() for p in str(value).split("|") if p.strip()}

    def _is_duplicate(self, t: Trigger) -> Trigger | None:
        """Return an existing trigger whose fight, log types and matcher
        overlap t's, or None. Case-insensitive on fight tag and matcher.
        Pipe joined fields overlap per part, so an imported 21|22 row
        collides with an existing plain 21 one and a row pinning A55D alone
        collides with an existing A55D|A55E, the same per part expansion the
        CLI merge dedups with. Without it both speak on the shared line.
        Triggers with no matcher at all never count as duplicates. An
        existing row with the SAME id is not skipped. A converted import
        can carry a colliding id, and that collision is exactly what the
        caller needs flagged."""
        key = self._matcher_key(t)
        if not key[1]:
            return None
        fight = t.fight.casefold()
        t_types = self._pipe_parts(t.log_type)
        t_ids = self._pipe_parts(t.ability_id)
        for other in self._triggers:
            if not (self._pipe_parts(other.log_type) & t_types):
                continue
            if other.fight.casefold() != fight:
                continue
            other_key = self._matcher_key(other)
            if other_key[0] != key[0]:
                continue
            if key[0] == "id":
                if self._pipe_parts(other.ability_id) & t_ids:
                    return other
            elif other_key[1] == key[1]:
                return other
        return None

    def _commit_new_trigger(self, t: Trigger) -> bool:
        """Append, register, row, and save a brand-new trigger after a duplicate
        check. Returns True if committed, False if the user cancelled or chose to
        edit the existing duplicate. Every new-trigger site routes through here.
        Not for in-place edits."""
        existing = self._is_duplicate(t)
        if existing is not None:
            box = ac.QMessageBox(self)
            box.setIcon(ac.QMessageBox.Icon.Question)
            box.setWindowTitle(_("Duplicate Trigger"))
            box.setText(
                _('A trigger named "{name}" already covers this fight, '
                  'log type, and matcher.').format(name=existing.name))
            box.setInformativeText(_("Edit the existing one, add this anyway, or cancel?"))
            edit_btn = box.addButton(_("Edit existing"), ac.QMessageBox.ButtonRole.AcceptRole)
            add_btn = box.addButton(_("Add anyway"), ac.QMessageBox.ButtonRole.ActionRole)
            box.addButton(ac.QMessageBox.StandardButton.Cancel)
            box.setDefaultButton(edit_btn)
            box.exec()
            clicked = box.clickedButton()
            if clicked is edit_btn:
                self._open_trigger_for_edit(existing)
                return False
            if clicked is not add_btn:
                return False  # Cancel or dialog closed
        self._triggers.append(t)
        self._local_ids.add(t.id)
        self._refresh_table()
        self._save_triggers()
        return True

    def _open_trigger_for_edit(self, existing: Trigger) -> None:
        """Open an existing trigger in the editor and replace it in place. Works
        on a Trigger by reference, not by table selection."""
        idx = next((i for i, x in enumerate(self._triggers) if x.id == existing.id), -1)
        if idx < 0:
            return
        dlg = TriggerDialog(trigger=existing, parent=self, current_zone=self._match_zone,
                            fight_picker=self._pick_fight_folder)
        accepted = dlg.exec() == QDialog.DialogCode.Accepted
        # Defer-destroy before the early return below can skip it, see
        # _edit_trigger. Same-stack reads after deleteLater are still safe.
        dlg.deleteLater()
        if accepted:
            updated = dlg.get_trigger(existing_id=existing.id)
            # The repo-update reload can rebuild self._triggers during the
            # modal's nested event loop, so the index captured above may now
            # address a different trigger. Re-resolve by id, like _edit_trigger,
            # and bail when the row is gone.
            idx = next((i for i, x in enumerate(self._triggers) if x.id == existing.id), -1)
            if idx < 0:
                return
            self._triggers[idx] = updated
            self._local_ids.add(updated.id)
            self._refresh_table()
            self._save_triggers()

    def _edit_trigger(self) -> None:
        key = self._selected_row_key()
        if self._is_engine_key(key):
            self._edit_engine_row(key)
            return
        t, _idx = self._selected_trigger()
        if t is None:
            return
        dlg = TriggerDialog(trigger=t, parent=self, current_zone=self._match_zone,
                            fight_picker=self._pick_fight_folder)
        accepted = dlg.exec() == QDialog.DialogCode.Accepted
        # Defer-destroy before the early return below can skip it, see
        # _add_trigger. Same-stack reads after deleteLater are still safe.
        dlg.deleteLater()
        if accepted:
            updated = dlg.get_trigger(existing_id=t.id)
            # The repo-update reload can rebuild self._triggers during the
            # modal's nested event loop, so the index captured above may now
            # address a different trigger. Re-resolve by id, mirroring
            # _open_trigger_for_edit, and bail when the row is gone.
            idx = next((i for i, x in enumerate(self._triggers) if x.id == t.id), -1)
            if idx < 0:
                return
            self._triggers[idx] = updated
            self._local_ids.add(updated.id)
            self._refresh_table()
            self._save_triggers()

    def _duplicate_trigger(self) -> None:
        t, _unused = self._selected_trigger()
        if t is None:
            return
        d = t.to_dict()
        d["id"] = str(uuid.uuid4())
        d["name"] = t.name + _(" (copy)")
        dup = Trigger.from_dict(d)
        self._triggers.append(dup)
        self._local_ids.add(dup.id)
        self._refresh_table()
        self._save_triggers()

    def _delete_trigger(self) -> None:
        t, _idx = self._selected_trigger()
        if t is None:
            return
        if ac.QMessageBox.question(
            self, _("Delete Trigger"), _('Delete "{name}"?').format(name=self._localized_name(t))
        ) == ac.QMessageBox.StandardButton.Yes:
            # Re-resolve by id after the modal. The repo-update reload can
            # rebuild the list during its nested event loop, orphaning the
            # captured index, see _edit_trigger.
            idx = next((i for i, x in enumerate(self._triggers) if x.id == t.id), -1)
            if idx < 0:
                return
            if t.id in self._official_ids:
                self._deleted_ids.add(t.id)
            self._local_ids.discard(t.id)
            self._triggers.pop(idx)
            self._refresh_table()
            self._save_triggers()

    def _test_trigger(self) -> None:
        # Engine rows, Triggevent/Triggernometry/cactbot, aren't local Trigger
        # objects, so _selected_trigger can't find them. Route them to the
        # engine preview instead of silently no-opping. That made "Test Fire" do
        # nothing on Triggevent callouts.
        key = self._selected_row_key()
        if self._is_engine_key(key):
            self._test_engine_callout(key)
            return
        t, _unused = self._selected_trigger()
        if t is None:
            return
        self._fire(t)

    def _on_table_context_menu(self, pos) -> None:
        key = self._selected_row_key()
        if self._is_engine_key(key):
            self._engine_row_context_menu(key, self._table.viewport().mapToGlobal(pos))
            return
        t, idx = self._selected_trigger()
        if t is None:
            return

        menu = QMenu(self._table)

        edit_action   = menu.addAction(_("Edit"))
        dup_action    = menu.addAction(_("Duplicate"))
        test_action   = menu.addAction(_("Test Fire"))
        menu.addSeparator()
        toggle_action = menu.addAction(_("Disable") if t.enabled else _("Enable"))
        menu.addSeparator()
        delete_action = menu.addAction(_("Delete"))

        reset_action = None
        if t.id in self._local_ids and t.id in self._official_ids:
            menu.addSeparator()
            reset_action = menu.addAction(_("Reset to Default"))

        folder_actions: list = []
        if t.id in self._local_ids and t.id not in self._official_ids and self._folders:
            menu.addSeparator()
            folder_menu = menu.addMenu(_("Move to Folder"))
            for fo in self._folders:
                a = folder_menu.addAction(fo["name"])
                a.setData(fo["name"])
                folder_actions.append(a)
            if any(t.fight == fo["name"] for fo in self._folders):
                folder_menu.addSeparator()
                rm = folder_menu.addAction(_("Remove from Folder"))
                rm.setData("")
                folder_actions.append(rm)

        chosen = menu.exec(self._table.viewport().mapToGlobal(pos))

        if chosen is edit_action:
            self._edit_trigger()
        elif chosen is dup_action:
            self._duplicate_trigger()
        elif chosen is test_action:
            self._test_trigger()
        elif chosen is toggle_action:
            # menu.exec ran a nested event loop. Re-fetch the live object by
            # id in case the repo-update reload rebuilt the list meanwhile.
            live = next((x for x in self._triggers if x.id == t.id), None)
            if live is None:
                return
            live.enabled = not live.enabled
            self._local_ids.add(live.id)
            self._refresh_table()
            self._save_triggers()
        elif chosen is delete_action:
            self._delete_trigger()
        elif reset_action and chosen is reset_action:
            self._reset_trigger(t, idx)
        elif chosen in folder_actions:
            live = next((x for x in self._triggers if x.id == t.id), None)
            if live is None:
                return
            live.fight = chosen.data()
            self._refresh_table()
            self._save_triggers()

    def _reset_trigger(self, t: Trigger, idx: int) -> None:
        original = self._official_triggers.get(t.id)
        if original is None:
            return
        # The caller's index was captured before the context menu's nested
        # event loop. Re-resolve by id, see _edit_trigger, and bail when the
        # reload meanwhile dropped the row.
        idx = next((i for i, x in enumerate(self._triggers) if x.id == t.id), -1)
        if idx < 0:
            return
        # Insert a COPY, not the pristine snapshot object itself. Aliasing it
        # into self._triggers would let a later in-place toggle mutate both, so
        # _save_triggers would compare the trigger against itself and silently
        # drop the override, the same self-comparison trap _official_triggers is
        # a snapshot to avoid.
        self._triggers[idx] = Trigger.from_dict(original.to_dict())
        self._local_ids.discard(t.id)
        self._refresh_table()
        self._save_triggers()

    def _reset_all_to_default(self) -> None:
        # Unchecks every trigger across all fights and sources, bundled, custom,
        # engine rows. Only clears the on/off checkmarks. It does not remove
        # triggers or restore values.
        changed = False
        for t in self._triggers:
            if t.enabled:
                t.enabled = False
                if t.id in self._official_ids:
                    self._local_ids.add(t.id)   # persist the disabled override
                changed = True
        # Engine rows aren't in self._triggers. Disable them via their per-source set.
        engine_srcs = set()
        for e in getattr(self, "_engine_inventory", []):
            src, tid = e.get("source"), e.get("id")
            if src and tid:
                dset = self._engine_disabled.setdefault(src, set())
                if tid not in dset:
                    dset.add(tid)
                    engine_srcs.add(src)
        # A full reset also clears the two Global-toggle direction flags.
        self._global_local_on_flag = False
        self._global_tv_on_flag = False
        self._settings["global_local_on"] = False
        self._settings["global_tv_on"] = False
        for src in engine_srcs:
            self._persist_engine_disabled(src)   # also saves settings
            self._apply_engine_disabled(src)
        if changed:
            self._save_triggers()
        self._save_settings()
        self._refresh_table()
        self._update_fight_controls()

    def _callout_dedup_key(self, text: str) -> str:
        """Normalized key for matching identical callouts across sources.
        Casefolded and whitespace collapsed, so 'Hyperdrive' and ' hyperdrive '
        collapse together while genuinely different callouts stay distinct."""
        return " ".join(str(text).casefold().split())

    def _claim_callout(self, text: str) -> None:
        """An own trigger callout. The caller still does its own speak and
        alert. This just claims the text so any pending guest for the same
        mechanic is dropped, and any later guest inside the window is
        silenced. Own triggers are never silenced by anything."""
        key = self._callout_dedup_key(text)
        now = time.monotonic()
        self._callout_claimed[key] = now + _CALLOUT_CLAIM_S
        # An earlier guest may still hold the severity marker for this text.
        # Drop it: an own claim is never upgraded by a later guest.
        self._guest_claim_sev.pop(key, None)
        pending = self._pending_guests.pop(key, None)
        if pending is not None:
            timer, _sev = pending
            timer.stop()
            timer.deleteLater()
        if len(self._callout_claimed) > 64:
            for k in [k for k, v in self._callout_claimed.items() if v <= now]:
                del self._callout_claimed[k]
                self._guest_claim_sev.pop(k, None)

    def _emit_guest_callout(self, text: str, severity: str = "info") -> None:
        """A guest callout, a cactbot timeline entry or a cactbot raidboss
        line. The program's own triggers win. If one already claimed this
        text, or claims it while this waits, the guest is silenced. Two
        guests with the same text collapse to one, keeping the higher
        severity. Own triggers off means emit now, no latency.

        The dedup key uses the localized form because own triggers claim
        their localized text. Keying guests on the raw source text would
        never match under localization, ja display vs english source, and
        the dedup would silently no-op for localized users."""
        if not text:
            return
        loc = self._localize_text(text)
        key = self._callout_dedup_key(loc)
        now = time.monotonic()
        if self._callout_claimed.get(key, 0.0) > now:
            # An own trigger or earlier guest covered it. A guest with a
            # higher tier than the guest holding the claim still upgrades
            # the alert, sound and overlay tier. The text is not spoken twice.
            prev = self._guest_claim_sev.get(key)
            if (prev is not None
                    and _GUEST_SEVERITY_RANK.get(severity, 0)
                    > _GUEST_SEVERITY_RANK.get(prev, 0)):
                self._guest_claim_sev[key] = severity
                self._emit_alert(loc, severity)
            return
        pending = self._pending_guests.get(key)
        if pending is not None:
            # An identical guest is already waiting. Keep its one timer,
            # raise the severity it will fire with.
            timer, prev = pending
            if (_GUEST_SEVERITY_RANK.get(severity, 0)
                    > _GUEST_SEVERITY_RANK.get(prev, 0)):
                self._pending_guests[key] = (timer, severity)
            return
        if not self._triggers_enabled:
            self._flush_guest(loc, severity, key)
            return
        # Defer here. An own trigger for the same mechanic claims the text
        # via _claim_callout, which cancels this timer before it fires. The
        # severity is read back from _pending_guests at fire time so the
        # upgrade above reaches it.
        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.timeout.connect(
            lambda _loc=loc, _key=key, _t=timer:
                self._flush_guest_deferred(_loc, _key, _t))
        self._pending_guests[key] = (timer, severity)
        timer.start(_GUEST_CALLOUT_DEFER_MS)

    def _clear_callout_dedup(self) -> None:
        """Drop all claim state and cancel pending guests. Called on
        encounter end, which also covers zone changes and re-instance, so a
        stale claim or a deferred guest from one pull cannot silence a
        callout in the next."""
        for timer, _sev in list(self._pending_guests.values()):
            try:
                timer.stop()
                timer.deleteLater()
            except Exception:  # noqa: BLE001 - a half-fired timer must not block reset
                pass
        self._pending_guests.clear()
        self._callout_claimed.clear()
        self._guest_claim_sev.clear()

    def _load_callout_defaults(self) -> dict:
        """Shipped callout rewrites, keyed as source -> {trigger-id -> text}."""
        if not ac.CALLOUT_DEFAULTS_FILE.exists():
            return {}
        try:
            raw = json.loads(ac.CALLOUT_DEFAULTS_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(raw, dict):
            return {}
        out: dict = {}
        for src in ("triggevent", "triggernometry"):
            rows = raw.get(src)
            if isinstance(rows, dict):
                out[src] = {k: v for k, v in rows.items() if isinstance(v, str)}
        return out

    def _callout_edit_dict(self, src: str) -> "dict | None":
        """The user's own edits, mutable. None means the source isn't live-editable."""
        if src == "triggevent":
            return self._triggevent_callout_edits
        if src == "triggernometry":
            return self._triggernometry_callout_edits
        return None

    def _callout_edits_for(self, src: str) -> "dict | None":
        """What the source will actually speak. Shipped defaults with the
        user's own edits layered on top. Read-only. Edit through
        _apply_callout_edit."""
        user = self._callout_edit_dict(src)
        if user is None:
            return None
        return {**self._shipped_callout_defaults.get(src, {}), **user}

    def _apply_callout_edit(self, src: str, tid: str, text: str) -> None:
        if src == "triggevent":
            self._set_triggevent_callout_edit(tid, text)
        elif src == "triggernometry":
            self._set_triggernometry_callout_edit(tid, text)

    def _reset_callout_edit(self, src: str, tid: str) -> None:
        if src == "triggevent":
            self._reset_triggevent_callout_edit(tid)
        elif src == "triggernometry":
            self._reset_triggernometry_callout_edit(tid)

    def _load_cached_callouts_ja(self) -> None:
        """Load the per-callout Japanese overlay from the writable download
        cache or the bundled copy, whichever is RICHER. Higher app_version
        first, then more entries. Picking the richer one stops a stale
        downloaded cache, say an older committed version, from shadowing a
        newer bundled file. Never raises. Rebinds the dicts, never mutates,
        so an in-flight _fire read stays safe."""
        best_key, parsed = None, {}
        for src in (ac._CALLOUTS_JA_CACHE, ac._CALLOUTS_JA_BUNDLE):
            try:
                cand = json.loads(src.read_text(encoding="utf-8"))
            except (OSError, ValueError):   # ValueError covers JSON + UnicodeDecode
                continue
            if not (isinstance(cand, dict) and isinstance(cand.get("callouts"), dict)):
                continue
            key = (updater.parse_version(str(cand.get("app_version") or "0")),
                   len(cand["callouts"]))
            if best_key is None or key > best_key:
                best_key, parsed = key, cand
        # Keep only non-empty str->str entries. One bad value can't blank a callout.
        _clean = lambda m: {k: v for k, v in (m if isinstance(m, dict) else {}).items()
                            if isinstance(k, str) and isinstance(v, str) and v}
        self._callouts_ja = _clean(parsed.get("callouts"))          # id -> ja display
        self._callouts_phrases_ja = _clean(parsed.get("phrases"))   # callout-text -> ja display
        self._callouts_readings = _clean(parsed.get("readings"))    # ja display -> kana reading
        self._callouts_names_ja = _clean(parsed.get("names"))       # id -> ja trigger name
        self._callouts_names_text_ja = _clean(parsed.get("names_text"))  # english name -> ja
        # Precompile regex patterns for phrase keys with a {token}. The engine
        # resolves Groovy, {event.estimatedRemainingDuration}, ternaries and
        # so on, before the text reaches us, so an exact lookup can never
        # hit. Each such key gets compiled to an anchored catch-all regex,
        # tried only after the exact dict misses. Static keys stay a plain
        # dict lookup.
        self._callouts_phrases_ja_patterns = _compile_phrase_patterns(self._callouts_phrases_ja)
        set_readings(self._callouts_readings)   # so every speak call gets kana, not just callers that pass reading=

    def _refresh_callouts_ja_async(self) -> None:
        """Fetch the latest callouts_ja.json from the repo, main branch, in
        the background and cache it. Size-capped read, atomic write, SILENT
        on any failure. The bundled or cached copy stands in. Emits
        _callouts_ja_signal."""
        ref = "main"
        url = f"https://raw.githubusercontent.com/{updater.REPO}/{ref}/callouts_ja.json"

        def _fetch() -> None:
            changed = False
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "NyaaTriggers"})
                with urllib.request.urlopen(req, timeout=15) as resp:
                    raw = resp.read(_CALLOUTS_JA_MAX_BYTES + 1)   # bounded, unlike repo-triggers
                if len(raw) > _CALLOUTS_JA_MAX_BYTES:
                    raise ValueError("callouts overlay too large")
                data = json.loads(raw)
                if not (isinstance(data, dict) and isinstance(data.get("callouts"), dict)):
                    raise ValueError("unexpected overlay format")
                _atomic_write_json(ac._CALLOUTS_JA_CACHE, data, indent=2)
                changed = True
            except Exception:
                changed = False   # silent, no dialog, keep the current map
            self._callouts_ja_signal.emit(changed)

        threading.Thread(target=_fetch, daemon=True).start()

    def _on_callouts_ja_refreshed(self, changed: bool) -> None:
        """GUI-thread slot. Reload the overlay from the freshened cache. Not
        a locale-catalog change, so it does NOT call reload_catalogs. The
        table displays localized names and callouts, so repaint it with the
        fresh maps."""
        if changed:
            self._load_cached_callouts_ja()
            self._refresh_table()
            self._apply_tab_filter()

    def _selected_row_key(self):
        items = self._table.selectedItems()
        if not items:
            return None
        cb = self._table.item(items[0].row(), _C_EN)
        return cb.data(Qt.ItemDataRole.UserRole) if cb else None

    def _fight_local_triggers(self, fight: str) -> list:
        out = [t for t in self._triggers if (t.fight or "") == fight]
        if fight == "":
            # The General leaf lists only official no-fight triggers. Pure-custom
            # ones live under Unsorted, so the General Local box acts only on what
            # it actually shows.
            out = [t for t in out
                   if not (t.id in self._local_ids and t.id not in self._official_ids)]
        return out

    def _fight_tv_ids(self, fight: str) -> list:
        return [e["id"] for e in self._engine_inventory
                if e.get("source") == "triggevent" and e.get("id")
                and self._engine_fight_tag(e) == fight]

    def _set_fight_local(self, fight: str, enabled: bool) -> None:
        changed = False
        for t in self._fight_local_triggers(fight):
            if t.enabled != enabled:
                t.enabled = enabled
                self._local_ids.add(t.id)
                changed = True
        if changed:
            self._save_triggers()

    def _set_sections_collapsed(self, collapsed: bool, *keys: str) -> None:
        """Collapse or expand the named table source groups."""
        for k in keys:
            self._src_collapsed[k] = collapsed

    def _set_fight_tv(self, fight: str, enabled: bool) -> None:
        dset = self._engine_disabled.setdefault("triggevent", set())
        for tid in self._fight_tv_ids(fight):
            if enabled:
                dset.discard(tid)
            else:
                dset.add(tid)
        self._persist_engine_disabled("triggevent")
        self._apply_engine_disabled("triggevent")

    def _all_tv_ids(self) -> list:
        return [e["id"] for e in getattr(self, "_engine_inventory", [])
                if e.get("source") == "triggevent" and e.get("id")]

    def _toggle_global_tv(self) -> None:
        # Direction is a stored flag that flips each click, so per-fight edits
        # never change what this button does.
        enable = not self._global_tv_on_flag
        self._global_tv_on_flag = enable
        self._settings["global_tv_on"] = enable
        dset = self._engine_disabled.setdefault("triggevent", set())
        if enable:
            for i in self._all_tv_ids():
                dset.discard(i)
        else:
            dset.update(self._all_tv_ids())
        self._persist_engine_disabled("triggevent")
        self._apply_engine_disabled("triggevent")
        self._set_sections_collapsed(not enable, "engine")   # expand on, collapse off
        self._refresh_table()
        self._update_fight_controls()

    def _toggle_global_local(self) -> None:
        enable = not self._global_local_on_flag
        self._global_local_on_flag = enable
        self._settings["global_local_on"] = enable
        for t in self._triggers:
            if t.enabled != enable:
                t.enabled = enable
                self._local_ids.add(t.id)
        self._save_triggers()
        self._save_settings()                      # persist the global_local_on flag
        if enable:
            # Re-push the schedule the off path cleared from the plugin.
            # The helper keeps cactbot's own bars up in cactbot mode.
            self._push_timeline_to_plugin()
        else:
            # The timeline is not a trigger row, so the loop above leaves it
            # running. Kill its clock the way the master Triggers switch
            # does, or it keeps talking over the engines. The helper clears
            # the bar, or re-pushes cactbot's own schedule in cactbot mode.
            self._timeline.reset()
            self._push_timeline_to_plugin()
        self._set_sections_collapsed(not enable, "general", "dot", "local")  # expand on, collapse off
        self._refresh_table()
        self._update_fight_controls()

    def _on_fight_local_only_toggled(self, checked: bool) -> None:
        # Turns this fight's local triggers on or off. Checking expands the
        # affected sections, General means General plus DoT. Unchecking
        # collapses them.
        f = self._fight_cur
        self._set_fight_local(f, checked)
        self._set_sections_collapsed(not checked, *(("general", "dot") if f == "" else ("local",)))
        self._refresh_table()
        self._update_fight_controls()

    def _on_fight_tv_only_toggled(self, checked: bool) -> None:
        # Same as the Local box, for this fight's Triggevent triggers.
        self._set_fight_tv(self._fight_cur, checked)
        self._set_sections_collapsed(not checked, "engine")
        self._refresh_table()
        self._update_fight_controls()

    def _append_group_header(self, key: str, label: str) -> None:
        """Bold, non-selectable section-divider row for a source group,
        local or engine. Clicking it collapses or expands that group's
        rows, see _on_table_cell_clicked."""
        row = self._table.rowCount()
        self._table.insertRow(row)
        bg = QBrush(QColor("#101013"))
        for col in range(self._table.columnCount()):
            cell = QTableWidgetItem("")
            cell.setFlags(Qt.ItemFlag.ItemIsEnabled)
            cell.setBackground(bg)
            self._table.setItem(row, col, cell)
        self._table.item(row, _C_EN).setData(Qt.ItemDataRole.UserRole, f"__hdr__:{key}")
        name = self._table.item(row, _C_NAME)
        arrow = "▶" if self._src_collapsed.get(key) else "▼"
        name.setText(f"{arrow}  {label}")
        fnt = name.font(); fnt.setBold(True); name.setFont(fnt)
        name.setForeground(QBrush(QColor("#ff8399")))

    def _set_group_header_arrow(self, row: int, key: str) -> None:
        name = self._table.item(row, _C_NAME)
        if name is None:
            return
        arrow = "▶" if self._src_collapsed.get(key) else "▼"
        name.setText(f"{arrow}  {name.text().lstrip(chr(0x25b6) + chr(0x25bc) + ' ')}")

    def _on_table_cell_clicked(self, row: int, _col: int) -> None:
        en = self._table.item(row, _C_EN)
        tid = en.data(Qt.ItemDataRole.UserRole) if en else None
        if isinstance(tid, str) and tid.startswith("__hdr__:"):
            key = tid.split(":", 1)[1]
            self._src_collapsed[key] = not self._src_collapsed.get(key, False)
            self._apply_tab_filter()

    def _set_local_enabled(self, enabled: bool) -> None:
        # Local engine on/off, driven by the master Triggers switch.
        self._local_enabled = bool(enabled)
        # Turning local triggers off must also tear down any pending reapply
        # warnings. A StatusTimerRunner fires on its own QTimer, independent
        # of the log loop, so without this it would still speak after the
        # master switch, or Cactbot, which disables local, is turned off.
        if not self._local_enabled:
            self._clear_status_timers()
            # The timeline runs on its own 50ms tick, so it keeps speaking
            # after the switch, and doubles Cactbot's callouts since Cactbot
            # disables local, unless it is reset here too.
            self._timeline.reset()
            # The clock just stopped. Route the plugin through the helper:
            # outside cactbot mode it clears, so the plugin stops
            # interpolating the last tick as if the pull were still running.
            # In cactbot mode it re-pushes cactbot's own schedule instead,
            # so turning Cactbot on does not blank the bars it just loaded.
            self._push_timeline_to_plugin()
        else:
            # Re-enabling re-pushes the schedule the off path cleared from
            # the plugin, same as the feed and plugin-link reconnects do.
            # The helper still holds it back while the global kill switch
            # is off.
            self._push_timeline_to_plugin()
        self._settings["local_enabled"] = self._local_enabled
        self._save_settings()

    def _set_triggers_enabled(self, enabled: bool) -> None:
        """Turn your editable callouts, Local plus Triggevent plus
        Triggernometry, on or off. Cactbot drives this, mutual exclusion.
        On by default. The Triggevent engine is not started or stopped
        here, only whether its callouts fire."""
        self._triggers_enabled = bool(enabled)
        self._settings["triggers_enabled"] = self._triggers_enabled
        self._save_settings()
        if enabled:
            # Stop cactbot and clear its persisted flag, so a stale
            # cactbot_enabled can't auto-start it at the next launch.
            self._set_cactbot_enabled(False)
        self._set_local_enabled(enabled)
        if TriggeventBridge.is_available():
            self._set_triggevent_enabled(enabled)
        # Triggernometry imports run alongside Local under this switch. The
        # converter makes simple ones Local rows. The sidecar runs the
        # scripted ones the converter skips. Only starts if a pack has been
        # imported.
        if TriggernometryBridge is not None and TriggernometryBridge.is_available():
            self._set_triggernometry_enabled(enabled)

    def _on_callouts_localized_changed(self, state: int) -> None:
        # Gates the spoken path, read live per fire, AND the table's
        # localized Name and TTS columns, built per row, so repaint the
        # table to switch both at once.
        self._settings["callouts_localized"] = bool(state)
        self._save_settings()
        self._refresh_table()
        self._apply_tab_filter()

    def _restore_triggers_from_repo(self) -> None:
        self._download_repo_triggers(self._restore_trig_btn, _("Restore from Repo"))

    def _export_triggers(self) -> None:
        if not ac.TRIGGERS_LOCAL_FILE.exists():
            ac.QMessageBox.information(self, _("Export Triggers"), _("No local triggers to export."))
            return
        dlg = ac.QFileDialog(self, _("Export Triggers"), "triggers_export.json",
                          _("JSON files (*.json)"))
        dlg.setAcceptMode(ac.QFileDialog.AcceptMode.AcceptSave)
        # The static helper cannot do this. Qt's own save dialog does not
        # derive a suffix from the name filter, so a retyped bare filename
        # would otherwise keep no extension.
        dlg.setDefaultSuffix("json")
        if dlg.exec() != QDialog.DialogCode.Accepted or not dlg.selectedFiles():
            return
        path = dlg.selectedFiles()[0]
        try:
            # Sibling copy plus rename, mirrors _import_triggers. An
            # interrupted export must not leave a truncated file at the
            # chosen path.
            dest = Path(path)
            tmp = dest.with_suffix(dest.suffix + ".tmp")
            try:
                shutil.copy2(ac.TRIGGERS_LOCAL_FILE, tmp)
                _fsync_file(tmp)
                os.replace(tmp, dest)
            except OSError:
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    pass
                raise
        except OSError as exc:
            ac.QMessageBox.critical(self, _("Export Failed"),
                                 _("Could not write file:\n{error}").format(error=exc))
            return
        ac.QMessageBox.information(self, _("Export Triggers"), _("Exported to:\n{path}").format(path=path))

    def _import_triggers(self) -> None:
        path, _unused = ac.QFileDialog.getOpenFileName(
            self, _("Import Triggers"), "", _("JSON files (*.json)")
        )
        if not path:
            return
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            # "in" is a substring test when data is a JSON string or list, so
            # those would pass here and then silently blank the local file.
            if not isinstance(data, dict) or "triggers" not in data:
                raise ValueError(_("File is missing a 'triggers' key - not a NyaaTriggers export"))
            # The key alone is not enough either. {"triggers": 42} passes the
            # test above, the loader then reads the junk value as an empty set
            # and the session comes back blank under a success dialog.
            trigs = data["triggers"]
            if not isinstance(trigs, list) or not all(isinstance(t, dict) for t in trigs):
                raise ValueError(_("File's 'triggers' is not a list of triggers - not a NyaaTriggers export"))
        except Exception as exc:
            ac.QMessageBox.critical(self, _("Import Failed"), str(exc))
            return
        answer = ac.QMessageBox.question(
            self, _("Import Triggers"),
            _("This will replace your current local triggers and folders.\n\nAre you sure?"),
        )
        if answer != ac.QMessageBox.StandardButton.Yes:
            return
        try:
            # Sibling copy plus rename. A crash mid-copy must not leave the
            # live triggers file truncated.
            tmp = ac.TRIGGERS_LOCAL_FILE.with_suffix(ac.TRIGGERS_LOCAL_FILE.suffix + ".tmp")
            shutil.copy2(path, tmp)
            # The import replaces the whole local file. Keep the pre-import
            # state as a .bak so a wrong pick is recoverable. Best effort,
            # like the .bad copies of a corrupt file.
            backup = None
            if ac.TRIGGERS_LOCAL_FILE.exists():
                try:
                    backup = ac.TRIGGERS_LOCAL_FILE.with_name(ac.TRIGGERS_LOCAL_FILE.name + ".bak")
                    shutil.copy2(ac.TRIGGERS_LOCAL_FILE, backup)
                except OSError:
                    backup = None
            _fsync_file(tmp)
            os.replace(tmp, ac.TRIGGERS_LOCAL_FILE)
        except OSError as exc:
            # e.g. a read-only install dir. Without this the slot dies silently
            # and the user gets neither an error nor the success dialog.
            ac.QMessageBox.critical(self, _("Import Failed"),
                                 _("Could not write file:\n{error}").format(error=exc))
            return
        self._load_triggers()
        msg = _("Triggers imported and reloaded.")
        if backup is not None:
            msg += "\n\n" + _("Your previous triggers were backed up to:\n{path}").format(path=backup)
        ac.QMessageBox.information(self, _("Import Triggers"), msg)

    def _create_trigger_from_prefill(self, pre: Trigger) -> None:
        dlg = TriggerDialog(trigger=pre, parent=self, current_zone=self._match_zone,
                            fight_picker=self._pick_fight_folder)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._commit_new_trigger(dlg.get_trigger())
        dlg.deleteLater()   # see _add_trigger

    def _trigger_files_stamp(self) -> tuple:
        """The mtime_ns, size pair per watched trigger file, None for
        missing ones. Rename-safe. The store writes .tmp then os.replace,
        so a changed stamp only ever appears once the new file is
        complete."""
        stamp = []
        for p in _watched_trigger_files():
            try:
                st = p.stat()
                stamp.append((st.st_mtime_ns, st.st_size))
            except OSError:
                stamp.append(None)
        return tuple(stamp)

    def _maybe_reload_triggers(self) -> None:
        """Hot-reload triggers when a watched file changed since the last
        load. Uses the exact startup and import path, _load_triggers. Same
        merge and retire semantics, table and fight-tree refresh, and the
        log loop reads self._triggers live, so the running engine picks
        the set up at once."""
        stamp = self._trigger_files_stamp()
        if stamp == self._triggers_mtime:
            return
        # A half-saved hand edit, non-atomic editor, must not swap the
        # live set for a partial parse. Skip this tick, the next one
        # retries.
        for p in _watched_trigger_files():
            try:
                if p.exists():
                    json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return
        self._load_triggers()   # re-baselines _triggers_mtime itself

    def _localized_callout(self, t: Trigger) -> str:
        """The callout template for trigger t in the active locale, else
        its English tts_text. Gated by callouts_localized, defaults on for
        a Japanese UI. The returned template still holds the {source},
        {target} and {count} tokens. Substitution runs after. Prefers the
        precise per-id overlay, then the text-keyed phrase map, then
        English, so a partial or stale overlay stays safe."""
        if not self._settings.get("callouts_localized", active_locale() == "ja"):
            return t.tts_text
        return (self._callouts_ja.get(t.id)
                or self._callouts_phrases_ja.get(t.tts_text)
                or t.tts_text)

    def _pick_fight_folder(self) -> str | None:
        """Open the fight picker. Returns the chosen folder name, "" for
        uncategorised, or None if cancelled."""
        known = {t.fight for t in self._triggers if t.fight}
        catalog = fight_catalog.load_catalog(
            _FIGHT_TREE, known, ac._DATA_DIR / "fight_catalog.json")
        fight_catalog.refresh_from_cactbot_async(ac._DATA_DIR / "fight_catalog.json")
        dlg = fight_catalog.FightPickerDialog(catalog, self)
        accepted = dlg.exec() == QDialog.DialogCode.Accepted
        folder = dlg.selected_folder() if accepted else None
        dlg.deleteLater()   # see _add_trigger
        return folder
