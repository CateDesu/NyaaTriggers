"""Record dropped callouts and crash reports in nyaatriggers.log. Throttle repeated reports
per site while retaining evidence for missing callouts.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from nyaatriggers.paths import data_root

_LOG_FILE = data_root() / "nyaatriggers.log"

_lock = threading.Lock()
_last: dict[str, float] = {}

# Rotate one retained generation at the size limit. Check and append under the same
# lock.
_MAX_BYTES = 1 << 20


def rotate_one_generation(path: Path) -> None:
    """Rotate to path.1, replacing the retained generation and preserving file permissions.
    """
    try:
        path.replace(path.with_name(path.name + ".1"))
    except OSError:
        # Keep appending if rotation fails or another process rotated first.
        pass


def _owner_only(path, flags):
    # Create logs with owner access only.
    return os.open(path, flags, 0o600)


def open_private_log(path: Path):
    """Open a private log and tighten any existing retained generation."""
    log = open(path, "a", encoding="utf-8", errors="replace", opener=_owner_only)
    for existing in (path, path.with_name(path.name + ".1")):
        try:
            os.chmod(existing, 0o600)
        except OSError:
            pass
    return log


# Tighten permissions after the first write because creation mode does not affect
# existing files.
_perms_tightened = False


def log_drop(site: str, detail: str, throttle_s: float = 1.0) -> None:
    """Append a DROP entry with throttling per site. A zero interval records every event.
    """
    global _perms_tightened
    now = time.monotonic()
    with _lock:
        if now - _last.get(site, -10.0) < throttle_s:
            return
        _last[site] = now
        try:
            if _LOG_FILE.exists() and _LOG_FILE.stat().st_size > _MAX_BYTES:
                rotate_one_generation(_LOG_FILE)
            with open(_LOG_FILE, "a", encoding="utf-8", errors="replace",
                      opener=_owner_only) as f:
                f.write(f"DROP {datetime.now():%Y-%m-%d %H:%M:%S} [{site}] {detail}\n")
            if not _perms_tightened:
                os.chmod(_LOG_FILE, 0o600)
                _perms_tightened = True
        except OSError:
            pass


def log_crash(text: str) -> None:
    """Append crash details under the drop log lock so rotation cannot split the write.
    """
    global _perms_tightened
    with _lock:
        try:
            if _LOG_FILE.exists() and _LOG_FILE.stat().st_size > _MAX_BYTES:
                rotate_one_generation(_LOG_FILE)
            with open(_LOG_FILE, "a", encoding="utf-8", errors="replace",
                      opener=_owner_only) as f:
                f.write(text)
            if not _perms_tightened:
                os.chmod(_LOG_FILE, 0o600)
                _perms_tightened = True
        except OSError:
            pass
