import unittest
from unittest.mock import Mock, patch

from PyQt6.QtCore import QPoint, QRect, Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QStyle, QStyleOptionButton

from nyaatriggers import main_window as mw
from nyaatriggers.locale_util import set_locale
from tests import test_session_ui as fixture


class ConnectionLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture.SessionUiTests.setUpClass()

    def setUp(self):
        self.case = fixture.SessionUiTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.addCleanup(set_locale, "en")

    def test_connection_controls_fit_while_engine_status_appears_and_clears(self):
        load_settings = mw.MainWindow._load_settings
        for language in ("en", "ja"):
            def settings(window):
                load_settings(window)
                window._settings["ui_language"] = language
            with patch.object(mw.MainWindow, "_load_settings", settings):
                window = mw.MainWindow()
            self.addCleanup(window._prog_sessions.close)
            self.addCleanup(window.close)
            window.show()
            states = (
                ({}, 0),
                ({"triggevent": ("good", "ready"), "triggernometry": ("good", "ready")}, 0),
                ({"triggevent": ("bad", "Java runtime or triggevent-core.jar not found")}, 0),
                ({"triggevent": ("bad", "Failed to launch sidecar: " + "Engine diagnostic detail " * 20)}, 2),
                ({}, 2),
                ({"triggevent": ("unknown", "Off"), "triggernometry": ("unknown", "Off")}, 0),
                ({}, 0),
            )
            for width, height in ((900, 600), (1280, 800)):
                window.resize(width, height)
                for engine_states, failures in states:
                    with self.subTest(language=language, size=(width, height), status=engine_states, failures=failures):
                        window._engine_sidecar_state = engine_states
                        window._engine_chain_failure_count = failures
                        window._update_engine_status_label()
                        window._update_engine_chain_label()
                        self.case.app.processEvents()
                        self.assertEqual((window.width(), window.height()), (width, height))
                        controls = [window._url_edit, window._conn_btn, window._status_lbl]
                        controls += [control for control in (window._engine_status_lbl, window._engine_chain_lbl)
                                     if control.isVisible()]
                        rectangles = [QRect(control.mapTo(window, QPoint()), control.size()) for control in controls]
                        for index, (control, rectangle) in enumerate(zip(controls, rectangles)):
                            self.assertTrue(window.rect().contains(rectangle), control.objectName())
                            for other in rectangles[index + 1:]:
                                self.assertFalse(rectangle.intersects(other), (rectangle, other))
                            self.assertGreaterEqual(control.height(), control.fontMetrics().height())
                        for label in controls[2:]:
                            self.assertGreaterEqual(label.width(), label.fontMetrics().horizontalAdvance(label.text()))
                        option = QStyleOptionButton()
                        window._conn_btn.initStyleOption(option)
                        contents = window._conn_btn.style().subElementRect(
                            QStyle.SubElement.SE_PushButtonContents, option, window._conn_btn)
                        self.assertGreaterEqual(contents.width(), window._conn_btn.fontMetrics().horizontalAdvance(window._conn_btn.text()))
                        url_point = window._url_edit.mapTo(window, QPoint(window._url_edit.width() - 10,
                                                                         window._url_edit.height() // 2))
                        self.assertIs(window.childAt(url_point), window._url_edit)
                        window._url_edit.setFocus()
                        window._url_edit.selectAll()
                        QTest.keyClicks(window._url_edit, "ws://127.0.0.1:10501/layout-test")
                        self.assertEqual(window._url_edit.text(), "ws://127.0.0.1:10501/layout-test")
                        connect = Mock()
                        with patch.object(window._ws, "connect_to", connect):
                            QTest.mouseClick(window._conn_btn, Qt.MouseButton.LeftButton)
                        connect.assert_called_once_with(window._url_edit.text())
                        expected_status = failures or any(state in ("bad", "good") for state, _text in engine_states.values())
                        self.assertEqual(window._engine_status_bar.isVisible(), bool(expected_status))

    def test_recovered_engine_status_does_not_retain_an_old_error_tooltip(self):
        window = self.case.window
        window._engine_sidecar_state = {"triggevent": ("bad", "Java runtime or triggevent-core.jar not found")}
        window._update_engine_status_label()
        self.assertIn("not found", window._engine_status_lbl.toolTip())
        window._engine_sidecar_state = {"triggevent": ("good", "ready")}
        window._update_engine_status_label()
        self.assertEqual(window._engine_status_lbl.toolTip(), "")


if __name__ == "__main__":
    unittest.main()
