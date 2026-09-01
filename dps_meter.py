"""ACT-style DPS meter, parsed straight from the combat log feed.

The IINACT/OverlayPlugin feed already delivers every FFXIV network log line,
01 ChangeZone, 02 ChangePrimaryPlayer, 03 AddCombatant, 21/22 abilities,
24 DoT/HoT ticks, 25 deaths, 33 ActorControl. This module mirrors ACT's
Encounter/Combatant aggregation on top of those lines, so the app can show
live per-player numbers without trusting, or needing, ACT's own CombatData
summaries. Qt-free by design. The engine is fed from the GUI thread but must
stay importable headless for tests.

Field layouts follow cactbot's LogGuide, upstream at OverlayPlugin/cactbot
docs/LogGuide.md, cross-checked against a real captured log. The effect-pair
decode is the wire-true one.

- flags byte 0 is the effect type, 0x03/0x05/0x06/0x33 damage, 0x04 heal,
  0x01/0x02 miss/dodge. Byte 1 is the severity, 0x20 crit, 0x40 direct hit.
  Verified against real captures. About 18/16/8% of damage effects carry
  0x20/0x40/0x60 there. Heal crits live in byte 2 instead, the "0x200004 is
  a crit heal" quirk. This meter's crit columns are damage-only, like ACT's
  parse columns, so heal crits are deliberately not counted.
- damage value. 0x0100 mask means hallowed, amount 0. 0x4000 mask means
  "a lot" of damage, bytes ABCD -> DAB as a 3-byte integer. That is the low
  byte shifted left 16, bitwise-or the high word. Otherwise the amount is
  the high word. Doc examples. 47280000 -> 18216, 423F400F -> 999999. The
  LogGuide's Hyperdrive caption claims 426B4001 -> 82538, a stale artifact
  of the pre-Shadowbringers "D A B-D" guess. The current doc's own formula
  gives 82539, which is what we produce.
- the Plenary 3F-zero shift needs no special case for pair selection.
  Iterating all eight effect pairs and skipping unknown types lands on the
  real pair anyway. The amount decode does need one. A shifted literal
  under 0x10000 shifted right by 16 reads 0, so heal pairs take it as-is.

Two estimation caveats, inherited from the wire format.

- DoT/HoT lines, the 24s, report one AGGREGATE tick per target across every
  active dot of that kind, with no crit/DH flags and no per-dot ability id.
  ACT credits the whole tick to the applier the same way. FFLogs instead
  re-estimates per-dot shares, so dot-heavy jobs read a little differently
  here than on FFLogs. This is the classic ACT vs FFLogs dot discrepancy.
- Combatants are keyed by entity id, but ids are only scoped to a pull and
  player job data arrives via 03 lines. The WS roster feeds top that up on
  subscribe, PartyChanged jobs and the ChangePrimaryPlayer id, so a
  mid-instance connect still classifies the party once the burst lands.

Pets merge into their owner, damage, healing and maxhit, like ACT's
"combine pets with owner". Any actor whose ownerId, from the 03 line or the
21/22 owner fields, maps to a player is folded into that player's row.

The on-screen meter is a view layered over the encounter, ACT-style. It
pauses once no damage has occurred for the configured idle timeout, two
minutes by default, keeps showing those frozen numbers, and resets to a
fresh segment when damage resumes. The encounter itself is never split by
downtime. The recorded log always captures the whole pull. A finalized
pull stays on screen until the next one begins.
"""

from __future__ import annotations

import time

from drop_log import log_drop

# Log line types the meter consumes, decimal strings as they arrive.
METER_LOG_TYPES = frozenset(("01", "02", "03", "21", "22", "24", "25", "33"))

# ActorControl, line 33, command for a wipe/reset.
_WIPE_COMMAND = "4000000F"

# Most player rows the overlay feed carries. Alliance raids run to 24. The
# plugin's own Max combatants setting narrows this down for display.
MAX_OVERLAY_ROWS = 24

