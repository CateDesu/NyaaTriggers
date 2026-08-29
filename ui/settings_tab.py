"""Settings pages and persistence, the language helpers and the
character name identity. Mixin for MainWindow, all state rides on self.
"""

from pathlib import Path
import json
import os
import shutil
import sys

from PyQt6.QtGui import QBrush, QColor, QTextCharFormat, QTextCursor
from PyQt6.QtWidgets import QDialog, QHBoxLayout, QLineEdit, QLabel

from trigger_engine import Trigger
from tts import speak
from locale_util import _, set_locale, active_locale
from plugin_link import DEFAULT_PORT, parse_port

import app_common as ac
from app_common import _AbilityData, _atomic_write_json, _fsync_file, _next_bad_name


class SettingsTabMixin:
    def _load_settings(self) -> None:
        if ac._SETTINGS_FILE.exists():
            bad = ""
            try:
                self._settings = json.loads(ac._SETTINGS_FILE.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                self._settings = {}
                bad = str(exc)
            if not isinstance(self._settings, dict):
                self._settings = {}
                bad = bad or "not a settings object"
            if bad:
                # A truncated or corrupt file, power loss mid-write, must not be
                # silently replaced with defaults on the next save. Keep a copy
                # for recovery and tell the user. Rotated .bad, .bad.1 and so on
                # so a second corruption doesn't overwrite the first copy.
                backup = _next_bad_name(ac._SETTINGS_FILE)
                try:
                    shutil.copy2(ac._SETTINGS_FILE, backup)
                except OSError:
                    backup = None
                # __init__ shows the warning once set_locale has run. The
                # locale comes from these settings, so the load cannot wait,
                # but the dialog should still speak the user's language.
                self._settings_load_warning = (bad, backup)
            # Upgrade migration. Local triggers used to be always-on. A non-empty
            # settings file without `local_enabled` is an upgrader, so keep their
            # triggers on instead of silently going quiet. Fresh installs stay off.
            if self._settings and "local_enabled" not in self._settings:
                self._settings["local_enabled"] = True
            # The hidden easter-egg alert sound was removed. Anyone who had it
            # selected persisted overlay_sound_file="__egg_sound__". Without this
            # it resolves to no file and alerts go silent, so coerce it back.
            if self._settings.get("overlay_sound_file") == "__egg_sound__":
                self._settings["overlay_sound_file"] = "ding.wav"
            # Migrate the old localhost default. Windows 11 with IPv6 resolves
            # localhost to ::1, where OverlayPlugin, bound to 127.0.0.1, is
            # unreachable, so the WS never connects. Only the exact old default
            # is rewritten. Custom URLs are kept as the user left them.
            if self._settings.get("ws_url") == "ws://localhost:10501/ws":
                self._settings["ws_url"] = "ws://127.0.0.1:10501/ws"
            # The Stable/Master/Rust update-channel choice was removed. Everyone
            # is on Stable now. Rewrite a stored "master" or "rust" so the stale
            # preference can't steer anything. Persisted on the next settings
            # save, like the other migrations above.
            if self._settings.get("update_channel") in ("master", "rust"):
                self._settings["update_channel"] = "stable"

    def _save_settings(self) -> None:
        try:
            _atomic_write_json(ac._SETTINGS_FILE, self._settings, indent=2)
        except OSError as exc:
            self._warn_save_failed(_("settings"), exc)

    def _save_settings_debounced(self) -> None:
        """Coalesce rapid-fire settings writes, slider drags, into one save."""
        self._settings_save_timer.start()

    def _warn_save_failed(self, what: str, exc: Exception) -> None:
        """Surface the first failed save once per session, say a read-only
        install dir. The UI shows edits that will silently vanish on restart."""
        print(f"[NyaaTriggers] could not save {what}: {exc}", file=sys.stderr)
        if self._save_warned:
            return
        self._save_warned = True
        ac.QMessageBox.warning(
            self, _("Save Failed"),
            _("Your {what} could not be written to disk, so changes will be "
              "lost when the app closes:\n{err}\n\nCheck that the folder is "
              "writable.").format(what=what, err=str(exc)))

    def _build_plugin_link_settings(self, layout) -> None:
        self._settings_header(layout, _("In-Game Overlay"))
        # Always on and auto-detected. No toggles here, just whether the game
        # plugin is talking to us right now, and where to get it.
        self._plugin_link_status_lbl = QLabel(_("● Off"))
        self._plugin_link_status_lbl.setStyleSheet("color:#8f8f9a; font-weight:bold;")
        layout.addWidget(self._plugin_link_status_lbl)
        repo_lbl = QLabel(
            '<a href="https://github.com/CateDesu/NyaaTriggers-Overlay">'
            'github.com/CateDesu/NyaaTriggers-Overlay</a>')
        repo_lbl.setOpenExternalLinks(True)
        layout.addWidget(repo_lbl)
        # Dual client setups move the second client's plugin off the default
        # port, this field points the app at it. Everyone else leaves it be.
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
        # Reflect the link's state as of Settings opening. Updates arrive via
        # status_changed from then on.
        self._update_plugin_link_status_label(*self._plugin_link.last_status())

    def _settings_header(self, layout, title: str) -> None:
        """Add a coral, hairline-underlined Settings section header."""
        lbl = QLabel(title)
        lbl.setStyleSheet(
            "color:#ff8399; font-weight:bold; font-size:13px; "
            "margin-top:10px; padding-bottom:3px; border-bottom:1px solid #26262e;")
        layout.addWidget(lbl)

    def _set_me_name(self, name: str) -> None:
        """Update the tracked local-player name, persist it, and reflect it in
        the Settings field. Called both from the 02 log line and manual edits."""
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
            return                              # no real change, no spurious prompt
        self._settings["ui_language"] = lang
        self._save_settings()                   # MUST persist before the restart reads it
        if ac.QMessageBox.question(
                self, _("Restart NyaaTriggers"),
                _("The interface language changed. Restart NyaaTriggers now to apply it?"),
        ) == ac.QMessageBox.StandardButton.Yes:
            self._restart_for_update()          # os.execv never returns, nothing after it

    def _save_raw_log(self) -> None:
        """Write the captured raw WS feed to a text file. Uses the complete
        capture, not the filtered Easy-to-Read log, so effects you apply are
        included."""
        lines = list(self._raw_capture)
        if not lines:
            ac.QMessageBox.information(
                self, _("Save Log"),
                _("No captured log lines yet - connect and run a pull first."))
            return
        dlg = ac.QFileDialog(self, _("Save Log"), "nyaa_log.txt",
                          _("Text files (*.txt);;All files (*)"))
        dlg.setAcceptMode(ac.QFileDialog.AcceptMode.AcceptSave)
        dlg.setDefaultSuffix("txt")   # see _export_triggers
        if dlg.exec() != QDialog.DialogCode.Accepted or not dlg.selectedFiles():
            return
        path = dlg.selectedFiles()[0]
        try:
            # Sibling tmp plus rename, like _atomic_write_json. An
            # interrupted write must not leave a truncated log the user
            # assumes is complete.
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
        ac.QMessageBox.information(
            self, _("Save Log"),
            _("Saved {count} line{plural} to:\n{path}").format(
                count=len(lines),
                plural="" if len(lines) == 1 else "s", path=path))

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
        """Translate a free-form callout by exact text. Used for engine
        callouts, Triggevent, cactbot, Triggernometry, which have no
        trigger id, so only the text-keyed phrase map applies. Gated by
        callouts_localized. English fallback so an unmatched or custom
        callout stays as-is. Dynamic {token} keys, the engine substitutes
        Groovy before we see the text, fall through to compiled regex
        patterns."""
        if not text or not self._settings.get("callouts_localized", active_locale() == "ja"):
            return text
        ja = self._callouts_phrases_ja.get(text)
        if ja:
            return ja
        for pat, ja_val in self._callouts_phrases_ja_patterns:
            if pat.match(text):
                return ja_val
        return text

    def _reading_for(self, text: str) -> str:
        """Kana reading of a localized callout, for TTS. Offline voices,
        espeak, and even some system voices can't read kanji and announce
        it as "Chinese letter", so speak the hiragana or katakana reading
        instead. Maps a Japanese display string to its reading. English
        and names, not in the map, pass through."""
        return self._callouts_readings.get(text) or text

    def _localized_name(self, t: Trigger) -> str:
        """Trigger NAME in the active locale, for display, table and
        dialogs. Display only, never spoken, so kanji needs no reading.
        Same gate as callouts. English fallback covers user copies and
        renames with no map entry. Engine triggers, Triggevent and
        Triggernometry, use Groovy classpath ids the id map doesn't hold,
        so fall through to a text-keyed, english name to ja, map."""
        if not self._settings.get("callouts_localized", active_locale() == "ja"):
            return t.name
        return (self._callouts_names_ja.get(t.id)
                or self._callouts_names_text_ja.get(t.name)
                or t.name)

    def _flush_pending_settings_save(self) -> None:
        """A debounced settings save still pending dies with the process.
        Flush it so the last slider position survives."""
        if self._settings_save_timer.isActive():
            self._settings_save_timer.stop()
            self._save_settings()
