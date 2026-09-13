"""Save and apply named trigger setups from the Triggers page."""

from copy import deepcopy

from PyQt6.QtWidgets import (
    QComboBox, QHBoxLayout, QInputDialog, QLabel, QPushButton, QSizePolicy,
    QToolButton, QVBoxLayout, QWidget,
)
from PyQt6.QtCore import Qt

from nyaatriggers import app_common as ac
from nyaatriggers.locale_util import _
from nyaatriggers.record_store import load_records, write_record
from nyaatriggers.trigger_profiles import SOURCES, apply_choices, capture_profile, validate_profile


class ProfilesMixin:
    def _build_profiles(self, layout):
        self._profiles_dir = ac._DATA_DIR / "trigger_profiles"
        self._profiles, errors = load_records(self._profiles_dir, validate_profile)
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
        self._profile_picker.setPlaceholderText(_("No saved profiles"))
        self._profile_picker.currentTextChanged.connect(self._profile_picker.setToolTip)
        controls.addWidget(self._profile_picker)
        self._profile_apply = QPushButton(_("Apply"))
        self._profile_new = QPushButton(_("Save new"))
        self._profile_update = QPushButton(_("Update"))
        for button, tooltip in (
            (self._profile_apply, _("Apply profile")),
            (self._profile_new, _("Save new profile")),
            (self._profile_update, _("Update saved profile")),
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
        self._refresh_profiles()
        if errors:
            self._set_profile_status(_("Some profiles could not be read and were kept unchanged: {error}").format(error="\n".join(errors[:3])))

    def _toggle_profiles(self, expanded):
        self._profile_body.setVisible(expanded)
        self._profile_toggle.setArrowType(Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow)

    def _set_profile_status(self, text):
        self._profile_status.setText(text)
        self._profile_status.setVisible(bool(text))
        if text:
            self._profile_toggle.setChecked(True)

    def _refresh_profiles(self, ident=None):
        ident = ident or self._profile_picker.currentData()
        self._profile_picker.clear()
        for profile in self._profiles:
            self._profile_picker.addItem(profile["name"], profile["id"])
        index = self._profile_picker.findData(ident)
        self._profile_picker.setCurrentIndex(max(0, index))
        self._profile_picker.setEnabled(bool(self._profiles))
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
            self._set_profile_status(_("Could not save profile: {error}").format(error=exc))
            return
        self._profiles.append(profile)
        self._refresh_profiles(profile["id"])
        self._set_profile_status(_("Saved profile: {name}").format(name=profile["name"]))

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
            self._set_profile_status(_("Could not save profile: {error}").format(error=exc))
            return
        self._profiles[self._profiles.index(old)] = profile
        self._set_profile_status(_("Saved profile: {name}").format(name=profile["name"]))

    def _apply_profile(self):
        profile = self._selected_profile()
        if profile is None:
            return
        if self._in_game_combat or self._dps_meter.current is not None:
            self._set_profile_status(_("Wait until combat ends before applying a profile."))
            return
        if getattr(self, "_local_corrupt", False):
            self._set_profile_status(_("Repair the unreadable local trigger file before applying a profile."))
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
            self._set_profile_status(_("Profile was not applied because the current setup could not be saved."))
            if not restored_triggers or not restored_settings:
                self._set_profile_status(_("Profile save and rollback failed. The previous setup is restored in memory. Check the data directory before restarting."))
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
        self._set_profile_status(_("Applied profile: {name}").format(name=profile["name"]))
