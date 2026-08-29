"""Voice page. Piper, Kokoro and system voices, the venv install flow,
alert sounds, and the volume and mute controls. tts.py owns the speaking,
this owns the config UI. Mixin for MainWindow, all state rides on self.
"""

from pathlib import Path
import json
import math
import os
import re
import shutil
import tempfile
import threading

from PyQt6.QtCore import Qt, QUrl
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QMenu, QSlider, QHBoxLayout, QPushButton, QLabel,
)

from tts import (
    speak, play_sound, play_notification, set_model, set_venv_path, set_master_volume, set_engine, set_jp_neural, kokoro_ready, _MAX_SOUND_BYTES,
)
from locale_util import _
import updater

import app_common as ac
from app_common import _JP_NEURAL_VOICES, _sweep_stale_update_parts, _voice_display


class VoiceTabMixin:
    def _scan_voices(self) -> list[tuple[str, Path]]:
        # User voices first, next to the exe they survive self-updates, then the
        # bundled set. On a stem clash the user copy wins. The Kokoro neural
        # model also lives here as .onnx. It is not a Piper voice, so keep it
        # out of the Piper list. It has its own combo entries.
        found: dict[str, Path] = {}
        for voices_dir in (ac._USER_VOICES_DIR, ac._BUNDLE_DIR / "voices"):
            if not voices_dir.exists():
                continue
            for p in sorted(voices_dir.glob("*.onnx")):
                if not p.stem.startswith("kokoro") and p.stem not in found:
                    found[p.stem] = p
        return [(stem, found[stem]) for stem in sorted(found)]

    def _on_master_volume_changed(self, value: int) -> None:
        self._vol_label.setText(f"{value}%")
        v = value / 100.0
        set_master_volume(v)
        self._settings["master_volume"] = v
        self._save_settings_debounced()

    def _on_mute_toggled(self, muted: bool) -> None:
        """Apply or clear the master mute. Driven by the button's checked state, so
        timed/zone mutes just flip the button and route through here too."""
        if not muted:
            # Any manual or expired un-mute also cancels pending timed mutes.
            self._mute_timer.stop()
            self._mute_until_zone = False
        if muted:
            set_master_volume(0.0)
            self._mute_btn.setText("🔇")
            self._vol_label.setText(_("muted"))
        else:
            # A hand edited numeric string would raise inside set_master_volume's
            # min(). Same coercion guard the slider's startup read applies.
            try:
                vol = float(self._settings.get("master_volume", 1.0))
            except (TypeError, ValueError):
                vol = 1.0
            # json parses NaN and Infinity fine. Comparisons against NaN are
            # all false, so the clamp in set_master_volume would pin it to 2.0
            # and every unmuted callout blasts at double volume.
            if not math.isfinite(vol):
                vol = 1.0
            set_master_volume(vol)
            self._mute_btn.setText("🔊")
            self._vol_label.setText(f"{self._vol_slider.value()}%")
        self._vol_slider.setEnabled(not muted)

    def _mute_for_minutes(self, minutes: float) -> None:
        self._mute_until_zone = False
        self._mute_btn.setChecked(True)         # _on_mute_toggled applies the mute
        self._mute_timer.start(int(minutes * 60_000))

    def _mute_until_next_zone(self) -> None:
        self._mute_timer.stop()
        self._mute_until_zone = True
        self._mute_btn.setChecked(True)
        self._on_mute_toggled(True)             # no-op refresh if already checked

    def _on_mute_context_menu(self, pos) -> None:
        menu = QMenu(self._mute_btn)
        a5 = menu.addAction(_("Mute for 5 minutes"))
        a15 = menu.addAction(_("Mute for 15 minutes"))
        az = menu.addAction(_("Mute until next zone"))
        menu.addSeparator()
        au = menu.addAction(_("Unmute"))
        au.setEnabled(self._mute_btn.isChecked())
        chosen = menu.exec(self._mute_btn.mapToGlobal(pos))
        if chosen is a5:
            self._mute_for_minutes(5)
        elif chosen is a15:
            self._mute_for_minutes(15)
        elif chosen is az:
            self._mute_until_next_zone()
        elif chosen is au:
            self._mute_btn.setChecked(False)

    def _alert_sound_path(self) -> str | None:
        name = self._settings.get("overlay_sound_file", "ding.wav")
        # A hand edited non-string must not raise out of the alert emit,
        # same guard _alert_sound_amp carries for its setting.
        if not isinstance(name, str) or not name:
            return None
        p = Path(name)
        if p.is_absolute():
            return str(p) if p.exists() else None
        # A user-imported SFX wins over a built-in of the same name.
        for cand in (ac._USER_SOUNDS_DIR / name, ac._BUNDLE_DIR / "sounds" / name):
            if cand.exists():
                return str(cand)
        return None

    @staticmethod
    def _sound_amp_from_fraction(v: float) -> float:
        """Map the 0..1 slider fraction to amplitude on a dB taper. Loudness is
        ~logarithmic and a linear slider feels dead until the last 10%.
        100% -> 0 dB or 1.0, 50% -> -20 dB or 0.1, 0% -> silent."""
        if v <= 0.0:
            return 0.0
        if v >= 1.0:
            return 1.0
        return 10.0 ** ((v - 1.0) * 2.0)   # 40 dB usable range

    def _alert_sound_amp(self) -> float:
        # Same coercion guard the slider init gets. A hand edited string,
        # NaN or Infinity must not raise out of the alert path.
        try:
            v = float(self._settings.get("overlay_sound_volume", 0.5))
        except (TypeError, ValueError):
            v = 0.5
        if not math.isfinite(v):
            v = 0.5
        return self._sound_amp_from_fraction(v)

    def _maybe_play_alert_sound(self, severity: str) -> None:
        if not self._settings.get("overlay_sound_enabled", False):
            return
        if (self._settings.get("overlay_sound_scope", "all") == "alarm"
                and severity != "alarm"):
            return
        path = self._alert_sound_path()
        if path:
            play_notification(path, self._alert_sound_amp())

    def _on_alert_sound_enabled_changed(self, state: int) -> None:
        self._settings["overlay_sound_enabled"] = bool(state)
        self._save_settings()

    def _on_alert_sound_file_changed(self, index: int) -> None:
        data = self._alert_sound_combo.itemData(index)
        if data == "__custom__":
            path, _unused = ac.QFileDialog.getOpenFileName(
                self, _("Choose a notification sound"), str(Path.home()),
                _("WAV audio (*.wav)"))
            if path:
                self._settings["overlay_sound_file"] = path
                self._save_settings()
            self._select_alert_sound_in_combo()   # reflect saved value either way
            return
        self._settings["overlay_sound_file"] = data
        self._save_settings()

    def _on_alert_sound_scope_changed(self, index: int) -> None:
        self._settings["overlay_sound_scope"] = (
            self._alert_sound_scope_combo.itemData(index) or "all")
        self._save_settings()

    def _on_alert_sound_volume_changed(self, value: int) -> None:
        # The alert sound's own level. tts multiplies it by the master volume.
        self._settings["overlay_sound_volume"] = value / 100.0
        self._save_settings_debounced()
        if hasattr(self, "_alert_sound_vol_lbl"):
            self._alert_sound_vol_lbl.setText(f"{value}%")

    def _on_alert_sound_test(self) -> None:
        path = self._alert_sound_path()
        if path:
            play_notification(path, self._alert_sound_amp())
        else:
            ac.QMessageBox.information(
                self, _("Notification sound"),
                _("That sound file could not be found. Pick another or browse to a "
                ".wav file."))

    def _import_sfx(self) -> None:
        """Copy a chosen .wav into the user sounds folder so it becomes a reusable
        picker entry. The bundled sounds folder is read-only on frozen builds.
        Selects and plays it as confirmation."""
        path, _unused = ac.QFileDialog.getOpenFileName(
            self, _("Import alert SFX"), str(Path.home()), _("WAV audio (*.wav)"))
        if not path:
            return
        src = Path(path)
        try:
            # The picker accepts any path, and this copy runs on the GUI
            # thread. A FIFO or device node would freeze the whole window, a
            # huge file would hang it. Same prechecks as the TTS sound copy.
            if not src.is_file() or src.stat().st_size > _MAX_SOUND_BYTES:
                ac.QMessageBox.warning(
                    self, _("Import SFX"),
                    _("Pick a regular .wav file no larger than {limit} MiB.").format(
                        limit=_MAX_SOUND_BYTES >> 20))
                return
            ac._USER_SOUNDS_DIR.mkdir(parents=True, exist_ok=True)
            dest = ac._USER_SOUNDS_DIR / src.name
            if dest.resolve() != src.resolve():     # picking a file already in the folder
                shutil.copy2(src, dest)
        except OSError as exc:
            ac.QMessageBox.warning(self, _("Import SFX"),
                                _("Could not import that sound:\n{error}").format(error=exc))
            return
        self._settings["overlay_sound_file"] = src.name
        self._save_settings()
        self._populate_sound_combo()
        self._select_alert_sound_in_combo()
        snd = self._alert_sound_path()
        if snd:
            play_notification(snd, self._alert_sound_amp())

    def _emit_alert(self, text: str, severity: str = "info") -> None:
        """One place every visual alert goes through. Plays the notification
        sound and pushes the callout to the companion Dalamud plugin, which
        draws it in game. Severities are the shared info/alert/alarm vocabulary,
        so the push maps one to one."""
        self._maybe_play_alert_sound(severity)
        self._plugin_link.send_alert(text, severity)

    def _build_alert_sound_settings(self, layout) -> None:
        self._settings_header(layout, _("Alert Sound"))
        self._alert_sound_cb = QCheckBox(_("Play a sound when an alert appears"))
        self._alert_sound_cb.setChecked(
            bool(self._settings.get("overlay_sound_enabled", False)))
        self._alert_sound_cb.stateChanged.connect(self._on_alert_sound_enabled_changed)
        layout.addWidget(self._alert_sound_cb)

        snd_row = QHBoxLayout()
        snd_row.addWidget(QLabel(_("Sound:")))
        self._alert_sound_combo = QComboBox()
        self._alert_sound_combo.setMaximumWidth(180)
        self._populate_sound_combo()
        self._alert_sound_combo.activated.connect(self._on_alert_sound_file_changed)
        snd_row.addWidget(self._alert_sound_combo)
        test_btn = QPushButton(_("Test"))
        test_btn.setMaximumWidth(70)
        test_btn.clicked.connect(self._on_alert_sound_test)
        snd_row.addWidget(test_btn)
        import_sfx_btn = QPushButton(_("Import SFX"))
        import_sfx_btn.setMaximumWidth(130)
        import_sfx_btn.clicked.connect(self._import_sfx)
        snd_row.addWidget(import_sfx_btn)
        snd_row.addStretch(1)
        layout.addLayout(snd_row)

        # Alert-sound volume, its own level, scaled by the master volume.
        vol_row = QHBoxLayout()
        vol_row.addWidget(QLabel(_("Volume:")))
        self._alert_sound_vol_slider = QSlider(Qt.Orientation.Horizontal)
        self._alert_sound_vol_slider.setMinimum(0)
        self._alert_sound_vol_slider.setMaximum(100)
        self._alert_sound_vol_slider.setMaximumWidth(180)
        # Same coercion guard the master volume slider gets. A hand edited
        # string, NaN or Infinity must not raise out of _build_ui.
        try:
            alert_vol = float(self._settings.get("overlay_sound_volume", 0.5))
        except (TypeError, ValueError):
            alert_vol = 0.5
        if not math.isfinite(alert_vol):
            alert_vol = 0.5
        cur_v = int(round(alert_vol * 100))
        self._alert_sound_vol_slider.setValue(max(0, min(100, cur_v)))
        self._alert_sound_vol_slider.valueChanged.connect(self._on_alert_sound_volume_changed)
        vol_row.addWidget(self._alert_sound_vol_slider)
        self._alert_sound_vol_lbl = QLabel(f"{self._alert_sound_vol_slider.value()}%")
        vol_row.addWidget(self._alert_sound_vol_lbl)
        vol_row.addStretch(1)
        layout.addLayout(vol_row)

        scope_row = QHBoxLayout()
        scope_row.addWidget(QLabel(_("Play for:")))
        self._alert_sound_scope_combo = QComboBox()
        self._alert_sound_scope_combo.setMaximumWidth(180)
        for label, data in ((_("All alerts"), "all"), (_("Alarms only"), "alarm")):
            self._alert_sound_scope_combo.addItem(label, userData=data)
        scidx = self._alert_sound_scope_combo.findData(
            self._settings.get("overlay_sound_scope", "all"))
        self._alert_sound_scope_combo.setCurrentIndex(scidx if scidx >= 0 else 0)
        self._alert_sound_scope_combo.currentIndexChanged.connect(
            self._on_alert_sound_scope_changed)
        scope_row.addWidget(self._alert_sound_scope_combo)
        scope_row.addStretch(1)
        layout.addLayout(scope_row)

    def _sound_combo_items(self) -> list[tuple[str, str]]:
        """label/data entries for the alert-sound picker."""
        items = [(_("Ding"), "ding.wav"), (_("Alert"), "alert.wav"), (_("Coin"), "coin.wav")]
        # User-imported SFX, by filename. Skip any that shadow a built-in.
        builtin = {data for _, data in items}
        try:
            for wav in sorted(ac._USER_SOUNDS_DIR.glob("*.wav")):
                if wav.name not in builtin:
                    items.append((wav.stem, wav.name))
        except OSError:
            pass
        items.append((_("Custom file..."), "__custom__"))
        return items

    def _populate_sound_combo(self) -> None:
        """Refill the sound picker, preserving the current selection."""
        combo = getattr(self, "_alert_sound_combo", None)
        if combo is None:
            return
        combo.blockSignals(True)
        combo.clear()
        for label, data in self._sound_combo_items():
            combo.addItem(label, userData=data)
        combo.blockSignals(False)
        self._select_alert_sound_in_combo()

    def _select_alert_sound_in_combo(self) -> None:
        name = self._settings.get("overlay_sound_file", "ding.wav")
        idx = self._alert_sound_combo.findData(name)
        if idx < 0:   # a custom absolute path -> show the Custom entry
            idx = self._alert_sound_combo.findData("__custom__")
        self._alert_sound_combo.blockSignals(True)
        self._alert_sound_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self._alert_sound_combo.blockSignals(False)

    def _on_triggevent_tts(self, text: str) -> None:
        # see _on_triggevent_callout. Stay silent while the engine runs but
        # callouts are off, i.e. Cactbot is on
        if not self._triggevent_mode:
            return
        self._triggevent_speak(text)

    def _on_triggernometry_tts(self, text: str) -> None:
        if not self._triggernometry_mode:
            return
        self._triggernometry_speak(text)

    def _on_triggernometry_sound(self, file: str, volume: int) -> None:
        # SoundMethod=ACT routes engine sound files here. Best effort.
        # Volume is a 0-100 int, play_sound wants 0.0-1.0.
        if not self._triggernometry_mode:
            return
        try:
            if file and os.path.isfile(file):
                play_sound(file, max(0.0, min(volume / 100.0, 1.0)))
        except Exception:  # noqa: BLE001 - never let a missing sound break callouts
            pass

    def _on_cactbot_tts(self, text: str) -> None:
        self._emit_guest_callout(text, "info")

    def _on_tts_engine_changed(self, index: int) -> None:
        eng = self._tts_engine_combo.itemData(index) or "piper"
        set_engine(eng)
        self._settings["tts_engine"] = eng
        self._save_settings()

    def _on_voice_changed(self, index: int) -> None:
        """Model picker for BOTH languages. A "kokoro:" prefixed entry turns
        on the in-app neural Japanese voice and sets it up on first pick.
        A Piper entry is the English voice and sends Japanese to espeak."""
        data = self._voice_combo.itemData(index)
        if isinstance(data, str) and data.startswith("kokoro:"):
            voice = data[len("kokoro:"):]
            self._settings["jp_neural_enabled"] = True
            self._settings["jp_neural_voice"] = voice
            self._save_settings()
            set_jp_neural(True, voice)          # Japanese auto-routes here since jp_auto is on
            if not kokoro_ready():
                self._on_kokoro_download()      # first pick, download and set it up now
            return
        if data:
            set_model(Path(data))
            self._settings["voice_model"] = self._voice_combo.itemText(index)
        self._settings["jp_neural_enabled"] = False
        self._save_settings()
        set_jp_neural(False)                    # Japanese falls back to espeak

    def _on_kokoro_dl_done(self, status: str) -> None:
        self._kokoro_setup_running = False
        self._kokoro_dl_btn.setEnabled(True)
        self._kokoro_dl_btn.setText(_("Download"))
        if status == "ready":
            ac.QMessageBox.information(self, _("Neural Japanese voice"),
                _("The neural Japanese voice is ready. Restart to use it."))
        elif status == "no-model":
            ac.QMessageBox.warning(self, _("Neural Japanese voice"),
                _("Could not download the voice model. Check your connection and try again."))
        else:   # deps install failed
            ac.QMessageBox.warning(self, _("Neural Japanese voice"),
                _("The app could not install the voice dependencies "
                  "(pip install kokoro-onnx). Check your internet connection, "
                  "that the app folder is writable, and that the disk has free "
                  "space, then click Download again. Callouts keep using espeak "
                  "until then.")
                + ("\n\n" + status[8:] if status.startswith("no-deps:") and status[8:] else ""))

    def _on_venv_changed(self) -> None:
        path = self._venv_edit.text().strip()
        self._settings["venv_path"] = path
        self._save_settings()
        set_venv_path(path)

    def _browse_venv(self) -> None:
        path = ac.QFileDialog.getExistingDirectory(self, _("Select Piper venv directory"),
                                                self._venv_edit.text())
        if path:
            self._venv_edit.setText(path)
            self._on_venv_changed()

    def _test_tts_settings(self) -> None:
        data = self._voice_combo.currentData()
        if isinstance(data, str) and data.startswith("kokoro:"):
            # Match the selected voice. A Japanese voice tests in Japanese,
            # with a kana reading so the espeak fallback doesn't say
            # "Chinese letter".
            speak("テストトリガー発動", reading="テストトリガーはつどう")
        else:
            speak("Test trigger fired")

    def _open_voices_folder(self) -> None:
        """Open the voices folder in the OS file manager. The USER one, not
        the bundled _internal dir a self-update wipes. See
        _USER_VOICES_DIR."""
        ac._USER_VOICES_DIR.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(ac._USER_VOICES_DIR)))

    def _populate_voice_combo(self) -> None:
        """Fill the Model dropdown. Piper voices first, friendly names with
        the path as item data, then the neural Japanese voices, whose item
        data is the "kokoro:" prefix plus the voice id."""
        for stem, path in self._scan_voices():
            self._voice_combo.addItem(_voice_display(stem), userData=str(path))
        # The neural Japanese voices ship inside the app now, kokoro-onnx
        # and the espeak-ng phonemizer are bundled in the frozen build too,
        # so list them on every platform. Their model downloads on first
        # pick.
        for vid, label in _JP_NEURAL_VOICES:
            self._voice_combo.addItem(label, userData="kokoro:" + vid)

    def _refresh_voice_combo(self) -> None:
        saved_data = self._voice_combo.currentData()
        self._voice_combo.blockSignals(True)
        self._voice_combo.clear()
        self._populate_voice_combo()   # both sections. Refresh must keep the JP voices
        idx = self._voice_combo.findData(saved_data)
        self._voice_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self._voice_combo.blockSignals(False)
        # If the active voice was removed, re-point TTS to whatever is now
        # selected so set_model and the saved setting follow the visible
        # choice.
        if idx < 0 and self._voice_combo.count():
            self._on_voice_changed(self._voice_combo.currentIndex())

    def _start_install(self, rel, kind: str) -> None:
        def _work() -> None:
            try:
                # Sweep .part leftovers from crashed earlier update downloads.
                _sweep_stale_update_parts(Path(tempfile.gettempdir()))
                if kind == "git":
                    self._upd_progress_signal.emit(-1, _("Running git pull..."))
                    ok, msg = updater.apply_git()
                elif kind == "frozen-linux":
                    url = updater.asset_for_platform(rel)
                    if not url:
                        ok, msg = False, _("No Linux build found in the latest release.")
                    else:
                        dest = Path(tempfile.gettempdir()) / updater.LINUX_ASSET

                        def _prog(done: int, total: int) -> None:
                            pct = int(done * 100 / total) if total else -1
                            mb = done / (1024 * 1024)
                            self._upd_progress_signal.emit(pct, _("Downloading... {mb:.0f} MB").format(mb=mb))

                        updater.download(url, dest, progress_cb=_prog)
                        self._upd_progress_signal.emit(-1, _("Verifying download..."))
                        ok, msg = updater.verify_release_asset(rel, updater.LINUX_ASSET, dest)
                        if ok:
                            self._upd_progress_signal.emit(-1, _("Installing..."))
                            ok, msg = updater.apply_frozen_linux(dest)
                        try:
                            dest.unlink()
                        except OSError:
                            pass
                elif kind == "frozen-windows":
                    url = updater.asset_for_platform(rel)
                    if not url:
                        ok, msg = False, _("No Windows build found in the latest release.")
                    else:
                        dest = Path(tempfile.gettempdir()) / updater.WINDOWS_ASSET

                        def _prog(done: int, total: int) -> None:
                            pct = int(done * 100 / total) if total else -1
                            mb = done / (1024 * 1024)
                            self._upd_progress_signal.emit(pct, _("Downloading... {mb:.0f} MB").format(mb=mb))

                        updater.download(url, dest, progress_cb=_prog)
                        self._upd_progress_signal.emit(-1, _("Verifying download..."))
                        ok, msg = updater.verify_release_asset(rel, updater.WINDOWS_ASSET, dest)
                        if ok:
                            # The running exe and loaded _internal/*.dll
                            # are OS-locked, so no in-place swap. Stage the
                            # new build and hand off to it.
                            self._upd_progress_signal.emit(-1, _("Preparing update..."))
                            ok, msg = updater.apply_frozen_windows(dest)
                        try:
                            dest.unlink()
                        except OSError:
                            pass
                else:
                    ok, msg = False, _("This install type cannot update itself.")
            except Exception as exc:  # noqa: BLE001
                ok, msg = False, str(exc)
            self._upd_done_signal.emit(ok, msg)
        try:
            threading.Thread(target=_work, daemon=True).start()
        except Exception as exc:  # noqa: BLE001 - a failed start must not strand the banner
            self._on_update_done(False, str(exc))
