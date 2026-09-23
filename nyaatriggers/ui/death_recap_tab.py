"""Death selection and the observed events leading up to it."""

from datetime import datetime

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (QAbstractItemView, QHBoxLayout, QHeaderView, QLabel, QListWidget,
                             QPushButton, QScrollArea, QSplitter, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from nyaatriggers.locale_util import _
from nyaatriggers import theme


class DeathRecapTabMixin:
    def _build_death_recap_tab(self):
        self._recap_records = []
        self._recap_context = None
        self._recap_read_errors = []
        page = QWidget()
        page.setObjectName("auroraPage")
        layout = QVBoxLayout(page)
        title = QLabel(_("Death Recap"))
        title.setStyleSheet(f"color: {theme.ACCENT}; font-size: 16pt; font-weight: bold;")
        layout.addWidget(title)
        hint = QLabel(_("Observed damage, healing and statuses from the last 15 seconds. "
                        "Keeps 80 deaths until close. Prog recaps are saved. "
                        "Healing includes overheal. Missing events and statuses cannot be recovered."))
        hint.setWordWrap(True)
        layout.addWidget(hint)
        controls = QHBoxLayout()
        self._recap_scope = QLabel(_("Recent deaths"))
        self._recap_scope.setTextFormat(Qt.TextFormat.PlainText)
        self._recap_scope.setWordWrap(True)
        controls.addWidget(self._recap_scope, 1)
        self._recap_recent = QPushButton(_("Recent deaths"))
        self._recap_recent.clicked.connect(self._show_recent_recaps)
        self._recap_recent.hide()
        controls.addWidget(self._recap_recent)
        self._recap_back = QPushButton(_("Back to Prog"))
        self._recap_back.clicked.connect(self._back_to_prog)
        self._recap_back.hide()
        controls.addWidget(self._recap_back)
        layout.addLayout(controls)
        self._recap_notice = QLabel()
        self._recap_notice.setTextFormat(Qt.TextFormat.PlainText)
        self._recap_notice.setWordWrap(True)
        layout.addWidget(self._recap_notice)
        split = QSplitter(Qt.Orientation.Horizontal)
        split.setChildrenCollapsible(False)
        split.setHandleWidth(6)
        self._recap_list = QListWidget()
        self._recap_list.setMinimumWidth(200)
        split.addWidget(self._recap_list)
        self._recap_detail = QSplitter(Qt.Orientation.Vertical)
        self._recap_detail.setChildrenCollapsible(False)
        self._recap_detail.setHandleWidth(6)
        self._recap_statuses = QLabel(_("No deaths recorded yet."))
        self._recap_statuses.setWordWrap(True)
        self._recap_statuses.setTextFormat(Qt.TextFormat.PlainText)
        self._recap_statuses.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        statuses = QScrollArea()
        statuses.setWidgetResizable(True)
        statuses.setMinimumHeight(45)
        statuses.setWidget(self._recap_statuses)
        self._recap_detail.addWidget(statuses)
        self._recap_table = QTableWidget(0, 5)
        self._recap_table.setHorizontalHeaderLabels(
            [_("Time"), _("Event"), _("Source"), _("Ability or status"), _("Amount")])
        self._recap_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._recap_table.verticalHeader().hide()
        header = self._recap_table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setMinimumSectionSize(60)
        metrics = self._recap_table.fontMetrics()
        event_width = max(metrics.horizontalAdvance(label) for label in
                          (_("Event"), _("Damage"), _("Healing"), _("DoT"), _("HoT"),
                           _("Status gained"), _("Status lost"), _("Instant death"))) + 24
        for column, width in enumerate((80, max(140, event_width), 160, 240, 100)):
            self._recap_table.setColumnWidth(column, width)
        header.sectionResized.connect(self._recap_table.resizeRowsToContents)
        self._recap_table.setMinimumHeight(140)
        self._recap_detail.addWidget(self._recap_table)
        self._recap_detail.setStretchFactor(1, 1)
        self._recap_detail.setSizes([65, 400])
        split.addWidget(self._recap_detail)
        split.setStretchFactor(1, 1)
        split.setSizes([220, 800])
        layout.addWidget(split, 1)
        self._recap_list.currentRowChanged.connect(self._select_recap)
        self._death_recap.on_death = self._recap_added
        return page

    def _recap_added(self, death):
        self._prog_sessions.update_active(self._dps_meter.full_snapshot())
        saved = self._prog_sessions.record_death(death)
        tab = self._prog_tab
        if (saved is not None and tab.session is self._prog_sessions.current
                and tab.table.rowCount() != len(tab.session["pulls"])):
            tab.refresh()
        if self._recap_context is None:
            self._recap_records = list(self._death_recap.deaths)
            self._refresh_recap_list()
        elif saved is not None:
            session, pull, _number = self._recap_context
            if (saved["session_id"], saved["pull_id"]) == (session["id"], pull["id"]):
                self._recap_records.insert(0, saved)
                self._refresh_recap_list()
        self._update_recap_notice()

    def _show_pull_recaps(self, session, pull, number):
        self._recap_context = (session, pull, number)
        self._recap_records, self._recap_read_errors = self._prog_sessions.recaps.load(session["id"], pull["id"])
        self._recap_scope.setText(_("{session} · Pull {number} · {zone}").format(
            session=session["name"], number=number, zone=session["zone"]))
        self._recap_recent.show()
        self._recap_back.show()
        self._refresh_recap_list()
        self._update_recap_notice()
        self._nav_buttons[self._stack.indexOf(self._death_recap_tab)].click()

    def _show_recent_recaps(self):
        self._recap_context = None
        self._recap_read_errors = []
        self._recap_records = list(self._death_recap.deaths)
        self._recap_scope.setText(_("Recent deaths"))
        self._recap_recent.hide()
        self._recap_back.hide()
        self._refresh_recap_list()
        self._update_recap_notice()

    def _back_to_prog(self):
        self._nav_buttons[self._stack.indexOf(self._prog_tab)].click()

    def _recap_save_warning(self):
        messages = []
        if self._prog_sessions.recaps.save_error:
            messages.append(_("Death recaps could not be saved: {error}").format(
                error=self._prog_sessions.recaps.save_error))
        if self._prog_sessions.recaps.dropped:
            messages.append(_("Storage stayed unavailable. {count} death recaps could not be retained for retry.").format(
                count=self._prog_sessions.recaps.dropped))
        return "\n".join(messages)

    def _update_recap_notice(self):
        warning = self._recap_save_warning()
        messages = [warning] if warning else []
        if self._recap_read_errors:
            messages.append(_("Some saved death recaps could not be read and were kept unchanged: {error}").format(
                error="\n".join(self._recap_read_errors[:3])))
        if self._recap_context is not None:
            _session, pull, _number = self._recap_context
            count = pull.get("recap_count")
            if count is None:
                messages.append(_("This pull was recorded before saved death recaps were available."))
            elif len(self._recap_records) < count:
                messages.append(_("Only {available} of {recorded} recorded death recaps are available.").format(
                    available=len(self._recap_records), recorded=count))
        self._recap_notice.setText("\n".join(messages))
        self._recap_notice.setVisible(bool(messages))

    def _refresh_recap_list(self):
        selected = self._recap_list.currentRow()
        selected_id = self._recap_list.currentItem().data(Qt.ItemDataRole.UserRole) if selected >= 0 else None
        self._recap_list.blockSignals(True)
        self._recap_list.clear()
        for death in self._recap_records:
            self._recap_list.addItem(f"{datetime.fromtimestamp(death['when']):%H:%M:%S}  {death['name']}\n{death['zone']}")
            item = self._recap_list.item(self._recap_list.count() - 1)
            item.setData(Qt.ItemDataRole.UserRole, death["id"])
        row = next((i for i, d in enumerate(self._recap_records) if d["id"] == selected_id), 0)
        self._recap_list.blockSignals(False)
        self._recap_list.setCurrentRow(row if self._recap_records else -1)
        self._select_recap(self._recap_list.currentRow())

    def _select_recap(self, row):
        if not 0 <= row < len(self._recap_records):
            self._recap_statuses.setText(_("No death recaps recorded for this pull.")
                                         if self._recap_context else _("No deaths recorded yet."))
            self._recap_table.setRowCount(0)
            return
        death = self._recap_records[row]
        statuses = ", ".join(s["name"] for s in death["statuses"]) or _("None observed")
        self._recap_statuses.setText(_("Statuses observed at death: {statuses}").format(statuses=statuses))
        labels = {"damage": _("Damage"), "heal": _("Healing"), "dot": _("DoT"),
                  "hot": _("HoT"), "gained": _("Status gained"), "lost": _("Status lost"),
                  "instant-death": _("Instant death")}
        events = death["events"]
        self._recap_table.setRowCount(len(events))
        for row, event in enumerate(reversed(events)):
            values = [f"{event['time']:.1f}s", labels[event["kind"]], event["source"],
                      event["name"], f"{event['amount']:,}" if event["amount"] is not None else ""]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setToolTip(value)
                self._recap_table.setItem(row, column, item)
        self._recap_table.resizeRowsToContents()
        self._recap_table.scrollToTop()
