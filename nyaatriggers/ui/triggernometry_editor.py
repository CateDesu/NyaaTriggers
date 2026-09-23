"""Open native packs from the trigger list and apply saved edits."""

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
    for path, trigger, _parent in trigger_entries(root):
        index = 0
        for action in trigger.findall("Actions/Action"):
            text = action.get("UseTTSTextExpression", "")
            if action.get("ActionType") != "UseTTS" or not text:
                continue
            rows.append({"source": "triggernometry", "id": str(uuid.UUID(trigger.get("Id"))) + "#" + str(index),
                         "name": trigger.get("Name", ""), "fight": path.split(" / ")[-1], "group": "", "text": text})
            index += 1
    return rows


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
        old_ids = {row["id"] for row in old_rows}
        new_ids = {row["id"] for row in new_rows}
        changed = {row["id"].split("#", 1)[0] for row in old_rows
                   if not any(new["id"] == row["id"] and new["text"] == row["text"] for new in new_rows)}
        # Keep disabled speech disabled if edited actions move to a new position.
        muted = {ident.split("#", 1)[0] for ident in self._triggernometry_disabled & old_ids} & changed
        document.save()
        edits = self._triggernometry_callout_edits
        for ident in old_ids | new_ids:
            if ident.split("#", 1)[0] in changed:
                edits.pop(ident, None)
        self._triggernometry_disabled.difference_update(old_ids - new_ids)
        self._triggernometry_disabled.update(ident for ident in new_ids if ident.split("#", 1)[0] in muted)
        self._settings["triggernometry_callout_edits"] = edits
        self._settings["triggernometry_disabled_triggers"] = sorted(self._triggernometry_disabled)
        self._save_settings()
        self._engine_inventory = [row for row in self._engine_inventory
                                  if row.get("source") != "triggernometry" or row.get("id") not in old_ids | new_ids]
        self._engine_inventory.extend(new_rows)
        self._save_triggernometry_inventory_cache()
        self._src_collapsed["triggernometry"] = False
        self._refresh_table()
        self._triggernometry_reload_pending = True
        self._apply_pending_triggernometry_packs()

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
