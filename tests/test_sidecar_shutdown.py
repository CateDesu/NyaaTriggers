from contextlib import ExitStack, contextmanager, nullcontext
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("NYAA_REPLAY_TEST", "1")

from nyaatriggers import diagnostics, drop_log, proc_env, triggevent_bridge as tv, triggernometry_bridge as tn
from tests.engine_fixture import joined_bridge_workers


BRIDGES = ((tv, tv.TriggeventBridge), (tn, tn.TriggernometryBridge))
POPEN = subprocess.Popen
CHILD = """
import os, signal, sys, time
from pathlib import Path
mode, ready = sys.argv[1:]
if os.name == 'posix' and mode != 'graceful':
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
if mode == 'group':
    if os.fork():
        sys.exit(0)
if mode in ('eof', 'group'):
    os.close(1)
    os.close(2)
pending = Path(ready + '.tmp')
pending.write_text(str(os.getpid()))
pending.replace(ready)
while True:
    time.sleep(1)
"""


def until(predicate, timeout=5, pump=False):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("Timed out waiting for fixture")
        if pump:
            from PyQt6.QtTest import QTest
            QTest.qWait(10)
        else:
            time.sleep(0.01)


@contextmanager
def launched(module, bridge, root, mode="stubborn"):
    ready = root / (str(time.monotonic_ns()) + ".ready")
    executable = root / "fixture-engine"
    executable.write_text("disposable engine")
    processes = []

    def spawn(_cmd, **kwargs):
        proc = POPEN([sys.executable, "-c", CHILD, mode, str(ready)], **kwargs)
        processes.append(proc)
        return proc

    with ExitStack() as isolation:
        isolation.enter_context(patch.object(module, "_log"))
        log_name = "triggevent.log" if module is tv else "triggernometry.log"
        isolation.enter_context(patch.object(module, "_log_path", return_value=root / log_name))
        isolation.enter_context(patch.object(drop_log, "_LOG_FILE", root / "nyaatriggers.log"))
        isolation.enter_context(patch.object(diagnostics, "_LOG_FILE", root / "diagnostics.log"))
        if module is tn:
            config = root / "triggernometry-core"
            config.mkdir(exist_ok=True)
            isolation.enter_context(patch.object(tn, "_rundata_dir", return_value=config))

        def close_streams():
            for proc in processes:
                for stream in (proc.stdin, proc.stdout, proc.stderr):
                    stream.close()

        isolation.callback(close_streams)
        isolation.enter_context(joined_bridge_workers(module, bridge))
        try:
            with ExitStack() as stack:
                stack.enter_context(patch.object(module.subprocess, "Popen", side_effect=spawn))
                if module is tv:
                    stack.enter_context(patch.object(tv, "_find_java", return_value=sys.executable))
                    stack.enter_context(patch.object(tv, "_find_jar", return_value=executable))
                    stack.enter_context(patch.object(tv, "_bundled_jre_dir", return_value=None))
                else:
                    stack.enter_context(patch.object(tn, "_find_exe", return_value=executable))
                    stack.enter_context(patch.object(tn, "has_mono", return_value=True))
                    stack.enter_context(patch.object(tn, "_find_packs", return_value=[]))
                    stack.enter_context(patch.object(tn, "_make_bundled_mono_executable"))
                bridge.start()
            if not processes:
                raise AssertionError("Bridge did not launch the fixture")
            proc = processes[0]
            until(ready.exists)
            runtime = Path(proc._nyaa_runtime_dir.name)
            yield proc, runtime, int(ready.read_text())
        finally:
            for proc in processes:
                proc_env.signal_group(proc, graceful=False)
                proc.wait(timeout=3)
            bridge.stop(wait=True)
            for proc in processes:
                proc_env.reap(proc)


