"""WebSocket client for the IINACT/ACT combat feed.

Parses LogLine / ChangeZone / ChangePrimaryPlayer / PartyChanged events,
drives NyaaTriggers' own engine and DPS logging off the log lines, and
forwards the raw message stream to the Triggevent and Triggernometry
sidecars. CombatData is subscribed only so that tee forwards it, DPS comes
from the log lines via dps_meter, not from CombatData.
"""

import json
import math

from PyQt6.QtCore import QObject, QTimer, QUrl, pyqtSignal
from PyQt6.QtNetwork import QAbstractSocket
from PyQt6.QtWebSockets import QWebSocket

# LogLine drives the local engine and DPS logging. The rest are subscribed
# for the raw_message tee, triggevent-core needs the state events for
# player/zone/party sync, and they are also consumed locally, player id,
# party-type map and zone, for the triggernometry-core combatant feed.
_SUBSCRIBE = json.dumps({"call": "subscribe", "events": [
    "LogLine", "CombatData", "ChangePrimaryPlayer", "ChangeZone", "PartyChanged",
    "InCombat",
]})

# Inbound text frames go straight to json.loads. Cap them so a hostile or buggy
# peer can't make the GUI thread parse and hold a giant message. Real ACT
# frames are kilobytes.
_MAX_WS_MESSAGE = 4 << 20


