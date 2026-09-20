"""Exercise the session pages and profile switching in the real window."""

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from contextlib import ExitStack
from copy import deepcopy
from itertools import permutations, product
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication, QInputDialog

from nyaatriggers import app_common as ac
from nyaatriggers import main_window as mw
from nyaatriggers import theme
from nyaatriggers.death_recap import MAX_DEATHS
from nyaatriggers.prog_session import CHECKPOINT_SECONDS, ProgSessions
from tests.test_session_features import ability, PLAYER, Clock
from nyaatriggers.trigger_engine import Trigger
from nyaatriggers.record_store import read_record, write_record
from nyaatriggers.trigger_profiles import DEFAULT_PROFILE_ID, capture_profile
from nyaatriggers.ui.settings_tab import SettingsTabMixin
from nyaatriggers.ui.prog_tab import DEATHS_COLUMN
from nyaatriggers.ui.prog_tab import PHASE_COLUMN
from tests.test_prog_phases import fixture_definition, marker


class SessionUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setStyleSheet(theme.STYLESHEET)

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.temp = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(patch("nyaatriggers.drop_log._LOG_FILE", self.temp / "nyaatriggers.log"))
        for key, value in {"_DATA_DIR": self.temp, "_SETTINGS_FILE": self.temp / "settings.json",
                           "TRIGGERS_LOCAL_FILE": self.temp / "triggers.local.json"}.items():
            self.stack.enter_context(patch.object(ac, key, value))
        def settings(window):
            window._settings.update(tts_engine="system", auto_connect=False, auto_check_updates=False,
                                    local_enabled=False, ui_language="en")
        self.stack.enter_context(patch.object(mw.MainWindow, "_load_settings", settings))
        for method in ("_load_triggers", "_load_timeline_for_zone", "_refresh_telesto_party"):
            self.stack.enter_context(patch.object(mw.MainWindow, method))
        for target in ((mw.PluginLink, "start"), (mw.QTimer, "singleShot"),
                       (mw, "kokoro_ready"), (mw, "_ensure_worker"), (ac.QMessageBox, "warning")):
            self.stack.enter_context(patch.object(*target))
        self.window = mw.MainWindow()
        self.addCleanup(self.window.close)
        self.clock = Clock()
        self.window._dps_meter._clock = self.clock
        self.window._prog_sessions.clock = self.clock
        self.window._death_recap.clock = self.clock

    def connect(self):
        window = self.window
        window._on_status_changed(True, "Connected")
        window._on_ws_zone_changed(1, "Test duty")
        window._on_ws_primary_player(int(PLAYER, 16), "Player")
        window._on_in_combat(False, False)
        window._prog_tab.tick()

    def line(self, fields):
        self.window._on_log_line("|".join(fields))

    def pull(self):
        self.window._on_in_combat(True, True)
        self.line(ability())
        self.clock.value += 12
        self.line(ability())
        self.window._on_in_combat(False, False)

    def phase_pull(self):
        self.connect()
        self.window._on_ws_zone_changed(1363, "UMAD")
        self.window._on_ws_primary_player(int(PLAYER, 16), "Player")
        self.window._prog_sessions.definitions = (fixture_definition(),)
        self.window._prog_tab.start_button.click()
        self.window._on_in_combat(True, True)
        self.line(marker(0))
        self.line(ability())
        return self.window._prog_sessions.current["pulls"][0]

    def test_window_icon_loads_from_bundled_assets(self):
        self.assertFalse(self.window.windowIcon().isNull())

    def test_fflogs_signal_ignores_an_older_request_and_keeps_zero_percentile(self):
        window = self.window
        window._fflogs_request_id = 2
        window._fflogs_signal.emit(2, {"amount": 1000, "percent": 0})
        self.assertIn("1.0k", window._fflogs_lbl.text())
        self.assertIn("(0%)", window._fflogs_lbl.text())
        window._fflogs_signal.emit(1, {"amount": 2000, "percent": 50})
        self.assertIn("1.0k", window._fflogs_lbl.text())

    def test_zone_name_correction_keeps_live_recap_history(self):
        self.connect()
        self.line(["26", "ts", "123", "Status", "30", PLAYER, "Player", PLAYER, "Player"])
        buffers = deepcopy(self.window._death_recap.buffers)
        self.assertTrue(buffers)
        self.window._on_ws_zone_changed(1, "Localized duty")
        self.assertEqual(self.window._death_recap.buffers, buffers)
        self.assertEqual(self.window._death_recap.zone, "Localized duty")

    def test_partial_metadata_after_a_name_correction_keeps_the_session(self):
        self.connect()
        window = self.window
        window._prog_tab.start_button.click()
        session = window._prog_sessions.current
        self.assertIsNotNone(session)
        window._on_ws_zone_changed(1, "Localized duty")
        window._on_ws_zone_changed(0, "Localized duty")
        self.assertIs(window._prog_sessions.current, session)

    def test_reconnect_zone_metadata_keeps_fresh_player_identity(self):
        self.connect()
        window = self.window
        window._on_status_changed(False, "Disconnected")
        window._on_status_changed(True, "Connected")
        window._on_ws_primary_player(int(PLAYER, 16), "Player")
        window._local_enabled = True
        window._triggers = [Trigger(ability_id="ABCD", delay_s=10)]
        self.line(["20", "ts", "40000001", "Boss", "ABCD", "Cast", PLAYER, "Player"])
        runner, = window._seq_runners
        window._on_ws_zone_changed(2, "New duty")
        with self.subTest(state="identity"):
            self.assertEqual(window._dps_meter._me_id, int(PLAYER, 16))
        with self.subTest(state="callouts"):
            self.assertEqual(window._seq_runners, [runner])

    def test_first_zone_id_resolves_a_known_name_without_another_event(self):
        window = self.window
        window._on_status_changed(True, "Connected")
        window._on_ws_zone_changed(1226, "")
        self.assertEqual(window._current_zone, "AAC Light-heavyweight M1 (Savage)")
        self.assertIn("AAC Light-heavyweight M1", window._zone_lbl.text())
        self.assertEqual(window._dps_meter._zone, window._current_zone)

    def test_reconnect_metadata_orders_preserve_new_work_through_real_signals(self):
        window = self.window
        window._local_enabled = True
        window._triggers = [Trigger(log_type="21", ability_id="A1", delay_s=60)]
        window._pull_capture._log_dir = self.temp / "captures"
        window._pull_capture.set_recording(True)
        for order in permutations(("player", "zone", "ability")):
            with self.subTest(order=order):
                window._ws.status_changed.emit(False, "Disconnected")
                window._ws.status_changed.emit(True, "Connected")
                window._ws.in_combat.emit(True, True)
                messages = {
                    "player": {"type": "ChangePrimaryPlayer", "charID": int(PLAYER, 16),
                               "charName": "Player"},
                    "zone": {"type": "ChangeZone", "zoneID": 1226, "zoneName": "Arena"},
                    "ability": {"type": "LogLine", "rawLine": "|".join(ability())},
                }
                for kind in order:
                    window._ws._on_message(json.dumps(messages[kind]))
                self.assertEqual(window._dps_meter._me_id, int(PLAYER, 16))
                self.assertIsNotNone(window._dps_meter.current)
                self.assertEqual(len(window._seq_runners), 1)
                self.assertTrue(window._pull_capture._in_pull)
                window._ws._on_message(json.dumps({"type": "LogLine", "rawLine": "01|ts|4CA|Arena|"}))
                self.assertIsNone(window._dps_meter.current)
                self.assertFalse(window._seq_runners)
                self.assertFalse(window._pull_capture._in_pull)

    def test_reconnect_to_another_duty_still_ends_the_previous_session(self):
        self.connect()
        window = self.window
        window._prog_tab.start_button.click()
        session = window._prog_sessions.current
        window._ws.status_changed.emit(False, "Disconnected")
        window._ws.status_changed.emit(True, "Connected")
        window._ws.primary_player.emit(int(PLAYER, 16), "Player")
        window._ws.zone_changed.emit(2, "New duty")
        self.assertIsNone(window._prog_sessions.current)
        self.assertEqual(session["state"], "ended")
        self.assertEqual(window._dps_meter._me_id, int(PLAYER, 16))

    def test_raw_zone_before_reconnect_metadata_still_clears_fresh_work(self):
        self.connect()
        window = self.window
        window._local_enabled = True
        window._triggers = [Trigger(ability_id="ABCD", delay_s=60)]
        window._ws.status_changed.emit(False, "Disconnected")
        window._ws.status_changed.emit(True, "Connected")
        self.line(["20", "ts", "40000001", "Boss", "ABCD", "Cast", PLAYER, "Player"])
        self.assertEqual(len(window._seq_runners), 1)
        self.line(["01", "ts", "01", "Test duty"])
        self.assertFalse(window._seq_runners)
        window._ws.primary_player.emit(int(PLAYER, 16), "Player")
        window._ws.zone_changed.emit(1, "Test duty")
        self.assertEqual(window._dps_meter._me_id, int(PLAYER, 16))

    def test_feed_loss_drops_old_actor_state_before_fresh_metadata_arrives(self):
        self.connect()
        window = self.window
        old_actor = int(PLAYER, 16) + 1
        fresh_actor = int(PLAYER, 16) + 2
        window._actor_jobs[old_actor] = 19
        window._umad_actor_names[old_actor] = "Old player"
        window._automark_pending.append((f"{old_actor:08X}", "attack1", "Old player", 0.0, "ABC", ""))
        window._ws.status_changed.emit(False, "Disconnected")
        window._ws.status_changed.emit(True, "Connected")
        window._on_ws_party_jobs({fresh_actor: 21})
        fresh_mark = (f"{fresh_actor:08X}", "attack2", "New player", 0.0, "DEF", "")
        window._automark_pending.append(fresh_mark)
        window._ws.zone_changed.emit(2, "New duty")
        with self.subTest(state="jobs"):
            self.assertNotIn(old_actor, window._actor_jobs)
        with self.subTest(state="names"):
            self.assertNotIn(old_actor, window._umad_actor_names)
        with self.subTest(state="pending marks"):
            self.assertEqual(window._automark_pending, [fresh_mark])
        self.assertEqual(window._actor_jobs[fresh_actor], 21)

    def test_reconnect_discards_old_cooldowns_and_keeps_fresh_ones(self):
        self.connect()
        window = self.window
        window._local_enabled = True
        window._triggers = [Trigger(ability_id="ABCD", cooldown_s=60)]
        cast = ["20", "ts", "40000001", "Boss", "ABCD", "Cast", PLAYER, "Player"]
        with patch.object(window, "_fire") as fire:
            self.line(cast)
            self.assertEqual(fire.call_count, 1)
            self.assertIsNone(window._dps_meter.current)
            window._ws.status_changed.emit(False, "Disconnected")
            window._ws.status_changed.emit(True, "Connected")
            self.line(cast)
            self.assertEqual(fire.call_count, 2)
            window._ws.zone_changed.emit(2, "New duty")
            self.line(cast)
            self.assertEqual(fire.call_count, 2)

    def test_capture_uses_confirmed_duty_after_late_reconnect_metadata(self):
        self.connect()
        window = self.window
        cap = window._pull_capture
        cap._log_dir = self.temp / "captures"
        cap.set_recording(True)
        window._triggers = [Trigger(fight="New fight", zone_regex="New duty")]
        window._ws._on_disconnected()
        window._ws.status_changed.emit(True, "Connected")
        window._ws._on_message(json.dumps({"type": "LogLine", "rawLine": "|".join(ability())}))
        path = cap._path
        self.assertTrue(cap._in_pull)
        window._ws._on_message(json.dumps({"type": "ChangeZone", "zoneID": 2, "zoneName": "New duty"}))
        window._ws._on_message(json.dumps({"type": "InCombat", "inACTCombat": False, "inGameCombat": False}))
        meta = json.loads(path.with_suffix(".meta.json").read_text())
        self.assertEqual((meta["fight"], meta["zone"]), ("New fight", "New duty"))

    def test_capture_without_reconnect_metadata_stays_unknown(self):
        self.connect()
        window = self.window
        cap = window._pull_capture
        cap._log_dir = self.temp / "captures"
        cap.set_recording(True)
        window._ws._on_disconnected()
        window._ws.status_changed.emit(True, "Connected")
        window._ws._on_message(json.dumps({"type": "LogLine", "rawLine": "|".join(ability())}))
        path = cap._path
        cap.close()
        meta = json.loads(path.with_suffix(".meta.json").read_text())
        self.assertEqual((meta["fight"], meta["zone"]), ("", ""))
        self.assertEqual(path.parent.name, "Unknown")

    def test_capture_metadata_corrections_do_not_relabel_a_finished_pull(self):
        self.connect()
        window = self.window
        window._ws.zone_changed.emit(1, "Test duty")
        cap = window._pull_capture
        cap._log_dir = self.temp / "captures"
        cap.set_recording(True)
        window._ws._on_message(json.dumps({"type": "LogLine", "rawLine": "|".join(ability())}))
        path = cap._path
        window._ws.zone_changed.emit(1, "Corrected duty")
        self.assertTrue(cap._in_pull)
        window._ws.zone_changed.emit(2, "Next duty")
        self.assertFalse(cap._in_pull)
        self.assertEqual(json.loads(path.with_suffix(".meta.json").read_text())["zone"], "Corrected duty")

    def test_automarkers_allow_fresh_duty_before_reconnect_metadata(self):
        self.connect()
        window = self.window
        window._settings["telesto_enabled"] = True
        window._triggers = [Trigger(fight="Old fight", zone_regex="Test duty")]
        window._redetect_zone_fight()
        window._automark_rules = [{"status": "ABC", "marker": "attack1", "fight": "New fight"}]
        window._ws._on_disconnected()
        window._ws.status_changed.emit(True, "Connected")
        window._redetect_zone_fight()
        with patch.object(window, "_mark_player", return_value=True) as mark:
            self.line(["26", "ts", "ABC", "Status", "30", "40000001", "Boss", PLAYER, "Player"])
            mark.assert_called_once()

    def test_specialized_automarkers_accept_unknown_duty_then_restore_the_filter(self):
        from nyaatriggers.umad_chains import RELEVANT_IDS, GAZE_FOLLOWUP_IDS
        self.connect()
        window = self.window
        window._settings["telesto_enabled"] = True
        window._umad_chain_enabled = window._umad_gaze_enabled = True
        window._triggers = [Trigger(fight="Other", zone_regex="Test duty")]
        window._redetect_zone_fight()
        window._ws._on_disconnected()
        window._ws.status_changed.emit(True, "Connected")
        status = ["26", "ts", next(iter(RELEVANT_IDS)), "Chain", "30", "40000001", "Boss", PLAYER, "Player"]
        gaze = list(status)
        gaze[2] = next(iter(window._umad_gaze.ids))
        cast = ["20", "ts", "40000001", "Boss", next(iter(GAZE_FOLLOWUP_IDS)), "Followup"]
        with patch.object(window._umad_chains, "on_gain", return_value=[]) as chain_gain, \
                patch.object(window._umad_gaze, "on_gain", return_value=[]) as gaze_gain, \
                patch.object(window._umad_gaze, "on_followup", return_value=[]) as followup:
            for fields in (status, gaze, cast):
                self.line(fields)
            for call in (chain_gain, gaze_gain, followup):
                call.assert_called_once()
            window._ws.zone_changed.emit(1, "Test duty")
            for fields in (status, gaze, cast):
                self.line(fields)
            for call in (chain_gain, gaze_gain, followup):
                call.assert_called_once()

    def test_reconnect_mark_refresh_restores_clearing_before_the_old_cooldown_expires(self):
        self.connect()
        window = self.window
        window._settings["telesto_enabled"] = True
        window._automark_clear_on_loss = True
        window._automark_rules = [{"status": "ABC", "marker": "attack1"}]
        status = ["26", "ts", "ABC", "Status", "30", "40000001", "Boss", PLAYER, "Player"]
        with patch("time.monotonic", side_effect=self.clock), \
                patch.object(window, "_mark_player", return_value=True) as mark, \
                patch.object(window, "_clear_player", return_value=True) as clear:
            self.line(status)
            mark.assert_called_once()
            window._ws._on_disconnected()
            window._ws.status_changed.emit(True, "Connected")
            window._ws.zone_changed.emit(1, "Test duty")
            window._ws.primary_player.emit(int(PLAYER, 16), "Player")
            self.clock.value += 1
            self.line(status)
            with self.subTest(action="refresh"):
                self.assertEqual(mark.call_count, 2)
            self.line(["30", *status[1:]])
            with self.subTest(action="clear"):
                clear.assert_called_once()

    def test_queued_automarkers_respect_the_confirmed_duty(self):
        self.connect()
        window = self.window
        window._settings["telesto_enabled"] = True
        window._triggers = [Trigger(fight="Old", zone_regex="Test duty"),
                            Trigger(fight="New", zone_regex="New duty")]
        window._automark_rules = [{"status": "ABC", "marker": marker, "fight": fight}
                                 for fight, marker in (("Old", "attack1"), ("New", "attack2"), ("", "attack3"))]
        window._ws._on_disconnected()
        window._ws.status_changed.emit(True, "Connected")
        with patch.object(window, "_mark_player", return_value=False):
            self.line(["26", "ts", "ABC", "Status", "30", "40000001", "Boss", PLAYER, "Player"])
        self.assertEqual(len(window._automark_pending), 3)
        window._ws.zone_changed.emit(2, "New duty")
        with patch.object(window, "_mark_player", return_value=True) as mark:
            window._retry_automark_pending()
            self.assertEqual([call.args[1] for call in mark.call_args_list], ["attack2", "attack3"])
        self.assertFalse(window._automark_pending)

    def test_queued_umad_markers_respect_late_duty_metadata(self):
        from nyaatriggers.umad_chains import ACCRETION, CRUST, GAZE_FOLLOWUP_IDS
        self.connect()
        window = self.window
        window._settings["telesto_enabled"] = True
        window._umad_chain_enabled = window._umad_gaze_enabled = True
        window._triggers = [Trigger(fight="Other", zone_regex="Test duty|New duty"),
                            Trigger(fight="UMAD", zone_regex="Dancing Mad|UMAD")]
        for kind, metadata in product(("chain", "gaze"), (None, (2, "New duty"), (1363, "UMAD"))):
            with self.subTest(kind=kind, metadata=metadata):
                window._ws._on_disconnected()
                window._ws.status_changed.emit(True, "Connected")
                with patch.object(window, "_mark_player", return_value=False):
                    if kind == "gaze":
                        self.line(["20", "ts", "40000001", "Boss", next(iter(GAZE_FOLLOWUP_IDS)), "Followup"])
                    for index, actor in enumerate((PLAYER, "10FF0002")):
                        effects = ((ACCRETION, f"{0xBBC + index:X}", CRUST) if kind == "chain"
                                   else (next(iter(window._umad_gaze.ids)),))
                        for effect in effects:
                            self.line(["26", "ts", effect, "Status", "30", "40000001", "Boss", actor, "Player"])
                pending = getattr(window, f"_umad_{kind}_pending")
                self.assertTrue(pending)
                if metadata is not None:
                    window._ws.zone_changed.emit(*metadata)
                with patch.object(window, "_mark_player", return_value=True) as mark:
                    getattr(window, f"_retry_umad_{kind}_pending")()
                    self.assertEqual(mark.call_count, 0 if metadata == (2, "New duty") else len(pending))
                self.assertFalse(getattr(window, f"_umad_{kind}_pending"))

    def test_queued_rules_keep_matching_marks_and_cancel_lost_statuses(self):
        self.connect()
        window = self.window
        window._settings["telesto_enabled"] = True
        window._triggers = [Trigger(fight="Old", zone_regex="Test duty"),
                            Trigger(fight="New", zone_regex="New duty")]
        status = ["26", "ts", "ABC", "Status", "30", "40000001", "Boss", PLAYER, "Player"]
        for metadata, shared_marker, lost in product((None, (1, "Test duty"), (2, "New duty")),
                                                     (False, True), (False, True)):
            with self.subTest(metadata=metadata, shared_marker=shared_marker, lost=lost):
                window._automark_rules = [{"status": "ABC", "marker": "attack1" if shared_marker else marker,
                                          "fight": fight}
                                         for fight, marker in (("Old", "attack1"), ("New", "attack2"), ("", "attack3"))]
                window._ws._on_disconnected()
                window._ws.status_changed.emit(True, "Connected")
                with patch.object(window, "_mark_player", return_value=False):
                    self.line(status)
                    self.assertEqual(len(window._automark_pending), 3)
                    window._retry_automark_pending()
                    self.assertEqual(len(window._automark_pending), 3)
                if metadata is not None:
                    window._ws.zone_changed.emit(*metadata)
                if lost:
                    self.line(["30", *status[1:]])
                expected = (["attack1", "attack3"] if metadata == (1, "Test duty") else
                            ["attack2", "attack3"] if metadata == (2, "New duty") else
                            ["attack1", "attack2", "attack3"])
                if shared_marker:
                    expected = ["attack1"]
                if lost:
                    expected = []
                with patch.object(window, "_mark_player", return_value=True) as mark:
                    window._retry_automark_pending()
                    self.assertEqual([call.args[1] for call in mark.call_args_list], expected)
                self.assertFalse(window._automark_pending)

    def test_a_successful_shared_mark_drops_earlier_failed_retries(self):
        self.connect()
        window = self.window
        window._settings["telesto_enabled"] = True
        window._automark_rules = [{"status": "ABC", "marker": "attack1", "fight": fight}
                                 for fight in ("Old", "New", "")]
        window._ws._on_disconnected()
        window._ws.status_changed.emit(True, "Connected")
        with patch.object(window, "_mark_player", return_value=False):
            self.line(["26", "ts", "ABC", "Status", "30", "40000001", "Boss", PLAYER, "Player"])
        self.assertEqual(len(window._automark_pending), 3)
        with patch.object(window, "_mark_player", side_effect=[False, True]) as mark:
            window._retry_automark_pending()
            self.assertEqual(mark.call_count, 2)
        self.assertFalse(window._automark_pending)

    def test_reconnect_death_does_not_inherit_the_previous_zone(self):
        self.connect()
        window = self.window
        window._ws._on_disconnected()
        window._ws.status_changed.emit(True, "Connected")
        self.line(ability())
        self.line(["25", "ts", PLAYER, "Player"])
        self.assertEqual(window._death_recap.deaths[0]["zone"], "")
        window._ws.zone_changed.emit(2, "New duty")
        self.line(["25", "ts", "10FF0002", "Other"])
        self.assertEqual(window._death_recap.deaths[0]["zone"], "New duty")
        self.assertEqual(window._death_recap.deaths[1]["zone"], "")

    def test_reconnect_cannot_restart_the_previous_duty_timeline(self):
        from nyaatriggers.timeline_parser import parse
        self.connect()
        window = self.window
        window._local_enabled = True
        window._timeline.load(parse('1 "Old duty call"'))
        window._on_in_combat(True, True)
        self.assertTrue(window._timeline.is_active())
        window._ws._on_disconnected()
        window._ws.status_changed.emit(True, "Connected")
        window._ws._on_message(json.dumps({"type": "InCombat", "inACTCombat": True, "inGameCombat": True}))
        with patch.object(window, "_emit_guest_callout") as callout:
            with patch("time.monotonic", return_value=window._timeline._t0 + 2):
                window._timeline._tick()
            callout.assert_not_called()

    def test_new_duty_after_reconnect_ends_a_zone_mute(self):
        self.connect()
        window = self.window
        with patch("nyaatriggers.ui.voice_tab.set_master_volume"):
            window._mute_until_next_zone()
            window._ws._on_disconnected()
            window._ws.status_changed.emit(True, "Connected")
            window._ws.zone_changed.emit(2, "New duty")
            self.assertFalse(window._mute_btn.isChecked())
            self.assertFalse(window._mute_until_zone)

    def test_same_duty_reconnect_and_other_mutes_stay_muted(self):
        self.connect()
        window = self.window
        with patch("nyaatriggers.ui.voice_tab.set_master_volume"):
            window._mute_until_next_zone()
            window._ws._on_disconnected()
            window._ws.zone_changed.emit(1, "Localized duty")
            self.assertTrue(window._mute_btn.isChecked())
            for mode in ("manual", "timer"):
                with self.subTest(mode=mode):
                    window._mute_btn.setChecked(False)
                    if mode == "timer":
                        window._mute_for_minutes(5)
                    else:
                        window._mute_btn.setChecked(True)
                    window._ws._on_disconnected()
                    window._ws.zone_changed.emit(window._current_zone_id + 1, "Other duty")
                    self.assertTrue(window._mute_btn.isChecked())

    def test_pending_timeline_history_is_bounded_and_ignores_unrelated_lines(self):
        window = self.window
        window._ws._on_disconnected()
        window._queue_timeline_event(["260", "", "1", "1"])
        window._queue_timeline_event(["02", "", PLAYER, "Player"])
        window._queue_timeline_event(["20", "", "40000001", "X" * 16385])
        self.assertEqual(len(window._pending_timeline_events), 1)
        for index in range(1100):
            window._queue_timeline_event(["20", str(index), "40000001", "Boss", "ABCD"])
        self.assertEqual(len(window._pending_timeline_events), 1024)
        self.assertEqual(window._pending_timeline_events[-1][1][1], "1099")

    def test_disabling_timelines_discards_events_waiting_for_a_schedule(self):
        self.connect()
        window = self.window
        for control in ("local", "global", "reset"):
            with self.subTest(control=control):
                window._local_enabled = True
                window._global_local_on_flag = True
                window._pending_timeline_events = [(1000, ["260", "", "1", "1"])]
                if control == "local":
                    window._set_local_enabled(False)
                elif control == "global":
                    window._toggle_global_local()
                else:
                    window._reset_all_to_default()
                self.assertFalse(window._pending_timeline_events)

    def test_late_zone_replays_syncs_after_the_initial_combat_event(self):
        from nyaatriggers.timeline_parser import parse
        self.connect()
        window = self.window
        window._local_enabled = True
        window._ws._on_disconnected()
        window._timeline.load(parse('30 "--sync--" StartsUsing { id: "ABCD" } window 60,60\n35 "Next cue"'))
        with patch("time.monotonic", side_effect=self.clock), \
                patch.object(window, "_emit_guest_callout") as callout:
            self.clock.value = 1000
            window._ws.in_combat.emit(True, True)
            self.clock.value = 1002
            self.line(["20", "ts", "40000001", "Boss", "ABCD", "Cast", PLAYER, "Player"])
            self.clock.value = 1005
            window._ws.zone_changed.emit(1, "Test duty")
            self.assertEqual(window._timeline.current_time(), 33)
            callout.assert_not_called()
            self.clock.value = 1007.1
            window._timeline._tick()
            callout.assert_called_once_with("Next cue", "info")

    def test_late_zone_keeps_elapsed_time_after_timeline_loops(self):
        from nyaatriggers.timeline_parser import parse
        self.connect()
        window = self.window
        window._local_enabled = True
        window._ws._on_disconnected()
        window._timeline.load(parse('1 "Loop cue"\n2 "--loop--" forcejump 0'))
        with patch("time.monotonic", side_effect=self.clock), \
                patch.object(window, "_emit_guest_callout") as callout:
            self.clock.value = 1000
            window._ws.in_combat.emit(True, True)
            self.clock.value = 1004.5
            window._ws.zone_changed.emit(1, "Test duty")
            self.assertEqual(window._timeline.current_time(), 0.5)
            callout.assert_not_called()
            self.clock.value = 1005.1
            window._timeline._tick()
            callout.assert_called_once_with("Loop cue", "info")

    def test_raw_combat_exit_cancels_the_unconfirmed_timeline_start(self):
        from nyaatriggers.timeline_parser import parse
        self.connect()
        window = self.window
        window._local_enabled = True
        window._ws._on_disconnected()
        window._timeline.load(parse('1 "Old cue"'))
        self.line(["260", "ts", "1", "1"])
        self.line(["260", "ts", "0", "0"])
        window._ws.zone_changed.emit(1, "Test duty")
        self.assertFalse(window._timeline.is_active())

    def test_late_zone_name_restores_timing_after_an_unknown_zone_id(self):
        from nyaatriggers.ui.timeline_tab import TimelineTabMixin
        self.connect()
        window = self.window
        window._local_enabled = True
        window._load_timeline_for_zone = TimelineTabMixin._load_timeline_for_zone.__get__(window)
        window._triggers = [Trigger(fight="Late", zone_regex="Late duty")]
        (self.temp / "Late.txt").write_text('5 "Next cue"\n')
        window._ws._on_disconnected()
        with patch("time.monotonic", side_effect=self.clock), \
                patch.object(ac, "TIMELINES_DIR", self.temp), \
                patch.object(ac, "_BUNDLE_TIMELINES_DIR", self.temp), \
                patch.object(window, "_emit_guest_callout") as callout:
            self.clock.value = 1000
            window._ws.in_combat.emit(True, True)
            self.clock.value = 1001
            window._ws.zone_changed.emit(99999, "")
            self.clock.value = 1002
            window._ws.zone_changed.emit(99999, "Late duty")
            self.assertTrue(window._timeline.is_active())
            self.assertEqual(window._timeline.current_time(), 2)
            callout.assert_not_called()
            self.clock.value = 1005.1
            window._timeline._tick()
            callout.assert_called_once_with("Next cue", "info")

    def test_downloaded_timeline_recovers_combat_after_zone_confirmation(self):
        from nyaatriggers.ui.timeline_tab import TimelineTabMixin
        window = self.window
        window._cactbot_mode = True
        window._local_enabled = False
        window._load_timeline_for_zone = TimelineTabMixin._load_timeline_for_zone.__get__(window)
        with patch("time.monotonic", side_effect=self.clock), \
                patch.object(ac, "TIMELINES_DIR", self.temp), \
                patch.object(ac, "_BUNDLE_TIMELINES_DIR", self.temp), \
                patch.object(window, "_cactbot_zone_entry", return_value=("Late", "late.txt")), \
                patch.object(window, "_fetch_cactbot_timeline"):
            self.clock.value = 1000
            window._ws.zone_changed.emit(99999, "Late duty")
            self.clock.value = 1001
            window._ws.in_combat.emit(True, True)
            self.clock.value = 1003
            (self.temp / "Late.cactbot.cache.txt").write_text('5 "Next cue"\n')
            window._on_cactbot_timeline_ready("Late")
            self.assertTrue(window._timeline.is_active())
            self.assertEqual(window._timeline.current_time(), 2)

    def test_timeline_loaded_at_combat_start_keeps_the_current_callout(self):
        from nyaatriggers.ui.timeline_tab import TimelineTabMixin
        self.connect()
        window = self.window
        window._local_enabled = True
        window._load_timeline_for_zone = TimelineTabMixin._load_timeline_for_zone.__get__(window)
        window._triggers = [Trigger(fight="Ready", zone_regex="Test duty")]
        (self.temp / "Ready.txt").write_text('0 "Pull started" InCombat { inGameCombat: "1" } jump 30\n35 "Next cue"')
        with patch.object(ac, "TIMELINES_DIR", self.temp), \
                patch.object(ac, "_BUNDLE_TIMELINES_DIR", self.temp), \
                patch.object(window, "_emit_guest_callout") as callout:
            window._on_in_combat(True, True)
            callout.assert_called_once_with("Pull started", "info")

    def test_reconnect_restores_the_confirmed_timeline_in_either_metadata_order(self):
        from nyaatriggers.ui.timeline_tab import TimelineTabMixin
        window = self.window
        window._local_enabled = True
        window._cactbot_mode = False
        window._load_timeline_for_zone = TimelineTabMixin._load_timeline_for_zone.__get__(window)
        window._triggers = [Trigger(fight="Old", zone_regex="Test duty"),
                            Trigger(fight="New", zone_regex="New duty")]
        for fight in ("Old", "New"):
            (self.temp / f"{fight}.txt").write_text(f'1 "{fight} cue"\n')
        with patch.object(ac, "TIMELINES_DIR", self.temp), \
                patch.object(ac, "_BUNDLE_TIMELINES_DIR", self.temp):
            for zone, order in product(((1, "Test duty"), (2, "New duty")),
                                       permutations(("zone", "combat"))):
                with self.subTest(zone=zone, order=order):
                    self.connect()
                    window._on_in_combat(True, True)
                    window._ws._on_disconnected()
                    with patch.object(window._plugin_link, "send_timeline") as schedule, \
                            patch.object(window, "_emit_guest_callout") as callout:
                        window._ws.status_changed.emit(True, "Connected")
                        schedule.assert_not_called()
                        for kind in order:
                            if kind == "zone":
                                window._ws.zone_changed.emit(*zone)
                            else:
                                window._ws.in_combat.emit(True, True)
                        expected = "Old cue" if zone[0] == 1 else "New cue"
                        self.assertTrue(window._timeline.is_active())
                        with patch("time.monotonic", return_value=window._timeline._t0 + 2):
                            window._timeline._tick()
                        callout.assert_called_once_with(expected, "info")
                        self.assertTrue(schedule.called)
                        self.assertEqual(schedule.call_args.args, ([(1.0, expected)],))

    def test_late_zone_keeps_the_actual_combat_flags_for_timeline_sync(self):
        from nyaatriggers.timeline_parser import parse
        self.connect()
        window = self.window
        window._local_enabled = True
        window._timeline.load(parse('0 "ACT sync" InCombat { inACTCombat: "1" } jump 30\n1 "Game cue"'))
        window._ws._on_disconnected()
        window._ws.status_changed.emit(True, "Connected")
        window._ws.in_combat.emit(False, True)
        with patch.object(window, "_emit_guest_callout") as callout:
            window._ws.zone_changed.emit(1, "Test duty")
            callout.assert_not_called()
            self.assertTrue(window._timeline.is_active())
            self.assertLess(window._timeline.current_time(), 1)

    def test_late_zone_preserves_the_observed_timeline_start(self):
        from nyaatriggers.timeline_parser import parse
        self.connect()
        window = self.window
        window._local_enabled = True
        for kind in ("combat", "ability", "raw combat"):
            with self.subTest(kind=kind):
                window._ws._on_disconnected()
                window._ws.status_changed.emit(True, "Connected")
                window._timeline.load(parse('1 "Missed cue"\n5 "Current cue"'))
                with patch("time.monotonic", side_effect=self.clock), \
                        patch.object(window, "_emit_guest_callout") as callout:
                    self.clock.value = 1000
                    if kind == "combat":
                        window._ws.in_combat.emit(True, True)
                    elif kind == "raw combat":
                        self.line(["260", "ts", "1", "1"])
                    else:
                        self.line(["20", "ts", "40000001", "Boss", "ABCD", "Cast", PLAYER, "Player"])
                    self.clock.value = 1002
                    window._ws.zone_changed.emit(1, "Test duty")
                    window._timeline._tick()
                    callout.assert_not_called()
                    self.clock.value = 1005.1
                    window._timeline._tick()
                    callout.assert_called_once_with("Current cue", "info")

    def test_late_zone_keeps_timeline_loops_running(self):
        from nyaatriggers.timeline_parser import parse
        self.connect()
        window = self.window
        window._local_enabled = True
        window._ws._on_disconnected()
        window._ws.status_changed.emit(True, "Connected")
        window._timeline.load(parse('1 "Loop cue"\n2 "--loop--" forcejump 0'))
        with patch("time.monotonic", side_effect=self.clock), \
                patch.object(window, "_emit_guest_callout") as callout:
            self.clock.value = 1000
            window._ws.in_combat.emit(True, True)
            self.clock.value = 1003
            window._ws.zone_changed.emit(1, "Test duty")
            callout.assert_not_called()
            self.clock.value += 1.1
            window._timeline._tick()
            callout.assert_not_called()
            self.clock.value += 1
            window._timeline._tick()
            callout.assert_called_once_with("Loop cue", "info")

    def test_late_zone_discards_timeline_starts_at_boundaries(self):
        from nyaatriggers.timeline_parser import parse
        self.connect()
        window = self.window
        window._local_enabled = True
        for boundary in ("wipe", "idle", "disconnect", "raw zone"):
            with self.subTest(boundary=boundary):
                window._ws._on_disconnected()
                window._ws.status_changed.emit(True, "Connected")
                window._timeline.load(parse('1 "Old cue"'))
                window._ws.in_combat.emit(True, True)
                if boundary == "wipe":
                    self.line(["33", "ts", "0", "4000000F"])
                elif boundary == "idle":
                    window._ws.in_combat.emit(False, False)
                elif boundary == "disconnect":
                    window._ws._on_disconnected()
                    window._ws.status_changed.emit(True, "Connected")
                else:
                    self.line(["01", "ts", "1", "Test duty"])
                window._ws.zone_changed.emit(1, "Test duty")
                self.assertFalse(window._timeline.is_active())

    def test_late_zone_applies_the_starting_ability_sync_before_elapsed_time(self):
        from nyaatriggers.timeline_parser import parse
        self.connect()
        window = self.window
        window._local_enabled = True
        window._ws._on_disconnected()
        window._ws.status_changed.emit(True, "Connected")
        window._timeline.load(parse('30 "--sync--" StartsUsing { id: "ABCD" } window 60,60\n35 "Next cue"'))
        with patch("time.monotonic", side_effect=self.clock), \
                patch.object(window, "_emit_guest_callout") as callout:
            self.clock.value = 1000
            self.line(["20", "ts", "40000001", "Boss", "ABCD", "Cast", PLAYER, "Player"])
            self.clock.value = 1002
            window._ws.zone_changed.emit(1, "Test duty")
            self.assertEqual(window._timeline.current_time(), 32)
            callout.assert_not_called()
            self.clock.value += 3.1
            window._timeline._tick()
            callout.assert_called_once_with("Next cue", "info")

    def test_reconnect_uses_name_fallback_until_fresh_player_identity(self):
        self.connect()
        window = self.window
        window._ws.status_changed.emit(False, "Disconnected")
        window._ws.status_changed.emit(True, "Connected")
        new_player = int(PLAYER, 16) + 1
        with patch.object(window._telesto_client, "mark_self", return_value=True) as mark_self, \
                patch.object(window._telesto_client, "mark_actor", return_value=False) as mark_actor:
            self.assertTrue(window._mark_player(f"{new_player:08X}", "attack1", "Player"))
            mark_self.assert_called_once_with("attack1")
            mark_actor.assert_not_called()
            window._ws.primary_player.emit(new_player, "Player")
            self.assertFalse(window._is_me_actor(PLAYER, "Player"))

    def test_reconnect_does_not_filter_new_callouts_using_the_previous_zone(self):
        self.connect()
        window = self.window
        window._local_enabled = True
        window._triggers = [Trigger(ability_id="ABCD", zone_regex="New duty", delay_s=60)]
        window._ws.status_changed.emit(False, "Disconnected")
        window._ws.status_changed.emit(True, "Connected")
        self.line(["20", "ts", "40000001", "Boss", "ABCD", "Cast", PLAYER, "Player"])
        self.assertEqual(len(window._seq_runners), 1)
        runner, = window._seq_runners
        window._ws.zone_changed.emit(2, "New duty")
        self.assertEqual(window._seq_runners, [runner])
        window._triggers.append(Trigger(ability_id="DCBA", zone_regex="Test duty", delay_s=60))
        self.line(["20", "ts", "40000001", "Boss", "DCBA", "Cast", PLAYER, "Player"])
        self.assertEqual(window._seq_runners, [runner])

    def test_pending_callouts_respect_the_zone_once_it_is_known(self):
        self.connect()
        window = self.window
        window._local_enabled = True
        zones = ("Test duty", "New duty", "Light-heavyweight M1", "")
        for kind, metadata in product(("delay", "sequence", "status"),
                                      (None, (2, "New duty"), (1226, "Localized duty"))):
            with self.subTest(kind=kind, metadata=metadata):
                window._ws.status_changed.emit(False, "Disconnected")
                window._ws.status_changed.emit(True, "Connected")
                if kind == "status":
                    settings = {"log_type": "26", "ability_id": "ABC", "expiry_warn_s": 5}
                    fields = ["26", "ts", "ABC", "Status", "30", "40000001", "Boss", PLAYER, "Player"]
                else:
                    settings = {"ability_id": "ABCD", "delay_s": 2}
                    fields = ["20", "ts", "40000001", "Boss", "ABCD", "Cast", PLAYER, "Player"]
                    if kind == "sequence":
                        settings.update(delay_s=0, sequence=[{"log_type": "20", "ability_id": "DCBA"}])
                window._triggers = [Trigger(zone_regex=zone, cooldown_s=0, **settings)
                                    for zone in zones]
                with patch("time.monotonic", side_effect=self.clock), patch.object(window, "_fire") as fire:
                    self.line(fields)
                    self.assertEqual(len(window._seq_runners) + len(window._status_timers), len(zones))
                    if metadata is not None:
                        window._ws.zone_changed.emit(*metadata)
                    if kind == "sequence":
                        self.line(["20", "ts", "40000001", "Boss", "DCBA", "Next", PLAYER, "Player"])
                    else:
                        self.clock.value += 31
                        for runner in list(window._seq_runners):
                            runner._expire()
                        for runner in list(window._status_timers):
                            runner._fire()
                    expected = zones if metadata is None else (
                        ("New duty", "") if metadata[0] == 2 else ("Light-heavyweight M1", ""))
                    self.assertEqual([call.args[0].zone_regex for call in fire.call_args_list], list(expected))
                    self.assertFalse(window._seq_runners)
                    self.assertFalse(window._status_timers)

    def test_reconnect_does_not_add_an_unconfirmed_duty_to_the_previous_session(self):
        self.connect()
        window = self.window
        window._prog_tab.start_button.click()
        session = window._prog_sessions.current
        window._ws.status_changed.emit(False, "Disconnected")
        window._ws.status_changed.emit(True, "Connected")
        window._ws.primary_player.emit(int(PLAYER, 16), "Player")
        window._ws.in_combat.emit(False, False)
        window._ws.in_combat.emit(True, True)
        self.line(ability())
        window._ws.zone_changed.emit(2, "New duty")
        self.assertIsNone(window._prog_sessions.current)
        self.assertEqual(session["pulls"], [])
        self.assertIsNotNone(window._dps_meter.current)

    def test_session_start_waits_for_fresh_zone_metadata_after_reconnect(self):
        self.connect()
        window = self.window
        window._ws.status_changed.emit(False, "Disconnected")
        window._ws.status_changed.emit(True, "Connected")
        window._ws.in_combat.emit(False, False)
        window._prog_tab.tick()
        with self.subTest(action="button"):
            self.assertFalse(window._prog_tab.start_button.isEnabled())
        with self.subTest(action="start"):
            window._prog_tab.start_session()
            self.assertIsNone(window._prog_sessions.current)
        window._ws.zone_changed.emit(2, "New duty")
        window._prog_tab.tick()
        self.assertTrue(window._prog_tab.start_button.isEnabled())
        window._prog_tab.start_button.click()
        self.assertEqual(window._prog_sessions.current["zone_id"], 2)

    def test_phase_markers_before_reconnect_metadata_do_not_start_old_session_pulls(self):
        previous = self.phase_pull()
        window = self.window
        session = window._prog_sessions.current
        window._ws.status_changed.emit(False, "Disconnected")
        window._ws.status_changed.emit(True, "Connected")
        window._ws.primary_player.emit(int(PLAYER, 16), "Player")
        window._ws.in_combat.emit(True, True)
        self.line(marker(0))
        self.line(ability())
        window._ws.zone_changed.emit(2, "New duty")
        self.assertEqual(session["pulls"], [previous])

    def test_same_duty_reconnect_resumes_after_idle_and_zone_in_either_order(self):
        self.connect()
        window = self.window
        window._prog_tab.start_button.click()
        session = window._prog_sessions.current
        for index, order in enumerate(permutations(("zone", "idle", "player")), start=1):
            with self.subTest(order=order):
                window._ws.status_changed.emit(False, "Disconnected")
                window._ws.status_changed.emit(True, "Connected")
                actions = {
                    "zone": lambda: window._ws.zone_changed.emit(1, "Test duty"),
                    "idle": lambda: window._ws.in_combat.emit(False, False),
                    "player": lambda: window._ws.primary_player.emit(int(PLAYER, 16), "Player"),
                }
                for kind in order:
                    actions[kind]()
                self.pull()
                self.assertIs(window._prog_sessions.current, session)
                self.assertEqual(len(session["pulls"]), index)
                self.assertTrue(session["pulls"][-1]["complete"])

    def test_phase_tracking_resumes_on_a_fresh_pull_after_zone_confirmation(self):
        self.phase_pull()
        window = self.window
        session = window._prog_sessions.current
        window._ws.status_changed.emit(False, "Disconnected")
        window._ws.status_changed.emit(True, "Connected")
        window._ws.primary_player.emit(int(PLAYER, 16), "Player")
        window._ws.in_combat.emit(True, True)
        self.line(marker(0))
        window._ws.zone_changed.emit(1363, "UMAD")
        window._ws.in_combat.emit(False, False)
        window._ws.in_combat.emit(True, True)
        self.line(marker(0))
        self.line(ability())
        self.assertEqual(len(session["pulls"]), 2)
        self.assertEqual(session["pulls"][-1]["phase_tracking"]["observations"][0]["phase"], "p1")

    def test_oversized_saved_volumes_allow_startup_and_unmute(self):
        settings = deepcopy(self.window._settings)
        for value, master, alert in ((10**400, 100, 50), (1e308, 200, 100), (-1e308, 0, 0)):
            with self.subTest(value=value):
                ac._SETTINGS_FILE.write_text(json.dumps(settings | {
                    "master_volume": value, "overlay_sound_volume": value}))
                with patch.object(mw.MainWindow, "_load_settings", SettingsTabMixin._load_settings):
                    window = mw.MainWindow()
                try:
                    self.assertEqual(window._vol_slider.value(), master)
                    self.assertEqual(window._alert_sound_vol_slider.value(), alert)
                    window._on_mute_toggled(True)
                    window._on_mute_toggled(False)
                    self.assertGreaterEqual(window._alert_sound_amp(), 0.0)
                    self.assertLessEqual(window._alert_sound_amp(), 1.0)
                finally:
                    window.close()

    def test_umad_without_verified_rules_is_explicitly_unavailable(self):
        self.connect()
        self.window._on_ws_zone_changed(1363, "UMAD")
        self.window._on_ws_primary_player(int(PLAYER, 16), "Player")
        tab = self.window._prog_tab
        tab.start_button.click()
        self.pull()
        self.assertEqual(tab.table.item(0, PHASE_COLUMN).text(), "Not recorded")
        self.assertIn("awaiting verified combat recordings", tab.phase_notice.text())
        self.assertEqual(tab.phase_table.rowCount(), 0)

    def test_live_phase_details_preserve_note_cursor_and_bookmark(self):
        pull = self.phase_pull()
        tab = self.window._prog_tab
        tab.note.setPlainText("Keep this edit")
        tab.bookmark.setChecked(True)
        cursor = tab.note.textCursor()
        cursor.setPosition(4)
        tab.note.setTextCursor(cursor)
        self.clock.value += 10
        self.line(marker(2))
        tab.tick()
        self.assertEqual(tab.table.item(0, PHASE_COLUMN).text(), "P3")
        self.assertEqual(tab.phase_table.rowCount(), 5)
        self.assertEqual(tab.phase_table.item(1, 1).text(), "—")
        self.assertEqual(tab.phase_table.item(2, 1).text(), "00:00:10")
        self.assertEqual(tab.note.textCursor().position(), 4)
        self.assertEqual(tab.note.toPlainText(), "Keep this edit")
        self.assertTrue(pull["bookmark"])
        self.window._nav_buttons[4].click()
        self.window.resize(900, 600)
        self.window.show()
        self.app.processEvents()
        self.window.grab().save("/tmp/nyaatriggers-phase-details.png")

    def test_transition_preserves_recap_buffer_and_logical_pull(self):
        pull = self.phase_pull()
        tab = self.window._prog_tab
        self.line(marker(0, transition=True))
        self.window._on_in_combat(False, False)
        self.clock.value += 5
        self.window._on_in_combat(True, True)
        self.line(marker(1))
        self.line(["25", "ts", PLAYER, "Player"])
        tab.tick()
        self.assertEqual(tab.table.rowCount(), 1)
        self.assertEqual(tab.table.item(0, PHASE_COLUMN).text(), "P2")
        recaps, errors = self.window._prog_sessions.recaps.load(self.window._prog_sessions.current["id"], pull["id"])
        self.assertEqual(errors, [])
        self.assertEqual(len(recaps), 1)
        self.assertTrue(any(e["kind"] == "damage" for e in recaps[0]["events"]))
        tab.recap_button.click()
        self.assertEqual(len(self.window._recap_records), 1)

    def test_corrupt_and_legacy_phase_views_clear_old_details(self):
        pull = self.phase_pull()
        tab = self.window._prog_tab
        tab.tick()
        self.assertEqual(tab.phase_table.rowCount(), 5)
        pull["phase_tracking"] = {"version": 999}
        tab.tick()
        self.assertEqual(tab.table.item(0, PHASE_COLUMN).text(), "Unavailable")
        self.assertIn("could not be read", tab.phase_notice.text())
        self.assertEqual(tab.phase_table.rowCount(), 0)
        self.assertTrue(tab.note.isEnabled())
        self.assertTrue(tab.recap_button.isEnabled())
        del pull["phase_tracking"]
        tab.tick()
        self.assertEqual(tab.table.item(0, PHASE_COLUMN).text(), "Not recorded")
        self.assertIn("was not recorded", tab.phase_notice.text())

    def test_timeout_updates_visible_ending_without_changing_notes(self):
        pull = self.phase_pull()
        tab = self.window._prog_tab
        tab.note.setPlainText("Review this transition")
        self.line(marker(0, transition=True))
        self.window._on_in_combat(False, False)
        self.clock.value += 31
        tab.tick()
        self.assertEqual(pull["ending"], "boundary-uncertain")
        self.assertIn("boundary could not be confirmed", tab.phase_notice.text())
        self.assertEqual(tab.note.toPlainText(), "Review this transition")

    def test_unrelated_damage_does_not_build_extra_meter_snapshots(self):
        self.phase_pull()
        with patch.object(self.window._dps_meter, "full_snapshot") as snapshot:
            self.line(ability())
        snapshot.assert_not_called()
        self.assertEqual(self.window._prog_sessions.current["pulls"][0]["ending"], "active")

    def test_navigation_recap_and_prog_end_to_end(self):
        window = self.window
        self.assertEqual(window._stack.count(), 7)
        for index, button in enumerate(window._nav_buttons):
            button.click()
            self.assertEqual(window._stack.currentIndex(), index)
            self.assertEqual(button.shortcut().toString(), f"Ctrl+{index + 1}")
        self.connect()
        tab = window._prog_tab
        tab.tick()
        self.assertTrue(tab.start_button.isEnabled())
        tab.start_button.click()
        self.pull()
        self.assertEqual(tab.table.rowCount(), 1)
        self.assertIn("1 complete pulls", tab.stats.text())
        tab.note.setPlainText("First clean towers")
        tab.bookmark.setChecked(True)
        self.pull()
        self.assertEqual(tab.table.rowCount(), 2)
        tab.table.selectRow(0)
        self.assertEqual(tab.note.toPlainText(), "First clean towers")
        self.assertTrue(tab.bookmark.isChecked())
        self.line(["25", "ts", PLAYER, "Player"])
        self.assertEqual(window._recap_list.count(), 1)
        self.assertGreater(window._recap_table.rowCount(), 0)
        tab.end_button.click()
        saved = ProgSessions(self.temp / "prog_sessions").sessions[0]
        self.assertEqual(saved["pulls"][0]["note"], "First clean towers")
        window._nav_buttons[4].click()
        window.show()
        self.app.processEvents()
        window.grab().save("/tmp/nyaatriggers-prog.png")

    def test_zone_change_and_shutdown_keep_partial_attempts(self):
        self.connect()
        window = self.window
        tab = window._prog_tab
        tab.start_button.click()
        window._on_in_combat(True, True)
        self.line(ability())
        self.clock.value += 5
        window._on_ws_zone_changed(2, "Other duty")
        self.assertIsNone(window._prog_sessions.current)
        pull = window._prog_sessions.sessions[0]["pulls"][0]
        self.assertEqual(pull["ending"], "duty-left")
        self.assertFalse(pull["complete"])

    def test_triggers_resize_keeps_profiles_collapsed_at_bottom(self):
        window = self.window
        window.show()
        for width, height in ((900, 600), (1280, 720), (1280, 1000), (900, 600)):
            with self.subTest(width=width, height=height):
                window.resize(width, height)
                self.app.processEvents()
                page = window._stack.currentWidget()
                self.assertEqual((window.width(), window.height()), (width, height))
                self.assertFalse(window._profile_body.isVisible())
                self.assertEqual(window._profile_panel.geometry().bottom(), page.height() - 1)
                self.assertLess(window._profile_panel.height(), 2 * window._profile_toggle.height())
                self.assertGreater(window._table.height(), page.height() * 0.6)

    def test_profile_dropdown_handles_long_names_and_status(self):
        window = self.window
        window._refresh_table()
        window._profiles = [capture_profile(window, "W" * 200)]
        window._refresh_profiles(window._profiles[0]["id"])
        window.resize(900, 600)
        window.show()
        self.app.processEvents()
        closed_height = window._table.height()
        window._profile_toggle.click()
        self.app.processEvents()
        self.assertTrue(window._profile_picker.isVisible())
        self.assertEqual(window._profile_picker.toolTip(), "W" * 200)
        for control in (window._profile_picker, window._profile_apply,
                        window._profile_new, window._profile_update, window._profile_delete):
            self.assertTrue(window._profile_controls.rect().contains(control.geometry()))
        window._profile_apply.click()
        self.app.processEvents()
        self.assertTrue(window._profile_status.isVisible())
        self.assertIn("Applied profile", window._profile_status.text())
        self.assertEqual((window.width(), window.height()), (900, 600))
        self.assertLess(window._profile_panel.height(), window._stack.height() * 0.3)
        window._profile_toggle.click()
        # Hiding the body posts another layout pass to its parent.
        self.app.processEvents()
        self.app.processEvents()
        self.assertFalse(window._profile_body.isVisible())
        self.assertEqual(window._table.height(), closed_height)

    def test_apply_profile_saves_choices_and_keeps_master_mode(self):
        window = self.window
        trigger = Trigger(id="local", enabled=True, tts_text="Stack")
        window._triggers = [trigger]
        window._local_ids.add(trigger.id)
        profile = capture_profile(window, "Tank")
        window._profiles = [profile]
        window._refresh_profiles(profile["id"])
        trigger.enabled = False
        trigger.tts_text = "Spread"
        window._profile_apply.click()
        self.assertTrue(trigger.enabled)
        self.assertEqual(trigger.tts_text, "Stack")
        self.assertFalse(window._cactbot_mode)
        stored = json.loads(ac.TRIGGERS_LOCAL_FILE.read_text())["triggers"][0]
        self.assertEqual(stored["tts_text"], "Stack")
        self.assertIn("Applied profile", window._profile_status.text())
        window._in_game_combat = True
        trigger.tts_text = "During combat"
        window._profile_apply.click()
        self.assertEqual(trigger.tts_text, "During combat")

    def test_failed_profile_save_restores_unknown_engine_choices(self):
        window = self.window
        trigger = Trigger(id="local", enabled=False, tts_text="Old")
        window._triggers = [trigger]
        profile = capture_profile(window, "Different")
        profile["local"]["local"].update(enabled=True, text="New")
        profile["engines"]["triggevent"]["unknown"] = {"enabled": False, "text": "Changed"}
        before = deepcopy(window._engine_disabled)
        window._profiles = [profile]
        window._refresh_profiles(profile["id"])
        with patch.object(window, "_save_settings", return_value=False):
            window._profile_apply.click()
        self.assertFalse(trigger.enabled)
        self.assertEqual(trigger.tts_text, "Old")
        self.assertEqual(window._engine_disabled, before)
        self.assertNotIn("unknown", window._triggevent_callout_edits)

    def saved_profile(self):
        window = self.window
        trigger = Trigger(id="local", enabled=False, tts_text="Normal setup")
        window._triggers = [trigger]
        window._local_ids.add(trigger.id)
        profile = capture_profile(window, "Tank")
        profile["local"][trigger.id].update(enabled=True, text="Tank setup")
        write_record(window._profiles_dir, profile)
        window._profiles.append(profile)
        window._refresh_profiles()
        return trigger, profile

    def restart_profile_window(self):
        self.window.close()

        def load_triggers(window):
            rows = json.loads(ac.TRIGGERS_LOCAL_FILE.read_text())["triggers"]
            window._triggers = [Trigger.from_dict(row) for row in rows]
            window._local_ids = {trigger.id for trigger in window._triggers}

        with patch.object(mw.MainWindow, "_load_settings", SettingsTabMixin._load_settings), \
                patch.object(mw.MainWindow, "_load_triggers", load_triggers):
            window = mw.MainWindow()
        self.addCleanup(window.close)
        return window

    def test_default_starts_selected_and_cannot_be_deleted_or_overwritten(self):
        trigger, profile = self.saved_profile()
        window = self.window
        self.assertEqual(window._profile_picker.currentData(), DEFAULT_PROFILE_ID)
        self.assertEqual(window._profile_picker.currentText(), "Default")
        self.assertFalse(window._profile_delete.isEnabled())
        self.assertFalse(window._profile_update.isEnabled())
        with patch.object(ac.QMessageBox, "question") as question:
            window._delete_profile()
            window._update_profile()
        question.assert_not_called()
        with patch.object(QInputDialog, "getText", return_value=(" default ", True)):
            window._profile_new.click()
        self.assertEqual(window._profiles, [profile])
        self.assertEqual(trigger.tts_text, "Normal setup")

    def test_default_restores_normal_edits_between_named_profiles(self):
        trigger, profile = self.saved_profile()
        window = self.window
        window._refresh_profiles(profile["id"])
        window._profile_apply.click()
        self.assertEqual(trigger.tts_text, "Tank setup")
        self.assertEqual(window._active_profile_id, profile["id"])
        trigger.tts_text = "Named profile edit"
        window._save_triggers()
        window._refresh_profiles(DEFAULT_PROFILE_ID)
        window._profile_apply.click()
        self.assertEqual(trigger.tts_text, "Normal setup")
        self.assertFalse(trigger.enabled)
        trigger.tts_text = "Updated normal setup"
        window._save_triggers()
        window._refresh_profiles(profile["id"])
        window._profile_apply.click()
        window._refresh_profiles(DEFAULT_PROFILE_ID)
        window._profile_apply.click()
        self.assertEqual(trigger.tts_text, "Updated normal setup")

    def test_named_profile_and_default_survive_restart(self):
        trigger, profile = self.saved_profile()
        self.window._refresh_profiles(profile["id"])
        self.window._profile_apply.click()
        window = self.restart_profile_window()
        self.assertEqual(window._profile_picker.currentData(), profile["id"])
        self.assertEqual(window._triggers[0].tts_text, "Tank setup")
        window._refresh_profiles(DEFAULT_PROFILE_ID)
        window._profile_apply.click()
        self.assertEqual(window._triggers[0].tts_text, "Normal setup")
        self.assertFalse(window._triggers[0].enabled)
        self.assertEqual(json.loads(ac._SETTINGS_FILE.read_text())["active_trigger_profile"], DEFAULT_PROFILE_ID)

    def test_delete_saved_profile_cancel_and_confirm(self):
        trigger, profile = self.saved_profile()
        window = self.window
        window._refresh_profiles(profile["id"])
        path = window._profiles_dir / (profile["id"] + ".json")
        with patch.object(ac.QMessageBox, "question", return_value=ac.QMessageBox.StandardButton.No):
            window._profile_delete.click()
        self.assertTrue(path.exists())
        with patch.object(ac.QMessageBox, "question", return_value=ac.QMessageBox.StandardButton.Yes):
            window._profile_delete.click()
        self.assertFalse(path.exists())
        self.assertEqual(window._profile_picker.count(), 1)
        self.assertEqual(window._profile_picker.currentData(), DEFAULT_PROFILE_ID)
        self.assertEqual(trigger.tts_text, "Normal setup")

    def test_delete_active_profile_restores_default_only_after_combat(self):
        trigger, profile = self.saved_profile()
        window = self.window
        window._refresh_profiles(profile["id"])
        window._profile_apply.click()
        path = window._profiles_dir / (profile["id"] + ".json")
        with patch.object(ac.QMessageBox, "question", return_value=ac.QMessageBox.StandardButton.Yes):
            window._in_game_combat = True
            window._profile_delete.click()
            self.assertTrue(path.exists())
            self.assertEqual(trigger.tts_text, "Tank setup")
            window._in_game_combat = False
            with patch.object(window, "_save_settings", return_value=False):
                window._profile_delete.click()
            self.assertTrue(path.exists())
            self.assertEqual(window._active_profile_id, profile["id"])
            self.assertEqual(trigger.tts_text, "Tank setup")
            window._profile_delete.click()
        self.assertFalse(path.exists())
        self.assertEqual(window._active_profile_id, DEFAULT_PROFILE_ID)
        self.assertEqual(trigger.tts_text, "Normal setup")

    def test_profile_storage_failures_keep_current_choices_and_saved_records(self):
        trigger, profile = self.saved_profile()
        window = self.window
        window._refresh_profiles(profile["id"])
        with patch("nyaatriggers.ui.profiles.write_record", side_effect=OSError("Disk unavailable")):
            window._profile_apply.click()
        self.assertEqual(window._active_profile_id, DEFAULT_PROFILE_ID)
        self.assertEqual(trigger.tts_text, "Normal setup")
        with patch.object(ac.QMessageBox, "question", return_value=ac.QMessageBox.StandardButton.Yes), \
                patch.object(Path, "unlink", side_effect=OSError("Read only")):
            window._profile_delete.click()
        self.assertIn(profile, window._profiles)
        self.assertTrue((window._profiles_dir / (profile["id"] + ".json")).exists())
        self.assertIn("Could not delete profile", window._profile_status.text())

    def test_unreadable_default_is_preserved_and_blocks_switching(self):
        trigger, profile = self.saved_profile()
        self.window._refresh_profiles(profile["id"])
        self.window._profile_apply.click()
        path = self.window._profiles_dir / (DEFAULT_PROFILE_ID + ".json")
        path.write_text("broken default")
        window = self.restart_profile_window()
        window._refresh_profiles(DEFAULT_PROFILE_ID)
        window._profile_apply.click()
        self.assertEqual(window._triggers[0].tts_text, "Tank setup")
        self.assertEqual(window._active_profile_id, profile["id"])
        self.assertEqual(path.read_text(), "broken default")
        self.assertIn("Default could not be loaded", window._profile_status.text())

    @unittest.skipUnless(os.name == "posix" and os.geteuid() != 0, "Needs directory permissions")
    def test_unreadable_profile_directory_does_not_replace_default(self):
        trigger, profile = self.saved_profile()
        self.window._refresh_profiles(profile["id"])
        self.window._profile_apply.click()
        folder = self.window._profiles_dir
        original = (folder / (DEFAULT_PROFILE_ID + ".json")).read_bytes()
        folder.chmod(0)
        try:
            window = self.restart_profile_window()
            self.assertEqual(window._active_profile_id, profile["id"])
            self.assertEqual(window._triggers[0].tts_text, "Tank setup")
            self.assertIn("Default could not be loaded", window._profile_status.text())
        finally:
            folder.chmod(0o700)
        self.assertEqual((folder / (DEFAULT_PROFILE_ID + ".json")).read_bytes(), original)

    def test_missing_active_profile_returns_to_default_on_restart(self):
        trigger, profile = self.saved_profile()
        self.window._refresh_profiles(profile["id"])
        self.window._profile_apply.click()
        (self.window._profiles_dir / (profile["id"] + ".json")).unlink()
        window = self.restart_profile_window()
        self.assertEqual(window._active_profile_id, DEFAULT_PROFILE_ID)
        self.assertEqual(window._triggers[0].tts_text, "Normal setup")
        saved = read_record(window._profiles_dir / (DEFAULT_PROFILE_ID + ".json"))
        self.assertEqual(saved["local"]["local"]["text"], "Normal setup")

    def test_missing_active_and_default_keep_current_choices_and_allow_switching(self):
        trigger, profile = self.saved_profile()
        self.window._refresh_profiles(profile["id"])
        self.window._profile_apply.click()
        for ident in (profile["id"], DEFAULT_PROFILE_ID):
            (self.window._profiles_dir / (ident + ".json")).unlink()
        self.window = self.restart_profile_window()
        self.assertEqual(self.window._active_profile_id, DEFAULT_PROFILE_ID)
        self.assertEqual(self.window._triggers[0].tts_text, "Tank setup")
        self.assertEqual(json.loads(ac._SETTINGS_FILE.read_text())["active_trigger_profile"], DEFAULT_PROFILE_ID)
        with patch.object(QInputDialog, "getText", return_value=("Raid", True)):
            self.window._profile_new.click()
        raid = self.window._profile_picker.currentData()
        self.window._triggers[0].tts_text = "Recovered default"
        self.window._profile_apply.click()
        self.assertEqual(self.window._active_profile_id, raid)
        self.assertEqual(self.window._triggers[0].tts_text, "Tank setup")
        self.window = self.restart_profile_window()
        self.window._refresh_profiles(DEFAULT_PROFILE_ID)
        self.window._profile_apply.click()
        self.assertEqual(self.window._active_profile_id, DEFAULT_PROFILE_ID)
        self.assertEqual(self.window._triggers[0].tts_text, "Recovered default")

    def test_missing_active_does_not_overwrite_an_unreadable_default(self):
        trigger, profile = self.saved_profile()
        self.window._refresh_profiles(profile["id"])
        self.window._profile_apply.click()
        (self.window._profiles_dir / (profile["id"] + ".json")).unlink()
        default = self.window._profiles_dir / (DEFAULT_PROFILE_ID + ".json")
        default.write_text("broken default")
        window = self.restart_profile_window()
        self.assertEqual(window._active_profile_id, profile["id"])
        self.assertEqual(window._triggers[0].tts_text, "Tank setup")
        self.assertEqual(default.read_text(), "broken default")
        self.assertIn("Default could not be loaded", window._profile_status.text())

    def test_missing_profile_recovery_waits_for_a_successful_save(self):
        trigger, profile = self.saved_profile()
        self.window._refresh_profiles(profile["id"])
        self.window._profile_apply.click()
        for ident in (profile["id"], DEFAULT_PROFILE_ID):
            (self.window._profiles_dir / (ident + ".json")).unlink()
        with patch.object(mw.MainWindow, "_save_settings", return_value=False):
            window = self.restart_profile_window()
        self.assertEqual(window._active_profile_id, profile["id"])
        self.assertEqual(window._triggers[0].tts_text, "Tank setup")
        self.assertEqual(json.loads(ac._SETTINGS_FILE.read_text())["active_trigger_profile"], profile["id"])
        window._restore_missing_profile()
        self.assertEqual(window._active_profile_id, DEFAULT_PROFILE_ID)

    def test_nameless_duty_transition_ends_session(self):
        self.connect()
        window = self.window
        window._prog_tab.start_button.click()
        window._on_ws_zone_changed(2, "")
        self.assertIsNone(window._prog_sessions.current)

    def test_live_duration_and_name_edits_survive_new_pull(self):
        self.connect()
        window = self.window
        tab = window._prog_tab
        tab.start_button.click()
        tab.name.setText("Static prog")
        tab.name.textEdited.emit("Static prog")
        window._on_in_combat(True, True)
        self.line(ability())
        self.clock.value += 20
        tab.tick()
        self.assertEqual(tab.table.item(0, 2).text(), "00:00:20")
        self.assertEqual(tab.name.text(), "Static prog")

    def test_saved_pull_navigation_survives_restart_and_live_updates(self):
        self.connect()
        window = self.window
        tab = window._prog_tab
        tab.start_button.click()
        window._on_in_combat(True, True)
        self.line(ability())
        self.line(["25", "ts", PLAYER, "First player"])
        window._on_in_combat(False, False)
        tab.note.setPlainText("Review this death")
        tab.recap_button.click()
        self.assertIs(window._stack.currentWidget(), window._death_recap_tab)
        self.assertIn("Pull 1", window._recap_scope.text())
        self.assertEqual(window._recap_records[0]["name"], "First player")
        window._recap_back.click()
        self.assertIs(window._stack.currentWidget(), tab)
        self.assertEqual(tab.note.toPlainText(), "Review this death")
        window._on_in_combat(True, True)
        self.line(ability())
        self.clock.value += 2
        self.line(["25", "ts", PLAYER, "Second player"])
        self.assertEqual(len(window._recap_records), 1)
        self.assertEqual(window._recap_records[0]["name"], "First player")
        window._recap_recent.click()
        self.assertEqual(window._recap_list.count(), 2)
        tab.end_button.click()
        window._death_recap.deaths.clear()
        restarted = ProgSessions(self.temp / "prog_sessions")
        window._prog_sessions = tab.sessions = restarted
        tab.refresh()
        tab.table.selectRow(0)
        tab.recap_button.click()
        self.assertEqual(window._recap_records[0]["name"], "First player")
        self.assertGreater(window._recap_table.rowCount(), 0)
        window.show()
        self.app.processEvents()
        window.grab().save("/tmp/nyaatriggers-saved-recap.png")

    def test_empty_and_legacy_recaps_clear_previous_details(self):
        self.connect()
        window = self.window
        tab = window._prog_tab
        self.assertFalse(tab.recap_button.isEnabled())
        tab.start_button.click()
        self.pull()
        self.line(["25", "ts", PLAYER, "Player"])
        self.assertGreater(window._recap_table.rowCount(), 0)
        self.pull()
        tab.table.selectRow(1)
        tab.recap_button.click()
        self.assertEqual(window._recap_table.rowCount(), 0)
        self.assertIn("No death recaps", window._recap_statuses.text())
        del tab.pull["recap_count"]
        tab.recap_button.click()
        self.assertIn("before saved death recaps", window._recap_notice.text())
        self.assertEqual(window._recap_list.count(), 0)

    def test_active_saved_view_tracks_deaths_without_changing_selection(self):
        self.connect()
        window = self.window
        tab = window._prog_tab
        tab.start_button.click()
        window._on_in_combat(True, True)
        self.line(ability())
        tab.recap_button.click()
        self.line(["25", "ts", PLAYER, "First death"])
        selected = window._recap_list.currentItem().data(Qt.ItemDataRole.UserRole)
        self.clock.value += 2
        self.line(ability())
        self.line(["25", "ts", PLAYER, "Second death"])
        self.assertEqual(window._recap_list.count(), 2)
        self.assertEqual(window._recap_list.currentItem().data(Qt.ItemDataRole.UserRole), selected)
        self.assertEqual(len(window._prog_sessions.recaps.load(tab.session["id"], tab.pull["id"])[0]), 2)

    def test_recap_save_and_load_errors_are_visible_without_losing_history(self):
        self.connect()
        window = self.window
        tab = window._prog_tab
        tab.start_button.click()
        window._on_in_combat(True, True)
        self.line(ability())
        with patch("nyaatriggers.recap_store.write_record", side_effect=OSError("Disk failed")):
            self.line(["25", "ts", PLAYER, "Player"])
            tab.recap_button.click()
            tab.tick()
            self.assertIn("Disk failed", tab.status.text())
            self.assertIn("Disk failed", window._recap_notice.text())
            self.assertEqual(window._recap_list.count(), 1)
        tab.flush()
        tab.tick()
        self.assertNotIn("Disk failed", window._recap_notice.text())
        path = next((self.temp / "prog_sessions").glob("recaps/*/*/*.json"))
        path.write_text("bad json")
        tab.recap_button.click()
        self.assertIn("could not be read", window._recap_notice.text())
        self.assertIn("Only 0 of 1", window._recap_notice.text())
        self.assertEqual(window._recap_table.rowCount(), 0)
        self.assertEqual(path.read_text(), "bad json")

    def test_normal_pull_end_resets_observations_before_next_pull(self):
        self.connect()
        window = self.window
        window._prog_tab.start_button.click()
        self.pull()
        window._on_in_combat(True, True)
        self.line(["25", "ts", PLAYER, "Player"])
        self.assertEqual(window._recap_records[0]["events"], [])

    def test_crash_after_death_keeps_observed_pull_duration_and_death_count(self):
        self.connect()
        window = self.window
        window._prog_tab.start_button.click()
        window._on_in_combat(True, True)
        self.line(ability())
        self.clock.value += 12
        self.line(["25", "ts", PLAYER, "Player"])
        saved = ProgSessions(self.temp / "prog_sessions").sessions[0]["pulls"][0]
        self.assertEqual(saved["deaths"], 1)
        self.assertEqual(saved["duration"], 12)

    def test_next_pull_keeps_preparation_statuses_and_healing_in_saved_recaps(self):
        self.connect()
        window = self.window
        tab = window._prog_tab
        tab.start_button.click()
        self.pull()
        self.clock.value += 3
        self.line(["26", "ts", "ABC", "Preparation buff", "30", PLAYER, "Player", PLAYER, "Player"])
        self.line(ability(pairs=[("04", "1F40000")]))
        self.line(["24", "ts", PLAYER, "Player", "HoT", "0", "A"])
        window._on_in_combat(True, True)
        self.clock.value += 1
        self.line(ability())
        self.line(["25", "ts", PLAYER, "Player"])
        death = window._recap_records[0]
        self.assertEqual([s["name"] for s in death["statuses"]], ["Preparation buff"])
        self.assertEqual([e["kind"] for e in death["events"]], ["gained", "heal", "hot", "damage"])
        self.assertEqual(death["events"][0]["time"], -1)
        saved, errors = window._prog_sessions.recaps.load(tab.session["id"], tab.session["pulls"][-1]["id"])
        self.assertEqual(errors, [])
        self.assertEqual(saved[0]["events"], death["events"])
        self.assertEqual(saved[0]["statuses"], [{"name": "Preparation buff", "source": "Player"}])

    def test_live_duration_and_break_time_are_checkpointed_without_edits(self):
        self.connect()
        window = self.window
        tab = window._prog_tab
        tab.start_button.click()
        window._on_in_combat(True, True)
        self.line(ability())
        with patch("nyaatriggers.prog_session.write_record", wraps=write_record) as save:
            for _ in range(60):
                self.clock.value += 1
                tab.tick()
        self.assertEqual(save.call_count, 60 // CHECKPOINT_SECONDS)
        saved = ProgSessions(self.temp / "prog_sessions").sessions[0]
        self.assertEqual(saved["pulls"][0]["duration"], 60)
        self.assertEqual(saved["pulls"][0]["ending"], "program-closed")
        self.assertEqual(saved["elapsed"], 60)
        self.line(ability())
        window._on_in_combat(False, False)
        self.clock.value += CHECKPOINT_SECONDS
        tab.tick()
        saved = ProgSessions(self.temp / "prog_sessions").sessions[0]
        self.assertEqual(saved["elapsed"], 60 + CHECKPOINT_SECONDS)
        self.assertEqual(saved["pulls"][0]["duration"], 60)

    def test_late_instant_death_restores_the_pull_row_and_saved_recap(self):
        self.connect()
        window = self.window
        tab = window._prog_tab
        tab.start_button.click()
        window._on_in_combat(True, True)
        self.line(ability(pairs=[("33", "0")]))
        self.clock.value += 1
        window._on_in_combat(False, False)
        self.assertEqual(tab.table.rowCount(), 0)
        self.clock.value += 0.2
        self.line(["25", "ts", PLAYER, "Player"])
        self.assertEqual(tab.table.rowCount(), 1)
        self.assertEqual(tab.table.item(0, DEATHS_COLUMN).text(), "1")
        self.assertTrue(tab.recap_button.isEnabled())
        tab.recap_button.click()
        self.assertEqual(window._recap_records[0]["events"][0]["kind"], "instant-death")
        loaded = ProgSessions(self.temp / "prog_sessions")
        pull = loaded.sessions[0]["pulls"][0]
        self.assertFalse(pull["complete"])
        self.assertEqual(pull["recap_count"], 1)
        self.assertEqual(len(loaded.recaps.load(tab.session["id"], pull["id"])[0]), 1)

    def test_duplicate_deaths_agree_between_prog_meter_and_recaps(self):
        self.connect()
        window = self.window
        tab = window._prog_tab
        tab.start_button.click()
        window._on_in_combat(True, True)
        self.line(ability())
        death = ["25", "ts", PLAYER, "Player"]
        self.line(death)
        self.line(death)
        tab.tick()
        self.assertEqual(tab.table.item(0, DEATHS_COLUMN).text(), "1")
        self.assertEqual(tab.pull["recap_count"], 1)
        self.assertEqual(window._dps_meter.full_snapshot()["Encounter"]["deaths"], 1)
        self.clock.value += 1.1
        self.line(death)
        window._on_in_combat(False, False)
        self.assertEqual(tab.pull["deaths"], 2)
        self.assertEqual(tab.pull["recap_count"], 2)
        window._on_in_combat(True, True)
        self.line(ability())
        self.line(death)
        tab.tick()
        pull = tab.session["pulls"][-1]
        self.assertEqual(pull["deaths"], 1)
        self.assertEqual(pull["recap_count"], 1)

    def test_duplicate_death_across_phase_transition_is_counted_once(self):
        pull = self.phase_pull()
        window = self.window
        tab = window._prog_tab
        death = ["25", "ts", PLAYER, "Player"]
        self.line(death)
        self.line(marker(0, transition=True))
        window._on_in_combat(False, False)
        self.clock.value += 0.2
        window._on_in_combat(True, True)
        self.line(marker(1))
        self.line(death)
        tab.tick()
        self.assertEqual(len(tab.session["pulls"]), 1)
        self.assertEqual(pull["deaths"], 1)
        self.assertEqual(pull["recap_count"], 1)
        self.assertEqual(window._dps_meter.full_snapshot()["Encounter"]["deaths"], 0)
        self.clock.value += 1.1
        self.line(["25", "later", PLAYER, "Player"])
        window._on_in_combat(False, False)
        self.assertEqual(pull["deaths"], 2)
        loaded = ProgSessions(self.temp / "prog_sessions", definitions=window._prog_sessions.definitions)
        restored = loaded.sessions[0]["pulls"][0]
        self.assertEqual(restored["deaths"], 2)
        self.assertEqual(restored["recap_count"], 2)
        self.assertEqual(len(loaded.recaps.load(tab.session["id"], pull["id"])[0]), 2)

    def test_death_cutoff_uses_arrival_time_despite_dispatch_delays(self):
        self.connect()
        window = self.window
        tab = window._prog_tab
        tab.start_button.click()
        death = ["25", "ts", PLAYER, "Player"]
        process = window._dps_meter.process

        def delayed_process(*args, **kwargs):
            self.clock.value += before_meter
            process(*args, **kwargs)
            self.clock.value += 0.002

        for before_meter, delay, expected in ((0, 0.999, 1), (0.002, 0.999, 1),
                                              (0.002, 1.0, 2), (0.002, 1.001, 2)):
            with self.subTest(before_meter=before_meter, delay=delay):
                window._on_in_combat(False, False)
                self.clock.value += 3
                window._on_in_combat(True, True)
                self.line(ability())
                self.line(death)
                pull = tab.session["pulls"][-1]
                self.clock.value += delay
                with patch.object(window._dps_meter, "process", side_effect=delayed_process):
                    self.line(death)
                tab.tick()
                self.assertEqual(window._dps_meter.full_snapshot()["Encounter"]["deaths"], expected)
                self.assertEqual(pull["deaths"], expected)
                self.assertEqual(pull["recap_count"], expected)
                loaded = ProgSessions(self.temp / "prog_sessions")
                recaps, errors = loaded.recaps.load(tab.session["id"], pull["id"])
                self.assertEqual(errors, [])
                self.assertEqual(len(recaps), expected)
                self.assertEqual(loaded.sessions[0]["pulls"][-1]["deaths"], expected)
                window._on_in_combat(False, False)

    def test_separate_combat_flag_edges_keep_pull_deaths_and_preparation(self):
        self.connect()
        window = self.window
        tab = window._prog_tab
        tab.start_button.click()
        for act_first in (True, False):
            with self.subTest(act_first=act_first):
                self.clock.value += 3
                self.line(["26", "ts", "ABC", "Preparation buff", "30", PLAYER, "Player", PLAYER, "Player"])
                window._on_in_combat(act_first, not act_first)
                self.line(ability())
                pull = tab.session["pulls"][-1]
                window._on_in_combat(True, True)
                self.assertEqual(window._dps_meter.full_snapshot()["Encounter"]["pull_id"], pull["id"])
                window._on_in_combat(not act_first, act_first)
                self.clock.value += 0.2
                self.line(["25", "ts", PLAYER, "Player"])
                window._on_in_combat(False, False)
                self.line(["25", "ts", PLAYER, "Player"])
                self.assertEqual(tab.session["pulls"][-1]["id"], pull["id"])
                self.assertEqual(pull["deaths"], 1)
                self.assertEqual(pull["recap_count"], 1)
                self.assertEqual(pull["ending"], "combat-ended")
                self.assertIsNone(window._dps_meter.current)
                saved, errors = window._prog_sessions.recaps.load(tab.session["id"], pull["id"])
                self.assertEqual(errors, [])
                self.assertEqual(len(saved), 1)
                self.assertEqual(saved[0]["statuses"], [{"name": "Preparation buff", "source": "Player"}])
                self.assertEqual([e["kind"] for e in saved[0]["events"]], ["gained", "damage"])
        self.assertEqual(len(tab.session["pulls"]), 2)

    def test_mixed_combat_flag_message_finishes_and_starts_distinct_pulls(self):
        self.connect()
        window = self.window
        tab = window._prog_tab
        tab.start_button.click()
        for act_first in (True, False):
            with self.subTest(act_first=act_first):
                self.clock.value += 3
                window._on_in_combat(act_first, not act_first)
                self.line(ability())
                self.line(["25", "ts", PLAYER, "Player"])
                first = tab.session["pulls"][-1]
                window._on_in_combat(not act_first, act_first)
                second = tab.session["pulls"][-1]
                self.assertNotEqual(first["id"], second["id"])
                self.assertEqual(first["ending"], "combat-ended")
                self.line(ability(pairs=[("03", "7D00000")]))
                self.line(["25", "ts", PLAYER, "Player"])
                tab.tick()
                self.assertEqual(window._dps_meter.full_snapshot()["Encounter"]["deaths"], 1)
                window._on_in_combat(False, False)
                loaded = ProgSessions(self.temp / "prog_sessions")
                for pull, amount in ((first, 1000), (second, 2000)):
                    self.assertEqual(pull["deaths"], 1)
                    self.assertEqual(pull["recap_count"], 1)
                    saved, errors = loaded.recaps.load(tab.session["id"], pull["id"])
                    self.assertEqual(errors, [])
                    self.assertEqual(len(saved), 1)
                    self.assertEqual([(e["kind"], e["amount"]) for e in saved[0]["events"]], [("damage", amount)])
        self.assertEqual(len(tab.session["pulls"]), 4)

    def test_separate_combat_flags_preserve_phase_transition_history(self):
        pull = self.phase_pull()
        window = self.window
        self.line(["26", "ts", "ABC", "Transition buff", "30", PLAYER, "Player", PLAYER, "Player"])
        self.line(marker(0, transition=True))
        window._on_in_combat(True, False)
        window._on_in_combat(False, False)
        self.clock.value += 3
        window._on_in_combat(False, True)
        window._on_in_combat(True, True)
        self.line(marker(1))
        self.line(["25", "ts", PLAYER, "Player"])
        window._on_in_combat(False, True)
        window._on_in_combat(False, False)
        self.assertEqual(window._prog_sessions.current["pulls"], [pull])
        self.assertEqual(pull["deaths"], 1)
        self.assertEqual(pull["recap_count"], 1)
        saved, errors = window._prog_sessions.recaps.load(window._prog_sessions.current["id"], pull["id"])
        self.assertEqual(errors, [])
        self.assertEqual(saved[0]["statuses"], [{"name": "Transition buff", "source": "Player"}])
        self.assertTrue(any(e["kind"] == "damage" for e in saved[0]["events"]))

    def test_death_in_phase_gap_is_not_counted_again_in_the_next_segment(self):
        pull = self.phase_pull()
        window = self.window
        self.line(marker(0, transition=True))
        window._on_in_combat(False, False)
        death = ["25", "ts", PLAYER, "Player"]
        self.line(death)
        self.clock.value += 0.2
        window._on_in_combat(True, True)
        self.line(marker(1))
        self.line(death)
        window._prog_tab.tick()
        self.assertEqual(pull["deaths"], 1)
        self.assertEqual(pull["recap_count"], 1)
        self.assertEqual(window._dps_meter.full_snapshot()["Encounter"]["deaths"], 0)

    def test_party_wipe_deduplicates_each_player_and_preserves_other_preparation(self):
        self.connect()
        window = self.window
        tab = window._prog_tab
        players = [f"{int(PLAYER, 16) + index:X}" for index in range(8)]
        for actor in players:
            self.line(["03", "ts", actor, actor, "18", "100", "0"])
        tab.start_button.click()
        window._on_in_combat(True, True)
        for index, actor in enumerate(players):
            self.line(ability(target=actor, pairs=[("03", f"{(index + 1) << 16:X}")]))
        for actor in players[:4]:
            for _ in range(2):
                self.line(["25", "ts", actor, actor])
        self.line(["33", "ts", "0", "4000000F"])
        for actor in players:
            self.line(["26", "ts", "ABC", "Preparation buff", "30", PLAYER, "Player", actor, actor])
        for actor in players[4:]:
            for _ in range(2):
                self.line(["25", "ts", actor, actor])
        tab.tick()
        first = tab.session["pulls"][0]
        self.assertEqual(first["deaths"], 8)
        self.assertEqual(first["recap_count"], 8)
        saved, errors = window._prog_sessions.recaps.load(tab.session["id"], first["id"])
        self.assertEqual(errors, [])
        self.assertEqual({d["actor"] for d in saved}, {int(actor, 16) for actor in players})
        self.assertEqual({d["actor"]: d["events"][0]["amount"] for d in saved},
                         {int(actor, 16): index + 1 for index, actor in enumerate(players)})
        window._on_in_combat(False, False)
        window._on_in_combat(True, True)
        self.line(ability())
        for actor in (players[0], players[4]):
            self.line(["25", "next", actor, actor])
        second = tab.session["pulls"][-1]
        saved, errors = window._prog_sessions.recaps.load(tab.session["id"], second["id"])
        self.assertEqual(errors, [])
        statuses = {d["actor"]: [s["name"] for s in d["statuses"]] for d in saved}
        self.assertEqual(statuses[int(players[0], 16)], ["Preparation buff"])
        self.assertEqual(statuses[int(players[4], 16)], [])
        self.assertEqual(second["recap_count"], 2)

    def test_pending_note_saves_to_its_pull_when_selection_changes(self):
        self.connect()
        tab = self.window._prog_tab
        tab.start_button.click()
        self.pull()
        self.pull()
        tab.table.selectRow(0)
        tab.note.setPlainText("First pull note")
        self.assertTrue(tab.save_timer.isActive())
        tab.table.selectRow(1)
        loaded = ProgSessions(self.temp / "prog_sessions").sessions[0]
        self.assertEqual([p["note"] for p in loaded["pulls"]], ["First pull note", ""])
        self.assertEqual(tab.note.toPlainText(), "")
        tab.note.setPlainText("Second pull note")
        tab.open_recaps()
        loaded = ProgSessions(self.temp / "prog_sessions").sessions[0]
        self.assertEqual([p["note"] for p in loaded["pulls"]], ["First pull note", "Second pull note"])

    def test_pending_note_survives_switching_sessions_and_background_collection(self):
        self.connect()
        tab = self.window._prog_tab
        tab.start_button.click()
        self.pull()
        first = tab.session
        tab.end_button.click()
        tab.start_button.click()
        self.pull()
        second = tab.session
        tab.picker.setCurrentIndex(tab.picker.findData(first["id"]))
        tab.note.setPlainText("Historical note")
        self.assertTrue(tab.save_timer.isActive())
        self.pull()
        self.assertIs(tab.session, first)
        self.assertEqual(tab.note.toPlainText(), "Historical note")
        tab.note.setPlainText("Final historical note")
        tab.picker.setCurrentIndex(tab.picker.findData(second["id"]))
        loaded = {s["id"]: s for s in ProgSessions(self.temp / "prog_sessions").sessions}
        self.assertEqual(loaded[first["id"]]["pulls"][0]["note"], "Final historical note")
        self.assertEqual([p["note"] for p in loaded[second["id"]]["pulls"]], ["", ""])

    def test_shutdown_flushes_a_pending_note_on_a_historical_session(self):
        self.connect()
        tab = self.window._prog_tab
        tab.start_button.click()
        self.pull()
        first = tab.session
        tab.end_button.click()
        tab.start_button.click()
        self.window._on_in_combat(True, True)
        self.line(ability())
        second = tab.session
        tab.picker.setCurrentIndex(tab.picker.findData(first["id"]))
        tab.note.setPlainText("Save on close")
        self.assertTrue(tab.save_timer.isActive())
        self.window.close()
        loaded = {s["id"]: s for s in ProgSessions(self.temp / "prog_sessions").sessions}
        self.assertEqual(loaded[first["id"]]["pulls"][0]["note"], "Save on close")
        self.assertEqual(loaded[second["id"]]["pulls"][0]["note"], "")
        self.assertEqual(loaded[second["id"]]["pulls"][0]["ending"], "program-closed")

    def test_recent_recap_eviction_keeps_matching_details_and_saved_history(self):
        self.connect()
        window = self.window
        tab = window._prog_tab
        tab.start_button.click()
        window._on_in_combat(True, True)
        for index in range(MAX_DEATHS):
            self.clock.value += 1.1
            self.line(ability(pairs=[("03", f"{(index + 1) << 16:X}")]))
            self.line(["25", "ts", PLAYER, f"Death {index + 1}"])
        window._recap_list.setCurrentRow(MAX_DEATHS - 1)
        oldest = window._recap_list.currentItem().data(Qt.ItemDataRole.UserRole)
        self.assertEqual(window._recap_table.item(0, 4).text(), "1")
        self.clock.value += 1.1
        self.line(ability(pairs=[("03", "3E70000")]))
        self.line(["25", "ts", PLAYER, "Newest death"])
        self.assertEqual(window._recap_list.count(), MAX_DEATHS)
        selected = window._recap_list.currentItem().data(Qt.ItemDataRole.UserRole)
        self.assertNotEqual(selected, oldest)
        row = window._recap_list.currentRow()
        self.assertEqual(window._recap_records[row]["id"], selected)
        self.assertEqual(window._recap_records[row]["name"], "Newest death")
        self.assertEqual(window._recap_table.item(0, 4).text(), "999")
        tab.open_recaps()
        self.assertEqual(window._recap_list.count(), MAX_DEATHS + 1)
        oldest_row = next(i for i, d in enumerate(window._recap_records) if d["id"] == oldest)
        window._recap_list.setCurrentRow(oldest_row)
        self.assertEqual(window._recap_table.item(0, 4).text(), "1")
        self.clock.value += 1.1
        self.line(ability(pairs=[("03", "10000")]))
        self.line(["25", "ts", PLAYER, "Another death"])
        self.assertEqual(window._recap_list.currentItem().data(Qt.ItemDataRole.UserRole), oldest)
        self.assertEqual(window._recap_table.item(0, 4).text(), "1")
        loaded = ProgSessions(self.temp / "prog_sessions")
        saved, errors = loaded.recaps.load(tab.session["id"], tab.pull["id"])
        self.assertEqual(errors, [])
        self.assertEqual(len(saved), MAX_DEATHS + 2)
        self.assertIn(oldest, {d["id"] for d in saved})

    def test_restored_empty_pull_reports_save_failures_and_recovers_without_duplicates(self):
        self.connect()
        window = self.window
        tab = window._prog_tab
        for target in ("nyaatriggers.recap_store.write_record", "nyaatriggers.prog_session.write_record"):
            with self.subTest(target=target):
                tab.start_button.click()
                window._on_in_combat(True, True)
                self.line(ability(pairs=[("33", "0")]))
                window._on_in_combat(False, False)
                self.clock.value += 0.2
                with patch(target, side_effect=OSError("Storage unavailable")):
                    self.line(["25", "ts", PLAYER, "Player"])
                    tab.open_recaps()
                    tab.tick()
                    self.assertIn("Storage unavailable", tab.status.text())
                    self.assertEqual(tab.table.rowCount(), 1)
                    self.assertEqual(window._recap_list.count(), 1)
                    ident = window._recap_records[0]["id"]
                tab.flush()
                tab.flush()
                tab.tick()
                self.assertNotIn("Storage unavailable", tab.status.text())
                self.assertNotIn("Storage unavailable", window._recap_notice.text())
                self.assertEqual(tab.pull["deaths"], 1)
                self.assertEqual(tab.pull["recap_count"], 1)
                loaded = ProgSessions(self.temp / "prog_sessions")
                saved, errors = loaded.recaps.load(tab.session["id"], tab.pull["id"])
                self.assertEqual(errors, [])
                self.assertEqual([d["id"] for d in saved], [ident])
                session = next(s for s in loaded.sessions if s["id"] == tab.session["id"])
                self.assertEqual(len(session["pulls"]), 1)
                self.assertEqual(session["pulls"][0]["recap_count"], 1)
                tab.end_session()
                window._show_recent_recaps()

    def test_zone_and_disconnect_stop_late_deaths_reaching_old_pull(self):
        self.connect()
        window = self.window
        window._prog_tab.start_button.click()
        self.pull()
        session = window._prog_sessions.current
        pull = session["pulls"][0]
        window._ws.status_changed.emit(False, "Lost feed")
        self.line(["25", "ts", PLAYER, "After disconnect"])
        self.assertEqual(window._prog_sessions.recaps.load(session["id"], pull["id"]), ([], []))
        window._on_ws_zone_changed(2, "Other duty")
        self.clock.value += 2
        self.line(["25", "ts", PLAYER, "Other duty"])
        self.assertIsNone(window._prog_sessions.current)
        self.assertEqual(pull["recap_count"], 0)

    def test_late_saved_death_updates_the_finished_pull_row(self):
        self.connect()
        window = self.window
        tab = window._prog_tab
        tab.start_button.click()
        self.pull()
        self.line(["25", "ts", PLAYER, "Player"])
        tab.tick()
        self.assertEqual(tab.table.item(0, DEATHS_COLUMN).text(), "1")


if __name__ == "__main__":
    unittest.main()