@unittest.skipUnless(os.name == "posix", "Requires inherited pipe descriptors")
class FixtureReadinessTests(unittest.TestCase):
    def test_readiness_waits_for_the_complete_pid(self):
        import select

        with tempfile.TemporaryDirectory() as temp:
            ready = Path(temp) / "child.ready"
            entered_read, entered_write = os.pipe()
            release_read, release_write = os.pipe()
            bootstrap = """
import os, sys
from pathlib import Path
entered, release = map(int, sys.argv[1:3])
script, mode, ready = sys.argv[3:]
sys.argv = ['fixture', mode, ready]
def paused_write(path, data, *args, **kwargs):
    with path.open('w') as output:
        os.write(entered, b'1')
        os.read(release, 1)
        return output.write(data)
Path.write_text = paused_write
exec(script)
"""
            proc = None
            try:
                proc = POPEN([sys.executable, "-c", bootstrap, str(entered_write),
                              str(release_read), CHILD, "graceful", str(ready)],
                             pass_fds=(entered_write, release_read),
                             stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                self.assertTrue(select.select([entered_read], [], [], 5)[0],
                                "Child did not reach the readiness write")
                self.assertEqual(os.read(entered_read, 1), b"1")
                self.assertFalse(ready.exists(), "Readiness exposed an incomplete PID")
                os.write(release_write, b"1")
                until(ready.exists)
                self.assertEqual(int(ready.read_text()), proc.pid)
            finally:
                if proc is not None:
                    proc.kill()
                    proc.communicate(timeout=3)
                for descriptor in (entered_read, entered_write, release_read, release_write):
                    os.close(descriptor)


class FixtureStorageTests(unittest.TestCase):
    def test_slow_workers_finish_before_log_paths_are_restored(self):
        for module, cls in BRIDGES:
            for fail in (False, True):
                with self.subTest(bridge=cls.__name__, fail=fail), tempfile.TemporaryDirectory() as temp:
                    root = Path(temp)
                    external, storage = root / "external.log", root / "fixture.log"
                    release, entered = threading.Event(), threading.Event()
                    join = threading.Thread.join
                    worker = None

                    def finish():
                        entered.set()
                        release.wait()
                        module._log("worker finished")

                    def join_worker(thread, timeout=None):
                        if thread is worker:
                            if timeout is not None:
                                # Expire a bounded join while the worker is blocked.
                                return join(thread, 0)
                            release.set()
                        return join(thread, timeout)

                    with patch.object(module, "_log_path", return_value=external):
                        try:
                            outcome = self.assertRaisesRegex(RuntimeError, "fixture failure") if fail else nullcontext()
                            with outcome, patch.object(threading.Thread, "join", join_worker), ExitStack() as stack:
                                stack.enter_context(patch.object(module, "_log_path", return_value=storage))
                                stack.enter_context(joined_bridge_workers(module, cls()))
                                worker = module.threading.Thread(target=finish, daemon=True)
                                worker.start()
                                self.assertTrue(entered.wait(2))
                                if fail:
                                    raise RuntimeError("fixture failure")
                        finally:
                            release.set()
                            if worker is not None:
                                join(worker, 2)
                    self.assertFalse(worker.is_alive())
                    self.assertFalse(external.exists())
                    self.assertIn("worker finished", storage.read_text())

    def test_storage_stays_temporary_through_cleanup_after_success_or_failure(self):
        for module, cls in BRIDGES:
            for fail in (False, True):
                with self.subTest(bridge=cls.__name__, fail=fail), tempfile.TemporaryDirectory() as temp:
                    root = Path(temp)
                    profile = root / "inaccessible-profile"
                    profile.write_text("not a directory")
                    storage = root / "fixture"
                    storage.mkdir()
                    bridge = cls()
                    log, stop = module._log, bridge.stop

                    def cleanup(wait=False):
                        log("fixture cleanup")
                        drop_log.log_drop("engine-feed", "fixture cleanup", throttle_s=0)
                        return stop(wait=wait)

                    with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(profile), "APPDATA": str(profile)}), \
                            patch.object(drop_log, "_LOG_FILE", root / "unexpected-drop.log"), \
                            patch.object(diagnostics, "_LOG_FILE", root / "unexpected-diagnostics.log"), \
                            patch.object(diagnostics, "_ENABLED", True), \
                            patch.object(bridge, "stop", side_effect=cleanup):
                        outcome = self.assertRaisesRegex(RuntimeError, "fixture failure") if fail else nullcontext()
                        with outcome:
                            with launched(module, bridge, storage, "graceful") as (proc, runtime, _):
                                log("fixture body")
                                diagnostics.record("engine_start", active=False)
                                if fail:
                                    raise RuntimeError("fixture failure")
                        self.assertIsNotNone(proc.poll())
                        self.assertFalse(runtime.exists())
                        name = "triggevent.log" if module is tv else "triggernometry.log"
                        contents = (storage / name).read_text()
                        self.assertIn("fixture body", contents)
                        self.assertIn("fixture cleanup", contents)
                        self.assertIn("fixture cleanup", (storage / "nyaatriggers.log").read_text())
                        self.assertIn('"event":"engine_start"', (storage / "diagnostics.log").read_text())
                        self.assertFalse((root / "unexpected-drop.log").exists())
                        self.assertFalse((root / "unexpected-diagnostics.log").exists())


