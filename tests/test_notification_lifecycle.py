from contextlib import ExitStack
import io
from pathlib import Path
from queue import Queue
import subprocess
import sys
import tempfile
import threading
from types import MethodType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from nyaatriggers import tts
from nyaatriggers.ui.voice_tab import VoiceTabMixin


class Playback:
    def __init__(self, finished=False):
        self.started = threading.Event()
        self.finished = threading.Event()
        self.returncode = None
        self.stderr = io.BytesIO()
        if finished:
            self.finish()

    def finish(self, code=0):
        self.returncode = code
        self.finished.set()

    def poll(self):
        return self.returncode

    def communicate(self, timeout=None):
        self.started.set()
        self.wait(timeout)
        return None, b""

    def wait(self, timeout=None):
        if not self.finished.wait(timeout):
            raise subprocess.TimeoutExpired("aplay", timeout)
        return self.returncode

    def terminate(self):
        self.finish(-15)

    def kill(self):
        self.finish(-9)


class NotificationLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.directory = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.sound = self.directory / "sound.wav"
        self.sound.write_bytes(b"sound")
        for name, value in {
                "_master_volume": 1.0, "_speech_suspended": False,
                "_generation": 0, "_notification_epoch": 0, "_current_proc": None,
                "_notification_procs": set(), "_notification_slots": threading.BoundedSemaphore(4),
                "_queue": Queue()}.items():
            self.stack.enter_context(patch.object(tts, name, value))
        self.playback = Playback()
        self.launch = self.stack.enter_context(patch.object(
            tts.subprocess, "Popen", return_value=self.playback))
        self.stack.enter_context(patch.object(tts.platform, "system", return_value="Linux"))
        self.stack.enter_context(patch.object(tts, "_wav_seconds", return_value=1))
        self.log = self.stack.enter_context(patch.object(tts, "log_drop"))
        self.workers = []
        self.addCleanup(self.finish_workers)

    def finish_workers(self):
        self.playback.finish()
        for worker in self.workers:
            worker.join(2)
            self.assertFalse(worker.is_alive())

    def start_notification(self, volume=1.0):
        self.assertTrue(tts._notification_slots.acquire(blocking=False))
        worker = threading.Thread(target=tts._notification_worker, args=(str(self.sound), volume))
        self.workers.append(worker)
        worker.start()
        return worker

    def test_mute_stops_active_notification_and_releases_its_slot(self):
        worker = self.start_notification()
        self.assertTrue(self.playback.started.wait(1))
        tts.set_master_volume(0)
        worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertEqual(tts._notification_procs, set())
        self.assertEqual(tts._notification_slots._value, 4)
        self.log.assert_not_called()

    def test_shutdown_reaps_active_notification_before_returning(self):
        self.start_notification()
        self.assertTrue(self.playback.started.wait(1))
        tts.suspend()
        self.assertTrue(self.playback.finished.is_set())
        self.assertTrue(tts._speech_suspended)

    def test_speech_interrupt_preserves_overlapping_notification(self):
        self.start_notification()
        self.assertTrue(self.playback.started.wait(1))
        tts.interrupt()
        self.assertFalse(self.playback.finished.is_set())

    def test_sound_preparation_before_mute_cannot_play_after_unmute(self):
        copying, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        copied = self.directory / "copy.wav"
        copied.write_bytes(b"copy")

        def copy(path):
            copying.set()
            self.assertTrue(release.wait(2))
            return str(copied)

        with patch.object(tts, "_copy_sound_to_tmp", side_effect=copy), \
                patch.object(tts, "_apply_volume", return_value=True):
            worker = self.start_notification(.5)
            self.assertTrue(copying.wait(1))
            tts.set_master_volume(0)
            tts.set_master_volume(1)
            release.set()
            worker.join(1)
        self.assertFalse(worker.is_alive())
        self.launch.assert_not_called()
        self.assertFalse(copied.exists())
        self.assertEqual(tts._notification_slots._value, 4)

    def test_admitted_notification_before_shutdown_stays_cancelled_after_resume(self):
        pending = []

        class DeferredThread:
            def __init__(self, target, args, **kwargs):
                pending.append((target, args))

            def start(self):
                pass

        with patch.object(tts.threading, "Thread", DeferredThread):
            tts.play_notification(str(self.sound))
        tts.suspend()
        tts.resume()
        target, args = pending.pop()
        target(*args)
        self.launch.assert_not_called()
        self.assertEqual(tts._notification_slots._value, 4)

    def test_muted_and_suspended_notifications_never_claim_slots(self):
        with patch.object(tts.threading, "Thread") as thread:
            tts.set_master_volume(0)
            tts.play_notification(str(self.sound))
            tts.set_master_volume(1)
            tts.suspend()
            tts.play_notification(str(self.sound))
        thread.assert_not_called()
        self.assertEqual(tts._notification_slots._value, 4)

    def test_fresh_notification_after_resume_plays_normally(self):
        self.playback.finish()
        tts.suspend()
        tts.resume()
        worker = self.start_notification()
        worker.join(1)
        self.launch.assert_called_once()
        self.assertEqual(tts._notification_slots._value, 4)

    def test_playback_read_failure_kills_and_reaps_its_child(self):
        with patch.object(self.playback, "communicate", side_effect=OSError("read failed")):
            with self.assertRaisesRegex(OSError, "read failed"):
                tts._play_wav_detached(str(self.sound))
        self.assertEqual(self.playback.returncode, -9)
        self.assertEqual(tts._notification_procs, set())
        self.assertTrue(self.playback.stderr.closed)

    def test_playback_timeout_kills_and_reaps_its_child(self):
        with patch.object(self.playback, "communicate", side_effect=subprocess.TimeoutExpired("aplay", 60)):
            tts._play_wav_detached(str(self.sound))
        self.assertEqual(self.playback.returncode, -9)
        self.assertEqual(tts._notification_procs, set())
        self.assertTrue(self.playback.stderr.closed)

    def test_failed_notification_worker_preserves_the_visual_alert_and_releases_slot(self):
        visual = Mock()
        host = SimpleNamespace(_settings={"overlay_sound_enabled": True},
                               _alert_sound_path=lambda: str(self.sound),
                               _alert_sound_amp=lambda: 1.0,
                               _plugin_link=SimpleNamespace(send_alert=visual))
        host._maybe_play_alert_sound = MethodType(VoiceTabMixin._maybe_play_alert_sound, host)
        with patch.object(tts.threading.Thread, "start", side_effect=RuntimeError("No worker available")):
            VoiceTabMixin._emit_alert(host, "Stack", "alarm")
        visual.assert_called_once_with("Stack", "alarm")
        self.launch.assert_not_called()
        self.assertEqual(tts._notification_slots._value, 4)
        self.assertTrue(any(call.args[0] == "tts-notify" and "No worker available" in call.args[1]
                            for call in self.log.call_args_list))
        self.playback.finish()
        worker = self.start_notification()
        worker.join(1)
        self.assertFalse(worker.is_alive())
        self.launch.assert_called_once()
        self.assertEqual(tts._notification_slots._value, 4)

    def test_failed_windows_notification_worker_preserves_alert_and_can_retry(self):
        visual = Mock()
        host = SimpleNamespace(_settings={"overlay_sound_enabled": True},
                               _alert_sound_path=lambda: str(self.sound),
                               _alert_sound_amp=lambda: 1.0,
                               _plugin_link=SimpleNamespace(send_alert=visual))
        host._maybe_play_alert_sound = MethodType(VoiceTabMixin._maybe_play_alert_sound, host)
        started = threading.Event()
        with patch.object(tts.platform, "system", return_value="Windows"), \
                patch.object(tts, "_worker_started", started):
            with patch.object(tts.threading.Thread, "start", side_effect=RuntimeError("No speech worker")):
                VoiceTabMixin._emit_alert(host, "Spread", "alarm")
            visual.assert_called_once_with("Spread", "alarm")
            self.assertFalse(started.is_set())
            self.assertTrue(tts._queue.empty())
            self.assertEqual(tts._notification_slots._value, 4)
            self.assertTrue(any(call.args[0] == "tts-notify" and "No speech worker" in call.args[1]
                                for call in self.log.call_args_list))
            with patch.object(tts.threading.Thread, "start") as retry:
                tts.play_notification(str(self.sound), .5)
            retry.assert_called_once()
            self.assertTrue(started.is_set())
            self.assertEqual(tts._queue.get_nowait(), ("wav", str(self.sound), .5))
            self.launch.assert_not_called()


class SpeechPlaybackLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(tts.platform, "system", return_value="Linux"))
        self.stack.enter_context(patch.object(tts, "_current_proc", None))
        self.stack.enter_context(patch.object(tts, "_interrupted_proc", None))
        self.stack.enter_context(patch.object(tts, "record"))
        self.stack.enter_context(patch.object(tts, "log_drop"))
        self.stack.enter_context(patch.object(tts, "_wav_seconds", return_value=1))
        self.child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        self.addCleanup(self.reap_child)
        self.stack.enter_context(patch.object(tts.subprocess, "Popen", return_value=self.child))

    def reap_child(self):
        if self.child.poll() is None:
            self.child.kill()
        self.child.wait(timeout=5)
        self.child.stderr.close()

    def test_speech_player_read_failure_reaps_its_child_before_releasing_ownership(self):
        with patch.object(self.child, "communicate", side_effect=OSError("read failed")):
            with self.assertRaisesRegex(OSError, "read failed"):
                tts._play_wav("/unused.wav", tts._generation)
        self.assertIsNotNone(self.child.poll())
        self.assertIsNone(tts._current_proc)
        self.assertTrue(self.child.stderr.closed)

    def test_speech_player_timeout_reaps_its_child_when_the_final_read_also_times_out(self):
        error = subprocess.TimeoutExpired("aplay", 60)
        with patch.object(self.child, "communicate", side_effect=error):
            tts._play_wav("/unused.wav", tts._generation)
        self.assertIsNotNone(self.child.poll())
        self.assertIsNone(tts._current_proc)
        self.assertTrue(self.child.stderr.closed)

    def test_system_speech_wait_failure_reaps_its_child(self):
        wait = self.child.wait
        calls = 0

        def fail_first_wait(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError("wait failed")
            return wait(*args, **kwargs)

        with patch.object(self.child, "wait", side_effect=fail_first_wait):
            with self.assertRaisesRegex(OSError, "wait failed"):
                tts._run_speak_proc(["espeak"], "Stack", stdin_text=False, gen=tts._generation)
        self.assertIsNotNone(self.child.poll())
        self.assertIsNone(tts._current_proc)
        self.assertTrue(self.child.stderr.closed)

    def test_system_speech_broken_pipe_reaps_before_allowing_the_next_callout(self):
        with patch.object(self.child, "communicate", side_effect=OSError("pipe failed")):
            self.assertTrue(tts._run_speak_proc(
                ["powershell"], "Stack", stdin_text=True, gen=tts._generation))
        self.assertIsNotNone(self.child.returncode)
        self.assertIsNone(tts._current_proc)
        self.assertTrue(self.child.stderr.closed)

    def test_unreaped_speech_child_blocks_another_backend_from_replacing_it(self):
        timeout = subprocess.TimeoutExpired("aplay", 5)
        with patch.object(self.child, "communicate", side_effect=timeout), \
                patch.object(self.child, "kill"), \
                patch.object(self.child, "wait", side_effect=timeout):
            with self.assertRaises(subprocess.TimeoutExpired):
                tts._play_wav("/unused.wav", tts._generation)
        self.assertIsNone(self.child.poll())
        self.assertIs(tts._current_proc, self.child)
        replacement = Mock(returncode=0, stdin=None, stderr=None)
        replacement.communicate.return_value = (None, b"")
        for backend in ("wav", "system"):
            with self.subTest(backend=backend), \
                    patch.object(tts, "_current_proc", self.child), \
                    patch.object(tts.subprocess, "Popen", return_value=replacement) as launch:
                if backend == "wav":
                    tts._play_wav("/unused.wav", tts._generation)
                else:
                    self.assertTrue(tts._run_speak_proc(
                        ["espeak"], "Stack", stdin_text=False, gen=tts._generation))
                launch.assert_not_called()
                self.assertIs(tts._current_proc, self.child)
        self.child.kill()
        self.child.wait(timeout=5)
        for backend in ("wav", "system"):
            with self.subTest(recovered_backend=backend), \
                    patch.object(tts, "_current_proc", self.child), \
                    patch.object(tts.subprocess, "Popen", return_value=replacement) as launch:
                if backend == "wav":
                    tts._play_wav("/unused.wav", tts._generation)
                else:
                    self.assertTrue(tts._run_speak_proc(
                        ["espeak"], "Stack", stdin_text=False, gen=tts._generation))
                launch.assert_called_once()
                self.assertIsNone(tts._current_proc)


if __name__ == "__main__":
    unittest.main()
