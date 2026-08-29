"""Automarker engines for UMAD, Dancing Mad Ultimate, status mechanics.

BlackHoleChains sequences the P3 black hole cleanse. All 8 players gain
Primordial Crust, 154E, plus First/Second/Third in Line, BBC/BBD/BBE. One DPS
and one healer also gain Accretion, 644, forming three queues, DPS, supports
and the Accretion pair. One roaming sign per queue sits on the earliest in-Line
member still holding Crust and jumps on each cleanse. Fail closed throughout,
a queue with unknown roles, membership or order stays silent.

CursedShriekPairs marks the P4 Kefka Says gazes. Two Grand Cross waves each
deal a pair of Cursed Shriek 15A7, and each wave's Inferno or Tsunami followup
cast tells whether its pair is the fake look-at gaze or the real look-away
one. Fake gets the look-at signs, real the look-away signs.

StatusPairs backs the compound automark rules, "A+B". All are pure state
machines, no Qt, no I/O, host passes time.monotonic. MainWindow wires them to
the ACT 20/26/30 lines and Telesto. Per id evidence in docs/UMAD-DEBUFFS.md.
"""

from __future__ import annotations

# Status ids in normalized hex, upper, no 0x, no leading zeros, same shape as
# MainWindow._norm_hex. _norm_id re-normalizes at the boundary.
ACCRETION = "644"
CRUST = "154E"   # DMU 7.5x Primordial Crust, verified live. NOT TOP's 645.
ORDER_IDS = {"BBC": 1, "BBD": 2, "BBE": 3}   # First/Second/Third in Line
#: Every status id the host needs to route to this engine, on 26 and 30 lines.
RELEVANT_IDS = frozenset(ORDER_IDS) | {ACCRETION, CRUST}

# Queue keys.
DPS, SUPPORT, ACC = "dps", "support", "accretion"
_EXPECTED = {DPS: 3, SUPPORT: 3, ACC: 2}
DEFAULT_MARKERS = {DPS: "attack1", SUPPORT: "attack2", ACC: "attack3"}

# A burst this long after the last event is a new mechanic instance, second
# black hole, next pull after a missed wipe. Reset instead of gluing onto
# stale state.
STALE_S = 90.0

# Within one burst every line lands inside a couple of seconds, one cast
# applies all the debuffs. A gain this long after the last event, while no
# queue ever started and nobody holds Crust, is the next pull's burst, so
# reset instead of gluing onto a dead instance's leftover player data.
BURST_GAP_S = 5.0

# ClassJob ids -> coarse role, closed-world through patch 7.5x, base classes
# included. Ids outside these sets, including future jobs, resolve to None so
# the role queues fail closed rather than mis-bucket a new tank/healer as DPS.
# Extend when new jobs ship.
_TANK_JOBS = {1, 3, 19, 21, 32, 37}                    # GLA MRD PLD WAR DRK GNB
_HEALER_JOBS = {6, 24, 28, 33, 40}                     # CNJ WHM SCH AST SGE
_DPS_JOBS = {2, 4, 5, 7, 20, 22, 23, 25, 26, 27, 29,   # PGL LNC ARC THM MNK DRG
             30, 31, 34, 35, 36, 38, 39, 41, 42}       # BRD BLM ACN SMN ROG NIN
                                                       # MCH SAM RDM BLU DNC RPR
                                                       # VPR PCT


def role_for_job(job: "int | None") -> "str | None":
    """Coarse role for a ClassJob id. 'support' for tanks and healers, 'dps',
    or None for unknown ids so callers fail closed."""
    if not job:
        return None
    if job in _TANK_JOBS or job in _HEALER_JOBS:
        return SUPPORT
    if job in _DPS_JOBS:
        return DPS
    return None


def _norm_id(effect_hex: str) -> str:
    """Normalize a status id, upper, no 0x, no leading zeros, matching
    MainWindow._norm_hex. Kept local so this module stays dependency-free."""
    s = str(effect_hex).strip().upper()
    if s.startswith("0X"):
        s = s[2:]
    return s.lstrip("0") or "0"


_HEX_DIGITS = frozenset("0123456789ABCDEF")


