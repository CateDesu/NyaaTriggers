"""Capture native failures that bypass the program's Python exception handler."""

import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest


ROOT = Path(__file__).resolve().parent.parent


class NativeCrashLogTests(unittest.TestCase):
    def run_child(self, directory, body):
        setup = """
            import os
            import signal
            import sys
            from pathlib import Path
            from nyaatriggers import drop_log

            directory = Path(sys.argv[1])
            drop_log.data_root = lambda: directory
            drop_log._LOG_FILE = directory / "nyaatriggers.log"
            if os.name == "posix":
                import resource
                resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
            if sys.platform == "linux":
                import ctypes
                ctypes.CDLL(None).prctl(4, 0, 0, 0, 0)
        """
        return subprocess.run(
            [sys.executable, "-c", textwrap.dedent(setup) + textwrap.dedent(body),
             str(directory)], cwd=ROOT, capture_output=True, text=True, timeout=15)

    @unittest.skipUnless(os.name == "posix", "Requires a native SIGSEGV signal")
    def test_native_crash_captures_main_and_worker_stacks(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.run_child(directory, """
                import threading
                assert drop_log.enable_native_crash_log()
                assert drop_log.enable_native_crash_log()
                started = threading.Event()
                stop = threading.Event()

                def background_worker():
                    started.set()
                    stop.wait()

                threading.Thread(target=background_worker, daemon=True).start()
                started.wait()

                def crash_editor():
                    os.kill(os.getpid(), signal.SIGSEGV)

                crash_editor()
            """)
            self.assertEqual(result.returncode, -signal.SIGSEGV, result.stderr)
            path = Path(directory) / "nyaatriggers.crash.log"
            text = path.read_text()
            self.assertIn("Fatal Python error: Segmentation fault", text)
            self.assertIn("crash_editor", text)
            self.assertIn("background_worker", text)
            self.assertIn(sys.version, text)
            self.assertEqual(text.count("\nSTART "), 1)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_rotation_preserves_the_previous_crash(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nyaatriggers.crash.log"
            backup = path.with_name(path.name + ".1")
            previous = "previous crash\n" * 100
            path.write_text(previous)
            path.chmod(0o666)
            backup.write_text("older crash")
            backup.chmod(0o666)
            result = self.run_child(directory, """
                import faulthandler
                drop_log._MAX_BYTES = 100
                assert drop_log.enable_native_crash_log()
                assert drop_log.enable_native_crash_log()
                faulthandler.dump_traceback(file=drop_log._native_crash_file)
            """)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(backup.read_text(), previous)
            self.assertIn("Current thread", path.read_text())
            self.assertEqual(path.read_text().count("\nSTART "), 1)
            if os.name == "posix":
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o600)

    def test_unwritable_log_does_not_prevent_startup(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "nyaatriggers.crash.log").mkdir()
            result = self.run_child(directory, """
                assert not drop_log.enable_native_crash_log()
                assert drop_log._native_crash_file is None
                print("startup continues")
            """)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("startup continues", result.stdout)
            self.assertIn("could not enable native crash logging",
                          (Path(directory) / "nyaatriggers.log").read_text())

    def test_write_and_close_failures_do_not_prevent_startup_or_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.run_child(directory, """
                import errno
                import io
                from unittest.mock import patch

                class FullDisk(io.RawIOBase):
                    def writable(self):
                        return True

                    def write(self, data):
                        raise OSError(errno.ENOSPC, 'disk full')

                log = io.TextIOWrapper(io.BufferedWriter(FullDisk()), encoding='utf-8')
                with patch.object(drop_log, 'open_private_log', return_value=log):
                    assert not drop_log.enable_native_crash_log()
                assert log.closed
                assert drop_log._native_crash_file is None
                assert drop_log.enable_native_crash_log()
                assert drop_log._native_crash_file is not None
                print('startup continues')
            """)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('startup continues', result.stdout)
            self.assertIn('disk full', (Path(directory) / 'nyaatriggers.log').read_text())


if __name__ == "__main__":
    unittest.main()
