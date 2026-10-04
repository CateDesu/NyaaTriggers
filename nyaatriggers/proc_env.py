"""Native process environments and shutdown."""
import os
import subprocess
import sys
import threading
import time


def child_env() -> dict:
    env = dict(os.environ)
    if not getattr(sys, "frozen", False):
        return env
    for var in ("LD_LIBRARY_PATH",):
        orig = env.get(var + "_ORIG")
        if orig is not None:
            env[var] = orig
        else:
            env.pop(var, None)
    return env


def signal_group(proc: subprocess.Popen, graceful: bool) -> None:
    try:
        if os.name == "posix":
            import signal
            # The process group may survive its wrapper.
            os.killpg(proc.pid, signal.SIGTERM if graceful else signal.SIGKILL)
        else:
            proc.terminate() if graceful else proc.kill()
    except (OSError, ProcessLookupError):
        try:
            proc.terminate() if graceful else proc.kill()
        except OSError:
            pass


def reap(proc: subprocess.Popen) -> None:
    """Wait for the sidecar, then kill surviving group members and release its files."""
    lock = proc.__dict__.setdefault("_nyaa_reap_lock", threading.Lock())
    with lock:
        if getattr(proc, "_nyaa_reaped", False):
            return
        try:
            deadline = time.monotonic() + 4
            try:
                proc.wait(timeout=4)
            except subprocess.TimeoutExpired:
                pass
            else:
                if os.name != "posix":
                    return
                while time.monotonic() < deadline:
                    try:
                        os.killpg(proc.pid, 0)
                    except ProcessLookupError:
                        return
                    except OSError:
                        break
                    time.sleep(0.05)
            signal_group(proc, graceful=False)
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
        finally:
            proc._nyaa_reaped = True
            runtime_dir = proc.__dict__.pop("_nyaa_runtime_dir", None)
            if runtime_dir is not None:
                runtime_dir.cleanup()