class SidecarShutdownTests(unittest.TestCase):
    def test_async_stop_then_wait_finishes_escalation_and_cleanup(self):
        for module, cls in BRIDGES:
            with self.subTest(bridge=cls.__name__), tempfile.TemporaryDirectory() as temp:
                bridge = cls()
                with launched(module, bridge, Path(temp)) as (proc, runtime, _):
                    cleanup = proc._nyaa_runtime_dir.cleanup
                    with patch.object(proc._nyaa_runtime_dir, "cleanup", wraps=cleanup) as cleaned:
                        start = time.monotonic()
                        bridge.stop()
                        self.assertLess(time.monotonic() - start, 1)
                        bridge.stop(wait=True)
                        elapsed = time.monotonic() - start
                        self.assertIsNotNone(proc.poll(), "Waiting stop forgot the retiring child")
                        self.assertFalse(runtime.exists())
                        cleaned.assert_called_once_with()
                        self.assertLess(elapsed, 7)
                        if os.name == "posix":
                            self.assertEqual(proc.returncode, -signal.SIGKILL)
                            self.assertGreaterEqual(elapsed, 3.5)
                        start = time.monotonic()
                        bridge.stop(wait=True)
                        self.assertLess(time.monotonic() - start, 0.5)
                        cleaned.assert_called_once_with()

    def test_reader_eof_retains_cleanup_until_wait(self):
        for module, cls in BRIDGES:
            with self.subTest(bridge=cls.__name__), tempfile.TemporaryDirectory() as temp:
                bridge = cls()
                with launched(module, bridge, Path(temp), "eof") as (proc, runtime, _):
                    until(lambda: bridge._proc is None)
                    self.assertIsNone(proc.poll())
                    start = time.monotonic()
                    bridge.stop(wait=True)
                    self.assertIsNotNone(proc.poll(), "Reader retirement lost its child")
                    self.assertFalse(runtime.exists())
                    self.assertLess(time.monotonic() - start, 7)

    def test_simultaneous_waiters_cover_detached_process_before_reaper_starts(self):
        for module, cls in BRIDGES:
            with self.subTest(bridge=cls.__name__), tempfile.TemporaryDirectory() as temp:
                bridge = cls()
                with launched(module, bridge, Path(temp)) as (proc, runtime, _):
                    detached, release = threading.Event(), threading.Event()
                    finished = [threading.Event(), threading.Event()]
                    errors = []
                    original = bridge._signal_group

                    def gated_signal(child, graceful):
                        detached.set()
                        if not release.wait(3):
                            raise AssertionError("Signal gate was not released")
                        original(child, graceful)

                    def stop(index):
                        try:
                            bridge.stop(wait=True)
                        except BaseException as exc:
                            errors.append(exc)
                        finally:
                            finished[index].set()

                    with patch.object(bridge, "_signal_group", side_effect=gated_signal):
                        first = threading.Thread(target=stop, args=(0,))
                        second = threading.Thread(target=stop, args=(1,))
                        try:
                            first.start()
                            self.assertTrue(detached.wait(2))
                            second.start()
                            self.assertFalse(finished[1].wait(0.15), "Concurrent wait skipped ownership")
                        finally:
                            release.set()
                            first.join(7)
                            if second.ident is not None:
                                second.join(7)
                    self.assertFalse(errors, errors)
                    self.assertTrue(all(event.is_set() for event in finished))
                    self.assertIsNotNone(proc.poll())
                    self.assertFalse(runtime.exists())

    def test_multiple_retirements_and_restart_do_not_touch_replacement(self):
        for module, cls in BRIDGES:
            with self.subTest(bridge=cls.__name__), tempfile.TemporaryDirectory() as temp, ExitStack() as stack:
                bridge = cls()
                root = Path(temp)
                old = []
                release, started, done = threading.Event(), threading.Event(), threading.Event()
                original = bridge._reap
                errors = []

                def gated_reap(proc):
                    if any(proc is item[0] for item in old):
                        if not release.wait(10):
                            raise AssertionError("Reap gate was not released")
                    original(proc)

                def stop():
                    try:
                        started.set()
                        bridge.stop(wait=True)
                    except BaseException as exc:
                        errors.append(exc)
                    finally:
                        done.set()

                with patch.object(bridge, "_reap", side_effect=gated_reap):
                    waiter = None
                    try:
                        for _ in range(2):
                            old.append(stack.enter_context(launched(module, bridge, root)))
                            bridge.stop()
                        current = stack.enter_context(launched(module, bridge, root, "graceful"))
                        waiter = threading.Thread(target=stop)
                        waiter.start()
                        self.assertTrue(started.wait(2))
                        until(lambda: bridge._proc is None)
                        self.assertFalse(done.wait(0.15), "Wait skipped the two older retirements")
                        start = time.monotonic()
                        replacement = stack.enter_context(launched(module, bridge, root))
                        self.assertLess(time.monotonic() - start, 1)
                        generation = bridge.generation()
                    finally:
                        release.set()
                        if waiter is not None:
                            waiter.join(7)
                self.assertFalse(errors, errors)
                self.assertTrue(done.is_set())
                for proc, runtime, _ in [*old, current]:
                    self.assertIsNotNone(proc.poll(), "An earlier generation survived the wait")
                    self.assertFalse(runtime.exists())
                self.assertIs(bridge._proc, replacement[0])
                self.assertIsNone(replacement[0].poll())
                self.assertTrue(bridge._gen_live(generation))

    def test_windows_wait_observes_termination_completion(self):
        for module, cls in BRIDGES:
            with self.subTest(bridge=cls.__name__), tempfile.TemporaryDirectory() as temp, \
                    patch.object(diagnostics, "_LOG_FILE", Path(temp) / "diagnostics.log"):
                entered, release, done = threading.Event(), threading.Event(), threading.Event()
                proc = Mock(pid=12345)
                proc._nyaa_reaped = False
                runtime = proc._nyaa_runtime_dir = Mock()

                def wait(timeout):
                    entered.set()
                    if not release.wait(2):
                        raise subprocess.TimeoutExpired("fixture", timeout)
                    return 0

                proc.wait.side_effect = wait
                bridge = cls()
                bridge._proc, bridge._active = proc, True
                with patch.object(proc_env, "os", SimpleNamespace(name="nt")):
                    bridge.stop()
                    self.assertTrue(entered.wait(1))
                    waiter = threading.Thread(target=lambda: (bridge.stop(wait=True), done.set()))
                    try:
                        waiter.start()
                        self.assertFalse(done.wait(0.15), "Windows wait skipped pending termination")
                    finally:
                        release.set()
                        waiter.join(3)
                    self.assertTrue(done.is_set())
                    proc.terminate.assert_called_once_with()
                    proc.kill.assert_not_called()
                    runtime.cleanup.assert_called_once_with()

    def test_windows_escalation_uses_direct_process_without_posix_groups(self):
        proc = Mock(pid=12345)
        proc._nyaa_reaped = False
        proc.wait.side_effect = [subprocess.TimeoutExpired("fixture", 4), 0]
        runtime = proc._nyaa_runtime_dir = Mock()
        with patch.object(proc_env, "os", SimpleNamespace(name="nt")):
            proc_env.signal_group(proc, graceful=True)
            proc_env.reap(proc)
            proc_env.reap(proc)
        proc.terminate.assert_called_once_with()
        proc.kill.assert_called_once_with()
        self.assertEqual([call.kwargs for call in proc.wait.call_args_list],
                         [{"timeout": 4}, {"timeout": 2}])
        runtime.cleanup.assert_called_once_with()


class ShutdownBoundaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from tests.test_session_ui import SessionUiTests
        SessionUiTests.setUpClass()

    def check_boundary(self, operation):
        from PyQt6.QtCore import QTimer
        from PyQt6.QtNetwork import QHostAddress
        from PyQt6.QtWebSockets import QWebSocketServer
        from PyQt6.QtWidgets import QApplication
        from nyaatriggers import tts, updater
        from nyaatriggers.telesto_client import TelestoClient
        from tests.test_session_ui import SessionUiTests

        single_shot = QTimer.singleShot
        for module, cls in BRIDGES:
            for state in ("connected", "disconnected", "reader_retired"):
                with self.subTest(bridge=cls.__name__, state=state):
                    case = unittest.TestCase()
                    try:
                        with patch.object(TelestoClient, "start"), \
                                patch.object(tv.TriggeventBridge, "is_available", return_value=False), \
                                patch.object(tn.TriggernometryBridge, "is_available", return_value=False):
                            SessionUiTests.setUp(case)
                        stack, window = case.stack, case.window
                        for target in ((tv.TriggeventBridge, "is_available"),
                                       (tn.TriggernometryBridge, "is_available")):
                            stack.enter_context(patch.object(*target, return_value=False))
                        stack.enter_context(patch.object(tts, "_ensure_worker"))
                        stack.enter_context(patch.object(QApplication, "quit"))
                        stack.enter_context(patch.object(QTimer, "singleShot", single_shot))
                        window._find_iinact_log_dir = lambda: None
                        window._triggevent_mode = window._triggernometry_mode = False
                        bridge = (window._ensure_triggevent_bridge() if module is tv
                                  else window._ensure_triggernometry_bridge())
                        server = QWebSocketServer("shutdown fixture", QWebSocketServer.SslMode.NonSecureMode)
                        self.assertTrue(server.listen(QHostAddress(QHostAddress.SpecialAddress.LocalHost), 0))
                        stack.callback(server.close)
                        peers = []
                        disconnected = []
                        server.newConnection.connect(lambda: peers.append(server.nextPendingConnection()))
                        window._ws.status_changed.connect(
                            lambda connected, _message: disconnected.append(not connected))
                        window._ws.connect_to(f"ws://127.0.0.1:{server.serverPort()}/ws")
                        until(lambda: window._connected and bool(peers), pump=True)
                        stack.callback(lambda: [peer.close() for peer in peers])
                        if module is tv:
                            window._triggevent_mode = True
                        else:
                            window._triggernometry_mode = True
                        mode = "eof" if state == "reader_retired" else "stubborn"
                        with launched(module, bridge, case.temp, mode) as (proc, runtime, _):
                            calls, boundary = [], []
                            original = bridge.stop

                            def stop(wait=False):
                                calls.append(wait)
                                return original(wait=wait)

                            def observe(*args):
                                boundary.append((proc.poll(), runtime.exists()))

                            with patch.object(bridge, "stop", side_effect=stop), \
                                    patch.object(updater, "relaunch", side_effect=observe), \
                                    patch.object(QTimer, "singleShot", side_effect=observe):
                                if state == "disconnected":
                                    window._ws.disconnect_from()
                                    until(lambda: not window._connected, pump=True)
                                    self.assertIsNone(bridge._proc)
                                elif state == "reader_retired":
                                    until(lambda: bridge._proc is None, pump=True)
                                start = time.monotonic()
                                getattr(window, operation)()
                                self.assertLess(time.monotonic() - start, 8)
                                self.assertIn(True, disconnected, "Actual disconnect was not delivered")
                                if state != "reader_retired" or module is tn:
                                    self.assertIn(False, calls, "Disconnect did not request async stop")
                                self.assertIn(True, calls, "Shutdown did not wait for the bridge")
                                self.assertIsNotNone(proc.poll(), "Shutdown returned with a live child")
                                self.assertFalse(runtime.exists())
                                if operation != "close":
                                    self.assertTrue(boundary, "Relaunch or quit was not reached")
                                    self.assertTrue(all(code is not None and not exists
                                                        for code, exists in boundary), boundary)
                    finally:
                        case.doCleanups()

    def test_close_finishes_cleanup_after_real_disconnect(self):
        self.check_boundary("close")

    def test_restart_finishes_cleanup_before_relaunch(self):
        self.check_boundary("_restart_for_update")

    def test_handoff_finishes_cleanup_before_scheduling_quit(self):
        self.check_boundary("_quit_for_windows_handoff")


