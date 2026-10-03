import unittest
from unittest.mock import patch

from PyQt6.QtCore import QEventLoop, QTimer

from nyaatriggers import updater
from nyaatriggers.timeline_parser import parse
from tests import test_session_ui as fixture


class TimelineShutdownTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture.SessionUiTests.setUpClass()

    def setUp(self):
        fixture.SessionUiTests.setUp(self)

    def check_cleanup(self, operation):
        timeline = self.window._timeline
        self.addCleanup(timeline.reset)
        timeline.load(parse('0.01 "Late cue"'))
        phases = []
        timeline.phase_update.connect(lambda *args: phases.append(args))
        timeline.start()
        self.assertTrue(timeline._timer.isActive())
        with patch.object(updater, "relaunch"):
            getattr(self.window, operation)()
        phases.clear()

        loop = QEventLoop()
        timer = QTimer()
        timer.setSingleShot(True)
        timer.timeout.connect(loop.quit)
        timer.start(100)
        loop.exec()

        self.assertEqual(phases, [])
        self.assertFalse(timeline.is_active())
        self.assertFalse(timeline._timer.isActive())

    def test_close_stops_timeline_without_a_socket_disconnect(self):
        self.check_cleanup("close")

    def test_update_restart_stops_timeline_without_a_socket_disconnect(self):
        self.check_cleanup("_restart_for_update")

    def test_windows_handoff_stops_timeline_without_a_socket_disconnect(self):
        self.check_cleanup("_quit_for_windows_handoff")


if __name__ == "__main__":
    unittest.main()
