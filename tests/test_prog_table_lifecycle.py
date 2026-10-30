import gc
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
import weakref

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QEvent
from PyQt6.QtTest import QSignalSpy
from PyQt6.QtWidgets import QApplication, QStackedWidget, QWidget

from nyaatriggers import theme
from nyaatriggers.prog_session import ProgSessions
from nyaatriggers.record_store import write_record
from nyaatriggers.ui.prog_tab import ProgTab
from tests.test_prog_comparison import pull, session


class ProgTableLifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setStyleSheet(theme.STYLESHEET)

    def setUp(self):
        self.directory = TemporaryDirectory()
        path = Path(self.directory.name)
        write_record(path, session([pull("p3")], started=2))
        self.sessions = ProgSessions(path)
        self.window = SimpleNamespace(
            _update_recap_notice=lambda: None, _recap_save_warning=lambda: "",
            _dps_meter=SimpleNamespace(full_snapshot=lambda: None))
        self.stack = QStackedWidget()
        self.stack.addWidget(QWidget())
        self.stack.resize(900, 600)

    def tearDown(self):
        self.sessions.close()
        self.stack.close()
        self.stack.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.directory.cleanup()

    def test_removed_tab_survives_pending_geometry_until_deferred_deletion(self):
        tab = ProgTab(self.window, self.sessions)
        self.stack.addWidget(tab)
        tab.phase_table.setColumnWidth(1, 1500)
        tab.milestone_table.setColumnWidth(0, 1500)
        destroyed = QSignalSpy(tab.destroyed)
        self.stack.removeWidget(tab)
        tab.deleteLater()
        ref = weakref.ref(tab)
        del tab
        gc.collect()
        self.assertIsNone(ref())
        self.assertEqual(len(destroyed), 0)
        self.app.processEvents()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.assertEqual(len(destroyed), 1)

    def test_both_tables_resize_when_the_horizontal_range_changes(self):
        tab = ProgTab(self.window, self.sessions)
        self.stack.addWidget(tab)
        for table in (tab.phase_table, tab.milestone_table):
            with self.subTest(columns=table.columnCount()):
                bar = table.horizontalScrollBar()
                bar.setRange(0, 0)
                bar.setRange(0, 10)
                expanded = table.height()
                bar.setRange(0, 0)
                self.assertEqual(expanded - table.height(), bar.sizeHint().height())
                self.assertEqual(table.minimumHeight(), table.maximumHeight())


if __name__ == "__main__":
    unittest.main()
