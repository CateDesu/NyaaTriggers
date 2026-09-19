"""Build ACT-style encounter totals from raw combat log lines without Qt or CombatData
dependencies. Field layouts follow cactbot LogGuide. Pet damage and healing belong to
their owners. DoT and HoT lines contain aggregate ticks without per-effect critical
flags, so these totals can differ from FFLogs estimates. Roster events supplement spawn
lines when connecting during an encounter. A separate display segment pauses on damage
inactivity and resets when damage resumes. Encounter totals retain the full pull, and
final rows remain visible until the next pull.
"""

from __future__ import annotations

import time
from uuid import uuid4

from nyaatriggers.drop_log import log_drop

METER_LOG_TYPES = frozenset(("01", "02", "03", "21", "22", "24", "25", "33"))

# ActorControl command for a wipe or reset.
_WIPE_COMMAND = "4000000F"

# Limit overlay rows to a full alliance. The plugin may display fewer.
MAX_OVERLAY_ROWS = 24

# Unknown jobs and noncombat classes use an empty acronym.
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

# Pause the display after this many seconds without damage. The next hit resets only the
# display segment.
DEFAULT_IDLE_TIMEOUT = 120.0
# Recover stale encounters at the next combat start regardless of display settings.
_STALE_ENCOUNTER_S = 120.0
DEATH_DUPLICATE_SECONDS = 1


def _actor_int(actor_id) -> "int | None":
    """Normalize numeric and hexadecimal actor IDs, with decimal fallback. Reject invalid
    IDs and no-target sentinels. Keep this independent of telesto_client for standalone
    use.
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
    """Decode a flags and damage pair into kind, amount, critical and direct hit. Ignore
    combo and positional bytes. Heal criticals are excluded from damage statistics and
    heals never direct hit. The extended damage formula gives 82539 for 426B4001,
    correcting the older LogGuide example.
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
        # Reject amounts outside the wire's 32 bits.
        v = 0
    if kind == "heal" and 0 < v < 0x10000:
        # Small literal heals such as Plenary are already unshifted.
        amount = v
    elif kind == "damage" and v & 0x0100:
        # The invulnerability flag means no damage was dealt.
        amount = 0
    elif v & 0x4000:        # The low byte becomes the high byte of extended damage.
        amount = ((v & 0xFF) << 16) | (v >> 16)
    else:
        amount = v >> 16
    return kind, amount, crit, dh


def _mmss(seconds: float) -> str:
    s = max(0, int(seconds))
    return f"{s // 60:02d}:{s % 60:02d}"


class _Combatant:
    """Player totals include contributions from owned pets."""

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
    """Encounter duration ends at the last combat action. last_damage controls only display
    idle handling. Monotonic start measures duration, while wall_start timestamps the
    saved pull.
    """

    __slots__ = ("title", "zone", "start", "last", "last_damage", "combatants",
                 "wall_start", "pull_id")

    def __init__(self, title: str, zone: str, start: float,
                 wall_start: "float | None" = None) -> None:
        self.title = title
        self.zone = zone
        self.start = start
        self.last: "float | None" = None
        self.last_damage: "float | None" = None
        self.combatants: "dict[int, _Combatant]" = {}
        self.wall_start = wall_start
        self.pull_id = str(uuid4())