def parse_compound(status: str) -> "tuple[str, str] | None":
    """Parse a compound automark token "A+B", both statuses on the same player,
    e.g. "644+BBC", into two normalized ids, else None. Both parts must be pure
    hex so an exact-name rule containing '+' falls through to the name-match
    path instead of becoming a dead compound."""
    if "+" not in status:
        return None
    parts = tuple(_norm_id(p) for p in status.split("+") if p.strip())
    if len(parts) != 2 or not all(p and set(p) <= _HEX_DIGITS for p in parts):
        return None
    return parts   # type: ignore[return-value]


def canon_status_key(status: str) -> str:
    """Canonical identity key for an automark status token. Part-wise
    normalization plus stable part order, so "644+0bbc", "0x644+BBC" and
    "BBC+644" are one identity. Preset sync, the rules-list label lookup, and
    compound cooldown keys must all use this."""
    pair = parse_compound(status)
    return "+".join(sorted(pair)) if pair else _norm_id(status)


class StatusPairs:
    """Which of a small tracked set of status ids each actor holds right now.

    Backs compound rules like "A+B". The pair arrives as separate 26 lines in
    either order, so a single-line matcher can't see both. Feed every gain and
    loss of a tracked id. Ask holds_all at match time. Pure, callers pass
    monotonic seconds. Reset per zone/wipe. Entries older than stale_s count
    as gone, so a LosesEffect missed over a reconnect can't leave a phantom
    status that marks the wrong player later."""

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
    """State machine for one instance of the black-hole cleanse queues.

    on_gain/on_loss/flush return action tuples.
      "mark", actor_id, marker_token   - place the sign on this player
      "clear", actor_id                - remove the sign from this player
    Actor ids are the raw hex strings from the log lines, upper-cased. A queue
    is "started" once keyed in _holder. None means it walked to its end."""

    def __init__(self, role_of, markers: "dict[str, str] | None" = None):
        self._role_of = role_of           # actor_id -> 'dps' | 'support' | None
        self._markers = dict(DEFAULT_MARKERS)
        if markers:
            self.set_markers(markers)
        self.reset()

    def set_markers(self, markers: "dict[str, str]") -> None:
        """Update per-queue marker tokens, a queue key to token map. Unknown
        keys ignored, missing ones keep their current token."""
        for queue in (DPS, SUPPORT, ACC):
            tok = markers.get(queue)
            if tok:
                self._markers[queue] = tok

    def reset(self) -> None:
        """Forget the current mechanic instance, zone change, wipe, new pull."""
        self._players: "dict[str, dict]" = {}    # actor -> {order, accretion, crust}
        self._holder: "dict[str, str | None]" = {}   # started queue -> current holder
        self._last_event = 0.0

    # -- event feed -----------------------------------------------------------
    def on_gain(self, effect_hex: str, actor_id: str, now: float) -> "list[tuple]":
        """A 26/GainsEffect for one of RELEVANT_IDS landed on a player."""
        effect_hex = _norm_id(effect_hex)
        if effect_hex not in RELEVANT_IDS:
            return []
        actions: "list[tuple]" = []
        if self._players and (
                now - self._last_event > STALE_S
                # ...or the previous instance fully resolved. Every started
                # queue walked to its end and nobody holds Crust. The crust
                # check keeps a partial instance alive for players still
                # waiting on tethers.
                or (self._holder
                    and all(v is None for v in self._holder.values())
                    and not any(p["crust"] for p in self._players.values()))
                # ...or it never started a queue at all and went quiet, first
                # pull wiped before the mechanic, next pull inside the stale
                # window. The gap guard keeps a live burst from resetting on
                # its own second line, nobody holds Crust that early either.
                or (not self._holder
                    and now - self._last_event > BURST_GAP_S
                    and not any(p["crust"] for p in self._players.values()))
                # ...or every started queue stalled on a missed Crust loss and
                # the mechanic went quiet. The only Crust left sits on the
                # queue heads themselves so nothing is still walking and the
                # old signs are stranded. The gap guard keeps a live
                # mid-cleanse instance from resetting on a late duplicate.
                or (self._holder
                    and now - self._last_event > BURST_GAP_S
                    and all(not p["crust"] or aid in self._holder.values()
                            for aid, p in self._players.items()))):
            # Clear the signs this instance still has up before dropping its
            # state, the way _reseat_accretion_head does, or they sit on the
            # players for the rest of the pull.
            actions += [("clear", a) for a in self.outstanding()]
            self.reset()
        self._last_event = now
        p = self._players.setdefault(actor_id.upper(),
                                     {"order": None, "accretion": False, "crust": False})
        if effect_hex == ACCRETION:
            p["accretion"] = True
            # Late 644. The Accretion queue's membership changed, so re-derive
            # its head and move the sign if a better holder emerged. Harmless
            # when the queue hasn't started.
            actions += self._reseat_accretion_head()
        elif effect_hex == CRUST:
            p["crust"] = True
        else:
            p["order"] = ORDER_IDS[effect_hex]
        # Fast path, mark any queue that just became provably complete.
        return actions + self._start_ready_queues(require_complete=True)

    def on_loss(self, effect_hex: str, actor_id: str, now: float) -> "list[tuple]":
        """A 30/LosesEffect. Only Crust removals advance the queues. Losing
        Accretion or an order debuff changes nothing."""
        if _norm_id(effect_hex) != CRUST:
            return []
        # Mirror on_gain's staleness reset. A Crust loss arriving ages after the
        # last event, reconnect or a replayed log, belongs to a dead instance and
        # must not walk a sign off that stale _players/_holder snapshot.
        if self._players and now - self._last_event > STALE_S:
            # Same rescue as on_gain, signs the dead instance still has up come
            # down before its state is forgotten.
            actions = [("clear", a) for a in self.outstanding()]
            self.reset()
            return actions
        actor_id = actor_id.upper()
        p = self._players.get(actor_id)
        if p is None:
            return []
        self._last_event = now
        p["crust"] = False
        # If they carried a queue's sign, pass it down, or clear it off them
        # when they were the last. An actor can head two queues after an
        # accretion reseat plus a late 644, so service every queue they head,
        # not just the first.
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
        """Debounced best-effort start for queues the fast path never marked.
        A member who died before assignment never appears, so the queue runs
        short-handed. Still fail-closed. Role queues wait for known roles and
        both 644s, and no queue starts while a crusted member's order is
        unknown, a guessed head walks the sign one player behind all mechanic."""
        # Mirror on_loss's staleness reset. A debounce firing ages after the
        # last event would mark off a dead instance's snapshot, and refreshing
        # _last_event here would glue the next real event onto it.
        if self._players and now - self._last_event > STALE_S:
            # Same rescue as on_gain, signs the dead instance still has up come
            # down before its state is forgotten.
            actions = [("clear", a) for a in self.outstanding()]
            self.reset()
            return actions
        self._last_event = now
        return self._start_ready_queues(require_complete=False)

    def outstanding(self) -> "list[str]":
        """Actors currently holding a queue sign, so an abort, wipe, toggle-off
        or a stale-instance reset can clear the marks it placed before the queue
        walked itself out. Stable order, dps, support, accretion, keeps the
        emitted clears deterministic."""
        held: "list[str]" = []
        for queue in (DPS, SUPPORT, ACC):
            actor = self._holder.get(queue)
            if actor and actor not in held:
                held.append(actor)
        return held

    def has_open_queues(self) -> bool:
        """True while a live instance could still start a queue, players are
        recorded and at least one queue has not started. The host re-arms its
        debounce flush off this when late job info lands, since roles the last
        flush needed may have just arrived. A loose yes costs one no-op flush."""
        return bool(self._players) and len(self._holder) < len(_EXPECTED)

    # -- internals ------------------------------------------------------------
    def _queues(self) -> "dict[str, list[str]]":
        """Queue membership sorted by in-Line order. Unknown order sorts last,
        actor id breaks ties. Unresolvable roles are dropped from the role
        queues."""
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
        """True when every member still holding Crust has a known in-Line
        order. Only then is "earliest in line" a fact, not a guess."""
        return all(self._players[a]["order"] is not None
                   for a in members if self._players[a]["crust"])

    def _reseat_accretion_head(self) -> "list[tuple]":
        """Recompute the Accretion head after a late 644. Move the sign if the
        newcomer belongs in front of the current holder, clear included, or
        the old holder keeps a sign nothing will ever remove."""
        holder = self._holder.get(ACC)
        if holder is None:              # not started, or already walked out
            return []
        members = self._queues()[ACC]
        if not self._order_known_for_crusted(members):
            return []                   # don't reseat onto a guessed order
        head = self._first_with_crust(members)
        if head is None or head == holder:
            return []
        self._holder[ACC] = head
        return [("clear", holder), ("mark", head, self._markers[ACC])]

    def _start_ready_queues(self, require_complete: bool) -> "list[tuple]":
        actions: "list[tuple]" = []
        queues = self._queues()
        unknown = self._unknown_role_count()
        # Role queues are only trustworthy once BOTH 644 players are known. An
        # Accretion player's order/Crust lines can land before their 644, and
        # until then they sit in a role queue, which could hit its expected
        # size with the wrong member. If the second 644 never arrives the role
        # queues stay silent all mechanic.
        acc_settled = len(queues[ACC]) >= 2
        for queue, members in queues.items():
            if queue in self._holder or not members:
                continue                # already started, walked or walking
            if queue != ACC and not acc_settled:
                continue    # an unflagged Accretion player may be hiding in here
            if not self._order_known_for_crusted(members):
                continue    # a crusted member's order is unknown, the head would be a guess
            if require_complete:
                # Provably complete, expected size, everyone ordered and crusted.
                if len(members) != _EXPECTED[queue]:
                    continue
                if any(self._players[a]["order"] is None or not self._players[a]["crust"]
                       for a in members):
                    continue
            elif queue != ACC and unknown:
                continue    # dps/support membership untrusted yet, stay silent
            first = self._first_with_crust(members)
            if first is None:
                continue    # everyone already cleansed, nothing to mark
            self._holder[queue] = first
            actions.append(("mark", first, self._markers[queue]))
        return actions