@unittest.skipUnless(sys.platform == "linux", "Linux orphan supervision")
class ParentShutdownTests(unittest.TestCase):
    def check_supervisor(self, scenario):
        for module, _ in BRIDGES:
            with self.subTest(bridge=module.__name__):
                result = subprocess.run([sys.executable, "-m", "tests.test_sidecar_shutdown",
                                         "--supervise", module.__name__, scenario],
                                        capture_output=True, text=True, timeout=25)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("owned children reaped and runtime removed", result.stdout)

    def test_wait_survives_parent_exec(self):
        self.check_supervisor("exec")

    def test_wait_survives_parent_exit(self):
        self.check_supervisor("exit")

    def test_reader_retirement_covers_exited_wrapper_and_live_group_child(self):
        self.check_supervisor("group")


def parent_exit(module, cls, operation, root):
    bridge = cls()
    launch = launched(module, bridge, root)
    proc, runtime, child = launch.__enter__()
    (root / "owned.json").write_text(json.dumps({"pid": child, "group": proc.pid,
                                                 "runtime": str(runtime)}))
    start = time.monotonic()
    bridge.stop()
    bridge.stop(wait=True)
    (root / "boundary.json").write_text(json.dumps({"code": proc.poll(),
                                                    "runtime_exists": runtime.exists(),
                                                    "elapsed": time.monotonic() - start}))
    if operation == "exec":
        os.execv(sys.executable, [sys.executable, "-c", "pass"])
    os._exit(0)


