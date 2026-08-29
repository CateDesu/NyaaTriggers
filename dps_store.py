"""On-disk DPS pull log. Chunked JSONL files, with retention.

Layout follows the ACT model raiders already know. One continuous log
that readers segment into fights later. Each line is one pull and
carries its fight title, so "which fight" stays a read-side filter. The
active log rolls over to a fresh file when either cap trips.

- one fight reaches MAX_PULLS_PER_LOG pulls in it, a long prog block
- it already holds MAX_FIGHTS_PER_LOG distinct fights and a pull of a new
  fight arrives

Once MAX_LOGS full logs sit in the folder, the oldest full log is culled.
The active log still being written never counts against the cap. Culling
is the parser's job in this ecosystem. ACT prunes its own logs and the
uploaders downstream only ever read, so it lives here. Files are named by
creation time, YYYY-MM-DD_HH-MM-SS.jsonl plus a _N suffix on a same-second
roll, so name order is age order. Non-jsonl files are never touched.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from pathlib import Path

from drop_log import log_drop

# Roll the active log once one fight reaches this many pulls in it.
MAX_PULLS_PER_LOG = 25
# Roll the active log once it holds this many distinct fights.
MAX_FIGHTS_PER_LOG = 5
# Keep at most this many log files, cull the oldest.
MAX_LOGS = 5

# The file write_pull last appended to. A backward clock step makes
# _current_log return a stale pre-step file forever, so the newest name is
# not always the active log. Remembering the real one keeps appends
# landing in it.
_last_written: "Path | None" = None

# Every encounter end writes on its own daemon thread, so two pulls ending
# a beat apart would race _last_written, the roll decision and the
# retention unlinks. One lock around the whole write plus cull section.
_write_lock = threading.Lock()

# os.open's mode only applies at creation, so a pre-existing 0644 log, say
# one restored from a backup, gets one best-effort chmod after the first
# write, when the file is known to exist. Same idiom as drop_log.
_perms_tightened = False


def write_pull(log_dir, data: dict, when: "datetime | None" = None) -> Path:
    """Append one pull, a snapshot dict, to the active log, rolling to a
    fresh file when the caps say so, then cull old logs. Returns the file
    written. A retention failure is logged and swallowed. It must never eat
    the pull that was just written."""
    global _last_written, _perms_tightened
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
        # New files are created owner-only, 0600 survives a 022 umask. A plain
        # open in append mode would inherit the umask and leave party
        # names/performance world-readable on shared hosts.
        def _owner_only(path, flags):
            return os.open(path, flags, 0o600)
        # A crash can truncate the previous append before its newline. Close
        # the partial line first or this pull concatenates onto it and both
        # become one unparseable record.
        needs_newline = False
        try:
            with open(path, "rb") as fh:
                if fh.seek(0, os.SEEK_END) > 0:
                    fh.seek(-1, os.SEEK_END)
                    needs_newline = fh.read(1) != b"\n"
        except OSError:
            pass
        with open(path, "a", encoding="utf-8", opener=_owner_only) as fh:
            if needs_newline:
                fh.write("\n")
            fh.write(line + "\n")
        if not _perms_tightened:
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
            _perms_tightened = True
        _last_written = path
        try:
            enforce_retention(log_dir, keep=path)
        except Exception as exc:  # noqa: BLE001 - retention must never eat a pull
            log_drop("dps-store", f"retention failed: {exc!r}")
        return path


def enforce_retention(log_dir, max_logs: "int | None" = None,
                      keep: "Path | None" = None) -> None:
    """Cull the oldest full logs once more than max_logs of them sit in the
    folder. The active log never counts against the cap. The active log is
    the one _last_written points at, not the lexicographic newest. Names
    are wall clock, so a backward step can retire a full log under the
    newest name while appends continue in an older one. `keep` is the file
    just written, excluded alongside. Only *.jsonl in the top level of
    log_dir is managed. Anything else is left alone."""
    if max_logs is None:
        max_logs = MAX_LOGS
    files = sorted(Path(log_dir).glob("*.jsonl"))
    # Nothing written yet this process, or _last_written points outside this
    # folder. Appends would land on the newest name, so that one is active.
    active = _last_written if _last_written in files \
        else (files[-1] if files else None)
    retired = [p for p in files if p != active and p != keep]
    for path in retired[:max(0, len(retired) - max_logs)]:
        try:
            path.unlink()
        except OSError as exc:
            log_drop("dps-store", f"could not delete {path}: {exc}")


def _current_log(log_dir: Path) -> "Path | None":
    """The active log, the newest by name. Names sort by creation time."""
    files = sorted(log_dir.glob("*.jsonl"))
    return files[-1] if files else None


def _new_log(log_dir: Path, when: datetime) -> Path:
    base = f"{when:%Y-%m-%d_%H-%M-%S}"
    for n in range(1000):
        # "_N" sorts after ".jsonl", and the zero padding keeps the suffix
        # ordered past 9, so same-second rolls stay in creation order.
        path = log_dir / (f"{base}.jsonl" if n == 0 else f"{base}_{n:03d}.jsonl")
        if not path.exists():
            return path
    raise OSError(f"could not allocate a log name in {log_dir}")


def _title_of(raw: str) -> str:
    """The fight title of one pull line. Corrupt lines count as Unknown so
    they still participate in the caps instead of leaking forever."""
    try:
        title = json.loads(raw).get("title")
    except (ValueError, AttributeError):
        title = None
    return str(title) if title else "Unknown"


def _is_full(path: Path, title: str) -> bool:
    """Whether appending `title` to this log would cross a cap. The fight
    already has MAX_PULLS_PER_LOG pulls in it, or the log already holds
    MAX_FIGHTS_PER_LOG distinct fights and this pull is a new one."""
    counts: "dict[str, int]" = {}
    try:
        # errors="replace" because a single bad byte, disk corruption, a crash
        # mid-write, would otherwise raise UnicodeDecodeError out of _is_full on
        # every write, permanently breaking DPS recording. _title_of tolerates
        # the resulting malformed lines as "Unknown".
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        # Still say not full. Claiming full on a transient read failure
        # would roll a fresh log per pull and fragment the history.
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
