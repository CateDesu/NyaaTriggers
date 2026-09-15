"""Per-pull raw feed capture for engine replay.

Records the verbatim IINACT WS feed for one pull at a time so a real pull can
be replayed through the sidecar jar later, see tools/replay_pull.py. Opt-in
via the triggevent_record_pulls setting. One .jsonl per pull plus a
.meta.json under pull_logs/<fight-or-zone>/.

Segmentation rides the parsed log_line signal: a pull starts on the first
ability line from a non-player source and ends on a wipe, a combat end, a
zone change, a feed drop, or the recorder switching off. Raw messages are
buffered for a few seconds before the start line. Each capture starts with
the current player, zone, party and combat state from WSClient. Captures are
bounded in size and duration, and only the newest few are kept per folder. Total storage
still grows as more fights and zones are recorded.

All slots run on the GUI thread, the same one the WSClient signals fire on,
so no locking. Nothing here fires TTS or builds triggers, it is a passive
recorder like its pull_recorder.py ancestor, only raw where that one kept
parsed lines.
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

# A pull opens on the first ability line, 20/21/22, whose caster is not a
# player. Player ids start with 1, so a boss or npc caster means the fight
# really began.
_ABILITY_TYPES = {"20", "21", "22"}
_WIPE_COMMAND = "4000000F"
# Raw messages kept for the pre-pull flush. The feed idles at a few messages
# per second out of combat, so 15s of it stays small and gives the replay
# the same warmup state the live engine saw. The count and byte caps keep a
# flooding peer from pinning memory inside that window.
_PRE_PULL_SECONDS = 15.0
_PRE_PULL_MAX_MESSAGES = 500
_PRE_PULL_MAX_BYTES = 8 << 20
# A pull that never closes on its own, say an idle in the same overworld
# zone after a FATE cast opened one, gets truncated at these caps instead of
# growing for hours. Real pulls end well under both.
_MAX_PULL_SECONDS = 45 * 60
_MAX_PULL_BYTES = 64 << 20
# Newest captures kept per folder. Replay wants recent pulls, not every pull
# since the opt-in was flipped.
_KEEP_CAPTURES = 20
_STATE_TYPES = frozenset({"changeprimaryplayer", "changezone", "partychanged", "incombat"})


def _owner_only(path, flags):
    return os.open(path, flags, 0o600)


def _sanitize(name: str) -> str:
    """Filesystem-safe folder name for a fight or zone."""
    cleaned = re.sub(r"[^A-Za-z0-9._ \-]+", "_", name).strip()
    if not cleaned.strip(". "):
        # Empty, or all dots like "..", which would escape the capture tree.
        return "Unknown"
    return cleaned


class PullCapture(QObject):
    """Writes one raw-feed .jsonl per pull, gated by set_recording."""

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
        # Returns (fight tag, zone name) for the file names. Assigned by the
        # caller since the metadata lives on the main window.
        self.context = lambda: ("", "")

    def set_recording(self, recording: bool) -> None:
        recording = bool(recording)
        if self._recording == recording:
            return
        self._recording = recording
        if recording:
            # A fresh stint warns again if the folder still cannot be written.
            self._warned_write = False
        else:
            self._buffer.clear()
            self._buffer_bytes = 0
            self._finalize("ended")

    @pyqtSlot(str)
    def on_raw_message(self, msg: str) -> None:
        """Verbatim WS message tee. Recorded during a pull, ring buffered
        between pulls so the pre-pull seconds make the capture too."""
        if not self._recording:
            return
        # One json object per line, same hygiene the sidecar feed applies.
        line = msg.replace("\r", " ").replace("\n", " ")
        if self._in_pull:
            self._write(line)
            return
        now = time.monotonic()
        self._buffer.append((now, line))
        self._buffer_bytes += len(line)
        cutoff = now - _PRE_PULL_SECONDS
        while self._buffer and (self._buffer[0][0] < cutoff
                                or len(self._buffer) > _PRE_PULL_MAX_MESSAGES
                                or self._buffer_bytes > _PRE_PULL_MAX_BYTES):
            self._buffer_bytes -= len(self._buffer.popleft()[1])

    @pyqtSlot(str)
    def on_log_line(self, raw: str) -> None:
        """Parsed ACT log line, used for pull segmentation only."""
        if not self._recording:
            return
        fields = raw.split("|")
        if not self._in_pull:
            if (fields[0] in _ABILITY_TYPES and len(fields) > 2
                    and fields[2] and not fields[2].startswith("1")):
                self._begin()
            return
        if (fields[0] == "33" and len(fields) > 3
                and fields[3].upper() == _WIPE_COMMAND):
            self._finalize("wipe")
        elif fields[0] == "01":
            # A raw zone line is a real transition, never a replay, so it
            # closes the pull even when the WS ChangeZone event never came.
            # Re-entering the same instance still ends the previous pull.
            self._finalize("reset")

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
        """Feed status. A drop mid-pull closes the capture with its own
        outcome so the reconnect replay burst cannot mislabel it a reset or
        merge the next pull into the stale file."""
        if not connected:
            self._finalize("feed-lost")
            self._buffer.clear()
            self._buffer_bytes = 0

    def close(self) -> None:
        """Finalize any open pull, called from closeEvent."""
        self.set_recording(False)

    # ------------------------------------------------------------------
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
            fh = open(path, "w", encoding="utf-8", opener=_owner_only)
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
            # Flush every line so an app crash mid-pull loses nothing. Cheap
            # next to WS message rates, and a replay needs everything it got.
            self._fh.flush()
            self._lines += 1
            self._bytes += len(line) + 1
        except OSError as e:
            # A full disk must not take the app down with the recorder.
            self._warn_write("write the capture", e)
            self._finalize("ended")
            return
        if (self._bytes > _MAX_PULL_BYTES
                or time.monotonic() - self._started > _MAX_PULL_SECONDS):
            self._finalize("truncated")

    def _warn_write(self, what: str, err: OSError) -> None:
        """One visible drop log line per recording stint when a capture
        cannot be written. Silent failures here meant the opt-in did nothing
        forever with no trace."""
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
