"""State machines for UMAD status automarkers. BlackHoleChains advances cleanse queues as
Crust is removed. CursedShriekPairs assigns gaze signs using the wave's followup cast.
StatusPairs tracks compound rules. The host supplies monotonic time and handles log
routing and marker actions. See docs/UMAD-DEBUFFS.md for status evidence.
"""

from __future__ import annotations

import math

# Normalize status IDs to uppercase hex without prefixes or leading zeros.
ACCRETION = "644"
CRUST = "154E"   # UMAD Primordial Crust uses 154E. TOP uses 645.
ORDER_IDS = {"BBC": 1, "BBD": 2, "BBE": 3}   # First/Second/Third in Line
RELEVANT_IDS = frozenset(ORDER_IDS) | {ACCRETION, CRUST}

DPS, SUPPORT, ACC = "dps", "support", "accretion"
_EXPECTED = {DPS: 3, SUPPORT: 3, ACC: 2}
DEFAULT_MARKERS = {DPS: "attack1", SUPPORT: "attack2", ACC: "attack3"}

# Treat a long event gap as a new mechanic instance.
STALE_S = 90.0

# Reset an unstarted instance after a burst gap if nobody still holds Crust.
BURST_GAP_S = 5.0

# Unknown job IDs have no inferred role. Add new jobs explicitly before assigning them
# to queues.
_TANK_JOBS = {1, 3, 19, 21, 32, 37}                    # GLA MRD PLD WAR DRK GNB
_HEALER_JOBS = {6, 24, 28, 33, 40}                     # CNJ WHM SCH AST SGE
_DPS_JOBS = {2, 4, 5, 7, 20, 22, 23, 25, 26, 27, 29,   # PGL LNC ARC THM MNK DRG
             30, 31, 34, 35, 36, 38, 39, 41, 42}       # BRD BLM ACN SMN ROG NIN
                                                       # MCH SAM RDM BLU DNC RPR
                                                       # VPR PCT


def role_for_job(job: "int | None") -> "str | None":
    """Return support, dps or None for an unknown job ID."""
    if not job:
        return None
    if job in _TANK_JOBS or job in _HEALER_JOBS:
        return SUPPORT
    if job in _DPS_JOBS:
        return DPS
    return None


def _norm_id(effect_hex: str) -> str:
    """Normalize a status ID locally without adding dependencies."""
    s = str(effect_hex).strip().upper()
    if s.startswith("0X"):
        s = s[2:]
    return s.lstrip("0") or "0"


_HEX_DIGITS = frozenset("0123456789ABCDEF")


def parse_compound(status: str) -> "tuple[str, str] | None":
    """Parse two hexadecimal status IDs joined by +. Return None for other text so name
    rules still work.
    """
    if "+" not in status:
        return None
    parts = tuple(_norm_id(p) for p in status.split("+") if p.strip())
    if len(parts) != 2 or not all(p and set(p) <= _HEX_DIGITS for p in parts):
        return None
    return parts   # type: ignore[return-value]


def canon_status_key(status: str) -> str:
    """Return a stable compound status key regardless of part order, prefixes or leading
    zeros.
    """
    pair = parse_compound(status)
    return "+".join(sorted(pair)) if pair else _norm_id(status)


class StatusPairs:
    """Track status gains and losses for compound rules. The host supplies monotonic time
    and resets on wipes or zone changes. Expire stale entries so a missed loss cannot
    leave a false match.
    """

    def __init__(self, tracked, stale_s: float = STALE_S) -> None:
        self.tracked: frozenset = frozenset(_norm_id(t) for t in tracked)
        self._stale_s = float(stale_s)
        self._held: "dict[str, dict[str, float]]" = {}   # actor -> id -> gained-at

    def on_gain(self, effect_hex: str, actor_id: str, now: float) -> None:
        eff = _norm_id(effect_hex)
        if eff in self.tracked:
            self._held.setdefault(str(actor_id).strip().upper(), {})[eff] = now

    def on_loss(self, effect_hex: str, actor_id: str) -> None:
        actor_id = str(actor_id).strip().upper()
        held = self._held.get(actor_id)
        if held is None:
            return
        held.pop(_norm_id(effect_hex), None)
        if not held:
            del self._held[actor_id]

    def holds_all(self, actor_id: str, ids, now: float) -> bool:
        held = self._held.get(str(actor_id).strip().upper())
        if held is None:
            return False
        return all(i in held and now - held[i] <= self._stale_s for i in ids)

    def reset(self) -> None:
        self._held.clear()