class DpsMeter:
    """Start on either combat flag rising or the first hostile player event. End on combat
    exit, wipes or zone lines. Skip empty encounters and retain the final snapshot until
    the next pull.
    """

    def __init__(self, clock=None) -> None:
        self._clock = clock or time.monotonic
        self._zone = ""
        self._awaiting_zone_metadata = True
        self._me_id: "int | None" = None
        self._jobs: "dict[int, int]" = {}      # Actor ID to ClassJob ID
        self._roster_jobs: dict[int, int] = {}
        self._owners: "dict[int, int]" = {}    # Pet ID to owner ID
        self._names: "dict[int, str]" = {}
        self._in_act = False
        self._in_game = False
        self.current: "_Encounter | None" = None
        self._view: "_Encounter | None" = None  # Display segment resets when damage resumes after idle.
        self._last_final: "dict | None" = None  # preserved pull, shown while idle
        self._idle_timeout = DEFAULT_IDLE_TIMEOUT
        self.on_encounter_end = None
        self.on_pull_start = None
        self.on_pull_finish = None
        self._last_end_time = 0.0
        self._death_times = {}
        self.is_duplicate_death = None

    def set_idle_timeout(self, secs) -> None:
        """Set the display idle timeout without splitting or shortening recorded
        encounters.
        """
        try:
            v = float(secs)
        except (TypeError, ValueError):
            return
        self._idle_timeout = min(600.0, max(15.0, v))

    def _note(self, table: dict, aid: int, value) -> None:
        """Bound actor caches so unrelated spawn lines cannot grow them indefinitely."""
        # Refresh insertion order when an actor is seen again, then evict the oldest
        # entries. Clearing the entire cache would lose current party jobs.
        table.pop(aid, None)
        table[aid] = value
        while len(table) > 1024:
            oldest = next(k for k in table if table is not self._jobs
                          or k not in self._roster_jobs)
            del table[oldest]

    def note_job(self, aid: int, job: int) -> None:
        """Update actor jobs from roster events as well as spawn lines."""
        if job:
            self._roster_jobs.pop(aid, None)
            self._roster_jobs[aid] = job
            while len(self._roster_jobs) > 24:
                del self._roster_jobs[next(iter(self._roster_jobs))]
            self._note(self._jobs, aid, job)
            # Update existing owner rows when roster details arrive after pet actions.
            for enc in (self.current, self._view):
                if enc is not None and aid in enc.combatants:
                    self._combatant(enc, aid)

    def set_me(self, aid) -> None:
        """Set local identity from subscription metadata. Invalid IDs leave it unchanged.
        """
        v = _actor_int(aid)
        if v is not None:
            self._me_id = v

    def _is_player(self, aid: "int | None") -> bool:
        if aid is None:
            return False
        return aid == self._me_id or self._jobs.get(aid, 0) != 0

    def _player_key(self, aid: "int | None") -> "int | None":
        """Resolve player pets to their owner, players to themselves and other actors to
        None.
        """
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
            # Fill an unnamed owner row created by an earlier pet action.
            if not c.name:
                c.name = name or self._names.get(key, "")
            if not c.job and self._jobs.get(key, 0):
                c.job = self._jobs[key]
        return c

    def _begin(self) -> None:
        if self.current is not None:
            # Finalize idle encounters before a new pull. A late tick may have reopened
            # one without a matching combat end.
            enc = self.current
            last = enc.last if enc.last is not None else enc.start
            if self._clock() - last <= _STALE_ENCOUNTER_S:
                return
            self.finalize()
        self._last_final = None
        self._death_times.clear()
        now = self._clock()
        wall = time.time()
        self.current = _Encounter(self._zone or "Encounter", self._zone,
                                  now, wall)
        self._view = _Encounter(self._zone or "Encounter", self._zone,
                                now, wall)
        self._notify_pull(self.on_pull_start, self.full_snapshot())

    @staticmethod
    def _notify_pull(callback, snapshot):
        if callback is not None:
            try:
                callback(snapshot)
            except Exception as exc:
                log_drop("pull-observer", f"{exc!r}")

    def finalize(self, reason="combat-ended") -> None:
        """Finalize and report a nonempty encounter. Also used when closing during a fight.
        Safe when no encounter is open.
        """
        enc = self.current
        if enc is None:
            return
        self.current = None
        self._view = None
        if not any(c.damage > 0 or c.damagetaken > 0
                   for c in enc.combatants.values()):
            empty = self._snapshot(enc, self._clock(), active=False)
            empty["Encounter"]["end_reason"] = "empty"
            empty["Encounter"]["boundary_reason"] = reason
            self._notify_pull(self.on_pull_finish, empty)
            return
        final = self._snapshot(enc, self._clock(), active=False)
        final["Encounter"]["end_reason"] = reason
        self._last_end_time = self._clock()
        self._last_final = final
        self._notify_pull(self.on_pull_finish, final)
        cb = self.on_encounter_end
        if cb is not None:
            try:
                cb(final)
            except Exception as exc:  # noqa: BLE001
                log_drop("dps-meter", f"on_encounter_end callback failed: {exc!r}")

    def _note_damage(self, now: float) -> None:
        """Reset the display segment when damage resumes after the idle timeout. Preserve
        full encounter totals.
        """
        view = self._view
        if view is None:
            return
        if view.last_damage is not None \
                and now - view.last_damage > self._idle_timeout:
            self._view = view = _Encounter(view.title, view.zone, now)
        view.last_damage = now

    def _view_paused(self, now: float) -> bool:
        view = self._view
        if view is None:
            return False
        last = view.last_damage if view.last_damage is not None else view.start
        return now - last > self._idle_timeout

    def feed_lost(self) -> None:
        """Finalize on feed loss and reset combat flags so subscription replay can start a
        fresh encounter.
        """
        self.finalize("feed-lost")
        self._in_act = False
        self._in_game = False
        self._jobs.clear()
        self._roster_jobs.clear()
        self._owners.clear()
        self._names.clear()
        self._me_id = None
        self._awaiting_zone_metadata = True

    def set_zone_metadata(self, name: str, *, zone_changed: bool | None = None) -> None:
        """Apply zone metadata for connections that missed the raw zone line. Repeated
        metadata must not end an encounter. A changed known zone finalizes before the
        overlay is cleared, even when metadata arrives before the raw zone line.
        An explicit False means the ID confirms this is still the same zone.
        """
        name = (name or "").strip()
        if zone_changed:
            # A known ID change is a boundary even when the name repeats or is absent.
            self._on_zone(["01", "", "", name])
            self._awaiting_zone_metadata = not bool(name)
            return
        if not name:
            return
        first_metadata = self._awaiting_zone_metadata or zone_changed is False
        self._awaiting_zone_metadata = False
        if name == self._zone:
            return
        if not first_metadata:
            self.finalize("duty-left")
        self._zone = name
        # Preserve roster data that arrived before initial zone metadata. Clear it on
        # later changes.
        if not first_metadata:
            self._jobs.clear()
            self._roster_jobs.clear()
            self._owners.clear()
            self._names.clear()
            self._me_id = None
        # Keep live damage when only the zone name changes.
        for enc in (self.current, self._view):
            if enc is not None and (not enc.zone or zone_changed is False):
                if not enc.zone or enc.title == enc.zone:
                    enc.title = name
                enc.zone = name

    def set_in_combat(self, in_act: bool, in_game: bool) -> None:
        """Either combat flag can start or end a pull. Process falling edges before rising
        edges in the same message so consecutive pulls stay separate.
        """
        act, game = bool(in_act), bool(in_game)
        if self.current is not None and (
                (self._in_act and not act) or (self._in_game and not game)):
            self.finalize()
        if (act and not self._in_act) or (game and not self._in_game):
            self._begin()
        self._in_act = act
        self._in_game = game

    def process(self, fields: "list[str]", raw: str = "", *, now=None) -> None:
        """Process fields from one log line, skipping unrelated or malformed input. A
        supplied timestamp aligns death checks with the recap.
        """
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
                self._on_death(fields, now)
            elif t == "33":
                if len(fields) > 3 and fields[3].upper() == _WIPE_COMMAND:
                    if self.current is not None:
                        self.finalize("wipe")
                    elif (self._last_final is not None
                          and self._last_final["Encounter"].get("end_reason") == "combat-ended"
                          and self._clock() - self._last_end_time <= 2):
                        self._last_final["Encounter"]["end_reason"] = "wipe"
                        self._notify_pull(self.on_pull_finish, self._last_final)
        except Exception:  # noqa: BLE001
            log_drop("dps-meter", f"skipped malformed {t} line: {str(raw)[:140]}")

    def _on_zone(self, fields: "list[str]") -> None:
        if len(fields) <= 3:
            return
        # Every raw zone line ends the encounter and clears identity because IDs can
        # change even when reentering the same instance.
        self.finalize("duty-left")
        self._zone = fields[3].strip()
        self._awaiting_zone_metadata = False
        self._jobs.clear()
        self._roster_jobs.clear()
        self._owners.clear()
        self._names.clear()
        self._me_id = None

    def _on_primary_player(self, fields: "list[str]") -> None:
        if len(fields) <= 3:
            return
        aid = _actor_int(fields[2])
        if aid is None:
            # Keep known identity until a valid replacement arrives.
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
        # Require player IDs because Trust and duty support NPCs also have combat jobs.
        if job and fields[2][:2] == "10":
            self._note(self._jobs, aid, job)
        owner = _actor_int(fields[6])
        if owner is not None and owner != aid:
            self._note(self._owners, aid, owner)
        # Update both encounter and display rows when late spawn lines supply actor
        # details.
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
        if src_key is None and tgt_key is None:
            return

        effects = []
        reflected = False
        for i in range(8, 24, 2):
            if i + 1 >= len(fields):
                break
            if not fields[i] and not fields[i + 1]:
                continue
            try:
                flags = int(fields[i], 16)
            except (TypeError, ValueError):
                continue
            if not 0 <= flags <= 0xFFFFFFFF:
                continue
            if flags & 0xFF == 0x1D:
                reflected = True
                continue
            effects.append((*_unpack_effect(fields[i], fields[i + 1]), reflected))
        # Damage and misses can start encounters. Healing and buffs before a pull must
        # not start the clock.
        if self.current is None:
            if not any(e[0] in ("damage", "miss") for e in effects):
                return
            self._begin()
        now = self._clock()
        if any(e[0] == "damage" and e[1] > 0 for e in effects):
            self._note_damage(now)
        for enc in (self.current, self._view):
            if enc is not None:
                if enc is self._view and self._view_paused(now):
                    continue
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
            # Use the trailing owner name instead of the pet name.
            src = self._combatant(enc, src_key,
                                  fields[3] if src_key == sid else owner_name)
        if src is not None:
            # Count every ability line as a swing, including status applications. ACT
            # counts damaging lines only.
            src.swings += 1
            src.touch(now)
        reflector = None
        for kind, amount, crit, dh, reflected in effects:
            if kind == "damage":
                dealer = src
                dealer_key = src_key
                victim_key, victim_id, victim_name = tgt_key, tid, fields[7]
                if reflected:
                    if reflector is None and tgt_key is not None:
                        reflector = self._combatant(enc, tgt_key,
                                                    fields[7] if tgt_key == tid else "")
                        if reflector is not src:
                            reflector.swings += 1
                            reflector.touch(now)
                    dealer = reflector
                    dealer_key = tgt_key
                    victim_key, victim_id, victim_name = src_key, sid, fields[3]
                if dealer is not None:
                    dealer.damage += amount
                    if amount > 0:     # misses/hallowed count as swings, not hits
                        dealer.hits += 1
                        if crit:
                            dealer.crits += 1
                        if dh:
                            dealer.dhits += 1
                        if crit and dh:
                            dealer.cdhits += 1
                        if amount > dealer.maxhit_amount:
                            dealer.maxhit_amount = amount
                            dealer.maxhit_name = ability
                # Self damage and pet targets do not count as owner damage taken.
                if victim_key is not None and victim_key != dealer_key \
                        and victim_key == victim_id:
                    victim = self._combatant(enc, victim_key, victim_name)
                    victim.damagetaken += amount
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
            # Reject negative amounts and values wider than 32 bits.
            amount = 0
        app_id = _actor_int(fields[17])
        app_key = self._player_key(app_id)
        tgt_key = self._player_key(tid)
        if app_key is None and (which != "DoT" or tgt_key is None or tgt_key != tid):
            return
        if self.current is None:
            # Only positive DoT damage involving a player can start an encounter here.
            if which != "DoT" or amount <= 0 or (app_key is None and tgt_key is None):
                return
            self._begin()
        now = self._clock()
        if which == "DoT" and amount > 0:
            self._note_damage(now)
        for enc in (self.current, self._view):
            if enc is not None:
                if enc is self._view and self._view_paused(now):
                    continue
                self._apply_dot_hot(enc, fields, which, amount, now,
                                    app_key, tgt_key, app_id, tid)

    def _apply_dot_hot(self, enc: _Encounter, fields: "list[str]",
                       which: str, amount: int, now: float,
                       app_key: "int | None", tgt_key: "int | None",
                       app_id: "int | None", tid: "int | None") -> None:
        if which not in ("DoT", "HoT") or amount <= 0:
            # Unsupported or empty ticks must not advance activity timestamps used by
            # rate calculations.
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
                # Exclude self damage and pet targets from damage taken, as in the
                # ability path.
                t = self._combatant(enc, tgt_key, fields[3])
                t.damagetaken += amount
        elif which == "HoT":
            if app_key is not None:
                c = self._combatant(enc, app_key,
                                    fields[18] if app_key == app_id else "")
                c.healed += amount
                c.touch(now)

    def _on_death(self, fields: "list[str]", now=None) -> None:
        if len(fields) <= 3:
            return
        tid = _actor_int(fields[2])
        key = self._player_key(tid)
        if key is None or key != tid:
            # Pet deaths do not count toward their owners.
            return
        if self.current is None:
            # A death outside combat must not start an encounter.
            return
        now = self._clock() if now is None else now
        previous = self._death_times.get(tid)
        duplicate = (self.is_duplicate_death(tid, now) if self.is_duplicate_death is not None
                     else previous is not None and now - previous < DEATH_DUPLICATE_SECONDS)
        if duplicate:
            return
        self._note(self._death_times, tid, now)
        for enc in (self.current, self._view):
            if enc is None:
                continue
            enc.last = now
            c = self._combatant(enc, key, fields[3])
            c.deaths += 1

    def _snapshot(self, enc: _Encounter, now: float, active: bool, full=False) -> dict:
        # Final duration stops at the last combat action. Apply idle limits only to the
        # live display, using encounter start if no damage was recorded.
        span_end = now if active or enc.last is None else enc.last
        if active and not full:
            idle_base = enc.last_damage if enc.last_damage is not None else enc.start
            span_end = min(span_end, idle_base + self._idle_timeout)
        dur = max(0.0, span_end - enc.start)
        players = sorted(enc.combatants.values(),
                         key=lambda c: c.damage, reverse=True)
        total_damage = sum(c.damage for c in players)
        total_deaths = sum(c.deaths for c in players)
        best = max(players, key=lambda c: c.maxhit_amount, default=None)

        # Add actor IDs to duplicate names so the result dictionary preserves both
        # players.
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
                "is_self": self._me_id is not None and c.aid == self._me_id,
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
                "has_damage": any(c.damage > 0 or c.damagetaken > 0 for c in players),
                "dps": encdps,
                "encdps": encdps,
                "ENCDPS": encdps,
                "maxhit": enc_maxhit,
                "deaths": total_deaths,
                "CurrentZoneName": enc.zone,
                "wall_start": enc.wall_start,
                "monotonic_start": enc.start,
                "last_activity": enc.last if enc.last is not None else enc.start,
                "pull_id": enc.pull_id,
            },
            "Combatant": combatants,
        }

    def full_snapshot(self):
        if self.current is None:
            return None
        return self._snapshot(self.current, self._clock(), active=True, full=True)

    def snapshot(self) -> dict:
        """Return the live display, the final pull between encounters, or an inactive empty
        state.
        """
        if self.current is not None:
            enc = self._view if self._view is not None else self.current
            return self._snapshot(enc, self._clock(), active=True)
        if self._last_final is not None:
            return self._last_final
        empty = _Encounter(self._zone or "Encounter", self._zone,
                           self._clock())
        return self._snapshot(empty, self._clock(), active=False)

    def overlay_rows(self, snapshot=None) -> list:
        """Return overlay rows in descending ENCDPS order with name, job, DPS, damage
        share, HPS, local flag and deaths. A supplied snapshot can describe a finished pull.
        Without one, return no rows outside an encounter.
        """
        if snapshot is None:
            if self.current is None:
                return []
            snapshot = self.snapshot()
        rows = []
        for c in snapshot["Combatant"].values():
            rows.append([c["name"], c["Job"], round(c["encdps"], 1),
                         round(c["damage%"], 1), round(c["enchps"], 1),
                         c["is_self"], c["deaths"]])
        rows.sort(key=lambda r: r[2], reverse=True)
        return rows[:MAX_OVERLAY_ROWS]
