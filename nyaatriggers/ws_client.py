"""Read IINACT combat events for local triggers and the meter, and forward raw messages to
engine sidecars. The meter uses log lines rather than CombatData summaries.
"""

import json
import math
import os

from PyQt6.QtCore import QObject, QTimer, QUrl, pyqtSignal
from PyQt6.QtNetwork import QAbstractSocket
from PyQt6.QtWebSockets import QWebSocket

# Subscribe to combat logs and world state for local processing and sidecar replay.
_SUBSCRIBE = json.dumps({"call": "subscribe", "events": [
    "LogLine", "CombatData", "ChangePrimaryPlayer", "ChangeZone", "PartyChanged",
    "InCombat",
]})

# Bound message size before JSON parsing on the GUI thread.
_MAX_WS_MESSAGE = 4 << 20
_PING_INTERVAL_MS = 15000
_PONG_TIMEOUT_MS = 10000


class WSClient(QObject):
    log_line = pyqtSignal(str)          # raw pipe-delimited ACT log line
    combatants = pyqtSignal(dict)       # me/list combatant snapshot with positions and HP
    party_jobs = pyqtSignal(dict)       # actor_id_int -> job_int from PartyChanged, decimal job ids
    zone_changed = pyqtSignal(int, str)     # zoneId and zoneName from ChangeZone
    primary_player = pyqtSignal(int, str)   # charID and charName from ChangePrimaryPlayer
    raw_message = pyqtSignal(str)       # every raw WS text msg, verbatim, teed to the sidecar
    status_changed = pyqtSignal(bool, str)  # connected and message
    in_combat = pyqtSignal(bool, bool)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._url = ""
        self._auto_reconnect = False
        self._reopen_on_disconnect = False   # connect_to over a live socket means reopen once closed
        self._player_id = 0             # tracked from ChangePrimaryPlayer, for combatant "me"
        self._party_types: dict = {}    # combatant_id -> 1 party, 2 alliance, from PartyChanged
        # Avoid a second status update when Qt emits disconnected after an error.
        self._error_reported = False
        # Cache subscription state for sidecars that start after these events arrive.
        self._state_cache: dict = {}    # msgtype -> raw_msg

        self._ws = QWebSocket(parent=self)
        self._ws.setMaxAllowedIncomingMessageSize(_MAX_WS_MESSAGE)
        self._ws.connected.connect(self._on_connected)
        self._ws.disconnected.connect(self._on_disconnected)
        self._ws.textMessageReceived.connect(self._on_message)
        self._ws.errorOccurred.connect(self._on_error)
        self._ws.pong.connect(self._on_pong)

        self._pending_ping = None
        self._heartbeat_timer = QTimer(self)
        self._heartbeat_timer.setSingleShot(True)
        self._heartbeat_timer.timeout.connect(self._heartbeat_tick)

        self._reconnect_timer = QTimer(self)
        self._reconnect_timer.setSingleShot(True)
        self._reconnect_timer.timeout.connect(self._open)
        self._reconnect_delay = 5000    # ms, doubled per retry up to 60 s

        # Each engine keeps polling enabled while it needs live combatants.
        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(600)
        self._poll_timer.timeout.connect(self._request_combatants)
        self._poll_enabled = False
        self._engine_poll_enabled = False
        self._refresh_ids: set[int] = set()
        self._refresh_all = False
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.setInterval(20)
        self._refresh_timer.timeout.connect(self._flush_combatant_requests)

    def connect_to(self, url: str) -> None:
        # Validate WebSocket schemes before passing the address to Qt.
        if url:
            scheme = QUrl(url).scheme().lower()
            if scheme not in ("ws", "wss"):
                self.status_changed.emit(
                    False, "Connect URL must start with ws:// or wss://")
                return
        self._url = url
        self._auto_reconnect = True
        self._reconnect_timer.stop()
        # Reset backoff for a user-requested reconnect.
        self._reconnect_delay = 5000
        if self._ws.state() != QAbstractSocket.SocketState.UnconnectedState:
            # Wait for asynchronous close before reopening so its disconnect callback
            # cannot abort a new connection.
            self._reopen_on_disconnect = True
            self._stop_heartbeat()
            self._ws.close()
            return
        self._open()

    def disconnect_from(self) -> None:
        self._auto_reconnect = False
        self._reopen_on_disconnect = False
        self._reconnect_timer.stop()
        self._stop_heartbeat()
        self._ws.close()

    def _open(self) -> None:
        if not self._url:
            return
        # Ignore stale reconnect timers while a socket is connected or connecting.
        state = self._ws.state()
        if state == QAbstractSocket.SocketState.ConnectedState:
            return
        if state == QAbstractSocket.SocketState.ConnectingState:
            # Abort a handshake that outlived its attempt deadline before starting
            # another.
            self._ws.abort()
        self._ws.open(QUrl(self._url))
        # Arm a deadline for this handshake. Successful connection cancels it.
        self._schedule_reconnect()

    def _on_connected(self) -> None:
        self._reconnect_timer.stop()
        self._reconnect_delay = 5000
        self._error_reported = False
        self._pending_ping = None
        self._heartbeat_timer.start(_PING_INTERVAL_MS)
        self.status_changed.emit(True, "Connected")
        self._ws.sendTextMessage(_SUBSCRIBE)
        if self._poll_enabled or self._engine_poll_enabled:
            self._request_combatants()
            self._poll_timer.start()

    def _on_disconnected(self) -> None:
        self._poll_timer.stop()
        self._refresh_timer.stop()
        self._refresh_ids.clear()
        self._refresh_all = False
        self._stop_heartbeat()
        # Discard identity mappings on feed loss. Subscription replay repopulates them.
        self._player_id = 0
        self._party_types = {}
        self._state_cache.clear()
        if self._error_reported:
            self._error_reported = False
        else:
            self.status_changed.emit(False, "Disconnected")
        if self._reopen_on_disconnect:
            self._reopen_on_disconnect = False
            self._open()
        # Retain a retry timer if immediate reopening fails.
        self._schedule_reconnect()

    def _stop_heartbeat(self) -> None:
        self._heartbeat_timer.stop()
        self._pending_ping = None

    def _heartbeat_tick(self) -> None:
        if self._ws.state() != QAbstractSocket.SocketState.ConnectedState:
            self._stop_heartbeat()
            return
        if self._pending_ping is not None:
            self._stop_heartbeat()
            self._error_reported = True
            self.status_changed.emit(False, "Connection timed out")
            self._ws.abort()
            self._schedule_reconnect()
            return
        # Match a fresh payload so a delayed pong cannot revive a later probe.
        self._pending_ping = os.urandom(8)
        self._heartbeat_timer.start(_PONG_TIMEOUT_MS)
        self._ws.ping(self._pending_ping)

    def _on_pong(self, _elapsed: int, payload) -> None:
        pending = self._pending_ping
        if pending is None:
            return
        # IINACT adds two zero bytes before the echoed payload. Accept that
        # reply too while still requiring the token from the current probe.
        if bytes(payload) not in (pending, b"\x00\x00" + pending):
            return
        if self._ws.state() != QAbstractSocket.SocketState.ConnectedState:
            return
        self._pending_ping = None
        self._heartbeat_timer.start(_PING_INTERVAL_MS)

    def set_combatant_polling(self, enabled: bool) -> None:
        """Poll combatants while connected and requested by an engine."""
        self._poll_enabled = bool(enabled)
        self._update_combatant_polling()

    def set_engine_combatant_polling(self, enabled: bool) -> None:
        self._engine_poll_enabled = bool(enabled)
        self._update_combatant_polling()

    def _update_combatant_polling(self) -> None:
        enabled = self._poll_enabled or self._engine_poll_enabled
        if enabled and self._ws.isValid():
            self._request_combatants()
            self._poll_timer.start()
        elif not enabled:
            self._poll_timer.stop()

    def _request_combatants(self) -> None:
        if self._ws.isValid():
            self._ws.sendTextMessage(json.dumps({"call": "getCombatants", "rseq": "allCombatants"}))

    def request_engine_combatants(self, ids) -> None:
        if not ids:
            self._refresh_all = True
        else:
            self._refresh_ids.update(ids)
        if not self._refresh_timer.isActive():
            self._refresh_timer.start()

    def _flush_combatant_requests(self) -> None:
        if self._ws.isValid():
            if self._refresh_all:
                self._request_combatants()
            elif self._refresh_ids:
                self._ws.sendTextMessage(json.dumps({"call": "getCombatants",
                    "rseq": "specificCombatants", "ids": sorted(self._refresh_ids)}))
        self._refresh_ids.clear()
        self._refresh_all = False

    def request_combatants_once(self) -> None:
        """Request one combatant snapshot to fill jobs missed before subscription."""
        self._request_combatants()

    def replay_state(self) -> None:
        """Replay cached identity and combat state to newly started sidecars, then request
        current combatants.
        """
        for msg in self.state_snapshot():
            self.raw_message.emit(msg)
        self._request_combatants()

    def state_snapshot(self) -> tuple[str, ...]:
        """Current world state for a new engine or pull recording."""
        return tuple(self._state_cache[key] for key in
                     ("changeprimaryplayer", "changezone", "partychanged", "incombat")
                     if key in self._state_cache)

    def _on_error(self, _err) -> None:
        if self._ws.isValid():
            return   # Wait for disconnected before treating a transient socket error as feed loss.
        self._error_reported = True
        self.status_changed.emit(False, self._ws.errorString())
        self._schedule_reconnect()

    def _on_message(self, msg: str) -> None:
        self.raw_message.emit(msg)
        try:
            data = json.loads(msg)
        except (ValueError, RecursionError):
            # Catch excessive JSON nesting separately because RecursionError is not a
            # ValueError.
            raw = msg.strip()
            if raw:
                self._emit_log_line(raw)
            return

        if not isinstance(data, dict):
            raw = msg.strip()
            if raw:
                self._emit_log_line(raw)
            return

        mtype = str(data.get("type", "")).lower()

        if mtype in ("changezone", "changeprimaryplayer", "partychanged", "incombat"):
            self._state_cache[mtype] = msg

        if mtype == "incombat":
            # Preserve both combat flags because sync rules can match either.
            self.in_combat.emit(bool(data.get("inACTCombat")), bool(data.get("inGameCombat")))
            return

        if mtype == "changeprimaryplayer":
            try:
                self._player_id = int(data.get("charID") or data.get("charId") or 0)
            except (TypeError, ValueError, OverflowError):
                # Do not pair a new player name with the previous player's ID.
                self._player_id = 0
            self.primary_player.emit(self._player_id,
                                     str(data.get("charName") or data.get("charname") or ""))
            return

        if mtype == "changezone":
            try:
                zid = int(data.get("zoneID") or data.get("zoneId") or 0)
            except (TypeError, ValueError, OverflowError):
                zid = 0
            self.zone_changed.emit(zid, str(data.get("zoneName") or ""))
            return

        if mtype == "partychanged":
            # Use PartyChanged for party membership because IINACT may report PartyType
            # as zero. Forward roster jobs for connections that missed spawn lines.
            pt: dict = {}
            jobs: dict = {}
            party = data.get("party")
            for m in (party if isinstance(party, list) else []):
                if not isinstance(m, dict):
                    continue
                mid = m.get("id")
                try:
                    mid = int(mid, 16) if isinstance(mid, str) else int(mid)
                except (TypeError, ValueError, OverflowError):
                    continue
                inp = m.get("inParty")
                pt[mid] = 1 if inp or inp is None else 2   # unknown counts as party
                try:
                    job = int(m.get("job") or 0)
                except (TypeError, ValueError, OverflowError):
                    job = 0
                if job:
                    jobs[mid] = job
            self._party_types = pt
            if jobs:
                self.party_jobs.emit(jobs)
            return

        if mtype == "combatants" or isinstance(data.get("combatants"), list):
            combs = data.get("combatants")
            if isinstance(combs, list):
                self.combatants.emit({"me": self._player_id, "list": _map_combatants(combs, self._party_types)})
            return

        raw = _extract_raw(data)
        if raw:
            self._emit_log_line(raw)

    def _emit_log_line(self, raw: str) -> None:
        fields = raw.split("|", 4)
        if fields[0] == "01" and len(fields) > 3 and len(fields[2]) <= 8:
            try:
                zone_id = int(fields[2], 16)
            except ValueError:
                pass
            else:
                if 0 <= zone_id <= 0xFFFFFFFF:
                    # Raw zone changes must also reach later recordings and sidecars.
                    self._state_cache["changezone"] = json.dumps({
                        "type": "ChangeZone", "zoneID": zone_id, "zoneName": fields[3]})
        self.log_line.emit(raw)

    def _schedule_reconnect(self) -> None:
        if self._auto_reconnect and not self._reconnect_timer.isActive():
            self._reconnect_timer.start(self._reconnect_delay)
            self._reconnect_delay = min(self._reconnect_delay * 2, 60000)