class BlackHoleChains:
    """Track black hole cleanse queues. Event methods return mark actions with actor and
    token, or clear actions with actor. Actor IDs are uppercase hex. A queue in _holder
    has started, and a None holder means it has finished.
    """

    def __init__(self, role_of, markers: "dict[str, str] | None" = None):
        self._role_of = role_of           # actor_id -> 'dps' | 'support' | None
        self._markers = dict(DEFAULT_MARKERS)
        if markers:
            self.set_markers(markers)
        self.reset()

    def set_markers(self, markers: "dict[str, str]") -> None:
        """Update known queue tokens, retaining any omitted values."""
        for queue in (DPS, SUPPORT, ACC):
            tok = markers.get(queue)
            if tok:
                self._markers[queue] = tok

    def reset(self) -> None:
        """Forget the current mechanic instance."""
        self._players: "dict[str, dict]" = {}    # actor -> {order, accretion, crust}
        self._holder: "dict[str, str | None]" = {}   # started queue -> current holder
        self._last_event = 0.0

    def on_gain(self, effect_hex: str, actor_id: str, now: float) -> "list[tuple]":
        """Handle a relevant status gain on a player."""
        effect_hex = _norm_id(effect_hex)
        if effect_hex not in RELEVANT_IDS:
            return []
        actions: "list[tuple]" = []
        if self._players and (
                now - self._last_event > STALE_S
                # A completed instance can reset once all queues finish and nobody holds
                # Crust.
                or (self._holder
                    and all(v is None for v in self._holder.values())
                    and not any(p["crust"] for p in self._players.values()))
                # An unstarted instance can reset after a burst gap. Keep the current
                # burst together.
                or (not self._holder
                    and now - self._last_event > BURST_GAP_S
                    and not any(p["crust"] for p in self._players.values()))
                # Reset stalled queues after a quiet gap when only their heads still
                # hold Crust.
                or (self._holder
                    and now - self._last_event > BURST_GAP_S
                    and all(not p["crust"] or aid in self._holder.values()
                            for aid, p in self._players.items()))):
            # Clear existing signs before discarding the instance state.
            actions += [("clear", a) for a in self.outstanding()]
            self.reset()
        self._last_event = now
        p = self._players.setdefault(actor_id.upper(),
                                     {"order": None, "accretion": False, "crust": False})
        if effect_hex == ACCRETION:
            p["accretion"] = True
            # A late Accretion gain can change queue order. Recompute the head and move
            # its sign if needed.
            actions += self._reseat_accretion_head()
        elif effect_hex == CRUST:
            p["crust"] = True
        else:
            p["order"] = ORDER_IDS[effect_hex]
        return actions + self._start_ready_queues(require_complete=True)

    def on_loss(self, effect_hex: str, actor_id: str, now: float) -> "list[tuple]":
        """Handle a status loss. Only Crust removal advances queues."""
        if _norm_id(effect_hex) != CRUST:
            return []
        # Discard stale state before a late loss can advance an old queue.
        if self._players and now - self._last_event > STALE_S:
            actions = [("clear", a) for a in self.outstanding()]
            self.reset()
            return actions
        actor_id = actor_id.upper()
        p = self._players.get(actor_id)
        if p is None:
            return []
        self._last_event = now
        p["crust"] = False
        # Advance every queue this actor heads because a late Accretion gain can put
        # them at the head of two.
        actions: "list[tuple]" = []
        for queue, members in self._queues().items():
            if self._holder.get(queue) != actor_id:
                continue
            nxt = self._first_with_crust(members)
            if nxt is None:
                self._holder[queue] = None
                actions.append(("clear", actor_id))
            else:
                self._holder[queue] = nxt
                actions.append(("mark", nxt, self._markers[queue]))
        return actions

    def flush(self, now: float) -> "list[tuple]":
        """Start incomplete queues after the debounce if membership and order are known.
        Role queues still require both Accretion players. Every remaining Crust holder
        needs a known order.
        """
        # A delayed flush must not mark from stale state or refresh its event time.
        if self._players and now - self._last_event > STALE_S:
            actions = [("clear", a) for a in self.outstanding()]
            self.reset()
            return actions
        self._last_event = now
        return self._start_ready_queues(require_complete=False)

    def outstanding(self) -> "list[str]":
        """Return current sign holders in queue order so the host can clear them on reset.
        """
        held: "list[str]" = []
        for queue in (DPS, SUPPORT, ACC):
            actor = self._holder.get(queue)
            if actor and actor not in held:
                held.append(actor)
        return held

    def has_open_queues(self) -> bool:
        """Whether recorded players could still start a queue. The host retries the
        debounce when late role information arrives.
        """
        return bool(self._players) and len(self._holder) < len(_EXPECTED)

    def _queues(self) -> "dict[str, list[str]]":
        """Sort queue members by order, then actor ID. Unknown orders come last and unknown
        roles are omitted from role queues.
        """
        buckets: "dict[str, list[str]]" = {ACC: [], DPS: [], SUPPORT: []}
        for aid, p in self._players.items():
            if p["accretion"]:
                buckets[ACC].append(aid)
            else:
                role = self._role_of(aid)
                if role in buckets:
                    buckets[role].append(aid)
        for members in buckets.values():
            members.sort(key=lambda a: (self._players[a]["order"] or 99, a))
        return buckets

    def _unknown_role_count(self) -> int:
        return sum(1 for aid, p in self._players.items()
                   if not p["accretion"] and self._role_of(aid) not in (DPS, SUPPORT))

    def _first_with_crust(self, members: "list[str]") -> "str | None":
        for aid in members:
            if self._players[aid]["crust"]:
                return aid
        return None

    def _order_known_for_crusted(self, members: "list[str]") -> bool:
        """Whether every remaining Crust holder has a known order."""
        return all(self._players[a]["order"] is not None
                   for a in members if self._players[a]["crust"])

    def _reseat_accretion_head(self) -> "list[tuple]":
        """Move the Accretion sign if a late gain reveals an earlier queue member."""
        holder = self._holder.get(ACC)
        if holder is None:
            return []
        members = self._queues()[ACC]
        if not self._order_known_for_crusted(members):
            return []                   # Wait for a known order.
        head = self._first_with_crust(members)
        if head is None or head == holder:
            return []
        self._holder[ACC] = head
        return [("clear", holder), ("mark", head, self._markers[ACC])]

    def _start_ready_queues(self, require_complete: bool) -> "list[tuple]":
        actions: "list[tuple]" = []
        queues = self._queues()
        unknown = self._unknown_role_count()
        # Wait for both Accretion players before trusting role membership. Their order
        # and Crust gains can arrive first and temporarily place them in a role queue.
        acc_settled = len(queues[ACC]) >= 2
        for queue, members in queues.items():
            if queue in self._holder or not members:
                continue
            if queue != ACC and not acc_settled:
                continue    # Role membership is not yet known.
            if not self._order_known_for_crusted(members):
                continue    # Wait for every remaining Crust holder's order.
            if require_complete:
                if len(members) != _EXPECTED[queue]:
                    continue
                if any(self._players[a]["order"] is None or not self._players[a]["crust"]
                       for a in members):
                    continue
            elif queue != ACC and unknown:
                continue
            first = self._first_with_crust(members)
            if first is None:
                continue
            self._holder[queue] = first
            actions.append(("mark", first, self._markers[queue]))
        return actions


