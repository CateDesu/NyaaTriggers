import builtins
import io
from contextlib import ExitStack, contextmanager
import json
import os
from pathlib import Path
from queue import Queue
import subprocess
import sys
import sysconfig
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QCoreApplication, QEventLoop, QTimer

from nyaatriggers import tts
from nyaatriggers.sequential import SequentialRunner
from nyaatriggers.timeline_engine import TimelineEngine
from nyaatriggers.timeline_parser import parse
from nyaatriggers.trigger_engine import Trigger


APP = QCoreApplication.instance() or QCoreApplication([])
ROOT = Path(__file__).resolve().parents[1]


class MechanicTranslationTests(unittest.TestCase):
    def setUp(self):
        from nyaatriggers.app_common import _compile_phrase_patterns
        from nyaatriggers.ui.settings_tab import SettingsTabMixin

        data = json.loads((ROOT / "assets/callouts_ja.json").read_text())
        self.host = types.SimpleNamespace(
            _settings={"callouts_localized": True},
            _callouts_phrases_ja=data["phrases"],
            _callouts_phrases_ja_patterns=_compile_phrase_patterns(data["phrases"]),
            _callouts_readings=data["readings"],
            _callouts_reading_patterns=_compile_phrase_patterns(
                data["readings"], minimum_literal=1, restrict_choices=False),
            _triggevent=None, _triggevent_mode=True, _connected=True,
            _triggevent_last_spoken={}, _dedup_speak_gate=lambda *args: True)
        self.host._localize_text = types.MethodType(SettingsTabMixin._localize_text, self.host)
        self.host._reading_for = types.MethodType(SettingsTabMixin._reading_for, self.host)

    def test_p6s_bait_keeps_the_native_safe_corner_in_both_outputs(self):
        from nyaatriggers.ui.engines import EnginesMixin

        for corner, display, reading in (
                ("Northeast", "北東", "ほくとう"),
                ("Southeast", "南東", "なんとう"),
                ("Southwest", "南西", "なんせい"),
                ("Northwest", "北西", "ほくせい")):
            with self.subTest(corner=corner):
                alerts = []
                self.host._emit_alert = lambda *args: alerts.append(args)
                english = "Bait middle, then " + corner
                translated = "中央誘導→" + display
                EnginesMixin._on_triggevent_callout(self.host, english, "info")
                self.assertEqual(alerts, [(translated, "info")])
                with patch("nyaatriggers.ui.engines.speak") as speak:
                    EnginesMixin._triggevent_speak(self.host, english)
                speak.assert_called_once_with(translated, reading="ちゅうおうゆうどう→" + reading)

    def test_unknown_safe_corner_stays_intact(self):
        text = "Bait middle, then custom marker A"
        self.assertEqual(self.host._localize_text(text), "中央誘導→custom marker A")

    def test_umad_direction_calls_keep_their_resolved_instructions(self):
        from nyaatriggers.ui.engines import EnginesMixin

        for text, display, reading in (
                ("Spread In Thunder, Look Away", "散開、雷の中、視線を避ける",
                 "さんかい、かみなりのなか、しせんをさける"),
                ("Spread Southeast", "散開 南東", "さんかい なんとう"),
                ("Center to South, North, Northwest", "中央から南、北、北西",
                 "ちゅうおうからみなみ、きた、ほくせい"),
                ("Starting Northwest -> Clockwise", "開始 北西 → 時計回り",
                 "かいし ほくせい → とけいまわり"),
                ("Starting Southeast -> CCW", "開始 南東 → 反時計回り",
                 "かいし なんとう → はんとけいまわり"),
                ("Stillness and Stack In Ice (with Daymyx Hylen, Mimzy Marleapa)",
                 "静止、氷の中でDaymyx Hylen, Mimzy Marleapaと頭割り",
                 "せいし、こおりのなかでDaymyx Hylen, Mimzy Marleapaとあたまわり"),
                ("Motion and Stack Out of Ice (with Mia Hunt)",
                 "動く、氷の外でMia Huntと頭割り", "うごく、こおりのそとでMia Huntとあたまわり")):
            with self.subTest(text=text):
                with patch("nyaatriggers.ui.engines.speak") as speak:
                    EnginesMixin._triggevent_speak(self.host, text)
                speak.assert_called_once_with(display, reading=reading)

    def test_actor_and_duration_only_translations_still_work(self):
        from nyaatriggers.app_common import _compile_phrase_patterns

        self.assertEqual(self.host._localize_text("Stack with Mia Hunt"), "Mia Huntと頭割り")
        phrases = {"Away from {event.source} ({event.estimatedRemainingDuration})": "離れる"}
        self.host._callouts_phrases_ja = phrases
        self.host._callouts_phrases_ja_patterns = _compile_phrase_patterns(phrases)
        self.assertEqual(self.host._localize_text("Away from Tank (5.0)"), "離れる")

    def test_specific_actor_callout_keeps_the_following_mechanic(self):
        for text, expected in (
                ("Stack with Mia Hunt, Bait Between", "Mia Huntと頭割り、間で誘導"),
                ("Stack with Sasha Kurone, Bait Away", "Sasha Kuroneと頭割り、離れて誘導"),
                ("Stack with Mia Hunt", "Mia Huntと頭割り")):
            with self.subTest(text=text):
                self.assertEqual(self.host._localize_text(text), expected)

    def test_umad_roles_and_prelude_cannot_be_swallowed_by_a_wildcard(self):
        for text, expected in (
                ("Circle, DPS have cone", "サークル、DPSに扇"),
                ("Cone, Supports have cone", "扇、タンク・ヒーラーに扇"),
                ("Stack, Supports have cone", "頭割り、タンク・ヒーラーに扇"),
                ("Stack on DPS", "DPSに頭割り"),
                ("Stack on Support", "タンク・ヒーラーに頭割り"),
                ("Bait Blizzards then Stacks", "氷を誘導してから頭割り")):
            with self.subTest(text=text):
                self.assertEqual(self.host._localize_text(text), expected)

    def test_direction_translations_still_respect_the_language_switch(self):
        self.host._settings["callouts_localized"] = False
        text = "Bait middle, then Northeast"
        self.assertEqual(self.host._localize_text(text), text)

    def test_older_cache_does_not_hide_bundle_mechanic_corrections(self):
        from nyaatriggers import app_common
        from nyaatriggers.ui.triggers_tab import TriggersTabMixin

        bundle = json.loads((ROOT / "assets/callouts_ja.json").read_text())
        cache = json.loads(json.dumps(bundle))
        cache["app_version"] = "1.3.0"
        cache["phrases"]["Spread {bigSafe}"] = "散開"
        with tempfile.TemporaryDirectory() as folder:
            cache_path = Path(folder) / "cache.json"
            bundle_path = Path(folder) / "bundle.json"
            cache_path.write_text(json.dumps(cache))
            bundle_path.write_text(json.dumps(bundle))
            with patch.object(app_common, "_CALLOUTS_JA_CACHE", cache_path), \
                    patch.object(app_common, "_CALLOUTS_JA_BUNDLE", bundle_path), \
                    patch("nyaatriggers.ui.triggers_tab.set_readings"):
                TriggersTabMixin._load_cached_callouts_ja(self.host)
        text = "Spread In Thunder, Look Away"
        self.assertEqual(self.host._localize_text(text), "散開、雷の中、視線を避ける")


