"""Visual editing of native Triggernometry trigger definitions."""

from copy import deepcopy
import xml.etree.ElementTree as ET

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QCompleter, QDialog, QDialogButtonBox, QFormLayout,
    QHBoxLayout, QLabel, QLineEdit, QListWidget, QMessageBox, QPlainTextEdit,
    QPushButton, QSplitter, QTabWidget, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from nyaatriggers.locale_util import _, N_
from nyaatriggers.triggevent_custom import fight_choices
from nyaatriggers.triggernometry_editor import normalized_id, parse_xml, trigger_entries


ACTION_TYPES = (("UseTTS", N_("Speak")), ("Variable", N_("Set or change a variable")),
                ("Placeholder", N_("Delay")), ("Trigger", N_("Run or control a trigger")),
                ("ExecuteScript", N_("C# script")))
COMPARISONS = ("StringEqualCase", "StringEqualNocase", "StringNotEqualCase", "StringNotEqualNocase",
               "NumericEqual", "NumericNotEqual", "NumericGreater", "NumericGreaterEqual",
               "NumericLess", "NumericLessEqual", "RegexMatch", "RegexNotMatch",
               "ListContains", "ListDoesNotContain")


def combo(values, value):
    field = QComboBox()
    for key, label in values:
        field.addItem(label, key)
    index = field.findData(value)
    if index < 0:
        field.addItem(value, value)
        index = field.count() - 1
    field.setCurrentIndex(index)
    return field


def options(values):
    return [(value, value) for value in values]


def footer(dialog, layout):
    buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
    buttons.accepted.connect(dialog.accept)
    buttons.rejected.connect(dialog.reject)
    layout.addWidget(buttons)


def replace_child(parent, tag, child):
    old = parent.find(tag)
    if old is not None:
        parent.remove(old)
    if child is not None:
        parent.append(deepcopy(child))


class XmlDialog(QDialog):
    def __init__(self, element, parent=None):
        super().__init__(parent)
        self.setWindowTitle(_("Edit XML"))
        self.resize(780, 600)
        self.element = deepcopy(element)
        layout = QVBoxLayout(self)
        self.text = QPlainTextEdit(ET.tostring(element, encoding="unicode"))
        layout.addWidget(self.text)
        footer(self, layout)

    def accept(self):
        try:
            updated = parse_xml(self.text.toPlainText(), self.element.tag)
            if self.element.get("Id") and updated.get("Id") != self.element.get("Id"):
                raise ValueError(_("Keep the existing ID. Use Duplicate to create a separate trigger."))
            self.element = updated
        except ValueError as exc:
            QMessageBox.warning(self, _("Invalid XML"), str(exc))
            return
        super().accept()


class AttributesDialog(QDialog):
    def __init__(self, element, fields, parent=None):
        super().__init__(parent)
        self.setWindowTitle(_("Edit condition"))
        self.resize(650, 300)
        self.element = deepcopy(element)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        layout.addLayout(form)
        self.fields = {}
        self.enabled = QCheckBox(_("Enabled"))
        self.enabled.setChecked(element.get("Enabled", "true").lower() == "true")
        form.addRow(self.enabled)
        for key, label, choices, default in fields:
            value = element.get(key, default)
            widget = combo(options(choices), value) if choices else QLineEdit(value)
            self.fields[key] = widget
            form.addRow(label, widget)
        footer(self, layout)

    def accept(self):
        self.element.set("Enabled", str(self.enabled.isChecked()).lower())
        for key, widget in self.fields.items():
            self.element.set(key, widget.currentData() if isinstance(widget, QComboBox) else widget.text())
        super().accept()