# Cursed Shriek gaze type follows the wave's Inferno or Tsunami cast. Gain order and
# duration do not identify it. See docs/UMAD-DEBUFFS.md.
CURSED_SHRIEK = "15A7"
GAZE_IDS = frozenset({CURSED_SHRIEK})
# Inferno indicates the fake gaze.
FAKE_FOLLOWUP_IDS = frozenset({"BB1E", "BB20"})    # Inferno, fire
# Tsunami indicates the real gaze.
REAL_FOLLOWUP_IDS = frozenset({"BB1F", "BB21"})    # Tsunami, water
GAZE_FOLLOWUP_IDS = FAKE_FOLLOWUP_IDS | REAL_FOLLOWUP_IDS
GAZE_PER_SET = 2
# Only the first two Grand Cross waves have gaze pairs.
GAZE_SETS = 2

# Real gaze pairs look away from each other. Fake gaze pairs look at each other.
AWAY1, AWAY2, LOOK1, LOOK2 = "away1", "away2", "look1", "look2"
_GAZE_KEYS = (AWAY1, AWAY2, LOOK1, LOOK2)
DEFAULT_GAZE_MARKERS = {AWAY1: "ignore1", AWAY2: "ignore2",
                        LOOK1: "bind1", LOOK2: "bind2"}


