"""Saved pull selection and cancellable combat log imports."""

from datetime import datetime

from PyQt6.QtCore import QTimer, Qt
from PyQt6.QtWidgets import (QApplication, QComboBox, QDialog, QDialogButtonBox, QLabel,
                             QListWidget, QListWidgetItem, QProgressBar, QPushButton, QVBoxLayout)

from nyaatriggers.locale_util import _
from nyaatriggers.recap_log import LogImportJob


class SavedRecapDialog(QDialog):
    def __init__(self, sessions, parent):
        super().__init__(parent)
        self.setWindowTitle(_("Saved death recaps"))
        self.resize(620, 420)
        self.sessions = list(sessions)
        self.selection = None
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(_("Choose a session and pull.")))
        self.session = QComboBox()
        for session in self.sessions:
            self.session.addItem(f"{datetime.fromtimestamp(session['started']):%Y-%m-%d %H:%M}  "
                                 f"{session['name']} · {session['zone']}")
        layout.addWidget(self.session)
        self.pulls = QListWidget()
        layout.addWidget(self.pulls)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Open | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        self.pulls.itemDoubleClicked.connect(lambda _item: self.accept())
        self.pulls.currentRowChanged.connect(self._select)
        self.session.currentIndexChanged.connect(self._refresh)
        layout.addWidget(self.buttons)
        self._refresh()

    def _refresh(self):
        self.pulls.clear()
        index = self.session.currentIndex()
        if 0 <= index < len(self.sessions):
            session = self.sessions[index]
            for number, pull in reversed(list(enumerate(session["pulls"], 1))):
                count = pull.get("recap_count")
                text = _("Pull {number} · {time} · {count} deaths").format(
                    number=number, time=f"{datetime.fromtimestamp(pull['started']):%H:%M:%S}",
                    count=count if count is not None else "?")
                item = QListWidgetItem(text, self.pulls)
                item.setData(Qt.ItemDataRole.UserRole, number - 1)
            self.pulls.setCurrentRow(0)
        self._select()

    def _select(self):
        item = self.pulls.currentItem()
        self.selection = None
        if item is not None:
            session = self.sessions[self.session.currentIndex()]
            index = item.data(Qt.ItemDataRole.UserRole)
            self.selection = session, session["pulls"][index], index + 1
        self.buttons.button(QDialogButtonBox.StandardButton.Open).setEnabled(self.selection is not None)

    def accept(self):
        if self.selection is not None:
            super().accept()


class LogImportDialog(QDialog):
    def __init__(self, path, parent):
        super().__init__(parent)
        self.setWindowTitle(_("Open combat log"))
        self.resize(430, 140)
        self.job = LogImportJob(path)
        self.result = None
        self.error = ""
        layout = QVBoxLayout(self)
        self.label = QLabel(_("Reading combat log…"))
        layout.addWidget(self.label)
        self.progress = QProgressBar()
        layout.addWidget(self.progress)
        self.cancel = QPushButton(_("Cancel"))
        self.cancel.clicked.connect(self.reject)
        layout.addWidget(self.cancel)
        self.timer = QTimer(self)
        self.timer.setInterval(100)
        self.timer.timeout.connect(self._poll)
        self.timer.start()
        QApplication.instance().aboutToQuit.connect(self.job.cancel.set)
        self.job.start()

    def reject(self):
        self.job.cancel.set()
        self.label.setText(_("Cancelling…"))
        self.cancel.setEnabled(False)

    def _poll(self):
        self.progress.setValue(self.job.progress)
        if not self.job.done.is_set():
            return
        self.timer.stop()
        QApplication.instance().aboutToQuit.disconnect(self.job.cancel.set)
        self.result, self.job.result = self.job.result, None
        self.error = self.job.error
        if self.job.cancel.is_set() or self.result is None:
            if self.result is not None:
                self.result.close()
                self.result = None
            super().reject()
        else:
            super().accept()
