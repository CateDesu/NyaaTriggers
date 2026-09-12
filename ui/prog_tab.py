"""Saved prog sessions and a selectable pull duration chart."""

from datetime import datetime

from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QRectF
from PyQt6.QtGui import QColor, QPainter
from PyQt6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QHBoxLayout,
                             QHeaderView, QLabel, QLineEdit, QPlainTextEdit, QPushButton,
                             QScrollArea, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from locale_util import _
from prog_session import summary
import theme


def duration(value):
    seconds = max(0, int(value))
    return f"{seconds // 3600:02d}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"


def ending_label(value):
    return {"active": _("In progress"), "combat-ended": _("Combat ended"),
            "wipe": _("Wipe"), "feed-lost": _("Connection lost"),
            "duty-left": _("Duty left"), "program-closed": _("Program closed"),
            "session-ended": _("Session ended")}.get(value, _("Interrupted"))


class PullChart(QWidget):
    selected = pyqtSignal(int)

    def __init__(self):
        super().__init__()
        self.pulls = []
        self.setMinimumHeight(90)

    def set_pulls(self, pulls):
        self.pulls = pulls
        self.setMinimumWidth(max(300, len(pulls) * 14))
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        if not self.pulls:
            painter.setPen(QColor(theme.SUBTEXT0))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, _("Pull durations will appear here."))
            return
        maximum = max(1, max(p["duration"] for p in self.pulls))
        width = self.width() / len(self.pulls)
        for index, pull in enumerate(self.pulls):
            height = max(2, (self.height() - 24) * pull["duration"] / maximum)
            color = theme.ACCENT if pull["complete"] else theme.SUBTEXT0
            painter.fillRect(QRectF(index * width + 2, self.height() - 22 - height,
                                    max(1, width - 4), height), QColor(color))
            if width >= 24:
                painter.setPen(QColor(theme.SUBTEXT0))
                painter.drawText(QRectF(index * width, self.height() - 20, width, 20),
                                 Qt.AlignmentFlag.AlignCenter, str(index + 1))

    def mousePressEvent(self, event):
        if self.pulls and event.button() == Qt.MouseButton.LeftButton:
            index = min(len(self.pulls) - 1, int(event.position().x() * len(self.pulls) / self.width()))
            self.selected.emit(max(0, index))


