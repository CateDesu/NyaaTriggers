from contextlib import ExitStack
import json
import queue
import unittest
from unittest.mock import patch

from nyaatriggers import app_common as ac
from nyaatriggers.telesto_client import TelestoClient
from nyaatriggers.trigger_engine import Trigger
from nyaatriggers.triggevent_bridge import TriggeventBridge
from nyaatriggers.ui.settings_tab import SettingsTabMixin
from nyaatriggers.umad_chains import (
    ACCELERATION_BOMB, CURSED_SHRIEK, FAKE_GAZE_VFX, FORKED_LIGHTNING,
    GAZE_VFX_STATUS, REAL_GAZE_VFX, StatusPairs,
)
from tests import test_session_ui as fixture


ACTORS = tuple(f"10FF{index:04X}" for index in range(1, 9))
MECHANICS = {"bomb": (ACCELERATION_BOMB, 4), "lightning": (FORKED_LIGHTNING, 2)}
MARKERS = {
    "bomb": {f"{kind}{index}": f"attack{offset + index}"
             for kind, offset in (("real", 0), ("fake", 4)) for index in range(1, 5)},
    "lightning": {"real1": "bind1", "real2": "bind2",
                  "fake1": "ignore1", "fake2": "ignore2"},
}


class GrandCrossWiringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture.SessionUiTests.setUpClass()

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(TelestoClient, "start"))
        self.stack.enter_context(patch.object(TriggeventBridge, "is_available", return_value=False))
        self.case = fixture.SessionUiTests()
        self.addCleanup(self.case.doCleanups)
        self.case.setUp()
        self.window = self.case.window
        self.clock = self.case.clock
        self.stack.enter_context(patch("time.monotonic", side_effect=self.clock))
        self.window._telesto_client.configure(delay_base_ms=0, delay_plus_ms=0)
        self.sent_commands = []
        self.stack.enter_context(patch.object(self.window._telesto_client, "_read_response",
                                              side_effect=self.accept_request))
        self.window._current_fight_tag = "UMAD"
        self.window._automark_cb.setChecked(True)
        for name, markers in MARKERS.items():
            for key, marker in markers.items():
                combo = self.window._umad_pair_combos[name][key]
                combo.setCurrentIndex(combo.findData(marker))
        self.roster()
        self.deliver()

    def roster(self, actors=ACTORS):
        body = json.dumps([{"actor": actor, "order": f"{slot:X}"}
                           for slot, actor in enumerate(actors, 1)]).encode()
        self.window._telesto_client._update_party_slots(body)

    def accept_request(self, request, timeout):
        message = json.loads(request.data)
        if message["type"] == "ExecuteCommand":
            self.sent_commands.append(message["payload"]["command"])
        return 200, b"{}"

    def deliver(self):
        """Dispatch the isolated queue through a simulated successful HTTP response."""
        first = len(self.sent_commands)
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
        return self.sent_commands[first:]

    def tell(self, vfx=REAL_GAZE_VFX):
        self.case.line(["26", f"tell-{self.clock.value}-{vfx}", GAZE_VFX_STATUS,
                        "VFX", "9999", "E0000000", "", "40000001", "Neo Exdeath", vfx])

    def status(self, effect, actor, duration=30, lost=False):
        self.case.line(["30" if lost else "26", "status", effect, "Status", str(duration),
                        "40000001", "Neo Exdeath", actor, f"Player {actor}"])

    def wave(self, name, actors=None, durations=None, tell=REAL_GAZE_VFX):
        effect, count = MECHANICS[name]
        actors = actors or ACTORS[:count]
        durations = durations or [30] * count
        self.window._umad_pair_checkboxes[name].setChecked(True)
        self.tell(tell)
        for actor, duration in zip(actors, durations):
            self.status(effect, actor, duration)
        return actors

    def reset(self, kind):
        if kind == "wipe":
            self.case.line(["33", "wipe", "80000000", "4000000F"])
        elif kind == "Kefka Says":
            self.case.line(["20", "cast", "40000001", "Neo Exdeath", "C2DC", "Kefka Says"])
        elif kind == "zone":
            self.window._on_ws_zone_changed(1, "Other duty")
        elif kind == "disable":
            self.window._automark_cb.setChecked(False)

    def test_real_and_fake_tells_reach_both_mechanics_through_log_dispatch(self):
        for name, (effect, count) in MECHANICS.items():
            for vfx, polarity in ((REAL_GAZE_VFX, "real"), (FAKE_GAZE_VFX, "fake")):
                with self.subTest(name=name, polarity=polarity):
                    self.window._umad_grand_cross_reset(clear_marks=True)
                    self.deliver()
                    self.window._umad_pair_checkboxes[name].setChecked(True)
                    self.tell(vfx)
                    actors = tuple(reversed(ACTORS[:count]))
                    durations = (60, 30, 60, 30) if count == 4 else (10, 60)
                    for actor, duration in zip(actors[:-1], durations[:-1]):
                        self.status(effect, actor, duration)
                        self.assertEqual(self.deliver(), [])
                    self.status(effect, actors[-1], durations[-1])
                    ordered = (ACTORS[0], ACTORS[2], ACTORS[1], ACTORS[3]) if count == 4 else ACTORS[:count]
                    self.assertEqual(self.deliver(), [
                        f"/mk {MARKERS[name][f'{polarity}{index}']} <{ACTORS.index(actor) + 1}>"
                        for index, actor in enumerate(ordered, 1)])
                    self.assertEqual(set(self.window._umad_grand_cross[name].outstanding()), set(actors))
                    for actor in actors:
                        self.status(effect, actor, 30)
                    self.assertEqual(self.deliver(), [])

    def test_marker_choices_and_enabled_state_survive_settings_reload(self):
        for checkbox in self.window._umad_pair_checkboxes.values():
            checkbox.setChecked(True)
        combo = self.window._umad_pair_combos["bomb"]["fake4"]
        combo.setCurrentIndex(combo.findData(""))
        saved = json.loads(ac._SETTINGS_FILE.read_text())
        for name, markers in MARKERS.items():
            self.assertTrue(saved[f"umad_{name}_enabled"])
            for key, marker in markers.items():
                expected = "" if (name, key) == ("bomb", "fake4") else marker
                self.assertEqual(saved[f"umad_{name}_marker_{key}"], expected)
        with patch.object(fixture.mw.MainWindow, "_load_settings", SettingsTabMixin._load_settings):
            restored = fixture.mw.MainWindow()
        self.addCleanup(restored._prog_sessions.close)
        self.addCleanup(restored.close)
        for name in MECHANICS:
            self.assertTrue(restored._umad_pair_checkboxes[name].isChecked())
            for key, combo in restored._umad_pair_combos[name].items():
                self.assertEqual(combo.currentData() or "", saved[f"umad_{name}_marker_{key}"])

    def test_either_local_controller_blocks_native_umad_until_disabled(self):
        bridge = self.window._ensure_triggevent_bridge()
        self.window._settings["native_umad_enabled"] = True
        self.window._automark_rules = []
        with patch.object(bridge, "set_automark") as configure:
            for name in MECHANICS:
                with self.subTest(name=name):
                    checkbox = self.window._umad_pair_checkboxes[name]
                    checkbox.setChecked(True)
                    self.assertTrue(self.window._native_umad_owned_locally())
                    self.assertFalse(configure.call_args.kwargs["native_umad"])
                    self.assertTrue(self.window._settings["native_umad_enabled"])
                    checkbox.setChecked(False)
                    self.assertFalse(self.window._native_umad_owned_locally())
                    self.assertTrue(configure.call_args.kwargs["native_umad"])

    def test_local_controllers_suspend_raw_gain_and_retry_rules(self):
        for name, (effect, count) in MECHANICS.items():
            with self.subTest(name=name):
                self.window._clear_actor_state()
                self.roster(())
                self.window._umad_pair_checkboxes[name].setChecked(False)
                self.window._automark_rules = [{"fight": "UMAD", "status": effect,
                                               "marker": "circle", "scope": "party"}]
                self.status(effect, ACTORS[0])
                self.assertEqual(len(self.window._automark_pending), 1)
                self.window._umad_pair_checkboxes[name].setChecked(True)
                self.roster()
                self.window._retry_automark_pending()
                self.assertEqual(self.deliver(), [])
                self.assertEqual(self.window._automark_pending, [])
                self.wave(name)
                self.assertEqual(self.deliver(), [
                    f"/mk {MARKERS[name][f'real{index}']} <{index}>" for index in range(1, count + 1)])
                self.assertTrue(all(owner[0] == f"umad_{name}"
                                    for owner in self.window._automark_owners.values()))
                self.window._umad_pair_checkboxes[name].setChecked(False)
                self.deliver()
                self.status(effect, ACTORS[0])
                self.assertEqual(self.deliver(), ["/mk circle <1>"])

    def test_status_loss_and_expiry_clear_only_the_matching_carriers(self):
        for name, (effect, count) in MECHANICS.items():
            with self.subTest(name=name):
                self.window._umad_grand_cross_reset(clear_marks=True)
                self.deliver()
                actors = self.wave(name, durations=[3] * count)
                self.deliver()
                self.status("FFFF", actors[0], lost=True)
                self.assertEqual(self.deliver(), [])
                self.status(effect, actors[0], lost=True)
                self.assertEqual(self.deliver(), ["/mk clear <1>"])
                self.clock.value += 3
                self.window._on_umad_grand_cross_flush()
                self.assertEqual(self.deliver(), [f"/mk clear <{index}>" for index in range(2, count + 1)])
                self.assertEqual(self.window._umad_grand_cross[name].outstanding(), [])
                self.assertFalse(self.window._umad_grand_cross[name].needs_flush())

    def test_loss_and_expiry_cancel_retries_before_the_roster_arrives(self):
        for name, (effect, count) in MECHANICS.items():
            for resolution in ("loss", "expiry"):
                with self.subTest(name=name, resolution=resolution):
                    self.window._umad_grand_cross_reset(clear_marks=True)
                    self.deliver()
                    self.roster(())
                    actors = self.wave(name, durations=[3] * count)
                    self.assertEqual(len(self.window._umad_grand_cross_pending[name]), count)
                    self.assertEqual(self.deliver(), [])
                    if resolution == "loss":
                        for actor in actors:
                            self.status(effect, actor, lost=True)
                    else:
                        self.clock.value += 3
                        self.window._on_umad_grand_cross_flush()
                    self.assertEqual(self.window._umad_grand_cross_pending[name], [])
                    self.roster()
                    self.window._retry_umad_grand_cross_pending()
                    self.assertEqual(self.deliver(), [])
                    self.assertEqual(self.window._umad_grand_cross[name].outstanding(), [])

    def test_queued_marks_respect_duty_metadata_received_after_reconnect(self):
        self.window._triggers = [Trigger(fight="Other", zone_regex="Other duty"),
                                 Trigger(fight="UMAD", zone_regex="UMAD")]
        for name, (_effect, count) in MECHANICS.items():
            with self.subTest(name=name):
                self.window._on_status_changed(False, "Disconnected")
                self.window._on_status_changed(True, "Connected")
                self.wave(name)
                self.assertEqual(len(self.window._umad_grand_cross_pending[name]), count)
                self.window._on_ws_zone_changed(1, "Other duty")
                self.assertEqual(self.window._current_fight_tag, "Other")
                self.roster()
                self.window._retry_umad_grand_cross_pending()
                self.assertEqual(self.deliver(), [])
                self.assertEqual(self.window._umad_grand_cross_pending[name], [])
                self.assertEqual(self.window._umad_grand_cross[name].outstanding(), [])

    def test_background_shutdown_stops_the_grand_cross_flush_timer(self):
        self.wave("lightning")
        self.assertTrue(self.window._umad_grand_cross_flush_timer.isActive())
        self.window._stop_background_timers()
        self.assertFalse(self.window._umad_grand_cross_flush_timer.isActive())

    def test_gaze_queued_marks_expire_before_the_next_timer_flush(self):
        self.window._umad_gaze_cb.setChecked(True)
        self.tell()
        for actor in ACTORS[:2]:
            self.status(CURSED_SHRIEK, actor, duration=3)
        self.assertEqual(set(self.window._automark_deliveries), set(ACTORS[:2]))
        self.clock.value += 3.1
        self.assertEqual(self.deliver(), [])
        self.window._on_umad_gaze_flush()
        self.assertEqual(self.deliver(), [])
        self.assertEqual(self.window._umad_gaze.outstanding(), [])

    def test_plain_rule_deadline_survives_delayed_roster_and_queued_delivery(self):
        self.window._automark_rules = [{"fight": "UMAD", "status": "D00D",
                                       "marker": "circle", "scope": "party"}]
        for boundary in ("queued", "roster before expiry", "roster after expiry"):
            with self.subTest(boundary=boundary):
                self.window._clear_actor_state()
                self.roster(ACTORS if boundary == "queued" else ())
                self.status("D00D", ACTORS[0], duration=3)
                if boundary == "queued":
                    self.assertIn(ACTORS[0], self.window._automark_deliveries)
                else:
                    self.assertEqual(len(self.window._automark_pending), 1)
                if boundary == "roster before expiry":
                    self.clock.value += 2
                    self.roster()
                    self.window._retry_automark_pending()
                    self.assertIn(ACTORS[0], self.window._automark_deliveries)
                    self.clock.value += 1.1
                else:
                    self.clock.value += 3.1
                    if boundary == "roster after expiry":
                        self.roster()
                        self.window._retry_automark_pending()
                self.assertEqual(self.window._automark_pending, [])
                self.assertEqual(self.deliver(), [])

    def test_compound_rule_deadline_uses_the_first_constituent_expiry(self):
        self.window._automark_rules = [{"fight": "UMAD", "status": "D00D+D00E",
                                       "marker": "circle", "scope": "party"}]
        self.window._automark_pairs = StatusPairs(("D00D", "D00E"))
        for boundary in ("queued", "roster after expiry", "completed after expiry"):
            with self.subTest(boundary=boundary):
                self.window._clear_actor_state()
                self.roster(() if boundary == "roster after expiry" else ACTORS)
                self.status("D00D", ACTORS[0], duration=3)
                self.assertEqual(self.deliver(), [])
                self.clock.value += 3.1 if boundary == "completed after expiry" else 2
                self.status("D00E", ACTORS[0], duration=60)
                if boundary == "queued":
                    self.assertIn(ACTORS[0], self.window._automark_deliveries)
                elif boundary == "roster after expiry":
                    self.assertEqual(len(self.window._automark_pending), 1)
                if boundary != "completed after expiry":
                    self.clock.value += 1.1
                if boundary == "roster after expiry":
                    self.roster()
                    self.window._retry_automark_pending()
                self.assertEqual(self.window._automark_pending, [])
                self.assertEqual(self.deliver(), [])

    def test_plain_and_compound_rules_keep_indefinite_duration_fallback(self):
        for status in ("D00D", "D00D+D00E"):
            for duration in ("invalid", "0", "nan"):
                with self.subTest(status=status, duration=duration):
                    self.window._clear_actor_state()
                    self.roster()
                    self.window._automark_rules = [{"fight": "UMAD", "status": status,
                                                   "marker": "circle", "scope": "party"}]
                    self.window._automark_pairs = StatusPairs(("D00D", "D00E"))
                    self.status("D00D", ACTORS[0], duration=duration)
                    if "+" in status:
                        self.status("D00E", ACTORS[0], duration=duration)
                    self.clock.value += 3.1
                    self.assertEqual(self.deliver(), ["/mk circle <1>"])

    def test_lifecycle_resets_clear_delivered_marks_and_cancel_pending_waves(self):
        for kind in ("wipe", "Kefka Says", "zone", "disable"):
            for known_roster in (False, True):
                with self.subTest(kind=kind, known_roster=known_roster):
                    self.window._on_ws_zone_changed(1363, "UMAD")
                    self.window._clear_actor_state()
                    self.window._current_fight_tag = "UMAD"
                    self.window._automark_cb.setChecked(True)
                    self.roster(ACTORS if known_roster else ())
                    self.wave("bomb", ACTORS[:4])
                    self.wave("lightning", ACTORS[4:6])
                    self.deliver()
                    self.reset(kind)
                    expected = [f"/mk clear <{index}>" for index in range(1, 7)] \
                        if known_roster and kind != "zone" else []
                    self.assertEqual(self.deliver(), expected)
                    for name in MECHANICS:
                        self.assertEqual(self.window._umad_grand_cross[name].outstanding(), [])
                        self.assertEqual(self.window._umad_grand_cross_pending[name], [])
                        self.assertEqual(self.window._umad_grand_cross_pending_since[name], {})
                    self.roster()
                    self.window._retry_umad_grand_cross_pending()
                    self.assertEqual(self.deliver(), [])

    def test_bomb_and_lightning_cleanup_preserve_cursed_shriek_marks(self):
        self.window._umad_gaze_cb.setChecked(True)
        self.wave("bomb", ACTORS[:4])
        self.wave("lightning", ACTORS[4:6])
        for actor in ACTORS[6:8]:
            self.status(CURSED_SHRIEK, actor)
        self.assertEqual(self.deliver(), [
            "/mk attack1 <1>", "/mk attack2 <2>", "/mk attack3 <3>", "/mk attack4 <4>",
            "/mk bind1 <5>", "/mk bind2 <6>", "/mk ignore1 <7>", "/mk ignore2 <8>"])
        self.status(ACCELERATION_BOMB, ACTORS[0], lost=True)
        self.window._umad_pair_checkboxes["lightning"].setChecked(False)
        self.assertEqual(self.deliver(), ["/mk clear <1>", "/mk clear <5>", "/mk clear <6>"])
        self.assertEqual(self.window._umad_gaze.outstanding(), list(ACTORS[6:8]))
        self.assertEqual(self.window._automark_owners[ACTORS[6]][0], "gaze")
        self.status(CURSED_SHRIEK, ACTORS[6], lost=True)
        self.assertEqual(self.deliver(), ["/mk clear <7>"])
        self.assertEqual(self.window._umad_grand_cross["bomb"].outstanding(), list(ACTORS[1:4]))


if __name__ == "__main__":
    unittest.main()