def _extract_raw(data: dict) -> str:
    """Extract a raw ACT log line, or an empty string for unrelated messages."""
    t = data.get("type", "")

    if str(t).lower() == "logline":
        line = data.get("line")
        raw = data.get("rawLine") or data.get("raw_line")
        if raw:
            # Coerce rawLine to the string required by its Qt signal.
            return str(raw)
        return "|".join(str(f) for f in line) if isinstance(line, list) else ""

    # Some IINACT versions use a broadcast wrapper.
    if str(t).lower() == "broadcast" and str(data.get("msgtype", "")).lower() == "logline":
        return str(data.get("msg", "")).strip()

    return ""


def _ci(v) -> int:
    # Integer conversion can overflow on nonfinite JSON numbers.
    try:
        return int(v)
    except (TypeError, ValueError, OverflowError):
        try:
            return int(float(v))
        except (TypeError, ValueError, OverflowError):
            return 0


def _cf(v) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    # Replace nonfinite values before sending strict JSON to the sidecar.
    return f if math.isfinite(f) else 0.0


def _map_combatants(combs: list, party_types: dict = None) -> list:
    """Normalize combatant field casing and numeric values for Triggernometry. Use
    PartyChanged membership when available.
    """
    party_types = party_types or {}
    out: list = []
    for c in combs:
        if not isinstance(c, dict):
            continue
        def g(*keys, default=None):
            for k in keys:
                if k in c and c[k] is not None:
                    return c[k]
            return default
        cid = _ci(g("ID", "id"))
        out.append({
            "id": cid,
            "name": str(g("Name", "name", default="") or ""),
            "job": _ci(g("Job", "job")),
            "level": _ci(g("Level", "level")),
            "party": party_types.get(cid, _ci(g("PartyType", "partyType", "party", default=0))),
            "hp": _ci(g("CurrentHP", "currentHp", "hp")),
            "maxhp": _ci(g("MaxHP", "maxHp", "maxhp")),
            "mp": _ci(g("CurrentMP", "currentMp", "mp")),
            "maxmp": _ci(g("MaxMP", "maxMp", "maxmp")),
            "x": _cf(g("PosX", "posX", "x")),
            "y": _cf(g("PosY", "posY", "y")),
            "z": _cf(g("PosZ", "posZ", "z")),
            "h": _cf(g("Heading", "heading", "h")),
            "targetid": _ci(g("TargetID", "targetID", "targetId", default=0)),
            "ownerid": _ci(g("OwnerID", "ownerID", "ownerId", default=0)),
            "bnpcid": _ci(g("BNpcID", "bNpcID", default=0)),
            "bnpcnameid": _ci(g("BNpcNameID", "bNpcNameID", default=0)),
            "worldid": _ci(g("WorldID", "worldID", default=0)),
            "worldname": str(g("WorldName", "worldName", default="") or ""),
            # Cast and distance fields may be unavailable in IINACT memory.
            "castid": _ci(g("CastBuffID", "castBuffID", "castid", default=0)),
            "casttargetid": _ci(g("CastTargetID", "castTargetID", default=0)),
            "casttime": _cf(g("CastDurationCurrent", "castDurationCurrent", default=0)),
            "maxcasttime": _cf(g("CastDurationMax", "castDurationMax", default=0)),
            "distance": _ci(g("EffectiveDistance", "effectiveDistance", default=0)),
        })
    return out
