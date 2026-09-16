"""Buffer the live feed while Triggevent restores its pull from local history."""

from datetime import datetime, timezone
import json
import sys

from PyQt6.QtCore import QObject, QTimer

from nyaatriggers.triggevent_bridge import _log
from nyaatriggers.ws_client import _extract_raw

_MAX_PENDING_BYTES = 16 << 20


class TriggeventRecovery(QObject):
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
            self._finish(self._generation, None, "Recovery buffer full")
            self.bridge.feed(raw)
            return
        self._pending.append(raw)
        self._bytes += size
        self._try_start()

    def _try_start(self, allow_empty=False):
        if not self._ready or self._loading or self._live:
            return
        if not self.bridge.supports_recovery():
            self._finish(self._generation, None, "This engine build does not support pull recovery")
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
            self._finish(generation, None, "No local log available for recovery")
            return

        if not self.bridge.supports_local_history():
            self._finish(generation, None, "This engine build cannot read local pull history")
            return
        try:
            zone, player = int(zone), int(player)
        except (TypeError, ValueError, OverflowError):
            self._finish(generation, None, "Current player or zone is invalid")
            return
        self._finish(generation, {"folder": str(folder), "anchor": anchor,
                                  "zone": zone, "player": player}, "")

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
        frames = list(initial) + self._pending
        first_log = next((line for raw in frames if (line := self._log_line(raw))), None)
        try:
            start = datetime.fromisoformat(first_log.split("|", 2)[1]) if first_log else datetime.now(timezone.utc)
        except (ValueError, IndexError):
            start = datetime.now(timezone.utc)
        if self.bridge.supports_recovery():
            timestamp = start.astimezone(timezone.utc).isoformat()
            if history:
                queued = self.bridge.recover(self._pending, timestamp, history=history, state=initial)
            else:
                queued = self.bridge.recover(frames, timestamp)
            if not queued:
                _log("recovery: waiting for space in the engine feed queue")
                self._loading = True
                QTimer.singleShot(100, lambda: self._finish(generation, history, reason))
                return
        else:
            for raw in list(initial) + self._pending:
                self.bridge.feed(raw)
        self._pending = []
        self._bytes = 0
        self._live = True
        _log("recovery: requested engine history restore" if history else f"recovery: current state only, {reason}")
        self.ws.request_combatants_once()
