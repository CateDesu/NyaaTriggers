import json
import threading
import unittest
from unittest.mock import patch

from PyQt6.QtCore import QCoreApplication, QEvent

from nyaatriggers import app_common as ac
from nyaatriggers.telesto_client import TelestoClient
from nyaatriggers.trigger_engine import Trigger
from tests import test_session_ui as fixture
from tests.test_session_features import BOSS


class DpsRecordingRecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture.SessionUiTests.setUpClass()

    def setUp(self):
        self.case = fixture.SessionUiTests()
        self.addCleanup(self.cleanup_case)
        with patch.object(TelestoClient, "start"):
            self.case.setUp()
        self.window = self.case.window
        self.case.connect()

    def cleanup_case(self):
        self.case.doCleanups()
        window = getattr(self.case, "window", None)
        if window is not None:
            window.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def finish_pull(self):
        self.window._on_in_combat(True, True)
        self.case.line(fixture.ability(source=fixture.PLAYER, target=BOSS))
        self.case.clock.value += 2
        self.window._on_in_combat(False, False)

    def test_encounters_record_automatically_with_absent_or_previously_disabled_setting(self):
        for choice in (None, False):
            with self.subTest(previous_choice=choice):
                if choice is not None:
                    self.window._settings["dps_enabled"] = choice
                self.finish_pull()
        for worker in self.window._dps_write_threads:
            worker.join(timeout=5)
            self.assertFalse(worker.is_alive())
        paths = list((self.case.temp / "dps_logs").glob("*.jsonl"))
        self.assertEqual(len(paths), 1)
        saved = [json.loads(line) for line in paths[0].read_text().splitlines()]
        self.assertEqual(len(saved), 2)
        self.assertEqual([row["combatants"][0]["damage"] for row in saved], [1000, 1000])
        self.assertEqual(len(self.window._prog_sessions.current["pulls"]), 2)

    def test_failed_writer_start_keeps_history_and_the_next_pull_can_record(self):
        trigger = Trigger(id="custom", enabled=False)
        self.window._triggers = [trigger]
        trigger._last_fired["boss"] = self.case.clock()
        with patch.object(threading.Thread, "start", side_effect=RuntimeError("Cannot start writer")), \
                patch.object(ac, "log_drop") as dropped:
            self.finish_pull()
        self.assertEqual(len(self.window._dps_history), 1)
        self.assertEqual(self.window._dps_history[0]["snapshot"]["Encounter"]["damage"], 1000)
        self.assertEqual(trigger._last_fired, {})
        self.assertEqual(self.window._dps_write_threads, [])
        self.assertEqual(len(self.window._prog_sessions.current["pulls"]), 1)
        self.assertTrue(self.window._prog_sessions.current["pulls"][0]["complete"])
        self.assertTrue(any(args[0] == "dps-snapshot" and "Cannot start writer" in args[1]
                            for args, _kwargs in dropped.call_args_list))
        self.finish_pull()
        for worker in self.window._dps_write_threads:
            worker.join(timeout=5)
            self.assertFalse(worker.is_alive())
        self.assertEqual(len(self.window._dps_history), 2)
        self.assertEqual(len(self.window._prog_sessions.current["pulls"]), 2)
        paths = list((self.case.temp / "dps_logs").glob("*.jsonl"))
        self.assertEqual(len(paths), 1)
        saved = [json.loads(line) for line in paths[0].read_text().splitlines()]
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]["combatants"][0]["damage"], 1000)


if __name__ == "__main__":
    unittest.main()
