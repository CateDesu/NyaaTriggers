from pathlib import Path
import json
import os
import shutil
import sys

from PyQt6.QtGui import QBrush, QColor, QTextCharFormat, QTextCursor
from PyQt6.QtWidgets import QDialog, QHBoxLayout, QLineEdit, QLabel

from nyaatriggers.trigger_engine import Trigger
from nyaatriggers.locale_util import _, active_locale
from nyaatriggers.plugin_link import DEFAULT_PORT, parse_port

from nyaatriggers import app_common as ac
from nyaatriggers.app_common import _AbilityData, _atomic_write_json, _fsync_file, _next_bad_name

_MAX_SETTINGS_BYTES = 4 << 20


class SettingsTabMixin:
    def _load_settings(self) -> None:
        self._settings_backup_pending = False
        if ac._SETTINGS_FILE.exists():
            bad = ""
            try:
                with ac._SETTINGS_FILE.open("rb") as fh:
                    data = fh.read(_MAX_SETTINGS_BYTES + 1)
                if len(data) > _MAX_SETTINGS_BYTES:
                    raise ValueError("settings file exceeds 4 MiB")
                self._settings = json.loads(data.decode("utf-8"))
            except (OSError, ValueError, RecursionError) as exc:
                self._settings = {}
                bad = str(exc)
            if not isinstance(self._settings, dict):
                self._settings = {}
                bad = bad or "not a settings object"
            if bad:
                # Preserve unreadable settings before defaults can overwrite them.
                backup = _next_bad_name(ac._SETTINGS_FILE)
                try:
                    shutil.copy2(ac._SETTINGS_FILE, backup)
                except OSError:
                    backup = None
                    self._settings_backup_pending = True
                # Apply the saved language before displaying warnings.
                self._settings_load_warning = (bad, backup)
            # Older settings predate the local trigger switch.
            if self._settings and "local_enabled" not in self._settings:
                self._settings["local_enabled"] = True
            if self._settings.get("overlay_sound_file") == "__egg_sound__":
                self._settings["overlay_sound_file"] = "ding.wav"
            # Migrate only the old localhost default. OverlayPlugin does not listen on IPv6.
            if self._settings.get("ws_url") == "ws://localhost:10501/ws":
                self._settings["ws_url"] = "ws://127.0.0.1:10501/ws"
            for key in ("triggers_enabled", "triggevent_enabled", "update_channel"):
                self._settings.pop(key, None)

    def _save_settings(self) -> bool:
        try:
            if getattr(self, "_settings_backup_pending", False):
                if ac._SETTINGS_FILE.exists():
                    shutil.copy2(ac._SETTINGS_FILE, _next_bad_name(ac._SETTINGS_FILE))
                self._settings_backup_pending = False
            _atomic_write_json(ac._SETTINGS_FILE, self._settings, indent=2)
            self._settings_save_failed = False
            return True
        except (OSError, TypeError, ValueError, RecursionError) as exc:
            self._settings_save_failed = True
            self._warn_save_failed(_("settings"), exc)
            return False

    def _save_settings_debounced(self) -> None:
        self._settings_save_timer.start()

    def _warn_save_failed(self, what: str, exc: Exception) -> None:
        print(f"[NyaaTriggers] could not save {what}: {exc}", file=sys.stderr)
        if self._save_warned:
            return
        self._save_warned = True
        ac.persistence_warning(
            self, _("Save Failed"),
            _("Could not save {what}. Changes will be lost when the program closes.\n"
              "{err}\n\nCheck that the folder is writable.").format(what=what, err=str(exc)))

    def _build_plugin_link_settings(self, layout) -> None:
        self._settings_header(layout, _("In-Game Overlay"))
        self._plugin_link_status_lbl = QLabel(_("● Off"))
        self._plugin_link_status_lbl.setStyleSheet("color:#8f8f9a; font-weight:bold;")
        layout.addWidget(self._plugin_link_status_lbl)
        repo_lbl = QLabel(
            '<a href="https://github.com/CateDesu/NyaaTriggers-Overlay">'
            'github.com/CateDesu/NyaaTriggers-Overlay</a>')
        repo_lbl.setOpenExternalLinks(True)
        layout.addWidget(repo_lbl)
        port_row = QHBoxLayout()
        port_row.addWidget(QLabel(_("Port:")))
        saved_port = parse_port(self._settings.get("plugin_port"))
        self._plugin_port_edit = QLineEdit(
            str(saved_port if saved_port is not None else DEFAULT_PORT))
        self._plugin_port_edit.setMaximumWidth(90)
        self._plugin_port_edit.editingFinished.connect(self._on_plugin_port_changed)
        port_row.addWidget(self._plugin_port_edit)
        port_note = QLabel(_("Must match the port in the game plugin"))
        port_note.setWordWrap(True)
        port_note.setStyleSheet("color:#8f8f9a;")
        port_row.addWidget(port_note, stretch=1)
        layout.addLayout(port_row)
        self._update_plugin_link_status_label(*self._plugin_link.last_status())

    def _settings_header(self, layout, title: str) -> None:
        lbl = QLabel(title)
        lbl.setStyleSheet(
            "color:#ff8399; font-weight:bold; font-size:13px; "
            "margin-top:10px; padding-bottom:3px; border-bottom:1px solid #26262e;")
        layout.addWidget(lbl)

    def _set_me_name(self, name: str) -> None:
        name = name.strip()
        self._me_name = name
        self._settings["char_name"] = name
        self._save_settings()
        if hasattr(self, "_char_edit") and self._char_edit.text().strip() != name:
            self._char_edit.setText(name)

    def _on_char_name_changed(self) -> None:
        self._set_me_name(self._char_edit.text())

    def _on_ui_language_changed(self, _idx: int) -> None:
        lang = self._ui_lang_combo.currentData() or "auto"
        if lang == self._settings.get("ui_language", "auto"):
            return
        self._settings["ui_language"] = lang
        self._save_settings()
        if ac.QMessageBox.question(
                self, _("Restart NyaaTriggers"),
                _("The interface language changed. Restart NyaaTriggers now to apply it?"),
        ) == ac.QMessageBox.StandardButton.Yes:
            self._restart_for_update()

    def _save_raw_log(self) -> None:
        """Export the captured feed, including lines hidden by filters."""
        lines = list(self._raw_capture)
        if not lines:
            ac.QMessageBox.information(
                self, _("Save Log"),
                _("No captured log lines yet - connect and run a pull first."))
            return
        dlg = ac.QFileDialog(self, _("Save Log"), "nyaa_log.txt",
                          _("Text files (*.txt);;All files (*)"))
        dlg.setAcceptMode(ac.QFileDialog.AcceptMode.AcceptSave)
        dlg.setDefaultSuffix("txt")
        if dlg.exec() != QDialog.DialogCode.Accepted or not dlg.selectedFiles():
            return
        path = dlg.selectedFiles()[0]
        try:
            # A sibling temporary file preserves the previous export if interrupted.
            dest = Path(path)
            tmp = dest.with_suffix(dest.suffix + ".tmp")
            try:
                tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
                _fsync_file(tmp)
                os.replace(tmp, dest)
            except OSError:
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    pass
                raise
        except OSError as exc:
            ac.QMessageBox.critical(self, _("Save Log"), _("Could not write file:\n{error}").format(error=exc))
            return
        message = (_("Saved {count} line to:\n{path}") if len(lines) == 1
                   else _("Saved {count} lines to:\n{path}"))
        ac.QMessageBox.information(
            self, _("Save Log"), message.format(count=len(lines), path=path))

    def _save_diagnostics(self) -> None:
        from nyaatriggers.diagnostics import export_diagnostics

        dlg = ac.QFileDialog(self, _("Save Diagnostics"), "nyaatriggers-diagnostics.jsonl",
                            _("Diagnostic logs (*.jsonl);;All files (*)"))
        dlg.setAcceptMode(ac.QFileDialog.AcceptMode.AcceptSave)
        dlg.setDefaultSuffix("jsonl")
        if dlg.exec() != QDialog.DialogCode.Accepted or not dlg.selectedFiles():
            return
        try:
            settings = dict(self._settings)
            for key, attr in (("triggevent_mode", "_triggevent_mode"),
                              ("triggernometry_mode", "_triggernometry_mode"),
                              ("connected", "_connected")):
                settings[key] = getattr(self, attr, None)
            mute = getattr(self, "_mute_btn", None)
            if mute is not None:
                settings["muted"] = mute.isChecked()
            count = export_diagnostics(Path(dlg.selectedFiles()[0]), settings)
        except (OSError, ValueError) as exc:
            ac.QMessageBox.critical(self, _("Save Diagnostics"),
                                   _("Could not write file:\n{error}").format(error=exc))
            return
        ac.QMessageBox.information(
            self, _("Save Diagnostics"),
            _("Saved {count} diagnostic records. Attach this file to your bug report. "
              "Combat logs, chat, character names, computer names, paths, and credentials are excluded.").format(count=count))

    def _write_ability_line(self, line: str, color: str,
                            log_type: str, ability_name: str, ability_id: str = "",
                            source: str = "", target: str = "") -> None:
        cursor = self._ability_log.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        if cursor.position() > 0:
            cursor.insertBlock()
        cursor.block().setUserData(_AbilityData(log_type, ability_name, ability_id, source, target))
        fmt = QTextCharFormat()
        fmt.setForeground(QBrush(QColor(color)))
        cursor.setCharFormat(fmt)
        cursor.insertText(line)
        self._ability_log.setTextCursor(cursor)
        self._ability_log.ensureCursorVisible()

    def _localize_text(self, text: str) -> str:
        """Translate exact text, then tokenized templates, preserving unmatched text."""
        if not text or not self._settings.get("callouts_localized", active_locale() == "ja"):
            return text
        ja = self._callouts_phrases_ja.get(text)
        if ja:
            return ja
        for pat, ja_val in self._callouts_phrases_ja_patterns:
            translated = pat.render(text, ja_val, self._callouts_phrases_ja)
            if translated is not None:
                return translated
        return text

    def _reading_for(self, text: str) -> str:
        """Return the kana reading, or the original text if unknown."""
        reading = self._callouts_readings.get(text)
        if reading:
            return reading
        for pattern, template in getattr(self, "_callouts_reading_patterns", ()):
            reading = pattern.render(text, template, self._callouts_readings)
            if reading is not None:
                return reading
        return text

    def _localized_name(self, t: Trigger) -> str:
        """Translate by trigger ID, then wording, or keep the original name."""
        if not self._settings.get("callouts_localized", active_locale() == "ja"):
            return t.name
        return (self._callouts_names_ja.get(t.id)
                or self._callouts_names_text_ja.get(t.name)
                or t.name)

    def _flush_pending_settings_save(self) -> None:
        if self._settings_save_timer.isActive() or getattr(self, "_settings_save_failed", False):
            self._settings_save_timer.stop()
            self._save_settings()
