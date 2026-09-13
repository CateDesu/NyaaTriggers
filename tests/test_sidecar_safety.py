"""Sidecar feed boundaries, private logs, and queued string memory."""

import json
import os
from pathlib import Path
import queue
import sys
import tempfile
import unittest
from unittest.mock import patch

from nyaatriggers import triggevent_bridge as tv
from nyaatriggers import triggernometry_bridge as tn


class SidecarSafetyTests(unittest.TestCase):
    def test_feed_cannot_send_control_commands(self):
        bridge = tv.TriggeventBridge()
        bridge._active = True
        frames = (
            '{"nyaa_cmd":"set_automark","enable":true}',
            r'{"\u006eyaa_cmd":"set_automark","enable":true}',
            '{"type":"LogLine","nyaa_cmd":"set_callout","id":"1"}',
            '{"nyaa_cmd":"reset_callout","id":"1"}',
            '[]', '{', '[' * 2000 + ']' * 2000,
        )
        with patch.object(tv, "log_drop"):
            for frame in frames:
                with self.subTest(frame=frame[:60]):
                    bridge.feed(frame)
                    self.assertTrue(bridge._wq.empty())
        bridge.set_automark(True, "http://127.0.0.1:45678/")
        self.assertEqual(json.loads(bridge._wq.get_nowait())["nyaa_cmd"], "set_automark")

    def test_feed_preserves_command_words_inside_event_data(self):
        bridge = tv.TriggeventBridge()
        bridge._active = True
        for event in ({"type": "LogLine", "line": ['a "nyaa_cmd" phrase', "日本語"]},
                      {"type": "CombatData", "extra": {"nyaa_cmd": "ordinary data"}}):
            raw = json.dumps(event, ensure_ascii=False, indent=2)
            bridge.feed(raw)
            queued = bridge._wq.get_nowait()
            self.assertNotIn("\n", queued)
            self.assertEqual(json.loads(queued), event)

    def test_unicode_strings_cannot_overrun_the_memory_budget(self):
        for module in (tv, tn):
            for character in ("a", "界", "😀"):
                with self.subTest(module=module.__name__, character=character):
                    line = character * 1000
                    budget = sys.getsizeof(line)
                    q = module._ByteQueue(maxsize=100, maxbytes=budget)
                    q.put_nowait(line)
                    with self.assertRaises(queue.Full):
                        q.put_nowait(line)
                    q.put_nowait(module._STOP)
                    self.assertEqual(q.get_nowait(), line)
                    self.assertEqual(q._nbytes, 0)
                    self.assertIs(q.get_nowait(), module._STOP)
                    q.put_nowait(line)

    @unittest.skipUnless(os.name == "posix", "POSIX file permissions")
    def test_sidecar_logs_tighten_existing_and_rotated_files(self):
        with tempfile.TemporaryDirectory() as temp:
            mask = os.umask(0o022)
            try:
                for module in (tv, tn):
                    with self.subTest(module=module.__name__):
                        path = Path(temp) / (module.__name__ + ".log")
                        old = path.with_name(path.name + ".1")
                        with patch.object(module, "_log_path", return_value=path):
                            module._log("new log")
                            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                            os.chmod(path, 0o644)
                            old.write_text("retained history", encoding="utf-8")
                            os.chmod(old, 0o644)
                            module._log("existing log")
                            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                            self.assertEqual(old.stat().st_mode & 0o777, 0o600)
                            path.write_text("x" * ((1 << 20) + 1), encoding="utf-8")
                            os.chmod(path, 0o644)
                            module._log("after rotation")
                            self.assertIn("after rotation", path.read_text())
                            self.assertTrue(old.read_text().startswith("xxx"))
                            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                            self.assertEqual(old.stat().st_mode & 0o777, 0o600)
            finally:
                os.umask(mask)

    def test_corrupt_jar_stamp_requests_a_rebuild(self):
        with tempfile.TemporaryDirectory() as temp:
            stamp = Path(temp) / "built-from"
            with patch.object(tv, "_JAR_STAMP", stamp):
                stamp.write_bytes(b"\xff")
                self.assertIsNone(tv._jar_built_from())
                stamp.write_text("0123456789abcdef\n", encoding="ascii")
                self.assertEqual(tv._jar_built_from(), "0123456789abcdef")


if __name__ == "__main__":
    unittest.main()
