import math
import re
import uuid
from pathlib import Path

from nyaatriggers.paths import bundle_root

from PyQt6.QtWidgets import (
    QCheckBox, QDialog, QDoubleSpinBox, QFileDialog, QFormLayout, QComboBox,
    QHBoxLayout, QDialogButtonBox, QLabel, QLineEdit, QMessageBox,
    QPushButton, QScrollArea, QSpinBox, QVBoxLayout, QWidget,
)
from PyQt6.QtCore import Qt

from nyaatriggers.trigger_engine import (Trigger, _HAVE_REGEX, _ID_IDX, _STATUS_TYPES,
                            _regex_mod, _regex_resource_limit, _safe_search,
                            _str_or, compile_user_regex)
from nyaatriggers.locale_util import _, N_

_SOUNDS_DIR = bundle_root() / "sounds"

_LOG_TYPES = [
    (N_("20 - NetworkStartsCasting"), "20"),
    (N_("21 - NetworkAbility"),       "21"),
    (N_("22 - NetworkAOEAbility"),    "22"),
    (N_("23 - NetworkCancelAbility"), "23"),
    (N_("26 - GainsEffect (status)"), "26"),
    (N_("30 - LosesEffect (status)"), "30"),
    (N_("00 - LogLine"),              "00"),
    (N_("Custom..."),                 None),
]

# Only gain line 26 carries duration. Loss line 30 uses a placeholder.
_DURATION_TYPES = frozenset({"26"})
_CUSTOM_IDX = len(_LOG_TYPES) - 1

_SEQ_LOG_TYPES = [("20", N_("20 - Cast")), ("21", N_("21 - Ability")),
                  ("22", N_("22 - AoE")),  ("23", N_("23 - Cancel"))]


def _regex_syntax_error(pattern: str) -> bool:
    # Do not compile again after a resource guard refuses a pattern.
    if _regex_resource_limit(pattern):
        return False
    # Use the matching engine to distinguish syntax errors from resource limits. Deep
    # nesting can also raise RecursionError.
    try:
        if _HAVE_REGEX:
            _regex_mod.compile(pattern)
        else:
            re.compile(pattern)
    except Exception:  # noqa: BLE001
        return True
    return False


