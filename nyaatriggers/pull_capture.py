"""Capture raw WebSocket messages for engine replay when recording is enabled. Each pull
has JSONL and metadata files under pull_logs. Include initial identity and combat state
plus buffered messages before the first enemy ability. End on wipe, combat exit, zone
change, feed loss or recording stop. Bound each capture and retain recent pulls per
folder. All slots run on the GUI thread.
"""

from __future__ import annotations

import json
import os
import re
import time
from collections import deque
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import QObject, pyqtSlot

from nyaatriggers import drop_log

# Start recording on an ability from a non-player caster.
_ABILITY_TYPES = {"20", "21", "22"}
_WIPE_COMMAND = "4000000F"
# Include recent messages before the pull so replay has the initial state. Bound both
# count and bytes.
_PRE_PULL_SECONDS = 15.0
_PRE_PULL_MAX_MESSAGES = 500
_PRE_PULL_MAX_BYTES = 8 << 20
# Cap captures that never receive an ending event.
_MAX_PULL_SECONDS = 45 * 60
_MAX_PULL_BYTES = 64 << 20
_KEEP_CAPTURES = 20
_STATE_TYPES = frozenset({"changeprimaryplayer", "changezone", "partychanged", "incombat"})


def _owner_only(path, flags):
    return os.open(path, flags, 0o600)


def _sanitize(name: str) -> str:
    """Filesystem-safe folder name for a fight or zone."""
    cleaned = re.sub(r"[^A-Za-z0-9._ \-]+", "_", name).strip()
    if not cleaned.strip(". "):
        # Reject empty names and dot-only paths.
        return "Unknown"
    return cleaned


