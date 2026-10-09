"""UMAD status automarkers. The host supplies monotonic time and marker routing.
See docs/UMAD-DEBUFFS.md for status evidence."""

from __future__ import annotations

import math

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

_TANK_JOBS = {1, 3, 19, 21, 32, 37}                    # GLA MRD PLD WAR DRK GNB
_HEALER_JOBS = {6, 24, 28, 33, 40}                     # CNJ WHM SCH AST SGE
_DPS_JOBS = {2, 4, 5, 7, 20, 22, 23, 25, 26, 27, 29,   # PGL LNC ARC THM MNK DRG
             30, 31, 34, 35, 36, 38, 39, 41, 42}       # BRD BLM ACN SMN ROG NIN
                                                       # MCH SAM RDM BLU DNC RPR
                                                       # VPR PCT


def role_for_job(job: "int | None") -> "str | None":
    if not job:
        return None
    if job in _TANK_JOBS or job in _HEALER_JOBS:
        return SUPPORT
    if job in _DPS_JOBS:
        return DPS
    return None


def _norm_id(effect_hex: str) -> str:
    s = str(effect_hex).strip().upper()
    if s.startswith("0X"):
        s = s[2:]
    return s.lstrip("0") or "0"


_HEX_DIGITS = frozenset("0123456789ABCDEF")


def parse_compound(status: str) -> "tuple[str, str] | None":
    """Parse two hex status IDs joined by +, or return None for name rules."""
    if "+" not in status:
        return None
    parts = tuple(_norm_id(p) for p in status.split("+") if p.strip())
    if len(parts) != 2 or not all(p and set(p) <= _HEX_DIGITS for p in parts):
        return None
    return parts   # type: ignore[return-value]


def canon_status_key(status: str) -> str:
    """Canonicalize compound IDs regardless of order, prefixes or leading zeros."""
    pair = parse_compound(status)
    return "+".join(sorted(pair)) if pair else _norm_id(status)


def status_expires_at(duration, now: float) -> float | None:
    try:
        remaining = float(duration)
    except (TypeError, ValueError, OverflowError):
        return None
    return now + remaining if math.isfinite(remaining) and remaining > 0 else None


class StatusPairs:
    """Track compound statuses, expiring missed losses. The host supplies time and resets."""

    def __init__(self, tracked, stale_s: float = STALE_S) -> None:
        self.tracked: frozenset = frozenset(_norm_id(t) for t in tracked)
        self._stale_s = float(stale_s)
        self._held: "dict[str, dict[str, tuple[float, float | None]]]" = {}

    def on_gain(self, effect_hex: str, actor_id: str, now: float, duration=None) -> None:
        eff = _norm_id(effect_hex)
        if eff in self.tracked:
            self._held.setdefault(str(actor_id).strip().upper(), {})[eff] = (
                now, status_expires_at(duration, now))

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
        return all(i in held and now - held[i][0] <= self._stale_s
                   and (held[i][1] is None or now < held[i][1]) for i in ids)

    def expires_at(self, actor_id: str, ids) -> float | None:
        held = self._held.get(str(actor_id).strip().upper(), {})
        deadlines = [held[effect][1] for effect in ids
                     if effect in held and held[effect][1] is not None]
        return min(deadlines) if deadlines else None

    def reset(self) -> None:
        self._held.clear()


