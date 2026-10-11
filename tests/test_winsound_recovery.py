from contextlib import ExitStack
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from nyaatriggers import tts


class WinsoundRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.release = threading.Event()
        self.entered = threading.Event()
        self.workers = []
        self.join = threading.Thread.join
        self.addCleanup(self.finish_workers)
        self.stack.enter_context(patch.object(tts, "_generation", 10))
        self.stack.enter_context(patch.object(tts, "_winsound_slot",
                                              threading.BoundedSemaphore(1), create=True))
        self.stack.enter_context(patch.object(tts, "log_drop"))
        self.stack.enter_context(patch.object(tts, "_wav_seconds", return_value=0))

    def finish_workers(self):
        self.release.set()
        for worker in self.workers:
            self.join(worker, 2)

    def test_timed_out_playback_keeps_channel_owned_until_driver_returns(self):
        calls = []

        def play(source, flags):
            self.workers.append(threading.current_thread())
            calls.append(source)
            if source == b"blocked":
                self.entered.set()
                self.release.wait(3)

        self.stack.enter_context(patch.dict("sys.modules", {
            "winsound": SimpleNamespace(PlaySound=play)}))

        def short_timeout(worker, timeout=None):
            self.assertTrue(self.entered.wait(1))
            self.join(worker, .01)

        with patch.object(threading.Thread, "join", short_timeout):
            tts._play_winsound(b"blocked", 0, gen=10)
            tts.interrupt()
            for _ in range(3):
                tts._play_winsound(b"later", 0, gen=11)
        self.assertEqual(calls, [b"blocked"],
                         "Timed out playback must retain the single Windows audio channel")
        self.release.set()
        self.finish_workers()
        tts._play_winsound(b"recovered", 0, gen=11)
        self.assertEqual(calls, [b"blocked", b"recovered"])

    def test_completed_playback_error_releases_channel(self):
        calls = []

        def play(source, flags):
            calls.append(source)
            if source == b"bad":
                raise RuntimeError("driver rejected audio")

        with patch.dict("sys.modules", {"winsound": SimpleNamespace(PlaySound=play)}):
            with self.assertRaisesRegex(RuntimeError, "driver rejected audio"):
                tts._play_winsound(b"bad", 0, gen=10)
            tts._play_winsound(b"valid", 0, gen=10)
        self.assertEqual(calls, [b"bad", b"valid"])

    def test_failed_worker_start_releases_channel(self):
        calls = []
        with patch.dict("sys.modules", {
                "winsound": SimpleNamespace(PlaySound=lambda *args: calls.append(args))}):
            with patch.object(threading.Thread, "start", side_effect=RuntimeError("no threads")):
                with self.assertRaisesRegex(RuntimeError, "no threads"):
                    tts._play_winsound(b"first", 0, gen=10)
            tts._play_winsound(b"second", 0, gen=10)
        self.assertEqual(calls, [(b"second", 0)])

    def test_stale_playback_does_not_hold_channel_or_reach_driver(self):
        calls = []
        with patch.dict("sys.modules", {
                "winsound": SimpleNamespace(PlaySound=lambda *args: calls.append(args))}):
            tts._play_winsound(b"old", 1, gen=9)
            tts._play_winsound(b"current", 2, gen=10)
        self.assertEqual(calls, [(b"current", 2)])


if __name__ == "__main__":
    unittest.main()
