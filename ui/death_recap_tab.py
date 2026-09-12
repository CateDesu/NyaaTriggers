"""Death selection and the observed events leading up to it."""

from datetime import datetime

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (QAbstractItemView, QHeaderView, QLabel, QListWidget,
                             QSplitter, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from locale_util import _
import theme


class DeathRecapTabMixin:
    def _build_death_recap_tab(self):
        page = QWidget()
        page.setObjectName("auroraPage")
        layout = QVBoxLayout(page)
        title = QLabel(_("Death Recap"))
        title.setStyleSheet(f"color: {theme.ACCENT}; font-size: 16pt; font-weight: bold;")
        layout.addWidget(title)
        hint = QLabel(_("The last 15 seconds of observed damage, healing, and statuses. "
                        "Keeps up to 80 deaths until the program closes. Healing includes overheal. "
                        "Missing events and unobserved statuses cannot be reconstructed."))
        hint.setWordWrap(True)
        layout.addWidget(hint)
        split = QSplitter(Qt.Orientation.Horizontal)
        self._recap_list = QListWidget()
        self._recap_list.setMinimumWidth(200)
        split.addWidget(self._recap_list)
        detail = QWidget()
        detail_layout = QVBoxLayout(detail)
        self._recap_statuses = QLabel(_("No deaths recorded yet."))
        self._recap_statuses.setWordWrap(True)
        self._recap_statuses.setTextFormat(Qt.TextFormat.PlainText)
        detail_layout.addWidget(self._recap_statuses)
        self._recap_table = QTableWidget(0, 5)
        self._recap_table.setHorizontalHeaderLabels(
            [_("Time"), _("Event"), _("Source"), _("Ability or status"), _("Amount")])
        self._recap_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._recap_table.verticalHeader().hide()
        self._recap_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        detail_layout.addWidget(self._recap_table)
        split.addWidget(detail)
        split.setStretchFactor(1, 1)
        layout.addWidget(split)
        self._recap_list.currentRowChanged.connect(self._select_recap)
        self._death_recap.on_death = self._recap_added
        return page

    def _recap_added(self, _death):
        selected = self._recap_list.currentRow()
        selected_id = self._recap_list.currentItem().data(Qt.ItemDataRole.UserRole) if selected >= 0 else None
        self._recap_list.blockSignals(True)
        self._recap_list.clear()
        for death in self._death_recap.deaths:
            self._recap_list.addItem(f"{datetime.fromtimestamp(death['when']):%H:%M:%S}  {death['name']}\n{death['zone']}")
            item = self._recap_list.item(self._recap_list.count() - 1)
            item.setData(Qt.ItemDataRole.UserRole, death["id"])
        row = next((i for i, d in enumerate(self._death_recap.deaths) if d["id"] == selected_id), 0)
        self._recap_list.blockSignals(False)
        self._recap_list.setCurrentRow(row)

    def _select_recap(self, row):
        if not 0 <= row < len(self._death_recap.deaths):
            return
        death = self._death_recap.deaths[row]
        statuses = ", ".join(s["name"] for s in death["statuses"]) or _("None observed")
        self._recap_statuses.setText(_("Statuses observed at death: {statuses}").format(statuses=statuses))
        labels = {"damage": _("Damage"), "heal": _("Healing"), "dot": _("DoT"),
                  "hot": _("HoT"), "gained": _("Status gained"), "lost": _("Status lost"),
                  "instant-death": _("Instant death")}
        events = death["events"]
        self._recap_table.setRowCount(len(events))
        for row, event in enumerate(events):
            values = [f"{event['time']:.1f}s", labels[event["kind"]], event["source"],
                      event["name"], f"{event['amount']:,}" if event["amount"] is not None else ""]
            for column, value in enumerate(values):
                self._recap_table.setItem(row, column, QTableWidgetItem(value))
        self._recap_table.scrollToBottom()
