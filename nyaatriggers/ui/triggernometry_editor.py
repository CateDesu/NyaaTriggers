import sys
import uuid

from PyQt6.QtWidgets import QInputDialog, QMessageBox

from nyaatriggers.locale_util import _
from nyaatriggers.triggernometry_dialog import TriggernometryDialog
from nyaatriggers.triggernometry_editor import PackDocument, normalized_id, parse_xml, trigger_entries
from nyaatriggers.triggernometry_bridge import packs_dir


def pack_paths():
    return sorted(path for path in packs_dir().iterdir() if path.is_file() and path.suffix.lower() == ".xml")


def speech_rows(root):
    rows = []
    fights = {id(folder.find("Triggers")): folder.get("Name", "") for folder in root.iter()
              if folder.tag in ("ExportedFolder", "Folder")}
    for _path, trigger, parent in trigger_entries(root):
        ident = normalized_id(trigger.get("Id"))
        if not ident:
            continue
        index = 0
        for action in trigger.findall("Actions/Action"):
            text = action.get("UseTTSTextExpression", "")
            if action.get("ActionType") != "UseTTS" or not text:
                continue
            rows.append({"source": "triggernometry", "id": ident + "#" + str(index),
                         "name": trigger.get("Name", ""), "fight": fights[id(parent)], "group": "", "text": text})
            index += 1
    return rows


def pack_speech_rows(directory):
    rows = []
    incomplete = False
    for path in sorted(directory.iterdir()):
        if path.is_file() and path.suffix.lower() == ".xml":
            try:
                rows.extend(speech_rows(PackDocument.load(path).root))
            except (OSError, ValueError, RecursionError) as exc:
                incomplete = True
                print(f"[triggernometry] could not read pack rows: {exc}", file=sys.stderr)
    return rows, incomplete


class TriggernometryEditorMixin:
    def _add_triggernometry_trigger(self):
        try:
            document = PackDocument(packs_dir() / ("custom-" + str(uuid.uuid4()) + ".xml"))
            document.add_trigger()
            self._open_triggernometry_document(document)
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, _("Could not open Triggernometry pack"), str(exc))

    def _manage_triggernometry_packs(self):
        try:
            paths = pack_paths()
            if not paths:
                self._add_triggernometry_trigger()
                return
            name, accepted = QInputDialog.getItem(self, _("Edit Triggernometry pack"), _("Pack"),
                                                 [path.name for path in paths], editable=False)
            if accepted:
                self._open_triggernometry_document(PackDocument.load(next(path for path in paths if path.name == name)))
        except (OSError, ValueError, RecursionError) as exc:
            QMessageBox.warning(self, _("Could not open Triggernometry pack"), str(exc))

    def _edit_triggernometry_definition(self, key):
        ident = normalized_id(key.split(":", 1)[1].split("#", 1)[0])
        matches = []
        try:
            for path in pack_paths():
                try:
                    document = PackDocument.load(path)
                except (OSError, ValueError, RecursionError):
                    continue
                if ident and any(normalized_id(trigger.get("Id")) == ident for _path, trigger, _parent in trigger_entries(document.root)):
                    matches.append(document)
            if len(matches) != 1:
                raise ValueError(_("Could not identify one pack for this trigger. Open Edit Triggernometry pack to choose it."))
            self._open_triggernometry_document(matches[0], ident)
        except (OSError, ValueError, RecursionError) as exc:
            QMessageBox.warning(self, _("Could not open Triggernometry pack"), str(exc))

    def _open_triggernometry_document(self, document, selected_id=None):
        dialog = TriggernometryDialog(document, self, selected_id=selected_id, save=self._save_triggernometry_document)
        try:
            dialog.exec()
        finally:
            dialog.deleteLater()

    def _save_triggernometry_document(self, document):
        old_rows = speech_rows(parse_xml(document.original)) if document.original is not None else []
        new_rows = speech_rows(document.root)
        document.save()
        self._update_triggernometry_pack_inventory(old_rows, new_rows, directory=document.path.parent)
        self._triggernometry_reload_pending = True
        self._apply_pending_triggernometry_packs()

    def _update_triggernometry_pack_inventory(self, old_rows, new_rows, *, directory):
        current = [row for row in self._engine_inventory if row.get("source") == "triggernometry"]
        previous = {row["id"]: row for row in old_rows}
        # The file may already contain an external edit.
        previous.update((row["id"], row) for row in self._read_triggernometry_inventory_cache())
        previous.update((row["id"], row) for row in current)
        affected = {row["id"].split("#", 1)[0] for row in old_rows + new_rows}
        incomplete = True
        try:
            managed, incomplete = pack_speech_rows(directory)
        except (OSError, ValueError, RecursionError) as exc:
            print(f"[triggernometry] could not refresh all pack rows: {exc}", file=sys.stderr)
            old_rows = [row for row in previous.values() if row["id"].split("#", 1)[0] in affected]
            new_rows += [row for row in current if row["id"].split("#", 1)[0] not in affected]
        else:
            old_rows = list(previous.values())
            new_rows = managed
            if incomplete:
                affected.update(row["id"].split("#", 1)[0] for row in new_rows)
                old_rows = [row for row in old_rows if row["id"].split("#", 1)[0] in affected]
        available = {row["id"].split("#", 1)[0] for row in new_rows}
        unavailable = [row for row in previous.values() if row["id"].split("#", 1)[0] not in available] if incomplete else []
        self._reconcile_triggernometry_speech(old_rows, new_rows)
        self._engine_inventory = [row for row in self._engine_inventory if row.get("source") != "triggernometry"]
        self._engine_inventory.extend(new_rows)
        self._save_triggernometry_inventory_cache(unavailable=unavailable)
        self._src_collapsed["triggernometry"] = False
        self._refresh_table()

    def _reconcile_triggernometry_speech(self, old_rows, new_rows):
        old_ids = {row["id"] for row in old_rows}
        new_ids = {row["id"] for row in new_rows}
        texts = {row["id"]: row["text"] for row in new_rows}
        changed = {row["id"].split("#", 1)[0] for row in old_rows
                   if texts.get(row["id"]) != row["text"]}
        # Keep disabled speech disabled if edited actions move to a new position.
        muted = {ident.split("#", 1)[0] for ident in self._triggernometry_disabled & old_ids} & changed
        edits = self._triggernometry_callout_edits
        previous_edits = dict(edits)
        previous_disabled = set(self._triggernometry_disabled)
        for ident in old_ids | new_ids:
            if ident.split("#", 1)[0] in changed:
                edits.pop(ident, None)
        self._triggernometry_disabled.difference_update(old_ids - new_ids)
        self._triggernometry_disabled.update(ident for ident in new_ids if ident.split("#", 1)[0] in muted)
        if edits != previous_edits or self._triggernometry_disabled != previous_disabled:
            self._settings["triggernometry_callout_edits"] = edits
            self._settings["triggernometry_disabled_triggers"] = sorted(self._triggernometry_disabled)
            self._save_settings()

    def _apply_pending_triggernometry_packs(self):
        pending = getattr(self, "_triggernometry_reload_pending", False)
        label = getattr(self, "_triggernometry_editor_status", None)
        if label is not None:
            label.setText(_("Triggernometry pack saved. Reload will run after combat ends.") if pending else "")
            label.setVisible(pending)
        if not pending or getattr(self, "_in_game_combat", False):
            return
        self._triggernometry_reload_pending = False
        if label is not None:
            label.hide()
        self._set_triggernometry_enabled(False)
        if self._triggers_enabled and not getattr(self, "_cactbot_mode", False):
            self._set_triggernometry_enabled(True)
