from copy import deepcopy

from PyQt6.QtWidgets import (
    QComboBox, QHBoxLayout, QInputDialog, QLabel, QPushButton, QSizePolicy,
    QToolButton, QVBoxLayout, QWidget,
)
from PyQt6.QtCore import Qt

from nyaatriggers import app_common as ac
from nyaatriggers.locale_util import _
from nyaatriggers.record_store import load_records, read_record, record_id, write_record
from nyaatriggers.trigger_profiles import (
    DEFAULT_PROFILE_ID, SOURCES, apply_choices, capture_profile, preserve_default, validate_profile,
)

_PENDING_PROFILE = "pending_trigger_profile"


class ProfilesMixin:
    def _build_profiles(self, layout):
        self._profiles_dir = ac._DATA_DIR / "trigger_profiles"
        records, errors = load_records(self._profiles_dir, validate_profile)
        self._default_profile = next((p for p in records if p["id"] == DEFAULT_PROFILE_ID), None)
        self._profiles = [p for p in records if p["id"] != DEFAULT_PROFILE_ID]
        self._default_unreadable = any(
            error.startswith((DEFAULT_PROFILE_ID + ".json:", str(self._profiles_dir) + ":"))
            for error in errors)
        try:
            self._active_profile_id = record_id(self._settings.get("active_trigger_profile", DEFAULT_PROFILE_ID))
        except ValueError:
            self._active_profile_id = DEFAULT_PROFILE_ID
        self._profile_panel = QWidget()
        self._profile_panel.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        panel = QVBoxLayout(self._profile_panel)
        panel.setContentsMargins(0, 0, 0, 0)
        self._profile_toggle = QToolButton()
        self._profile_toggle.setText(_("Profiles"))
        self._profile_toggle.setCheckable(True)
        self._profile_toggle.setArrowType(Qt.ArrowType.RightArrow)
        self._profile_toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self._profile_toggle.setAutoRaise(True)
        self._profile_toggle.setToolTip(_("Profiles save trigger choices and wording. Apply between pulls. New triggers keep their current settings."))
        panel.addWidget(self._profile_toggle, 0, Qt.AlignmentFlag.AlignLeft)
        self._profile_body = QWidget()
        body = QVBoxLayout(self._profile_body)
        body.setContentsMargins(0, 0, 0, 0)
        panel.addWidget(self._profile_body)
        self._profile_body.hide()
        self._profile_toggle.toggled.connect(self._toggle_profiles)
        layout.addWidget(self._profile_panel)
        self._profile_controls = QWidget()
        self._profile_controls.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        controls = QHBoxLayout(self._profile_controls)
        controls.setContentsMargins(0, 0, 0, 0)
        label = QLabel(_("Profile:"))
        controls.addWidget(label)
        self._profile_picker = QComboBox()
        self._profile_picker.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self._profile_picker.setMinimumContentsLength(16)
        self._profile_picker.setMaximumWidth(240)
        controls.addWidget(self._profile_picker)
        self._profile_apply = QPushButton(_("Apply"))
        self._profile_new = QPushButton(_("Save new"))
        self._profile_update = QPushButton(_("Update"))
        self._profile_delete = QPushButton(_("Delete"))
        for button, tooltip in (
            (self._profile_apply, _("Apply profile")),
            (self._profile_new, _("Save new profile")),
            (self._profile_update, _("Update saved profile")),
            (self._profile_delete, _("Delete profile")),
        ):
            button.setToolTip(tooltip)
            controls.addWidget(button)
        controls.addStretch()
        body.addWidget(self._profile_controls)
        self._profile_status = QLabel()
        self._profile_status.setTextFormat(Qt.TextFormat.PlainText)
        self._profile_status.setWordWrap(True)
        self._profile_status.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Maximum)
        self._profile_status.hide()
        body.addWidget(self._profile_status)
        self._profile_apply.clicked.connect(self._apply_profile)
        self._profile_new.clicked.connect(self._save_new_profile)
        self._profile_update.clicked.connect(self._update_profile)
        self._profile_delete.clicked.connect(self._delete_profile)
        self._profile_picker.currentIndexChanged.connect(self._profile_selection_changed)
        self._refresh_profiles()
        if errors:
            self._set_profile_status(_("Some profiles could not be read and were kept unchanged: {error}").format(error="\n".join(errors[:3])))
        elif self._active_profile_id != DEFAULT_PROFILE_ID and self._default_profile is None:
            self._set_profile_status(_("Default could not be loaded. Repair the saved Default profile before switching profiles."))

    def _toggle_profiles(self, expanded):
        self._profile_body.setVisible(expanded)
        self._profile_toggle.setArrowType(Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow)

    def _set_profile_status(self, text):
        self._profile_status.setText(text)
        self._profile_status.setVisible(bool(text))
        if text:
            self._profile_toggle.setChecked(True)

    def _refresh_profiles(self, ident=None):
        ident = ident or self._profile_picker.currentData() or self._active_profile_id
        self._profile_picker.blockSignals(True)
        self._profile_picker.clear()
        self._profile_picker.addItem(_("Default"), DEFAULT_PROFILE_ID)
        for profile in self._profiles:
            name = profile["name"]
            if name.casefold() in {"default", _("Default").casefold()}:
                name = _("Saved profile: {name}").format(name=name)
            self._profile_picker.addItem(name, profile["id"])
        index = self._profile_picker.findData(ident)
        self._profile_picker.setCurrentIndex(max(0, index))
        self._profile_picker.blockSignals(False)
        self._profile_selection_changed()

    def _profile_selection_changed(self):
        ident = self._profile_picker.currentData()
        named = ident is not None and ident != DEFAULT_PROFILE_ID
        self._profile_apply.setEnabled(
            _PENDING_PROFILE not in self._settings
            and (named or self._active_profile_id != DEFAULT_PROFILE_ID))
        self._profile_update.setEnabled(named)
        self._profile_delete.setEnabled(named)
        self._profile_picker.setToolTip(
            self._profile_picker.currentText() if named else
            _("Default keeps the changes you save while no named profile is applied."))

    def _selected_profile(self):
        ident = self._profile_picker.currentData()
        if ident == DEFAULT_PROFILE_ID:
            if self._active_profile_id == DEFAULT_PROFILE_ID:
                return capture_profile(self, _("Default"), DEFAULT_PROFILE_ID)
            return self._default_profile
        return next((p for p in self._profiles if p["id"] == ident), None)

    def _restore_missing_profile(self):
        recovering = _PENDING_PROFILE in self._settings
        if recovering and not self._recover_profile_change():
            return
        if (self._active_profile_id != DEFAULT_PROFILE_ID
                and not any(p["id"] == self._active_profile_id for p in self._profiles)):
            default = self._default_profile
            if default is None and not self._default_unreadable:
                # Both records are gone. Keep the current choices as Default.
                default = capture_profile(self, _("Default"), DEFAULT_PROFILE_ID)
            restored = self._activate_profile(default)
            if recovering and restored:
                self._set_profile_status(_("Recovered the interrupted profile change and returned to Default because the saved profile is unavailable."))

    def _profile_intent_path(self):
        return self._profiles_dir / ".pending" / (DEFAULT_PROFILE_ID + ".json")

    def _profile_local_changed(self, baseline):
        from nyaatriggers.ui.triggers_tab import _read_local_triggers
        try:
            current = _read_local_triggers()
        except FileNotFoundError:
            current = None
        except (OSError, ValueError, RecursionError):
            return True
        return current != baseline

    def _write_profile_intent(self, profile, *, preserve_local=False):
        record_id(profile.get("id"))
        validate_profile(profile)
        write_record(self._profile_intent_path().parent,
                     {"version": 1, "id": DEFAULT_PROFILE_ID, "profile": deepcopy(profile),
                      "local_baseline": deepcopy(getattr(self, "_local_trigger_baseline", None)),
                      "preserve_local": preserve_local})

    def _persist_profile_choice(self, profile, *, preserve_local=False):
        self._settings["active_trigger_profile"] = profile["id"]
        self._settings[_PENDING_PROFILE] = True
        if not self._save_settings() or (not preserve_local and not self._save_triggers()):
            return False
        self._settings.pop(_PENDING_PROFILE)
        if not self._save_settings():
            self._settings[_PENDING_PROFILE] = True
            return False
        if not self._default_unreadable:
            try:
                self._profile_intent_path().unlink(missing_ok=True)
            except OSError:
                pass
        return True

    def _recover_profile_change(self):
        with ac.defer_persistence_warnings(self):
            return self._recover_profile_change_now()

    def _recover_profile_change_now(self):
        returned_to_default = False
        try:
            if self._settings[_PENDING_PROFILE] is not True:
                raise ValueError("Invalid pending profile change")
            record = read_record(self._profile_intent_path())
            profile = record.get("profile")
            if not isinstance(profile, dict):
                raise ValueError("Missing pending profile snapshot")
            record_id(profile.get("id"))
            validate_profile(profile)
            preserve_local = record.get("preserve_local", False)
            if type(preserve_local) is not bool:
                raise ValueError("Invalid local recovery mode")
            if not preserve_local and ("local_baseline" not in record
                    or self._profile_local_changed(record["local_baseline"])):
                preserve_local = True
                self._write_profile_intent(profile, preserve_local=True)
            if (preserve_local and profile["id"] != DEFAULT_PROFILE_ID
                    and not any(p["id"] == profile["id"] for p in self._profiles)):
                default = self._default_profile
                if default is None and not self._default_unreadable:
                    default = capture_profile(self, _("Default"), DEFAULT_PROFILE_ID)
                if default is None:
                    self._set_profile_status(_("Default could not be loaded. Repair the saved Default profile before switching profiles."))
                    self._profile_selection_changed()
                    return False
                profile = deepcopy(default)
                self._write_profile_intent(profile, preserve_local=True)
                returned_to_default = True
        except (OSError, ValueError, TypeError, KeyError, RecursionError, OverflowError) as exc:
            self._set_profile_status(_("Could not recover the interrupted profile change: {error}\nRepair the recovery record in trigger_profiles/.pending before switching profiles.").format(error=exc))
            self._profile_selection_changed()
            return False
        previous_speech = {ident: self._triggevent_effective_tts(ident)
                           for ident in profile["engines"].get("triggevent", {})}
        if preserve_local:
            profile = deepcopy(profile)
            profile["local"] = {}
        apply_choices(self, profile)
        if not self._persist_profile_choice(profile, preserve_local=preserve_local):
            self._set_profile_status(_("The interrupted profile change could not be saved. Check that the data folder is writable, then restart to finish recovery."))
            self._profile_selection_changed()
            return False
        self._profile_applied(profile, previous_speech)
        if returned_to_default:
            self._set_profile_status(_("Recovered the interrupted profile change and returned to Default because the saved profile is unavailable."))
        return True

    def _save_new_profile(self):
        name, accepted = QInputDialog.getText(self, _("Save new profile"), _("Profile name:"))
        if not accepted or not name.strip():
            return
        if name.strip().casefold() in {"default", _("Default").casefold()}:
            self._set_profile_status(_("Default is reserved for your normal setup. Choose another profile name."))
            return
        try:
            profile = capture_profile(self, name)
            write_record(self._profiles_dir, profile)
        except (OSError, ValueError) as exc:
            self._set_profile_status(_("Could not save profile: {error}").format(error=exc))
            return
        self._profiles.append(profile)
        self._refresh_profiles(profile["id"])
        self._set_profile_status(_("Saved profile: {name}").format(name=profile["name"]))

    def _update_profile(self):
        old = self._selected_profile()
        if old is None or old["id"] == DEFAULT_PROFILE_ID:
            return
        if ac.QMessageBox.question(self, _("Update saved profile"),
                                  _("Replace the saved choices in {name} with the current setup?").format(name=old["name"])) != ac.QMessageBox.StandardButton.Yes:
            return
        try:
            profile = capture_profile(self, old["name"], old["id"])
            write_record(self._profiles_dir, profile)
        except (OSError, ValueError) as exc:
            self._set_profile_status(_("Could not save profile: {error}").format(error=exc))
            return
        self._profiles[self._profiles.index(old)] = profile
        self._set_profile_status(_("Saved profile: {name}").format(name=profile["name"]))

    def _delete_profile(self):
        profile = self._selected_profile()
        if profile is None or profile["id"] == DEFAULT_PROFILE_ID:
            return
        active = profile["id"] == self._active_profile_id
        message = (_("Delete {name}? This will return to Default.") if active else
                   _("Delete the saved profile {name}?"))
        if ac.QMessageBox.question(
                self, _("Delete profile"), message.format(name=profile["name"]),
                ac.QMessageBox.StandardButton.Yes | ac.QMessageBox.StandardButton.No,
                ac.QMessageBox.StandardButton.No) != ac.QMessageBox.StandardButton.Yes:
            return
        if active and not self._activate_profile(self._default_profile):
            return
        try:
            path = self._profiles_dir / (record_id(profile["id"]) + ".json")
            path.unlink(missing_ok=True)
        except (OSError, ValueError) as exc:
            self._set_profile_status(_("Could not delete profile: {error}").format(error=exc))
            return
        self._profiles.remove(profile)
        self._refresh_profiles(self._active_profile_id)
        self._set_profile_status(_("Deleted profile: {name}").format(name=profile["name"]))

    def _apply_profile(self):
        self._activate_profile(self._selected_profile())

    def _activate_profile(self, profile):
        with ac.defer_persistence_warnings(self):
            return self._activate_profile_now(profile)

    def _activate_profile_now(self, profile):
        if _PENDING_PROFILE in self._settings:
            self._set_profile_status(_("An interrupted profile change still needs recovery. Repair the recovery record in trigger_profiles/.pending, then restart before switching profiles."))
            return False
        if profile is None:
            self._set_profile_status(_("Default could not be loaded. Repair the saved Default profile before switching profiles."))
            return False
        if self._in_game_combat or self._dps_meter.current is not None:
            self._set_profile_status(_("Wait until combat ends before applying a profile."))
            return False
        if getattr(self, "_local_corrupt", False):
            self._set_profile_status(_("Repair the unreadable local trigger file before applying a profile."))
            return False
        if profile["id"] != DEFAULT_PROFILE_ID:
            if self._default_unreadable or (self._active_profile_id != DEFAULT_PROFILE_ID and self._default_profile is None):
                self._set_profile_status(_("Default could not be loaded. Repair the saved Default profile before switching profiles."))
                return False
            previous = None if self._active_profile_id == DEFAULT_PROFILE_ID else self._default_profile
            try:
                default = preserve_default(self, previous, profile)
                write_record(self._profiles_dir, default)
            except (OSError, ValueError) as exc:
                self._set_profile_status(_("Could not save Default: {error}").format(error=exc))
                return False
            self._default_profile = default
        before = capture_profile(self, "Previous setup", self._active_profile_id)
        rollback = preserve_default(self, before, profile)
        old_settings = deepcopy(self._settings)
        old_local_ids = self._local_ids.copy()
        old_disabled = deepcopy(self._engine_disabled)
        old_edits = {src: deepcopy(getattr(self, f"_{src}_callout_edits", {})) for src in SOURCES}
        try:
            self._write_profile_intent(profile)
        except (OSError, ValueError) as exc:
            self._set_profile_status(_("Could not save profile recovery data: {error}").format(error=exc))
            return False
        previous_speech = {ident: self._triggevent_effective_tts(ident)
                           for ident in profile["engines"].get("triggevent", {})}
        apply_choices(self, profile)
        if not self._persist_profile_choice(profile):
            preserve_local = self._profile_local_changed(getattr(self, "_local_trigger_baseline", None))
            try:
                if preserve_local:
                    self._write_profile_intent(rollback, preserve_local=True)
                else:
                    self._write_profile_intent(rollback)
                rollback_ready = True
            except (OSError, ValueError):
                rollback_ready = False
            apply_choices(self, before)
            for source in SOURCES:
                self._engine_disabled[source].clear()
                self._engine_disabled[source].update(old_disabled[source])
                edits = getattr(self, f"_{source}_callout_edits", None)
                if edits is not None:
                    edits.clear()
                    edits.update(old_edits[source])
            self._settings = old_settings
            self._local_ids = old_local_ids
            self._settings[_PENDING_PROFILE] = True
            restored = rollback_ready and self._persist_profile_choice(rollback, preserve_local=preserve_local)
            if (rollback_ready and not restored and not preserve_local
                    and self._profile_local_changed(getattr(self, "_local_trigger_baseline", None))):
                try:
                    self._write_profile_intent(rollback, preserve_local=True)
                except (OSError, ValueError):
                    pass
                else:
                    restored = self._persist_profile_choice(rollback, preserve_local=True)
            self._set_profile_status(_("Profile was not applied because the current setup could not be saved."))
            if not restored:
                self._set_profile_status(_("Profile save and rollback failed. The previous setup is restored in memory. Check the data directory before restarting."))
            self._profile_selection_changed()
            return False
        self._profile_applied(profile, previous_speech)
        return True

    def _profile_applied(self, profile, previous_speech):
        self._active_profile_id = profile["id"]
        self._refresh_profiles(self._active_profile_id)
        self._clear_status_timers()
        self._clear_seq_runners()
        self._clear_callout_dedup()
        for trigger in self._triggers:
            trigger._last_fired.clear()
        for source in SOURCES:
            self._apply_engine_disabled(source)
            bridge = getattr(self, f"_{source}", None)
            if source == "cactbot" or bridge is None:
                continue
            defaults = self._callout_edits_for(source) or {}
            for ident in profile["engines"].get(source, {}):
                if ident in defaults:
                    bridge.set_callout(ident, tts=defaults[ident], text=defaults[ident])
                else:
                    bridge.reset_callout(ident)
            if source == "triggevent":
                changed = []
                for ident, previous in previous_speech.items():
                    current = self._triggevent_effective_tts(ident)
                    if previous is None or current is None or previous != current:
                        changed.append(ident)
                bridge.cancel_speech(changed)
        self._refresh_table()
        self._apply_tab_filter()
        name = _("Default") if profile["id"] == DEFAULT_PROFILE_ID else profile["name"]
        self._set_profile_status(_("Applied profile: {name}").format(name=name))