def supervised(module, cls, scenario):
    import ctypes
    import shutil
    assert ctypes.CDLL(None).prctl(36, 1, 0, 0, 0) == 0
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        child = group = parent = runtime_path = None
        try:
            if scenario == "group":
                bridge = cls()
                with launched(module, bridge, root, "group") as (proc, runtime, child):
                    group = proc.pid
                    until(lambda: bridge._proc is None)
                    assert proc.wait(timeout=2) == 0
                    assert os.waitpid(child, os.WNOHANG) == (0, 0), "Descendant was not alive"
                    start = time.monotonic()
                    bridge.stop(wait=True)
                    assert time.monotonic() - start < 7
                    reaped = []

                    def dead():
                        pid, status = os.waitpid(child, os.WNOHANG)
                        if pid:
                            reaped.append(status)
                        return bool(pid)

                    until(dead, timeout=1)
                    child = None
                    assert os.WIFSIGNALED(reaped[0]) and os.WTERMSIG(reaped[0]) == signal.SIGKILL
                    assert not runtime.exists()
            else:
                parent = POPEN([sys.executable, "-m", "tests.test_sidecar_shutdown", "--parent",
                                module.__name__, scenario, str(root)],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                try:
                    out, err = parent.communicate(timeout=12)
                finally:
                    ownership = root / "owned.json"
                    if ownership.exists():
                        owned = json.loads(ownership.read_text())
                        child, group = owned["pid"], owned["group"]
                        runtime_path = Path(owned["runtime"])
                assert parent.returncode == 0, out + err
                boundary = json.loads((root / "boundary.json").read_text())
                assert boundary["code"] is not None, "Child survived the parent boundary"
                assert not boundary["runtime_exists"], "Runtime survived the parent boundary"
                assert 3.5 <= boundary["elapsed"] < 7, boundary
                assert not Path(owned["runtime"]).exists()
                try:
                    os.kill(child, 0)
                except ProcessLookupError:
                    child = None
                else:
                    raise AssertionError("Child still exists after parent exit")
            print("owned children reaped and runtime removed")
        finally:
            if parent is not None:
                if parent.poll() is None:
                    parent.kill()
                parent.communicate(timeout=3)
            if group is not None:
                try:
                    os.killpg(group, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            if child is not None:
                try:
                    os.waitpid(child, 0)
                except ChildProcessError:
                    pass
            if runtime_path is not None:
                shutil.rmtree(runtime_path, ignore_errors=True)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] in ("--supervise", "--parent"):
        module, cls = next((module, cls) for module, cls in BRIDGES if module.__name__ == sys.argv[2])
        if sys.argv[1] == "--supervise":
            supervised(module, cls, sys.argv[3])
        else:
            parent_exit(module, cls, sys.argv[3], Path(sys.argv[4]))
    else:
        unittest.main()
