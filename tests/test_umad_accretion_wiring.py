from contextlib import ExitStack
from datetime import datetime
import json
from pathlib import Path
import queue
import unittest
from unittest.mock import patch

from PyQt6 import sip
from PyQt6.QtCore import QCoreApplication, QEvent

from nyaatriggers import app_common as ac
from nyaatriggers.telesto_client import TelestoClient
from nyaatriggers.trigger_engine import Trigger
from nyaatriggers.triggevent_bridge import TriggeventBridge
from nyaatriggers.umad_chains import ACCRETION, CRUST
from tests import test_session_ui as fixture


ACTORS = tuple(f"10FF{index:04X}" for index in range(1, 9))
FIRST, SECOND = ACTORS[:2]
SEQUENCE = Path(__file__).parent / "fixtures" / "umad_accretion_sequence.log"


class AccretionWiringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture.SessionUiTests.setUpClass()

    def setUp(self):
        from tests.test_automarker_pipeline import Endpoint

        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(TelestoClient, "start"))
        self.stack.enter_context(patch.object(TriggeventBridge, "is_available", return_value=False))
        self.endpoint = self.stack.enter_context(Endpoint(actors=ACTORS))
        self.case = fixture.SessionUiTests()
        self.case.setUp()
        self.window = self.case.window
        self.extra_windows = []
        self.addCleanup(self.dispose)
        self.clock = self.case.clock
        self.stack.enter_context(patch("time.monotonic", side_effect=self.clock))
        self.window._settings["telesto_uri"] = self.endpoint.url
        self.window._telesto_client.configure(delay_base_ms=0, delay_plus_ms=0)
        self.window._current_fight_tag = "UMAD"
        self.window._automark_cb.setChecked(True)
        self.window._umad_accretion_cb.setChecked(True)
        self.roster()
        self.deliver()

    def dispose(self):
        for window in self.extra_windows:
            window._prog_sessions.close()
            window.close()
            window.deleteLater()
        self.case.doCleanups()
        self.window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.assertTrue(sip.isdeleted(self.window))
        self.assertTrue(all(sip.isdeleted(window) for window in self.extra_windows))

    def roster(self, actors=ACTORS):
        body = json.dumps({"response": [
            {"actor": actor, "order": f"{slot:X}"}
            for slot, actor in enumerate(actors, 1)]}).encode()
        self.window._telesto_client._update_party_slots(body)

    def deliver(self):
        first = len(self.endpoint.commands)
        client = self.window._telesto_client
        while True:
            try:
                item = client._queue.get_nowait()
            except queue.Empty:
                break
            try:
                client._send_queued(item, client._stopping)
            finally:
                client._finish_delivery(item)
                client._queue.task_done()
        return self.endpoint.commands[first:]

    def status(self, effect, actor, *, lost=False):
        self.case.line(["30" if lost else "26", "status", effect, "Status",
                        "0" if lost else "120", "40000001", "Chaos", actor,
                        f"Player {actor}"])

    def assign(self):
        for effect, actor in (("BBD", SECOND), (ACCRETION, FIRST),
                              (CRUST, FIRST), ("BBC", FIRST),
                              (ACCRETION, SECOND), (CRUST, SECOND)):
            self.status(effect, actor)

    def new_window(self, preferences):
        def settings(window):
            window._settings.update(tts_engine="system", auto_connect=False,
                                    auto_check_updates=False, local_enabled=False,
                                    ui_language="en", **preferences)
        with patch.object(fixture.mw.MainWindow, "_load_settings", settings):
            window = fixture.mw.MainWindow()
        self.extra_windows.append(window)
        return window

    def test_primary_sequence_dispatches_only_crust_completion_and_ignores_duplicates(self):
        initial = self.clock.value
        origin = None
        expected = []
        for raw in SEQUENCE.read_text().splitlines():
            if not raw or raw.startswith("#"):
                continue
            fields = raw.split("|")
            timestamp = datetime.fromisoformat(fields[1])
            origin = origin or timestamp
            self.clock.value = initial + (timestamp - origin).total_seconds()
            self.window._on_log_line(raw)
            if fields[0] == "26" and fields[2] == ACCRETION and fields[7] == FIRST:
                expected.append("/mk ignore1 <1>")
            elif fields[0] == "30" and fields[2] == CRUST:
                expected.extend(["/mk clear <1>", "/mk ignore2 <2>"] if fields[7] == FIRST
                                else ["/mk clear <2>"])
            self.deliver()
            self.assertEqual(self.endpoint.commands, expected, raw)
            self.window._on_log_line(raw)
            self.assertEqual(self.deliver(), [], raw)
        self.assertEqual(expected, ["/mk ignore1 <1>", "/mk clear <1>",
                                    "/mk ignore2 <2>", "/mk clear <2>"])
        self.assertEqual(self.window._umad_accretion.outstanding(), [])

    def test_delayed_roster_handoff_never_revives_the_completed_first_carrier(self):
        self.roster(())
        self.assign()
        self.assertEqual(self.deliver(), [])
        self.assertEqual(self.window._umad_accretion_pending, [("mark", FIRST, "ignore1")])
        self.status(ACCRETION, FIRST, lost=True)
        self.status(ACCRETION, SECOND, lost=True)
        self.clock.value += 30
        self.status(CRUST, FIRST, lost=True)
        self.assertEqual(self.window._umad_accretion_pending, [("mark", SECOND, "ignore2")])
        self.roster()
        self.window._retry_umad_accretion_pending()
        self.assertEqual(self.deliver(), ["/mk ignore2 <2>"])
        self.status(CRUST, FIRST, lost=True)
        self.assertEqual(self.deliver(), [])
        self.status(CRUST, SECOND, lost=True)
        self.assertEqual(self.deliver(), ["/mk clear <2>"])

    def test_completed_queued_mark_is_cancelled_before_http_dispatch(self):
        self.assign()
        first = self.window._automark_deliveries[FIRST][-1].delivery
        self.status(CRUST, FIRST, lost=True)
        self.assertTrue(first.cancelled)
        self.assertEqual(self.deliver(), ["/mk ignore2 <2>"])

    def test_pending_roster_request_expires_without_refreshing_its_admission_time(self):
        self.roster(())
        self.assign()
        for elapsed in (10, 20, 30.1):
            self.clock.value = 1000 + elapsed
            self.window._retry_umad_accretion_pending()
        self.assertEqual(self.window._umad_accretion_pending, [])
        self.roster()
        self.window._retry_umad_accretion_pending()
        self.assertEqual(self.deliver(), [])

    def test_disable_cancels_pending_marks_and_clears_attempted_marks(self):
        for master in (False, True):
            for delivered in (False, True):
                with self.subTest(master=master, delivered=delivered):
                    self.window._automark_cb.setChecked(True)
                    self.window._umad_accretion_cb.setChecked(True)
                    self.assign()
                    if delivered:
                        self.assertEqual(self.deliver(), ["/mk ignore1 <1>"])
                    checkbox = self.window._automark_cb if master else self.window._umad_accretion_cb
                    checkbox.setChecked(False)
                    self.assertEqual(self.deliver(), ["/mk clear <1>"] if delivered else [])
                    self.status(CRUST, FIRST, lost=True)
                    self.window._retry_umad_accretion_pending()
                    self.assertEqual(self.deliver(), [])
                    self.assertEqual(self.window._umad_accretion.outstanding(), [])
                    self.assertEqual(self.window._umad_accretion_pending, [])

    def test_wipe_clears_current_carrier_and_discards_roster_retries(self):
        for roster in (False, True):
            with self.subTest(roster=roster):
                self.roster(ACTORS if roster else ())
                self.assign()
                self.deliver()
                self.case.line(["33", "wipe", "80000000", "4000000F"])
                self.roster()
                self.window._retry_umad_accretion_pending()
                self.assertEqual(self.deliver(), ["/mk clear <1>"] if roster else [])
                self.assertEqual(self.window._umad_accretion.outstanding(), [])
                self.assertEqual(self.window._umad_accretion_pending, [])

    def test_other_duty_and_feed_loss_discard_old_assignments(self):
        self.window._current_fight_tag = "Other"
        self.assign()
        self.assertEqual(self.deliver(), [])
        self.window._current_fight_tag = "UMAD"
        self.roster(())
        self.assign()
        self.window._triggers = [Trigger(fight="Other", zone_regex="Other duty")]
        self.window._on_ws_zone_changed(1, "Other duty")
        self.roster()
        self.window._retry_umad_accretion_pending()
        self.assertEqual(self.deliver(), [])
        self.assertEqual(self.window._umad_accretion.outstanding(), [])
        self.window._current_fight_tag = "UMAD"
        self.assign()
        self.window._on_status_changed(False, "Disconnected")
        self.roster()
        self.window._retry_umad_accretion_pending()
        self.assertEqual(self.deliver(), [])
        self.assertEqual(self.window._umad_accretion_pending, [])

    def test_clear_all_cannot_restore_pending_accretion_after_roster_arrives(self):
        self.roster(())
        self.assign()
        self.window._on_automark_clear_all()
        self.assertEqual(self.deliver(), [f"/mk clear <{slot}>" for slot in range(1, 9)])
        self.roster()
        self.window._retry_umad_accretion_pending()
        self.status(CRUST, FIRST, lost=True)
        self.assertEqual(self.deliver(), [])
        self.assertEqual(self.window._umad_accretion.outstanding(), [])
        self.assertEqual(self.window._umad_accretion_pending, [])

    def test_saved_legacy_rules_stay_dormant_and_do_not_claim_native_p4_ownership(self):
        legacy = [{"fight": "UMAD", "status": "15A7", "marker": "triangle", "scope": "party"}]
        window = self.new_window({"automark_rules": legacy, "umad_chain_enabled": True})
        self.assertEqual(window._settings["automark_rules"], legacy)
        self.assertEqual(window._automark_rules, [])
        self.assertTrue(window._umad_accretion_cb.isChecked())
        self.assertEqual({key: combo.currentData() for key, combo in window._umad_accretion_combos.items()},
                         {"first": "ignore1", "second": "ignore2"})
        self.assertFalse(window._native_umad_owned_locally())
        self.assertFalse(hasattr(window, "_automark_rules_list"))

    def test_explicit_accretion_preferences_override_legacy_toggle_and_keep_role_controls_separate(self):
        window = self.new_window({"umad_chain_enabled": True, "umad_accretion_enabled": False,
                                  "umad_chain_marker_accretion": "attack3",
                                  "umad_accretion_marker_first": "bind1",
                                  "umad_accretion_marker_second": "bind2"})
        self.assertTrue(window._umad_chain_cb.isChecked())
        self.assertFalse(window._umad_accretion_cb.isChecked())
        self.assertEqual(set(window._umad_chain_combos), {"dps", "support"})
        self.assertEqual({key: combo.currentData() for key, combo in window._umad_accretion_combos.items()},
                         {"first": "bind1", "second": "bind2"})

    def test_shutdown_stops_the_accretion_flush_timer(self):
        self.assign()
        self.assertTrue(self.window._umad_accretion_flush_timer.isActive())
        self.window._stop_background_timers()
        self.assertFalse(self.window._umad_accretion_flush_timer.isActive())


if __name__ == "__main__":
    unittest.main()
