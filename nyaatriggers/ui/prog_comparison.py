"""Phase progress and comparisons between saved sessions in one duty."""

from datetime import datetime

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (QAbstractItemView, QApplication, QComboBox, QHBoxLayout,
                             QHeaderView, QLabel, QPushButton, QSizePolicy, QTableWidget,
                             QTableWidgetItem, QVBoxLayout, QWidget)

from nyaatriggers.locale_util import _
from nyaatriggers.prog_session import compare_sessions, summary


def duration(value):
    seconds = max(0, int(value))
    return f"{seconds // 3600:02d}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"


def session_label(session):
    label = f"{datetime.fromtimestamp(session['started']):%Y-%m-%d %H:%M}  {session['name']}"
    return _("{session} · Archived").format(session=label) if session.get("archived") is True else label


def phase_label(data):
    if data["status"] != "available":
        return {"not-recorded": _("Not recorded"), "not-supported": _("Not supported"),
                "tracking-differs": _("Unavailable"), "unavailable": _("Unavailable")}[data["status"]]
    if data["furthest"] is None:
        return _("No confirmation")
    return _("P{number}").format(number=data["definition"].phases.index(data["furthest"]) + 1)


def exclusion_text(data):
    labels = {"active": _("In progress"), "interrupted": _("Interrupted"),
              "uncertain": _("Uncertain boundary"), "unavailable": _("Unavailable"),
              "not-recorded": _("Not recorded"), "not-supported": _("Not supported"),
              "tracking-differs": _("Different tracking")}
    details = " · ".join(_("{reason}: {count}").format(reason=labels[reason], count=count)
                         for reason, count in data["excluded"].items())
    return _("Excluded pulls: {details}").format(details=details) if details else ""


def progress_text(data):
    if data["status"] == "available":
        if not data["eligible"]:
            text = _("Phase progress: No eligible pulls.")
        elif data["furthest"] is None:
            text = _("Phase progress: No confirmations on {eligible} eligible pulls.").format(eligible=data["eligible"])
        else:
            index = data["definition"].phases.index(data["furthest"])
            text = _("Phase progress: {phase} confirmed on {reached} of {eligible} eligible pulls.").format(
                phase=phase_label(data), reached=data["counts"][index], eligible=data["eligible"])
    else:
        text = {"not-recorded": _("Phase progress: Not recorded."),
                "not-supported": _("Phase progress: Not supported for this duty."),
                "unavailable": _("Phase progress: Unavailable."),
                "tracking-differs": _("Phase progress: Unavailable because tracking differs.")}[data["status"]]
    excluded = exclusion_text(data)
    return text + "\n" + excluded if excluded else text


def rate_text(count, total):
    return _("{count}/{total} · {percent}%").format(count=count, total=total, percent=f"{100 * count / total:.1f}") if total else _("No eligible pulls")


