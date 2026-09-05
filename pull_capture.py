"""Per-pull raw feed capture for engine replay.

Records the verbatim IINACT WS feed for one pull at a time so a real pull can
be replayed through the sidecar jar later, see tools/replay_pull.py. Opt-in
via the triggevent_record_pulls setting. One .jsonl per pull plus a
.meta.json under pull_logs/<fight-or-zone>/.

Segmentation rides the parsed log_line signal: a pull starts on the first
ability line from a non-player source and ends on a wipe, a combat end, a
zone change, or the recorder switching off. Raw messages are buffered for a
few seconds before the start line so the capture also holds the pre-pull
state the live engine had seen.

All slots run on the GUI thread, the same one the WSClient signals fire on,
so no locking. Nothing here fires TTS or builds triggers, it is a passive
recorder like its pull_recorder.py ancestor, only raw where that one kept
parsed lines.
"""

from __future__ import annotations

import json
import re
import time
from collections import deque
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import QObject, pyqtSlot

# A pull opens on the first ability line, 20/21/22, whose caster is not a
# player. Player ids start with 1, so a boss or npc caster means the fight
# really began.
_ABILITY_TYPES = {"20", "21", "22"}
_WIPE_COMMAND = "4000000F"
# Raw messages kept for the pre-pull flush. The feed idles at a few messages
# per second out of combat, so 15s of it stays small and gives the replay
# the same warmup state the live engine saw.
_PRE_PULL_SECONDS = 15.0


def _sanitize(name: str) -> str:
    """Filesystem-safe folder name for a fight or zone."""
    cleaned = re.sub(r"[^A-Za-z0-9._ \-]+", "_", name).strip()
    return cleaned or "Unknown"


class PullCapture(QObject):
    """Writes one raw-feed .jsonl per pull, gated by set_recording."""

    def __init__(self, log_dir: Path, parent=None) -> None:
        super().__init__(parent)
        self._log_dir = Path(log_dir)
        self._recording = False
        self._in_pull = False
        self._buffer: "deque[tuple[float, str]]" = deque()
        self._fh = None
        self._path: "Path | None" = None
        self._lines = 0
        self._started = 0.0
        self._started_wall = ""
        self._fight = ""
        self._zone = ""
        # Returns (fight tag, zone name) for the file names. Assigned by the
        # caller since the metadata lives on the main window.
        self.context = lambda: ("", "")

    def set_recording(self, recording: bool) -> None:
        recording = bool(recording)
        if self._recording == recording:
            return
        self._recording = recording
        if not recording:
            self._buffer.clear()
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
        cutoff = now - _PRE_PULL_SECONDS
        while self._buffer and self._buffer[0][0] < cutoff:
            self._buffer.popleft()

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

    @pyqtSlot(bool, bool)
    def on_in_combat(self, act: bool, game: bool) -> None:
        if self._in_pull and not game:
            self._finalize("clear")

    @pyqtSlot(int, str)
    def on_zone_changed(self, zone_id: int, name: str) -> None:
        if self._in_pull:
            self._finalize("reset")

    def close(self) -> None:
        """Finalize any open pull, called from closeEvent."""
        self.set_recording(False)

    # ------------------------------------------------------------------
    def _begin(self) -> None:
        self._fight, self._zone = self.context()
        folder = self._log_dir / _sanitize(self._fight or self._zone or "Unknown")
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError:
            return
        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S-%f")
        path = folder / f"{stamp}.jsonl"
        try:
            fh = open(path, "w", encoding="utf-8")
        except OSError:
            return
        self._fh = fh
        self._path = path
        self._in_pull = True
        self._lines = 0
        self._started = time.monotonic()
        self._started_wall = datetime.now().isoformat(timespec="seconds")
        for _ts, line in self._buffer:
            self._write(line)
        self._buffer.clear()

    def _write(self, line: str) -> None:
        if self._fh is None:
            return
        try:
            self._fh.write(line + "\n")
            # Flush every line so an app crash mid-pull loses nothing. Cheap
            # next to WS message rates, and a replay needs everything it got.
            self._fh.flush()
            self._lines += 1
        except OSError:
            # A full disk must not take the app down with the recorder.
            self._finalize("ended")

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
            path.with_suffix(".meta.json").write_text(
                json.dumps(meta, indent=2) + "\n", encoding="utf-8")
        except OSError:
            pass
