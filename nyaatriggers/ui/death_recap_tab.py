"""Death selection and the observed events leading up to it."""

from datetime import datetime
from pathlib import Path
import time

from PyQt6.QtCore import Qt, QTimer, QSize
from PyQt6.QtGui import QColor, QIcon
from PyQt6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QMessageBox, QLineEdit, QListWidgetItem, QAbstractItemView, QHBoxLayout, QHeaderView, QLabel, QListWidget,
                             QPushButton, QScrollArea, QSplitter, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from nyaatriggers.locale_util import _
from nyaatriggers import theme, app_common as ac
from nyaatriggers.recap_filters import DEFAULT_HIDDEN_STATUSES, hidden_statuses
from nyaatriggers.ui.recap_widgets import RecapDelegate, RecapIcons, status_key, status_name
from nyaatriggers.ui.recap_browser import LogImportDialog, SavedRecapDialog


class DeathRecapTabMixin:
    def _build_death_recap_tab(self):
        self._recap_records = []
        self._recap_context = None
        self._recap_read_errors = []
        self._recap_visible = []
        self._recap_import = None
        self._recap_import_dialog = None
        self._recap_hidden = hidden_statuses(self._settings)
        self._recap_icons = RecapIcons(ac._DATA_DIR / "cache" / "recap_icons", self)
        page = QWidget()
        page.setObjectName("auroraPage")
        layout = QVBoxLayout(page)
        title = QLabel(_("Death Recap"))
        title.setStyleSheet(f"color: {theme.ACCENT}; font-size: 16pt; font-weight: bold;")
        layout.addWidget(title)
        hint = QLabel(_("The last 60 seconds before death. Green is HP, light green is confirmed healing, yellow is shields."))
        hint.setWordWrap(True)
        layout.addWidget(hint)
        filters = QHBoxLayout()
        filters.addWidget(QLabel(_("Player")))
        self._recap_player = QComboBox()
        self._recap_player.setMinimumWidth(180)
        self._recap_player.addItem(_("All players"), None)
        self._recap_player.currentIndexChanged.connect(self._filter_recap_player)
        filters.addWidget(self._recap_player)
        self._recap_filter = QPushButton(_("Filter buffs"))
        self._recap_filter.clicked.connect(self._filter_recap_buffs)
        filters.addWidget(self._recap_filter)
        filters.addStretch()
        layout.addLayout(filters)
        event_filters = QHBoxLayout()
        self._recap_damage = QCheckBox(_("Damage"))
        self._recap_damage.setChecked(self._settings.get("recap_show_damage", True) is not False)
        self._recap_healing = QCheckBox(_("Healing"))
        self._recap_healing.setChecked(self._settings.get("recap_show_healing", True) is not False)
        legacy_changes = self._settings.get("recap_show_status_changes", False) is True
        self._recap_buffs = QCheckBox(_("Buff changes"))
        self._recap_buffs.setChecked(self._settings.get("recap_show_buffs", legacy_changes) is not False)
        self._recap_debuffs = QCheckBox(_("Debuff changes"))
        self._recap_debuffs.setChecked(self._settings.get("recap_show_debuffs", legacy_changes) is not False)
        self._recap_health = QCheckBox(_("HP and shield updates"))
        self._recap_health.setChecked(self._settings.get("recap_show_health", False) is True)
        for checkbox in (self._recap_damage, self._recap_healing, self._recap_buffs,
                         self._recap_debuffs, self._recap_health):
            checkbox.toggled.connect(self._recap_options_changed)
            event_filters.addWidget(checkbox)
        event_filters.addStretch()
        layout.addLayout(event_filters)
        controls = QHBoxLayout()
        self._recap_scope = QLabel(_("Recent deaths"))
        self._recap_scope.setTextFormat(Qt.TextFormat.PlainText)
        self._recap_scope.setWordWrap(True)
        controls.addWidget(self._recap_scope, 1)
        saved = QPushButton(_("Saved pulls…"))
        saved.clicked.connect(self._browse_saved_recaps)
        controls.addWidget(saved)
        open_log = QPushButton(_("Open log…"))
        open_log.clicked.connect(self._open_recap_log)
        controls.addWidget(open_log)
        self._recap_log_pull = QComboBox()
        self._recap_log_pull.setMinimumWidth(140)
        self._recap_log_pull.hide()
        self._recap_log_pull.currentIndexChanged.connect(self._filter_recap_player)
        controls.addWidget(self._recap_log_pull)
        self._recap_previous = QPushButton(_("Previous pull"))
        self._recap_previous.clicked.connect(lambda: self._step_recap_pull(-1))
        self._recap_previous.hide()
        controls.addWidget(self._recap_previous)
        self._recap_next = QPushButton(_("Next pull"))
        self._recap_next.clicked.connect(lambda: self._step_recap_pull(1))
        self._recap_next.hide()
        controls.addWidget(self._recap_next)
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
        self._recap_notice.hide()
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
        self._recap_table = QTableWidget(0, 6)
        self._recap_table.setHorizontalHeaderLabels(
            [_("Time"), _("Amount"), _("Ability"), _("Source"), _("HP before"), _("Status effects")])
        self._recap_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._recap_table.verticalHeader().hide()
        header = self._recap_table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setMinimumSectionSize(60)
        header.setStretchLastSection(True)
        for column, width in enumerate((75, 110, 240, 165, 240, 340)):
            self._recap_table.setColumnWidth(column, width)
        self._recap_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._recap_table.setIconSize(QSize(22, 22))
        self._recap_delegate = RecapDelegate(self._recap_icons, self._recap_hidden, self._recap_table)
        self._recap_table.setItemDelegate(self._recap_delegate)
        self._recap_icons.changed.connect(self._recap_metadata_changed)
        header.sectionResized.connect(self._recap_table.resizeRowsToContents)
        self._recap_table.setMinimumHeight(140)
        self._recap_detail.addWidget(self._recap_table)
        self._recap_detail.setStretchFactor(1, 1)
        self._recap_detail.setSizes([45, 500])
        split.addWidget(self._recap_detail)
        split.setStretchFactor(1, 1)
        split.setSizes([220, 800])
        layout.addWidget(split, 1)
        self._recap_list.currentRowChanged.connect(self._select_recap)
        self._recap_age_timer = QTimer(self)
        self._recap_age_timer.setInterval(30000)
        self._recap_age_timer.timeout.connect(self._update_recap_ages)
        self._recap_age_timer.start()
        self._death_recap.on_death = self._recap_added
        return page

    def _recap_added(self, death):
        self._prog_sessions.update_active(self._dps_meter.full_snapshot())
        saved = self._prog_sessions.record_death(death)
        tab = self._prog_tab
        if (saved is not None and tab.session is self._prog_sessions.current
                and tab.table.rowCount() != len(tab.session["pulls"])):
            tab.refresh()
        if self._recap_context is None and self._recap_import is None:
            self._recap_records = list(self._death_recap.deaths)
            self._refresh_recap_list()
        elif saved is not None and self._recap_context is not None:
            session, pull, _number = self._recap_context
            if (saved["session_id"], saved["pull_id"]) == (session["id"], pull["id"]):
                self._recap_records.insert(0, saved)
                self._refresh_recap_list()
        self._update_recap_notice()

    def _show_pull_recaps(self, session, pull, number):
        self._close_imported_recaps()
        self._recap_context = (session, pull, number)
        self._recap_records, self._recap_read_errors = self._prog_sessions.recaps.load(session["id"], pull["id"])
        self._recap_scope.setText(_("{session} · Pull {number} · {zone}").format(
            session=session["name"], number=number, zone=session["zone"]))
        self._recap_recent.show()
        self._recap_back.show()
        self._recap_previous.setVisible(True)
        self._recap_next.setVisible(True)
        self._recap_previous.setEnabled(number > 1)
        self._recap_next.setEnabled(number < len(session["pulls"]))
        self._refresh_recap_list()
        self._update_recap_notice()
        self._nav_buttons[self._stack.indexOf(self._death_recap_tab)].click()

    def _show_recent_recaps(self):
        self._close_imported_recaps()
        self._recap_context = None
        self._recap_read_errors = []
        self._recap_records = list(self._death_recap.deaths)
        self._recap_scope.setText(_("Recent deaths"))
        self._recap_recent.hide()
        self._recap_back.hide()
        self._recap_previous.hide()
        self._recap_next.hide()
        self._refresh_recap_list()
        self._update_recap_notice()

    def _browse_saved_recaps(self):
        dialog = SavedRecapDialog(self._prog_sessions.sessions, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._show_pull_recaps(*dialog.selection)
        dialog.deleteLater()

    def _step_recap_pull(self, direction):
        if self._recap_context is None:
            return
        session, pull, _number = self._recap_context
        index = next((i for i, candidate in enumerate(session["pulls"]) if candidate["id"] == pull["id"]), -1)
        target = index + direction
        if index >= 0 and 0 <= target < len(session["pulls"]):
            self._show_pull_recaps(session, session["pulls"][target], target + 1)

    def _open_recap_log(self):
        directory = self._settings.get("recap_log_directory", "")
        if not isinstance(directory, str):
            directory = ""
        path, _filter = QFileDialog.getOpenFileName(self, _("Open IINACT combat log"), directory,
                                                   _("Combat logs (*.log *.txt);;All files (*)"))
        if not path:
            return
        dialog = LogImportDialog(path, self)
        self._recap_import_dialog = dialog
        accepted = dialog.exec() == QDialog.DialogCode.Accepted
        self._recap_import_dialog = None
        if accepted:
            self._show_imported_recaps(dialog.result)
            dialog.result = None
            self._settings["recap_log_directory"] = str(Path(path).parent)
            self._save_settings_debounced()
        elif dialog.error:
            QMessageBox.warning(self, _("Open combat log"), _("Could not read this combat log: {error}").format(error=dialog.error))
        dialog.deleteLater()

    def _show_imported_recaps(self, imported):
        self._close_imported_recaps()
        self._recap_import = imported
        self._recap_context = None
        self._recap_read_errors = []
        self._recap_records = imported.records
        self._recap_scope.setText(_("Combat log: {name}").format(name=imported.path.name))
        self._recap_recent.show()
        self._recap_back.hide()
        self._recap_previous.hide()
        self._recap_next.hide()
        self._recap_log_pull.blockSignals(True)
        self._recap_log_pull.clear()
        self._recap_log_pull.addItem(_("All pulls"), None)
        for number in sorted({death["pull"] for death in imported.records}):
            self._recap_log_pull.addItem(_("Pull {number}").format(number=number) if number else _("Outside a pull"), number)
        self._recap_log_pull.blockSignals(False)
        self._recap_log_pull.show()
        self._refresh_recap_list()
        self._update_recap_notice()

    def _close_imported_recaps(self):
        if self._recap_import is not None:
            self._recap_import.close()
            self._recap_import = None
        self._recap_log_pull.hide()

    def _stop_recap_import(self):
        if self._recap_import_dialog is not None:
            self._recap_import_dialog.reject()
        self._close_imported_recaps()

    def _back_to_prog(self):
        if self._recap_context is not None:
            session, pull, _number = self._recap_context
            tab = self._prog_tab
            tab.flush()
            tab.picker.setCurrentIndex(tab.picker.findData(session["id"]))
            row = next((i for i, candidate in enumerate(reversed(session["pulls"]))
                        if candidate["id"] == pull["id"]), -1)
            if row >= 0:
                tab.table.selectRow(row)
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
            session, pull, number = self._recap_context
            self._recap_next.setEnabled(number < len(session["pulls"]))
            count = pull.get("recap_count")
            if count is None:
                messages.append(_("This pull was recorded before saved death recaps were available."))
            elif len(self._recap_records) < count:
                messages.append(_("Only {available} of {recorded} recorded death recaps are available.").format(
                    available=len(self._recap_records), recorded=count))
        if self._recap_import is not None:
            messages.append(_("Imported {count} deaths. Pulls are inferred from combat activity and wipe signals.").format(
                count=len(self._recap_records)))
            if self._recap_import.skipped:
                messages.append(_("Skipped {count} malformed log lines.").format(count=self._recap_import.skipped))
        self._recap_notice.setText("\n".join(messages))
        self._recap_notice.setVisible(bool(messages))

    def _filter_recap_player(self):
        self._refresh_recap_list(update_players=False)

    def _recap_options_changed(self):
        self._settings.update(recap_show_damage=self._recap_damage.isChecked(),
                              recap_show_healing=self._recap_healing.isChecked(),
                              recap_show_buffs=self._recap_buffs.isChecked(),
                              recap_show_debuffs=self._recap_debuffs.isChecked(),
                              recap_show_health=self._recap_health.isChecked())
        self._save_settings_debounced()
        self._select_recap(self._recap_list.currentRow())

    def _filter_recap_buffs(self):
        dialog = QDialog(self)
        dialog.setWindowTitle(_("Filter buffs"))
        dialog.resize(430, 530)
        layout = QVBoxLayout(dialog)
        label = QLabel(_("Check a status to hide it throughout the recap. Offensive buffs are hidden by default. Mitigation, healing effects, and debuffs stay visible. Saved combat data is kept."))
        label.setWordWrap(True)
        layout.addWidget(label)
        search = QLineEdit()
        search.setPlaceholderText(_("Search statuses"))
        layout.addWidget(search)
        listing = QListWidget()
        listing.setIconSize(self._recap_table.iconSize())
        layout.addWidget(listing)
        known = dict(self._recap_import.statuses) if self._recap_import is not None else {}
        for death in self._recap_records if self._recap_import is None else []:
            for status in death["statuses"] + [s for e in death["events"]
                                               for key in ("statuses", "source_statuses") for s in e.get(key, [])]:
                known[status_key(status)] = status
            for event in death["events"]:
                if event.get("status_id"):
                    status = {"id": event["status_id"], "name": event["name"]}
                    known.setdefault(status_key(status), status)
        for key in self._recap_hidden | DEFAULT_HIDDEN_STATUSES:
            metadata = self._recap_icons.catalog.get("Status", {}).get(key, {})
            known.setdefault(key, {"name": metadata.get("name", key.removeprefix("name:")),
                                   "id": int(key) if key.isdecimal() and len(key) <= 10 else None})
        for key, status in sorted(known.items(), key=lambda pair: pair[1]["name"].casefold()):
            metadata, pixmap = self._recap_icons.get("Status", status.get("id"))
            item = QListWidgetItem(QIcon(pixmap), status_name(status, metadata), listing)
            item.setData(Qt.ItemDataRole.UserRole, key)
            item.setData(Qt.ItemDataRole.UserRole + 1, status)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setToolTip(_("Check to hide") + f"\nID: {key}" +
                            ("\n" + metadata["description"] if metadata.get("description") else ""))
            item.setCheckState(Qt.CheckState.Checked if key in self._recap_hidden else Qt.CheckState.Unchecked)

        def filter_rows(text):
            for row in range(listing.count()):
                item = listing.item(row)
                item.setHidden(text.casefold() not in item.text().casefold())

        def refresh_icons():
            for row in range(listing.count()):
                item = listing.item(row)
                status = item.data(Qt.ItemDataRole.UserRole + 1)
                metadata, pixmap = self._recap_icons.get("Status", status.get("id"))
                item.setIcon(QIcon(pixmap))
                item.setText(status_name(status, metadata))
            filter_rows(search.text())

        search.textChanged.connect(filter_rows)
        self._recap_icons.changed.connect(refresh_icons)
        show_all = QPushButton(_("Show all statuses"))
        show_all.clicked.connect(lambda: [listing.item(row).setCheckState(Qt.CheckState.Unchecked)
                                          for row in range(listing.count())])
        defaults = QPushButton(_("Restore defaults"))
        defaults.clicked.connect(lambda: [listing.item(row).setCheckState(
            Qt.CheckState.Checked if listing.item(row).data(Qt.ItemDataRole.UserRole) in DEFAULT_HIDDEN_STATUSES
            else Qt.CheckState.Unchecked) for row in range(listing.count())])
        actions = QHBoxLayout()
        actions.addWidget(show_all)
        actions.addWidget(defaults)
        layout.addLayout(actions)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        result = dialog.exec()
        self._recap_icons.changed.disconnect(refresh_icons)
        if result == QDialog.DialogCode.Accepted:
            self._recap_hidden.clear()
            self._recap_hidden.update(listing.item(row).data(Qt.ItemDataRole.UserRole)
                                      for row in range(listing.count())
                                      if listing.item(row).checkState() == Qt.CheckState.Checked)
            self._settings["recap_hidden_statuses"] = sorted(self._recap_hidden)
            self._save_settings_debounced()
            self._select_recap(self._recap_list.currentRow())
        dialog.deleteLater()

    def _update_recap_ages(self):
        for row, death in enumerate(self._recap_visible):
            age = max(0, int(time.time() - death["when"]))
            text = (_("{count} seconds ago").format(count=age) if age < 60 else
                    _("{count} minutes ago").format(count=age // 60) if age < 3600 else
                    f"{datetime.fromtimestamp(death['when']):%m/%d %H:%M}")
            item = self._recap_list.item(row)
            item.setText(f"{text}  {death['name']}")
            item.setToolTip(f"{datetime.fromtimestamp(death['when']):%Y-%m-%d %H:%M:%S}\n{death['zone']}")

    def _refresh_recap_list(self, update_players=True):
        selected = self._recap_list.currentRow()
        selected_id = self._recap_list.currentItem().data(Qt.ItemDataRole.UserRole) if selected >= 0 else None
        actor = self._recap_player.currentData()
        if update_players:
            self._recap_player.blockSignals(True)
            self._recap_player.clear()
            self._recap_player.addItem(_("All players"), None)
            players = {death["actor"]: death["name"] for death in self._recap_records}
            for ident, name in sorted(players.items(), key=lambda pair: pair[1]):
                self._recap_player.addItem(name, ident)
            index = self._recap_player.findData(actor)
            self._recap_player.setCurrentIndex(max(0, index))
            self._recap_player.blockSignals(False)
            actor = self._recap_player.currentData()
        self._recap_visible = [d for d in self._recap_records if actor is None or d["actor"] == actor]
        if self._recap_import is not None and self._recap_log_pull.currentData() is not None:
            self._recap_visible = [d for d in self._recap_visible if d["pull"] == self._recap_log_pull.currentData()]
        self._recap_list.blockSignals(True)
        self._recap_list.clear()
        for death in self._recap_visible:
            item = QListWidgetItem("", self._recap_list)
            item.setData(Qt.ItemDataRole.UserRole, death["id"])
        self._update_recap_ages()
        row = next((i for i, d in enumerate(self._recap_visible) if d["id"] == selected_id), 0)
        self._recap_list.blockSignals(False)
        self._recap_list.setCurrentRow(row if self._recap_visible else -1)
        self._select_recap(self._recap_list.currentRow())

    def _select_recap(self, row):
        if not 0 <= row < len(self._recap_visible):
            self._recap_statuses.setText(_("No deaths match this selection.") if self._recap_import is not None else
                                        _("No death recaps recorded for this pull.") if self._recap_context else
                                        _("No deaths recorded yet."))
            self._recap_table.setRowCount(0)
            return
        death = self._recap_visible[row]
        if self._recap_import is not None:
            try:
                death = self._recap_import.read(death)
            except (OSError, ValueError) as exc:
                self._recap_statuses.setText(_("Could not read this death recap: {error}").format(error=exc))
                self._recap_table.setRowCount(0)
                return
        statuses = ", ".join(status_name(s, self._recap_icons.metadata("Status", s.get("id")))
                             for s in self._recap_delegate.statuses({"statuses": death["statuses"]})) or _("None observed")
        self._recap_statuses.setText(_("Statuses observed at death: {statuses}").format(statuses=statuses))
        labels = {"damage": _("Damage"), "heal": _("Healing"), "dot": _("DoT"),
                  "hot": _("Regen"), "gained": _("Status gained"), "lost": _("Status lost"),
                  "instant-death": _("Instant death"), "health": _("HP and shields")}
        def show_status(event):
            if event["kind"] not in ("gained", "lost"):
                return True
            metadata = self._recap_icons.metadata("Status", event.get("status_id"))
            if metadata.get("icon") == 0:
                return False
            category = metadata.get("category", 0)
            if category == 1:
                return self._recap_buffs.isChecked()
            if category == 2:
                return self._recap_debuffs.isChecked()
            return self._recap_buffs.isChecked() or self._recap_debuffs.isChecked()
        events = [event for event in reversed(death["events"])
                  if (self._recap_healing.isChecked() or event["kind"] not in ("heal", "hot"))
                  and (self._recap_damage.isChecked() or event["kind"] not in ("damage", "dot", "instant-death"))
                  and show_status(event)
                  and (self._recap_health.isChecked() or event["kind"] != "health")
                  and not (event["kind"] in ("gained", "lost") and
                           status_key({"id": event.get("status_id"), "name": event["name"]}) in self._recap_hidden)]
        self._recap_table.setRowCount(len(events))
        for row, event in enumerate(events):
            kind = event["kind"]
            amount = ""
            healing = kind in ("heal", "hot")
            if event["amount"] is not None:
                severity = "!!" if event.get("critical") and event.get("direct_hit") else "!" if event.get("critical") else ""
                amount = ("+" if healing else "-") + f"{event['amount']:,}" + severity
            elif kind in ("gained", "lost"):
                amount = "+" if kind == "gained" else "-"
                if kind == "gained" and event.get("status_duration") is not None:
                    amount += f"{event['status_duration']:g}s"
            name = labels[kind] if kind in ("health", "hot", "dot", "instant-death") else event["name"]
            if kind in ("hot", "dot", "gained", "lost") and event.get("status_id"):
                metadata = self._recap_icons.metadata("Status", event["status_id"])
                name = status_name({"name": event["name"] if kind in ("gained", "lost") else ""}, metadata) or name
            values = [f"{event['time']:.1f}s", amount, name, event["source"],
                      "" if event.get("hp") is not None and event.get("max_hp") else _("Not recorded"),
                      "" if "statuses" in event else _("Not recorded")]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setToolTip(value)
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, event)
                if column == 1:
                    item.setForeground(QColor("#b5ed76" if healing else "#45c4eb"))
                    item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                    item.setToolTip(f"{labels[kind]}: {amount}")
                    details = []
                    damage_types = {1: _("Slashing"), 2: _("Piercing"), 3: _("Blunt"), 4: _("Shot"),
                                    5: _("Magic"), 6: _("Breath"), 7: _("Physical"), 8: _("Limit Break")}
                    if event.get("damage_type") in damage_types:
                        details.append(damage_types[event["damage_type"]])
                    for key, label in (("critical", _("Critical")), ("direct_hit", _("Direct hit")),
                                       ("blocked", _("Blocked")), ("parried", _("Parried"))):
                        if event.get(key):
                            details.append(label)
                    if details:
                        item.setToolTip(item.toolTip() + "\n" + "\n".join(details))
                self._recap_table.setItem(row, column, item)
        self._recap_table.resizeRowsToContents()
        self._recap_table.scrollToTop()

    def _recap_metadata_changed(self):
        scrollbar = self._recap_table.verticalScrollBar()
        position = scrollbar.value()
        self._select_recap(self._recap_list.currentRow())
        scrollbar.setValue(position)