class _StepRow(QWidget):
    def __init__(self, data: dict | None = None, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        self._type = QComboBox()
        self._type.setMaximumWidth(130)
        for val, label in _SEQ_LOG_TYPES:
            self._type.addItem(_(label), userData=val)

        self._id = QLineEdit()
        self._id.setPlaceholderText(_("Ability ID"))
        self._id.setMaximumWidth(90)

        self._regex = QLineEdit()
        self._regex.setPlaceholderText(_("Ability Regex"))

        self._timeout = QDoubleSpinBox()
        self._timeout.setRange(0.5, 120.0)
        self._timeout.setDecimals(1)
        self._timeout.setValue(10.0)
        self._timeout.setSuffix(" s")
        self._timeout.setMaximumWidth(75)

        del_btn = QPushButton("✕")
        del_btn.setFixedWidth(26)
        self.remove_btn = del_btn

        layout.addWidget(self._type)
        layout.addWidget(QLabel(_("ID:")))
        layout.addWidget(self._id)
        layout.addWidget(QLabel(_("Regex:")))
        layout.addWidget(self._regex, stretch=1)
        layout.addWidget(QLabel(_("Timeout:")))
        layout.addWidget(self._timeout)
        layout.addWidget(del_btn)

        # Remember a loaded value that was clamped so saving can warn about it.
        self._timeout_clamped = None

        if data:
            lt = _str_or(data.get("log_type"), "20")
            idx = next((i for i in range(self._type.count())
                        if self._type.itemData(i) == lt), -1)
            if idx < 0:
                # Add custom log types to the combo so they survive editing.
                self._type.addItem(lt, userData=lt)
                idx = self._type.count() - 1
            self._type.setCurrentIndex(idx)
            self._id.setText(_str_or(data.get("ability_id"), ""))
            self._regex.setText(_str_or(data.get("ability_regex"), ""))
            try:
                timeout = float(data.get("timeout_s", 10.0))
            except (TypeError, ValueError, OverflowError):
                timeout = 10.0
            # Use the default for nonfinite timeouts.
            if not math.isfinite(timeout):
                timeout = 10.0
            self._timeout.setValue(timeout)
            if self._timeout.value() != timeout:
                self._timeout_clamped = timeout
                # A user edit clears the warning for the loaded value.
                self._timeout.valueChanged.connect(self._clear_timeout_clamp)

    def _clear_timeout_clamp(self, _=None) -> None:
        self._timeout_clamped = None

    def to_dict(self) -> dict:
        d: dict = {
            "log_type": self._type.currentData(),
            "timeout_s": self._timeout.value(),
        }
        if self._id.text().strip():
            d["ability_id"] = self._id.text().strip()
        if self._regex.text().strip():
            d["ability_regex"] = self._regex.text().strip()
        return d


class _SequenceBuilder(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(4)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFixedHeight(130)
        self._scroll.setStyleSheet("QScrollArea { border: none; }")

        self._container = QWidget()
        self._steps_layout = QVBoxLayout(self._container)
        self._steps_layout.setContentsMargins(0, 0, 0, 0)
        self._steps_layout.setSpacing(2)
        self._steps_layout.addStretch()
        self._scroll.setWidget(self._container)

        self._rows: list[_StepRow] = []

        add_btn = QPushButton(_("+ Add Step"))
        add_btn.setMaximumWidth(100)
        add_btn.clicked.connect(lambda: self._add_row())

        outer.addWidget(self._scroll)
        outer.addWidget(add_btn)

    def _add_row(self, data: dict | None = None) -> None:
        row = _StepRow(data=data, parent=self._container)
        row.remove_btn.clicked.connect(lambda checked=False, r=row: self._remove_row(r))
        self._rows.append(row)
        self._steps_layout.insertWidget(self._steps_layout.count() - 1, row)

    def _remove_row(self, row: _StepRow) -> None:
        if row in self._rows:
            self._rows.remove(row)
        row.setParent(None)
        row.deleteLater()

    def get_sequence(self) -> list:
        return [r.to_dict() for r in self._rows]

    def set_sequence(self, steps: list) -> None:
        for row in list(self._rows):
            self._remove_row(row)
        for step in steps:
            self._add_row(step)


class TriggerDialog(QDialog):
    def __init__(self, trigger: Trigger | None = None, parent=None, current_zone: str = "",
                 fight_picker=None, current_fight: str = ""):
        super().__init__(parent)
        self.setWindowTitle(_("Edit Trigger") if trigger else _("New Trigger"))
        self.setMinimumWidth(520)
        self._current_zone = current_zone
        self._fight_picker = fight_picker
        # Keep the original value before the widget clamps it.
        self._clamped = []
        self._build_ui()
        if current_fight and not trigger:
            self._fight.setText(current_fight)
        if trigger:
            self._populate(trigger)

    def _build_ui(self) -> None:
        layout = QFormLayout(self)
        self._form = layout
        layout.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)

        self._name = QLineEdit()
        layout.addRow(_("Name:"), self._name)

        self._type_combo = QComboBox()
        for label, _unused in _LOG_TYPES:
            self._type_combo.addItem(_(label))
        self._type_custom = QLineEdit()
        self._type_custom.setPlaceholderText(_("type number"))
        self._type_custom.setVisible(False)
        self._type_combo.currentIndexChanged.connect(self._on_type_changed)
        # Update applicable fields when a custom log type is typed.
        self._type_custom.textChanged.connect(lambda _=None: self._update_dur_row_visibility())
        self._type_custom.textChanged.connect(lambda _=None: self._update_id_enabled())
        type_row = QWidget()
        rh = QHBoxLayout(type_row)
        rh.setContentsMargins(0, 0, 0, 0)
        rh.addWidget(self._type_combo, stretch=2)
        rh.addWidget(self._type_custom, stretch=1)
        layout.addRow(_("Log Type:"), type_row)

        self._ability_id = QLineEdit()
        self._ability_id.setPlaceholderText(_("Hex ID from ACT log field[4] - e.g. A55B or A55D|A55E"))
        layout.addRow(_("Ability ID:"), self._ability_id)

        self._regex = QLineEdit()
        self._regex.setPlaceholderText(_("Regex on ability name - used when Ability ID is blank"))
        self._regex_dot = QLabel()
        self._regex_dot.setFixedWidth(20)
        regex_row = QWidget()
        rr = QHBoxLayout(regex_row)
        rr.setContentsMargins(0, 0, 0, 0)
        rr.addWidget(self._regex)
        rr.addWidget(self._regex_dot)
        self._regex.textChanged.connect(self._update_regex_dot)
        layout.addRow(_("Ability Regex:"), regex_row)

        # Duration filters apply only to status gain lines.
        self._dur_min = QDoubleSpinBox()
        self._dur_max = QDoubleSpinBox()
        for sb in (self._dur_min, self._dur_max):
            sb.setRange(0.0, 9999.0)
            sb.setDecimals(1)
            sb.setSuffix(" s")
            sb.setMaximumWidth(90)
        self._dur_row = QWidget()
        dh = QHBoxLayout(self._dur_row)
        dh.setContentsMargins(0, 0, 0, 0)
        dh.addWidget(QLabel(_("min")))
        dh.addWidget(self._dur_min)
        dh.addWidget(QLabel(_("max")))
        dh.addWidget(self._dur_max)
        dh.addWidget(QLabel(_("(0 = any; for ordered-timer mechanics)")))
        dh.addStretch(1)
        layout.addRow(_("Duration:"), self._dur_row)

        self._count_min = QSpinBox()
        self._count_max = QSpinBox()
        for sb in (self._count_min, self._count_max):
            sb.setRange(0, 999)
            sb.setMaximumWidth(90)
        self._count_row = QWidget()
        ch = QHBoxLayout(self._count_row)
        ch.setContentsMargins(0, 0, 0, 0)
        ch.addWidget(QLabel(_("min")))
        ch.addWidget(self._count_min)
        ch.addWidget(QLabel(_("max")))
        ch.addWidget(self._count_max)
        ch.addWidget(QLabel(_("(0 = any; for stacking debuffs. {count} token available)")))
        ch.addStretch(1)
        layout.addRow(_("Stacks:"), self._count_row)

        self._scope = QComboBox()
        self._scope.addItem(_("You - the effect is on you"), "self")
        self._scope.addItem(_("Target - you applied it (e.g. Death's Design)"), "by_me")
        self._scope.addItem(_("Anyone"), "any")
        layout.addRow(_("Applies to:"), self._scope)

        self._warn = QDoubleSpinBox()
        self._warn.setRange(0.0, 60.0)
        self._warn.setDecimals(1)
        self._warn.setSingleStep(0.5)
        self._warn.setSuffix(" s")
        self._warn.setMaximumWidth(90)
        self._warn_row = QWidget()
        wh = QHBoxLayout(self._warn_row)
        wh.setContentsMargins(0, 0, 0, 0)
        wh.addWidget(self._warn)
        wh.addWidget(QLabel(_("before it expires (0 = speak on apply; needs Log Type 26)")))
        wh.addStretch(1)
        layout.addRow(_("Reapply warning:"), self._warn_row)

        self._tts = QLineEdit()
        self._tts.setPlaceholderText(_("Spoken text - use {source}, {target} or {count} as tokens"))
        tts_test = QPushButton("▶")
        tts_test.setMaximumWidth(30)
        tts_test.clicked.connect(self._test_tts)
        tts_row = QWidget()
        tr = QHBoxLayout(tts_row)
        tr.setContentsMargins(0, 0, 0, 0)
        tr.setSpacing(4)
        tr.addWidget(self._tts)
        tr.addWidget(tts_test)
        layout.addRow(_("TTS Text:"), tts_row)

        self._sound = QLineEdit()
        self._sound.setPlaceholderText(_("Path to .wav file - leave blank for none"))
        sound_browse = QPushButton(_("Browse"))
        sound_browse.setMaximumWidth(70)
        sound_browse.clicked.connect(self._browse_sound)
        sound_test = QPushButton("▶")
        sound_test.setMaximumWidth(30)
        sound_test.clicked.connect(self._test_sound)
        sound_row = QWidget()
        sh = QHBoxLayout(sound_row)
        sh.setContentsMargins(0, 0, 0, 0)
        sh.setSpacing(4)
        sh.addWidget(self._sound)
        sh.addWidget(sound_browse)
        sh.addWidget(sound_test)
        layout.addRow(_("Alert Sound:"), sound_row)

        self._fight = QLineEdit()
        self._fight.setPlaceholderText(_("e.g. M4S, FRU, UWU"))
        fight_row = QWidget()
        fr = QHBoxLayout(fight_row)
        fr.setContentsMargins(0, 0, 0, 0)
        fr.setSpacing(4)
        fr.addWidget(self._fight)
        if self._fight_picker is not None:
            fight_pick = QPushButton(_("Pick…"))
            fight_pick.setMaximumWidth(70)
            fight_pick.clicked.connect(self._pick_fight)
            fr.addWidget(fight_pick)
        layout.addRow(_("Fight Tag:"), fight_row)

        self._zone = QLineEdit()
        self._zone.setPlaceholderText(_("Regex matched against zone name - leave blank for any zone"))
        self._zone_dot = QLabel()
        self._zone_dot.setFixedWidth(20)
        zone_row = QWidget()
        zh = QHBoxLayout(zone_row)
        zh.setContentsMargins(0, 0, 0, 0)
        zh.addWidget(self._zone)
        zh.addWidget(self._zone_dot)
        self._zone.textChanged.connect(self._update_zone_dot)
        layout.addRow(_("Zone Regex:"), zone_row)

        # Cooldown spaces reminders without blocking timer refreshes. It also combines
        # bursts from the same AoE.
        self._cooldown = QDoubleSpinBox()
        self._cooldown.setRange(0.0, 3600.0)
        self._cooldown.setDecimals(1)
        self._cooldown.setSuffix(" s")
        self._cooldown.setValue(5.0)
        layout.addRow(_("Cooldown:"), self._cooldown)

        self._speed = QDoubleSpinBox()
        self._speed.setRange(0.5, 3.0)
        self._speed.setDecimals(1)
        self._speed.setSingleStep(0.1)
        self._speed.setValue(1.0)
        self._speed.setMaximumWidth(70)
        layout.addRow(_("Speed:"), self._speed)

        self._interrupt = QCheckBox(_("Interrupt - cut current TTS and speak immediately"))
        layout.addRow("", self._interrupt)

        self._sequence = _SequenceBuilder()
        layout.addRow(_("Follow-up Steps:"), self._sequence)

        self._enabled = QCheckBox(_("Enabled"))
        self._enabled.setChecked(True)
        layout.addRow("", self._enabled)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        layout.addRow(btns)

        self._update_dur_row_visibility()
        self._update_id_enabled()

    def _update_zone_dot(self, pattern: str) -> None:
        if not pattern:
            self._zone_dot.setText("")
            return
        rx = compile_user_regex(pattern, re.IGNORECASE)
        if rx is None:
            color = "#5e6480"
            tip = _("Invalid regex")
        elif not self._current_zone:
            color = "#5e6480"
            tip = ""
        else:
            matched = bool(_safe_search(rx, self._current_zone))
            color = "#a6e3a1" if matched else "#f38ba8"
            tip = (_("Matches: {zone}").format(zone=self._current_zone) if matched
                   else _("No match: {zone}").format(zone=self._current_zone))
        self._zone_dot.setText(f'<span style="color:{color}">●</span>')
        self._zone_dot.setToolTip(tip)

    def _update_regex_dot(self, pattern: str) -> None:
        if not pattern:
            self._regex_dot.setText("")
            return
        if compile_user_regex(pattern) is not None:
            self._regex_dot.setText('<span style="color:#a6e3a1">●</span>')
            self._regex_dot.setToolTip("")
        else:
            self._regex_dot.setText('<span style="color:#f38ba8">●</span>')
            self._regex_dot.setToolTip(
                _("Invalid regex - the engine cannot compile it"))

    def _on_type_changed(self, idx: int) -> None:
        self._type_custom.setVisible(idx == _CUSTOM_IDX)
        self._update_dur_row_visibility()
        self._update_id_enabled()

    def _update_dur_row_visibility(self) -> None:
        parts = self._log_type_parts()
        dur_visible = bool(parts & _DURATION_TYPES)
        status_visible = bool(parts & _STATUS_TYPES)
        warn_visible = "26" in parts and parts <= _STATUS_TYPES
        if hasattr(self._form, "setRowVisible"):
            self._form.setRowVisible(self._dur_row, dur_visible)
            self._form.setRowVisible(self._count_row, status_visible)
            self._form.setRowVisible(self._scope, status_visible)
            self._form.setRowVisible(self._warn_row, warn_visible)
        else:  # Older Qt versions can hide only the fields.
            self._dur_row.setVisible(dur_visible)
            self._count_row.setVisible(status_visible)
            self._scope.setVisible(status_visible)
            self._warn_row.setVisible(warn_visible)

    def _update_id_enabled(self) -> None:
        # ID filtering requires an ID field in every selected log type.
        self._ability_id.setEnabled(self._log_type_parts() <= _ID_IDX.keys())

    def _browse_sound(self) -> None:
        start = str(_SOUNDS_DIR) if _SOUNDS_DIR.exists() else str(Path.home())
        path, _unused =QFileDialog.getOpenFileName(
            self, _("Select alert sound"), start, _("WAV files (*.wav)") + ";;" + _("All files (*)")
        )
        if path:
            self._sound.setText(path)

    def _test_sound(self) -> None:
        path = self._sound.text().strip()
        if path:
            from nyaatriggers.tts import play_sound
            play_sound(path)

    _PREVIEW_TOKENS = {"source": "the boss", "target": "you", "count": "2"}

    def _test_tts(self) -> None:
        text = self._tts.text().strip()
        if not text:
            return
        sub = lambda m: self._PREVIEW_TOKENS.get(m.group(1), m.group(1))
        preview = re.sub(r"\{(\w+)\}", sub, text)
        from nyaatriggers.tts import speak, reading_for
        # Resolve the kana reading before substituting tokens.
        reading = reading_for(text)
        spoken = re.sub(r"\{(\w+)\}", sub, reading) if reading else None
        speak(preview, speed=self._speed.value(), reading=spoken)

    def _pick_fight(self) -> None:
        if self._fight_picker is None:
            return
        chosen = self._fight_picker()
        # An empty category is intentional. None means the picker was cancelled.
        if chosen is not None:
            self._fight.setText(chosen)

    def _load_spin(self, spin, value, label: str) -> None:
        # Clamp before converting to a C++ integer and keep the original for the save
        # warning.
        spin.setValue(max(spin.minimum(), min(spin.maximum(), value)))
        if spin.value() != value:
            self._clamped.append((spin, label, value))
            # Connect after loading so only user edits clear the warning.
            spin.valueChanged.connect(lambda _=None, s=spin: self._discard_clamp(s))

    def _discard_clamp(self, spin) -> None:
        self._clamped = [entry for entry in self._clamped if entry[0] is not spin]

    def _populate(self, t: Trigger) -> None:
        self._name.setText(t.name)
        self._ability_id.setText(t.ability_id)
        self._regex.setText(t.ability_regex)
        self._tts.setText(t.tts_text)
        self._sound.setText(t.sound_file)
        self._fight.setText(t.fight)
        self._zone.setText(t.zone_regex)
        self._load_spin(self._cooldown, t.cooldown_s, _("Cooldown"))
        self._load_spin(self._speed, t.speed, _("Speed"))
        self._interrupt.setChecked(t.interrupt)
        self._load_spin(self._dur_min, t.duration_min, _("Duration min"))
        self._load_spin(self._dur_max, t.duration_max, _("Duration max"))
        self._load_spin(self._count_min, t.count_min, _("Stacks min"))
        self._load_spin(self._count_max, t.count_max, _("Stacks max"))
        scope_idx = self._scope.findData(t.status_scope or "self")
        self._scope.setCurrentIndex(scope_idx if scope_idx >= 0 else 0)
        self._load_spin(self._warn, t.expiry_warn_s, _("Reapply warning"))
        self._enabled.setChecked(t.enabled)
        if t.sequence:
            self._sequence.set_sequence(t.sequence)

        for i, (_lbl, val) in enumerate(_LOG_TYPES[:-1]):
            if val == t.log_type:
                self._type_combo.setCurrentIndex(i)
                return
        self._type_combo.setCurrentIndex(_CUSTOM_IDX)
        self._type_custom.setText(t.log_type)

    def _log_type(self) -> str:
        idx = self._type_combo.currentIndex()
        if idx == _CUSTOM_IDX:
            return self._type_custom.text().strip() or "20"
        return _LOG_TYPES[idx][1]

    def _log_type_parts(self) -> set:
        return {p.strip() for p in self._log_type().split("|") if p.strip()}

    def _usable_ability_id(self) -> str:
        # Ignore a stale ID filter when the selected types have no ID field.
        if self._log_type_parts() <= _ID_IDX.keys():
            return self._ability_id.text().strip()
        return ""

    def _pending_clamps(self) -> list:
        # Warn only about clamped fields that will be saved.
        keep_dur = bool(self._log_type_parts() & _DURATION_TYPES)
        keep_count = bool(self._log_type_parts() & _STATUS_TYPES)
        keep_warn = ("26" in self._log_type_parts()
                     and self._log_type_parts() <= _STATUS_TYPES)
        entries = [(label, original, spin.value())
                   for spin, label, original in self._clamped
                   if (keep_dur or spin not in (self._dur_min, self._dur_max))
                   and (keep_count or spin not in (self._count_min, self._count_max))
                   and (keep_warn or spin is not self._warn)]
        for n, row in enumerate(self._sequence._rows, 1):
            if row._timeout_clamped is not None:
                entries.append((_("Step {n} timeout").format(n=n),
                                row._timeout_clamped, row._timeout.value()))
        return entries

    def accept(self) -> None:
        if self._type_combo.currentIndex() == _CUSTOM_IDX:
            custom = self._type_custom.text().strip()
            # Wire types use two ASCII digits. Regex \d also accepts other digits.
            if not re.fullmatch(r"[0-9]{2}(\|[0-9]{2})*", custom):
                QMessageBox.warning(
                    self, _("Invalid log type"),
                    _("The custom log type must be a 2 digit type number, or "
                      "several pipe-separated like 21|22. Anything else never "
                      "matches a log line, so this trigger would never fire."))
                return
        # The ID filter takes precedence over regex.
        aid = self._usable_ability_id()
        if aid and not re.fullmatch(r"[0-9A-Fa-f]+(\|[0-9A-Fa-f]+)*", aid):
            QMessageBox.warning(
                self, _("Invalid ability ID"),
                _("The Ability ID must be hex digits, or several pipe-separated "
                  "like A55D|A55E. Anything else never matches a log line, so "
                  "this trigger would never fire."))
            return
        # Validate regex only when no ID filter is set.
        pattern = self._regex.text().strip()
        if (pattern and not aid
                and compile_user_regex(pattern) is None):
            if _regex_syntax_error(pattern):
                title = _("Invalid regex")
                body = _("The ability regex does not compile, and with no "
                         "Ability ID set this trigger would never fire. Fix "
                         "the regex or enter an Ability ID.")
            else:
                title = _("Unsafe regex")
                body = _("The ability regex was rejected as potentially "
                         "catastrophic, and with no Ability ID set this "
                         "trigger would never fire. Simplify the regex or "
                         "enter an Ability ID.")
            QMessageBox.warning(self, title, body)
            return
        # Sequence steps use the same ID first validation.
        for n, row in enumerate(self._sequence._rows, 1):
            step_aid = row._id.text().strip()
            if step_aid and not re.fullmatch(r"[0-9A-Fa-f]+(\|[0-9A-Fa-f]+)*",
                                             step_aid):
                QMessageBox.warning(
                    self, _("Invalid ability ID"),
                    _("The Ability ID for step {n} is not hex, so the sequence "
                      "could never advance past that step. Fix the ID or clear "
                      "it.").format(n=n))
                return
            step_pattern = row._regex.text().strip()
            if (step_pattern and not step_aid
                    and compile_user_regex(step_pattern) is None):
                if _regex_syntax_error(step_pattern):
                    title = _("Invalid regex")
                    body = _("The ability regex for step {n} does not "
                             "compile, so the sequence could never advance "
                             "past that step. Fix the regex or clear it.")
                else:
                    title = _("Unsafe regex")
                    body = _("The ability regex for step {n} was rejected as "
                             "potentially catastrophic, so the sequence "
                             "could never advance past that step. Simplify "
                             "the regex or clear it.")
                QMessageBox.warning(self, title, body.format(n=n))
                return
        # A nonempty zone regex always applies.
        zone = self._zone.text().strip()
        if zone and compile_user_regex(zone) is None:
            if _regex_syntax_error(zone):
                title = _("Invalid regex")
                body = _("The zone regex does not compile, so this trigger "
                         "would never fire. Fix the regex or leave the zone "
                         "blank.")
            else:
                title = _("Unsafe regex")
                body = _("The zone regex was rejected as potentially "
                         "catastrophic, so this trigger would never fire. "
                         "Simplify the regex or leave the zone blank.")
            QMessageBox.warning(self, title, body)
            return
        # Zero leaves the upper bound open.
        parts = self._log_type_parts()
        if parts & _DURATION_TYPES:
            if 0 < self._dur_max.value() < self._dur_min.value():
                QMessageBox.warning(
                    self, _("Invalid duration window"),
                    _("Duration min is greater than duration max, so this "
                      "trigger would never fire. Fix the window, or set max "
                      "to 0 for no upper bound."))
                return
        if parts & _STATUS_TYPES:
            if 0 < self._count_max.value() < self._count_min.value():
                QMessageBox.warning(
                    self, _("Invalid stacks window"),
                    _("Stacks min is greater than stacks max, so this "
                      "trigger would never fire. Fix the window, or set max "
                      "to 0 for no upper bound."))
                return
        # Require an ID or regex to avoid matching every line.
        if not aid and not pattern:
            QMessageBox.warning(
                self, _("No matcher"),
                _("This trigger has no Ability ID and no ability regex, so it "
                  "would fire on every line of its log type. Enter an Ability "
                  "ID or a regex."))
            return
        clamped = self._pending_clamps()
        if clamped:
            details = "\n".join(f"{label}: {original} → {new}"
                                for label, original, new in clamped)
            choice = QMessageBox.warning(
                self, _("Out of range values"),
                _("Some saved values are outside the range this editor "
                  "allows and would be written back clamped:"
                  "\n\n{details}\n\n"
                  "OK saves the clamped values. Cancel returns to editing."
                  ).format(details=details),
                QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel)
            if choice != QMessageBox.StandardButton.Ok:
                return
        super().accept()

    def get_trigger(self, existing_id: str | None = None) -> Trigger:
        lt = self._log_type()
        parts = self._log_type_parts()
        # Save only fields supported by the selected log types.
        has_dur = bool(parts & _DURATION_TYPES)
        has_status = bool(parts & _STATUS_TYPES)
        dmin = self._dur_min.value() if has_dur else 0.0
        dmax = self._dur_max.value() if has_dur else 0.0
        cmin = self._count_min.value() if has_status else 0
        cmax = self._count_max.value() if has_status else 0
        scope = self._scope.currentData() if has_status else "self"
        # Expiry requires status types including gain 26. Mixed ability types are not
        # eligible.
        warn = (self._warn.value()
                if "26" in parts and parts <= _STATUS_TYPES else 0.0)
        return Trigger(
            id=existing_id or str(uuid.uuid4()),
            name=self._name.text().strip() or _("Unnamed"),
            log_type=lt,
            ability_id=self._usable_ability_id(),
            ability_regex=self._regex.text().strip(),
            tts_text=self._tts.text().strip(),
            cooldown_s=self._cooldown.value(),
            enabled=self._enabled.isChecked(),
            fight=self._fight.text().strip(),
            zone_regex=self._zone.text().strip(),
            sequence=self._sequence.get_sequence(),
            speed=self._speed.value(),
            interrupt=self._interrupt.isChecked(),
            duration_min=dmin,
            duration_max=dmax,
            count_min=cmin,
            count_max=cmax,
            status_scope=scope,
            expiry_warn_s=warn,
            sound_file=self._sound.text().strip(),
        )
