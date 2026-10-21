from copy import deepcopy
from dataclasses import dataclass, replace
import json
from pathlib import Path
import uuid

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QMessageBox

from nyaatriggers import app_common as ac
from nyaatriggers import trigger_conversion as conversion
from nyaatriggers import triggevent_custom as custom
from nyaatriggers.locale_util import _
from nyaatriggers.trigger_engine import Trigger
from nyaatriggers.triggernometry_bridge import packs_dir
from nyaatriggers.triggernometry_editor import (
    PackDocument, normalized_id, parse_xml, prepared_packs, trigger_entries,
    script_trigger_references, validate_native, validate_pack, xml_bytes,
)
from nyaatriggers.ui.triggernometry_editor import speech_rows


def _read_optional(path):
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _json_bytes(data):
    return json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8")


@dataclass
class _Change:
    path: Path
    before: bytes | None
    after: bytes
    write: object


class _MoveRollbackError(ValueError):
    pass


def _persist_move(changes):
    attempted = []
    try:
        for change in changes:
            if _read_optional(change.path) != change.before:
                raise ValueError(_("A trigger file changed outside the program. Reload it before moving this trigger."))
        for change in changes:
            if any(_read_optional(previous.path) != previous.after for previous in attempted):
                raise ValueError(_("A saved trigger file changed again. The move was stopped to preserve the source."))
            if _read_optional(change.path) != change.before:
                raise ValueError(_("A trigger file changed outside the program. Reload it before moving this trigger."))
            attempted.append(change)
            change.write()
            if _read_optional(change.path) != change.after:
                raise ValueError(_("A saved trigger file changed again. The move was stopped to preserve the source."))
    except (OSError, ValueError, TypeError, RecursionError) as exc:
        errors = []
        for change in reversed(attempted):
            try:
                current = _read_optional(change.path)
                if current == change.before:
                    continue
                if current != change.after:
                    raise ValueError(_("The file changed again and was left untouched"))
                if change.before is None:
                    change.path.unlink()
                else:
                    ac._atomic_write_bytes(change.path, change.before)
            except (OSError, ValueError) as rollback:
                errors.append(f"{change.path}: {rollback}")
        if errors:
            raise _MoveRollbackError(_("The move failed and some files could not be restored. Review these files before restarting:\n{files}")
                                     .format(files="\n".join(errors))) from exc
        raise