# ── Cursed Shriek gaze pairing, P4 ───────────────────────────────────────────
# Fight confirmed from IINACT logs, 2026-08-20, 08-23 and 08-25. Two sets of
# two Cursed Shriek 15A7 carriers, one set per Grand Cross wave 15s apart.
# Set 1 reads a 60.00s timer, set 2 reads 69.00s, and both gains of a set
# share one timestamp. Both carriers of a set are the same kind, real or fake
# look-at, and which kind rides which set swaps per pull. The tell is the
# wave's fire or water followup cast, its 20 cast line starts ~4s before the
# shriek gains land. On the labeled pull, 2026-08-25 23:05, Inferno rode the
# fake set and Tsunami the real one. See docs/UMAD-DEBUFFS.md.
CURSED_SHRIEK = "15A7"
#: Status ids that count as a Cursed Shriek gaze.
GAZE_IDS = frozenset({CURSED_SHRIEK})
#: Followup cast ids whose wave's gaze set is the fake, look at, gaze.
FAKE_FOLLOWUP_IDS = frozenset({"BB1E", "BB20"})    # Inferno, fire
#: Followup cast ids whose wave's gaze set is the real, look away, gaze.
REAL_FOLLOWUP_IDS = frozenset({"BB1F", "BB21"})    # Tsunami, water
#: Every followup cast id the host routes here on 20 lines.
GAZE_FOLLOWUP_IDS = FAKE_FOLLOWUP_IDS | REAL_FOLLOWUP_IDS
#: Carriers in one gaze set, one support and one DPS per wave.
GAZE_PER_SET = 2
#: Gaze sets in one phase, one per Grand Cross wave, the third wave has none.
GAZE_SETS = 2

