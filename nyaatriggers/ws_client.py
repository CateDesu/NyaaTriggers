import json
import math
import os
import time

from PyQt6.QtCore import QObject, QTimer, QUrl, pyqtSignal
from PyQt6.QtNetwork import QAbstractSocket
from PyQt6.QtWebSockets import QWebSocket

from nyaatriggers.combatant_responses import CombatantResponses, extract_raw as _extract_raw, request_tag
from nyaatriggers.diagnostics import record

_SUBSCRIBE = json.dumps({"call": "subscribe", "events": [
    "LogLine", "CombatData", "ChangePrimaryPlayer", "ChangeZone", "PartyChanged",
    "InCombat",
]})

_MAX_WS_MESSAGE = 4 << 20
_PING_INTERVAL_MS = 15000
_PONG_TIMEOUT_MS = 10000


class WSClient(QObject):
    log_line = pyqtSignal(str)          # raw pipe-delimited ACT log line
    combatants = pyqtSignal(dict)       # me/list combatant snapshot with positions and HP
    party_jobs = pyqtSignal(dict)       # actor_id_int -> job_int from PartyChanged, decimal job ids
    zone_changed = pyqtSignal(int, str)     # zoneId and zoneName from ChangeZone
    primary_player = pyqtSignal(int, str)   # charID and charName from ChangePrimaryPlayer
    raw_message = pyqtSignal(str)       # every raw WS text message for recordings
    engine_message = pyqtSignal(str)    # accepted replies with native response tags
    status_changed = pyqtSignal(bool, str)  # connected and message
    in_combat = pyqtSignal(bool, bool)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._url = ""
        self._auto_reconnect = False
        self._reopen_on_disconnect = False   # connect_to over a live socket means reopen once closed
        self._player_id = 0             # tracked from ChangePrimaryPlayer, for combatant "me"
        self._party_types: dict = {}    # combatant_id -> 1 party, 2 alliance, from PartyChanged
        self._error_reported = False
        self._state_cache: dict = {}
        self._combatant_sequence = 0
        self._combatant_responses = CombatantResponses()
        self._diag_frames = 0
        self._diag_bytes = 0
        self._diag_log_lines = 0
        self._diag_rejected = 0
        self._diag_at = time.monotonic()

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
        self._reconnect_delay = 5000

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
        if url:
            scheme = QUrl(url).scheme().lower()
            if scheme not in ("ws", "wss"):
                self.status_changed.emit(
                    False, "Connect URL must start with ws:// or wss://")
                return
        self._url = url
        self._auto_reconnect = True
        self._reconnect_timer.stop()
        self._reconnect_delay = 5000
        state = self._ws.state()
        if state in (QAbstractSocket.SocketState.HostLookupState,
                     QAbstractSocket.SocketState.ConnectingState):
            # An unfinished handshake may never emit disconnected after close.
            self._reopen_on_disconnect = False
            self._stop_heartbeat()
            self._ws.abort()
            self._open()
            return
        if state != QAbstractSocket.SocketState.UnconnectedState:
            # Wait for close so its callback cannot abort a new connection.
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
        # Cancel local work even when the peer cannot acknowledge a close.
        self._ws.abort()

    def _open(self) -> None:
        if not self._url:
            return
        # Ignore stale reconnect timers while a socket is connected or connecting.
        state = self._ws.state()
        if state == QAbstractSocket.SocketState.ConnectedState:
            return
        if state == QAbstractSocket.SocketState.ConnectingState:
            # Abort an expired handshake before starting another.
            self._ws.abort()
        self._ws.open(QUrl(self._url))
        record("ws_state", state="connecting")
        self._schedule_reconnect()

    def _on_connected(self) -> None:
        record("ws_state", state="connected")
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
        record("ws_state", state="disconnected")
        self._poll_timer.stop()
        self._refresh_timer.stop()
        self._refresh_ids.clear()
        self._refresh_all = False
        self._stop_heartbeat()
        # Discard identity mappings on feed loss. Subscription replay repopulates them.
        self._player_id = 0
        self._party_types = {}
        self._state_cache.clear()
        self._combatant_responses.reset(self._combatant_sequence)
        if self._error_reported:
            self._error_reported = False
        else:
            self.status_changed.emit(False, "Disconnected")
        if self._reopen_on_disconnect:
            self._reopen_on_disconnect = False
            self._open()
        self._schedule_reconnect()

    def _stop_heartbeat(self) -> None:
        self._heartbeat_timer.stop()
        self._pending_ping = None

    def _heartbeat_tick(self) -> None:
        if self._ws.state() != QAbstractSocket.SocketState.ConnectedState:
            self._stop_heartbeat()
            return
        if self._pending_ping is not None:
            record("ws_state", state="heartbeat_timeout")
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
        # Accept IINACT's two zero prefix bytes while still checking the current probe token.
        if bytes(payload) not in (pending, b"\x00\x00" + pending):
            record("ws_state", state="pong_rejected")
            return
        if self._ws.state() != QAbstractSocket.SocketState.ConnectedState:
            return
        self._pending_ping = None
        self._heartbeat_timer.start(_PING_INTERVAL_MS)
        record("ws_state", state="pong_accepted", elapsed_ms=_elapsed)

    def set_combatant_polling(self, enabled: bool) -> None:
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
            self._ws.sendTextMessage(json.dumps({"call": "getCombatants",
                "rseq": self._combatant_tag("allCombatants")}))

    def _combatant_tag(self, kind):
        self._combatant_sequence += 1
        return request_tag(kind, self._combatant_sequence, self._combatant_responses.zone_token or "")

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
                    "rseq": self._combatant_tag("specificCombatants"), "ids": sorted(self._refresh_ids)}))
        self._refresh_ids.clear()
        self._refresh_all = False

    def request_combatants_once(self) -> None:
        self._request_combatants()

    def replay_state(self) -> None:
        """Replay cached state before requesting current combatants."""
        for msg in self.state_snapshot():
            self.raw_message.emit(msg)
            self.engine_message.emit(msg)
        self._request_combatants()

    def state_snapshot(self) -> tuple[str, ...]:
        return tuple(self._state_cache[key] for key in
                     ("changeprimaryplayer", "changezone", "partychanged", "incombat")
                     if key in self._state_cache)

    def _on_error(self, _err) -> None:
        if self._ws.isValid():
            return   # Wait for disconnected before treating a transient socket error as feed loss.
        self._error_reported = True
        record("ws_state", state="error", error_code=getattr(_err, "value", 0))
        self.status_changed.emit(False, self._ws.errorString())
        self._schedule_reconnect()

    def _on_message(self, msg: str) -> None:
        self._diag_frames += 1
        self._diag_bytes += len(msg.encode("utf-8", errors="replace"))
        now = time.monotonic()
        if now - self._diag_at >= 10:
            record("ws_feed", frames=self._diag_frames, bytes=self._diag_bytes,
                   log_lines=self._diag_log_lines, rejected=self._diag_rejected)
            self._diag_at = now
        self.raw_message.emit(msg)
        try:
            data = json.loads(msg)
        except (ValueError, RecursionError):
            self._combatant_responses.observe_raw(msg.strip(), self._combatant_sequence)
            self.engine_message.emit(msg)
            raw = msg.strip()
            if raw:
                self._emit_log_line(raw)
            return

        if not isinstance(data, dict):
            self.engine_message.emit(msg)
            raw = msg.strip()
            if raw:
                self._emit_log_line(raw)
            return

        accepted = self._combatant_responses.accept(data, self._combatant_sequence)
        if accepted is None:
            self._diag_rejected += 1
            return
        self.engine_message.emit(msg if accepted is data else json.dumps(accepted))
        data = accepted
        mtype = str(data.get("type", "")).lower()

        if mtype in ("changezone", "changeprimaryplayer", "partychanged", "incombat"):
            self._state_cache[mtype] = msg

        if mtype == "incombat":
            self.in_combat.emit(bool(data.get("inACTCombat")), bool(data.get("inGameCombat")))
            return

        if mtype == "changeprimaryplayer":
            self._player_id = _signal_id(data.get("charID") or data.get("charId") or 0)
            self.primary_player.emit(self._player_id,
                                     str(data.get("charName") or data.get("charname") or ""))
            return

        if mtype == "changezone":
            zid = _signal_id(data.get("zoneID") or data.get("zoneId") or 0)
            self.zone_changed.emit(zid, str(data.get("zoneName") or ""))
            return

        if mtype == "partychanged":
            # Use PartyChanged membership because IINACT may report PartyType as zero.
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
                pt[mid] = 1 if inp or inp is None else 2
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
            # Partial replies cannot replace the full roster.
            if isinstance(combs, list) and data.get("rseq") != "specificCombatants":
                self.combatants.emit({"me": self._player_id, "list": _map_combatants(combs, self._party_types)})
            return

        raw = _extract_raw(data)
        if raw:
            self._emit_log_line(raw)

    def _emit_log_line(self, raw: str) -> None:
        self._diag_log_lines += 1
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
        elif fields[0] == "02" and len(fields) > 3 and len(fields[2]) <= 8:
            try:
                player_id = int(fields[2], 16)
            except ValueError:
                pass
            else:
                if 0x10000000 <= player_id < 0x11000000:
                    self._player_id = player_id
                    self._state_cache["changeprimaryplayer"] = json.dumps({
                        "type": "ChangePrimaryPlayer", "charID": player_id, "charName": fields[3]})
        self.log_line.emit(raw)

    def _schedule_reconnect(self) -> None:
        if self._auto_reconnect and not self._reconnect_timer.isActive():
            record("ws_state", state="reconnect_scheduled", delay_ms=self._reconnect_delay)
            self._reconnect_timer.start(self._reconnect_delay)
            self._reconnect_delay = min(self._reconnect_delay * 2, 60000)


def _signal_id(value) -> int:
    """Reject IDs that Qt would truncate when emitting its signed int signal."""
    try:
        ident = int(value)
    except (TypeError, ValueError, OverflowError):
        return 0
    return ident if 0 <= ident <= 0x7FFFFFFF else 0


def _ci(v) -> int:
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
    """Normalize Triggernometry combatants, using PartyChanged membership when available."""
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
