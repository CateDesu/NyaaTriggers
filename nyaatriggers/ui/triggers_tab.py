"""Fight tree, trigger table and callout controls for MainWindow."""

from pathlib import Path
import json
import os
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

from nyaatriggers.trigger_engine import Trigger, _as_bool
from nyaatriggers.trigger_profiles import merge_local_choices
from nyaatriggers.trigger_dialog import TriggerDialog
from nyaatriggers.tts import set_readings
from nyaatriggers.locale_util import _, active_locale
from nyaatriggers.triggevent_bridge import TriggeventBridge
try:
    from nyaatriggers.triggernometry_bridge import TriggernometryBridge
except Exception:  # noqa: BLE001
    TriggernometryBridge = None  # type: ignore
from nyaatriggers import fight_catalog
from nyaatriggers import updater

from nyaatriggers import app_common as ac
from nyaatriggers.app_common import (
    _CALLOUTS_JA_MAX_BYTES, _CALLOUT_CLAIM_S, _C_EN, _C_FIGHT, _C_NAME, _C_RE, _C_TTS, _C_TYPE, _C_ZONE, _FIGHT_TREE, _GENERAL_TAB, _GUEST_CALLOUT_DEFER_MS, _GUEST_SEVERITY_RANK, _ITEM_ID_ROLE, _ITEM_TYPE_ROLE, _SECTION_ROLE, _TREE_FIGHTS, _VERSION, _as_strset, _atomic_write_json, _compile_phrase_patterns, _fsync_file, _next_bad_name, _repo_download_version, _watched_trigger_files,
)


def _read_local_triggers():
    data = json.loads(ac.TRIGGERS_LOCAL_FILE.read_text(encoding="utf-8"))
    if (not isinstance(data, dict) or not isinstance(data.get("triggers", []), list)
            or not all(isinstance(row, dict) for row in data.get("triggers", []))):
        raise ValueError("Invalid local trigger file")
    return data


