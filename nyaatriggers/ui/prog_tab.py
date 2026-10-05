from datetime import datetime

from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QRectF
from PyQt6.QtGui import QColor, QPainter
from PyQt6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QComboBox, QHBoxLayout,
                             QHeaderView, QLabel, QLineEdit, QPlainTextEdit, QPushButton,
                             QScrollArea, QTableWidget, QTableWidgetItem, QToolButton,
                             QVBoxLayout, QWidget)

from nyaatriggers.locale_util import _
from nyaatriggers.prog_session import phase_summary, summary
from nyaatriggers.prog_phases import definition_for, read_tracking
from nyaatriggers.ui.prog_comparison import SessionComparison, duration, progress_text, session_label
from nyaatriggers import theme

PHASE_COLUMN = 3
ENDING_COLUMN = 4
DEATHS_COLUMN = 5
BOOKMARK_COLUMN = 6


def ending_label(value):
    return {"active": _("In progress"), "combat-ended": _("Combat ended"),
            "wipe": _("Wipe"), "feed-lost": _("Connection lost"),
            "duty-left": _("Duty left"), "program-closed": _("Program closed"),
            "session-ended": _("Session ended")}.get(value, _("Interrupted"))


def phase_details(pull, zone_id, definitions):
    if pull is None:
        return "", "", []
    data, definition, error = read_tracking(pull, zone_id, definitions)
    if error:
        return _("Unavailable"), _("Phase data could not be read. Notes and recaps remain available."), []
    if data is None:
        if "phase_tracking" not in pull or definition_for(zone_id, definitions) is not None:
            return _("Not recorded"), _("Phase tracking was not recorded for this pull."), []
        return _("Not supported"), _("Phase tracking is not supported for this duty."), []
    observations = {o["phase"]: o for o in data["observations"]}
    furthest = max((definition.phases.index(p) for p in observations), default=-1)
    label = _("P{number}").format(number=furthest + 1) if furthest >= 0 else _("No confirmation")
    state = {"recording": _("Recording"), "complete": _("Recording complete"),
             "interrupted": _("Recording interrupted"), "uncertain": _("Pull boundary could not be confirmed.")}[data["coverage"]]
    if data["transition"] is not None:
        state = _("Phase transition")
    elif data["coverage"] == "interrupted":
        state += " · " + ending_label(data["reason"])
    notice = state + "\n" + _("Times show the first observed confirmation after pull start, not the exact phase transition.")
    rows = []
    for index, phase in enumerate(definition.phases):
        observation = observations.get(phase)
        reached = _("Established by a later phase. Confirmation time unavailable.") if index < furthest else _("No confirmation")
        rows.append([_("P{number}").format(number=index + 1), duration(observation["at"]) if observation else "—",
                     _("Confirmed") if observation else reached])
    return label, notice, rows


class PullChart(QWidget):
    selected = pyqtSignal(int)

    def __init__(self):
        super().__init__()
        self.pulls = []
        self.tooltips = []
        self.setMouseTracking(True)
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

    def mouseMoveEvent(self, event):
        if self.tooltips:
            index = max(0, min(len(self.tooltips) - 1, int(event.position().x() * len(self.tooltips) / self.width())))
            self.setToolTip(self.tooltips[index])


