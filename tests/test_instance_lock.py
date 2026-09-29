"""Separate launches cannot overwrite one data folder's saved choices."""

from contextlib import ExitStack
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from nyaatriggers.instance_lock import InstanceLock

REPO = Path(__file__).resolve().parents[1]


class InstanceLockTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.data = self.root / "program"
        self.data.mkdir()
        self.cache = self.root / "cache"

    def lock(self, directory=None):
        lock = InstanceLock(directory or self.data, self.cache)
        self.addCleanup(lock.close)
        return lock

    def process(self, script, *args):
        process = subprocess.Popen([sys.executable, "-c", script, *map(str, args)],
                                   cwd=REPO, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, encoding="utf-8", errors="replace")

        def cleanup():
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=5)

        self.addCleanup(cleanup)
        return process

    @staticmethod
    def wait_for(path):
        deadline = time.monotonic() + 10
        while not path.exists():
            if time.monotonic() >= deadline:
                raise AssertionError(f"Timed out waiting for {path.name}")
            time.sleep(.01)

    def test_same_folder_and_alias_contend_while_separate_folders_coexist(self):
        first = self.lock()
        self.assertTrue(first.acquire())
        self.assertTrue(first.acquire())
        self.assertFalse(self.lock().acquire(timeout=0))
        second = self.root / "second"
        second.mkdir()
        self.assertTrue(self.lock(second).acquire(timeout=0))
        if os.name != "nt":
            alias = self.root / "alias"
            alias.symlink_to(self.data, target_is_directory=True)
            self.assertFalse(self.lock(alias).acquire(timeout=0))
        first.close()
        self.assertTrue(self.lock().acquire(timeout=0))

    @unittest.skipUnless(os.name == "posix" and os.geteuid() != 0, "Needs directory permissions")
    def test_read_only_program_folder_uses_the_same_cache_lock(self):
        self.data.chmod(0o500)
        self.addCleanup(self.data.chmod, 0o700)
        lock = self.lock()
        self.assertTrue(lock.acquire())
        self.assertEqual(list(self.data.iterdir()), [])
        self.data.chmod(0o700)
        self.assertFalse(self.lock().acquire(timeout=0))

    def test_simultaneous_processes_allow_only_one_writer(self):
        script = '''
from pathlib import Path
import sys, time
from nyaatriggers.instance_lock import InstanceLock
data, cache, root, role = map(Path, sys.argv[1:])
(root / ('ready-' + str(role))).touch()
while not (root / 'go').exists():
    time.sleep(.005)
lock = InstanceLock(data, cache)
owned = lock.acquire(timeout=0)
with (root / ('result-' + str(role))).open('w') as result:
    result.write(str(owned))
(root / ('done-' + str(role))).touch()
if owned:
    while not (root / 'release').exists():
        time.sleep(.005)
lock.close()
'''
        processes = [self.process(script, self.data, self.cache, self.root, role)
                     for role in ("first", "second")]
        for role in ("first", "second"):
            self.wait_for(self.root / ("ready-" + role))
        (self.root / "go").touch()
        for role in ("first", "second"):
            self.wait_for(self.root / ("done-" + role))
        results = [(self.root / ("result-" + role)).read_text() for role in ("first", "second")]
        self.assertCountEqual(results, ["True", "False"])
        (self.root / "release").touch()
        for process in processes:
            output, error = process.communicate(timeout=5)
            self.assertEqual(process.returncode, 0, output + error)

    def test_killed_holder_releases_without_deleting_the_lock_file(self):
        process = self.process('''
import sys, time
from nyaatriggers.instance_lock import InstanceLock
lock = InstanceLock(*sys.argv[1:])
assert lock.acquire()
print('ready', flush=True)
time.sleep(20)
''', self.data, self.cache)
        self.assertEqual(process.stdout.readline().strip(), "ready")
        lock = self.lock()
        self.assertFalse(lock.acquire(timeout=0))
        path = lock.path
        process.kill()
        process.communicate(timeout=5)
        self.assertTrue(path.exists())
        self.assertTrue(lock.acquire(timeout=0))
        lock.close()
        self.assertTrue(path.exists())

    @unittest.skipUnless(os.name == "posix", "POSIX replaces the process with exec")
    def test_exec_reacquires_in_the_same_process_and_failed_exec_keeps_ownership(self):
        child = """
import os, sys
from nyaatriggers.instance_lock import InstanceLock
lock = InstanceLock(*sys.argv[1:])
assert lock.acquire(timeout=0)
print(os.getpid())
"""
        process = self.process('''
import os, sys
from nyaatriggers.instance_lock import InstanceLock
lock = InstanceLock(*sys.argv[1:3])
assert lock.acquire()
os.execv(sys.executable, [sys.executable, '-c', sys.argv[3], *sys.argv[1:3]])
''', self.data, self.cache, child)
        output, error = process.communicate(timeout=5)
        self.assertEqual(process.returncode, 0, output + error)
        self.assertEqual(int(output.strip()), process.pid)
        lock = self.lock()
        self.assertTrue(lock.acquire())
        from nyaatriggers import updater
        with patch.object(updater, "relaunch_args", return_value=("/missing/nyaatriggers", ["missing"])):
            with self.assertRaises(OSError):
                updater.relaunch()
        self.assertFalse(self.lock().acquire(timeout=0))

    def test_detached_restart_waits_until_the_old_program_exits(self):
        child = '''
from pathlib import Path
import sys
from nyaatriggers.instance_lock import InstanceLock
lock = InstanceLock(*sys.argv[1:3])
assert lock.acquire(timeout=2)
Path(sys.argv[3]).write_text('restarted')
'''
        result = self.root / "restarted"
        process = self.process('''
import os, subprocess, sys, time
from nyaatriggers.instance_lock import InstanceLock
lock = InstanceLock(*sys.argv[1:3])
assert lock.acquire()
flags = 0x00000008 if os.name == 'nt' else 0
subprocess.Popen([sys.executable, '-c', sys.argv[4], *sys.argv[1:4]],
                 close_fds=True, creationflags=flags)
time.sleep(.15)
os._exit(0)
''', self.data, self.cache, result, child)
        output, error = process.communicate(timeout=5)
        self.assertEqual(process.returncode, 0, output + error)
        self.wait_for(result)
        self.assertEqual(result.read_text(), "restarted")

    @unittest.skipUnless(os.name == "nt", "Needs Windows error codes")
    def test_windows_reports_lock_failures_other_than_contention(self):
        import ctypes
        from nyaatriggers import instance_lock

        with (self.data / "lock-probe").open("wb") as stream:
            for code in (33, 5, 6):
                with self.subTest(code=code):
                    def fail(*_args):
                        ctypes.set_last_error(code)
                        return 0

                    with patch.object(instance_lock, "_lock_file", side_effect=fail):
                        if code == 33:
                            self.assertFalse(instance_lock._try_lock(stream.fileno()))
                        else:
                            with self.assertRaises(OSError) as caught:
                                instance_lock._try_lock(stream.fileno())
                            self.assertEqual(caught.exception.winerror, code)

    def test_startup_rejects_duplicates_before_setup_window_or_boot_marker(self):
        owner = self.lock()
        self.assertTrue(owner.acquire())
        script = '''
from pathlib import Path
import os, sys
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
import main
data, cache = map(Path, sys.argv[1:])
main.data_root = lambda: data
main.QStandardPaths.writableLocation = lambda _kind: str(cache)
main._set_setup_locale = lambda: None
main.QMessageBox.information = lambda _parent, title, _message: print(title)
main.QMessageBox.warning = lambda *_args: (_ for _ in ()).throw(AssertionError('unexpected warning'))
main._run_program = lambda _app: (_ for _ in ()).throw(AssertionError('program started twice'))
main.main()
'''
        process = self.process(script, self.data, self.cache)
        output, error = process.communicate(timeout=10)
        self.assertEqual(process.returncode, 0, output + error)
        self.assertIn("already running", output)
        self.assertEqual(list(self.data.iterdir()), [])

    def test_startup_holds_ownership_until_success_or_failure_returns(self):
        script = '''
from pathlib import Path
import os, sys
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
import main
from nyaatriggers.instance_lock import InstanceLock
data, cache = map(Path, sys.argv[1:3])
main.data_root = lambda: data
main.QStandardPaths.writableLocation = lambda _kind: str(cache)
def run(_app):
    other = InstanceLock(data, cache)
    assert not other.acquire(timeout=0)
    if sys.argv[3] == 'fail':
        raise ValueError('constructor failed')
main._run_program = run
try:
    main.main()
except ValueError:
    assert sys.argv[3] == 'fail'
other = InstanceLock(data, cache)
assert other.acquire(timeout=0)
other.close()
'''
        for outcome in ("success", "fail"):
            with self.subTest(outcome=outcome):
                process = self.process(script, self.data, self.cache, outcome)
                output, error = process.communicate(timeout=10)
                self.assertEqual(process.returncode, 0, output + error)

    def test_unavailable_cache_reports_failure_without_starting_or_writing_data(self):
        self.cache.write_text("not a directory")
        process = self.process('''
from pathlib import Path
import os, sys
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
import main
data, cache = map(Path, sys.argv[1:])
main.data_root = lambda: data
main.QStandardPaths.writableLocation = lambda _kind: str(cache)
main._set_setup_locale = lambda: None
main.QMessageBox.warning = lambda _parent, title, _message: print(title)
main._run_program = lambda _app: (_ for _ in ()).throw(AssertionError('unsafe startup'))
main.main()
''', self.data, self.cache)
        output, error = process.communicate(timeout=10)
        self.assertEqual(process.returncode, 0, output + error)
        self.assertIn("Could not start NyaaTriggers", output)
        self.assertEqual(list(self.data.iterdir()), [])
        self.assertEqual(self.cache.read_text(), "not a directory")


if __name__ == "__main__":
    unittest.main()
