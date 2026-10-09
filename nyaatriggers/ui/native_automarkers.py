from copy import deepcopy

from PyQt6.QtCore import QSignalBlocker, Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QFormLayout, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit, QListWidget, QPushButton, QSpinBox,
    QTabWidget, QVBoxLayout, QWidget,
)

from nyaatriggers.locale_util import _


class NativeAutomarkersPanel(QWidget):
    changed = pyqtSignal(str, object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._settings = {}
        self._values = {}
        self._invalid_overrides = set()
        self._editors = {}
        self._tabs = None
        self._loading = False
        self._jobs = []
        self._markers = []
        self._blocked_umad = False
        self._umad_note = None
        self._engine_message = _("Loading native automarker controls.")
        self._engine_note = QLabel(self._engine_message)
        self._engine_note.setWordWrap(True)
        self._layout.addWidget(self._engine_note)

    def set_engine_status(self, message):
        self._engine_message = message
        if message and self._settings:
            message += "\n" + _("Changes are saved and applied when the Triggevent engine is ready.")
        self._engine_note.setText(message)
        self._engine_note.setVisible(bool(message))
        self.setEnabled(bool(self._settings))

    def set_inventory(self, payload, overrides, requested_umad, blocked_umad):
        self._loading = True
        self._engine_message = ""
        self.setEnabled(True)
        previous_tab = getattr(self, "_tabs", None)
        previous_fight = previous_tab.tabText(previous_tab.currentIndex()) if previous_tab else ""
        selections = {ident: editor._input.currentItem().text() for ident, editor in self._editors.items()
                      if hasattr(editor, "_priority_note") and editor._input.currentItem() is not None}
        while self._layout.count():
            item = self._layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
        self._tabs = None
        self._settings = {}
        self._values = {}
        self._invalid_overrides = set()
        self._editors = {}
        self._umad_note = None
        self._blocked_umad = bool(blocked_umad)
        payload = payload if isinstance(payload, dict) else {}
        overrides = overrides if isinstance(overrides, dict) else {}
        self._jobs = self._choices(payload.get("jobs"))
        self._markers = self._choices(payload.get("markers"))
        self._engine_note = QLabel(self._engine_message)
        self._engine_note.setWordWrap(True)
        self._engine_note.setVisible(bool(self._engine_message))
        self._layout.addWidget(self._engine_note)
        error = QLabel(payload.get("error") if isinstance(payload.get("error"), str) else "")
        error.setObjectName("native_automarker_error")
        error.setTextFormat(Qt.TextFormat.PlainText)
        error.setWordWrap(True)
        error.setVisible(bool(error.text()))
        self._layout.addWidget(error)
        for setting in payload.get("settings", ()) if isinstance(payload.get("settings"), list) else ():
            if not isinstance(setting, dict) or not isinstance(setting.get("id"), str):
                continue
            ident = setting["id"]
            if ident in self._settings:
                continue
            value = bool(requested_umad) if ident == "native_umad" else overrides.get(ident, setting.get("value"))
            if not self._valid(setting, value):
                if ident in overrides:
                    self._invalid_overrides.add(ident)
                value = setting.get("value", setting.get("default"))
            if not self._valid(setting, value):
                value = setting.get("default")
            if self._valid(setting, value):
                self._settings[ident] = setting
                self._values[ident] = deepcopy(value)
        if not self._settings:
            self.setEnabled(False)
            self._layout.addWidget(QLabel(_("Native automarker controls are unavailable.")))
            self._loading = False
            return
        rendered = set()
        transport = [ident for ident in self._settings if ident.startswith("telesto.")]
        if transport:
            group = self._group(_("Marking transport"), transport)
            self._layout.addWidget(group)
            rendered.update(transport)
        self._tabs = QTabWidget()
        self._tabs.setObjectName("native_automarker_duties")
        self._layout.addWidget(self._tabs)
        fights = {}
        mechanics = payload.get("mechanics")
        for mechanic in mechanics if isinstance(mechanics, list) else ():
            if not isinstance(mechanic, dict) or not isinstance(mechanic.get("fight"), str):
                continue
            fight = mechanic["fight"]
            if fight not in fights:
                page = QWidget()
                fights[fight] = QVBoxLayout(page)
                self._tabs.addTab(page, fight)
            ids = mechanic.get("settings")
            ids = [ident for ident in ids if isinstance(ident, str) and ident in self._settings] if isinstance(ids, list) else []
            fresh = list(dict.fromkeys(ident for ident in ids if ident not in rendered))
            shared = list(dict.fromkeys(ident for ident in ids if ident in rendered and ident not in transport))
            group = self._group(str(mechanic.get("name") or mechanic.get("id") or fight), fresh)
            group.setObjectName("native_mechanic." + str(mechanic.get("id") or ""))
            if shared:
                labels = ", ".join(str(self._settings[ident].get("label") or ident) for ident in shared)
                note = QLabel(_("Shared controls: {controls}").format(controls=labels))
                note.setWordWrap(True)
                group.layout().addRow(note)
            fights[fight].addWidget(group)
            rendered.update(fresh)
        remaining = [ident for ident in self._settings if ident not in rendered]
        if remaining:
            self._layout.addWidget(self._group(_("Other native controls"), remaining))
        for layout in fights.values():
            layout.addStretch(1)
        for index in range(self._tabs.count()):
            if self._tabs.tabText(index) == previous_fight:
                self._tabs.setCurrentIndex(index)
        for ident, selected in selections.items():
            editor = self._editors.get(ident)
            if editor is not None and hasattr(editor, "_priority_note"):
                matches = editor._input.findItems(selected, Qt.MatchFlag.MatchExactly)
                if matches:
                    editor._input.setCurrentItem(matches[0])
        self._refresh_priorities()
        self._loading = False

    def set_umad_state(self, requested_umad, blocked_umad):
        self._blocked_umad = bool(blocked_umad)
        editor = self._editors.get("native_umad")
        if editor is not None:
            self._values["native_umad"] = bool(requested_umad)
            with QSignalBlocker(editor._input):
                editor._input.setChecked(bool(requested_umad))
        if self._umad_note is not None:
            self._umad_note.setVisible(self._blocked_umad)

    @staticmethod
    def _choices(items):
        if not isinstance(items, list):
            return []
        result = []
        seen = set()
        for item in items:
            if isinstance(item, dict) and isinstance(item.get("id"), str) and item["id"] not in seen:
                result.append((item["id"], str(item.get("label") or item["id"])))
                seen.add(item["id"])
        return result

    def _valid(self, setting, value):
        kind = setting.get("type")
        if kind == "boolean":
            return type(value) is bool
        if kind == "integer":
            minimum = setting.get("min", -(1 << 63))
            maximum = setting.get("max", (1 << 63) - 1)
            return type(value) is int and type(minimum) is int and type(maximum) is int and minimum <= value <= maximum
        if kind == "jobs":
            choices = {ident for ident, _label in self._jobs}
            return isinstance(value, list) and all(isinstance(item, str) for item in value) and len(value) == len(choices) and set(value) == choices
        if kind == "marker_map":
            slots = self._choices(setting.get("slots"))
            markers = {ident for ident, _label in self._markers}
            return (isinstance(value, dict) and set(value) == {ident for ident, _label in slots}
                    and bool(slots) and all(isinstance(row, dict) and type(row.get("enabled")) is bool
                                          and isinstance(row.get("marker"), str) and row["marker"] in markers
                                          for row in value.values()))
        return False

    def _group(self, label, ids):
        group = QGroupBox(label)
        layout = QFormLayout(group)
        for ident in ids:
            setting = self._settings[ident]
            editor = self._editor(setting)
            editor.setObjectName(ident)
            self._editors[ident] = editor
            layout.addRow(str(setting.get("label") or ident), editor)
        return group

    def _editor(self, setting):
        ident = setting["id"]
        value = self._values[ident]
        kind = setting["type"]
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        if kind == "boolean":
            widget = QCheckBox()
            widget.setObjectName(ident + ".value")
            widget.setChecked(value)
            widget.toggled.connect(lambda checked: self._emit(ident, checked))
            layout.addWidget(widget)
            container._input = widget
            if ident == "native_umad":
                note = QLabel(_("Local UMAD P4 controls currently own marking."))
                note.setWordWrap(True)
                note.setVisible(self._blocked_umad)
                layout.addWidget(note)
                self._umad_note = note
        elif kind == "integer":
            minimum, maximum = setting.get("min"), setting.get("max")
            if type(minimum) is int and type(maximum) is int and -(1 << 31) <= minimum <= maximum < (1 << 31):
                widget = QSpinBox()
                widget.setRange(minimum, maximum)
                widget.setKeyboardTracking(False)
                widget.setValue(value)
                widget.valueChanged.connect(lambda number: self._emit(ident, number))
            else:
                widget = QLineEdit(str(value))
                widget.editingFinished.connect(lambda: self._integer_edit(ident, widget))
            widget.setObjectName(ident + ".value")
            layout.addWidget(widget)
            container._input = widget
        elif kind == "jobs":
            widget = QListWidget()
            widget.setObjectName(ident + ".value")
            widget.setMaximumHeight(155)
            widget.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
            widget.addItems(value)
            widget.model().rowsMoved.connect(lambda *_args: self._jobs_edit(ident, widget))
            layout.addWidget(widget)
            buttons = QHBoxLayout()
            for label, direction in ((_("Up"), -1), (_("Down"), 1)):
                button = QPushButton(label)
                button.setObjectName(ident + (".up" if direction < 0 else ".down"))
                button.clicked.connect(lambda _checked=False, delta=direction: self._move_job(ident, widget, delta))
                buttons.addWidget(button)
            layout.addLayout(buttons)
            note = QLabel()
            note.setObjectName(ident + ".effective")
            note.setWordWrap(True)
            layout.addWidget(note)
            container._input = widget
            container._priority_note = note
            container._priority_buttons = [buttons.itemAt(index).widget() for index in range(buttons.count())]
        elif kind == "marker_map":
            rows = QFormLayout()
            container._slots = {}
            for slot, label in self._choices(setting.get("slots")):
                row = QWidget()
                row_layout = QHBoxLayout(row)
                row_layout.setContentsMargins(0, 0, 0, 0)
                enabled = QCheckBox()
                enabled.setObjectName(ident + "." + slot + ".enabled")
                enabled.setChecked(value[slot]["enabled"])
                marker = QComboBox()
                marker.setObjectName(ident + "." + slot + ".marker")
                for token, name in self._markers:
                    marker.addItem(name, token)
                marker.setCurrentIndex(marker.findData(value[slot]["marker"]))
                marker.setEnabled(enabled.isChecked())
                enabled.toggled.connect(lambda checked, combo=marker: combo.setEnabled(checked))
                enabled.toggled.connect(lambda _checked, cid=ident, control=container: self._map_edit(cid, control))
                marker.currentIndexChanged.connect(lambda _index, cid=ident, control=container: self._map_edit(cid, control))
                row_layout.addWidget(enabled)
                row_layout.addWidget(marker)
                rows.addRow(label, row)
                container._slots[slot] = (enabled, marker)
            layout.addLayout(rows)
            presets = QComboBox()
            presets.setObjectName(ident + ".preset")
            presets.addItem(_("Choose a preset"), None)
            choices = setting.get("presets")
            for preset in choices if isinstance(choices, list) else ():
                if isinstance(preset, dict) and self._valid(setting, preset.get("value")):
                    presets.addItem(str(preset.get("name") or ""), deepcopy(preset["value"]))
            presets.activated.connect(lambda index: self._preset(ident, presets.itemData(index)))
            layout.addWidget(presets)
        reset = QPushButton(_("Reset to default"))
        reset.setObjectName(ident + ".reset")
        reset.clicked.connect(lambda: self._preset(ident, setting.get("default")))
        reset.setEnabled(self._valid(setting, setting.get("default")))
        layout.addWidget(reset)
        container._reset = reset
        return container

    def _emit(self, ident, value):
        if (self._loading or not self._valid(self._settings[ident], value)
                or (self._values.get(ident) == value and ident not in self._invalid_overrides)):
            return
        self._invalid_overrides.discard(ident)
        self._values[ident] = deepcopy(value)
        self._refresh_priorities()
        self.changed.emit(ident, deepcopy(value))

    def _integer_edit(self, ident, widget):
        try:
            value = int(widget.text().strip())
        except ValueError:
            value = None
        if self._valid(self._settings[ident], value):
            widget.setToolTip("")
            self._emit(ident, value)
        else:
            widget.setText(str(self._values[ident]))
            widget.setToolTip(_("Enter an integer within the allowed range."))

    def _jobs_edit(self, ident, widget):
        self._emit(ident, [widget.item(index).text() for index in range(widget.count())])

    def _move_job(self, ident, widget, direction):
        row = widget.currentRow()
        target = row + direction
        if not 0 <= row < widget.count() or not 0 <= target < widget.count():
            return
        item = widget.takeItem(row)
        widget.insertItem(target, item)
        widget.setCurrentRow(target)
        self._jobs_edit(ident, widget)

    def _map_edit(self, ident, container):
        self._emit(ident, {slot: {"enabled": enabled.isChecked(), "marker": marker.currentData()}
                           for slot, (enabled, marker) in container._slots.items()})

    def _preset(self, ident, value):
        setting = self._settings[ident]
        if not self._valid(setting, value):
            return
        editor = self._editors[ident]
        kind = setting["type"]
        if kind in ("boolean", "integer"):
            widget = editor._input
            with QSignalBlocker(widget):
                if kind == "boolean":
                    widget.setChecked(value)
                elif isinstance(widget, QSpinBox):
                    widget.setValue(value)
                else:
                    widget.setText(str(value))
        elif kind == "jobs":
            with QSignalBlocker(editor._input.model()):
                editor._input.clear()
                editor._input.addItems(value)
        elif kind == "marker_map":
            for slot, (enabled, marker) in editor._slots.items():
                with QSignalBlocker(enabled), QSignalBlocker(marker):
                    enabled.setChecked(value[slot]["enabled"])
                    marker.setCurrentIndex(marker.findData(value[slot]["marker"]))
                    marker.setEnabled(value[slot]["enabled"])
        self._emit(ident, value)

    def _refresh_priorities(self):
        for ident, setting in self._settings.items():
            if setting["type"] != "jobs" or ident not in self._editors:
                continue
            editor = self._editors[ident]
            override = setting.get("override_enabled")
            enabled = self._values.get(override) if isinstance(override, str) else None
            enabled = enabled if type(enabled) is bool else True
            editor._input.setEnabled(enabled)
            editor._reset.setEnabled(enabled and self._valid(setting, setting.get("default")))
            for button in editor._priority_buttons:
                button.setEnabled(enabled)
            parent = setting.get("parent")
            effective = self._values.get(parent) if isinstance(parent, str) else None
            if not self._valid(setting, effective):
                effective = setting.get("effective_order")
            if enabled or not self._valid(setting, effective):
                effective = self._values[ident]
            editor._priority_note.setText(_("Effective priority: {jobs}").format(jobs=", ".join(effective)))
