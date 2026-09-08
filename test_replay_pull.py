"""Replay timing tests with real pipes and a small stand-in engine."""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from tools import replay_pull


def log_line(stamp):
    return json.dumps({"type": "LogLine", "line": ["00", stamp, "test"]})


class ReplayTests(unittest.TestCase):
    def run_replay(self, lines, receiver, *options):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            capture = root / "pull.jsonl"
            capture.write_text("\n".join(lines), encoding="utf-8")
            jar = root / "engine.jar"
            jar.touch()
            script = root / "receiver.py"
            script.write_text(receiver, encoding="utf-8")
            args = ["replay_pull", str(capture), "--jar", str(jar),
                    "--hold", "0", "--timeout", "5", *options]
            output = io.StringIO()
            started = time.monotonic()
            with patch.object(sys, "argv", args), patch.object(
                    replay_pull, "_java_cmd", return_value=[sys.executable, "-u", str(script)]), \
                    contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                result = replay_pull.main()
            return result, time.monotonic() - started, output.getvalue()

    def test_pacing_and_hold_reach_receiver(self):
        code, elapsed, output = self.run_replay(
            [log_line("2026-09-07T00:00:00Z"), log_line("2026-09-07T00:00:00.600Z")],
            """import json, sys, time
stamps = [time.monotonic() for line in sys.stdin]
gap = stamps[1] - stamps[0]
hold = time.monotonic() - stamps[-1]
print(json.dumps({'t': 'callout', 'text': f'{gap:.3f} {hold:.3f}'}))
""", "--hold", "0.3")
        self.assertEqual(code, 0, output)
        timing = next(line for line in output.splitlines() if "callout:" in line)
        gap, hold = map(float, timing.split(":", 1)[1].split())
        self.assertGreater(gap, 0.35)
        self.assertGreater(hold, 0.2)

    def test_timestamp_sleep_obeys_deadline(self):
        code, elapsed, output = self.run_replay(
            [log_line("2026-09-07T00:00:00Z"), log_line("2026-09-07T00:00:30Z")],
            "import sys\nfor line in sys.stdin: pass\n", "--timeout", "1")
        self.assertEqual(code, 1, output)
        self.assertIn("did not finish within 1s", output)
        self.assertLess(elapsed, 3)

    def test_full_pipe_obeys_deadline(self):
        code, elapsed, output = self.run_replay(
            [json.dumps({"padding": "x" * 2_000_000})],
            "import time\ntime.sleep(60)\n", "--timeout", "1")
        self.assertEqual(code, 1, output)
        self.assertLess(elapsed, 3)

    def test_hold_and_exit_obey_deadline(self):
        for receiver, options in (
                ("import sys\nfor line in sys.stdin: pass\n", ["--hold", "30"]),
                ("import sys, time\nsys.stdin.read()\ntime.sleep(30)\n", [])):
            code, elapsed, output = self.run_replay(["{}"], receiver, "--timeout", "1", *options)
            self.assertEqual(code, 1, output)
            self.assertLess(elapsed, 3)

    @unittest.skipUnless(os.name == "posix", "requires process groups")
    def test_exited_wrapper_does_not_orphan_blocked_child(self):
        code, elapsed, output = self.run_replay(
            [json.dumps({"padding": "x" * 2_000_000})],
            "import os, time\nif os.fork(): os._exit(0)\ntime.sleep(60)\n",
            "--timeout", "1")
        self.assertEqual(code, 1, output)
        self.assertLess(elapsed, 3)

    def test_raw_json_shapes_and_mixed_timestamps(self):
        lines = ["[]", "null", "42", '"text"', "{bad", "{\"type\":\"LogLine\",\"line\":{}}",
                 log_line("2026-09-07T00:00:00Z"), log_line("2026-09-07T00:00:00.100")]
        code, elapsed, output = self.run_replay(lines,
            "import sys\nfor line in sys.stdin: pass\n")
        self.assertEqual(code, 0, output)
        self.assertIsNone(replay_pull._line_time(log_line("invalid")))
        self.assertEqual(replay_pull._line_time(log_line("2026-09-07T00:00:00Z")),
                         replay_pull._line_time(log_line("2026-09-06T19:00:00-05:00")))
        self.assertIsNone(replay_pull._line_time("9" * 5000))

    def test_invalid_timing_values_fail_cleanly(self):
        for options in (["--speed", "nan"], ["--hold", "inf"], ["--timeout", "-1"],
                        ["--speed", "1e-320"]):
            code, elapsed, output = self.run_replay(
                [log_line("2026-09-07T00:00:00Z"), log_line("2026-09-07T00:00:01Z")],
                "import sys\nsys.stdin.read()\n", *options)
            self.assertEqual(code, 1, output)
            self.assertIn("ERROR:", output)


if __name__ == "__main__":
    unittest.main()
