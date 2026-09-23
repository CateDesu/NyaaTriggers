"""Create and manage callouts made with the Triggevent builder."""

from copy import deepcopy
import re
import uuid

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QInputDialog, QLabel, QMenu, QMessageBox

from nyaatriggers import triggevent_custom as custom
from nyaatriggers.locale_util import _
from nyaatriggers.triggevent_dialog import TriggeventDialog
from nyaatriggers.app_common import _C_EN, _stale_gen


class CustomTriggeventMixin:
    def _init_custom_triggevent(self):
        self._custom_triggevent = []
        self._custom_triggevent_error = ""
        self._custom_triggevent_dialog = None
        try:
            self._custom_triggevent = custom.load_triggers()
        except (OSError, ValueError, RecursionError) as exc:
            self._custom_triggevent_error = str(exc)

    def _setup_custom_triggevent_controls(self, layout):
        menu = QMenu(self._add_btn)
        menu.addAction(_("Local trigger..."), self._add_trigger)
        menu.addAction(_("Triggevent callout..."), self._add_custom_triggevent)
        menu.addAction(_("Triggernometry trigger..."), self._add_triggernometry_trigger)
        menu.addSeparator()
        menu.addAction(_("Edit Triggernometry pack..."), self._manage_triggernometry_packs)
        self._add_btn.setMenu(menu)
        self._custom_triggevent_status_label = QLabel()
        self._custom_triggevent_status_label.setWordWrap(True)
        self._custom_triggevent_status_label.setStyleSheet("color:#f9e2af;")
        layout.addWidget(self._custom_triggevent_status_label)
        self._triggernometry_editor_status = QLabel()
        self._triggernometry_editor_status.setWordWrap(True)
        self._triggernometry_editor_status.hide()
        layout.addWidget(self._triggernometry_editor_status)
        self._show_custom_triggevent_status()

    def _custom_triggevent_for_key(self, key):
        if not isinstance(key, str) or not key.startswith("triggevent:nyaa:"):
            return None
        ident = key.split(":", 1)[1]
        return next((row for row in getattr(self, "_custom_triggevent", []) if row["id"] == ident), None)

    def _merge_custom_triggevent_inventory(self):
        self._engine_inventory = [row for row in self._engine_inventory
                                  if not (row.get("source") == "triggevent"
                                          and str(row.get("id", "")).startswith("nyaa:"))]
        for row in self._custom_triggevent:
            words = [text for step in row["steps"] if step["kind"] == "callout"
                     for text in (step["text"], step["otherwise"]) if text]
            self._engine_inventory.append({
                "source": "triggevent", "id": row["id"], "name": row["name"],
                "fight": row["fight"], "group": "", "text": " / ".join(words),
            })

    def _on_custom_triggevent_ready(self, generation):
        bridge = self._triggevent
        if _stale_gen(bridge, generation):
            return
        if not bridge.supports_custom_triggers():
            if self._custom_triggevent:
                self._custom_triggevent_error = _("Update the Triggevent engine and restart to run builder callouts.")
                self._show_custom_triggevent_status()
            return
        self._sync_custom_triggevent()

    def _sync_custom_triggevent(self):
        bridge = getattr(self, "_triggevent", None)
        if bridge is None or not hasattr(self, "_custom_triggevent"):
            return
        if not bridge.supports_custom_triggers():
            self._show_custom_triggevent_status()
            return
        enabled = getattr(self, "_triggevent_mode", False) and not getattr(self, "_cactbot_mode", False)
        disabled = self._engine_disabled.get("triggevent", set())
        bridge.set_custom_triggers([row for row in self._custom_triggevent
                                    if enabled and row["id"] not in disabled])

    def _on_custom_triggevent_status(self, ok, message, generation):
        if _stale_gen(self._triggevent, generation):
            return
        if ok:
            try:
                custom.load_triggers()
            except (OSError, ValueError, RecursionError):
                return
        self._custom_triggevent_error = "" if ok else message
        self._show_custom_triggevent_status()

    def _custom_triggevent_status_text(self):
        if self._custom_triggevent_error:
            return _("Custom Triggevent callouts: {error}").format(error=self._custom_triggevent_error)
        bridge = getattr(self, "_triggevent", None)
        if bridge is None or not bridge.is_active():
            return _("Callouts are saved for the next Triggevent engine start.")
        if not bridge.supports_custom_triggers():
            return _("Waiting for Triggevent. If this persists, update the engine and restart.")
        if not getattr(self, "_triggevent_mode", False) or getattr(self, "_cactbot_mode", False):
            return _("Triggevent callouts are currently switched off.")
        return ""

    def _show_custom_triggevent_status(self):
        text = self._custom_triggevent_status_text()
        label = getattr(self, "_custom_triggevent_status_label", None)
        if label is not None:
            label.setText(text)
            label.setVisible(bool(text) and bool(self._custom_triggevent or self._custom_triggevent_error))
        dialog = self._custom_triggevent_dialog
        if dialog is not None:
            dialog.status.setText(text)

    def _add_custom_triggevent(self):
        self._edit_custom_triggevent()

    def _edit_custom_triggevent(self, original=None, *, duplicate=False):
        try:
            rows = custom.load_triggers()
        except (OSError, ValueError, RecursionError) as exc:
            QMessageBox.warning(self, _("Could not read Triggevent callouts"),
                                _("Your saved callouts could not be read. Fix the file before editing:\n{path}\n\n{error}")
                                .format(path=custom.CUSTOM_FILE, error=exc))
            return
        self._custom_triggevent = rows
        self._custom_triggevent_error = ""
        self._sync_custom_triggevent()
        self._refresh_table()
        if original is not None:
            original = next((row for row in rows if row["id"] == original["id"]), None)
            if original is None:
                QMessageBox.warning(self, _("Callout removed"), _("This callout was removed outside the editor."))
                return
        definition = deepcopy(original)
        if duplicate:
            definition["id"] = "nyaa:" + str(uuid.uuid4())
            definition["name"] = definition["name"][:190] + _(" (copy)")
        expected = None if duplicate else deepcopy(original)
        dialog = TriggeventDialog(
            self, definition,
            status=self._custom_triggevent_status_text(),
            preview=self._preview_custom_triggevent,
            save=lambda updated: self._save_custom_triggevent(updated, expected))
        self._custom_triggevent_dialog = dialog
        try:
            dialog.exec()
        finally:
            self._custom_triggevent_dialog = None
            dialog.deleteLater()

    def _preview_custom_triggevent(self, text):
        values = {"source": _("the boss"), "start.source": _("the boss"),
                  "target": _("you"), "start.target": _("you"), "player": _("you"),
                  "id": "B123", "start.id": "A123"}
        self._speak_engine_preview(re.sub(r"\{([^{}]*)\}", lambda m: values.get(m[1], ""), text))

    def _test_custom_triggevent(self, definition):
        texts = [text for step in definition["steps"] if step["kind"] == "callout"
                 for text in (step["text"], step["otherwise"] if step["condition"] != "always" else "") if text]
        text, accepted = QInputDialog.getItem(self, _("Preview callout"),
                                            _("Choose the speech to preview. This does not test event matching."),
                                            texts, editable=False)
        if accepted:
            self._preview_custom_triggevent(text)

    def _save_custom_triggevent(self, updated, original=None):
        rows = custom.load_triggers()
        current = next((row for row in rows if row["id"] == updated["id"]), None)
        if current != original:
            raise ValueError(_("This callout changed outside the editor. Reopen it before saving."))
        rows = [updated if row["id"] == updated["id"] else row for row in rows]
        if current is None:
            rows.append(updated)
        custom.save_triggers(rows)
        self._custom_triggevent = rows
        self._custom_triggevent_error = ""
        self._src_collapsed["engine"] = False
        self._sync_custom_triggevent()
        self._refresh_table()
        self._show_custom_triggevent_status()
        if hasattr(self, "_table"):
            self._restore_tree_selection(updated["fight"], "custom_group" if not updated["fight"] else None)
            key = "triggevent:" + updated["id"]
            for index in range(self._table.rowCount()):
                item = self._table.item(index, _C_EN)
                if item is not None and item.data(Qt.ItemDataRole.UserRole) == key:
                    self._table.selectRow(index)
                    self._table.scrollToItem(item)
                    break

    def _delete_custom_triggevent(self, original):
        if QMessageBox.question(self, _("Delete Triggevent callout"),
                                _('Delete "{name}"?').format(name=original["name"])) != QMessageBox.StandardButton.Yes:
            return
        try:
            rows = custom.load_triggers()
            current = next((row for row in rows if row["id"] == original["id"]), None)
            if current != original:
                raise ValueError(_("This callout changed outside the editor. Reopen it before saving."))
            rows = [row for row in rows if row["id"] != original["id"]]
            custom.save_triggers(rows)
        except (OSError, ValueError, RecursionError) as exc:
            QMessageBox.warning(self, _("Could not save Triggevent callout"), str(exc))
            return
        self._custom_triggevent = rows
        self._sync_custom_triggevent()
        self._refresh_table()
        self._show_custom_triggevent_status()

    def _custom_triggevent_menu(self, key, global_pos):
        original = self._custom_triggevent_for_key(key)
        menu = QMenu(self._table)
        edit = menu.addAction(_("Edit sequence..."))
        duplicate = menu.addAction(_("Duplicate"))
        preview = menu.addAction(_("Test speech"))
        delete = menu.addAction(_("Delete"))
        chosen = menu.exec(global_pos)
        if chosen is edit:
            self._edit_custom_triggevent(original)
        elif chosen is duplicate:
            self._edit_custom_triggevent(original, duplicate=True)
        elif chosen is preview:
            self._test_custom_triggevent(original)
        elif chosen is delete:
            self._delete_custom_triggevent(original)
