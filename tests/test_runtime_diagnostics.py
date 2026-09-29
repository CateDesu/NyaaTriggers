"""Runtime evidence stays useful without recording the data being delivered."""

from contextlib import ExitStack
import json
from pathlib import Path
from queue import Queue
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PyQt6.QtCore import QCoreApplication

from nyaatriggers import diagnostics, tts
from nyaatriggers.ws_client import WSClient


APP = QCoreApplication.instance() or QCoreApplication([])
PRIVATE = "PersonalCanary /home/PrivateUser token=SecretValue"


class RuntimeDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.log = self.root / "diagnostics.log"
        self.stack.enter_context(patch.object(diagnostics, "_LOG_FILE", self.log))
        self.stack.enter_context(patch.object(diagnostics, "_ENABLED", True))
        self.stack.enter_context(patch.object(tts, "log_drop"))

    def rows(self, event):
        text = self.log.read_text() if self.log.exists() else ""
        for secret in ("PersonalCanary", "PrivateUser", "SecretValue"):
            self.assertNotIn(secret, text)
        return [row["data"] for line in text.splitlines()
                if (row := json.loads(line))["event"] == event]

    def test_feed_diagnostics_preserve_raw_and_surrogate_messages(self):
        client = WSClient()
        received = []
        client.engine_message.connect(received.append)
        client._diag_at = 0
        raw = "00|2026-09-28T12:00:00.000Z|0038|" + PRIVATE
        client._on_message(raw)
        client._on_message(json.dumps({"type": "ChangePrimaryPlayer", "charName": PRIVATE, "charID": 123}))
        # Diagnostics must not raise while counting an invalid Unicode payload.
        client._on_message('{"type":"probe","text":"\ud800"}')
        self.assertEqual(received[0], raw)
        self.assertEqual(len(received), 3)
        self.assertEqual(self.rows("ws_feed")[0]["frames"], 1)

    def test_queue_evidence_distinguishes_eviction_and_muting_without_text(self):
        queue = Queue(maxsize=1)
        with patch.object(tts, "_queue", queue), patch.object(tts, "_master_volume", 1), \
                patch.object(tts, "_speech_suspended", False):
            tts._enqueue(("tts", PRIVATE, 1, 1, None))
            tts._enqueue(("tts", "next", 1, 1, None))
            self.assertEqual(queue.get_nowait()[1], "next")
            with patch.object(tts, "_master_volume", 0):
                tts._enqueue(("tts", PRIVATE, 1, 1, None))
            self.assertTrue(queue.empty())
        self.assertEqual([r["result"] for r in self.rows("tts_queue")],
                         ["queued", "evicted", "queued", "muted"])

    def test_failed_voice_and_failed_player_have_separate_evidence(self):
        with patch.object(tts, "_master_volume", 1), patch.object(tts, "_engine", "piper"), \
                patch.object(tts, "_jp_neural", False), patch.object(tts, "_jp_auto", False), \
                patch.object(tts, "_load_piper", return_value=None), patch.object(tts, "_piper_failed", True):
            tts._pipeline(PRIVATE)
        self.assertEqual([r["result"] for r in self.rows("tts_backend")], ["attempt", "unavailable"])
        process = SimpleNamespace(returncode=17, communicate=lambda **_: (b"", PRIVATE.encode()))
        with patch.object(tts.platform, "system", return_value="Linux"), \
                patch.object(tts.subprocess, "Popen", return_value=process), \
                patch.object(tts, "_wav_seconds", return_value=1.0):
            tts._play_wav("/home/PrivateUser/voice.wav")
        self.assertEqual(self.rows("tts_backend")[-1],
                         {"backend": "aplay", "result": "failed", "returncode": 17})

    def test_unwritable_diagnostic_sink_does_not_stop_feed_or_queue(self):
        with patch.object(diagnostics, "_LOG_FILE", self.root):
            client = WSClient()
            received = []
            client.engine_message.connect(received.append)
            client._diag_at = 0
            client._on_message(PRIVATE)
            self.assertEqual(received, [PRIVATE])
            queue = Queue()
            with patch.object(tts, "_queue", queue), patch.object(tts, "_master_volume", 1), \
                    patch.object(tts, "_speech_suspended", False):
                tts._enqueue(("tts", PRIVATE, 1, 1, None))
            self.assertEqual(queue.get_nowait()[1], PRIVATE)


if __name__ == "__main__":
    unittest.main()