class WSClient(QObject):
    log_line = pyqtSignal(str)          # raw pipe-delimited ACT log line
    combatants = pyqtSignal(dict)       # me/list combatant snapshot with positions and HP
    party_jobs = pyqtSignal(dict)       # actor_id_int -> job_int from PartyChanged, decimal job ids
    zone_changed = pyqtSignal(int, str)     # zoneId and zoneName from ChangeZone
    primary_player = pyqtSignal(int, str)   # charID and charName from ChangePrimaryPlayer
    raw_message = pyqtSignal(str)       # every raw WS text msg, verbatim, teed to the sidecar
    status_changed = pyqtSignal(bool, str)  # connected and message
    # inACTCombat and inGameCombat from InCombat. Combat state transitions,
    # needed for timeline InCombat syncs and leave-combat detection.
    in_combat = pyqtSignal(bool, bool)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._url = ""
        self._auto_reconnect = False
        self._reopen_on_disconnect = False   # connect_to over a live socket means reopen once closed
        self._player_id = 0             # tracked from ChangePrimaryPlayer, for combatant "me"
        self._party_types: dict = {}    # combatant_id -> 1 party, 2 alliance, from PartyChanged
        # Set when _on_error already reported an error-driven drop, so the
        # disconnected signal that Qt fires right after does not emit a second
        # status_changed with different text and flicker the status label.
        self._error_reported = False
        # Last zone/player/party messages. IINACT sends these ONCE, on subscribe.
        # The Triggevent Engine boots ~10s after we connect, so its feed dropped
        # them and it never learns the zone. Kept here so replay_state can hand
        # them to the engine when it comes up. Fixes callouts on a mid-instance start.
        self._state_cache: dict = {}    # msgtype -> raw_msg

        self._ws = QWebSocket(parent=self)
        self._ws.setMaxAllowedIncomingMessageSize(_MAX_WS_MESSAGE)
        self._ws.connected.connect(self._on_connected)
        self._ws.disconnected.connect(self._on_disconnected)
        self._ws.textMessageReceived.connect(self._on_message)
        self._ws.errorOccurred.connect(self._on_error)

        self._reconnect_timer = QTimer(self)
        self._reconnect_timer.setSingleShot(True)
        self._reconnect_timer.timeout.connect(self._open)
        self._reconnect_delay = 5000    # ms, doubled per retry up to 60 s

        # getCombatants carries live positions/HP the LogLine/CombatData feeds
        # do not. Off by default. main_window enables it only while the
        # Triggernometry sidecar is active, since it needs ${_me}/position.
        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(600)
        self._poll_timer.timeout.connect(self._request_combatants)
        self._poll_enabled = False

    # ------------------------------------------------------------------
    def connect_to(self, url: str) -> None:
        # Only ws/wss are accepted. A pasted file://, http:// or junk URL would
        # otherwise be handed to Qt and surface as a confusing connection error.
        if url:
            scheme = QUrl(url).scheme().lower()
            if scheme not in ("ws", "wss"):
                self.status_changed.emit(
                    False, "Connect URL must start with ws:// or wss://")
                return
        self._url = url
        self._auto_reconnect = True
        self._reconnect_timer.stop()
        # A user-initiated reconnect starts from the base delay. Otherwise a
        # prior long backoff, up to 60s, makes the first failed retry wait a
        # full minute even though ACT may now be reachable.
        self._reconnect_delay = 5000
        if self._ws.state() != QAbstractSocket.SocketState.UnconnectedState:
            # close is asynchronous. Opening immediately would race the
            # deferred disconnect, whose handler then armed a reconnect that
            # later re-opened over the fresh connection and aborted it.
            # Reopen from the disconnected handler instead.
            self._reopen_on_disconnect = True
            self._ws.close()
            return
        self._open()

    def disconnect_from(self) -> None:
        self._auto_reconnect = False
        self._reopen_on_disconnect = False
        self._reconnect_timer.stop()
        self._ws.close()

    # ------------------------------------------------------------------
    def _open(self) -> None:
        if not self._url:
            return
        # Never open over a live or connecting socket. It aborts the healthy
        # connection. A stray reconnect timer armed by a transient errorOccurred
        # could otherwise churn the link every 5 s.
        state = self._ws.state()
        if state == QAbstractSocket.SocketState.ConnectedState:
            return
        if state == QAbstractSocket.SocketState.ConnectingState:
            # QWebSocket has no handshake timeout, and a host that accepts
            # TCP then goes silent mid handshake never raises an OS error.
            # The timer firing means this attempt already had a full backoff
            # interval, so cut it loose and fall through to a fresh open
            # instead of parking the retry loop on the stuck socket forever.
            self._ws.abort()
        self._ws.open(QUrl(self._url))
        # Watchdog for the fresh attempt. The timer that fired to start it is
        # spent, and a stalled handshake emits no signal that would arm a new
        # one. _on_connected cancels it on success.
        self._schedule_reconnect()

    def _on_connected(self) -> None:
        self._reconnect_timer.stop()   # a pending reconnect must not fire now
        self._reconnect_delay = 5000   # backoff resets after a good connect
        self._error_reported = False
        self.status_changed.emit(True, "Connected")
        self._ws.sendTextMessage(_SUBSCRIBE)
        if self._poll_enabled:
            self._poll_timer.start()

    def _on_disconnected(self) -> None:
        self._poll_timer.stop()
        # Player/party identity may change while we're down. Both are relearned
        # from the ChangePrimaryPlayer/PartyChanged burst IINACT sends on the
        # resubscribe, so drop the stale mapping rather than carry it over.
        self._player_id = 0
        self._party_types = {}
        self._state_cache.clear()
        if self._error_reported:
            # _on_error already told the UI about this drop with the real
            # error text. A second emit here would just relabel it Disconnected.
            self._error_reported = False
        else:
            self.status_changed.emit(False, "Disconnected")
        if self._reopen_on_disconnect:
            self._reopen_on_disconnect = False
            self._open()               # user-requested reconnect, no wait
        # Always arm the retry timer too. If the immediate reopen above bailed
        # or its attempt fails, the timer retries. _on_connected stops it.
        self._schedule_reconnect()

    # ------------------------------------------------------------------
    def set_combatant_polling(self, enabled: bool) -> None:
        """Toggle the getCombatants poll. Safe to call anytime. Only polls while connected."""
        self._poll_enabled = bool(enabled)
        if self._poll_enabled and self._ws.isValid():
            self._request_combatants()   # one immediately so ${_me} populates fast
            self._poll_timer.start()
        elif not self._poll_enabled:
            self._poll_timer.stop()

    def _request_combatants(self) -> None:
        if self._ws.isValid():
            self._ws.sendTextMessage(json.dumps({"call": "getCombatants"}))

    def request_combatants_once(self) -> None:
        """One-off getCombatants regardless of the polling toggle. The reply
        comes via `combatants`. Backfills party jobs when the app starts
        mid-instance and the 03 AddedCombatant burst is long gone."""
        self._request_combatants()

    def replay_state(self) -> None:
        """Re-tee the cached zone, player, party and combat state messages to
        sidecar listeners and re-request combatants. IINACT sends the world
        state once on subscribe, but
        the Triggevent Engine boots ~10s later, so it missed the zone and stays
        disarmed on a mid-instance start. Called when the engine comes up so it
        learns the current zone with no reconnect or zone change needed. No-op if
        nothing cached yet. The engine then gets state live once it is active."""
        for key in ("changeprimaryplayer", "changezone", "partychanged", "incombat"):
            msg = self._state_cache.get(key)
            if msg:
                self.raw_message.emit(msg)
        self._request_combatants()

    def _on_error(self, _err) -> None:
        if self._ws.isValid():
            return   # transient error on a live socket, a real drop fires disconnected
        self._error_reported = True   # _on_disconnected follows, let it stay quiet
        self.status_changed.emit(False, self._ws.errorString())
        self._schedule_reconnect()

    def _on_message(self, msg: str) -> None:
        # Tee the raw message verbatim to the sidecar listeners before parsing.
        self.raw_message.emit(msg)
        try:
            data = json.loads(msg)
        except (ValueError, RecursionError):
            # RecursionError: a hostile frame nested past CPython's limit fits
            # under the 4 MiB cap, and it derives from RuntimeError, not
            # ValueError, so it needs naming to land here.
            raw = msg.strip()
            if raw:
                self.log_line.emit(raw)
            return

        if not isinstance(data, dict):
            raw = msg.strip()
            if raw:
                self.log_line.emit(raw)
            return

        mtype = str(data.get("type", "")).lower()

        # Keep the one-shot world-state messages so replay_state can re-feed a
        # sidecar that started after this one arrived.
        if mtype in ("changezone", "changeprimaryplayer", "partychanged", "incombat"):
            self._state_cache[mtype] = msg

        if mtype == "incombat":
            # ACT keeps its own combat notion next to the game's. The timeline
            # cares about the game one, but syncs may match either field.
            self.in_combat.emit(bool(data.get("inACTCombat")), bool(data.get("inGameCombat")))
            return

        if mtype == "changeprimaryplayer":
            try:
                self._player_id = int(data.get("charID") or data.get("charId") or 0)
            except (TypeError, ValueError, OverflowError):
                # Reset, not keep. Pairing the new name with the previous
                # character's id would skew combatants "me" as well.
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
            # getCombatants reports PartyType=0 on Linux/IINACT, so derive
            # party/alliance membership from the PartyChanged roster. It also
            # carries each member's ClassJob, a decimal int. Tee that out so
            # role-aware features get jobs even when the 03 burst was missed.
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
            self.log_line.emit(raw)

    def _schedule_reconnect(self) -> None:
        if self._auto_reconnect and not self._reconnect_timer.isActive():
            self._reconnect_timer.start(self._reconnect_delay)
            self._reconnect_delay = min(self._reconnect_delay * 2, 60000)