def _id_int(actor_id) -> int:
    """Parse actor IDs locally for ordering when party slots are unavailable."""
    s = str(actor_id).strip()
    if s[:2].lower() == "0x":
        s = s[2:]
    try:
        return int(s, 16)
    except ValueError:
        return 0


class CursedShriekPairs:
    """Assign Cursed Shriek pairs using each wave's followup cast. Inferno means fake gaze
    and Tsunami means real gaze. Wait for both carriers and a known polarity before
    marking. Number pairs by party slot, falling back to actor ID. Return the same
    actions as BlackHoleChains.
    """

    def __init__(self, gaze_ids=GAZE_IDS, markers: "dict[str, str] | None" = None,
                 slot_of=None):
        self._ids: frozenset = frozenset(_norm_id(i) for i in gaze_ids)
        self._slot_of = slot_of or (lambda _aid: None)
        self._markers = dict(DEFAULT_GAZE_MARKERS)
        if markers:
            self.set_markers(markers)
        self.reset()

    @property
    def ids(self) -> frozenset:
        return self._ids

    def set_markers(self, markers: "dict[str, str]") -> None:
        """Update known gaze tokens, retaining any omitted values."""
        for key in _GAZE_KEYS:
            tok = markers.get(key)
            if tok:
                self._markers[key] = tok

    def reset(self) -> None:
        """Forget the current phase."""
        self._polarity: "str | None" = None    # armed by on_followup, per set
        self._polarity_t = 0.0                 # when the armed tell landed
        self._set: "list[str]" = []            # the open set's carriers
        self._set_t = 0.0                      # first gain time of the open set
        self._sets_done = 0                    # closed sets, assigned or not
        self._assigned: "dict[str, str]" = {}  # actor -> slot key while marked
        self._active_until: "dict[str, float]" = {}
        self._last_event = 0.0

    def on_followup(self, effect_hex: str, now: float) -> "list[tuple]":
        """Arm the next gaze type from an Inferno or Tsunami cast. Gains place the marks.
        """
        eff = _norm_id(effect_hex)
        if eff in FAKE_FOLLOWUP_IDS:
            kind = LOOK1
        elif eff in REAL_FOLLOWUP_IDS:
            kind = AWAY1
        else:
            return []
        actions: "list[tuple]" = []
        if self._live() and now - self._last_event > STALE_S:
            # Clear stale signs and state before arming a new phase.
            actions += [("clear", a) for a in self.outstanding()]
            self.reset()
        self._last_event = now
        self._polarity = kind
        self._polarity_t = now
        return actions

    def on_gain(self, effect_hex: str, actor_id: str, duration, now: float) -> "list[tuple]":
        """Handle a gaze gain. Duration bounds repeats but does not identify gaze type."""
        if _norm_id(effect_hex) not in self._ids:
            return []
        actor_id = str(actor_id).strip().upper()
        actions: "list[tuple]" = []
        if self._polarity is not None and now - self._polarity_t > STALE_S:
            self._polarity = None
        if self._live() and now - self._last_event > STALE_S:
            actions += [("clear", a) for a in self.outstanding()]
            self.reset()
        elif self._sets_done >= GAZE_SETS:
            if (actor_id in self._assigned and self._polarity is None
                    and (0 <= now - self._set_t <= BURST_GAP_S
                         or now <= self._active_until.get(actor_id, 0.0))):
                self._last_event = now
                return actions
            # A later gain starts a new phase. Keep a fresh tell from that wave.
            actions += [("clear", a) for a in self.outstanding()]
            armed = self._polarity if now - self._last_event <= STALE_S else None
            armed_t = self._polarity_t if armed is not None else 0.0
            self.reset()
            self._polarity = armed
            self._polarity_t = armed_t
        elif actor_id in self._assigned:
            self._last_event = now
            return actions
        self._last_event = now
        # Discard an incomplete set after its burst gap. Preserve a tell armed after
        # that set opened because it belongs to the next wave.
        if self._set and now - self._set_t > BURST_GAP_S:
            for actor in self._set:
                self._active_until.pop(actor, None)
            self._set = []
            if self._polarity_t <= self._set_t:
                self._polarity = None
        if actor_id in self._set:
            return actions
        if not self._set:
            self._set_t = now
        self._set.append(actor_id)
        self._active_until.pop(actor_id, None)
        try:
            remaining = float(duration)
        except (TypeError, ValueError):
            remaining = 0.0
        if math.isfinite(remaining) and remaining > 0:
            self._active_until[actor_id] = now + min(remaining, STALE_S)
        if len(self._set) < GAZE_PER_SET:
            return actions
        # A complete pair without a known polarity remains unmarked.
        polarity, self._polarity = self._polarity, None
        self._sets_done += 1
        if polarity is None:
            for actor in self._set:
                self._active_until.pop(actor, None)
            self._set = []
            return actions
        keys = (LOOK1, LOOK2) if polarity == LOOK1 else (AWAY1, AWAY2)
        pair = self._ordered(self._set)
        self._set = []
        for actor, key in zip(pair, keys):
            self._assigned[actor] = key
            actions.append(("mark", actor, self._markers[key]))
        return actions

    def on_loss(self, effect_hex: str, actor_id: str, now: float) -> "list[tuple]":
        """Clear the sign when this player loses the gaze."""
        if _norm_id(effect_hex) not in self._ids:
            return []
        actor_id = str(actor_id).strip().upper()
        # Discard stale state before a late loss can refresh the phase time.
        if self._live() and now - self._last_event > STALE_S:
            actions = [("clear", a) for a in self.outstanding()]
            self.reset()
            return actions
        self._last_event = now
        if actor_id in self._set:
            # An incomplete set has no marks. Remove this carrier but preserve a tell
            # from the next wave.
            self._set.remove(actor_id)
            self._active_until.pop(actor_id, None)
            if not self._set and self._polarity_t <= self._set_t:
                self._polarity = None
        if actor_id in self._assigned:
            self._assigned.pop(actor_id)
            self._active_until.pop(actor_id, None)
            return [("clear", actor_id)]
        return []

    def flush(self, now: float) -> "list[tuple]":
        """Discard incomplete sets and clear signs from stale phases after the debounce.
        """
        if self._live() and now - self._last_event > STALE_S:
            actions = [("clear", a) for a in self.outstanding()]
            self.reset()
            return actions
        self._last_event = now
        if self._set and now - self._set_t > BURST_GAP_S:
            for actor in self._set:
                self._active_until.pop(actor, None)
            self._set = []
            if self._polarity_t <= self._set_t:
                self._polarity = None
        return []

    def outstanding(self) -> "list[str]":
        """Return current gaze sign holders in a stable order for cleanup."""
        return sorted(self._assigned, key=_id_int)

    def _live(self) -> bool:
        return bool(self._polarity is not None or self._set or self._assigned or self._sets_done)

    def _ordered(self, actors: "list[str]") -> "list[str]":
        """Order each pair by party slot, falling back to actor ID."""
        def key(a):
            slot = self._slot_of(a)
            # Slot zero is valid, so check against None.
            return (0, slot, "") if slot is not None else (1, _id_int(a), a)
        return sorted(actors, key=key)