# ClassJob id -> acronym. Ids 8-18 are crafting/gathering classes and map to
# "", no combat row worth labelling. 0 is NPC/none. Closed-world through
# patch 7.x. Unknown future jobs degrade to "" rather than a wrong guess.
JOB_ACRONYMS = {
    1: "GLA", 2: "PGL", 3: "MRD", 4: "LNC", 5: "ARC", 6: "CNJ", 7: "THM",
    19: "PLD", 20: "MNK", 21: "WAR", 22: "DRG", 23: "BRD", 24: "WHM",
    25: "BLM", 26: "ACN", 27: "SMN", 28: "SCH", 29: "ROG", 30: "NIN",
    31: "MCH", 32: "DRK", 33: "AST", 34: "SAM", 35: "RDM", 36: "BLU",
    37: "GNB", 38: "DNC", 39: "RPR", 40: "SGE", 41: "VPR", 42: "PCT",
}

_DAMAGE_TYPES = frozenset((0x03, 0x05, 0x06, 0x33))
_HEAL_TYPE = 0x04
_MISS_TYPES = frozenset((0x01, 0x02))

# Default damage-idle timeout for the on-screen meter. The DPS tab offers a
# dropdown from 15s to 10m. After this long with no damage the live view
# pauses. The next hit starts a fresh segment. Display only. The recorded
# pull is never split.
DEFAULT_IDLE_TIMEOUT = 120.0


def _actor_int(actor_id) -> "int | None":
    """Actor id as an int, hex string or int with a decimal fallback, so padded
    or case variants of the same id resolve to one key. None for blank/invalid
    ids and the no-target sentinels 0 / E0000000. Mirrors
    telesto_client._actor_int. Duplicated so this module stays dependency-free.
    """
    if actor_id is None:
        return None
    if isinstance(actor_id, bool):          # bool is an int subclass
        return None
    if isinstance(actor_id, int):
        v = actor_id
    else:
        s = str(actor_id).strip()
        if not s:
            return None
        try:
            v = int(s, 16)
        except ValueError:
            try:
                v = int(s)
            except ValueError:
                return None
    if v <= 0 or v == 0xE0000000:
        return None
    return v


def _unpack_effect(flags_hex: str, dmg_hex: str) -> "tuple[str, int, bool, bool]":
    """Decode one [flags, damage] effect pair from a 21/22 line.

    Returns kind, amount, crit, dh, with kind in {"damage", "heal", "miss",
    "none"}. "none" covers status applications and padding pairs. The two
    middle flag bytes are ability-specific, combo/positional data, and are
    ignored. Heals never direct hit, so dh is always False for them.
    """
    try:
        f = int(flags_hex, 16)
    except (TypeError, ValueError):
        f = 0
    etype = f & 0xFF
    severity = (f >> 8) & 0xFF
    crit = bool(severity & 0x20)
    dh = bool(severity & 0x40) and etype != _HEAL_TYPE
    if etype in _DAMAGE_TYPES:
        kind = "damage"
    elif etype == _HEAL_TYPE:
        kind = "heal"
    elif etype in _MISS_TYPES:
        kind = "miss"
    else:
        kind = "none"
    try:
        v = int(dmg_hex, 16)
    except (TypeError, ValueError):
        v = 0
    if not 0 <= v <= 0xFFFFFFFF:
        # Negative hex parses fine and 9+ digit fields overflow the 32-bit
        # wire value. Both are a bad line, credit nothing.
        v = 0
    if kind == "heal" and 0 < v < 0x10000:
        # Shifted literal-value lines, the Plenary family, carry the heal
        # unshifted. A value this small shifted right by 16 reads 0.
        amount = v
    elif kind == "damage" and v & 0x0100:
        # hallowed/invulnerable, the number is not damage
        amount = 0
    elif v & 0x4000:        # "a lot" of damage, the low byte is the real top byte
        amount = ((v & 0xFF) << 16) | (v >> 16)
    else:
        amount = v >> 16
    return kind, amount, crit, dh


def _mmss(seconds: float) -> str:
    s = max(0, int(seconds))
    return f"{s // 60:02d}:{s % 60:02d}"