# ----------------------------------------------------------------------
def _extract_raw(data: dict) -> str:
    """Extract a raw pipe-delimited log line from a parsed IINACT/OverlayPlugin
    WebSocket message. Returns "" for non-LogLine messages."""
    t = data.get("type", "")

    # the standard OverlayPlugin / IINACT layout
    if str(t).lower() == "logline":
        line = data.get("line")
        raw = data.get("rawLine") or data.get("raw_line")
        if raw:
            # rawLine is not guaranteed to be a str, and the pyqtSignal(str)
            # it feeds raises TypeError on anything else. Coerce like the
            # broadcast path below.
            return str(raw)
        return "|".join(str(f) for f in line) if isinstance(line, list) else ""

    # broadcast wrapper some IINACT versions use
    if str(t).lower() == "broadcast" and str(data.get("msgtype", "")).lower() == "logline":
        return str(data.get("msg", "")).strip()

    return ""


def _ci(v) -> int:
    # OverflowError rides along with the ValueError pair. json accepts 1e999
    # and Infinity, and int on the resulting float raises it, not ValueError.
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
    # inf and nan must not reach the sidecar feed. json.dumps would emit the
    # non standard Infinity or NaN tokens, which the strict .NET JSON parser
    # on the other end rejects, dropping the whole combatant snapshot.
    return f if math.isfinite(f) else 0.0


def _map_combatants(combs: list, party_types: dict = None) -> list:
    """Map getCombatants entries, PascalCase FFXIV_ACT_Plugin model with
    camelCase fallbacks, to the triggernometry-core schema, numeric values and
    lowercase keys. party_types, id -> 1 or 2 from PartyChanged, supplies
    membership since IINACT reports PartyType=0. Exact key casing unverified
    against live IINACT."""
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
            # cast/distance fields ParseCombatant reads, may come back 0 from IINACT memory
            "castid": _ci(g("CastBuffID", "castBuffID", "castid", default=0)),
            "casttargetid": _ci(g("CastTargetID", "castTargetID", default=0)),
            "casttime": _cf(g("CastDurationCurrent", "castDurationCurrent", default=0)),
            "maxcasttime": _cf(g("CastDurationMax", "castDurationMax", default=0)),
            "distance": _ci(g("EffectiveDistance", "effectiveDistance", default=0)),
        })
    return out