# Marker slots. The "away" pair look away from each other, real gaze, the "look"
# pair look at each other, fake/reversed gaze. Defaults follow the strat's
# terms. The ignore signs for look-away, the bind/"chain" signs for look-at.
AWAY1, AWAY2, LOOK1, LOOK2 = "away1", "away2", "look1", "look2"
_GAZE_KEYS = (AWAY1, AWAY2, LOOK1, LOOK2)
DEFAULT_GAZE_MARKERS = {AWAY1: "ignore1", AWAY2: "ignore2",
                        LOOK1: "bind1", LOOK2: "bind2"}


def _id_int(actor_id) -> int:
    """Parse an actor id to an int for the 1/2 ordering fallback. Local hex/dec
    parse so this module stays dependency-free, no telesto_client import."""
    s = str(actor_id).strip()
    if s[:2].lower() == "0x":
        s = s[2:]
    try:
        return int(s, 16)
    except ValueError:
        return 0


class CursedShriekPairs:
    """State machine for one UMAD P4 Cursed Shriek gaze phase.

    Each of the first two Grand Cross waves deals one pair of 15A7 gazes.
    Both carriers of a set are the same kind, and the kind swaps per pull,
    so arrival order or timer alone can't pick the signs. The kind is told
    by the wave's followup cast fed to on_followup. Inferno arms the fake
    kind for the next set, Tsunami the real kind, and the cast line starts
    about 4s before the gains land. The fake set gets the look-at signs, the
    real set the look-away signs, each pair numbered 1/2 by party slot
    falling back to actor id so a strat can pin who stands where.

    Fail-closed throughout, a mis-aimed gaze in an Ultimate is worse than an
    unmarked one. A set whose followup never arrived marks nothing, an
    incomplete set marks nothing, and stray gains between waves pair into a
    set with no tell of their own and mark nothing.
    on_followup/on_gain/on_loss return the same action tuples as
    BlackHoleChains. Pure. The host passes monotonic time and an optional
    slot_of callback."""

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
        """Update the four sign tokens, a slot key to token map. Unknown
        keys ignored, missing ones keep their current token."""
        for key in _GAZE_KEYS:
            tok = markers.get(key)
            if tok:
                self._markers[key] = tok

    def reset(self) -> None:
        """Forget the current phase, zone change, wipe, new pull."""
        self._polarity: "str | None" = None    # armed by on_followup, per set
        self._polarity_t = 0.0                 # when the armed tell landed
        self._set: "list[str]" = []            # the open set's carriers
        self._set_t = 0.0                      # first gain time of the open set
        self._sets_done = 0                    # closed sets, assigned or not
        self._assigned: "dict[str, str]" = {}  # actor -> slot key while marked
        self._last_event = 0.0

    # -- event feed -----------------------------------------------------------
    def on_followup(self, effect_hex: str, now: float) -> "list[tuple]":
        """A 20 StartsCast for a wave's fire or water followup. Inferno arms
        the fake kind for the next gaze set, Tsunami the real kind. Returns
        no actions, the marks come with the gains."""
        eff = _norm_id(effect_hex)
        if eff in FAKE_FOLLOWUP_IDS:
            kind = LOOK1
        elif eff in REAL_FOLLOWUP_IDS:
            kind = AWAY1
        else:
            return []
        actions: "list[tuple]" = []
        if self._live() and now - self._last_event > STALE_S:
            # A followup ages after the last event belongs to a dead phase.
            # Drop its state before arming, or a stale polarity could mark
            # the next pull's first set the wrong way. Signs still up come
            # down too, reset alone would strand them.
            actions += [("clear", a) for a in self.outstanding()]
            self.reset()
        self._last_event = now
        self._polarity = kind
        self._polarity_t = now
        return actions

    def on_gain(self, effect_hex: str, actor_id: str, duration, now: float) -> "list[tuple]":
        """A 26/GainsEffect for a gaze status. `duration` is the timer, 26
        field 4, kept for signature stability. It reads 60s on set 1 and 69s
        on set 2 but carries no real or fake meaning, the followup cast does."""
        if _norm_id(effect_hex) not in self._ids:
            return []
        actor_id = str(actor_id).strip().upper()
        actions: "list[tuple]" = []
        if self._live() and now - self._last_event > STALE_S:
            # Signs a dead phase still has up come down first, the state
            # reset alone would leave them on the players for the rest of
            # the pull.
            actions += [("clear", a) for a in self.outstanding()]
            self.reset()
        elif self._sets_done >= GAZE_SETS:
            # Every set of the phase is closed. A gain now, even for a
            # carrier the old phase marked, is the next pull gluing on with
            # its 30 lines missed, so come down clean and start fresh rather
            # than point stale signs at a new deal. A polarity armed moments
            # ago belongs to this very wave, its followup already landed, so
            # keep it armed across the reset.
            actions += [("clear", a) for a in self.outstanding()]
            armed = self._polarity if now - self._last_event <= STALE_S else None
            armed_t = self._polarity_t if armed is not None else 0.0
            self.reset()
            self._polarity = armed
            self._polarity_t = armed_t
        elif actor_id in self._assigned:
            self._last_event = now
            return actions   # refresh of a live carrier, keep the sign
        self._last_event = now
        # An open set that went quiet never completed, its partner 26 never
        # came. Discard it so the leftovers can't pair with the next wave.
        # A tell armed after the set opened belongs to the wave whose gains
        # are arriving now, only the dead wave's own tell dies with it.
        if self._set and now - self._set_t > BURST_GAP_S:
            self._set = []
            if self._polarity_t <= self._set_t:
                self._polarity = None
        if actor_id in self._set:
            return actions       # duplicate of the open set, no-op
        if not self._set:
            self._set_t = now
        self._set.append(actor_id)
        if len(self._set) < GAZE_PER_SET:
            return actions       # waiting on the partner gain
        # The set is complete. Without a polarity there is nothing to mark
        # with, fail closed and let the burst gap discard take the leftovers.
        polarity, self._polarity = self._polarity, None
        self._sets_done += 1
        if polarity is None:
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
        """A 30/LosesEffect. The gaze resolved on this player. Clear its sign so
        the field is clean for the next mechanic."""
        if _norm_id(effect_hex) not in self._ids:
            return []
        actor_id = str(actor_id).strip().upper()
        # Mirror on_gain's staleness reset. A loss arriving ages after the last
        # event, reconnect or a replayed log, belongs to a dead phase and must
        # not refresh _last_event, or the next gain glues onto it.
        if self._live() and now - self._last_event > STALE_S:
            # Signs the dead phase still has up come down before its state
            # is forgotten.
            actions = [("clear", a) for a in self.outstanding()]
            self.reset()
            return actions
        self._last_event = now
        if actor_id in self._set:
            # They lost it before the set completed. An incomplete set marks
            # nothing, drop them and let the burst gap discard the rest. A
            # tell armed after the set opened is the next wave's, keep it.
            self._set.remove(actor_id)
            if not self._set and self._polarity_t <= self._set_t:
                self._polarity = None
        if actor_id in self._assigned:
            self._assigned.pop(actor_id)
            return [("clear", actor_id)]
        return []

    def flush(self, now: float) -> "list[tuple]":
        """Debounce backstop. Assignment happens on the completing gain, so
        this only sweeps dead state, an open set whose partner gain never
        came inside the burst window. Clears signs when a stale phase is
        found, the same defect class as the chain engine's flush."""
        if self._live() and now - self._last_event > STALE_S:
            actions = [("clear", a) for a in self.outstanding()]
            self.reset()
            return actions
        self._last_event = now
        if self._set and now - self._set_t > BURST_GAP_S:
            self._set = []
            if self._polarity_t <= self._set_t:
                self._polarity = None
        return []

    def outstanding(self) -> "list[str]":
        """Actors currently wearing a gaze sign, so an abort, wipe, toggle-off
        or a stale-phase reset can clear what it placed. Deterministic
        order."""
        return sorted(self._assigned, key=_id_int)

    def _live(self) -> bool:
        return bool(self._set or self._assigned or self._sets_done)

    # -- internals ------------------------------------------------------------
    def _ordered(self, actors: "list[str]") -> "list[str]":
        """Stable 1/2 order within a pair. Party slot if known, else actor int.
        The lower sorts to the '1' sign."""
        def key(a):
            slot = self._slot_of(a)
            # `is not None`, not truthiness. A 0-based host's slot 0 is real.
            return (0, slot, "") if slot is not None else (1, _id_int(a), a)
        return sorted(actors, key=key)
