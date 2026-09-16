"""Restore a Triggevent pull from the local ACT log before joining its live feed."""

from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import threading
import time

from PyQt6.QtCore import QObject, QTimer, pyqtSignal

from nyaatriggers.triggevent_bridge import _log
from nyaatriggers.ws_client import _extract_raw

_MAX_HISTORY_BYTES = 64 << 20
_MAX_PENDING_BYTES = 16 << 20


def _event(line):
    return json.dumps({"type": "LogLine", "rawLine": line}, ensure_ascii=False)


def read_history(folder: Path, anchor: str, zone: int, player: int) -> tuple[list[str], str]:
    """Find an exact live log line and recover its preceding pull once."""
    candidates = sorted(folder.glob("Network_*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    selected = []
    remaining = 512 << 20
    for path in candidates[:2]:
        size = path.stat().st_size
        count = min(size, remaining)
        selected.append((path, size - count))
        remaining -= count
        if not remaining:
            break
    history = []
    history_bytes = 0
    last_zone = None
    combatants = {}
    positions = {}
    seed = []
    matched = False
    for path, start in reversed(selected):
        with path.open("rb") as stream:
            stream.seek(start)
            if start:
                stream.readline()
            for encoded in stream:
                line = encoded.decode("utf-8-sig", errors="replace").rstrip("\r\n")
                if line == anchor:
                    matched = True
                    break
                parts = line.split("|")
                try:
                    kind = int(parts[0])
                    if kind == 1:
                        last_zone = int(parts[2], 16)
                        combatants.clear()
                        positions.clear()
                        history = []
                        history_bytes = 0
                        seed = []
                    elif kind == 3:
                        combatants[int(parts[2], 16)] = line
                    elif kind == 4:
                        combatants.pop(int(parts[2], 16), None)
                        positions.pop(parts[2], None)
                    elif kind == 261:
                        actor = parts[3]
                        if parts[2] == "Remove":
                            positions.pop(actor, None)
                        elif parts[2] in ("Add", "Change"):
                            values = positions.setdefault(actor, {})
                            values.update(zip(parts[4:-1:2], parts[5:-1:2]))
                    elif kind == 33 and int(parts[3], 16) in (0x40000010, 0x4000000F):
                        history = []
                        history_bytes = 0
                        seed = list(combatants.values())
                        for actor, values in positions.items():
                            fields = [item for pair in values.items() for item in pair]
                            seed.append("|".join(["261", parts[1], "Add", actor, *fields, "0"]))
                    if last_zone is not None:
                        history.append(line)
                        history_bytes += sys.getsizeof(line)
                        if history_bytes > _MAX_HISTORY_BYTES:
                            history = []
                            last_zone = None
                except (ValueError, IndexError):
                    continue
        if matched:
            break
    if not matched:
        return [], "Live log line has not reached the local log"
    if not history or last_zone != zone:
        return [], "No complete pull boundary for the current zone in the local log"
    if player not in combatants:
        return [], "Current player is missing from the local log"
    timestamp = history[0].split("|", 2)[1]
    # Seed combatants at the boundary without advancing the replay clock.
    seed = ["|".join((line.split("|", 2)[0], timestamp, line.split("|", 2)[2])) for line in seed]
    return [_event(line) for line in seed + history], ""



class TriggeventRecovery(QObject):
    _loaded = pyqtSignal(int, object, str)

    def __init__(self, bridge, ws, log_folder, parent=None):
        super().__init__(parent)
        self.bridge = bridge
        self.ws = ws
        self.log_folder = log_folder
        self._generation = 0
        self._pending = []
        self._bytes = 0
        self._ready = False
        self._loading = False
        self._live = False
        self._lost_connection = False
        self._initial_state = ws.state_snapshot()
        self._loaded.connect(self._finish)
        bridge.ready.connect(self._on_ready)
        bridge.status.connect(self._on_status)
        ws.raw_message.connect(self.feed)
        ws.status_changed.connect(self._connection)

    def _on_status(self, active, _message, generation):
        if generation != self.bridge.generation():
            return
        if active and generation != self._generation:
            self._generation = generation
            self._pending = []
            self._bytes = 0
            self._ready = self._loading = self._live = False
            self._initial_state = self.ws.state_snapshot()

    def _connection(self, connected, _message):
        if not connected:
            self._lost_connection = True
            if self.bridge.supports_recovery():
                self.bridge._send_command({"nyaa_cmd": "pause_feed"})
        elif self._lost_connection:
            self._lost_connection = False
            if self.bridge.is_active():
                # A new engine avoids mixing a missed pull with old pending waits.
                self.bridge.stop()
                self.bridge.start()

    def _on_ready(self, generation):
        if generation != self._generation or generation != self.bridge.generation():
            return
        self._ready = True
        self._try_start()
        QTimer.singleShot(1500, lambda: self._try_start(True) if generation == self._generation else None)

    def feed(self, raw):
        if self._live:
            self.bridge.feed(raw)
            return
        size = sys.getsizeof(raw)
        if self._bytes + size > _MAX_PENDING_BYTES:
            _log("recovery: feed buffer exceeded its limit")
            self._finish(self._generation, [], "Recovery buffer full")
            self.bridge.feed(raw)
            return
        self._pending.append(raw)
        self._bytes += size
        self._try_start()

    def _try_start(self, allow_empty=False):
        if not self._ready or self._loading or self._live:
            return
        if not self.bridge.supports_recovery():
            self._finish(self._generation, [], "This engine build does not support pull recovery")
            return
        state = {}
        for raw in self.ws.state_snapshot():
            data = json.loads(raw)
            state[str(data.get("type", "")).lower()] = data
        anchor = next((line for raw in self._pending
                       if (line := self._log_line(raw))), None)
        if not anchor and not allow_empty:
            return
        if not allow_empty and not {"changezone", "changeprimaryplayer"} <= state.keys():
            return
        self._loading = True
        generation = self._generation
        zone = state.get("changezone", {}).get("zoneID", 0)
        player = state.get("changeprimaryplayer", {}).get("charID", 0)
        folder = self.log_folder()
        if not folder or not anchor:
            self._finish(generation, [], "No local log available for recovery")
            return

        def load():
            history, reason = [], ""
            try:
                # IINACT flushes the disk log after broadcasting the same line.
                for attempt in range(7):
                    history, reason = read_history(Path(folder), anchor, int(zone), int(player))
                    if reason != "Live log line has not reached the local log":
                        break
                    if attempt < 6:
                        time.sleep(.5)
            except (OSError, TypeError, ValueError, IndexError) as exc:
                reason = str(exc)
            self._loaded.emit(generation, history, reason)

        threading.Thread(target=load, daemon=True, name="triggevent-recovery").start()

    @staticmethod
    def _log_line(raw):
        try:
            data = json.loads(raw)
            return _extract_raw(data) if isinstance(data, dict) else None
        except (ValueError, RecursionError):
            return None

    def _finish(self, generation, history, reason):
        if generation != self._generation or generation != self.bridge.generation() or self._live:
            return
        # State seeds go first. Later changes retain their original feed order.
        initial = self.ws.state_snapshot() if history else self._initial_state or self.ws.state_snapshot()
        frames = list(initial) + list(history) + self._pending
        first_log = next((line for raw in frames if (line := self._log_line(raw))), None)
        try:
            start = datetime.fromisoformat(first_log.split("|", 2)[1]) if first_log else datetime.now(timezone.utc)
        except (ValueError, IndexError):
            start = datetime.now(timezone.utc)
        if self.bridge.supports_recovery():
            if not self.bridge.recover(frames, start.astimezone(timezone.utc).isoformat()):
                _log("recovery: history did not fit, keeping the buffered live feed")
                self._loading = False
                QTimer.singleShot(100, lambda: self._finish(generation, [], "History exceeded the queue limit"))
                return
        else:
            for raw in list(initial) + self._pending:
                self.bridge.feed(raw)
        self._pending = []
        self._bytes = 0
        self._live = True
        restored = len(history) if self.bridge.supports_recovery() else 0
        _log(f"recovery: restored {restored} historical events" + (f", {reason}" if reason else ""))
        self.ws.request_combatants_once()