class ConditionsWidget(QWidget):
    def __init__(self, element=None, parent=None):
        super().__init__(parent)
        self.element = deepcopy(element) if element is not None else None
        layout = QVBoxLayout(self)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabel(_("Conditions"))
        layout.addWidget(self.tree)
        row = QHBoxLayout()
        for label, action in ((_("Add comparison"), self.add_comparison), (_("Add group"), self.add_group),
                              (_("Edit"), self.edit), (_("Remove"), self.remove)):
            button = QPushButton(label)
            button.clicked.connect(action)
            row.addWidget(button)
        layout.addLayout(row)
        self.tree.itemDoubleClicked.connect(lambda *_args: self.edit())
        self.refresh()

    def refresh(self):
        self.tree.clear()

        def add(element, parent):
            if not isinstance(element.tag, str):
                return
            if element.tag in ("Condition", "ConditionGroup"):
                text = element.get("Grouping", "Or")
            else:
                text = " ".join(element.get(key, "") for key in ("ExpressionL", "ConditionType", "ExpressionR"))
            if element.get("Enabled", "true").lower() != "true":
                text += " · " + _("Disabled")
            item = QTreeWidgetItem(parent, [text or element.tag])
            item.setData(0, Qt.ItemDataRole.UserRole, element)
            for child in element:
                add(child, item)

        if self.element is not None:
            add(self.element, self.tree)
            self.tree.expandAll()

    def selected(self):
        item = self.tree.currentItem()
        return item.data(0, Qt.ItemDataRole.UserRole) if item else None

    def _add(self, group):
        if self.element is None:
            self.element = ET.Element("Condition", Enabled="true", Grouping="And")
        target = self.selected()
        if target is None or target.tag not in ("Condition", "ConditionGroup"):
            target = self.element
        child = ET.Element("ConditionGroup" if group else "ConditionSingle", Enabled="true")
        if group:
            child.set("Grouping", "And")
        else:
            child.attrib.update(ExpressionL="", ExpressionR="", ExpressionTypeL="String",
                                ExpressionTypeR="String", ConditionType="StringEqualNocase")
        updated = self._edit(child)
        if updated is not None:
            target.append(updated)
        self.refresh()

    def add_comparison(self):
        self._add(False)

    def add_group(self):
        self._add(True)

    def _edit(self, element):
        if element.tag in ("Condition", "ConditionGroup"):
            fields = [("Grouping", _("Match"), ("And", "Or", "Xor", "Not"), "Or")]
        elif element.tag == "ConditionSingle":
            fields = [("ExpressionL", _("Left expression"), None, ""),
                      ("ExpressionTypeL", _("Left value type"), ("String", "Numeric"), "String"),
                      ("ConditionType", _("Comparison"), COMPARISONS, "StringEqualNocase"),
                      ("ExpressionR", _("Right expression"), None, ""),
                      ("ExpressionTypeR", _("Right value type"), ("String", "Numeric"), "String")]
        else:
            dialog = XmlDialog(element, self)
            try:
                return dialog.element if dialog.exec() else None
            finally:
                dialog.deleteLater()
        dialog = AttributesDialog(element, fields, self)
        try:
            return dialog.element if dialog.exec() else None
        finally:
            dialog.deleteLater()

    def edit(self):
        current = self.selected()
        if current is None:
            return
        updated = self._edit(current)
        if updated is not None:
            current.clear()
            current.attrib.update(updated.attrib)
            current.extend(deepcopy(list(updated)))
        self.refresh()

    def remove(self):
        selected = self.selected()
        if selected is self.element:
            self.element = None
        elif selected is not None:
            for parent in self.element.iter():
                if selected in list(parent):
                    parent.remove(selected)
                    break
        self.refresh()


class ActionDialog(QDialog):
    def __init__(self, element, triggers, parent=None):
        super().__init__(parent)
        self.element = deepcopy(element)
        kind = element.get("ActionType", "UseTTS")
        self.setWindowTitle(_("Edit action"))
        self.resize(760, 630)
        layout = QVBoxLayout(self)
        tabs = QTabWidget()
        layout.addWidget(tabs)
        page = QWidget()
        form = QFormLayout(page)
        tabs.addTab(page, dict((key, _(label)) for key, label in ACTION_TYPES).get(kind, kind))
        self.fields = {}

        def field(key, label, default="", choices=None, multiline=False):
            value = element.get(key, default)
            if key == "TriggerId" and normalized_id(value):
                value = next((ident for ident, _label in choices if normalized_id(ident) == normalized_id(value)), value)
            widget = combo(choices, value) if choices else QPlainTextEdit(value) if multiline else QLineEdit(value)
            self.fields[key] = widget
            form.addRow(label, widget)

        field("ExecutionDelayExpression", _("Delay before action in milliseconds"), "0")
        field("Asynchronous", _("Execution"), "True", [("False", _("Wait for completion")), ("True", _("Run in background"))])
        if kind == "UseTTS":
            field("UseTTSTextExpression", _("Say"), multiline=True)
        elif kind == "Variable":
            field("VariableOp", _("Operation"), "Unset", options(("SetString", "SetNumeric", "Increment", "Unset", "UnsetRegex")))
            field("VariableName", _("Variable name"))
            field("VariableExpression", _("Value or expression"))
        elif kind == "Trigger":
            field("TriggerOp", _("Operation"), "FireTrigger", options(("FireTrigger", "CancelTrigger", "EnableTrigger", "DisableTrigger", "CancelAllTrigger")))
            choices = [("", _("Choose a trigger"))] + [
                (item.get("Id"), path + " / " + item.get("Name", "")) for path, item, _parent in triggers]
            field("TriggerId", _("Trigger"), choices=choices)
            field("TriggerText", _("Event text"), "${_event}")
            field("TriggerForce", _("Skip checks"), "", [("", _("Keep all checks")), ("regexp", _("Skip regular expression"))])
        elif kind == "ExecuteScript":
            field("ExecScriptExpression", _("C# script"), multiline=True)
            field("ExecScriptAssembliesExpression", _("Additional assemblies"))
        self.conditions = ConditionsWidget(element.find("Condition"))
        tabs.addTab(self.conditions, _("Conditions"))
        note = QLabel(_("Expressions can use ${name} for a regex capture, ${var:name} for a variable, and ${_me.name} for your character."))
        note.setWordWrap(True)
        layout.addWidget(note)
        footer(self, layout)

    def accept(self):
        if (self.element.get("ActionType") == "Trigger"
                and self.fields["TriggerOp"].currentData() != "CancelAllTrigger"
                and not self.fields["TriggerId"].currentData()):
            QMessageBox.warning(self, _("Choose a trigger"), _("Select the trigger this action should run or control."))
            return
        for key, widget in self.fields.items():
            if isinstance(widget, QComboBox):
                value = widget.currentData()
            elif isinstance(widget, QPlainTextEdit):
                value = widget.toPlainText()
            else:
                value = widget.text()
            if key == "TriggerId" and not value:
                self.element.attrib.pop(key, None)
            else:
                self.element.set(key, value)
        replace_child(self.element, "Condition", self.conditions.element)
        super().accept()