class PullCapture(QObject):

    def __init__(self, log_dir: Path, parent=None, *, state_snapshot=None) -> None:
        super().__init__(parent)
        self._log_dir = Path(log_dir)
        self._recording = False
        self._in_pull = False
        self._buffer: "deque[tuple[float, str]]" = deque()
        self._buffer_bytes = 0
        self._fh = None
        self._path: "Path | None" = None
        self._lines = 0
        self._bytes = 0
        self._started = 0.0
        self._started_wall = ""
        self._fight = ""
        self._zone = ""
        self._warned_write = False
        self._state_snapshot = state_snapshot or (lambda: ())
        # The caller supplies fight and zone names for capture paths.
        self.context = lambda: ("", "")

    def set_recording(self, recording: bool) -> None:
        recording = bool(recording)
        if self._recording == recording:
            return
        self._recording = recording
        if recording:
            # Allow another warning when recording is enabled again.
            self._warned_write = False
        else:
            self._buffer.clear()
            self._buffer_bytes = 0
            self._finalize("ended")

    @pyqtSlot(str)
    def on_raw_message(self, msg: str) -> None:
        """Record messages during a pull and buffer recent messages between pulls."""
        if not self._recording:
            return
        # Keep each raw message on one JSONL line.
        line = msg.replace("\r", " ").replace("\n", " ")
        if self._in_pull:
            self._write(line)
            return
        now = time.monotonic()
        self._buffer.append((now, line))
        self._buffer_bytes += len(line.encode("utf-8"))
        cutoff = now - _PRE_PULL_SECONDS
        while self._buffer and (self._buffer[0][0] < cutoff
                                or len(self._buffer) > _PRE_PULL_MAX_MESSAGES
                                or self._buffer_bytes > _PRE_PULL_MAX_BYTES):
            self._buffer_bytes -= len(self._buffer.popleft()[1].encode("utf-8"))

    @pyqtSlot(str)
    def on_log_line(self, raw: str) -> None:
        """Parsed ACT log line, used for pull segmentation only."""
        if not self._recording:
            return
        fields = raw.split("|")
        if fields[0] == "01":
            self._finalize("reset")
            self._buffer.clear()
            self._buffer_bytes = 0
            # Keep the zone boundary for replay when ChangeZone is unavailable.
            self.on_raw_message(json.dumps({"type": "LogLine", "rawLine": raw,
                                            "line": fields}))
            return
        if not self._in_pull:
            if (fields[0] in _ABILITY_TYPES and len(fields) > 2
                    and fields[2] and not fields[2].startswith("1")):
                self._begin()
            return
        if (fields[0] == "33" and len(fields) > 3
                and fields[3].upper() == _WIPE_COMMAND):
            self._finalize("wipe")

    @pyqtSlot(bool, bool)
    def on_in_combat(self, act: bool, game: bool) -> None:
        if self._in_pull and not game:
            self._finalize("clear")

    @pyqtSlot(int, str)
    def on_zone_changed(self, zone_id: int, name: str) -> None:
        if self._in_pull:
            self._finalize("reset")
        self._buffer.clear()
        self._buffer_bytes = 0

    @pyqtSlot(bool, str)
    def on_status_changed(self, connected: bool, _msg: str) -> None:
        """Mark feed loss separately so reconnect events cannot merge pulls or relabel the
        ending.
        """
        if not connected:
            self._finalize("feed-lost")
            self._buffer.clear()
            self._buffer_bytes = 0

    def close(self) -> None:
        self.set_recording(False)

    def _begin(self) -> None:
        self._fight, self._zone = self.context()
        folder = self._log_dir / _sanitize(self._fight or self._zone or "Unknown")
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            self._warn_write("create the capture folder", e)
            return
        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S-%f")
        path = folder / f"{stamp}.jsonl"
        try:
            fh = open(path, "w", encoding="utf-8", newline="\n", opener=_owner_only)
        except OSError as e:
            self._warn_write("open the capture file", e)
            return
        self._fh = fh
        self._path = path
        self._in_pull = True
        self._lines = 0
        self._bytes = 0
        self._started = time.monotonic()
        self._started_wall = datetime.now().isoformat(timespec="seconds")
        state = self._state_snapshot()
        for line in state:
            self._write(line.replace("\r", " ").replace("\n", " "))
        for _ts, line in self._buffer:
            if state:
                try:
                    data = json.loads(line)
                except (ValueError, RecursionError):
                    data = None
                if isinstance(data, dict) and str(data.get("type", "")).lower() in _STATE_TYPES:
                    continue
            self._write(line)
        self._buffer.clear()
        self._buffer_bytes = 0

    def _write(self, line: str) -> None:
        if self._fh is None:
            return
        try:
            self._fh.write(line + "\n")
            # Flush each message to reduce data lost on a process crash.
            self._fh.flush()
            self._lines += 1
            self._bytes += len(line.encode("utf-8")) + 1
        except OSError as e:
            self._warn_write("write the capture", e)
            self._finalize("ended")
            return
        if (self._bytes > _MAX_PULL_BYTES
                or time.monotonic() - self._started > _MAX_PULL_SECONDS):
            self._finalize("truncated")

    def _warn_write(self, what: str, err: OSError) -> None:
        """Report the first write failure in each recording period."""
        if self._warned_write:
            return
        self._warned_write = True
        drop_log.log_drop("pull-capture",
                          f"could not {what}, pulls are not being recorded: {err}",
                          throttle_s=0)

    def _prune(self, folder: Path, keep: Path | None = None) -> None:
        """Keep the current capture and the newest other names in this folder."""
        try:
            files = sorted(folder.glob("*.jsonl"))
            retired = [p for p in files if p != keep]
            for p in retired[:max(0, len(files) - _KEEP_CAPTURES)]:
                p.unlink(missing_ok=True)
                p.with_suffix(".meta.json").unlink(missing_ok=True)
        except OSError:
            pass

    def _finalize(self, outcome: str) -> None:
        if not self._in_pull:
            return
        self._in_pull = False
        fh, self._fh = self._fh, None
        if fh is not None:
            try:
                fh.close()
            except OSError:
                pass
        path = self._path
        if path is None:
            return
        meta = {
            "fight": self._fight,
            "zone": self._zone,
            "outcome": outcome,
            "started": self._started_wall,
            "duration_sec": round(time.monotonic() - self._started, 1),
            "lines": self._lines,
        }
        try:
            with open(path.with_suffix(".meta.json"), "w", encoding="utf-8",
                      opener=_owner_only) as fh:
                fh.write(json.dumps(meta, indent=2) + "\n")
        except OSError:
            pass
        self._prune(path.parent, keep=path)