class ProgTab(QWidget):
    def __init__(self, window, sessions):
        super().__init__()
        self.window = window
        self.sessions = sessions
        self.session = None
        self.pull = None
        self.dirty = None
        self.setObjectName("auroraPage")
        layout = QVBoxLayout(self)
        controls = QHBoxLayout()
        self.picker = QComboBox()
        self.picker.setMinimumWidth(180)
        controls.addWidget(self.picker, 1)
        self.start_button = QPushButton(_("Start session"))
        self.end_button = QPushButton(_("End session"))
        controls.addWidget(self.start_button)
        controls.addWidget(self.end_button)
        layout.addLayout(controls)
        self.name = QLineEdit()
        self.name.setMaxLength(200)
        self.name.setPlaceholderText(_("Session name"))
        layout.addWidget(self.name)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.status)
        self.stats = QLabel()
        self.stats.setWordWrap(True)
        layout.addWidget(self.stats)
        self.chart = PullChart()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFixedHeight(125)
        scroll.setWidget(self.chart)
        layout.addWidget(scroll)
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels([_("Pull"), _("Started"), _("Duration"),
                                              _("Ending"), _("Deaths"), _("Bookmark")])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.table, 1)
        self.bookmark = QCheckBox(_("Bookmark this pull"))
        pull_controls = QHBoxLayout()
        pull_controls.addWidget(self.bookmark)
        pull_controls.addStretch()
        self.recap_button = QPushButton(_("View death recaps"))
        pull_controls.addWidget(self.recap_button)
        layout.addLayout(pull_controls)
        self.note = QPlainTextEdit()
        self.note.setPlaceholderText(_("Notes for the selected pull"))
        self.note.setMaximumHeight(85)
        layout.addWidget(self.note)
        self.save_timer = QTimer(self)
        self.save_timer.setSingleShot(True)
        self.save_timer.setInterval(400)
        self.save_timer.timeout.connect(self.flush)
        self.picker.currentIndexChanged.connect(self.select_session)
        self.start_button.clicked.connect(self.start_session)
        self.end_button.clicked.connect(self.end_session)
        self.table.itemSelectionChanged.connect(self.select_pull)
        self.chart.selected.connect(self.table.selectRow)
        self.bookmark.toggled.connect(self.edit_pull)
        self.recap_button.clicked.connect(self.open_recaps)
        self.note.textChanged.connect(self.edit_pull)
        self.name.editingFinished.connect(self.rename)
        self.name.textEdited.connect(self.edit_name)
        self.refresh()

    def refresh(self):
        selected = self.session["id"] if self.session else None
        self.picker.blockSignals(True)
        self.picker.clear()
        for session in self.sessions.sessions:
            label = f"{datetime.fromtimestamp(session['started']):%Y-%m-%d %H:%M}  {session['name']}"
            self.picker.addItem(label, session["id"])
        index = self.picker.findData(selected)
        self.picker.setCurrentIndex(max(0, index))
        self.picker.blockSignals(False)
        self.select_session()

    def select_session(self, _index=0):
        self.flush()
        ident = self.picker.currentData()
        self.session = next((s for s in self.sessions.sessions if s["id"] == ident), None)
        self.name.setEnabled(self.session is not None)
        self.name.setText(self.session["name"] if self.session else "")
        selected = self.pull["id"] if self.pull else None
        self.table.blockSignals(True)
        pulls = self.session["pulls"] if self.session else []
        self.table.setRowCount(len(pulls))
        for index, pull in enumerate(pulls):
            values = [str(index + 1), f"{datetime.fromtimestamp(pull['started']):%H:%M:%S}",
                      duration(pull["duration"]), ending_label(pull["ending"]), str(pull["deaths"]),
                      "★" if pull["bookmark"] else ""]
            for column, value in enumerate(values):
                self.table.setItem(index, column, QTableWidgetItem(value))
        self.table.blockSignals(False)
        self.chart.set_pulls(pulls)
        if pulls:
            row = next((i for i, p in enumerate(pulls) if p["id"] == selected), len(pulls) - 1)
            self.table.selectRow(row)
        self.select_pull()
        self.tick()

    def select_pull(self):
        self.flush()
        row = self.table.currentRow()
        pulls = self.session["pulls"] if self.session else []
        self.pull = pulls[row] if 0 <= row < len(pulls) else None
        self.bookmark.blockSignals(True)
        self.note.blockSignals(True)
        self.bookmark.setEnabled(self.pull is not None)
        self.note.setEnabled(self.pull is not None)
        self.recap_button.setEnabled(self.pull is not None)
        self.bookmark.setChecked(self.pull["bookmark"] if self.pull else False)
        self.note.setPlainText(self.pull["note"] if self.pull else "")
        self.bookmark.blockSignals(False)
        self.note.blockSignals(False)

    def open_recaps(self):
        self.flush()
        if self.session is not None and self.pull is not None:
            number = self.session["pulls"].index(self.pull) + 1
            self.window._show_pull_recaps(self.session, self.pull, number)

    def edit_pull(self):
        if self.pull is None:
            return
        self.pull["bookmark"] = self.bookmark.isChecked()
        self.pull["note"] = self.note.toPlainText()[:4000]
        if len(self.note.toPlainText()) > 4000:
            self.note.blockSignals(True)
            self.note.setPlainText(self.pull["note"])
            self.note.blockSignals(False)
        self.dirty = self.session
        self.save_timer.start()
        row = self.table.currentRow()
        if row >= 0:
            self.table.setItem(row, 5, QTableWidgetItem("★" if self.pull["bookmark"] else ""))

    def edit_name(self, text):
        if self.session is not None:
            self.session["name"] = text.strip() or self.session["zone"]
            self.dirty = self.session
            self.save_timer.start()

    def rename(self):
        if self.session is not None:
            self.session["name"] = self.name.text().strip() or self.session["zone"]
            self.sessions.save(self.session)
            self.refresh()

    def flush(self):
        self.save_timer.stop()
        if self.dirty is not None:
            self.sessions.save(self.dirty)
            self.dirty = None
        self.sessions.flush_pending()

    def start_session(self):
        window = self.window
        try:
            self.session = self.sessions.start(window._current_zone, window._current_zone_id,
                                               window._current_zone,
                                               window._in_game_combat or window._dps_meter.current is not None)
        except (OSError, ValueError) as exc:
            self.sessions.save_error = str(exc)
        self.refresh()

    def end_session(self):
        self.flush()
        self.sessions.end(self.window._dps_meter.full_snapshot())
        self.refresh()

    def tick(self):
        active = self.sessions.current
        self.sessions.update_active(self.window._dps_meter.full_snapshot())
        if active is not None and self.session is active and self.sessions.pending:
            for row, pull in enumerate(active["pulls"]):
                if pull["id"] == self.sessions.pending and row < self.table.rowCount():
                    self.table.setItem(row, 2, QTableWidgetItem(duration(pull["duration"])))
                    self.table.setItem(row, 4, QTableWidgetItem(str(pull["deaths"])))
                    self.chart.update()
                    break
        if self.session is not None:
            for row, pull in enumerate(self.session["pulls"]):
                item = self.table.item(row, 4)
                if item is not None and item.text() != str(pull["deaths"]):
                    item.setText(str(pull["deaths"]))
        self.start_button.setEnabled(active is None and self.window._connected
                                     and self.window._current_zone_id > 0
                                     and self.window._combat_known)
        self.end_button.setEnabled(active is not None)
        if self.sessions.save_error:
            text = _("Session changes could not be saved: {error}").format(error=self.sessions.save_error)
        elif self.sessions.errors:
            text = _("Some saved sessions could not be read and were kept unchanged: {error}").format(
                error="\n".join(self.sessions.errors[:3]))
        elif active:
            text = _("Collecting pulls for {zone}.").format(zone=active["zone"]) if self.sessions.ready else _("Waiting for combat to end before collecting a full pull.")
        else:
            text = _("Start a session in the current duty to collect pulls. Saved sessions remain available after restart.")
        recap_warning = self.window._recap_save_warning()
        if recap_warning:
            text += "\n" + recap_warning
        self.status.setText(text)
        self.window._update_recap_notice()
        if self.session:
            stats = summary(self.session)
            self.stats.setText(_("{pulls} complete pulls · {interrupted} interrupted · Longest {longest} · Combat {combat} · Session {elapsed}").format(
                pulls=stats["pulls"], interrupted=stats["interrupted"], longest=duration(stats["longest"]),
                combat=duration(stats["combat"]), elapsed=duration(self.sessions.elapsed(self.session))))
        else:
            self.stats.setText(_("No sessions recorded yet."))