class BlackHoleChains:
    """Return mark or clear actions for uppercase actor IDs.
_holder tracks started queues, with None marking a finished queue."""

    def __init__(self, role_of, markers: "dict[str, str] | None" = None,
                 *, include_accretion: bool = True):
        self._role_of = role_of           # actor_id -> 'dps' | 'support' | None
        self._output_queues = (DPS, SUPPORT, ACC) if include_accretion else (DPS, SUPPORT)
        self._markers = dict(DEFAULT_MARKERS)
        if markers:
            self.set_markers(markers)
        self.reset()

    def set_markers(self, markers: "dict[str, str]") -> None:
        for queue in self._output_queues:
            tok = markers.get(queue)
            if tok:
                self._markers[queue] = tok

    def reset(self) -> None:
        self._players: "dict[str, dict]" = {}    # actor -> {order, accretion, crust}
        self._holder: "dict[str, str | None]" = {}   # started queue -> current holder
        self._last_event = 0.0

    def on_gain(self, effect_hex: str, actor_id: str, now: float) -> "list[tuple]":
        effect_hex = _norm_id(effect_hex)
        if effect_hex not in RELEVANT_IDS:
            return []
        actions: "list[tuple]" = []
        if self._players and (
                now - self._last_event > STALE_S
                # Reset completed instances after all queues finish and nobody holds Crust.
                or (self._holder
                    and all(v is None for v in self._holder.values())
                    and not any(p["crust"] for p in self._players.values()))
                # Keep the current burst together before resetting unstarted instances.
                or (not self._holder
                    and now - self._last_event > BURST_GAP_S
                    and not any(p["crust"] for p in self._players.values()))
                # Reset stalled queues after a quiet gap when only heads hold Crust.
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
            # Late Accretion gains can move a queue head and its sign.
            if ACC in self._output_queues:
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
        # Late Accretion can make one actor head two queues.
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
        """Start queues after debounce once orders are known. Role queues also need both Accretion players."""
        # A delayed flush must not mark from stale state or refresh its event time.
        if self._players and now - self._last_event > STALE_S:
            actions = [("clear", a) for a in self.outstanding()]
            self.reset()
            return actions
        return self._start_ready_queues(require_complete=False)

    def outstanding(self) -> "list[str]":
        held: "list[str]" = []
        for queue in self._output_queues:
            actor = self._holder.get(queue)
            if actor and actor not in held:
                held.append(actor)
        return held

    def has_open_queues(self) -> bool:
        """Report queues that may start after late role information arrives."""
        return bool(self._players) and len(self._holder) < len(self._output_queues)

    def _queues(self) -> "dict[str, list[str]]":
        """Sort by order then actor ID, placing unknown orders last and omitting unknown roles."""
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
        return all(self._players[a]["order"] is not None
                   for a in members if self._players[a]["crust"])

    def _reseat_accretion_head(self) -> "list[tuple]":
        holder = self._holder.get(ACC)
        if holder is None:
            return []
        members = self._queues()[ACC]
        if not self._order_known_for_crusted(members):
            return []
        head = self._first_with_crust(members)
        if head is None or head == holder:
            return []
        self._holder[ACC] = head
        return [("clear", holder), ("mark", head, self._markers[ACC])]

    def _start_ready_queues(self, require_complete: bool) -> "list[tuple]":
        actions: "list[tuple]" = []
        queues = self._queues()
        unknown = self._unknown_role_count()
        # Wait for both Accretion players. Early order and Crust gains can misidentify role queues.
        acc_settled = len(queues[ACC]) >= 2
        for queue, members in queues.items():
            if queue not in self._output_queues:
                continue
            if queue in self._holder or not members:
                continue
            if len(members) != _EXPECTED[queue]:
                continue
            if queue != ACC and not acc_settled:
                continue
            if not self._order_known_for_crusted(members):
                continue
            if require_complete:
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


ACCRETION_IDS = frozenset({ACCRETION, CRUST, "BBC", "BBD"})
DEFAULT_ACCRETION_MARKERS = {"first": "ignore1", "second": "ignore2"}


class AccretionQueue:
    """Keep each carrier marked until its third tether hit removes Crust."""

    def __init__(self, markers: "dict[str, str] | None" = None):
        self._markers = dict(DEFAULT_ACCRETION_MARKERS)
        if markers is not None:
            self.set_markers(markers)
        self.reset()

    @property
    def ids(self) -> frozenset:
        return ACCRETION_IDS

    def set_markers(self, markers: "dict[str, str]") -> None:
        for key in DEFAULT_ACCRETION_MARKERS:
            marker = markers.get(key)
            if isinstance(marker, str):
                self._markers[key] = marker

    def reset(self) -> None:
        self._players: "dict[str, dict]" = {}
        self._members: "tuple[str, ...]" = ()
        self._holder: "str | None" = None
        self._marked: "str | None" = None
        self._invalid = False
        self._last_event = 0.0

    def on_gain(self, effect_hex: str, actor_id: str, now: float) -> "list[tuple]":
        effect = _norm_id(effect_hex)
        actor = str(actor_id).strip().upper()
        if effect not in self.ids or not actor:
            return []
        actions = self._expire(now)
        if self._members and self._holder is None and now - self._last_event > BURST_GAP_S:
            self.reset()
        self._last_event = now
        player = self._players.setdefault(actor, {"order": None, "accretion": False,
                                                   "completed": False})
        if effect == ACCRETION:
            player["accretion"] = True
        elif effect in ORDER_IDS:
            player["order"] = ORDER_IDS[effect]
        return actions + self._advance()

    def on_loss(self, effect_hex: str, actor_id: str, now: float) -> "list[tuple]":
        effect = _norm_id(effect_hex)
        actor = str(actor_id).strip().upper()
        if effect not in self.ids or not actor:
            return []
        actions = self._expire(now)
        player = self._players.get(actor)
        if player is None:
            return actions
        self._last_event = now
        if effect == CRUST:
            player["completed"] = True
            actions += self._advance()
        return actions

    def flush(self, now: float) -> "list[tuple]":
        return self._expire(now) + self._advance()

    def needs_flush(self) -> bool:
        return bool(self._players) and (not self._members or self._holder is not None)

    def outstanding(self) -> "list[str]":
        return [self._marked] if self._marked is not None else []

    def _expire(self, now: float) -> "list[tuple]":
        if self._players and now - self._last_event > STALE_S:
            actions = [("clear", actor) for actor in self.outstanding()]
            self.reset()
            return actions
        return []

    def _advance(self) -> "list[tuple]":
        carriers = [actor for actor, player in self._players.items() if player["accretion"]]
        ordered = tuple(sorted(carriers, key=lambda actor: (
            self._players[actor]["order"] or 99, actor)))
        valid = (len(carriers) == 2
                 and [self._players[actor]["order"] for actor in ordered] == [1, 2])
        if len(carriers) > 2 or (self._members and (not valid or ordered != self._members)):
            self._invalid = True
        if self._invalid:
            actions = [("clear", actor) for actor in self.outstanding()]
            self._holder = self._marked = None
            return actions
        if not self._members:
            if not valid:
                return []
            self._members = ordered
        holder = next((actor for actor in self._members
                       if not self._players[actor]["completed"]), None)
        if holder == self._holder:
            return []
        actions = [("clear", actor) for actor in self.outstanding()]
        self._holder = holder
        self._marked = None
        if holder is not None:
            key = "first" if self._players[holder]["order"] == 1 else "second"
            marker = self._markers[key]
            if marker:
                self._marked = holder
                actions.append(("mark", holder, marker))
        return actions


# Neo Exdeath's status VFX identifies real and fake Grand Cross debuffs.
CURSED_SHRIEK = "15A7"
GAZE_IDS = frozenset({CURSED_SHRIEK})
GAZE_VFX_STATUS = "808"
FAKE_GAZE_VFX = "461"
REAL_GAZE_VFX = "462"
GAZE_TELL_S = 12.0
GAZE_PER_SET = 2
# Only the first two Grand Cross waves have gaze pairs.
GAZE_SETS = 2

# Real gaze pairs look away from each other. Fake gaze pairs look at each other.
AWAY1, AWAY2, LOOK1, LOOK2 = "away1", "away2", "look1", "look2"
_GAZE_KEYS = (AWAY1, AWAY2, LOOK1, LOOK2)
DEFAULT_GAZE_MARKERS = {AWAY1: "ignore1", AWAY2: "ignore2",
                        LOOK1: "bind1", LOOK2: "bind2"}


def _id_int(actor_id) -> int:
    s = str(actor_id).strip()
    if s[:2].lower() == "0x":
        s = s[2:]
    try:
        return int(s, 16)
    except ValueError:
        return 0


class CursedShriekPairs:
    """Pair real or fake gazes using Neo Exdeath status VFX. Retain shared signs until gaze loss."""

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
        for key in _GAZE_KEYS:
            tok = markers.get(key)
            if tok:
                self._markers[key] = tok

    def reset(self) -> None:
        self._polarity: "str | None" = None
        self._polarity_t = 0.0                 # when the armed tell landed
        self._last_vfx_event = None
        self._set: "list[str]" = []            # the open set's carriers
        self._set_t = 0.0                      # first gain time of the open set
        self._sets_done = 0
        self._assigned: "dict[str, str]" = {}
        self._marked: "dict[str, str]" = {}
        self._active_until: "dict[str, float]" = {}
        self._last_event = 0.0

    def on_vfx(self, vfx_hex: str, now: float, event_id=None) -> "list[tuple]":
        """Arm one pair from the status loop VFX carried by Neo Exdeath."""
        vfx = _norm_id(vfx_hex)
        if vfx == FAKE_GAZE_VFX:
            kind = LOOK1
        elif vfx == REAL_GAZE_VFX:
            kind = AWAY1
        else:
            return []
        actions = self.flush(now)
        if self._sets_done >= GAZE_SETS:
            return actions
        event = (vfx, now if event_id is None else event_id)
        if event == self._last_vfx_event:
            return actions
        self._last_vfx_event = event
        self._last_event = now
        self._polarity = kind
        self._polarity_t = now
        return actions + self._assign_pair(now)

    def on_gain(self, effect_hex: str, actor_id: str, duration, now: float) -> "list[tuple]":
        """Handle a gaze gain. Duration bounds repeats but does not identify gaze type."""
        if _norm_id(effect_hex) not in self._ids:
            return []
        actor_id = str(actor_id).strip().upper()
        actions = self.flush(now)
        if self._polarity is not None and now - self._polarity_t > GAZE_TELL_S:
            self._polarity = None
        if self._sets_done >= GAZE_SETS or actor_id in self._assigned:
            return actions
        self._last_event = now
        if actor_id in self._set or len(self._set) >= GAZE_PER_SET:
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
        return actions + self._assign_pair(now)

    def _assign_pair(self, now: float) -> "list[tuple]":
        if len(self._set) != GAZE_PER_SET or self._polarity is None:
            return []
        polarity, self._polarity = self._polarity, None
        self._sets_done += 1
        keys = (LOOK1, LOOK2) if polarity == LOOK1 else (AWAY1, AWAY2)
        pair = self._ordered(self._set)
        self._set = []
        for actor, key in zip(pair, keys):
            self._assigned[actor] = key
        return self._mark_available(now)

    def _mark_available(self, now: float) -> "list[tuple]":
        actions = []
        occupied = set(self._marked.values())
        for actor, key in self._assigned.items():
            marker = self._markers[key]
            if (actor in self._marked or marker in occupied
                    or now >= self._active_until.get(actor, math.inf)):
                continue
            self._marked[actor] = marker
            occupied.add(marker)
            actions.append(("mark", actor, marker))
        return actions

    def on_loss(self, effect_hex: str, actor_id: str, now: float) -> "list[tuple]":
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
            # Remove incomplete carriers without discarding the next wave's tell.
            self._set.remove(actor_id)
            self._active_until.pop(actor_id, None)
            if not self._set and self._polarity_t <= self._set_t:
                self._polarity = None
        if actor_id in self._assigned:
            self._assigned.pop(actor_id)
            self._active_until.pop(actor_id, None)
            actions = [("clear", actor_id)] if self._marked.pop(actor_id, None) else []
            return actions + self._mark_available(now)
        return []

    def flush(self, now: float) -> "list[tuple]":
        if self._live() and now - self._last_event > STALE_S:
            actions = [("clear", a) for a in self.outstanding()]
            self.reset()
            return actions
        if self._set and now - self._set_t > BURST_GAP_S:
            for actor in self._set:
                self._active_until.pop(actor, None)
            self._set = []
            if self._polarity_t <= self._set_t:
                self._polarity = None
        actions = []
        for actor, expires in list(self._active_until.items()):
            if now >= expires:
                self._active_until.pop(actor)
                if actor in self._set:
                    self._set.remove(actor)
                    if not self._set and self._polarity_t <= self._set_t:
                        self._polarity = None
                self._assigned.pop(actor, None)
                if self._marked.pop(actor, None):
                    actions.append(("clear", actor))
        return actions + self._mark_available(now)

    def needs_flush(self) -> bool:
        return bool(self._assigned or self._set)

    def outstanding(self) -> "list[str]":
        return sorted(self._marked, key=_id_int)

    def expires_at(self, actor_id: str) -> float | None:
        return self._active_until.get(str(actor_id).strip().upper())

    def _live(self) -> bool:
        return bool(self._polarity is not None or self._set or self._assigned or self._sets_done)

    def _ordered(self, actors: "list[str]") -> "list[str]":
        def key(a):
            slot = self._slot_of(a)
            return (0, slot, "") if slot is not None else (1, _id_int(a), a)
        return sorted(actors, key=key)


FORKED_LIGHTNING = "15A8"
ACCELERATION_BOMB = "15AA"


class GrandCrossPairs:
    """Assign a complete Grand Cross wave from Neo Exdeath's real or fake tell."""

    def __init__(self, status_id: str, carriers_per_wave: int = 2,
                 markers: "dict[str, str] | None" = None, slot_of=None):
        if carriers_per_wave not in (2, 4):
            raise ValueError("Grand Cross waves have two or four carriers")
        self._ids = frozenset({_norm_id(status_id)})
        self._carriers_per_wave = carriers_per_wave
        self._slot_of = slot_of or (lambda _actor: None)
        self._keys = tuple(f"{kind}{index}"
                           for kind in ("real", "fake")
                           for index in range(1, carriers_per_wave + 1))
        self._markers = {key: f"attack{index}"
                         for index, key in enumerate(self._keys, 1)}
        if markers is not None:
            self.set_markers(markers)
        self.reset()

    @property
    def ids(self) -> frozenset:
        return self._ids

    def set_markers(self, markers: "dict[str, str]") -> None:
        for key in self._keys:
            marker = markers.get(key)
            if isinstance(marker, str):
                self._markers[key] = marker

    def reset(self) -> None:
        self._polarity: "str | None" = None
        self._polarity_t = 0.0
        self._seen_vfx_events = set()
        self._set: "dict[str, float]" = {}
        self._set_t = 0.0
        self._sets_done = 0
        self._assigned: "dict[str, str]" = {}
        self._marked: "dict[str, str]" = {}
        self._active_until: "dict[str, float]" = {}
        self._last_event = 0.0

    def on_vfx(self, vfx_hex: str, now: float, event_id=None) -> "list[tuple]":
        vfx = _norm_id(vfx_hex)
        if vfx not in (FAKE_GAZE_VFX, REAL_GAZE_VFX):
            return []
        actions = self.flush(now)
        event = (vfx, now if event_id is None else event_id)
        if self._sets_done >= GAZE_SETS or event in self._seen_vfx_events:
            return actions
        self._seen_vfx_events.add(event)
        self._last_event = now
        self._polarity = "fake" if vfx == FAKE_GAZE_VFX else "real"
        self._polarity_t = now
        return actions + self._assign_wave(now)

    def on_gain(self, effect_hex: str, actor_id: str, duration,
                now: float) -> "list[tuple]":
        if _norm_id(effect_hex) not in self._ids:
            return []
        actions = self.flush(now)
        actor = str(actor_id).strip().upper()
        if (not actor or self._sets_done >= GAZE_SETS or actor in self._assigned
                or actor in self._set or len(self._set) >= self._carriers_per_wave):
            return actions
        try:
            remaining = float(duration)
        except (TypeError, ValueError, OverflowError):
            return actions
        if not math.isfinite(remaining) or remaining <= 0:
            return actions
        if not self._set:
            self._set_t = now
        self._last_event = now
        self._set[actor] = remaining
        self._active_until[actor] = now + min(remaining, STALE_S)
        return actions + self._assign_wave(now)

    def _assign_wave(self, now: float) -> "list[tuple]":
        if len(self._set) != self._carriers_per_wave or self._polarity is None:
            return []
        polarity, self._polarity = self._polarity, None
        if self._carriers_per_wave == 4:
            by_duration = sorted(self._set, key=self._set.get)
            actors = (sorted(by_duration[:2], key=self._carrier_key)
                      + sorted(by_duration[2:], key=self._carrier_key))
        else:
            actors = sorted(self._set, key=self._carrier_key)
        self._sets_done += 1
        for index, actor in enumerate(actors, 1):
            self._assigned[actor] = f"{polarity}{index}"
        self._set.clear()
        return self._mark_available(now)

    def _carrier_key(self, actor: str) -> tuple:
        slot = self._slot_of(actor)
        party_key = (0, slot, _id_int(actor), actor) if slot is not None else (
            1, 0, _id_int(actor), actor)
        return party_key

    def _mark_available(self, now: float) -> "list[tuple]":
        actions = []
        occupied = set(self._marked.values())
        for actor, key in self._assigned.items():
            marker = self._markers[key]
            if (not marker or actor in self._marked or marker in occupied
                    or now >= self._active_until[actor]):
                continue
            self._marked[actor] = marker
            occupied.add(marker)
            actions.append(("mark", actor, marker))
        return actions

    def on_loss(self, effect_hex: str, actor_id: str, now: float) -> "list[tuple]":
        if _norm_id(effect_hex) not in self._ids:
            return []
        actor = str(actor_id).strip().upper()
        if self._live() and now - self._last_event > STALE_S:
            return self.flush(now)
        if actor not in self._set and actor not in self._assigned:
            return self.flush(now)
        actions = []
        self._last_event = now
        if actor in self._set:
            self._set.pop(actor)
            if not self._set and self._polarity_t <= self._set_t:
                self._polarity = None
        self._assigned.pop(actor, None)
        self._active_until.pop(actor, None)
        if self._marked.pop(actor, None):
            actions.append(("clear", actor))
        return actions + self.flush(now)

    def flush(self, now: float) -> "list[tuple]":
        if self._live() and now - self._last_event > STALE_S:
            actions = [("clear", actor) for actor in self.outstanding()]
            self.reset()
            return actions
        if self._polarity is not None and now - self._polarity_t > GAZE_TELL_S:
            self._polarity = None
        if self._set and now - self._set_t > BURST_GAP_S:
            for actor in self._set:
                self._active_until.pop(actor, None)
            self._set.clear()
            if self._polarity_t <= self._set_t:
                self._polarity = None
        actions = []
        for actor, expires in list(self._active_until.items()):
            if now >= expires:
                self._active_until.pop(actor)
                if actor in self._set:
                    self._set.pop(actor)
                    if not self._set and self._polarity_t <= self._set_t:
                        self._polarity = None
                self._assigned.pop(actor, None)
                if self._marked.pop(actor, None):
                    actions.append(("clear", actor))
        return actions + self._mark_available(now)

    def needs_flush(self) -> bool:
        return bool(self._assigned or self._set)

    def outstanding(self) -> "list[str]":
        return sorted(self._marked, key=_id_int)

    def expires_at(self, actor_id: str) -> float | None:
        return self._active_until.get(str(actor_id).strip().upper())

    def _live(self) -> bool:
        return bool(self._polarity is not None or self._set or self._assigned
                    or self._sets_done)