class TriggerConversionMixin:
    def _add_engine_move_menu(self, menu, key):
        source = key.split(":", 1)[0] if self._is_engine_key(key) else "local"
        submenu = menu.addMenu(_("Move to engine"))
        for target, label in (("local", _("Local")), ("triggevent", _("Triggevent")),
                              ("triggernometry", _("Triggernometry"))):
            action = submenu.addAction(label)
            action.setEnabled(target != source)
            action.triggered.connect(lambda _checked=False, destination=target:
                                     self._move_trigger_to_engine(key, destination))

    def _conversion_native_source(self, key):
        ident = normalized_id(key.split(":", 1)[1].split("#", 1)[0])
        directory = packs_dir()
        paths = [path for path in directory.iterdir() if path.is_file() and path.suffix.lower() == ".xml"]
        matches, all_roots = [], []
        for path, raw in prepared_packs(paths):
            root = parse_xml(raw)
            all_roots.append((path, root))
            for _folder, trigger, _parent in trigger_entries(root):
                if normalized_id(trigger.get("Id")) == ident:
                    matches.append(path)
        if len(matches) != 1:
            raise ValueError(_("Could not identify one pack for this trigger. Reload its pack before moving it."))
        aliases = {ident}
        for path, root in all_roots:
            if path == matches[0]:
                aliases.update(normalized_id(trigger.get("NyaaSourceId"))
                               for _folder, trigger, _parent in trigger_entries(root)
                               if normalized_id(trigger.get("Id")) == ident)
        aliases.discard("")
        for path, root in all_roots:
            if next(script_trigger_references(root, aliases), None) is not None:
                raise ValueError(_("A Triggernometry script contains this trigger's ID. Update that script before moving the trigger."))
            for _folder, trigger, _parent in trigger_entries(root):
                if path == matches[0] and normalized_id(trigger.get("Id")) == ident:
                    continue
                if any(action.get("ActionType") == "Trigger"
                       and action.get("TriggerOp") != "CancelAllTrigger"
                       and normalized_id(action.get("TriggerId")) == ident
                       for action in trigger.iter("Action")):
                    raise ValueError(_("Another Triggernometry trigger references this trigger. Update that reference before moving it."))
        document = PackDocument.load(matches[0])
        return document, ident

    def _move_trigger_to_engine(self, key, target_source):
        try:
            return self._perform_engine_move(key, target_source)
        except (OSError, ValueError, TypeError, RecursionError) as exc:
            QMessageBox.warning(self, _("Could not move trigger"), str(exc))
            return False

    def _perform_engine_move(self, key, target_source):
        if target_source not in ("local", "triggevent", "triggernometry"):
            raise ValueError(_("Choose a supported engine"))
        if not isinstance(key, str):
            raise ValueError(_("Select a trigger before moving it"))
        source = key.split(":", 1)[0] if self._is_engine_key(key) else "local"
        if source == target_source:
            return True
        if getattr(self, "_in_game_combat", False):
            raise ValueError(_("Move triggers after combat ends so pending actions cannot overlap the converted trigger."))
        local_source = custom_source = document = None
        local_definition = None
        original_settings = deepcopy(self._settings)
        native_ident = ""
        custom_rows = custom.load_triggers() if "triggevent" in (source, target_source) else None
        if source == "local":
            local_source = next((trigger for trigger in self._triggers if trigger.id == key), None)
            if local_source is None:
                raise ValueError(_("This trigger no longer exists. Reload before moving it."))
            local_definition = deepcopy(local_source.to_dict())
            portable = conversion.from_local(local_source)
        elif source == "triggevent":
            custom_source = self._custom_triggevent_for_key(key)
            if custom_source is None:
                raise ValueError(_("This Triggevent trigger has no editable definition to convert. Its engine code remains in Triggevent."))
            current = next((row for row in custom_rows if row["id"] == custom_source["id"]), None)
            if current != custom_source:
                raise ValueError(_("This callout changed outside the editor. Reopen it before moving it."))
            portable = conversion.from_triggevent(custom_source)
            text = self._callout_edits_for(source).get(custom_source["id"], portable.tts_text)
            portable = replace(portable, enabled=custom_source["id"] not in self._engine_disabled[source],
                               tts_text=text)
        elif source == "triggernometry":
            document, native_ident = self._conversion_native_source(key)
            entry = self._engine_entry_for_key(key)
            current_rows = speech_rows(document.root)
            previous_rows = [row for row in self._engine_inventory
                             if row.get("source") == source and row.get("id", "").split("#", 1)[0] == native_ident]

            def signature(rows):
                return {row["id"]: (row.get("name", ""), row.get("text", ""), self._engine_fight_tag(row))
                        for row in rows if row.get("id", "").split("#", 1)[0] == native_ident}

            bridge = getattr(self, "_triggernometry", None)
            if (entry is None or signature(previous_rows) != signature(current_rows)
                    or bridge is not None and bridge.is_active() and not bridge.pack_is_current(document.path)):
                raise ValueError(_("This Triggernometry definition changed. Reload its pack before moving the trigger."))
            portable = conversion.from_triggernometry(document.root, native_ident,
                                                     fight=self._engine_fight_tag(entry) if entry else "",
                                                     text_overrides=self._callout_edits_for(source))
            muted = any(ident.split("#", 1)[0] == native_ident for ident in self._engine_disabled[source])
            portable = replace(portable, enabled=portable.enabled and not muted)
        else:
            raise ValueError(_("This engine does not provide editable trigger definitions to convert."))
        display = getattr(self, "_trigger_display_edits", {}).get(key)
        display_portable = None
        if display is not None:
            if source == "local":
                display_portable = conversion.from_local(Trigger.from_dict({**local_definition, "tts_text": display}))
            elif source == "triggevent":
                definition = deepcopy(custom_source)
                definition["steps"][0]["text"] = display
                display_portable = conversion.from_triggevent(definition)
            else:
                display_portable = conversion.from_triggernometry(
                    document.root, native_ident, text_overrides={native_ident + "#0": display})
        converter = {"local": conversion.to_local, "triggevent": conversion.to_triggevent,
                     "triggernometry": conversion.to_triggernometry}[target_source]
        while True:
            try:
                effective = replace(portable, enabled=True) if target_source == "triggernometry" else portable
                destination = converter(effective)
                break
            except conversion.ConversionAdjustmentRequired as adjustment:
                answer = QMessageBox.question(
                    self, _("Review trigger conversion"),
                    _("Moving {name} requires this change:\n\n{change}\n\nMove the trigger with this change?")
                    .format(name=portable.name, change=adjustment.description),
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No)
                if answer != QMessageBox.StandardButton.Yes:
                    return False
                portable = replace(adjustment.adjusted, enabled=portable.enabled)
        display_text = None
        if display_portable is not None:
            visual = converter(replace(effective, tts_text=display_portable.tts_text))
            if target_source == "local":
                display_text = visual.tts_text
            elif target_source == "triggevent":
                display_text = visual["steps"][0]["text"]
            else:
                display_text = speech_rows(visual)[0]["text"]
        if self._settings != original_settings:
            raise ValueError(_("Your settings changed while conversion was open. Review the trigger again before moving it."))
        if getattr(self, "_in_game_combat", False):
            raise ValueError(_("Combat started while conversion was open. Move this trigger after combat ends."))
        if document is not None and _read_optional(document.path) != document.original:
            raise ValueError(_("This pack changed outside the editor. Reopen it before moving it."))
        if document is not None:
            self._conversion_native_source(key)
        if custom_source is not None and next((row for row in custom.load_triggers()
                                              if row["id"] == custom_source["id"]), None) != custom_source:
            raise ValueError(_("This callout changed outside the editor. Reopen it before moving it."))
        if local_source is not None and (not any(trigger is local_source for trigger in self._triggers)
                                         or local_source.to_dict() != local_definition):
            raise ValueError(_("This trigger changed while conversion was open. Reload before moving it."))
        return self._save_engine_move(source, target_source, portable, destination,
                                      local_source, custom_source, custom_rows, document, native_ident, display_text)

    def _save_engine_move(self, source, target, portable, destination,
                          local_source, custom_source, custom_rows, document, native_ident, display_text):
        triggers = list(self._triggers)
        local_ids = set(self._local_ids)
        deleted_ids = set(self._deleted_ids)
        settings = deepcopy(self._settings)
        if getattr(self, "_settings_backup_pending", False):
            raise ValueError(_("Repair the unreadable settings file before moving triggers."))
        disabled = {engine: set(ids) for engine, ids in self._engine_disabled.items()}
        old_native_rows = [row for row in self._engine_inventory if row.get("source") == "triggernometry"]
        changes = {}
        local_payload = None
        if "local" in (source, target):
            from nyaatriggers.ui.triggers_tab import _read_local_triggers
            if getattr(self, "_local_corrupt", False):
                raise ValueError(_("Repair the unreadable local trigger file before moving triggers."))
            local_before = _read_optional(ac.TRIGGERS_LOCAL_FILE)
            current = _read_local_triggers() if local_before is not None else None
            if current != getattr(self, "_local_trigger_baseline", current):
                raise ValueError(_("Your local triggers changed outside the program. Wait for them to reload before moving triggers."))
            if local_source is not None:
                triggers.remove(local_source)
                local_ids.discard(local_source.id)
                if local_source.id in self._official_ids:
                    deleted_ids.add(local_source.id)
            if target == "local":
                triggers.append(destination)
                local_ids.add(destination.id)
            local_payload = self._local_trigger_payload(triggers, local_ids, deleted_ids)
            after = _json_bytes(local_payload)
            changes["local"] = _Change(ac.TRIGGERS_LOCAL_FILE, local_before, after,
                                        lambda payload=after: ac._atomic_write_bytes(ac.TRIGGERS_LOCAL_FILE, payload))
        if custom_rows is not None:
            custom_before = _read_optional(custom.CUSTOM_FILE)
            if custom.load_triggers() != custom_rows:
                raise ValueError(_("Your Triggevent callouts changed outside the program. Reload before moving triggers."))
            custom_rows = deepcopy(custom_rows)
            if custom_source is not None:
                custom_rows = [row for row in custom_rows if row["id"] != custom_source["id"]]
            if target == "triggevent":
                custom_rows.append(destination)
            custom.validate_document({"version": 1, "triggers": custom_rows})
            after = _json_bytes({"version": 1, "triggers": custom_rows})
            changes["triggevent"] = _Change(custom.CUSTOM_FILE, custom_before, after,
                                             lambda: custom.save_triggers(custom_rows))
        if source == "triggernometry":
            for _folder, trigger, parent in trigger_entries(document.root):
                if normalized_id(trigger.get("Id")) == native_ident:
                    parent.remove(trigger)
                    break
            validate_pack(document.root)
            after = xml_bytes(document.root)
            validate_native(after)
            changes["triggernometry"] = _Change(document.path, document.original, after, document.save)
        if target == "triggernometry":
            path = packs_dir() / ("converted-" + str(uuid.uuid4()) + ".xml")
            destination_document = PackDocument(path)
            destination_document.root = destination
            validate_pack(destination)
            after = xml_bytes(destination)
            validate_native(after)
            changes["triggernometry"] = _Change(path, None, after, destination_document.save)
        if source in ("triggevent", "triggernometry"):
            prefix = custom_source["id"] if custom_source is not None else native_ident
            old_ids = {ident for ident in disabled[source] if ident == prefix or ident.split("#", 1)[0] == prefix}
            disabled[source].difference_update(old_ids)
            edits = settings.get(source + "_callout_edits", {})
            settings[source + "_callout_edits"] = {ident: text for ident, text in edits.items()
                                                    if ident != prefix and ident.split("#", 1)[0] != prefix}
        if target == "triggevent":
            destination_key = "triggevent:" + destination["id"]
            if not portable.enabled:
                disabled[target].add(destination["id"])
        elif target == "triggernometry":
            rows = speech_rows(destination)
            if not rows:
                raise ValueError(_("The converted trigger has no speech action"))
            destination_key = "triggernometry:" + rows[0]["id"]
            if not portable.enabled:
                disabled[target].update(row["id"] for row in rows)
        else:
            destination_key = destination.id
        display_edits = dict(getattr(self, "_trigger_display_edits", {}))
        if local_source is not None:
            display_edits.pop(local_source.id, None)
        else:
            prefix = source + ":" + (custom_source["id"] if custom_source is not None else native_ident)
            display_edits = {key: value for key, value in display_edits.items()
                             if key != prefix and not key.startswith(prefix + "#")}
        if display_text is not None:
            display_edits[destination_key] = display_text
        if display_edits or "trigger_display_edits" in settings:
            settings["trigger_display_edits"] = display_edits
        for engine, ids in disabled.items():
            settings[engine + "_disabled_triggers"] = sorted(ids)
        settings_before = _read_optional(ac._SETTINGS_FILE)
        settings_after = _json_bytes(settings)
        settings_change = _Change(ac._SETTINGS_FILE, settings_before, settings_after,
                                  lambda: ac._atomic_write_bytes(ac._SETTINGS_FILE, settings_after))
        ordered = [changes[target], changes[source], settings_change]
        bridge = getattr(self, "_triggernometry", None)
        was_active = bool(bridge and bridge.is_active()) if "triggernometry" in (source, target) else False
        if was_active:
            bridge.stop(wait=True)
        try:
            _persist_move(ordered)
        except (OSError, ValueError, TypeError, RecursionError) as exc:
            if was_active and not isinstance(exc, _MoveRollbackError):
                self._set_triggernometry_enabled(True)
            raise
        if local_source is not None:
            self._set_trigger_enabled(local_source, False)
        self._triggers = triggers
        self._local_ids = local_ids
        self._deleted_ids = deleted_ids
        if local_payload is not None:
            self._local_trigger_baseline = deepcopy(local_payload)
            self._triggers_mtime = ()
        if custom_rows is not None:
            self._custom_triggevent = custom_rows
        self._settings = settings
        self._trigger_display_edits = display_edits
        for engine, ids in disabled.items():
            self._engine_disabled[engine].clear()
            self._engine_disabled[engine].update(ids)
            if engine in ("triggevent", "triggernometry"):
                edits = settings.get(engine + "_callout_edits", {})
                setattr(self, "_" + engine + "_callout_edits", edits)
                settings[engine + "_callout_edits"] = edits
        if custom_source is not None and getattr(self, "_triggevent", None) is not None:
            self._triggevent.cancel_speech([custom_source["id"]])
        if custom_rows is not None:
            self._apply_engine_disabled("triggevent")
            self._sync_custom_triggevent()
        if "triggernometry" in (source, target):
            self._update_triggernometry_pack_inventory(old_native_rows, [], directory=packs_dir())
            self._triggernometry_reload_pending = True
            self._apply_pending_triggernometry_packs()
        self._src_collapsed[{"local": "local", "triggevent": "engine", "triggernometry": "triggernometry"}[target]] = False
        self._refresh_table()
        self._restore_tree_selection(portable.fight, "custom_group" if not portable.fight else None)
        for row in range(self._table.rowCount()):
            item = self._table.item(row, ac._C_EN)
            if item is not None and item.data(Qt.ItemDataRole.UserRole) == destination_key:
                self._table.selectRow(row)
                self._table.scrollToItem(item)
                break
        self._update_fight_controls()
        return True
