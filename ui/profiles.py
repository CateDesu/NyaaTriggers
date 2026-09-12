"""Save and apply named trigger setups from the Triggers page."""

from copy import deepcopy

from PyQt6.QtWidgets import QComboBox, QHBoxLayout, QInputDialog, QLabel, QPushButton
from PyQt6.QtCore import Qt

import app_common as ac
from locale_util import _
from record_store import load_records, write_record
from trigger_profiles import SOURCES, apply_choices, capture_profile, validate_profile


class ProfilesMixin:
    def _build_profiles(self, layout):
        self._profiles_dir = ac._DATA_DIR / "trigger_profiles"
        self._profiles, errors = load_records(self._profiles_dir, validate_profile)
        controls = QHBoxLayout()
        controls.addWidget(QLabel(_("Profile:")))
        self._profile_picker = QComboBox()
        controls.addWidget(self._profile_picker, 1)
        self._profile_apply = QPushButton(_("Apply profile"))
        self._profile_new = QPushButton(_("Save new profile"))
        self._profile_update = QPushButton(_("Update saved profile"))
        for button in (self._profile_apply, self._profile_new, self._profile_update):
            controls.addWidget(button)
        layout.addLayout(controls)
        self._profile_status = QLabel()
        self._profile_status.setTextFormat(Qt.TextFormat.PlainText)
        self._profile_status.setWordWrap(True)
        layout.addWidget(self._profile_status)
        self._profile_apply.clicked.connect(self._apply_profile)
        self._profile_new.clicked.connect(self._save_new_profile)
        self._profile_update.clicked.connect(self._update_profile)
        self._refresh_profiles()
        if errors:
            self._profile_status.setText(_("Some profiles could not be read and were kept unchanged: {error}").format(error="\n".join(errors[:3])))
        else:
            self._profile_status.setText(_("Profiles save trigger choices and wording. Apply between pulls. New triggers keep their current settings."))

    def _refresh_profiles(self, ident=None):
        ident = ident or self._profile_picker.currentData()
        self._profile_picker.clear()
        for profile in self._profiles:
            self._profile_picker.addItem(profile["name"], profile["id"])
        index = self._profile_picker.findData(ident)
        self._profile_picker.setCurrentIndex(max(0, index))
        self._profile_apply.setEnabled(bool(self._profiles))
        self._profile_update.setEnabled(bool(self._profiles))

    def _selected_profile(self):
        ident = self._profile_picker.currentData()
        return next((p for p in self._profiles if p["id"] == ident), None)

    def _save_new_profile(self):
        name, accepted = QInputDialog.getText(self, _("Save new profile"), _("Profile name:"))
        if not accepted or not name.strip():
            return
        try:
            profile = capture_profile(self, name)
            write_record(self._profiles_dir, profile)
        except (OSError, ValueError) as exc:
            self._profile_status.setText(_("Could not save profile: {error}").format(error=exc))
            return
        self._profiles.append(profile)
        self._refresh_profiles(profile["id"])
        self._profile_status.setText(_("Saved profile: {name}").format(name=profile["name"]))

    def _update_profile(self):
        old = self._selected_profile()
        if old is None:
            return
        if ac.QMessageBox.question(self, _("Update saved profile"),
                                  _("Replace the saved choices in {name} with the current setup?").format(name=old["name"])) != ac.QMessageBox.StandardButton.Yes:
            return
        try:
            profile = capture_profile(self, old["name"], old["id"])
            write_record(self._profiles_dir, profile)
        except (OSError, ValueError) as exc:
            self._profile_status.setText(_("Could not save profile: {error}").format(error=exc))
            return
        self._profiles[self._profiles.index(old)] = profile
        self._profile_status.setText(_("Saved profile: {name}").format(name=profile["name"]))

    def _apply_profile(self):
        profile = self._selected_profile()
        if profile is None:
            return
        if self._in_game_combat or self._dps_meter.current is not None:
            self._profile_status.setText(_("Wait until combat ends before applying a profile."))
            return
        if getattr(self, "_local_corrupt", False):
            self._profile_status.setText(_("Repair the unreadable local trigger file before applying a profile."))
            return
        before = capture_profile(self, "Previous setup")
        old_settings = deepcopy(self._settings)
        old_local_ids = self._local_ids.copy()
        old_disabled = deepcopy(self._engine_disabled)
        old_edits = {src: deepcopy(getattr(self, f"_{src}_callout_edits", {})) for src in SOURCES}
        apply_choices(self, profile)
        if not self._save_triggers() or not self._save_settings():
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
            restored_triggers = self._save_triggers()
            restored_settings = self._save_settings()
            self._profile_status.setText(_("Profile was not applied because the current setup could not be saved."))
            if not restored_triggers or not restored_settings:
                self._profile_status.setText(_("Profile save and rollback failed. The previous setup is restored in memory. Check the data directory before restarting."))
            return
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
        self._refresh_table()
        self._apply_tab_filter()
        self._profile_status.setText(_("Applied profile: {name}").format(name=profile["name"]))
