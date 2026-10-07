from datetime import datetime, timezone
import json
import sys

from PyQt6.QtCore import QObject, QTimer

from nyaatriggers.diagnostics import record
from nyaatriggers.triggevent_bridge import _log
from nyaatriggers.ws_client import _extract_raw

_MAX_PENDING_BYTES = 16 << 20
_PROGRESS_TIMEOUT_MS = 60_000
_RESTART_DELAY_MS = 1000
_MAX_RESTART_DELAY_MS = 30_000
_FALLBACK_REASONS = {
    "This engine build does not support pull recovery": "recovery_unsupported",
    "No local log available for recovery": "no_local_log",
    "This engine build cannot read local pull history": "history_unsupported",
    "Current player or zone is invalid": "invalid_world_state",
    "": "state_only",
}


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
        self._ending = False
        self._checkpoint = 0
        self._lost_connection = False
        self._connected = False
        self._restart_on_connect = False
        self._retry_delay_ms = _RESTART_DELAY_MS
        self._retry_generation = -1
        self._preserve_pending = False
        self._last_diagnostic = None
        self._initial_state = ws.state_snapshot()
        self._retry_timer = QTimer(self)
        self._retry_timer.setSingleShot(True)
        self._retry_timer.timeout.connect(self._retry_engine)
        self._progress_timer = QTimer(self)
        self._progress_timer.setSingleShot(True)
        self._progress_timer.setInterval(_PROGRESS_TIMEOUT_MS)
        self._progress_timer.timeout.connect(self._recovery_timed_out)
        bridge.ready.connect(self._on_ready)
        bridge.status.connect(self._on_status)
        bridge.recovery_progress.connect(self._on_progress)
        bridge.feed_overflow.connect(self._restart)
        ws.engine_message.connect(self.feed)
        ws.status_changed.connect(self._connection)

    def _diagnostic(self, state, reason, generation=None):
        generation = self._generation if generation is None else generation
        key = generation, state, reason
        if key == self._last_diagnostic:
            return
        self._last_diagnostic = key
        record("recovery_state", gen=generation, state=state, reason=reason,
               pending_frames=len(self._pending), pending_bytes=self._bytes,
               checkpoint=self._checkpoint)

    def _on_status(self, active, _message, generation):
        if generation != self.bridge.generation():
            return
        if active and not self.bridge.is_active():
            return
        if not active:
            if self._live and _message != "Off":
                self._initial_state = self.ws.state_snapshot()
            self._diagnostic("stopped", "disconnect" if self._lost_connection else
                             "requested" if _message == "Off" else "engine_exit", generation)
            self._progress_timer.stop()
            self._ready = self._loading = self._live = self._ending = False
            if _message != "Off":
                self._restart_on_connect = True
                self._schedule_retry()
            elif not self._lost_connection:
                self._restart_on_connect = False
                self._retry_timer.stop()
        if active and generation != self._generation:
            self._retry_timer.stop()
            self._progress_timer.stop()
            self._restart_on_connect = False
            self._generation = generation
            if not self._preserve_pending:
                self._pending = []
                self._bytes = 0
                self._initial_state = self.ws.state_snapshot()
            self._ready = self._loading = self._live = False
            self._ending = False
            self._checkpoint = 0
            self._diagnostic("starting", "engine_start")
            self._progress_timer.start()

    def _schedule_retry(self):
        if (self._connected and not self._lost_connection and self._restart_on_connect
                and not self.bridge.is_active() and not self._retry_timer.isActive()):
            self._retry_generation = self.bridge.generation()
            self._retry_timer.start(self._retry_delay_ms)
            self._retry_delay_ms = min(self._retry_delay_ms * 2, _MAX_RESTART_DELAY_MS)

    def _retry_engine(self):
        if (not self._connected or self._lost_connection or not self._restart_on_connect
                or self._retry_generation != self.bridge.generation() or self.bridge.is_active()):
            return
        self._diagnostic("restart", "engine_exit")
        self._preserve_pending = True
        try:
            self.bridge.start()
        finally:
            self._preserve_pending = False

    def _restart(self, generation, reason="feed_overflow"):
        if generation != self._generation or generation != self.bridge.generation():
            return
        self._progress_timer.stop()
        if self.bridge.is_active():
            self._diagnostic("restart", reason)
            self._preserve_pending = reason == "progress_timeout"
            try:
                self.bridge.stop()
                self.bridge.start()
            finally:
                self._preserve_pending = False

    def _recovery_timed_out(self):
        if (not self._ready or self._loading) and not self._live and not self._lost_connection:
            _log("recovery: engine readiness timed out" if not self._ready else
                 "recovery: engine acknowledgement timed out")
            self._restart(self._generation, "progress_timeout")

    def _connection(self, connected, _message):
        was_connected = self._connected
        self._connected = bool(connected)
        self._diagnostic("connected" if connected else "disconnected", "connection")
        if not connected:
            self._lost_connection = True
            self._retry_timer.stop()
            self._progress_timer.stop()
            if self.bridge.is_active():
                self._restart_on_connect = True
                self.bridge.stop()
        elif not was_connected and (self._lost_connection or self._loading or self._live
                                    or self._restart_on_connect):
            self._retry_timer.stop()
            self._lost_connection = False
            restart = self._restart_on_connect
            self._restart_on_connect = False
            if self.bridge.is_active():
                # A new engine avoids mixing a missed pull with old pending waits.
                self.bridge.stop()
                restart = True
            if restart:
                self._diagnostic("restart", "reconnect")
                self.bridge.start()

    def _on_ready(self, generation):
        if (generation != self._generation or generation != self.bridge.generation()
                or not self.bridge.is_active()):
            return
        if not self._ready:
            self._progress_timer.stop()
        self._ready = True
        self._diagnostic("ready", "ready")
        self._try_start()
        QTimer.singleShot(1500, lambda: self._try_start(True) if generation == self._generation else None)

    def feed(self, raw):
        if self._live or self._ending:
            generation = self._generation
            initial = self.ws.state_snapshot() if self._live else None
            queued = self.bridge.feed(raw)
            if queued is not False and generation == self._generation:
                return
            if generation == self._generation:
                if initial is not None:
                    self._initial_state = initial
                self._live = self._ending = False
        size = sys.getsizeof(raw)
        if self._bytes + size > _MAX_PENDING_BYTES:
            _log("recovery: feed buffer exceeded its limit")
            self._diagnostic("buffer_overflow", "buffer_overflow")
            if self.bridge.is_active():
                self._diagnostic("restart", "buffer_overflow")
            self._pending = []
            self._bytes = 0
            if self.bridge.is_active():
                self.bridge.stop()
                self.bridge.start()
            if size > _MAX_PENDING_BYTES:
                return
        self._pending.append(raw)
        self._bytes += size
        self._try_start()

    def _try_start(self, allow_empty=False):
        if (not self._ready or self._loading or self._live or self._lost_connection
                or not self.bridge.is_active()):
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
        if (generation != self._generation or generation != self.bridge.generation()
                or self._live or self._lost_connection or not self.bridge.is_active()):
            return
        if not self._progress_timer.isActive():
            self._diagnostic("restoring" if history else "fallback",
                             "history_restore" if history else _FALLBACK_REASONS.get(reason, "unknown"))
            self._progress_timer.start()
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
            checkpoint = 1 if self.bridge.supports_catchup() else None
            if history:
                queued = self.bridge.recover(self._pending, timestamp, history=history, state=initial,
                                             checkpoint=checkpoint)
            else:
                queued = self.bridge.recover(frames, timestamp, checkpoint=checkpoint)
            if not queued:
                _log("recovery: waiting for space in the engine feed queue")
                self._diagnostic("queue_wait", "queue_full")
                self._loading = True
                QTimer.singleShot(100, lambda: self._finish(generation, history, reason))
                return
            self._checkpoint = checkpoint or 0
        else:
            for raw in list(initial) + self._pending:
                self.bridge.feed(raw)
                if generation != self.bridge.generation():
                    return
        self._pending = []
        self._bytes = 0
        self._loading = True
        self._live = not self.bridge.supports_catchup()
        if self._live:
            self._progress_timer.stop()
            self._retry_delay_ms = _RESTART_DELAY_MS
            self._diagnostic("live", "catchup_unsupported")
        _log("recovery: requested engine history restore" if history else f"recovery: current state only, {reason}")
        self.ws.request_combatants_once()

    def _on_progress(self, message, generation):
        if (generation != self._generation or generation != self.bridge.generation()
                or not self._loading or self._live or self._lost_connection
                or not self.bridge.is_active()
                or message.get("checkpoint") != self._checkpoint):
            return
        if message.get("t") == "recovered" and self._ending:
            self._progress_timer.stop()
            self._retry_delay_ms = _RESTART_DELAY_MS
            self._live = True
            self._ending = self._loading = False
            self._diagnostic("live", "acknowledged")
            _log(f"recovery: {message.get('status', 'unknown')}, "
                 f"skipped {message.get('skipped', 0)}, {message.get('reason', '')}")
        elif message.get("t") == "recovery_checkpoint" and not self._ending:
            self._progress_timer.start()
            self._flush_pending(generation, self._checkpoint)

    def _flush_pending(self, generation, acknowledged):
        if (generation != self._generation or generation != self.bridge.generation()
                or acknowledged != self._checkpoint or self._ending or self._live
                or self._lost_connection or not self.bridge.is_active()):
            return
        finish = not self._pending
        checkpoint = self._checkpoint + 1
        if not self.bridge.catch_up(self._pending, checkpoint, finish=finish):
            self._diagnostic("queue_wait", "queue_full")
            QTimer.singleShot(100, lambda: self._flush_pending(generation, acknowledged))
            return
        self._diagnostic("ending" if finish else "catchup", "checkpoint")
        self._pending = []
        self._bytes = 0
        self._checkpoint = checkpoint
        self._ending = finish
        self._progress_timer.start()
