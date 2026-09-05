"""Drop logging, one line per lost callout suspect, appended to nyaatriggers.log.

The crash hook in main.py writes CRASH entries to the same file. This covers the
silent non-crashing losses, cooldown eats, timeline sync skips, TTS drops, engine
feed overflows, so a fight where callouts went missing still leaves evidence.
Every site is throttled to one line per second so a hot path like an AoE pack
eating one cooldown per mob can't flood the log.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

# Same location rule as main.py's crash log. Beside the exe when frozen,
# beside the sources otherwise.
_LOG_FILE = (Path(sys.executable).parent if getattr(sys, "frozen", False)
             else Path(__file__).parent) / "nyaatriggers.log"

_lock = threading.Lock()
_last: dict[str, float] = {}

# Cap the log at 1 MiB, the same ceiling the sidecar logs use. Without this the
# file grows forever across sessions. The plugin-tx site alone logs on every
# alert/timeline push. Past the cap the file rotates one generation to .1 so
# recent history survives instead of dropping everything. Checked under _lock
# so the size-check, rotate, append sequence is atomic.
_MAX_BYTES = 1 << 20


def rotate_one_generation(path: Path) -> None:
    """Rename path to path.1, overwriting any prior .1. Keeps one old
    generation of history instead of deleting the whole log at the cap.
    The rename carries the file's permissions over to the .1."""
    try:
        path.replace(path.with_name(path.name + ".1"))
    except FileNotFoundError:
        # A racing process rotated first. The append below still lands on
        # the fresh file instead of being dropped.
        pass


def _owner_only(path, flags):
    # New logs are created owner-only, 0600 survives a 022 umask. A plain
    # open in append mode would inherit the umask and leave fight/callout
    # details world-readable on shared hosts. Same idiom as dps_store.
    return os.open(path, flags, 0o600)


# os.open's mode only applies at creation, so a pre-existing 0644 log gets one
# best-effort chmod after the first write, when the file is known to exist.
_perms_tightened = False


def log_drop(site: str, detail: str, throttle_s: float = 1.0) -> None:
    """Append `DROP <ts> [site] detail`, at most once per throttle_s per site.
    Pass 0 to log every event, used where each one matters, like plugin sends."""
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
    """Append a preformatted crash block under the same lock log_drop uses.
    main._log_crash writes to the same file. Without the shared lock a crash
    write can land between log_drop's size check and its unlink, and go to an
    orphaned inode instead of the fresh file."""
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
