from contextlib import ExitStack
from pathlib import Path
from queue import Queue
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from nyaatriggers import tts


class SynthesisRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.release = threading.Event()
        self.addCleanup(self.release.set)
        self.entered = threading.Event()
        self.threads = []
        self.addCleanup(self.finish_threads)
        for name, value in {
                "_generation": 10, "_queue": Queue(), "_current_proc": None,
                "_engine": "piper", "_master_volume": 1.0, "_jp_auto": True,
                "_jp_neural": False, "_piper_voice": None, "_piper_failed": False,
                "_kokoro": None, "_kokoro_failed": False,
                "_synthesis_slots": threading.BoundedSemaphore(2),
                "_SYNTH_TIMEOUT_S": 2.0}.items():
            self.stack.enter_context(patch.object(tts, name, value))
        self.playback = self.stack.enter_context(patch.object(tts, "_play_wav"))
        self.fallback = self.stack.enter_context(patch.object(tts, "_system_speak", return_value=True))
        self.stack.enter_context(patch.object(tts, "log_drop"))

    def finish_threads(self):
        self.release.set()
        for thread in self.threads:
            thread.join(3)

    def blocked_call(self, text, gen=10):
        errors = []

        def run():
            try:
                tts._pipeline(text, gen=gen)
            except Exception as exc:
                errors.append(exc)

        worker = threading.Thread(target=run)
        self.threads.append(worker)
        worker.start()
        self.assertTrue(self.entered.wait(1))
        tts.interrupt()
        worker.join(0.5)
        self.assertFalse(worker.is_alive(), "Canceled synthesis still holds the speech worker")
        self.assertEqual(errors, [])
        self.assertFalse(self.release.is_set())

    def test_canceled_piper_synthesis_leaves_the_next_pull_free_to_speak(self):
        owner = self

        class Voice:
            def __init__(self, blocked=False):
                self.blocked = blocked

            def synthesize_wav(self, text, wav, **kwargs):
                if self.blocked:
                    owner.entered.set()
                    owner.release.wait(3)
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(22050)
                wav.writeframes(b"\0\0")

        old = tts._piper_voice = Voice(blocked=True)
        fresh = Voice()

        def load(gen=None):
            if tts._piper_voice is None:
                tts._piper_voice = fresh
            return tts._piper_voice

        self.stack.enter_context(patch.object(tts, "_load_piper", side_effect=load))
        self.blocked_call("Previous pull")
        self.assertIsNone(tts._piper_voice)
        self.assertFalse(tts._piper_failed)
        tts._pipeline("Cones then lines", gen=11)
        self.playback.assert_called_once()
        self.assertIs(tts._piper_voice, fresh)
        self.assertIsNot(tts._piper_voice, old)
        self.fallback.assert_not_called()

    def test_canceled_kokoro_synthesis_leaves_the_next_pull_free_to_speak(self):
        import numpy as np
        owner = self

        class Voice:
            def __init__(self, blocked=False):
                self.blocked = blocked

            def create(self, text, **kwargs):
                if self.blocked:
                    owner.entered.set()
                    owner.release.wait(3)
                return np.zeros(10, dtype="float32"), 22050

        tts._jp_neural = True
        tts._kokoro = Voice(blocked=True)
        fresh = Voice()

        def load(gen=None):
            if tts._kokoro is None:
                tts._kokoro = fresh
            return tts._kokoro

        self.stack.enter_context(patch.object(tts, "_load_kokoro", side_effect=load))
        self.blocked_call("散開")
        self.assertIsNone(tts._kokoro)
        self.assertFalse(tts._kokoro_failed)
        tts._pipeline("頭割り", gen=11)
        self.playback.assert_called_once()
        self.assertIs(tts._kokoro, fresh)
        self.fallback.assert_not_called()

    def test_repeated_interruptions_bound_abandoned_work_and_recover_after_it_finishes(self):
        owner = self
        active, peak, calls = [], [], []
        lock = threading.Lock()

        class Voice:
            def synthesize_wav(self, text, wav, **kwargs):
                with lock:
                    active.append(text)
                    calls.append(text)
                    peak.append(len(active))
                owner.entered.set()
                owner.release.wait(3)
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(22050)
                wav.writeframes(b"\0\0")
                with lock:
                    active.remove(text)

        def load(gen=None):
            if tts._piper_voice is None:
                tts._piper_voice = Voice()
            return tts._piper_voice

        self.stack.enter_context(patch.object(tts, "_load_piper", side_effect=load))
        self.blocked_call("First canceled call")
        self.entered.clear()
        self.blocked_call("Second canceled call", gen=11)
        tts._pipeline("Capacity full", gen=12)
        self.assertEqual(calls, ["First canceled call", "Second canceled call"])
        self.assertEqual(max(peak), 2)
        self.assertFalse(tts._piper_failed)
        self.playback.assert_not_called()
        self.fallback.assert_not_called()
        self.release.set()
        for _ in range(2):
            self.assertTrue(tts._synthesis_slots.acquire(timeout=1))
        for _ in range(2):
            tts._synthesis_slots.release()
        tts._pipeline("Fresh speech", gen=12)
        self.playback.assert_called_once()
        self.assertEqual(calls[-1], "Fresh speech")

    def test_live_synthesis_timeout_keeps_the_failed_session_retired(self):
        owner = self

        class Voice:
            def synthesize_wav(self, text, wav, **kwargs):
                owner.entered.set()
                owner.release.wait(3)
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(22050)
                wav.writeframes(b"\0\0")

        tts._piper_voice = Voice()
        tts._SYNTH_TIMEOUT_S = 0.05
        tts._pipeline("Stalled current call", gen=10)
        self.assertTrue(tts._piper_failed)
        self.assertIsNone(tts._piper_voice)
        tts._pipeline("Next call", gen=10)
        self.playback.assert_not_called()
        self.fallback.assert_not_called()

    def test_canceled_piper_construction_does_not_fail_the_next_pull(self):
        folder = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        model = folder / "voice.onnx"
        Path(str(model) + ".json").write_text("{}")
        builds = []
        owner = self

        class Options:
            def add_session_config_entry(self, *args):
                pass

        class Voice:
            def __init__(self, **kwargs):
                pass

            def synthesize_wav(self, text, wav, **kwargs):
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(22050)
                wav.writeframes(b"\0\0")

        def construct(*args, **kwargs):
            builds.append(True)
            if len(builds) == 1:
                owner.entered.set()
                owner.release.wait(3)
            return object()

        modules = {
            "onnxruntime": SimpleNamespace(SessionOptions=Options, InferenceSession=construct),
            "piper.config": SimpleNamespace(PiperConfig=SimpleNamespace(from_dict=lambda data: data)),
            "piper.voice": SimpleNamespace(PiperVoice=Voice),
        }
        self.stack.enter_context(patch.dict(sys.modules, modules))
        self.stack.enter_context(patch.object(tts, "_PIPER_MODEL", model))
        self.stack.enter_context(patch.object(tts, "_purge_stale_venv_modules"))
        self.blocked_call("Previous pull construction")
        self.assertFalse(tts._piper_failed)
        self.assertIsNone(tts._piper_voice)
        tts._pipeline("Fresh call", gen=11)
        self.playback.assert_called_once()
        self.assertEqual(len(builds), 2)
        self.fallback.assert_not_called()

    def test_canceled_kokoro_construction_does_not_fail_the_next_pull(self):
        import numpy as np
        folder = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        model, voices = folder / "model.onnx", folder / "voices.bin"
        model.touch()
        voices.touch()
        builds = []
        owner = self

        class Voice:
            def __init__(self, *args):
                builds.append(True)
                if len(builds) == 1:
                    owner.entered.set()
                    owner.release.wait(3)

            def create(self, text, **kwargs):
                return np.zeros(10, dtype="float32"), 22050

        self.stack.enter_context(patch.dict(sys.modules, {"kokoro_onnx": SimpleNamespace(Kokoro=Voice)}))
        self.stack.enter_context(patch.object(tts, "_KOKORO_MODEL", model))
        self.stack.enter_context(patch.object(tts, "_KOKORO_VOICES", voices))
        self.stack.enter_context(patch.object(tts, "_purge_stale_venv_modules"))
        tts._jp_neural = True
        self.blocked_call("散開")
        self.assertFalse(tts._kokoro_failed)
        self.assertIsNone(tts._kokoro)
        tts._pipeline("頭割り", gen=11)
        self.playback.assert_called_once()
        self.assertEqual(len(builds), 2)
        self.fallback.assert_not_called()

    def test_interrupt_before_loader_entry_does_not_start_construction_with_a_fresh_stamp(self):
        for backend, text in (("piper", "Spread"), ("kokoro", "散開")):
            with self.subTest(backend=backend):
                tts._generation = 10
                tts._jp_neural = backend == "kokoro"
                loader = getattr(tts, "_load_" + backend)

                def interrupt_then_load(gen):
                    tts.interrupt()
                    return loader(gen)

                with patch.object(tts, "_load_" + backend, side_effect=interrupt_then_load), \
                        patch.object(tts, "_synth_call") as construction:
                    tts._pipeline(text, gen=10)
                construction.assert_not_called()
                self.assertFalse(tts._piper_failed)
                self.assertFalse(tts._kokoro_failed)
                self.playback.assert_not_called()
                self.fallback.assert_not_called()

    def test_completed_constructor_error_only_fails_its_live_generation(self):
        import numpy as np
        folder = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        model, voices = folder / "model.onnx", folder / "voices.bin"
        model.touch()
        voices.touch()
        Path(str(model) + ".json").write_text("{}")

        class Options:
            def add_session_config_entry(self, *args):
                pass

        class Voice:
            def __init__(self, *args, **kwargs):
                pass

            def synthesize_wav(self, text, wav, **kwargs):
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(22050)
                wav.writeframes(b"\0\0")

            def create(self, text, **kwargs):
                return np.zeros(10, dtype="float32"), 22050

        class CompletedThread(threading.Thread):
            def start(self):
                super().start()
                self.join(1)

        for backend, text in (("piper", "Spread"), ("kokoro", "散開")):
            for canceled in (True, False):
                with self.subTest(backend=backend, canceled=canceled), ExitStack() as stack:
                    builds = []
                    tts._generation = 10
                    tts._piper_voice = tts._kokoro = None
                    tts._piper_failed = tts._kokoro_failed = False
                    tts._jp_neural = backend == "kokoro"
                    self.playback.reset_mock()
                    self.fallback.reset_mock()

                    def construct(*args, **kwargs):
                        builds.append(True)
                        if len(builds) == 1:
                            if canceled:
                                tts.interrupt()
                            raise RuntimeError("Constructor failed before its caller resumed")
                        return Voice()

                    modules = {
                        "onnxruntime": SimpleNamespace(SessionOptions=Options, InferenceSession=construct),
                        "piper.config": SimpleNamespace(PiperConfig=SimpleNamespace(from_dict=lambda data: data)),
                        "piper.voice": SimpleNamespace(PiperVoice=Voice),
                        "kokoro_onnx": SimpleNamespace(Kokoro=construct),
                    }
                    stack.enter_context(patch.dict(sys.modules, modules))
                    stack.enter_context(patch.object(tts.threading, "Thread", CompletedThread))
                    stack.enter_context(patch.multiple(tts, _PIPER_MODEL=model,
                                                      _KOKORO_MODEL=model, _KOKORO_VOICES=voices))
                    stack.enter_context(patch.object(tts, "_purge_stale_venv_modules"))
                    stack.enter_context(patch.object(tts, "_log_once"))
                    tts._pipeline(text, gen=10)
                    self.assertEqual(getattr(tts, "_" + backend + "_failed"), not canceled)
                    tts._pipeline(text, gen=tts._generation)
                    if canceled:
                        self.assertEqual(len(builds), 2)
                        self.playback.assert_called_once()
                        self.fallback.assert_not_called()
                    else:
                        self.assertEqual(len(builds), 1)
                        self.playback.assert_not_called()


if __name__ == "__main__":
    unittest.main()