class ChainRefreshTests(unittest.TestCase):
    @staticmethod
    def complete_chain(now):
        from nyaatriggers.umad_chains import BlackHoleChains, DPS, SUPPORT

        roles = {"10000004": DPS, "10000005": DPS,
                 "10000006": SUPPORT, "10000007": SUPPORT, "10000008": SUPPORT}
        engine = BlackHoleChains(roles.get)
        for actor, order in (("10000001", "BBC"), ("10000002", "BBD"),
                             ("10000003", "BBC"), ("10000004", "BBD"),
                             ("10000005", "BBE"), ("10000006", "BBC"),
                             ("10000007", "BBD"), ("10000008", "BBE")):
            if actor in ("10000001", "10000002"):
                engine.on_gain("644", actor, now)
            engine.on_gain(order, actor, now)
            engine.on_gain("154E", actor, now)
        return roles, engine

    def test_continuous_combatant_polling_does_not_starve_role_backfill(self):
        from nyaatriggers.ui.automarkers_tab import AutomarkersTabMixin
        from nyaatriggers.ui.connection import ConnectionMixin
        from nyaatriggers.umad_chains import role_for_job

        roles, engine = self.complete_chain(time.monotonic())
        actions, snapshots, flushes = [], [], []
        timer = QTimer()
        timer.setSingleShot(True)
        timer.setInterval(1200)

        def flush():
            emitted = engine.flush(time.monotonic())
            flushes.append(emitted)
            actions.extend(emitted)

        timer.timeout.connect(flush)
        host = types.SimpleNamespace(
            _umad_chain_enabled=True, _umad_chains=engine, _umad_chain_flush_timer=timer,
            _note_actor_job=lambda aid, job: roles.update({f"{aid:08X}": role_for_job(job)}))
        host._rearm_umad_chain_flush = types.MethodType(
            AutomarkersTabMixin._rearm_umad_chain_flush, host)

        def poll():
            snapshots.append(time.monotonic())
            ConnectionMixin._on_ws_combatants_jobs(
                host, {"list": [{"id": 0x10000003, "job": 34 if len(snapshots) >= 3 else 0}]})

        polling = QTimer()
        polling.setInterval(600)
        polling.timeout.connect(poll)
        loop = QEventLoop()
        deadline = QTimer()
        deadline.setSingleShot(True)
        deadline.timeout.connect(loop.quit)
        try:
            timer.start()
            polling.start()
            deadline.start(3900)
            loop.exec()
        finally:
            timer.stop()
            polling.stop()
            deadline.stop()
        self.assertGreaterEqual(len(snapshots), 5)
        self.assertGreaterEqual(len(flushes), 2)
        self.assertEqual(flushes[0], [])
        self.assertIn(("mark", "10000003", "attack1"), actions)

    def test_repeated_roster_retries_do_not_keep_old_chain_assignments_alive(self):
        from nyaatriggers.umad_chains import BlackHoleChains, DPS

        roles = {}
        engine = BlackHoleChains(roles.get)
        for actor, order, accretion in (("10000001", "BBC", True),
                                        ("10000002", "BBD", True),
                                        ("10000003", "BBC", False)):
            if accretion:
                engine.on_gain("644", actor, 10)
            engine.on_gain(order, actor, 10)
            engine.on_gain("154E", actor, 10)
        for now in (30, 60, 90):
            self.assertEqual(engine.flush(now), [])
        roles["10000003"] = DPS
        self.assertEqual(engine.flush(120), [("clear", "10000001")])
        self.assertEqual(engine.outstanding(), [])
        self.assertFalse(engine.has_open_queues())

    def test_actual_status_updates_keep_a_late_role_assignment_live(self):
        from nyaatriggers.umad_chains import DPS

        roles, engine = self.complete_chain(10)
        engine.flush(60)
        engine.on_gain("154E", "10000003", 85)
        roles["10000003"] = DPS
        self.assertEqual(engine.flush(120), [("mark", "10000003", "attack1")])


class SystemSpeechFallbackTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "posix", "executable shebangs require POSIX")
    def test_missing_backend_interpreter_does_not_skip_a_working_voice(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            broken = root / "spd-say"
            broken.write_text(f"#!{root}/removed-python\n")
            broken.chmod(0o755)
            fallback = root / "espeak-ng"
            fallback.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$NYAA_SPEECH_CAPTURE"\n')
            fallback.chmod(0o755)
            capture = root / "spoken"
            with patch.dict(os.environ, {"PATH": folder, "NYAA_SPEECH_CAPTURE": str(capture)}), \
                    patch.object(tts.platform, "system", return_value="Linux"), \
                    patch.object(tts, "_jp_auto", True), \
                    patch.object(tts, "_master_volume", 1.0), \
                    patch.object(tts, "_log_once"):
                for text, reading in (("散開", "さんかい"), ("Spread", None)):
                    with self.subTest(text=text):
                        capture.unlink(missing_ok=True)
                        handled = tts._system_speak(text, reading=reading)
                        self.assertTrue(capture.exists(), "Working fallback voice was never started")
                        self.assertTrue(handled)
                        self.assertIn(reading or text, capture.read_text().splitlines())


class KokoroInstallerTests(unittest.TestCase):
    def test_interpreter_selection_waits_for_environment_repair_lock(self):
        import install

        held = []

        @contextmanager
        def lock():
            held.append(True)
            try:
                yield
            finally:
                held.pop()

        def interpreter():
            self.assertEqual(held, [True])
            return "/repaired/voice/python"

        def run(command, **options):
            self.assertEqual(held, [True])
            self.assertEqual(command[0], "/repaired/voice/python")
            self.assertEqual(options, {"timeout": 19, "capture_output": True,
                                       "env": {"VOICE_INSTALL_TEST": "yes"}})
            return subprocess.CompletedProcess(command, 0, "first\nsecond\nthird\nfourth\n", "")

        with patch.object(install, "setup_lock", side_effect=lock), \
                patch.object(install, "run_setup_command", side_effect=run), \
                patch.object(tts, "_venv_python", side_effect=interpreter), \
                patch.object(tts.proc_env, "child_env", return_value={"VOICE_INSTALL_TEST": "yes"}):
            self.assertEqual(tts.install_kokoro_deps(19), (True, "second\nthird\nfourth"))

    def test_failed_owned_installer_retains_its_error_output(self):
        import install

        error = subprocess.CalledProcessError(1, ["pip"], output="unused", stderr="one\ntwo\nthree\nfour\n")
        with patch.object(install, "setup_lock"), \
                patch.object(install, "run_setup_command", side_effect=error), \
                patch.object(tts, "_venv_python", return_value="python"), \
                patch.object(tts.subprocess, "run", side_effect=error):
            self.assertEqual(tts.install_kokoro_deps(), (False, "two\nthree\nfour"))


class SequenceCaptureTests(unittest.TestCase):
    def make_runner(self, sequence, captured=None, delay=0):
        done = []
        trigger = Trigger(sequence=sequence, delay_s=delay)
        runner = SequentialRunner(trigger, captured or {},
                                  lambda runner, values: done.append(values),
                                  lambda runner: None)
        self.addCleanup(runner.cancel)
        return runner, done

    def status(self, count, kind="26"):
        return [kind, "ts", "B50", "Stacking Debuff", "30", "40000001",
                "Boss", "10000002", "Other Player", count]

    def test_status_followup_captures_its_decimal_count(self):
        for kind in ("26", "30"):
            with self.subTest(kind=kind):
                runner, done = self.make_runner(
                    [{"log_type": "26|30", "ability_id": "B50"}],
                    {"count": "", "source": "Old Boss", "target": "Player"})
                self.assertTrue(runner.try_advance(self.status("0A", kind)))
                self.assertEqual(done, [{"count": "10", "source": "Boss",
                                         "target": "Other Player"}])

    def test_followup_does_not_reuse_an_old_count_after_a_bad_status(self):
        for count in ("", "unknown"):
            with self.subTest(count=count):
                runner, done = self.make_runner([{"log_type": "26"}], {"count": "4"})
                runner.try_advance(self.status(count))
                self.assertEqual(done[0]["count"], "")

    def test_truncated_followup_does_not_reuse_an_old_count(self):
        runner, done = self.make_runner([{"log_type": "26"}], {"count": "4"})
        runner.try_advance(self.status("01")[:9])
        self.assertEqual(done[0]["count"], "")

    def test_nonstatus_followup_preserves_the_status_count(self):
        runner, done = self.make_runner([{"log_type": "20"}], {"count": "4"})
        runner.try_advance(["20", "ts", "40000001", "Boss", "ABCD", "Cast",
                            "10000001", "Player"])
        self.assertEqual(done[0]["count"], "4")

    def test_delayed_completion_keeps_the_last_followup_count(self):
        now = [1000.0]
        with patch("time.monotonic", side_effect=lambda: now[0]):
            runner, done = self.make_runner([{"log_type": "26"}], delay=3)
            self.assertFalse(runner.try_advance(self.status("02")))
            runner.try_advance(self.status("03"))
            self.assertEqual(done, [])
            now[0] += 3
            runner._expire()
            self.assertEqual(done[0]["count"], "2")


class SequenceRepeatTests(unittest.TestCase):
    @staticmethod
    def hit(sequence, target=0, source="40000001"):
        fields = ["22", "ts", source, "Boss", "ABCD", "Raidwide",
                  f"1000000{target + 1}", f"Player {target + 1}"]
        fields += ["0"] * (44 - len(fields))
        fields += [sequence, str(target), "8"]
        return fields

    def test_one_raidwide_cannot_complete_a_wait_for_the_next_raidwide(self):
        from tests.test_callout_review_fixes import Host

        now = [1000.0]
        with patch("time.monotonic", side_effect=lambda: now[0]), \
                patch("nyaatriggers.main_window.speak") as speech, \
                patch("nyaatriggers.ui.instance_tab.ac.log_drop"):
            host = Host(now)
            self.addCleanup(host._clear_seq_runners)
            host._triggers = [Trigger(log_type="21|22", ability_id="ABCD",
                tts_text="After the second raidwide", cooldown_s=5,
                sequence=[{"log_type": "21|22", "ability_id": "ABCD"}])]
            for target in range(8):
                fields = self.hit("0000CAFE", target)
                host._dispatch_log_line(fields, "|".join(fields))
            speech.assert_not_called()
            now[0] += 1
            fields = self.hit("0000CAFF", 3)
            host._dispatch_log_line(fields, "|".join(fields))
            speech.assert_called_once()

    def test_repeated_followup_targets_do_not_satisfy_the_next_step(self):
        done = []
        trigger = Trigger(sequence=[{"log_type": "21|22", "ability_id": "ABCD"}] * 2)
        runner = SequentialRunner(trigger, {}, lambda runner, values: done.append(values),
                                  lambda runner: None)
        self.addCleanup(runner.cancel)
        self.assertFalse(runner.try_advance(self.hit("0000CAFE", 2)))
        for target in (2, 3, 4):
            self.assertFalse(runner.try_advance(self.hit("0000CAFE", target)))
        self.assertEqual(done, [])
        self.assertTrue(runner.try_advance(self.hit("0000CAFF", 5)))
        self.assertEqual(done[0]["target"], "Player 6")

    def test_identical_sequence_numbers_from_other_casters_are_distinct_actions(self):
        done = []
        trigger = Trigger(sequence=[{"log_type": "22", "ability_id": "ABCD"}] * 2)
        runner = SequentialRunner(trigger, {}, lambda runner, values: done.append(values),
                                  lambda runner: None)
        self.addCleanup(runner.cancel)
        runner.try_advance(self.hit("0000CAFE"))
        self.assertTrue(runner.try_advance(self.hit("0000CAFE", source="40000002")))
        self.assertEqual(len(done), 1)


class DelayedScheduleTests(unittest.TestCase):
    def setUp(self):
        from nyaatriggers.ui.instance_tab import InstanceTabMixin
        from nyaatriggers.ui.timeline_tab import TimelineTabMixin

        self.now = 1000.0
        self.clock = patch("time.monotonic", new=lambda: self.now)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.engine = TimelineEngine()
        self.addCleanup(self.engine.reset)
        self.host = types.SimpleNamespace(
            _pending_timeline_events=[], _awaiting_zone_metadata=False,
            _cactbot_mode=False, _local_enabled=True, _global_local_on_flag=True,
            _timeline=self.engine, _timeline_reset_on_combat_end=False)
        self.queue = lambda fields: InstanceTabMixin._queue_timeline_event(self.host, fields)
        self.resume = lambda: TimelineTabMixin._resume_timeline_events(self.host)
        self.spoken = []
        self.engine.tts.connect(self.spoken.append)

    def combat(self, elapsed, active):
        self.now = 1000 + elapsed
        self.queue(["260", "", "1", "1" if active else "0"])

    def load(self, elapsed, reset_on_end=False):
        self.now = 1000 + elapsed
        self.engine.load(parse('10 "Old cue"\n25 "Boss returns"'))
        self.host._timeline_reset_on_combat_end = reset_on_end
        self.resume()

    def test_late_schedule_preserves_the_clock_through_an_intermission(self):
        self.combat(0, True)
        self.combat(15, False)
        self.load(18)
        self.assertTrue(self.engine.is_active())
        self.assertEqual(self.engine.current_time(), 18)
        self.assertEqual(self.spoken, [])
        self.now = 1025
        self.engine._tick()
        self.assertEqual(self.spoken, ["Boss returns"])

    def test_late_sample_schedule_does_not_resume_a_finished_combat(self):
        self.combat(0, True)
        self.combat(15, False)
        self.load(18, reset_on_end=True)
        self.assertFalse(self.engine.is_active())
        self.assertEqual(self.spoken, [])
        self.assertEqual(self.host._pending_timeline_events, [])

    def test_late_sample_schedule_resumes_only_the_next_combat(self):
        self.combat(0, True)
        self.combat(15, False)
        self.combat(17, True)
        self.load(18, reset_on_end=True)
        self.assertEqual(self.engine.current_time(), 1)
        self.assertEqual(self.spoken, [])
        self.now = 1027
        self.engine._tick()
        self.assertEqual(self.spoken, ["Old cue"])

    def test_unconfirmed_zone_still_discards_a_finished_combat(self):
        self.host._awaiting_zone_metadata = True
        self.combat(0, True)
        self.combat(15, False)
        self.host._awaiting_zone_metadata = False
        self.load(18)
        self.assertFalse(self.engine.is_active())


class TimelineLoopTests(unittest.TestCase):
    def engine(self, schedule):
        engine = TimelineEngine()
        engine.load(parse(schedule))
        self.addCleanup(engine.reset)
        return engine

    def test_vanguard_forced_loop_remains_armed_after_an_early_sync(self):
        engine = TimelineEngine()
        engine.load(parse((ROOT / "timelines/vanguard.cactbot.txt").read_text()))
        self.addCleanup(engine.reset)
        now = [1000.0]
        with patch("time.monotonic", side_effect=lambda: now[0]):
            engine.start()
            engine._snap(1158.8)
            engine.process_line(["21", "ts", "40000001", "Vanguard Commander R8",
                                 "8ECF", "Enhanced Mobility"])
            self.assertAlmostEqual(engine.current_time(), 1085)
            now[0] += 73.9
            engine._tick()
            self.assertAlmostEqual(engine.current_time(), 1085)

    def test_repeated_syncs_do_not_repeat_the_callout_or_disable_the_next_loop(self):
        engine = self.engine('2 "Warning"\n4 "Loop" Ability { id: "ABCD" } window 10,10 forcejump 1')
        spoken = []
        engine.tts.connect(spoken.append)
        now = [1000.0]
        line = ["21", "ts", "40000001", "Boss", "ABCD", "Loop"]
        with patch("time.monotonic", side_effect=lambda: now[0]):
            engine.start()
            now[0] += 3.8
            engine.process_line(line)
            engine.process_line(line)
            self.assertEqual(spoken, ["Loop"])
            for _ in range(3):
                now[0] += 1
                engine._tick()
                now[0] += 2
                engine._tick()
                self.assertAlmostEqual(engine.current_time(), 1)
            self.assertEqual(spoken, ["Loop", "Warning", "Loop", "Warning", "Loop", "Warning", "Loop"])

    def test_forward_sync_skips_earlier_forced_jumps(self):
        engine = self.engine('2 "--loop--" forcejump 1\n10 "--next--" Ability { id: "ABCD" } window 20,20\n11 "Next"')
        spoken = []
        engine.tts.connect(spoken.append)
        now = [1000.0]
        with patch("time.monotonic", side_effect=lambda: now[0]):
            engine.start()
            engine.process_line(["21", "ts", "40000001", "Boss", "ABCD", "Next"])
            now[0] += 1
            engine._tick()
            self.assertEqual(engine.current_time(), 11)
            self.assertEqual(spoken, ["Next"])

    def test_recovery_keeps_loops_after_a_recorded_early_sync(self):
        engine = self.engine('2 "Warning"\n4 "Loop" Ability { id: "ABCD" } window 1,1 forcejump 1')
        spoken = []
        engine.tts.connect(spoken.append)
        now = [1010.3]
        with patch("time.monotonic", side_effect=lambda: now[0]):
            engine.resume([(1000, ["260", "", "1", "1"]),
                           (1003.8, ["21", "ts", "40000001", "Boss", "ABCD", "Loop"])])
            self.assertAlmostEqual(engine.current_time(), 1.5)
            self.assertEqual(spoken, [])
            now[0] += 0.5
            engine._tick()
            self.assertEqual(spoken, ["Warning"])

    def test_reloading_live_timeline_keeps_the_next_forced_loop(self):
        schedule = '2 "Warning"\n4 "Loop" Ability { id: "ABCD" } window 1,1 forcejump 1'
        engine = self.engine(schedule)
        now = [1000.0]
        with patch("time.monotonic", side_effect=lambda: now[0]):
            engine.start()
            now[0] += 3.8
            engine.process_line(["21", "ts", "40000001", "Boss", "ABCD", "Loop"])
            engine.load(parse(schedule), preserve_time=True)
            now[0] += 3
            engine._tick()
            self.assertAlmostEqual(engine.current_time(), 1)

    def test_equal_time_sync_does_not_rearm_a_consumed_jump(self):
        engine = self.engine('4 "Once" forcejump 4\n4 "--sync--" Ability { id: "ABCD" }')
        spoken = []
        engine.tts.connect(spoken.append)
        now = [1000.0]
        with patch("time.monotonic", side_effect=lambda: now[0]):
            engine.start()
            now[0] += 4
            engine._tick()
            engine.process_line(["21", "ts", "40000001", "Boss", "ABCD", "Hit"])
            engine._tick()
            self.assertEqual(spoken, ["Once"])


class TimelineConstraintTests(unittest.TestCase):
    def test_quoted_field_names_keep_their_scalar_constraints(self):
        for quote in ('"', "'"):
            with self.subTest(quote=quote):
                entry, = parse('100 "Raidwide" StartsUsing { '
                               f'{quote}id{quote}: "ABCD", {quote}source{quote}: "Boss" '
                               '} window 100,100')
                self.assertEqual(entry.event_fields, {"id": "ABCD", "source": "Boss"})
                engine = TimelineEngine()
                self.assertFalse(engine._entry_matches(entry,
                    ["20", "ts", "40000001", "Unrelated Boss", "9999", "Other Cast"]))
                self.assertTrue(engine._entry_matches(entry,
                    ["20", "ts", "40000001", "Boss", "ABCD", "Raidwide"]))

    def test_quoted_array_keys_keep_all_id_alternatives(self):
        for quote in ('"', "'"):
            with self.subTest(quote=quote):
                entry, = parse('100 "Raidwide" StartsUsing { '
                               f'{quote}id{quote}: ["ABCD", "ABCE"], source: "Boss" '
                               '} window 100,100')
                engine = TimelineEngine()
                for ident, matches in (("ABCD", True), ("ABCE", True), ("9999", False)):
                    self.assertEqual(engine._entry_matches(entry,
                        ["20", "ts", "40000001", "Boss", ident, "Cast"]), matches)

    def test_quoted_unsupported_fields_prevent_unconstrained_syncs(self):
        entry, = parse('100 "Raidwide" StartsUsing { "unknownConstraint": "ABCD" }')
        self.assertEqual(entry.event_fields, {"unknownConstraint": "ABCD"})
        self.assertFalse(TimelineEngine()._entry_matches(entry,
            ["20", "ts", "40000001", "Boss", "ABCD", "Cast"]))

    def test_quoted_field_syntax_inside_a_value_does_not_add_constraints(self):
        entry, = parse('100 "Chat" GameLog { "line": "id: [\'ABCD\', \'ABCE\']" }')
        self.assertEqual(entry.event_fields, {"line": "id: ['ABCD', 'ABCE']"})


class SpeechCancellationTests(unittest.TestCase):
    def test_cancel_during_kokoro_loading_skips_synthesis_and_system_fallback(self):
        from unittest.mock import Mock

        voice = Mock()

        def load(gen=None):
            tts._generation += 1
            return voice

        with patch.object(tts, "_generation", 10), \
                patch.object(tts, "_master_volume", 1.0), \
                patch.object(tts, "_jp_neural", True), \
                patch.object(tts, "_load_kokoro", side_effect=load), \
                patch.object(tts, "_synth_call", side_effect=lambda work: (True, work())), \
                patch.object(tts, "_system_speak") as fallback, \
                patch.object(tts, "_play_wav") as playback, \
                patch.object(tts, "_log_once"), \
                patch.dict(sys.modules, {"numpy": types.SimpleNamespace()}):
            tts._pipeline("散開", reading="さんかい", gen=10)
        voice.create.assert_not_called()
        fallback.assert_not_called()
        playback.assert_not_called()

    def test_muting_stops_an_active_speech_backend_and_clears_queued_speech(self):
        result = []
        worker = threading.Thread(target=lambda: result.append(tts._run_speak_proc(
            [sys.executable, "-c", "import time; time.sleep(30)"], "",
            stdin_text=False)))
        with patch.object(tts, "_master_volume", 1.0), \
                patch.object(tts, "_queue", Queue()), \
                patch.object(tts, "_generation", 10):
            try:
                worker.start()
                deadline = time.monotonic() + 5
                while tts._current_proc is None and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertIsNotNone(tts._current_proc)
                tts._enqueue(("tts", "Queued before mute", 1.0, 1.0, None))
                tts.set_master_volume(0)
                worker.join(2)
                self.assertFalse(worker.is_alive())
                self.assertEqual(result, [True])
                self.assertTrue(tts._queue.empty())
                self.assertGreater(tts._generation, 10)
            finally:
                tts.interrupt()
                worker.join(5)

    def test_positive_volume_adjustments_do_not_interrupt_current_speech(self):
        with patch.object(tts, "_master_volume", 1.0), \
                patch.object(tts, "interrupt") as stop:
            tts.set_master_volume(0.5)
            tts.set_master_volume(2)
            stop.assert_not_called()
            tts.set_master_volume(0)
            stop.assert_called_once()
            tts.set_master_volume(0)
            stop.assert_called_once()
            tts.set_master_volume(1)
            stop.assert_called_once()

    def test_callouts_queued_while_muted_cannot_reappear_after_unmuting(self):
        with patch.object(tts, "_master_volume", 0.0), \
                patch.object(tts, "_queue", Queue()), \
                patch.object(tts, "_ensure_worker"):
            tts.speak("During mute")
            tts.play_sound("during-mute.wav")
            tts.set_master_volume(1)
            tts.speak("After mute")
            self.assertEqual(list(tts._queue.queue),
                             [("tts", "After mute", 1.0, 1.0, None)])

    def test_dequeued_speech_cancelled_before_dispatch_skips_neural_synthesis(self):
        with patch.object(tts, "_generation", 12), \
                patch.object(tts, "_engine", "piper"), \
                patch.object(tts, "_jp_neural", True), \
                patch.object(tts, "_load_piper", return_value=None) as piper, \
                patch.object(tts, "_kokoro_speak") as kokoro, \
                patch.object(tts, "log_drop"):
            tts._pipeline("Raidwide", gen=11)
            tts._pipeline("全体攻撃", gen=11)
            piper.assert_not_called()
            kokoro.assert_not_called()

    def test_cancel_during_model_load_skips_the_old_callout_synthesis(self):
        synthesized = []

        class Voice:
            def synthesize_wav(self, text, wav, **kwargs):
                synthesized.append(text)
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(22050)
                wav.writeframes(b"\0\0")

        def loaded_after_interrupt(gen=None):
            tts.interrupt()
            return Voice()

        with patch.object(tts, "_generation", 10), \
                patch.object(tts, "_engine", "piper"), \
                patch.object(tts, "_load_piper", side_effect=loaded_after_interrupt), \
                patch.object(tts, "_play_wav") as playback, \
                patch.object(tts, "log_drop"):
            tts._pipeline("Previous pull", gen=10)
            self.assertEqual(synthesized, [])
            playback.assert_not_called()


class VoicePathCompatibilityTests(unittest.TestCase):
    def test_voice_path_changes_cannot_expose_other_python_versions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            current = Path(sysconfig.get_path("purelib", scheme="venv",
                           vars={"base": directory, "platbase": directory}))
            stale = root / "lib/python0.1/site-packages"
            current.mkdir(parents=True)
            stale.mkdir(parents=True)
            (root / "pyvenv.cfg").write_text(
                f"version = {sys.version_info.major}.{sys.version_info.minor}.0\n")
            (current / "nyaa_current_voice_probe.py").write_text("value = 42\n")
            (stale / "nyaa_foreign_voice_probe.py").write_text("value = 13\n")
            script = """
import importlib.util
import sys
from nyaatriggers import tts
tts.set_venv_path(sys.argv[1])
import nyaa_current_voice_probe
assert nyaa_current_voice_probe.value == 42
assert importlib.util.find_spec('nyaa_foreign_voice_probe') is None
from nyaatriggers import trigger_engine
assert trigger_engine._HAVE_REGEX
assert trigger_engine.compile_user_regex('(?:ABCD|ABCE)') is not None
"""
            result = subprocess.run([sys.executable, "-c", script, directory],
                                    cwd=ROOT, capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)


class VoiceEnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        model, voices = self.root / "model.onnx", self.root / "voices.bin"
        model.touch()
        voices.touch()
        self.stack.enter_context(patch.multiple(tts,
            _KOKORO_MODEL=model, _KOKORO_VOICES=voices,
            _kokoro=None, _kokoro_failed=True, _kokoro_import_failed=True,
            _kokoro_epoch=10, _piper_voice=None, _piper_failed=False, _piper_epoch=10,
            _FFXIV_VENV=self.root / "old", _venv_sps=[], _stale_venv_sps=set()))
        self.stack.enter_context(patch.object(tts, "_purge_stale_venv_modules"))
        module = types.ModuleType("kokoro_onnx")
        module.Kokoro = lambda *args: object()
        self.stack.enter_context(patch.dict(sys.modules, {"kokoro_onnx": module}))

    @unittest.skipIf(os.name == "nt", "Windows may expand unknown users without a home lookup")
    def test_invalid_saved_home_falls_back_without_a_startup_exception(self):
        with patch.object(Path, "home", return_value=self.root), \
                patch.object(tts, "_logged_once", set()), \
                patch("sys.stderr", new_callable=io.StringIO) as error:
            tts.set_venv_path("~nyaa_missing_audit_user_6a78/voice")
            tts.set_venv_path("~nyaa_missing_audit_user_6a78/voice")
        self.assertEqual(tts._FFXIV_VENV, self.root / ".venv" / "ffxiv")
        self.assertEqual(error.getvalue().count("invalid voice environment"), 1)

    def test_failed_site_discovery_preserves_active_path_and_sessions(self):
        previous = tts._FFXIV_VENV
        voice = tts._piper_voice = object()
        kokoro = tts._kokoro = object()
        with patch.object(tts, "voice_site_packages", side_effect=RuntimeError("discovery failed")):
            with self.assertRaisesRegex(RuntimeError, "discovery failed"):
                tts.set_venv_path(str(self.root / "new"))
        self.assertEqual(tts._FFXIV_VENV, previous)
        self.assertIs(tts._piper_voice, voice)
        self.assertIs(tts._kokoro, kokoro)
        self.assertEqual((tts._piper_epoch, tts._kokoro_epoch), (10, 10))

    def test_new_voice_environment_retries_previous_kokoro_failures(self):
        tts.set_venv_path(str(self.root / "new"))
        self.assertTrue(tts.kokoro_ready())
        self.assertIsNotNone(tts._load_kokoro())
        self.assertFalse(tts._kokoro_failed)

    def test_new_environment_replaces_an_existing_kokoro_session(self):
        previous = tts._kokoro = object()
        tts._kokoro_failed = tts._kokoro_import_failed = False
        tts.set_venv_path(str(self.root / "new"))
        self.assertGreater(tts._kokoro_epoch, 10)
        self.assertIsNot(tts._load_kokoro(), previous)

    def test_late_old_import_failures_do_not_disable_the_new_environment(self):
        real_import = builtins.__import__
        for load in (tts.kokoro_ready, tts._load_kokoro):
            with self.subTest(load=load.__name__):
                tts._kokoro = None
                tts._kokoro_failed = tts._kokoro_import_failed = False

                def import_old_environment(name, *args, **kwargs):
                    if name == "kokoro_onnx":
                        tts.set_venv_path(str(self.root / "new"))
                        raise ImportError("old environment is no longer available")
                    return real_import(name, *args, **kwargs)

                with patch("builtins.__import__", side_effect=import_old_environment), \
                        patch.object(tts, "_log_once"):
                    self.assertFalse(load())
                self.assertFalse(tts._kokoro_failed)
                self.assertFalse(tts._kokoro_import_failed)
                self.assertTrue(tts.kokoro_ready())
                self.assertIsNotNone(tts._load_kokoro())


if __name__ == "__main__":
    unittest.main()
