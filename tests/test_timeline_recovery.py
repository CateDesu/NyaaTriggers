"""Compare delayed timeline recovery with uninterrupted event delivery."""

from itertools import product
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
