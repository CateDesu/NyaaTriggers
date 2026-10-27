from contextlib import ExitStack
import json
import os
from pathlib import Path
from queue import Queue
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from nyaatriggers import tts
from tests.test_notification_lifecycle import Playback


@unittest.skipUnless(sys.platform == "linux", "Requires Linux playback")
class LinuxAudioPlaybackTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.calls = self.root / "calls.jsonl"
        self.sound = str(self.root / "sound with spaces.wav")
        program = self.root / "aplay"
        script = self.root / "aplay.py"
        script.write_text(
            "import json, os, sys\n"
            "device = 'pulse' if '-D' in sys.argv else 'default'\n"
            "with open(os.environ['NYAA_TEST_AUDIO_CALLS'], 'a') as output:\n"
            "    output.write(json.dumps({'device': device, 'args': sys.argv[1:]}) + '\\n')\n"
            "sys.exit(int(os.environ['NYAA_TEST_' + device.upper() + '_EXIT']))\n"
        )
        program.write_text(
            "#!/bin/sh\n"
            f'exec {shlex.quote(sys.executable)} {shlex.quote(str(script))} "$@"\n'
        )
        program.chmod(0o700)
        self.stack.enter_context(patch.dict(os.environ, {
            "PATH": str(self.root) + os.pathsep + os.environ.get("PATH", ""),
            "NYAA_TEST_AUDIO_CALLS": str(self.calls),
            "NYAA_TEST_PULSE_EXIT": "0", "NYAA_TEST_DEFAULT_EXIT": "0",
        }))
        for name, value in {
            "_current_proc": None, "_interrupted_proc": None, "_generation": 4,
            "_notification_epoch": 0, "_notification_procs": set(),
            "_master_volume": 1.0, "_speech_suspended": False, "_queue": Queue(),
        }.items():
            self.stack.enter_context(patch.object(tts, name, value))
        self.stack.enter_context(patch.object(tts, "_wav_seconds", return_value=0))
        self.record = self.stack.enter_context(patch.object(tts, "record"))
        self.log = self.stack.enter_context(patch.object(tts, "log_drop"))
        self.processes = []
        self.after_exit = None
        popen = subprocess.Popen

        def launch(*args, **kwargs):
            process = popen(*args, **kwargs)
            self.processes.append(process)
            wait = process.wait

            def finish(*args, **kwargs):
                result = wait(*args, **kwargs)
                if self.after_exit:
                    callback, self.after_exit = self.after_exit, None
                    callback()
                return result

            process.wait = finish
            return process

        self.launch = self.stack.enter_context(patch.object(tts.subprocess, "Popen", side_effect=launch))
        self.addCleanup(self.check_children)

    def check_children(self):
        for process in self.processes:
            self.assertIsNotNone(process.poll())
            self.assertTrue(process.stderr.closed)
        self.assertIsNone(tts._current_proc)
        self.assertEqual(tts._notification_procs, set())

    def devices(self):
        rows = [json.loads(line) for line in self.calls.read_text().splitlines()]
        for row in rows:
            self.assertEqual(row["args"][-2:], ["--", self.sound])
        return [row["device"] for row in rows]

    def play(self, kind):
        if kind == "speech":
            tts._play_wav(self.sound, gen=4)
        else:
            tts._play_wav_detached(self.sound, epoch=0)

    def test_speech_and_notifications_use_pulse_without_opening_the_stalled_default(self):
        for kind in ("speech", "notification"):
            with self.subTest(kind=kind):
                self.play(kind)
                self.assertEqual(self.devices(), ["pulse"])
                self.calls.unlink()
        self.log.assert_not_called()

    def test_missing_pulse_device_or_server_falls_back_without_reporting_a_dropped_callout(self):
        os.environ["NYAA_TEST_PULSE_EXIT"] = "1"
        for kind in ("speech", "notification"):
            with self.subTest(kind=kind):
                self.play(kind)
                self.assertEqual(self.devices(), ["pulse", "default"])
                self.calls.unlink()
        self.log.assert_not_called()
        failures = [call.kwargs for call in self.record.call_args_list
                    if call.args[0] == "tts_backend" and call.kwargs.get("result") == "failed"]
        self.assertEqual([row["device"] for row in failures], ["pulse"])

    def test_both_devices_failing_report_one_dropped_playback(self):
        os.environ["NYAA_TEST_PULSE_EXIT"] = os.environ["NYAA_TEST_DEFAULT_EXIT"] = "1"
        for kind in ("speech", "notification"):
            with self.subTest(kind=kind):
                self.log.reset_mock()
                self.play(kind)
                self.assertEqual(self.devices(), ["pulse", "default"])
                self.calls.unlink()
                self.log.assert_called_once()

    def test_interrupt_between_failed_pulse_and_fallback_cannot_replay_old_speech(self):
        os.environ["NYAA_TEST_PULSE_EXIT"] = "1"

        def interrupt(event, **data):
            if event == "tts_backend" and data.get("result") == "failed":
                tts.interrupt()

        self.record.side_effect = interrupt
        self.play("speech")
        self.assertEqual(self.devices(), ["pulse"])

    def test_mute_during_failed_pulse_cancels_notification_fallback(self):
        os.environ["NYAA_TEST_PULSE_EXIT"] = "1"
        self.after_exit = lambda: tts.set_master_volume(0)
        self.play("notification")
        self.assertEqual(self.devices(), ["pulse"])
        self.log.assert_not_called()

    def test_short_playback_stalls_are_killed_without_replaying_on_another_device(self):
        for kind in ("speech", "notification"):
            with self.subTest(kind=kind):
                process = Playback()
                with patch.object(tts, "_wav_seconds", return_value=1), \
                        patch.object(tts.subprocess, "Popen", return_value=process) as launch, \
                        patch.object(process, "communicate", side_effect=[
                            subprocess.TimeoutExpired("aplay", 7), (None, b"")]) as communicate:
                    self.play(kind)
                self.assertLessEqual(communicate.call_args_list[0].kwargs["timeout"], 7)
                launch.assert_called_once()
                self.assertEqual(process.returncode, -9)
                self.assertTrue(process.stderr.closed)

    def test_long_sounds_keep_enough_time_to_finish(self):
        for kind in ("speech", "notification"):
            with self.subTest(kind=kind):
                process = Playback(finished=True)
                with patch.object(tts, "_wav_seconds", return_value=100), \
                        patch.object(tts.subprocess, "Popen", return_value=process), \
                        patch.object(process, "communicate", return_value=(None, b"")) as communicate:
                    self.play(kind)
                self.assertGreater(communicate.call_args.kwargs["timeout"], 100)

    def test_missing_aplay_is_reported_without_retrying_the_same_executable(self):
        with patch.object(tts.subprocess, "Popen", side_effect=FileNotFoundError) as launch, \
                patch.object(tts, "_aplay_missing") as missing:
            self.play("speech")
        launch.assert_called_once()
        missing.assert_called_once()


if __name__ == "__main__":
    unittest.main()