class SessionComparison(QWidget):
    def __init__(self, before_change):
        super().__init__()
        self.before_change = before_change
        self.sessions = None
        self.session = None
        self.compared_id = None
        self.initialized = False
        self.include_archived = False
        self.candidates = []
        self._candidates_key = None
        self._view = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.picker = QComboBox()
        self.picker.setAccessibleName(_("Compared session"))
        self.picker.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.picker.setMinimumContentsLength(16)
        controls = QHBoxLayout()
        controls.addWidget(self.picker, 1)
        self.copy_button = QPushButton(_("Copy comparison"))
        self.copy_button.setEnabled(False)
        self.copy_button.clicked.connect(self.copy_comparison)
        controls.addWidget(self.copy_button)
        layout.addLayout(controls)
        self.identity = QLabel()
        self.identity.setWordWrap(True)
        self.identity.setTextFormat(Qt.TextFormat.PlainText)
        self.identity.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        layout.addWidget(self.identity)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels([_("Measure"), _("Selected session"), _("Compared session"), _("Change")])
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(25)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        layout.addWidget(self.table)
        self.notice = QLabel()
        self.notice.setWordWrap(True)
        self.notice.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.notice)
        self.picker.currentIndexChanged.connect(self.select_comparison)

    def refresh(self, session, sessions, include_archived=None):
        if include_archived is not None:
            self.include_archived = include_archived
        if self.session is not None and (session is None or self.session["zone_id"] != session["zone_id"]):
            self.compared_id = None
        self.session = session
        self.sessions = sessions
        self.candidates = sorted((s for s in sessions.sessions
                                  if session is not None and s["id"] != session["id"]
                                  and s["zone_id"] == session["zone_id"] and s["state"] != "active"
                                  and (self.include_archived or s.get("archived") is not True)),
                                 key=lambda s: s["started"], reverse=True)
        if self.compared_id not in {s["id"] for s in self.candidates}:
            self.compared_id = None
        key = tuple((s["id"], s["name"], s["started"], s.get("archived") is True) for s in self.candidates)
        if key != self._candidates_key:
            self._candidates_key = key
            self.picker.blockSignals(True)
            self.picker.clear()
            self.picker.addItem(_("Select a session to compare") if key else _("No other sessions for this duty"), None)
            for candidate in self.candidates:
                self.picker.addItem(session_label(candidate), candidate["id"])
            self.picker.blockSignals(False)
        self.picker.blockSignals(True)
        self.picker.setCurrentIndex(max(0, self.picker.findData(self.compared_id)))
        self.picker.blockSignals(False)
        self.picker.setEnabled(bool(self.candidates))
        self.update_view()

    def open(self):
        if not self.initialized and self.session is not None:
            self.initialized = True
            for candidate in self.candidates:
                if candidate["started"] >= self.session["started"]:
                    continue
                result = compare_sessions(self.session, candidate, self.sessions.definitions)
                plain = all(data["status"] in ("not-recorded", "not-supported")
                            for data in (result["selected"], result["compared"]))
                no_durations = not summary(self.session)["pulls"] or not summary(candidate)["pulls"]
                if (result["phase_status"] == "available" or result["durations_comparable"]
                        or (plain and no_durations)):
                    self.compared_id = candidate["id"]
                    break
        self.refresh(self.session, self.sessions)

    def select_comparison(self):
        self.before_change()
        self.compared_id = self.picker.currentData()
        self.update_view()

    def copy_comparison(self):
        self.before_change()
        self.update_view()
        if self._view is None:
            return
        identity, rows, notice = self._view
        headings = [_("Measure"), _("Selected session"), _("Compared session"), _("Change")]
        lines = [identity, "\t".join(headings)]
        for index, values in enumerate(rows):
            values = [*values]
            if values[3]:
                values[3] = self.table.item(index, 3).toolTip()
            lines.append("\t".join(values))
        lines.append(notice)
        QApplication.clipboard().setText("\n".join(lines))

    def update_view(self):
        compared = next((s for s in self.candidates if s["id"] == self.compared_id), None)
        self.picker.setToolTip(session_label(compared) if compared else self.picker.currentText())
        if compared is None or self.session is None:
            self.copy_button.setEnabled(False)
            self.table.setRowCount(0)
            self.table.hide()
            self.identity.clear()
            self.notice.setText(_("Select a saved session in this duty to compare finished pulls.") if self.candidates
                                else _("No other sessions for this duty"))
            self._view = None
            return
        self.copy_button.setEnabled(True)
        result = compare_sessions(self.session, compared, self.sessions.definitions)
        left, right = result["selected"], result["compared"]
        ordinary_left, ordinary_right = summary(self.session), summary(compared)
        rows = [[_("Finished pulls"), str(ordinary_left["pulls"]), str(ordinary_right["pulls"]), ""],
                [_("Eligible phase pulls"), str(left["eligible"]) if left["status"] == "available" else _("Unavailable"),
                 str(right["eligible"]) if right["status"] == "available" else _("Unavailable"), ""],
                [_("Furthest confirmed phase"), phase_label(left), phase_label(right), ""]]
        if result["durations_comparable"] or not ordinary_left["pulls"] or not ordinary_right["pulls"]:
            rows.append([_("Longest finished pull"), duration(ordinary_left["longest"]) if ordinary_left["pulls"] else _("No finished pulls"),
                         duration(ordinary_right["longest"]) if ordinary_right["pulls"] else _("No finished pulls"), ""])
        else:
            rows.append([_("Longest finished pull"), _("Unavailable"), _("Unavailable"), ""])
        change_tooltips = {}
        for index, row in enumerate(result["phases"]):
            change = "—"
            if row["change"] is not None:
                value = round(row["change"], 1)
                amount = f"{value:+.1f}" if value else "0.0"
                change = _("{change} points").format(change=amount)
                change_tooltips[len(rows)] = _("{change} percentage points").format(change=amount)
            rows.append([_("P{number}").format(number=index + 1), rate_text(row["selected"], left["eligible"]),
                         rate_text(row["compared"], right["eligible"]), change])
        notice = {"available": _("Rates describe confirmed observations among eligible pulls. Missing feed events can hide confirmations."),
                  "tracking-differs": _("Phase comparisons are unavailable because tracking differs."),
                  "unavailable": _("Phase comparisons are unavailable because saved tracking could not be read."),
                  "not-recorded": _("Phase comparisons were not recorded for these sessions."),
                  "not-supported": _("Phase comparisons are not supported for this duty.")}[result["phase_status"]]
        if result["phases"]:
            notice += "\n" + _("Changes are shown in percentage points.")
        if not result["durations_comparable"] and ordinary_left["pulls"] and ordinary_right["pulls"]:
            notice += "\n" + _("Longest pulls cannot be compared because duration tracking differs.")
        for label, data in ((_("Selected session"), left), (_("Compared session"), right)):
            excluded = exclusion_text(data)
            if excluded:
                notice += "\n" + _("{session}: {details}").format(session=label, details=excluded)
        identity = _("Selected: {session}").format(session=session_label(self.session)) + "\n" + _("Compared: {session}").format(session=session_label(compared))
        view = identity, rows, notice
        if view == self._view:
            return
        self._view = view
        self.identity.setText(identity)
        self.notice.setText(notice)
        self.table.show()
        self.table.setRowCount(len(rows))
        for index, values in enumerate(rows):
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setToolTip(change_tooltips.get(index, value) if column == 3 else value)
                self.table.setItem(index, column, item)
        self.table.setFixedHeight(self.table.horizontalHeader().height()
                                  + len(rows) * self.table.verticalHeader().defaultSectionSize()
                                  + 2 * self.table.frameWidth())
