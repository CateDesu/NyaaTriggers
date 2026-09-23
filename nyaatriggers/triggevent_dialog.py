"""Build conditional Triggevent callouts without scripts."""

from copy import deepcopy

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QComboBox, QCompleter, QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout,
    QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton,
    QScrollArea, QVBoxLayout, QWidget,
)

from nyaatriggers.locale_util import _, N_
from nyaatriggers.triggevent_custom import MAX_STEPS, fight_choices, new_trigger, validate_trigger


EVENT_LABELS = (
    ("cast", N_("Cast starts")), ("ability", N_("Ability resolves")),
    ("status_gain", N_("Status gained")), ("status_loss", N_("Status lost")),
    ("headmarker", N_("Headmarker applied")), ("tether", N_("Tether appears")),
)
TARGET_LABELS = (
    ("any", N_("Anyone")), ("me", N_("Me")),
    ("start_target", N_("Target of the starting event")),
    ("start_source", N_("Source of the starting event")),
)
CONDITION_LABELS = (
    ("always", N_("Always")), ("start_id", N_("Starting event ID matches")),
    ("event_id", N_("Latest event ID matches")),
    ("my_status", N_("I currently have a status")),
    ("no_my_status", N_("I currently have none of these statuses")),
)


def _combo(options, current):
    widget = QComboBox()
    for key, label in options:
        widget.addItem(_(label), key)
    widget.setCurrentIndex(max(0, widget.findData(current)))
    return widget


def _seconds(value, minimum=0.1):
    widget = QDoubleSpinBox()
    widget.setDecimals(1)
    widget.setRange(minimum, 600)
    widget.setValue(value)
    widget.setSuffix(_(" s"))
    return widget


class EventFields(QWidget):
    def __init__(self, data, *, start=False):
        super().__init__()
        layout = QFormLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.event = _combo(EVENT_LABELS, data.get("event", "cast"))
        self.ids = QLineEdit(data.get("ids", ""))
        self.ids.setPlaceholderText(_("Hex IDs, for example A123 | A124"))
        self.ids.setMaxLength(512)
        self.target = _combo(TARGET_LABELS[:2] if start else TARGET_LABELS, data.get("target", "any"))
        layout.addRow(_("Event"), self.event)
        layout.addRow(_("Ability, status, marker or tether IDs"), self.ids)
        layout.addRow(_("Target"), self.target)

    def value(self):
        return {"event": self.event.currentData(), "ids": self.ids.text().strip(),
                "target": self.target.currentData()}


class CalloutStep(QGroupBox):
    def __init__(self, data, owner):
        super().__init__()
        self.kind = data["kind"]
        outer = QVBoxLayout(self)
        bar = QHBoxLayout()
        self.title = QLabel()
        bar.addWidget(self.title)
        bar.addStretch()
        self.up = QPushButton(_("Up"))
        self.down = QPushButton(_("Down"))
        remove = QPushButton(_("Remove"))
        for button in (self.up, self.down, remove):
            bar.addWidget(button)
        self.up.clicked.connect(lambda: owner.move_step(self, -1))
        self.down.clicked.connect(lambda: owner.move_step(self, 1))
        remove.clicked.connect(lambda: owner.remove_step(self))
        outer.addLayout(bar)
        if self.kind == "wait":
            self.event = EventFields(data)
            outer.addWidget(self.event)
        elif self.kind == "delay":
            self.seconds = _seconds(data.get("seconds", 1.0))
            form = QFormLayout()
            form.addRow(_("Wait for"), self.seconds)
            outer.addLayout(form)
        else:
            form = QFormLayout()
            self.condition = _combo(CONDITION_LABELS, data.get("condition", "always"))
            self.ids = QLineEdit(data.get("ids", ""))
            self.ids.setMaxLength(512)
            self.ids.setPlaceholderText(_("Hex IDs, for example A123 | A124"))
            self.text = QLineEdit(data.get("text", ""))
            self.otherwise = QLineEdit(data.get("otherwise", ""))
            self.text.setMaxLength(2000)
            self.otherwise.setMaxLength(2000)
            self.otherwise.setPlaceholderText(_("Leave blank to stay silent"))
            form.addRow(_("Condition"), self.condition)
            form.addRow(_("Matching IDs"), self.ids)
            form.addRow(_("Say"), self._speech_field(self.text, owner))
            form.addRow(_("Otherwise say"), self._speech_field(self.otherwise, owner))
            outer.addLayout(form)
            self.condition.currentIndexChanged.connect(self._condition_changed)
            self._condition_changed()

    def _speech_field(self, field, owner):
        row = QHBoxLayout()
        row.addWidget(field, 1)
        button = QPushButton(_("Test speech"))
        button.setToolTip(_("Preview speech with sample placeholder values"))
        button.setEnabled(owner._preview is not None)
        button.clicked.connect(lambda: owner._preview(field.text()))
        row.addWidget(button)
        return row

    def _condition_changed(self):
        conditional = self.condition.currentData() != "always"
        self.ids.setEnabled(conditional)
        self.otherwise.setEnabled(conditional)

    def value(self):
        if self.kind == "wait":
            return {"kind": "wait", **self.event.value()}
        if self.kind == "delay":
            return {"kind": "delay", "seconds": self.seconds.value()}
        return {"kind": "callout", "condition": self.condition.currentData(),
                "ids": self.ids.text().strip(), "text": self.text.text().strip(),
                "otherwise": self.otherwise.text().strip()}