class TriggersTabMixin:
    def _load_retired_ids(self) -> set[str]:
        """Read retired trigger IDs from strings or records with an ID and reason."""
        self._trigger_replacements = {}
        src = ac._REPO_RETIRED_FILE
        # Load downloaded retirements only when their version stamp matches the program.
        if not (src.exists() and src != ac.RETIRED_FILE
                and _repo_download_version() == _VERSION):
            src = ac.RETIRED_FILE
        if not src.exists():
            return set()
        try:
            raw = json.loads(src.read_text(encoding="utf-8"))
        except (OSError, ValueError, RecursionError):
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
                targets = row.get("replaced_by")
                if isinstance(targets, list) and all(isinstance(t, str) for t in targets):
                    self._trigger_replacements[row["id"]] = targets
        self._trigger_replacements = {
            ident: [t for t in targets if t not in out]
            for ident, targets in self._trigger_replacements.items()
        }
        return out

    def _load_triggers(self) -> None:
        # Block local saves while the file is unreadable. Each reload checks again.
        self._local_corrupt = False
        official: list[Trigger] = []
        # Use repository downloads only while their stamp matches the running version.
        # Remove stale cache files after an update.
        _override = ac._REPO_TRIGGERS_FILE
        if _repo_download_version() != _VERSION:
            for _p in (ac._REPO_TRIGGERS_FILE, ac._REPO_RETIRED_FILE, ac._REPO_TRIGGERS_VERSION):
                try:
                    _p.unlink(missing_ok=True)
                except OSError:
                    pass
        # Recheck the stamp because deleting a stale file may have failed.
        _trig_src = (_override if (_override.exists() and _override != ac.TRIGGERS_FILE
                                   and _repo_download_version() == _VERSION)
                     else ac.TRIGGERS_FILE)
        # Fall back to bundled triggers if the downloaded set is corrupt.
        for _src in dict.fromkeys((_trig_src, ac.TRIGGERS_FILE)):
            if not _src.exists():
                continue
            try:
                data = json.loads(_src.read_text(encoding="utf-8"))
                # Valid JSON must still contain a trigger list.
                if not isinstance(data, list):
                    raise ValueError("not a trigger list")
                official = [Trigger.from_dict(d) for d in data if isinstance(d, dict)]
            except (OSError, ValueError, KeyError, TypeError, RecursionError) as exc:
                ac.log_drop("triggers", f"{_src.name} unreadable: {exc!r}")
                continue
            break
        self._retired_ids = self._load_retired_ids()
        # A stale download may still contain retired IDs.
        official = [t for t in official if t.id not in self._retired_ids]
        self._official_ids      = {t.id for t in official}
        # Keep independent objects in the official snapshot. Local mutations must not
        # change the baseline used to save overrides.
        self._official_triggers = {t.id: Trigger.from_dict(t.to_dict()) for t in official}

        local_triggers: list[Trigger] = []
        enabled_overrides: dict[str, bool] = {}
        self._deleted_ids = set()
        # Parse the local file once so an external write cannot mix versions within a
        # load.
        raw = None
        if ac.TRIGGERS_LOCAL_FILE.exists():
            try:
                raw = _read_local_triggers()
            except (OSError, ValueError, KeyError, TypeError, RecursionError):
                self._handle_local_corrupt()
        if isinstance(raw, dict):
            trigs = raw.get("triggers")
            if not isinstance(trigs, list):
                trigs = []
            for d in trigs:
                if not isinstance(d, dict):
                    continue
                # Apply slim enabled overrides directly. from_dict would invent missing
                # trigger content.
                if d.get("id") and set(d.keys()) <= {"id", "enabled"}:
                    enabled_overrides[str(d["id"])] = _as_bool(d.get("enabled"), True)
                else:
                    local_triggers.append(Trigger.from_dict(d))
            # Accept only string IDs so malformed entries cannot break loading or
            # sorting.
            self._deleted_ids = {x for x in _as_strset(raw.get("deleted"))
                                 if isinstance(x, str)}
        # Carry saved wording and enabled choices to the surviving definitions.
        choices = {ident: {"enabled": enabled} for ident, enabled in enabled_overrides.items()}
        for trigger in local_triggers:
            choice = {"enabled": trigger.enabled, "text": trigger.tts_text}
            off = self._official_triggers.get(trigger.id)
            if off is not None:
                td, od = trigger.to_dict(), off.to_dict()
                td.pop("enabled", None)
                od.pop("enabled", None)
                # Treat unchanged full copies as toggle records.
                if td == od:
                    choice.pop("text")
            choices[trigger.id] = choice
        choices = merge_local_choices(choices, self._trigger_replacements)
        enabled_overrides = {ident: choice["enabled"] for ident, choice in choices.items()}
        for trigger in local_triggers:
            if trigger.id in choices:
                trigger.enabled = choices[trigger.id]["enabled"]
                if "text" in choices[trigger.id]:
                    trigger.tts_text = choices[trigger.id]["text"]
        # Remove retired local triggers and tombstones before merging or saving.
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
                # Collapse matching legacy full copies into toggle overrides. Preserve
                # differing copies because they may contain user edits.
                ld, od = loc.to_dict(), t.to_dict()
                ld.pop("enabled", None)
                od.pop("enabled", None)
                if ld == od:
                    t.enabled = loc.enabled
                    merged.append(t)
                else:
                    merged.append(loc)   # Preserve the local edit.
                continue
            if t.id in enabled_overrides:
                t.enabled = enabled_overrides[t.id]
                if "text" in choices[t.id]:
                    t.tts_text = choices[t.id]["text"]
            merged.append(t)
        for t in local_triggers:
            if t.id not in self._official_ids:
                merged.append(t)
        # Reuse unchanged live trigger objects so pending timers, sequences and
        # cooldowns retain their identity. Changed triggers get new objects to
        # invalidate old work.
        prev = {t.id: t for t in getattr(self, "_triggers", [])}
        for i, t in enumerate(merged):
            old = prev.get(t.id)
            if old is not None and old.to_dict() == t.to_dict():
                merged[i] = old
        self._triggers = merged

        self._folders = []
        if isinstance(raw, dict):
            folders = raw.get("folders", [])
            if isinstance(folders, list):
                # Validate folder IDs and names before building the tree.
                self._folders = [fo for fo in folders
                                 if isinstance(fo, dict)
                                 and isinstance(fo.get("id"), str)
                                 and isinstance(fo.get("name"), str)]

        # Update watched file stamps after every load path.
        self._triggers_mtime = self._trigger_files_stamp()
        self._refresh_table()

    def _handle_local_corrupt(self) -> None:
        """Back up an unreadable local file, warn the user and block saves until it can be
        read again.
        """
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
            _("Your local triggers file could not be read. Edits are paused "
              "until the file is fixed or removed.") + where)

    def _save_triggers(self) -> bool:
        if getattr(self, "_local_corrupt", False):
            # Do not overwrite an unreadable local file. A successful reload clears this
            # block.
            return False
        # Recheck before saving because an external editor may have corrupted the file
        # since the last poll.
        try:
            _read_local_triggers()
        except FileNotFoundError:
            pass
        except (OSError, ValueError, RecursionError):
            self._handle_local_corrupt()
            return False
        to_save = [t for t in self._triggers if t.id in self._local_ids]
        records: list[dict] = []
        for t in to_save:
            off = self._official_triggers.get(t.id)
            if off is not None:
                td, od = t.to_dict(), off.to_dict()
                td.pop("enabled", None)
                od.pop("enabled", None)
                if td == od:
                    # Save only the toggle when content matches the bundled trigger.
                    if t.enabled != off.enabled:
                        records.append({"id": t.id, "enabled": t.enabled})
                    continue
            records.append(t.to_dict())
        try:
            # Handle malformed values within the save error handler.
            data = {
                "triggers": records,
                "deleted":  sorted(self._deleted_ids),
                "folders":  self._folders,
            }
            _atomic_write_json(ac.TRIGGERS_LOCAL_FILE, data, indent=2)
            # Acknowledge only this local write so other changed files still reload.
            previous = getattr(self, "_triggers_mtime", ())
            current = self._trigger_files_stamp()
            if len(previous) == len(current):
                self._triggers_mtime = tuple(
                    current[i] if path == ac.TRIGGERS_LOCAL_FILE else previous[i]
                    for i, path in enumerate(_watched_trigger_files()))
        except (OSError, TypeError, ValueError) as exc:
            # Keep edits in memory and report when the file cannot be saved.
            self._warn_save_failed(_("triggers"), exc)
            return False
        return True

    @staticmethod
    def _tree_item_path(item) -> tuple:
        """Build an arrowless label path so repeated expansion names have distinct keys.
        """
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

        # Repaint once after rebuilding to avoid flashing an empty tree.
        self._tree.setUpdatesEnabled(False)
        self._tree.blockSignals(True)
        self._tree.clear()

        gen = QTreeWidgetItem([_(_GENERAL_TAB)])
        gen.setData(0, Qt.ItemDataRole.UserRole, "")
        bold = gen.font(0); bold.setBold(True); gen.setFont(0, bold)
        self._tree.addTopLevelItem(gen)

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

        self._build_tbd_tree_section()

        self._build_custom_tree_section()

        # Restore expansion by label path without arrow glyphs. Skip animation during
        # the rebuild.
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

        self._tree.blockSignals(False)
        if not self._restore_tree_selection(cur_fight, cur_item_type):
            self._tree.setCurrentItem(gen)
        self._tree.setUpdatesEnabled(True)

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
        """Group fight tags missing from the curated tree under TBD without moving their
        data.
        """
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
        th.setFlags(Qt.ItemFlag.ItemIsEnabled)   # Clicking toggles expansion.
        f0 = th.font(0); f0.setBold(True); th.setFont(0, f0)
        th.setSizeHint(0, QSize(0, 28))
        self._tree.addTopLevelItem(th)
        for fight in sorted(tbd):
            fi = QTreeWidgetItem([f"{fight}  ({tbd[fight]})"])
            fi.setData(0, Qt.ItemDataRole.UserRole, fight)
            fi.setSizeHint(0, QSize(0, 22))
            th.addChild(fi)

    def _build_custom_tree_section(self) -> None:
        """List custom triggers that have no usable fight leaf under Unsorted. Avoid
        duplicating triggers already shown under a curated fight.
        """
        custom = [t for t in self._triggers
                  if t.id in self._local_ids and t.id not in self._official_ids
                  and (not t.zone_regex.strip() or not t.fight)
                  and (t.zone_regex.strip() or t.fight not in _TREE_FIGHTS)]

        sep = QTreeWidgetItem([""])
        sep.setFlags(Qt.ItemFlag.NoItemFlags)
        sep.setSizeHint(0, QSize(0, 14))
        sep.setData(0, _ITEM_TYPE_ROLE, "sep")
        self._tree.addTopLevelItem(sep)

        ci = QTreeWidgetItem([f"▶ {_('Unsorted')}"])
        ci.setData(0, Qt.ItemDataRole.UserRole, None)
        ci.setData(0, _ITEM_TYPE_ROLE, "custom_hdr")
        ci.setFlags(Qt.ItemFlag.ItemIsEnabled)
        f = ci.font(0); f.setBold(True); ci.setFont(0, f)
        ci.setSizeHint(0, QSize(0, 28))
        self._tree.addTopLevelItem(ci)

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
            # Disable inline editing because names changed there would be lost on
            # refresh.
            fi.setFlags(fi.flags() & ~Qt.ItemFlag.ItemIsEditable)
            fi.setSizeHint(0, QSize(0, 22))
            ci.addChild(fi)

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
        # Track visited folder IDs so duplicate IDs or cycles cannot recurse
        # indefinitely.
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
        # Folder membership uses names, so duplicate names would share members.
        if any(f["name"] == name for f in self._folders):
            ac.QMessageBox.information(
                self, _("New Folder"),
                _("A folder named '{name}' already exists.").format(name=name))
            return
        self._folders.append({"id": str(uuid.uuid4()), "name": name, "parent_id": parent_id})
        self._save_triggers()
        self._refresh_tree()

    def _create_fight_folder(self) -> None:
        """Create a folder using the fight picker."""
        folder = self._pick_fight_folder()
        if not folder:
            return
        if any(f["name"] == folder for f in self._folders):
            ac.QMessageBox.information(
                self, _("New Folder"),
                _("A folder named '{name}' already exists.").format(name=folder))
            return
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
        # Renaming to an existing folder would merge membership.
        if name != folder["name"] and any(f["name"] == name for f in self._folders):
            ac.QMessageBox.information(
                self, _("Rename Folder"),
                _("A folder named '{name}' already exists.").format(name=name))
            return
        old_name = folder["name"]
        folder["name"] = name
        # Retag local triggers when their folder name changes.
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
        # Track visited descendants to tolerate duplicate folder IDs.
        def _collect(fid: str, seen: "set[str]") -> list[str]:
            ids = [fid]
            seen.add(fid)
            for ch in self._folders:
                cid = ch.get("id")
                if ch.get("parent_id") == fid and cid not in seen:
                    ids.extend(_collect(cid, seen))
            return ids
        to_remove = set(_collect(folder_id, set()))
        # Delete local triggers tagged with removed folder names. Preserve official
        # triggers.
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
        # Keep English names searchable alongside localized display text.
        trigger_map = {t.id: t for t in self._triggers} if query else {}
        matches = 0
        header_rows: list[tuple[int, str]] = []
        section_has = {"general": False, "dot": False, "local": False,
                       "engine": False, "triggernometry": False}
        # Batch row visibility changes into one repaint.
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
                    hidden = True   # Keep headers visible when sections are collapsed.
            self._table.setRowHidden(row, hidden)
        # Hide section headers during search to show one result list.
        for row, key in header_rows:
            show = (not query) and section_has.get(key, False)
            self._table.setRowHidden(row, not show)
            if show:
                self._set_group_header_arrow(row, key)
        self._table.setUpdatesEnabled(True)
        if query:
            self._search_count_lbl.setText(_("{matches} of {total}").format(
                matches=matches, total=self._table.rowCount() - len(header_rows)))
        else:
            self._search_count_lbl.setText("")

    def _refresh_table(self) -> None:
        # Suppress repaints until the table is rebuilt, restoring them even on failure.
        prev = self._table.blockSignals(True)
        self._table.setUpdatesEnabled(False)
        try:
            self._table.setRowCount(0)
            # Split General triggers into General and DoT sections. Expiry warnings
            # identify DoT rows.
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
            # Keep engine rows outside the saved local trigger list.
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
        # Localize visible cells while preserving stored English text.
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
        # Delete the dialog after use because its window parent remains alive.
        dlg.deleteLater()

    @staticmethod
    def _matcher_key(t: Trigger) -> tuple[str, str]:
        """Return the normalized matcher, preferring an ID filter over regex."""
        if t.ability_id:
            return ("id", t.ability_id.casefold())
        return ("regex", t.ability_regex.casefold())

    @staticmethod
    def _pipe_parts(value: str) -> set[str]:
        """Split pipe alternatives, trim whitespace and uppercase each value."""
        return {p.strip().upper() for p in str(value).split("|") if p.strip()}

    def _is_duplicate(self, t: Trigger) -> Trigger | None:
        """Find a trigger with overlapping fight, types and matcher, ignoring case. Expand
        alternatives for comparison and exclude empty matchers. Include matching IDs so
        import collisions are reported.
        """
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
        """Add and save a new trigger after duplicate handling. Return False if cancelled
        or redirected to an existing trigger.
        """
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
                return False
        self._triggers.append(t)
        self._local_ids.add(t.id)
        self._refresh_table()
        self._save_triggers()
        return True

    def _open_trigger_for_edit(self, existing: Trigger) -> None:
        """Edit an existing trigger by reference."""
        idx = next((i for i, x in enumerate(self._triggers) if x.id == existing.id), -1)
        if idx < 0:
            return
        dlg = TriggerDialog(trigger=existing, parent=self, current_zone=self._match_zone,
                            fight_picker=self._pick_fight_folder)
        accepted = dlg.exec() == QDialog.DialogCode.Accepted
        # Queue deletion before an early return can skip it.
        dlg.deleteLater()
        if accepted:
            updated = dlg.get_trigger(existing_id=existing.id)
            # Resolve the trigger by ID after the modal loop because a reload may have
            # replaced the list.
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
        dlg.deleteLater()
        if accepted:
            updated = dlg.get_trigger(existing_id=t.id)
            # Resolve the trigger by ID after the modal loop because a reload may have
            # replaced the list.
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
            # Resolve by ID after the modal loop in case the list reloaded.
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
        # Engine rows need their own preview because they are not local Trigger objects.
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
            # Fetch the current object after the menu's nested event loop.
            live = next((x for x in self._triggers if x.id == t.id), None)
            if live is None:
                return
            self._set_trigger_enabled(live, not live.enabled)
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
        # Resolve by ID because the context menu may have allowed a reload.
        idx = next((i for i, x in enumerate(self._triggers) if x.id == t.id), -1)
        if idx < 0:
            return
        # Copy the official snapshot before restoring it so future edits cannot mutate
        # the save baseline.
        self._triggers[idx] = Trigger.from_dict(original.to_dict())
        self._local_ids.discard(t.id)
        self._refresh_table()
        self._save_triggers()

    def _reset_all_to_default(self) -> None:
        changed = False
        for t in self._triggers:
            if t.enabled:
                self._set_trigger_enabled(t, False)
                if t.id in self._official_ids:
                    self._local_ids.add(t.id)
                changed = True
        # Engine rows store disabled IDs separately from local triggers.
        engine_srcs = set()
        for e in getattr(self, "_engine_inventory", []):
            src, tid = e.get("source"), e.get("id")
            if src and tid:
                dset = self._engine_disabled.setdefault(src, set())
                if tid not in dset:
                    dset.add(tid)
                    engine_srcs.add(src)
        self._global_local_on_flag = False
        self._global_tv_on_flag = False
        self._settings["global_local_on"] = False
        self._settings["global_tv_on"] = False
        if not getattr(self, "_cactbot_mode", False):
            self._timeline.reset()
            self._clear_callout_dedup()
        self._push_timeline_to_plugin()
        for src in engine_srcs:
            self._persist_engine_disabled(src)
            self._apply_engine_disabled(src)
        if changed:
            self._save_triggers()
        self._save_settings()
        self._refresh_table()
        self._update_fight_controls()

    def _callout_dedup_key(self, text: str) -> str:
        """Normalize callout text for comparison across sources."""
        return " ".join(str(text).casefold().split())

    def _claim_callout(self, text: str) -> None:
        """Claim text for a local callout and suppress matching guests within the window.
        The caller handles speech and alerts.
        """
        key = self._callout_dedup_key(text)
        now = time.monotonic()
        self._callout_claimed[key] = now + _CALLOUT_CLAIM_S
        # A local claim replaces any guest severity state.
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
        """Deduplicate guest callouts using localized text. Local triggers take precedence.
        Matching guests share one callout at the highest severity. Emit immediately when
        local triggers are off.
        """
        if not text:
            return
        loc = self._localize_text(text)
        key = self._callout_dedup_key(loc)
        now = time.monotonic()
        if self._callout_claimed.get(key, 0.0) > now:
            # A stronger guest can upgrade the alert severity without repeating speech.
            prev = self._guest_claim_sev.get(key)
            if (prev is not None
                    and _GUEST_SEVERITY_RANK.get(severity, 0)
                    > _GUEST_SEVERITY_RANK.get(prev, 0)):
                self._guest_claim_sev[key] = severity
                self._emit_alert(loc, severity)
            return
        pending = self._pending_guests.get(key)
        if pending is not None:
            # Keep one pending timer and raise its severity.
            timer, prev = pending
            if (_GUEST_SEVERITY_RANK.get(severity, 0)
                    > _GUEST_SEVERITY_RANK.get(prev, 0)):
                self._pending_guests[key] = (timer, severity)
            return
        if not self._triggers_enabled:
            self._flush_guest(loc, severity, key)
            return
        # Defer guests so a local callout can cancel them. Read severity at fire time to
        # include pending upgrades.
        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.timeout.connect(
            lambda _loc=loc, _key=key, _t=timer:
                self._flush_guest_deferred(_loc, _key, _t))
        self._pending_guests[key] = (timer, severity)
        timer.start(_GUEST_CALLOUT_DEFER_MS)

    def _clear_callout_dedup(self) -> None:
        """Clear claims and pending guests at encounter boundaries."""
        for timer, _sev in list(self._pending_guests.values()):
            try:
                timer.stop()
                timer.deleteLater()
            except Exception:  # noqa: BLE001
                pass
        self._pending_guests.clear()
        self._callout_claimed.clear()
        self._guest_claim_sev.clear()

    def _load_callout_defaults(self) -> dict:
        """Load shipped wording by source and trigger ID."""
        if not ac.CALLOUT_DEFAULTS_FILE.exists():
            return {}
        try:
            raw = json.loads(ac.CALLOUT_DEFAULTS_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError, RecursionError):
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
        """Return editable user overrides or None for unsupported sources."""
        if src == "triggevent":
            return self._triggevent_callout_edits
        if src == "triggernometry":
            return self._triggernometry_callout_edits
        return None

    def _callout_edits_for(self, src: str) -> "dict | None":
        """Merge shipped wording with user edits for reading. Use _apply_callout_edit to
        change it.
        """
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
        """Load Japanese callouts from the cache or bundle, preferring the newer version
        then the larger map. Replace dictionaries atomically for active readers.
        """
        best_key, parsed = None, {}
        for src in (ac._CALLOUTS_JA_CACHE, ac._CALLOUTS_JA_BUNDLE):
            try:
                cand = json.loads(src.read_text(encoding="utf-8"))
            except (OSError, ValueError, RecursionError):
                continue
            if not (isinstance(cand, dict) and isinstance(cand.get("callouts"), dict)):
                continue
            key = (updater.parse_version(str(cand.get("app_version") or "0")),
                   len(cand["callouts"]))
            if best_key is None or key > best_key:
                best_key, parsed = key, cand
        _clean = lambda m: {k: v for k, v in (m if isinstance(m, dict) else {}).items()
                            if isinstance(k, str) and isinstance(v, str) and v}
        self._callouts_ja = _clean(parsed.get("callouts"))          # id -> ja display
        self._callouts_phrases_ja = _clean(parsed.get("phrases"))   # callout-text -> ja display
        self._callouts_readings = _clean(parsed.get("readings"))    # ja display -> kana reading
        self._callouts_names_ja = _clean(parsed.get("names"))       # id -> ja trigger name
        self._callouts_names_text_ja = _clean(parsed.get("names_text"))  # english name -> ja
        # Compile tokenized phrase keys for resolved engine text. Try them only after
        # exact lookup fails.
        self._callouts_phrases_ja_patterns = _compile_phrase_patterns(self._callouts_phrases_ja)
        set_readings(self._callouts_readings)   # Apply kana readings to every speech call.

    def _refresh_callouts_ja_async(self) -> None:
        """Refresh cached Japanese callouts in the background. Retain existing data on
        failure and emit _callouts_ja_signal on completion.
        """
        ref = "main"
        url = f"https://raw.githubusercontent.com/{updater.REPO}/{ref}/assets/callouts_ja.json"

        def _fetch() -> None:
            changed = False
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "NyaaTriggers"})
                with urllib.request.urlopen(req, timeout=15) as resp:
                    raw = resp.read(_CALLOUTS_JA_MAX_BYTES + 1)
                if len(raw) > _CALLOUTS_JA_MAX_BYTES:
                    raise ValueError("callouts overlay too large")
                data = json.loads(raw)
                if not (isinstance(data, dict) and isinstance(data.get("callouts"), dict)):
                    raise ValueError("unexpected overlay format")
                _atomic_write_json(ac._CALLOUTS_JA_CACHE, data, indent=2)
                changed = True
            except Exception:
                changed = False
            self._callouts_ja_signal.emit(changed)

        threading.Thread(target=_fetch, daemon=True).start()

    def _on_callouts_ja_refreshed(self, changed: bool) -> None:
        """Reload callout translations and repaint the table. UI locale catalogs are
        separate.
        """
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
            # Limit the General checkbox to official untagged triggers. Custom ones
            # appear under Unsorted.
            out = [t for t in out
                   if not (t.id in self._local_ids and t.id not in self._official_ids)]
        return out

    def _fight_tv_ids(self, fight: str) -> list:
        return [e["id"] for e in self._engine_inventory
                if e.get("source") == "triggevent" and e.get("id")
                and self._engine_fight_tag(e) == fight]

    def _set_trigger_enabled(self, trigger: Trigger, enabled: bool) -> None:
        trigger.enabled = enabled
        if not enabled:
            for runner in list(getattr(self, "_status_timers", ())):
                if runner.trigger is trigger:
                    self._drop_status_timer(runner)
            for runner in list(getattr(self, "_seq_runners", ())):
                if runner.trigger is trigger:
                    self._drop_seq_runner(runner)

    def _set_fight_local(self, fight: str, enabled: bool) -> None:
        changed = False
        for t in self._fight_local_triggers(fight):
            if t.enabled != enabled:
                self._set_trigger_enabled(t, enabled)
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
        # Use a stored direction so individual fight edits cannot change the next global
        # toggle.
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
        self._set_sections_collapsed(not enable, "engine")
        self._refresh_table()
        self._update_fight_controls()

    def _toggle_global_local(self) -> None:
        enable = not self._global_local_on_flag
        self._global_local_on_flag = enable
        self._settings["global_local_on"] = enable
        for t in self._triggers:
            if t.enabled != enable:
                self._set_trigger_enabled(t, enable)
                self._local_ids.add(t.id)
        self._save_triggers()
        self._save_settings()
        if enable:
            # Restore the plugin schedule, respecting cactbot mode.
            self._push_timeline_to_plugin()
        else:
            # Stop the separate local timeline clock too. Preserve cactbot bars while
            # that mode is active.
            if not getattr(self, "_cactbot_mode", False):
                self._timeline.reset()
                self._clear_callout_dedup()
            self._push_timeline_to_plugin()
        self._set_sections_collapsed(not enable, "general", "dot", "local")
        self._refresh_table()
        self._update_fight_controls()

    def _on_fight_local_only_toggled(self, checked: bool) -> None:
        f = self._fight_cur
        self._set_fight_local(f, checked)
        self._set_sections_collapsed(not checked, *(("general", "dot") if f == "" else ("local",)))
        self._refresh_table()
        self._update_fight_controls()

    def _on_fight_tv_only_toggled(self, checked: bool) -> None:
        self._set_fight_tv(self._fight_cur, checked)
        self._set_sections_collapsed(not checked, "engine")
        self._refresh_table()
        self._update_fight_controls()

    def _append_group_header(self, key: str, label: str) -> None:
        """Add a section header that toggles its source group."""
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
        self._local_enabled = bool(enabled)
        # Cancel pending expiry warnings because their timers run independently of log
        # matching.
        if not self._local_enabled:
            self._clear_status_timers()
            self._clear_seq_runners()
            self._clear_callout_dedup()
            # Reset the local clock while preserving an active Cactbot timeline.
            if not getattr(self, "_cactbot_mode", False):
                self._timeline.reset()
            # Clear the stopped local clock from the plugin while preserving the cactbot
            # schedule if active.
            self._push_timeline_to_plugin()
        else:
            # Restore the plugin schedule when reenabling, subject to the global switch.
            self._push_timeline_to_plugin()
        self._settings["local_enabled"] = self._local_enabled
        self._save_settings()

    def _set_triggers_enabled(self, enabled: bool) -> None:
        """Set the master mode for Local, Triggevent and Triggernometry callouts. Exclude
        cactbot without stopping the background Triggevent engine.
        """
        self._triggers_enabled = bool(enabled)
        if enabled:
            # Clear the saved cactbot flag so it cannot restart on the next launch.
            self._set_cactbot_enabled(False)
        self._set_local_enabled(enabled)
        if TriggeventBridge.is_available():
            self._set_triggevent_enabled(enabled)
        # Run imported Triggernometry packs alongside Local under this switch.
        if TriggernometryBridge is not None and TriggernometryBridge.is_available():
            self._set_triggernometry_enabled(enabled)

    def _on_callouts_localized_changed(self, state: int) -> None:
        # Refresh visible translations as well as the spoken language.
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
        # Set a default suffix because Qt does not infer one from the name filter.
        dlg.setDefaultSuffix("json")
        if dlg.exec() != QDialog.DialogCode.Accepted or not dlg.selectedFiles():
            return
        path = dlg.selectedFiles()[0]
        try:
            # Export through a sibling temporary file so interruption preserves the
            # previous file.
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
            imported = Path(path).read_bytes()
            data = json.loads(imported.decode("utf-8"))
            # Require an object before checking for the triggers key.
            if not isinstance(data, dict) or "triggers" not in data:
                raise ValueError(_("File is missing a 'triggers' key - not a NyaaTriggers export"))
            # Require a trigger list before replacing the local file.
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
            # Import through a sibling temporary file to avoid partial writes.
            tmp = ac.TRIGGERS_LOCAL_FILE.with_suffix(ac.TRIGGERS_LOCAL_FILE.suffix + ".tmp")
            tmp.write_bytes(imported)
            # Keep a backup because import replaces the complete local file.
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
        dlg.deleteLater()

    def _trigger_files_stamp(self) -> tuple:
        """Return size and nanosecond modification time for watched files, or None when
        missing.
        """
        stamp = []
        for p in _watched_trigger_files():
            try:
                st = p.stat()
                stamp.append((st.st_mtime_ns, st.st_size))
            except OSError:
                stamp.append(None)
        return tuple(stamp)

    def _maybe_reload_triggers(self) -> None:
        """Reload changed trigger files through the normal merge path and refresh the
        interface.
        """
        stamp = self._trigger_files_stamp()
        # Keep checking blocked files even when size and timestamp are unchanged.
        if stamp == self._triggers_mtime and not getattr(self, "_local_corrupt", False):
            return
        # Retry incomplete external writes on the next poll. Repository files can use
        # the bundled fallback.
        for p in (ac.TRIGGERS_FILE, ac.TRIGGERS_LOCAL_FILE):
            try:
                if p.exists():
                    json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError, RecursionError):
                return
        self._load_triggers()

    def _localized_callout(self, t: Trigger) -> str:
        """Select a callout template for the active language while preserving substitution
        tokens. Prefer translations by ID for unchanged official text, then phrase
        translations. Preserve custom text.
        """
        if not self._settings.get("callouts_localized", active_locale() == "ja"):
            return t.tts_text
        official = getattr(self, "_official_triggers", {}).get(t.id)
        if official is not None and official.tts_text == t.tts_text:
            translated = self._callouts_ja.get(t.id)
            if translated:
                return translated
        return self._callouts_phrases_ja.get(t.tts_text) or t.tts_text

    def _pick_fight_folder(self) -> str | None:
        """Return the chosen folder, an empty string for uncategorised, or None on
        cancellation.
        """
        known = {t.fight for t in self._triggers if t.fight}
        catalog = fight_catalog.load_catalog(
            _FIGHT_TREE, known, ac._DATA_DIR / "fight_catalog.json")
        fight_catalog.refresh_from_cactbot_async(ac._DATA_DIR / "fight_catalog.json")
        dlg = fight_catalog.FightPickerDialog(catalog, self)
        accepted = dlg.exec() == QDialog.DialogCode.Accepted
        folder = dlg.selected_folder() if accepted else None
        dlg.deleteLater()
        return folder
