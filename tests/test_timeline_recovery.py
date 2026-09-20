"""Compare delayed timeline recovery with uninterrupted event delivery."""

from itertools import product
from pathlib import Path
import unittest
from unittest.mock import patch

from PyQt6.QtCore import QCoreApplication

from nyaatriggers.timeline_engine import TimelineEngine
from nyaatriggers.timeline_parser import parse


APP = QCoreApplication.instance() or QCoreApplication([])
COMBAT = ["260", "", "1", "1"]
CAST = ["20", "", "40000001", "Boss", "ABCD", "Cast"]


class TimelineRecoveryTests(unittest.TestCase):
    def test_recovery_matches_uninterrupted_clocks_and_future_calls(self):
        schedules = (
            '0 "First"\n3 "Second"\n8 "Third"',
            '1 "Cue"\n2 "--loop--" forcejump 0',
            '1 "Once"\n3 "Again"\n4 "--loop--" forcejump 2',
            '1 "First"\n2 "--skip--" forcejump 10\n11 "Next"',
            '1 "First"\n30 "--sync--" StartsUsing { id: "ABCD" } window 60,60\n35 "Next"',
            '1 "First"\n3 "--stop--" StartsUsing { id: "ABCD" } jump 0\n8 "Later"',
        )
        for schedule, stop, cast_at in product(schedules, range(1, 25), (None, 2, 5)):
            with self.subTest(schedule=schedule, stop=stop, cast_at=cast_at):
                now = [1000.0]
                reference, recovered = TimelineEngine(), TimelineEngine()
                reference.load(parse(schedule))
                recovered.load(parse(schedule))
                old_calls, new_calls = [], []
                reference.tts.connect(old_calls.append)
                recovered.tts.connect(new_calls.append)
                events = [(1000.0, COMBAT)]
                with patch("time.monotonic", side_effect=lambda: now[0]):
                    reference.process_line(COMBAT)
                    for step in range(1, stop * 2 + 2):
                        now[0] = 1000 + step / 2
                        reference._tick()
                        if step / 2 == cast_at:
                            reference.process_line(CAST)
                            events.append((now[0], CAST))
                    recovered.resume(events)
                    self.assertEqual(new_calls, [])
                    self.assertEqual(recovered.is_active(), reference.is_active())
                    self.assertAlmostEqual(recovered.current_time(), reference.current_time())
                    old_calls.clear()
                    for _ in range(12):
                        now[0] += 0.5
                        reference._tick()
                        recovered._tick()
                    self.assertEqual(new_calls, old_calls)
                reference.reset()
                recovered.reset()

    def test_long_pause_skips_repeated_loops_without_replaying_each_one(self):
        engine = TimelineEngine()
        engine.load(parse('1 "Cue"\n2 "--loop--" forcejump 0'))
        self.addCleanup(engine.reset)
        with patch("time.monotonic", return_value=1000000.5), \
                patch.object(engine, "_tick", wraps=engine._tick) as tick:
            engine.resume([(1000, COMBAT)])
            self.assertEqual(engine.current_time(), 0.5)
            self.assertLess(tick.call_count, 10)

    def test_fractional_jumps_preserve_elapsed_recovery_time(self):
        for jump, expected, cue in ((0, 0.6, 1), (1.1, 1.5, 1.9), (10, 15.2, 16)):
            with self.subTest(jump=jump):
                engine = TimelineEngine()
                engine.load(parse(f'{cue} "Cue"\n2.3 "--jump--" forcejump {jump}'))
                self.addCleanup(engine.reset)
                spoken = []
                engine.tts.connect(spoken.append)
                now = [1007.5]
                with patch("time.monotonic", side_effect=lambda: now[0]):
                    engine.resume([(1000, COMBAT)])
                    self.assertAlmostEqual(engine.current_time(), expected)
                    self.assertEqual(spoken, [])
                    now[0] += cue - expected + 0.01
                    engine._tick()
                    engine._tick()
                    self.assertEqual(spoken, ["Cue"])

    def test_fractional_recovery_finishes_at_a_loop_boundary(self):
        engine = TimelineEngine()
        engine.load(parse('2.3 "--jump--" forcejump 1.1'))
        self.addCleanup(engine.reset)
        tick = engine._tick
        calls = []

        def bounded_tick():
            calls.append(None)
            self.assertLess(len(calls), 20)
            tick()

        with patch("time.monotonic", return_value=1003.5), \
                patch.object(engine, "_tick", side_effect=bounded_tick):
            engine.resume([(1000, COMBAT)])
            self.assertAlmostEqual(engine.current_time(), 1.1)

    def test_fractional_loops_keep_time_after_a_large_skip(self):
        for jump, elapsed, expected in ((0, 1000000.5, 1.9), (1.1, 1000.5, 2.1)):
            with self.subTest(jump=jump):
                engine = TimelineEngine()
                engine.load(parse(f'2.3 "--jump--" forcejump {jump}'))
                self.addCleanup(engine.reset)
                with patch("time.monotonic", return_value=1000 + elapsed), \
                        patch.object(engine, "_tick", wraps=engine._tick) as tick:
                    engine.resume([(1000, COMBAT)])
                    self.assertAlmostEqual(engine.current_time(), expected, places=6)
                    self.assertLess(tick.call_count, 20)

    def test_umad_recovery_keeps_elapsed_time_after_the_phase_jump(self):
        engine = TimelineEngine()
        schedule = Path(__file__).resolve().parents[1] / "timelines" / "UMAD.txt"
        engine.load(parse(schedule.read_text()))
        self.addCleanup(engine.reset)
        with patch("time.monotonic", return_value=1390.1):
            engine.resume([(1000.1, COMBAT)])
            self.assertAlmostEqual(engine.current_time(), 500.8)

    def test_recovery_keeps_existing_signal_blocking(self):
        engine = TimelineEngine()
        engine.load(parse('1 "Cue"'))
        self.addCleanup(engine.reset)
        engine.blockSignals(True)
        with patch("time.monotonic", return_value=1002):
            engine.resume([(1000, COMBAT)])
        self.assertTrue(engine.signalsBlocked())
        self.assertIsNone(engine._replay_now)


if __name__ == "__main__":
    unittest.main()
