"""Keep programs sharing one data folder from overwriting each other's settings."""

import errno
import os
from pathlib import Path
import time
from uuid import NAMESPACE_URL, uuid5

if os.name == "nt":
    import ctypes
    from ctypes import wintypes
    import msvcrt

    _lock_file = ctypes.WinDLL("kernel32", use_last_error=True).LockFile
    _lock_file.argtypes = (wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                          wintypes.DWORD, wintypes.DWORD)
    _lock_file.restype = wintypes.BOOL


def _try_lock(fd):
    if os.name == "nt":
        if _lock_file(msvcrt.get_osfhandle(fd), 0, 0, 1, 0):
            return True
        error = ctypes.get_last_error()
        if error == 33:
            return False
        raise ctypes.WinError(error)
    import fcntl
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError as exc:
        if exc.errno not in (errno.EACCES, errno.EAGAIN):
            raise
        return False


class InstanceLock:
    def __init__(self, directory, cache):
        identity = os.path.normcase(str(Path(directory).resolve()))
        self.path = Path(cache) / "NyaaTriggers" / "instances" / (
            uuid5(NAMESPACE_URL, identity).hex + ".lock")
        self._fd = None

    def acquire(self, timeout=2.0):
        if self._fd is not None:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        deadline = time.monotonic() + timeout
        try:
            while True:
                if _try_lock(fd):
                    self._fd = fd
                    return True
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                time.sleep(min(.05, remaining))
        finally:
            if self._fd != fd:
                os.close(fd)

    def close(self):
        fd, self._fd = self._fd, None
        if fd is not None:
            os.close(fd)
