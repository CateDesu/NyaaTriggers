from contextlib import ExitStack
from queue import Empty, Queue
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from nyaatriggers import tts
from tests.test_notification_lifecycle import Playback


class EndWorker(BaseException):
    pass


class CalloutFreshnessTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.clock = 100.0
        self.context = SimpleNamespace(deadline=None, expired=False)
        for name, value in {
            "_speech_context": self.context, "_queue": Queue(), "_generation": 4,
            "_current_proc": None, "_interrupted_proc": None, "_master_volume": 1.0,
            "_speech_suspended": False, "_engine": "piper", "_jp_neural": False,
            "_jp_auto": False, "_piper_voice": None, "_piper_failed": False,
            "_kokoro": None, "_kokoro_failed": False,
            "_synthesis_slots": threading.BoundedSemaphore(2),
        }.items():
            self.stack.enter_context(patch.object(tts, name, value))
        self.stack.enter_context(patch.object(tts.time, "monotonic", side_effect=lambda: self.clock))
        self.record = self.stack.enter_context(patch.object(tts, "record"))
        self.log = self.stack.enter_context(patch.object(tts, "log_drop"))

    def enqueue(self, text):
        tts._enqueue(("tts", text, 1.0, 1.0, None))

    def run_worker(self):
        get = tts._queue.get

        def next_item():
            try:
                return get(block=False)
            except Empty:
                raise EndWorker from None

        with patch.object(tts._queue, "get", side_effect=next_item), self.assertRaises(EndWorker):
            tts._worker_loop()
        self.assertIsNone(self.context.deadline)

    def voice(self, before=None):
        spoken = []

        def synthesize(text, wav, **kwargs):
            spoken.append(text)
            if before:
                before()
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(22050)
            wav.writeframes(b"\0\0")

        voice = SimpleNamespace(synthesize_wav=synthesize)
        tts._piper_voice = voice
        self.stack.enter_context(patch.object(tts, "_load_piper", return_value=voice))
        return spoken

    def test_stalled_audio_cannot_replay_the_backlog_and_fresh_speech_still_plays(self):
        spoken = self.voice()
        stalled, fresh = Playback(), Playback(finished=True)
        communicate = stalled.communicate

        def stall(timeout):
            if stalled.returncode is not None:
                return communicate(timeout)
            self.clock += 60
            self.enqueue("Current mechanic")
            raise subprocess.TimeoutExpired("aplay", timeout)

        self.enqueue("First call")
        self.enqueue("Old instruction one")
        self.enqueue("Old instruction two")
        with patch.object(tts.platform, "system", return_value="Linux"), \
                patch.object(tts, "_wav_seconds", return_value=0), \
                patch.object(stalled, "communicate", side_effect=stall), \
                patch.object(tts.subprocess, "Popen", side_effect=[stalled, fresh]) as launch:
            self.run_worker()
        self.assertEqual(spoken, ["First call", "Current mechanic"])
        self.assertEqual(launch.call_count, 2)
        self.assertEqual(stalled.returncode, -9)
        self.assertTrue(stalled.stderr.closed)
        self.assertTrue(fresh.stderr.closed)
        expired = [call for call in self.record.call_args_list
                   if call.args[0] == "tts_queue" and call.kwargs.get("result") == "expired"]
        self.assertEqual(len(expired), 2)

    def test_queue_age_limit_preserves_recent_calls_and_discards_expired_calls(self):
        for age, expected in [(2.99, 1), (3.0, 0), (600.0, 0)]:
            with self.subTest(age=age):
                self.enqueue("Instruction")
                self.clock += age
                with patch.object(tts, "_pipeline") as pipeline:
                    self.run_worker()
                self.assertEqual(pipeline.call_count, expected)

    def test_queued_alert_sounds_follow_the_same_age_limit(self):
        tts._enqueue(("wav", "old.wav", 1.0))
        self.clock += 4
        with patch.object(tts, "_play_wav_file") as play:
            self.run_worker()
        play.assert_not_called()

    def test_synthesis_that_finishes_late_cannot_start_audio_or_disable_the_voice(self):
        def slow():
            self.clock += 4

        self.voice(slow)
        self.context.deadline = self.clock + 3
        with patch.object(tts, "_play_wav") as play:
            tts._pipeline("Expired during synthesis", gen=4)
        play.assert_not_called()
        self.assertFalse(tts._piper_failed)
        self.assertTrue(self.context.expired)

    def test_stalled_synthesis_releases_the_worker_without_waiting_a_minute(self):
        release = threading.Event()

        def blocked():
            self.clock += 4
            release.wait(2)

        self.voice(blocked)
        self.context.deadline = self.clock + 3
        started = time.perf_counter()
        try:
            with patch.object(tts, "_play_wav") as play:
                tts._pipeline("Stalled synthesis", gen=4)
            self.assertLess(time.perf_counter() - started, 1)
            self.assertFalse(release.is_set())
            self.assertFalse(tts._piper_failed)
            self.assertIsNone(tts._piper_voice)
            play.assert_not_called()
        finally:
            release.set()
            self.assertTrue(tts._synthesis_slots.acquire(timeout=1))
            self.assertTrue(tts._synthesis_slots.acquire(timeout=1))
            tts._synthesis_slots.release()
            tts._synthesis_slots.release()

    def test_cold_model_loading_can_finish_for_the_next_callout(self):
        self.context.deadline = self.clock + 3

        def load():
            self.clock += 4
            return "Loaded voice"

        ok, voice = tts._synth_call(load, gen=4)
        self.assertTrue(ok)
        self.assertEqual(voice, "Loaded voice")
        self.assertTrue(tts._callout_expired())

    def test_slow_kokoro_loading_keeps_the_voice_without_speaking_the_expired_call(self):
        voice = SimpleNamespace(create=Mock())
        self.context.deadline = self.clock + 3

        def load(gen):
            self.clock += 4
            tts._kokoro = voice
            return voice

        with patch.object(tts, "_load_kokoro", side_effect=load), \
                patch.object(tts, "_play_wav_bytes") as play:
            self.assertTrue(tts._kokoro_speak("頭割り", gen=4))
        voice.create.assert_not_called()
        play.assert_not_called()
        self.assertIs(tts._kokoro, voice)
        self.assertFalse(tts._kokoro_failed)

    def test_expired_kokoro_synthesis_cannot_fall_back_to_system_speech(self):
        def create(*args, **kwargs):
            self.clock += 4
            return [0.0], 22050

        voice = SimpleNamespace(create=create)
        tts._kokoro = voice
        tts._jp_neural = True
        self.context.deadline = self.clock + 3
        with patch.object(tts, "_load_kokoro", return_value=voice), \
                patch.object(tts, "_system_speak") as fallback, \
                patch.object(tts, "_play_wav") as play:
            tts._pipeline("頭割り", gen=4)
        fallback.assert_not_called()
        play.assert_not_called()
        self.assertFalse(tts._kokoro_failed)

    def test_linux_and_system_players_reject_expiry_immediately_before_spawn(self):
        self.context.deadline = self.clock
        with patch.object(tts.platform, "system", return_value="Linux"), \
                patch.object(tts.subprocess, "Popen") as launch:
            tts._play_wav("unused.wav", gen=4)
            self.assertTrue(tts._run_speak_proc(["espeak"], "Old instruction", False, gen=4))
        launch.assert_not_called()

    def test_speech_that_started_fresh_can_finish_normally(self):
        self.context.deadline = self.clock + 3
        process = Playback(finished=True)

        def finish(timeout):
            self.clock += 6
            return None, b""

        with patch.object(tts.platform, "system", return_value="Linux"), \
                patch.object(tts, "_wav_seconds", return_value=5), \
                patch.object(process, "communicate", side_effect=finish), \
                patch.object(tts.subprocess, "Popen", return_value=process) as launch:
            tts._play_wav("current.wav", gen=4)
        launch.assert_called_once()
        self.assertFalse(self.context.expired)
        self.log.assert_not_called()

    def test_windows_rechecks_age_when_its_playback_thread_starts(self):
        play = Mock()
        self.context.deadline = self.clock + 3
        owner = self

        class DeferredThread:
            def __init__(self, target, **kwargs):
                self.target = target

            def start(self):
                owner.clock += 4
                self.target()

            def join(self, timeout):
                pass

            def is_alive(self):
                return False

        with patch.dict(sys.modules, {"winsound": SimpleNamespace(PlaySound=play)}), \
                patch.object(tts.threading, "Thread", DeferredThread):
            tts._play_winsound(b"audio", 0, gen=4)
        play.assert_not_called()
        self.assertTrue(self.context.expired)

    def test_queue_deadline_is_cleared_after_a_worker_error(self):
        self.enqueue("Failed call")
        with patch.object(tts, "_pipeline", side_effect=RuntimeError("Failed call")), \
                patch.object(tts, "record_exception"), patch.object(tts.traceback, "print_exc"):
            self.run_worker()
        self.assertFalse(tts._callout_expired())


if __name__ == "__main__":
    unittest.main()