class TriggernometryDialog(QDialog):
    def __init__(self, document, parent=None, *, selected_id=None, save=None):
        super().__init__(parent)
        self.document = document
        self._save = save
        self.current = None
        self.setWindowTitle(_("Triggernometry pack editor"))
        self.resize(1050, 800)
        layout = QVBoxLayout(self)
        note = QLabel(_("Saving reloads Triggernometry and clears its running sequences and variables. During combat, reload waits until combat ends. Changed speech resets text overrides. Triggers with muted speech stay muted when their speech changes."))
        note.setWordWrap(True)
        layout.addWidget(note)
        layout.addWidget(QLabel(document.path.name))
        self.fight = QLineEdit(document.root.find("ExportedFolder").get("Name", ""))
        self.fight.setPlaceholderText(_("Search fights by name or abbreviation..."))
        self._fight_choices = {row["label"]: row for row in fight_choices()}
        completer = QCompleter(list(self._fight_choices), self.fight)
        completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        completer.setFilterMode(Qt.MatchFlag.MatchContains)
        self.fight.setCompleter(completer)
        completer.activated[str].connect(self.choose_fight)
        self.fight.textEdited.connect(self.clear_fight)
        self.fight_hint = QLabel()
        self._fight_hint()
        form = QFormLayout()
        form.addRow(_("Pack fight"), self.fight)
        form.addRow("", self.fight_hint)
        layout.addLayout(form)
        splitter = QSplitter()
        layout.addWidget(splitter, 1)
        left = QWidget()
        left_layout = QVBoxLayout(left)
        self.triggers = QListWidget()
        left_layout.addWidget(self.triggers)
        row = QHBoxLayout()
        for label, action in ((_("Add trigger"), self.add_trigger), (_("Duplicate"), self.duplicate), (_("Delete"), self.delete)):
            button = QPushButton(label)
            button.clicked.connect(action)
            row.addWidget(button)
        left_layout.addLayout(row)
        splitter.addWidget(left)
        self.tabs = QTabWidget()
        splitter.addWidget(self.tabs)
        splitter.setSizes([320, 700])
        self.triggers.currentRowChanged.connect(self.select_trigger)
        self.refresh(selected_id)
        footer(self, layout)

    def _fight_hint(self):
        folder = self.document.root.find("ExportedFolder")
        zone = folder.get("FfxivZoneFilterRegularExpression", "")
        active = folder.get("FFXIVZoneFilterEnabled", "false").lower() == "true"
        labels = [_('Zone filter: {zone}').format(zone=zone)] if active else []
        if folder.get("ZoneFilterEnabled", "false").lower() == "true":
            labels.append(_("Zone name filter: {zone}").format(zone=folder.get("ZoneFilterRegularExpression", "")))
        self.fight_hint.setText(" · ".join(labels) if labels else _("No outer folder zone restriction. Choose a search result to assign a fight, or leave it under Unsorted."))
        self.fight_hint.setWordWrap(True)

    def choose_fight(self, label):
        choice = self._fight_choices.get(label)
        if choice is None:
            return
        folder = self.document.root.find("ExportedFolder")
        folder.set("Name", choice["fight"])
        folder.set("FFXIVZoneFilterEnabled", "True")
        folder.set("FfxivZoneFilterRegularExpression", "^" + str(choice["zone_id"]) + "$")
        folder.set("ZoneFilterEnabled", "False")
        folder.attrib.pop("ZoneFilterRegularExpression", None)
        self.fight.setText(choice["fight"])
        self._fight_hint()

    def clear_fight(self, text):
        folder = self.document.root.find("ExportedFolder")
        folder.set("Name", "Unsorted")
        folder.set("FFXIVZoneFilterEnabled", "False")
        folder.attrib.pop("FfxivZoneFilterRegularExpression", None)
        folder.set("ZoneFilterEnabled", "False")
        folder.attrib.pop("ZoneFilterRegularExpression", None)
        self._fight_hint()

    def flush(self):
        if self.current is None:
            return
        self.current.set("Name", self.name.text())
        self.current.set("RegularExpression", self.pattern.text())
        self.current.set("Source", self.source.currentData())
        self.current.set("Enabled", str(self.enabled.isChecked()).lower())
        self.current.set("Sequential", str(self.sequential.isChecked()))
        replace_child(self.current, "Condition", self.conditions.element)
        for index, (_path, trigger, _parent) in enumerate(self.entries):
            if trigger is self.current:
                self.triggers.item(index).setText(self.current.get("Name"))
                break

    def refresh(self, selected_id=None):
        selected_id = normalized_id(selected_id)
        self.triggers.blockSignals(True)
        self.triggers.clear()
        self.entries = trigger_entries(self.document.root)
        index = 0
        for position, (path, trigger, _parent) in enumerate(self.entries):
            self.triggers.addItem(trigger.get("Name", ""))
            self.triggers.item(position).setToolTip(path + "\n" + trigger.get("Id", ""))
            if selected_id and normalized_id(trigger.get("Id")) == selected_id:
                index = position
        self.triggers.blockSignals(False)
        self.current = None
        if self.entries:
            self.triggers.setCurrentRow(index)
        else:
            self.select_trigger(-1)

    def select_trigger(self, index):
        self.flush()
        while self.tabs.count():
            page = self.tabs.widget(0)
            self.tabs.removeTab(0)
            page.deleteLater()
        self.current = self.entries[index][1] if 0 <= index < len(self.entries) else None
        if self.current is None:
            return
        trigger = self.current
        basic = QWidget()
        form = QFormLayout(basic)
        self.name = QLineEdit(trigger.get("Name", ""))
        self.pattern = QLineEdit(trigger.get("RegularExpression", ""))
        self.pattern.setPlaceholderText(r"^20\|[^|]*\|[^|]*\|[^|]*\|C403\|")
        self.source = combo([("FFXIVNetwork", _("Raw network log")), ("Log", _("Formatted ACT log")),
                             ("None", _("Called by another trigger")), ("Endpoint", _("Endpoint")), ("ACT", "ACT")], trigger.get("Source", "Log"))
        self.enabled = QCheckBox(_("Enabled"))
        self.enabled.setChecked(trigger.get("Enabled", "false").lower() == "true")
        self.sequential = QCheckBox(_("Run actions in order"))
        self.sequential.setChecked(trigger.get("Sequential", "false").lower() == "true")
        form.addRow(_("Name"), self.name)
        form.addRow(_("Event source"), self.source)
        form.addRow(_("Regular expression"), self.pattern)
        form.addRow(self.enabled)
        form.addRow(self.sequential)
        advanced = QPushButton(_("Edit trigger XML..."))
        advanced.clicked.connect(self.edit_xml)
        form.addRow(advanced)
        self.tabs.addTab(basic, _("Trigger"))
        self.conditions = ConditionsWidget(trigger.find("Condition"))
        self.tabs.addTab(self.conditions, _("Conditions"))
        page = QWidget()
        layout = QVBoxLayout(page)
        self.actions = QListWidget()
        layout.addWidget(self.actions)
        row = QHBoxLayout()
        self.action_type = combo([(key, _(label)) for key, label in ACTION_TYPES], "UseTTS")
        row.addWidget(self.action_type)
        for label, action in ((_("Add action"), self.add_action), (_("Edit"), self.edit_action),
                              (_("Remove"), self.remove_action), (_("Up"), lambda: self.move_action(-1)),
                              (_("Down"), lambda: self.move_action(1))):
            button = QPushButton(label)
            button.clicked.connect(action)
            row.addWidget(button)
        layout.addLayout(row)
        self.actions.itemDoubleClicked.connect(lambda *_args: self.edit_action())
        self.tabs.addTab(page, _("Actions"))
        self.refresh_actions()

    def refresh_actions(self):
        self.actions.clear()
        container = self.current.find("Actions")
        self.action_elements = [] if container is None else [a for a in container if a.tag == "Action"]
        def order(action):
            try:
                return int(action.get("OrderNumber", "0"))
            except ValueError:
                return 0
        self.action_elements.sort(key=order)
        for action in self.action_elements:
            kind = action.get("ActionType", "")
            label = dict((key, _(text)) for key, text in ACTION_TYPES).get(kind, kind)
            text = next((action.get(key) for key in ("UseTTSTextExpression", "VariableName", "TriggerId", "ExecScriptExpression") if action.get(key)), "")
            if kind == "Trigger" and normalized_id(text):
                text = next((trigger.get("Name", "") for _path, trigger, _parent in self.entries
                             if normalized_id(trigger.get("Id")) == normalized_id(text)), text)
            self.actions.addItem(label + " · " + text.replace("\n", " ")[:100] + " · " + action.get("ExecutionDelayExpression", "0") + " ms")

    def _edit_action(self, action):
        if action.get("ActionType") in dict(ACTION_TYPES):
            dialog = ActionDialog(action, trigger_entries(self.document.root), self)
        else:
            dialog = XmlDialog(action, self)
        try:
            return dialog.element if dialog.exec() else None
        finally:
            dialog.deleteLater()

    def add_action(self):
        action = ET.Element("Action", ActionType=self.action_type.currentData(), Asynchronous="False")
        if action.get("ActionType") == "Variable":
            action.set("VariableOp", "SetString")
        elif action.get("ActionType") == "Trigger":
            action.set("TriggerForce", "regexp")
        updated = self._edit_action(action)
        if updated is None:
            return
        container = self.current.find("Actions")
        if container is None:
            container = ET.SubElement(self.current, "Actions")
        updated.set("OrderNumber", str(max((int(a.get("OrderNumber", "0")) for a in self.action_elements), default=0) + 1))
        container.append(updated)
        self.refresh_actions()
        self.actions.setCurrentRow(len(self.action_elements) - 1)

    def edit_action(self):
        index = self.actions.currentRow()
        if index < 0:
            return
        action = self.action_elements[index]
        updated = self._edit_action(action)
        if updated is not None:
            container = self.current.find("Actions")
            position = list(container).index(action)
            container.remove(action)
            container.insert(position, updated)
            self.refresh_actions()
            self.actions.setCurrentRow(index)

    def remove_action(self):
        index = self.actions.currentRow()
        if index >= 0:
            self.current.find("Actions").remove(self.action_elements[index])
            self.refresh_actions()

    def move_action(self, direction):
        index = self.actions.currentRow()
        other = index + direction
        if index < 0 or not 0 <= other < len(self.action_elements):
            return
        elements = self.action_elements
        elements[index], elements[other] = elements[other], elements[index]
        container = self.current.find("Actions")
        for action in elements:
            container.remove(action)
        for position, action in enumerate(elements, 1):
            action.set("OrderNumber", str(position))
            container.append(action)
        self.refresh_actions()
        self.actions.setCurrentRow(other)

    def add_trigger(self):
        self.flush()
        trigger = self.document.add_trigger()
        self.refresh(trigger.get("Id"))

    def duplicate(self):
        self.flush()
        if self.current is not None:
            trigger = self.document.add_trigger(self.current)
            self.refresh(trigger.get("Id"))

    def delete(self):
        if self.current is None:
            return
        if QMessageBox.question(self, _("Delete Trigger"), _('Delete "{name}"?').format(name=self.current.get("Name"))) != QMessageBox.StandardButton.Yes:
            return
        for _path, trigger, parent in self.entries:
            if trigger is self.current:
                parent.remove(trigger)
                break
        self.current = None
        self.refresh()

    def edit_xml(self):
        self.flush()
        dialog = XmlDialog(self.current, self)
        try:
            if dialog.exec():
                for _path, trigger, parent in self.entries:
                    if trigger is self.current:
                        index = list(parent).index(trigger)
                        parent.remove(trigger)
                        parent.insert(index, dialog.element)
                        self.refresh(dialog.element.get("Id"))
                        break
        finally:
            dialog.deleteLater()

    def accept(self):
        self.flush()
        try:
            if self._save is None:
                self.document.save()
            else:
                self._save(self.document)
        except (OSError, ValueError, RecursionError) as exc:
            QMessageBox.warning(self, _("Could not save Triggernometry pack"), str(exc))
            return
        super().accept()