class _Combatant:
    """One player's running totals for the current encounter. Pets never get
    a record of their own. Their contribution lands on the owner's record."""

    __slots__ = ("aid", "name", "job", "damage", "healed", "swings", "hits",
                 "crits", "dhits", "cdhits", "maxhit_name", "maxhit_amount",
                 "deaths", "damagetaken", "first", "last")

    def __init__(self, aid: int, name: str = "", job: int = 0) -> None:
        self.aid = aid
        self.name = name
        self.job = job
        self.damage = 0
        self.healed = 0
        self.swings = 0
        self.hits = 0
        self.crits = 0
        self.dhits = 0
        self.cdhits = 0
        self.maxhit_name = ""
        self.maxhit_amount = 0
        self.deaths = 0
        self.damagetaken = 0
        self.first: "float | None" = None   # own-activity window, for dps/hps
        self.last: "float | None" = None

    def touch(self, now: float) -> None:
        if self.first is None:
            self.first = now
        self.last = now


class _Encounter:
    """One pull, ACT-style. Titled by the zone, wall-clock bounded, holding
    every player who did or took anything. `last` is the last recorded combat
    activity. A finalized encounter's duration ends there, not at the
    finalize event. ACT trims the out-of-combat tail the same way, and a
    stale in-combat flag, or a wipe nobody acknowledges for minutes, would
    otherwise stretch the fight clock and dilute every rate. `last_damage`
    is only maintained on the live display view, where it drives the idle
    pause and the segment reset. `wall_start` is the epoch-seconds begin
    stamp. The monotonic `start` drives durations, but the pull log needs
    the wall-clock begin so the record opens at pull start, not at the
    encounter-end write."""

    __slots__ = ("title", "zone", "start", "last", "last_damage", "combatants",
                 "wall_start")

    def __init__(self, title: str, zone: str, start: float,
                 wall_start: "float | None" = None) -> None:
        self.title = title
        self.zone = zone
        self.start = start
        self.last: "float | None" = None
        self.last_damage: "float | None" = None
        self.combatants: "dict[int, _Combatant]" = {}
        self.wall_start = wall_start