class ProgTab(QWidget):
    def __init__(self, window, sessions):
        super().__init__()
        self.window = window
        self.sessions = sessions
        self.session = None
        self._hidden_selection = None
        self.pull = None
        self.dirty = None
        self.setObjectName("auroraPage")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.content_scroll = QScrollArea()
        self.content_scroll.setWidgetResizable(True)
        self.content_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        body = QWidget()
        layout = QVBoxLayout(body)
        self.content_scroll.setWidget(body)
        header = QVBoxLayout()
        header.setContentsMargins(9, 9, 9, 0)
        outer.addLayout(header)
        search_controls = QHBoxLayout()
        self.session_search = QLineEdit()
        self.session_search.setPlaceholderText(_("Find sessions by name or duty"))
        self.session_search.setAccessibleName(_("Find sessions by name or duty"))
        self.session_search.setClearButtonEnabled(True)
        search_controls.addWidget(self.session_search, 1)
        self.show_archived = QCheckBox(_("Show archived"))
        search_controls.addWidget(self.show_archived)
        self.session_count = QLabel()
        search_controls.addWidget(self.session_count)
        header.addLayout(search_controls)
        controls = QHBoxLayout()
        self.picker = QComboBox()
        self.picker.setMinimumWidth(180)
        self.picker.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.picker.setMinimumContentsLength(16)
        controls.addWidget(self.picker, 1)
        self.compare_button = QPushButton(_("Compare with…"))
        self.compare_button.setCheckable(True)
        controls.addWidget(self.compare_button)
        self.start_button = QPushButton(_("Start session"))
        self.end_button = QPushButton(_("End session"))
        controls.addWidget(self.start_button)
        controls.addWidget(self.end_button)
        header.addLayout(controls)
        outer.addWidget(self.content_scroll)
        self.name = QLineEdit()
        self.name.setMaxLength(200)
        self.name.setPlaceholderText(_("Session name"))
        name_controls = QHBoxLayout()
        name_controls.addWidget(self.name, 1)
        self.archive_button = QPushButton(_("Archive session"))
        name_controls.addWidget(self.archive_button)
        layout.addLayout(name_controls)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.status)
        self.stats = QLabel()
        self.stats.setWordWrap(True)
        layout.addWidget(self.stats)
        self.phase_progress = QLabel()
        self.phase_progress.setWordWrap(True)
        self.phase_progress.setTextFormat(Qt.TextFormat.PlainText)
        self.phase_progress.setToolTip(_("Phase rates include complete attempts with matching verified rules. Interrupted, active, unreadable, and unrecorded pulls are excluded."))
        layout.addWidget(self.phase_progress)
        self.comparison = SessionComparison(self.flush)
        self.comparison.hide()
        layout.addWidget(self.comparison)
        self.chart = PullChart()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFixedHeight(125)
        scroll.setWidget(self.chart)
        layout.addWidget(scroll)
        self.table = QTableWidget(0, 7)
        self.table.setMinimumHeight(150)
        self.table.setHorizontalHeaderLabels([_("Pull"), _("Started"), _("Duration"),
                                              _("Furthest phase"), _("Ending"), _("Deaths"), _("Bookmark")])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(ENDING_COLUMN, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setMinimumSectionSize(60)
        layout.addWidget(self.table, 1)
        self.phase_heading = QLabel(_("Phase confirmations"))
        layout.addWidget(self.phase_heading)
        self.phase_notice = QLabel()
        self.phase_notice.setWordWrap(True)
        self.phase_notice.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.phase_notice)
        self.phase_table = QTableWidget(0, 3)
        self.phase_table.setHorizontalHeaderLabels([_("Phase"), _("Confirmed at"), _("Observation")])
        self.phase_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.phase_table.verticalHeader().hide()
        self.phase_table.verticalHeader().setDefaultSectionSize(23)
        self.phase_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.phase_table)
        self._phase_view = None
        self.bookmark = QCheckBox(_("Bookmark this pull"))
        pull_controls = QHBoxLayout()
        pull_controls.addWidget(self.bookmark)
        self.previous_bookmark = QToolButton()
        self.previous_bookmark.setText("‹ ★")
        self.previous_bookmark.setToolTip(_("Previous bookmarked pull"))
        self.previous_bookmark.setAccessibleName(_("Previous bookmarked pull"))
        pull_controls.addWidget(self.previous_bookmark)
        self.next_bookmark = QToolButton()
        self.next_bookmark.setText("★ ›")
        self.next_bookmark.setToolTip(_("Next bookmarked pull"))
        self.next_bookmark.setAccessibleName(_("Next bookmarked pull"))
        pull_controls.addWidget(self.next_bookmark)
        pull_controls.addStretch()
        self.copy_pull_button = QPushButton(_("Copy pull summary"))
        pull_controls.addWidget(self.copy_pull_button)
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
        self.session_search.textChanged.connect(self.refresh)
        self.show_archived.toggled.connect(self.refresh)
        self.archive_button.clicked.connect(self.toggle_archived)
        self.compare_button.toggled.connect(self.toggle_comparison)
        self.start_button.clicked.connect(self.start_session)
        self.end_button.clicked.connect(self.end_session)
        self.table.itemSelectionChanged.connect(self.select_pull)
        self.chart.selected.connect(lambda index: self.table.selectRow(self.table.rowCount() - 1 - index))
        self.bookmark.toggled.connect(self.edit_pull)
        self.previous_bookmark.clicked.connect(lambda: self.move_bookmark(-1))
        self.next_bookmark.clicked.connect(lambda: self.move_bookmark(1))
        self.copy_pull_button.clicked.connect(self.copy_pull_summary)
        self.recap_button.clicked.connect(self.open_recaps)
        self.note.textChanged.connect(self.edit_pull)
        self.name.editingFinished.connect(self.rename)
        self.name.textEdited.connect(self.edit_name)
        self.refresh()

    def refresh(self):
        selected = self.session["id"] if self.session else self._hidden_selection[0] if self._hidden_selection else None
        words = self.session_search.text().casefold().split()
        matches = [session for session in self.sessions.sessions
                   if (self.show_archived.isChecked() or session.get("archived") is not True)
                   and all(word in (session["name"] + " " + session["zone"]).casefold() for word in words)]
        if not matches and self.session is not None:
            self._hidden_selection = self.session["id"], self.pull["id"] if self.pull else None
        self.picker.blockSignals(True)
        self.picker.clear()
        for session in matches:
            self.picker.addItem(session_label(session), session["id"])
        index = self.picker.findData(selected)
        self.picker.setCurrentIndex(max(0, index))
        self.picker.blockSignals(False)
        self.picker.setEnabled(bool(matches))
        self.session_count.setText(_("{shown} of {total} sessions").format(shown=len(matches), total=len(self.sessions.sessions)))
        self.session_count.setVisible(bool(words))
        self.select_session()

    def select_session(self, _index=0):
        self.flush()
        ident = self.picker.currentData()
        previous = self.session
        self.session = next((s for s in self.sessions.sessions if s["id"] == ident), None)
        self.name.setEnabled(self.session is not None)
        name = self.session["name"] if self.session else ""
        if self.session is not previous or (not self.name.hasFocus() and self.name.text() != name):
            self.name.setText(name)
        selected = self.pull["id"] if self.pull else (
            self._hidden_selection[1] if self._hidden_selection and self._hidden_selection[0] == ident else None)
        if self.session is not None:
            self._hidden_selection = None
        self.table.blockSignals(True)
        pulls = self.session["pulls"] if self.session else []
        self.table.setRowCount(len(pulls))
        for index, pull in enumerate(reversed(pulls)):
            values = [str(len(pulls) - index), f"{datetime.fromtimestamp(pull['started']):%H:%M:%S}",
                      duration(pull["duration"]), self.phase_values(pull)[0], ending_label(pull["ending"]), str(pull["deaths"]),
                      "★" if pull["bookmark"] else ""]
            for column, value in enumerate(values):
                self.table.setItem(index, column, QTableWidgetItem(value))
        self.table.blockSignals(False)
        self.chart.set_pulls(pulls)
        if pulls:
            row = next((i for i, p in enumerate(reversed(pulls)) if p["id"] == selected), 0)
            self.table.selectRow(row)
        self.select_pull()
        self.tick()

    def select_pull(self):
        self.flush()
        row = self.table.currentRow()
        pulls = self.session["pulls"] if self.session else []
        previous = self.pull
        self.pull = pulls[-1 - row] if 0 <= row < len(pulls) else None
        self.bookmark.blockSignals(True)
        self.note.blockSignals(True)
        self.bookmark.setEnabled(self.pull is not None)
        self.note.setEnabled(self.pull is not None)
        self.recap_button.setEnabled(self.pull is not None)
        self.copy_pull_button.setEnabled(self.pull is not None)
        self.bookmark.setChecked(self.pull["bookmark"] if self.pull else False)
        note = self.pull["note"] if self.pull else ""
        if self.pull is not previous or self.note.toPlainText() != note:
            self.note.setPlainText(note)
        self.bookmark.blockSignals(False)
        self.note.blockSignals(False)
        self.refresh_phase_details()
        self.refresh_bookmark_buttons()

    def bookmark_targets(self):
        pulls = self.session["pulls"] if self.session else []
        selected = next((index for index, pull in enumerate(pulls) if pull is self.pull), None)
        if selected is None:
            return None, None
        previous = next((index for index in range(selected - 1, -1, -1) if pulls[index]["bookmark"]), None)
        following = next((index for index in range(selected + 1, len(pulls)) if pulls[index]["bookmark"]), None)
        return previous, following

    def refresh_bookmark_buttons(self):
        previous, following = self.bookmark_targets()
        self.previous_bookmark.setEnabled(previous is not None)
        self.next_bookmark.setEnabled(following is not None)

    def move_bookmark(self, direction):
        previous, following = self.bookmark_targets()
        target = previous if direction < 0 else following
        if target is not None:
            self.table.selectRow(len(self.session["pulls"]) - 1 - target)

    def copy_pull_summary(self):
        self.flush()
        self.tick()
        if self.session is None or self.pull is None:
            return
        pull = self.pull
        number = self.session["pulls"].index(pull) + 1
        phase, notice, rows = self.phase_values(pull)
        lines = [_("Session: {session}").format(session=session_label(self.session)),
                 _("Duty: {zone}").format(zone=self.session["zone"]),
                 _("Pull {number} · {started}").format(number=number,
                     started=f"{datetime.fromtimestamp(pull['started']):%Y-%m-%d %H:%M:%S}"),
                 _("Duration: {duration} · Ending: {ending}").format(
                     duration=duration(pull["duration"]), ending=ending_label(pull["ending"])),
                 _("Deaths: {deaths}").format(deaths=pull["deaths"])]
        if "recap_count" in pull:
            lines.append(_("Death recaps: {count}").format(count=pull["recap_count"]))
        if pull["bookmark"]:
            lines.append(_("Bookmarked"))
        lines.extend([_("Phase: {phase}").format(phase=phase), notice])
        if rows:
            lines.append("\t".join([_("Phase"), _("Confirmed at"), _("Observation")]))
            lines.extend("\t".join(row) for row in rows)
        if pull["note"]:
            lines.extend([_("Notes:"), pull["note"]])
        QApplication.clipboard().setText("\n".join(lines))

    def phase_values(self, pull):
        return phase_details(pull, self.session["zone_id"] if self.session else 0, self.sessions.definitions)

    def refresh_phase_details(self):
        view = self.phase_values(self.pull)
        if view == self._phase_view:
            return
        self._phase_view = view
        _, notice, rows = view
        self.phase_heading.setVisible(self.pull is not None)
        self.phase_notice.setText(notice)
        self.phase_notice.setVisible(bool(notice))
        self.phase_table.setVisible(bool(rows))
        self.phase_table.setRowCount(len(rows))
        for row, values in enumerate(rows):
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setToolTip(value)
                self.phase_table.setItem(row, column, item)
        self.phase_table.setFixedHeight(self.phase_table.horizontalHeader().height()
                                        + len(rows) * self.phase_table.verticalHeader().defaultSectionSize()
                                        + 2 * self.phase_table.frameWidth())

    def open_recaps(self):
        self.flush()
        if self.session is not None and self.pull is not None:
            number = self.session["pulls"].index(self.pull) + 1
            self.window._show_pull_recaps(self.session, self.pull, number)

    def toggle_comparison(self, opened):
        self.flush()
        if opened:
            self.comparison.open()
        self.comparison.setVisible(opened)

    def edit_pull(self):
        if self.pull is None:
            return
        self.pull["bookmark"] = self.bookmark.isChecked()
        self.pull["note"] = self.note.toPlainText()[:4000]
        if len(self.note.toPlainText()) > 4000:
            position = self.note.textCursor().position()
            self.note.blockSignals(True)
            self.note.setPlainText(self.pull["note"])
            cursor = self.note.textCursor()
            cursor.setPosition(min(position, self.note.document().characterCount() - 1))
            self.note.setTextCursor(cursor)
            self.note.blockSignals(False)
        self.dirty = self.session
        self.save_timer.start()
        row = self.table.currentRow()
        if row >= 0:
            self.table.setItem(row, BOOKMARK_COLUMN, QTableWidgetItem("★" if self.pull["bookmark"] else ""))
        self.refresh_bookmark_buttons()

    def edit_name(self, text):
        if self.session is not None:
            self.session["name"] = text.strip() or self.session["zone"]
            self.dirty = self.session
            self.save_timer.start()

    def rename(self):
        if self.session is not None:
            self.session["name"] = self.name.text().strip() or self.session["zone"]
            if self.name.text() != self.session["name"]:
                self.name.setText(self.session["name"])
            self.sessions.save(self.session)
            self.refresh()

    def toggle_archived(self):
        self.flush()
        if self.session is not None and self.session is not self.sessions.current and self.session["state"] != "active":
            if not self.sessions.set_archived(self.session, self.session.get("archived") is not True):
                self.tick()
                return
            self.refresh()

    def flush(self):
        self.save_timer.stop()
        if self.dirty is not None:
            self.sessions.save(self.dirty)
            self.dirty = None
        self.sessions.flush_pending()

    def start_session(self):
        window = self.window
        if window._awaiting_zone_metadata:
            return
        try:
            self.session = self.sessions.start(window._current_zone, window._current_zone_id,
                                               window._current_zone,
                                               window._in_game_combat or window._dps_meter.current is not None)
            self.session_search.blockSignals(True)
            self.session_search.clear()
            self.session_search.blockSignals(False)
        except (OSError, ValueError) as exc:
            self.sessions.save_error = str(exc)
        self.refresh()

    def end_session(self):
        self.flush()
        self.sessions.end(self.window._dps_meter.full_snapshot())
        self.refresh()

    def tick(self):
        self.sessions.poll_saves()
        active = self.sessions.current
        self.sessions.update_active(self.window._dps_meter.full_snapshot())
        self.sessions.check_phase_timeout()
        self.sessions.checkpoint()
        if active is not None and self.session is active and self.sessions.pending:
            self.chart.update()
        if self.session is not None:
            tooltips = []
            for index, pull in enumerate(self.session["pulls"]):
                row = len(self.session["pulls"]) - 1 - index
                phase, notice, _rows = self.phase_values(pull)
                for column, value in ((DEATHS_COLUMN, str(pull["deaths"])),
                                      (PHASE_COLUMN, phase), (ENDING_COLUMN, ending_label(pull["ending"])),
                                      (2, duration(pull["duration"]))):
                    item = self.table.item(row, column)
                    if item is not None and item.text() != value:
                        item.setText(value)
                item = self.table.item(row, PHASE_COLUMN)
                if item is not None:
                    item.setToolTip(notice)
                tooltips.append(f"{index + 1} · {duration(pull['duration'])} · {phase} · {ending_label(pull['ending'])}")
            self.chart.tooltips = tooltips
        self.refresh_phase_details()
        self.start_button.setEnabled(active is None and self.window._connected
                                     and not self.window._awaiting_zone_metadata
                                     and self.window._current_zone_id > 0
                                     and self.window._combat_known)
        self.end_button.setEnabled(active is not None)
        self.compare_button.setEnabled(self.session is not None)
        self.archive_button.setEnabled(self.session is not None and self.session is not active
                                       and self.session["state"] != "active")
        self.archive_button.setText(_("Restore session") if self.session and self.session.get("archived") is True
                                    else _("Archive session"))
        self.picker.setToolTip(session_label(self.session) if self.session else "")
        if self.sessions.save_error:
            text = _("Session changes could not be saved: {error}").format(error=self.sessions.save_error)
        elif self.sessions.errors:
            text = _("Some saved sessions could not be read and were kept unchanged: {error}").format(
                error="\n".join(self.sessions.errors[:3]))
        elif active:
            text = _("Collecting pulls for {zone}.").format(zone=active["zone"]) if self.sessions.ready else _("Waiting for combat to end before collecting a full pull.")
            if not self.sessions.ready and self.sessions.definition is not None:
                text = _("Waiting for a verified fresh pull.")
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
            self.phase_progress.setText(progress_text(phase_summary(self.session, self.sessions.definitions)))
        else:
            empty = _("No sessions recorded yet.")
            if self.sessions.sessions:
                empty = (_("No sessions match your search.") if self.session_search.text().strip()
                         else _("No sessions to display. Enable Show archived to view archived sessions."))
            self.stats.setText(empty)
            self.phase_progress.clear()
        self.comparison.refresh(self.session, self.sessions, self.show_archived.isChecked())
