"""Store pulls as JSONL with bounded retention. Roll files when a fight reaches its pull
limit or adding a fight would exceed the distinct fight limit. Keep the active log in
addition to the retained completed logs. Manage only top-level JSONL files.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from pathlib import Path

from nyaatriggers.drop_log import log_drop

MAX_PULLS_PER_LOG = 25
MAX_FIGHTS_PER_LOG = 5
# Keep this many retired logs in addition to the active log.
MAX_LOGS = 5

# Track the actual active file because a backward clock change can make newer filenames
# sort before older ones.
_last_written: "Path | None" = None

# Serialize appends, rollover and retention across encounter writer threads.
_write_lock = threading.Lock()


def write_pull(log_dir, data: dict, when: "datetime | None" = None) -> Path:
    """Append a pull, roll if needed and prune old logs. Return the written path. Log
    retention failures without discarding the new pull.
    """
    global _last_written
    with _write_lock:
        when = when or datetime.now()
        log_dir = Path(log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        title = str(data.get("title") or "Unknown")
        path = _last_written
        if path is None or path.parent != log_dir or not path.exists():
            path = _current_log(log_dir)
        if path is not None and _is_full(path, title):
            path = None
        if path is None:
            path = _new_log(log_dir, when)
        line = json.dumps(data, ensure_ascii=False)
        # Create logs with owner access only, independently of the process umask.
        def _owner_only(path, flags):
            return os.open(path, flags, 0o600)
        # Finish any partial final line before appending so crash damage cannot corrupt
        # the next record.
        needs_newline = False
        try:
            with open(path, "rb") as fh:
                if fh.seek(0, os.SEEK_END) > 0:
                    fh.seek(-1, os.SEEK_END)
                    needs_newline = fh.read(1) != b"\n"
        except OSError:
            pass
        with open(path, "a", encoding="utf-8", opener=_owner_only) as fh:
            # A restored file can replace the active log between pulls.
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
            if needs_newline:
                fh.write("\n")
            fh.write(line + "\n")
        _last_written = path
        try:
            enforce_retention(log_dir, keep=path)
        except Exception as exc:  # noqa: BLE001
            log_drop("dps-store", f"retention failed: {exc!r}")
        return path


def enforce_retention(log_dir, max_logs: "int | None" = None,
                      keep: "Path | None" = None) -> None:
    """Prune completed logs while preserving the active file and keep. Track activity by
    the last write rather than filename order because the system clock may move
    backward.
    """
    if max_logs is None:
        max_logs = MAX_LOGS
    files = sorted(Path(log_dir).glob("*.jsonl"))
    # Fall back to the newest filename when this process has no active file in the
    # directory.
    active = _last_written if _last_written in files \
        else (files[-1] if files else None)
    retired = [p for p in files if p != active and p != keep]
    for path in retired[:max(0, len(retired) - max_logs)]:
        try:
            path.unlink()
        except OSError as exc:
            log_drop("dps-store", f"could not delete {path}: {exc}")


def _current_log(log_dir: Path) -> "Path | None":
    """Return the last filename in sorted order."""
    files = sorted(log_dir.glob("*.jsonl"))
    return files[-1] if files else None


def _new_log(log_dir: Path, when: datetime) -> Path:
    base = f"{when:%Y-%m-%d_%H-%M-%S}"
    for n in range(1000):
        # Zero padding preserves suffix order for rolls within the same second.
        path = log_dir / (f"{base}.jsonl" if n == 0 else f"{base}_{n:03d}.jsonl")
        if not path.exists():
            return path
    raise OSError(f"could not allocate a log name in {log_dir}")


def _title_of(raw: str) -> str:
    """Use Unknown for malformed records so they still count toward rollover limits."""
    try:
        title = json.loads(raw).get("title")
    except (ValueError, AttributeError, RecursionError):
        title = None
    return str(title) if title else "Unknown"


def _is_full(path: Path, title: str) -> bool:
    """Check whether appending this fight would exceed a pull or distinct fight limit.
    """
    counts: "dict[str, int]" = {}
    try:
        # Replace invalid bytes so one damaged line cannot prevent future recording.
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        # Keep the active log on transient read errors instead of creating a file for
        # every pull.
        log_drop("dps-store", f"active log unreadable, roll caps skipped: {exc}")
        return False
    for raw in lines:
        if not raw.strip():
            continue
        t = _title_of(raw)
        counts[t] = counts.get(t, 0) + 1
        if t == title and counts[t] >= MAX_PULLS_PER_LOG:
            return True
    return title not in counts and len(counts) >= MAX_FIGHTS_PER_LOG