class DpsMeter:
    """Feed combat log lines in, read ACT-shaped snapshots out.

    Encounter lifecycle mirrors ACT. It starts on a combat flag rising, either
    InCombat bool. ACT can hold its own flag high across consecutive pulls of
    one instance. Failing that it starts lazily on the first combat effect
    that involves a player, so starting the app mid-fight still meters the
    pull. It finalizes on a combat flag dropping, on a wipe, or on any 01
    zone line, since re-entering the same instance is the next pull.
    Encounters with no player damage and no player damage taken are dropped
    silently. The last finalized pull is preserved for display until the next
    one begins.
    """

    def __init__(self, clock=None) -> None:
        self._clock = clock or time.monotonic
        self._zone = ""
        self._me_id: "int | None" = None
        self._jobs: "dict[int, int]" = {}      # actor id -> ClassJob id, nonzero means a player
        self._owners: "dict[int, int]" = {}    # pet or summon id -> owner id
        self._names: "dict[int, str]" = {}     # actor id -> last seen name
        self._in_act = False
        self._in_game = False
        self.current: "_Encounter | None" = None
        self._view: "_Encounter | None" = None  # display segment, resets on idle
        self._last_final: "dict | None" = None  # preserved pull, shown while idle
        self._idle_timeout = DEFAULT_IDLE_TIMEOUT
        # Called with the final snapshot dict when a non-empty encounter ends.
        self.on_encounter_end = None

    def set_idle_timeout(self, secs) -> None:
        """How long the on-screen meter keeps ticking after the last damage
        before it pauses, resetting on the next hit. Display only. The
        recorded pull is never split or shortened by this."""
        try:
            v = float(secs)
        except (TypeError, ValueError):
            return
        self._idle_timeout = min(600.0, max(15.0, v))

    # ------------------------------------------------------------------
    # actor bookkeeping
    # ------------------------------------------------------------------
    def _note(self, table: dict, aid: int, value) -> None:
        """Bounded insert into an actor map. 03 lines stream for every
        passer-by, so a city session would grow these maps without a cap.
        Mirrors the 1024-entry cap _note_actor_job gives main_window."""
        # Dicts are insertion ordered, so evict only the oldest entries. A
        # re-note re-inserts the actor at the back, the trim then drops who
        # was seen longest ago instead of who arrived first, which can be
        # the current party under a city worth of passers-by. Insert first,
        # then trim back to the cap, or the map would rest at 1025.
        # Clearing the whole map dropped the current party too, and their
        # damage stopped crediting until a fresh 03 arrived per player.
        table.pop(aid, None)
        table[aid] = value
        while len(table) > 1024:
            del table[next(iter(table))]

    def note_job(self, aid: int, job: int) -> None:
        """A roster job from outside the log stream, the WS PartyChanged
        burst main_window forwards. Same map the 03 lines fill, so a
        mid-instance connect stops reading the party as enemies once the
        roster lands."""
        if job:
            self._note(self._jobs, aid, job)
            # The burst can land after a pet line already opened the
            # owner's row at job 0. Run the same late upgrade the 03
            # handler runs, or the job cell stays blank until the owner
            # personally acts.
            for enc in (self.current, self._view):
                if enc is not None and aid in enc.combatants:
                    self._combatant(enc, aid)

    def set_me(self, aid) -> None:
        """The local player from the WS ChangePrimaryPlayer event, replayed
        on subscribe. Pins _me_id before any 02 line arrives. A blank or
        malformed id changes nothing."""
        v = _actor_int(aid)
        if v is not None:
            self._me_id = v

    def _is_player(self, aid: "int | None") -> bool:
        if aid is None:
            return False
        return aid == self._me_id or self._jobs.get(aid, 0) != 0

    def _player_key(self, aid: "int | None") -> "int | None":
        """The combatant record key for an actor. The owner id for player
        pets, the id itself for players, None for enemies and their minions."""
        if aid is None:
            return None
        owner = self._owners.get(aid)
        if owner is not None and owner != aid:
            return owner if self._is_player(owner) else None
        return aid if self._is_player(aid) else None

    def _combatant(self, enc: _Encounter, key: int, name: str = "") -> _Combatant:
        c = enc.combatants.get(key)
        if c is None:
            c = _Combatant(key, name or self._names.get(key, ""),
                           self._jobs.get(key, 0))
            enc.combatants[key] = c
        else:
            # Records created by a pet's line start nameless, the line only
            # names the pet. The owner's name lands once an 03 or an owner
            # line supplies it.
            if not c.name:
                c.name = name or self._names.get(key, "")
            if not c.job and self._jobs.get(key, 0):
                c.job = self._jobs[key]
        return c

    def _begin(self) -> None:
        if self.current is not None:
            # A stray late tick can reopen an encounter nobody finalizes.
            # Past the idle timeout it is dead weight. Close it out before
            # the fresh pull starts, or the two merge into one phantom.
            enc = self.current
            last = enc.last if enc.last is not None else enc.start
            if self._clock() - last <= self._idle_timeout:
                return
            self.finalize()
        # A new pull pushes the preserved one off screen.
        self._last_final = None
        now = self._clock()
        wall = time.time()
        self.current = _Encounter(self._zone or "Encounter", self._zone,
                                  now, wall)
        # The on-screen view runs alongside the encounter. Only the view
        # resets on damage idle. The encounter always logs the whole pull.
        self._view = _Encounter(self._zone or "Encounter", self._zone,
                                now, wall)

    def finalize(self) -> None:
        """End the current encounter, if any, and emit on_encounter_end for
        non-empty ones. Safe to call with nothing in progress. Also the
        app-close hook so a quit mid-fight still records."""
        enc = self.current
        if enc is None:
            return
        self.current = None
        self._view = None
        if not any(c.damage > 0 or c.damagetaken > 0
                   for c in enc.combatants.values()):
            return                      # empty pull, nothing worth keeping
        final = self._snapshot(enc, self._clock(), active=False)
        self._last_final = final      # stays on screen until the next pull
        cb = self.on_encounter_end
        if cb is not None:
            try:
                cb(final)
            except Exception as exc:  # noqa: BLE001 - a consumer bug must not kill the feed
                log_drop("dps-meter", f"on_encounter_end callback failed: {exc!r}")

    def _note_damage(self, now: float) -> None:
        """Stamp damage activity on the display view. Damage landing more
        than the idle timeout after the previous hit resets the view first.
        The frozen numbers give way to a fresh segment starting with this
        hit. The encounter is never touched. The log keeps the whole pull."""
        view = self._view
        if view is None:
            return
        if view.last_damage is not None \
                and now - view.last_damage > self._idle_timeout:
            self._view = view = _Encounter(view.title, view.zone, now)
        view.last_damage = now

    # ------------------------------------------------------------------
    # feed
    # ------------------------------------------------------------------
    def set_in_combat(self, in_act: bool, in_game: bool) -> None:
        """InCombat event, inACTCombat and inGameCombat. A rising edge on either
        flag begins the encounter. A falling edge on either ends it. ACT can
        hold inACTCombat high across back-to-back pulls of one instance, so
        keying only on it would merge pulls. Keying only on inGameCombat
        would miss ACT-only combat. A mixed message, one flag falling while
        the other rises, finalizes the open encounter before the new begin
        so the two pulls never merge. Wipes and zone changes still finalize
        via their own lines."""
        act, game = bool(in_act), bool(in_game)
        if self.current is not None and (
                (self._in_act and not act) or (self._in_game and not game)):
            self.finalize()
        if (act and not self._in_act) or (game and not self._in_game):
            self._begin()
        self._in_act = act
        self._in_game = game

    def process(self, fields: "list[str]", raw: str = "") -> None:
        """One log line pre-split on '|'. Only METER_LOG_TYPES carry meter
        data. Anything else returns right away. Never raises on malformed
        input. A bad line is skipped, not fatal."""
        if not fields:
            return
        t = fields[0]
        try:
            if t == "01":
                self._on_zone(fields)
            elif t == "02":
                self._on_primary_player(fields)
            elif t == "03":
                self._on_add_combatant(fields)
            elif t in ("21", "22"):
                self._on_ability(fields)
            elif t == "24":
                self._on_dot_hot(fields)
            elif t == "25":
                self._on_death(fields)
            elif t == "33":
                if len(fields) > 3 and fields[3].upper() == _WIPE_COMMAND:
                    self.finalize()
        except Exception:  # noqa: BLE001 - defensive: the GUI wraps this too
            log_drop("dps-meter", f"skipped malformed {t} line: {str(raw)[:140]}")

    # ------------------------------------------------------------------
    # line handlers
    # ------------------------------------------------------------------
    def _on_zone(self, fields: "list[str]") -> None:
        if len(fields) <= 3:
            return
        # A zone change hard-ends any pull in progress, like ACT, including
        # re-entering the same instance for the next pull. Entity ids are
        # reassigned per entry, so actor knowledge must reset anyway, the
        # local player id too. The next 02 line pins it again.
        self.finalize()
        self._zone = fields[3].strip()
        self._jobs.clear()
        self._owners.clear()
        self._names.clear()
        self._me_id = None

    def _on_primary_player(self, fields: "list[str]") -> None:
        if len(fields) <= 3:
            return
        aid = _actor_int(fields[2])
        if aid is None:
            # A blank or garbage id must not wipe a known good one. The
            # WS fed set_me ignores such ids the same way. The next valid
            # 02 line can still correct the pin.
            return
        self._me_id = aid
        name = fields[3].strip()
        if name:
            self._note(self._names, self._me_id, name)

    def _on_add_combatant(self, fields: "list[str]") -> None:
        if len(fields) <= 6:
            return
        aid = _actor_int(fields[2])
        if aid is None:
            return
        name = fields[3].strip()
        if name:
            self._note(self._names, aid, name)
        try:
            job = int(fields[4], 16)
        except (TypeError, ValueError):
            job = 0
        # Players only, the '10'-prefixed ids, same filter main_window gives
        # the same 03 line. Duty support and Trust NPCs carry real ClassJob
        # ids and would otherwise land as rows in the meter and overlay.
        if job and fields[2][:2] == "10":
            self._note(self._jobs, aid, job)
        owner = _actor_int(fields[6])   # "0000"/"00" parse to 0 -> unowned
        if owner is not None and owner != aid:
            self._note(self._owners, aid, owner)
        # Late 03 lines can upgrade a record created by an earlier 21 line.
        # Both records, the encounter log and the on-screen view, or the
        # overlay keeps the stale nameless label until the owner acts again.
        for enc in (self.current, self._view):
            if enc is not None and aid in enc.combatants:
                self._combatant(enc, aid)

    def _on_ability(self, fields: "list[str]") -> None:
        if len(fields) < 24:
            return
        sid = _actor_int(fields[2])
        tid = _actor_int(fields[6])
        # Pets are also identified by the owner fields trailing 21/22 lines.
        owner_name = ""
        if len(fields) > 47:
            owner = _actor_int(fields[47])
            if owner is not None and sid is not None and owner != sid:
                self._note(self._owners, sid, owner)
                if len(fields) > 48:
                    owner_name = fields[48].strip()
        src_key = self._player_key(sid)
        tgt_key = self._player_key(tid)

        effects = []
        for i in range(8, 24, 2):
            if i + 1 >= len(fields):
                break
            if not fields[i] and not fields[i + 1]:
                continue
            effects.append(_unpack_effect(fields[i], fields[i + 1]))
        # Only hostile action opens an encounter lazily. A pre-pull regen or
        # buff, status effects and heals, minutes before the engage must not
        # start the clock, or every pull's duration would include the
        # preamble. Damage and misses count. Heals alone do not.
        if self.current is None:
            if (src_key is None and tgt_key is None) or not any(
                    e[0] in ("damage", "miss") for e in effects):
                return
            self._begin()
        now = self._clock()
        if any(e[0] == "damage" and e[1] > 0 for e in effects):
            self._note_damage(now)
        # Everything lands twice. On the encounter, the log, and on the
        # display view, what the meter shows right now.
        for enc in (self.current, self._view):
            if enc is not None:
                self._apply_ability(enc, fields, effects, now,
                                    src_key, tgt_key, sid, tid, owner_name)

    def _apply_ability(self, enc: _Encounter, fields: "list[str]",
                       effects: list, now: float,
                       src_key: "int | None", tgt_key: "int | None",
                       sid: "int | None", tid: "int | None",
                       owner_name: str) -> None:
        if any(e[0] != "none" for e in effects):
            enc.last = now
        ability = fields[5] if len(fields) > 5 else ""
        src = None
        if src_key is not None:
            # A pet's line names the pet, not the owner it merges into. The
            # ownerName trailing the line, or a later 03, supplies the owner.
            src = self._combatant(enc, src_key,
                                  fields[3] if src_key == sid else owner_name)
        if src is not None:
            # One swing per ability line, hit or miss, pure status lines too.
            # A deliberate divergence from ACT, which counts damaging lines
            # only. Any cast reads as activity here.
            src.swings += 1
            src.touch(now)
        tgt = None
        for kind, amount, crit, dh in effects:
            if kind == "damage":
                if src is not None:
                    src.damage += amount
                    if amount > 0:     # misses/hallowed count as swings, not hits
                        src.hits += 1
                        if crit:
                            src.crits += 1
                        if dh:
                            src.dhits += 1
                        if crit and dh:
                            src.cdhits += 1
                        if amount > src.maxhit_amount:
                            src.maxhit_amount = amount
                            src.maxhit_name = ability
                if tgt_key is not None and tgt_key != src_key \
                        and tgt_key == tid:
                    # Enemy damage on players is only tracked as taken. The
                    # enemy itself never becomes a meter row. Self-damage
                    # credits damage only, ACT excludes it from taken. A pet
                    # target resolves to its owner and credits no one, like
                    # ACT credits pet deaths to no one.
                    if tgt is None:
                        tgt = self._combatant(enc, tgt_key, fields[7])
                    tgt.damagetaken += amount
            elif kind == "heal":
                if src is not None:
                    src.healed += amount

    def _on_dot_hot(self, fields: "list[str]") -> None:
        if len(fields) < 19:
            return
        tid = _actor_int(fields[2])
        which = fields[4]
        try:
            amount = int(fields[6], 16)
        except (TypeError, ValueError):
            try:
                amount = int(fields[6])
            except (TypeError, ValueError):
                amount = 0
        if not 0 <= amount <= 0xFFFFFFFF:
            # Same guard as the 21 path. Negative hex parses fine and 9+
            # digit fields overflow the wire value. A bad tick is skipped.
            amount = 0
        app_id = _actor_int(fields[17])
        app_key = self._player_key(app_id)
        tgt_key = self._player_key(tid)
        if self.current is None:
            # DoT ticks are hostile and can open an encounter. A pre-pull
            # regen, a HoT, cannot. A zero-amount tick carries no damage, so
            # it must not open a phantom one either.
            if which != "DoT" or amount <= 0 or (app_key is None and tgt_key is None):
                return
            self._begin()
        now = self._clock()
        if which == "DoT" and amount > 0:
            self._note_damage(now)
        for enc in (self.current, self._view):
            if enc is not None:
                self._apply_dot_hot(enc, fields, which, amount, now,
                                    app_key, tgt_key, app_id, tid)

    def _apply_dot_hot(self, enc: _Encounter, fields: "list[str]",
                       which: str, amount: int, now: float,
                       app_key: "int | None", tgt_key: "int | None",
                       app_id: "int | None", tid: "int | None") -> None:
        if which not in ("DoT", "HoT") or amount <= 0:
            # A tick with an unknown which field or no amount credits nothing,
            # so it must not bump the encounter clock or the applier's
            # activity stamp either. Both feed the rate denominators.
            return
        enc.last = now
        if which == "DoT":
            if app_key is not None:
                c = self._combatant(enc, app_key,
                                    fields[18] if app_key == app_id else "")
                c.damage += amount
                c.touch(now)
            if tgt_key is not None and tgt_key != app_key \
                    and tgt_key == tid:
                # A pet tick resolves to its owner and credits no one, same
                # as the ability path above. A tick the applier lands on
                # itself credits damage only, ACT excludes self damage from
                # taken there too.
                t = self._combatant(enc, tgt_key, fields[3])
                t.damagetaken += amount
        elif which == "HoT":
            if app_key is not None:
                c = self._combatant(enc, app_key,
                                    fields[18] if app_key == app_id else "")
                c.healed += amount
                c.touch(now)

    def _on_death(self, fields: "list[str]") -> None:
        if len(fields) <= 3:
            return
        tid = _actor_int(fields[2])
        key = self._player_key(tid)
        if key is None or key != tid:
            # Not a player, or a pet resolving to its owner. ACT credits pet
            # deaths to no one, so the owner's count stays untouched.
            return
        if self.current is None:
            # No lazy begin here, unlike the hostile-line paths. A real in
            # combat death always follows the damage that opened the pull, so
            # an open encounter already exists. An out-of-combat death would
            # otherwise start a phantom one with a running clock.
            return
        now = self._clock()
        for enc in (self.current, self._view):
            if enc is None:
                continue
            enc.last = now
            c = self._combatant(enc, key, fields[3])
            c.deaths += 1

    # ------------------------------------------------------------------
    # reporting
    # ------------------------------------------------------------------
    def _snapshot(self, enc: _Encounter, now: float, active: bool) -> dict:
        # A finalized fight's clock stops at the last recorded combat action,
        # not at whenever the end signal arrived. The live one keeps ticking.
        # The idle clamp is a display-view thing. The live view pauses at the
        # timeout after the last damage, while a finalized encounter always
        # keeps its full wall-clock length, downtime included. A whiffed pull
        # opens on a miss and never stamps last_damage, so the clamp falls
        # back to the encounter start or the live clock would run unbounded.
        span_end = now if active or enc.last is None else enc.last
        if active:
            idle_base = enc.last_damage if enc.last_damage is not None else enc.start
            span_end = min(span_end, idle_base + self._idle_timeout)
        dur = max(0.0, span_end - enc.start)
        players = sorted(enc.combatants.values(),
                         key=lambda c: c.damage, reverse=True)
        total_damage = sum(c.damage for c in players)
        total_deaths = sum(c.deaths for c in players)
        best = max(players, key=lambda c: c.maxhit_amount, default=None)

        # Cross-world duplicates share a display name. A name-keyed dict keeps
        # only the last record written, the lower damage one given the sort
        # above, so disambiguate collisions with the actor id.
        display = [c.name or f"{c.aid:X}" for c in players]
        dupes = {n for n in display if display.count(n) > 1}
        combatants = {}
        for c in players:
            active_secs = 0.0 if c.first is None else max(0.0, c.last - c.first)
            per = max(1.0, active_secs)      # own-activity rate, ACT's "DPS"
            enc_per = max(1.0, dur)          # encounter-length rate, "ENCDPS"
            name = c.name or f"{c.aid:X}"
            if name in dupes:
                name = f"{name} ({c.aid:X})"
            maxhit = (f"{c.maxhit_name}-{c.maxhit_amount}"
                      if c.maxhit_name else "")
            hits = c.hits
            combatants[name] = {
                "name": name,
                "Job": JOB_ACRONYMS.get(c.job, ""),
                "damage": c.damage,
                "damage%": (c.damage / total_damage * 100.0) if total_damage else 0.0,
                "dps": c.damage / per,
                "encdps": c.damage / enc_per,
                "ENCDPS": c.damage / enc_per,
                "swings": c.swings,
                "hits": c.hits,
                "crithits": c.crits,
                "crithit%": (c.crits / hits * 100.0) if hits else 0.0,
                "DirectHitPct": (c.dhits / hits * 100.0) if hits else 0.0,
                "CritDirectHitPct": (c.cdhits / hits * 100.0) if hits else 0.0,
                "maxhit": maxhit,
                "MAXHIT": maxhit,
                "deaths": c.deaths,
                "healed": c.healed,
                "enchps": c.healed / enc_per,
                "ENCHPS": c.healed / enc_per,
                "damagetaken": c.damagetaken,
            }
        enc_maxhit = (f"{best.maxhit_name}-{best.maxhit_amount}"
                      if best is not None and best.maxhit_name else "")
        encdps = total_damage / max(1.0, dur)
        return {
            "isActive": active,
            "Encounter": {
                "title": enc.title,
                "duration": _mmss(dur),
                "DURATION": int(dur),
                "damage": total_damage,
                "dps": encdps,
                "encdps": encdps,
                "ENCDPS": encdps,
                "maxhit": enc_maxhit,
                "deaths": total_deaths,
                "CurrentZoneName": enc.zone,
                "wall_start": enc.wall_start,
            },
            "Combatant": combatants,
        }

    def snapshot(self) -> dict:
        """What the meter should show right now. The live display view while
        a fight runs, paused and reset by damage idle, the last finalized
        pull between pulls, or an inactive shell if there has been no pull
        yet."""
        if self.current is not None:
            enc = self._view if self._view is not None else self.current
            return self._snapshot(enc, self._clock(), active=True)
        if self._last_final is not None:
            return self._last_final
        empty = _Encounter(self._zone or "Encounter", self._zone,
                           self._clock())
        return self._snapshot(empty, self._clock(), active=False)

    def overlay_rows(self) -> list:
        """Top players by ENCDPS in the current display view, capped at
        MAX_OVERLAY_ROWS, returned as [name, job, encdps, damage%, enchps,
        is_self, deaths] rows for the in-game overlay. Empty when no
        encounter is running."""
        enc = self._view if self._view is not None else self.current
        if enc is None:
            return []
        span_end = self._clock()
        # Same fallback as _snapshot. A miss-opened pull has no last_damage
        # yet, so the clamp bases on the encounter start instead.
        idle_base = enc.last_damage if enc.last_damage is not None else enc.start
        span_end = min(span_end, idle_base + self._idle_timeout)
        enc_per = max(1.0, span_end - enc.start)
        total_damage = sum(c.damage for c in enc.combatants.values())
        rows = []
        for c in enc.combatants.values():
            encdps = c.damage / enc_per
            pct = (c.damage / total_damage * 100.0) if total_damage else 0.0
            rows.append([c.name or f"{c.aid:X}", JOB_ACRONYMS.get(c.job, ""),
                         round(encdps, 1), round(pct, 1),
                         round(c.healed / enc_per, 1),
                         bool(self._me_id is not None and c.aid == self._me_id),
                         c.deaths])
        rows.sort(key=lambda r: r[2], reverse=True)
        return rows[:MAX_OVERLAY_ROWS]
