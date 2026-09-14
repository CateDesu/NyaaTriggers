"""Reader failures and surviving child processes must not strand a sidecar."""

from contextlib import ExitStack
import io
import json
import os
import queue
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from nyaatriggers import triggevent_bridge as tv
from nyaatriggers import triggernometry_bridge as tn


BRIDGES = ((tv, tv.TriggeventBridge), (tn, tn.TriggernometryBridge))


class BrokenStream:
    def readline(self, limit):
        raise OSError("stdout failed")


class SidecarRecoveryTests(unittest.TestCase):
    def reader_case(self, module, cls, stream, *, stale=False, parser=None):
        bridge = cls()
        bridge._active = True
        bridge._gen = 3
        proc = SimpleNamespace(stdout=stream, poll=lambda: 0)
        live_proc = object() if stale else proc
        bridge._proc = live_proc
        relay = Mock()
        relay.is_finished.return_value = False
        if module is tn:
            bridge._telesto = relay
        pending = queue.Queue(maxsize=1)
        pending.put("old input")
        speech = []
        bridge.tts.connect(lambda text, gen: speech.append(text))
        with ExitStack() as stack:
            reaped = stack.enter_context(patch.object(bridge, "_reap"))
            stack.enter_context(patch.object(module, "_log"))
            stack.enter_context(patch.object(module, "log_drop"))
            if parser is not None:
                stack.enter_context(patch.object(module.json, "loads", side_effect=parser))
            gen = 1 if stale else 3
            if module is tv:
                bridge._read_loop(proc, pending, {}, gen)
            else:
                bridge._read_loop(proc, pending, gen)
            reaped.assert_called_once_with(proc)
        self.assertIs(pending.get_nowait(), module._STOP)
        self.assertEqual(bridge.is_active(), stale)
        self.assertIs(bridge._proc, live_proc if stale else None)
        if module is tn:
            if stale:
                self.assertIs(bridge._telesto, relay)
                relay.close.assert_not_called()
            else:
                self.assertIsNone(bridge._telesto)
                relay.close.assert_called_once_with()
        return speech

    def test_recursion_error_does_not_drop_the_next_callout(self):
        loads = json.loads

        def parser(line):
            if line == '{"deep":true}':
                raise RecursionError("nested JSON")
            return loads(line)

        for module, cls in BRIDGES:
            with self.subTest(bridge=cls.__name__):
                stream = io.StringIO('{"deep":true}\n{"t":"callout","tts":"next"}\n')
                self.assertEqual(self.reader_case(module, cls, stream, parser=parser), ["next"])

    def test_deep_json_with_the_running_interpreter(self):
        frame = '{"deep":' + '[' * 1200 + '0' + ']' * 1200 + '}\n'
        for module, cls in BRIDGES:
            with self.subTest(bridge=cls.__name__):
                stream = io.StringIO(frame + '{"t":"callout","tts":"next"}\n')
                self.assertEqual(self.reader_case(module, cls, stream), ["next"])

    def test_read_failure_releases_current_and_retired_processes(self):
        for module, cls in BRIDGES:
            for stale in (False, True):
                with self.subTest(bridge=cls.__name__, stale=stale):
                    self.reader_case(module, cls, BrokenStream(), stale=stale)

    def test_missing_stdout_still_cleans_up(self):
        for module, cls in BRIDGES:
            with self.subTest(bridge=cls.__name__):
                self.reader_case(module, cls, None)

    def test_repeated_cleanup_does_not_signal_a_retired_process_group(self):
        for module, cls in BRIDGES:
            with self.subTest(bridge=cls.__name__):
                proc = Mock(pid=12345)
                proc._nyaa_reaped = False
                with patch.object(module.os, "name", "posix"), \
                        patch.object(module.os, "killpg", side_effect=ProcessLookupError) as killpg:
                    cls._reap(proc)
                    first_calls = killpg.call_count
                    cls._reap(proc)
                    self.assertEqual(killpg.call_count, first_calls)
                    proc.wait.assert_called_once_with(timeout=4)

    @unittest.skipUnless(sys.platform == "linux" and shutil.which("xvfb-run"),
                         "Requires Linux and Xvfb")
    def test_stop_reaps_a_child_after_the_wrapper_exits(self):
        for module, cls in BRIDGES:
            with self.subTest(bridge=cls.__name__):
                result = subprocess.run(
                    [sys.executable, "-m", __spec__.name, "--group-check", module.__name__],
                    capture_output=True, text=True, timeout=15)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("child killed after wrapper exit", result.stdout)


def group_check(module_name):
    import ctypes
    import importlib

    # Keep orphaned fixture children under this test process for cleanup.
    assert ctypes.CDLL(None).prctl(36, 1, 0, 0, 0) == 0
    module = importlib.import_module(module_name)
    cls = module.TriggeventBridge if module is tv else module.TriggernometryBridge
    child_code = (
        "import os,signal,time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "print(os.getpid(), flush=True); time.sleep(30)"
    )
    with tempfile.TemporaryDirectory() as temp, \
            patch.dict(os.environ, {"XDG_CONFIG_HOME": temp}):
        proc = subprocess.Popen(
            ["xvfb-run", "-a", sys.executable, "-c", child_code],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, start_new_session=True)
        child = None
        try:
            assert select.select([proc.stdout], [], [], 5)[0], "child did not start"
            child = int(proc.stdout.readline())
            bridge = cls()
            bridge._active = True
            bridge._gen = 1
            bridge._proc = proc
            bridge.stop(wait=True)
            assert proc.poll() is not None, "wrapper still running"
            deadline = time.monotonic() + 1
            while time.monotonic() < deadline:
                pid, status = os.waitpid(child, os.WNOHANG)
                if pid:
                    child = None
                    assert os.WIFSIGNALED(status) and os.WTERMSIG(status) == signal.SIGKILL
                    print("child killed after wrapper exit")
                    break
                time.sleep(0.01)
            else:
                raise AssertionError("child survived stop with wait")
        finally:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait(timeout=3)
            if child is not None:
                os.waitpid(child, 0)
            proc.stdin.close()
            proc.stdout.close()
            proc.stderr.close()
            while True:
                try:
                    pid, _ = os.waitpid(-1, os.WNOHANG)
                except ChildProcessError:
                    break
                if not pid:
                    break


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--group-check":
        group_check(sys.argv[2])
    else:
        unittest.main()