class TriggeventDialog(QDialog):
    def __init__(self, parent=None, trigger=None, *, fight="", zone_id=0, save=None, preview=None, status=""):
        super().__init__(parent)
        self.setWindowTitle(_("Triggevent callout builder"))
        self.resize(760, 780)
        self._original = deepcopy(trigger or new_trigger(fight, zone_id))
        self._save = save
        self._preview = preview
        self.steps = []
        outer = QVBoxLayout(self)
        intro = QLabel(_("Choose what starts the sequence, then add waits and callouts in order. "
                         "The starting event stays available to later callout conditions."))
        intro.setWordWrap(True)
        outer.addWidget(intro)
        self.status = QLabel(status)
        self.status.setWordWrap(True)
        outer.addWidget(self.status)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        body = QWidget()
        layout = QVBoxLayout(body)
        form = QFormLayout()
        self.name = QLineEdit(self._original["name"])
        self.name.setMaxLength(200)
        self.fight = QLineEdit(self._original["fight"])
        self.fight.setMaxLength(200)
        self.fight.setClearButtonEnabled(True)
        self.fight.setPlaceholderText(_("Search fights by name or abbreviation..."))
        self._selected_fight = self._original["fight"]
        self._fight_choices = {row["label"]: row for row in fight_choices()}
        completer = QCompleter(list(self._fight_choices), self.fight)
        completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        completer.setFilterMode(Qt.MatchFlag.MatchContains)
        completer.setMaxVisibleItems(10)
        self.fight.setCompleter(completer)
        completer.activated[str].connect(self._choose_fight)
        self.zone = QLineEdit(str(self._original["zone_id"]))
        self.zone.setPlaceholderText(_("Decimal zone ID, or 0 for any zone"))
        self._fight_hint = QLabel()
        self._fight_hint.setWordWrap(True)
        self.fight.textChanged.connect(self._fight_edited)
        self._update_fight_hint()
        self.timeout = _seconds(self._original["timeout_s"], 1)
        form.addRow(_("Name"), self.name)
        form.addRow(_("Fight"), self.fight)
        form.addRow("", self._fight_hint)
        form.addRow(_("Zone ID"), self.zone)
        form.addRow(_("Total sequence timeout"), self.timeout)
        layout.addLayout(form)
        start = QGroupBox(_("Start when"))
        start_layout = QVBoxLayout(start)
        self.start = EventFields(self._original["start"], start=True)
        start_layout.addWidget(self.start)
        layout.addWidget(start)
        self.step_layout = QVBoxLayout()
        layout.addLayout(self.step_layout)
        for step in self._original["steps"]:
            self.add_step(step)
        buttons = QHBoxLayout()
        self.add_buttons = []
        for kind, label in (("wait", _("Add event wait")), ("delay", _("Add delay")),
                            ("callout", _("Add callout"))):
            button = QPushButton(label)
            button.clicked.connect(lambda checked=False, kind=kind: self.add_step({"kind": kind}))
            self.add_buttons.append(button)
            buttons.addWidget(button)
        self._renumber()
        layout.addLayout(buttons)
        help_text = QLabel(_("Spoken text can use {player}, {source}, {target}, {id}, "
                             "{start.source}, {start.target} and {start.id}. "
                             "Source, target and ID refer to the latest matched event. "
                             "A running sequence ignores new starting events until it finishes or times out."))
        help_text.setWordWrap(True)
        layout.addWidget(help_text)
        layout.addStretch()
        scroll.setWidget(body)
        outer.addWidget(scroll, 1)
        footer = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        footer.accepted.connect(self.accept)
        footer.rejected.connect(self.reject)
        outer.addWidget(footer)

    def _choose_fight(self, label):
        choice = self._fight_choices.get(label)
        if choice is None:
            return
        self._selected_fight = choice["fight"]
        self.fight.blockSignals(True)
        self.fight.setText(choice["fight"])
        self.fight.blockSignals(False)
        self.zone.setText(str(choice["zone_id"]))
        self._update_fight_hint(choice["zone_name"])

    def _fight_edited(self, text):
        choice = self._fight_choices.get(text)
        if self._selected_fight and (text == self._selected_fight or (
                choice and choice["fight"] == self._selected_fight
                and str(choice["zone_id"]) == self.zone.text())):
            return
        self._selected_fight = ""
        self.zone.setText("0")
        self._update_fight_hint()

    def _update_fight_hint(self, zone_name=""):
        if self._selected_fight:
            self._fight_hint.setText(zone_name or self._selected_fight)
        elif self.fight.text().strip():
            self._fight_hint.setText(_("Choose a search result to assign a fight. Otherwise this callout goes into Unsorted."))
        else:
            self._fight_hint.setText(_("No fight selected. This callout will go into Unsorted."))

    def add_step(self, data):
        if len(self.steps) >= MAX_STEPS:
            return
        row = CalloutStep(data, self)
        self.steps.append(row)
        self.step_layout.addWidget(row)
        self._renumber()

    def remove_step(self, row):
        self.steps.remove(row)
        self.step_layout.removeWidget(row)
        row.deleteLater()
        self._renumber()

    def move_step(self, row, direction):
        index = self.steps.index(row)
        other = index + direction
        if 0 <= other < len(self.steps):
            self.steps.pop(index)
            self.steps.insert(other, row)
            self.step_layout.removeWidget(row)
            self.step_layout.insertWidget(other, row)
            self._renumber()

    def _renumber(self):
        labels = {"wait": _("Wait for an event"), "delay": _("Delay"), "callout": _("Callout")}
        for index, row in enumerate(self.steps):
            row.title.setText(_("Step {number}: {action}").format(number=index + 1, action=labels[row.kind]))
            row.up.setEnabled(index > 0)
            row.down.setEnabled(index < len(self.steps) - 1)
        for button in getattr(self, "add_buttons", []):
            button.setEnabled(len(self.steps) < MAX_STEPS)

    def get_trigger(self):
        zone = self.zone.text().strip()
        if not zone.isascii() or not zone.isdecimal():
            raise ValueError(_("Enter a decimal zone ID or 0 for any zone"))
        return {"id": self._original["id"], "name": self.name.text().strip(),
                "fight": self._selected_fight, "zone_id": int(zone),
                "timeout_s": self.timeout.value(), "start": self.start.value(),
                "steps": [row.value() for row in self.steps]}

    def accept(self):
        try:
            definition = self.get_trigger()
            validate_trigger(definition)
            if self._save is not None:
                self._save(definition)
        except (OSError, ValueError, RecursionError) as exc:
            QMessageBox.warning(self, _("Could not save Triggevent callout"), str(exc))
            return
        super().accept()
