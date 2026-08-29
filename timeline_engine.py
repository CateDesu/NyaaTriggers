"""Timeline engine. Drives cactbot-format fight timelines.

Clock lifecycle
  - starts on the first non-player ability, log types 20/21/22, after a zone-in
  - resets on ActorControl command 4000000F, the instance wipe/reset, on a
    zone change, or when the feed drops

Sync
  When an incoming log line matches an entry's event type and fields within
  its acceptance window, the clock snaps so the current fight time equals
  entry.time, or entry.jump if set. All entries before the new time get
  marked fired without speaking TTS.
"""

import re
import time as _time
from typing import TYPE_CHECKING

from PyQt6.QtCore import QObject, QTimer, pyqtSignal

from trigger_engine import compile_user_regex, _safe_fullmatch
from drop_log import log_drop

if TYPE_CHECKING:
    from timeline_parser import TimelineEntry

# cactbot event type -> ACT log line types plus netregex key -> field index.
# Keys absent from the index map are ignored rather than failing the match,
# so timelines using fields we don't track still sync on the ones we do.
_SYNC_TYPES: dict[str, tuple[tuple[str, ...], dict[str, int]]] = {
    "Ability":          (("21", "22"), {"id": 4, "source": 3}),
    "StartsUsing":      (("20",),      {"id": 4, "source": 3}),
    "ActorControl":     (("33",),      {"command": 3}),
    "GainsEffect":      (("26",),      {"effectId": 2, "effect": 3, "source": 6, "target": 8}),
    "LosesEffect":      (("30",),      {"effectId": 2, "effect": 3, "source": 6, "target": 8}),
    "InCombat":         (("260",),     {"inACTCombat": 2, "inGameCombat": 3}),
    "AddedCombatant":   (("03",),      {"id": 2, "name": 3}),
    "RemovedCombatant": (("04",),      {"id": 2, "name": 3}),
    "GameLog":          (("00",),      {"code": 2, "line": 4}),
    "NameToggle":       (("34",),      {"id": 2, "name": 3, "toggle": 6}),
    "SystemLogMessage": (("41",),      {"id": 3}),
    "HeadMarker":       (("27",),      {"targetId": 2, "target": 3, "id": 6}),
}


def _check_sync_type_collisions() -> None:
    """Startup guard. Two rows claiming the same log line type with different
    field-index maps mis-index one of them. The dropped ActorControlSelf row
    collided with NameToggle on 34, which cactbot defines as id/name/toggle.
    Sharing a type with an identical map is fine."""
    claimed: dict[str, tuple[str, dict[str, int]]] = {}
    for name, (types, idx_map) in _SYNC_TYPES.items():
        for lt in types:
            prev = claimed.get(lt)
            assert prev is None or prev[1] == idx_map, (
                f"_SYNC_TYPES: log type {lt} claimed by both {prev[0]} and "
                f"{name} with different field-index maps")
            claimed[lt] = (name, idx_map)


_check_sync_type_collisions()


def _field_matches(pattern: str, value: str) -> bool:
    """cactbot netregex values are regex sources. GameLog line syncs end in
    '.*?' for example. Plain ids/names are regexes that match themselves.
    Fall back to a case-insensitive literal compare if the pattern doesn't
    compile or is rejected by the catastrophic-backtracking guard. Compiled
    once per pattern via the shared cache. This runs for every log line."""
    rx = compile_user_regex(pattern, re.IGNORECASE)
    if rx is None:
        # No regex engine installed, or the pattern was refused. A metachar
        # pattern compared literally never matches, say 6DA[2-9A-D], leaving
        # the sync dead. Say so once per pattern instead of drifting in silence.
        if pattern not in _fallback_logged:
            _fallback_logged.add(pattern)
            log_drop("timeline-sync",
                     f"sync pattern {pattern!r} refused or regex module missing; "
                     "literal compare now, metachars never match")
        return pattern.upper() == value.upper()
    return _safe_fullmatch(rx, value) is not None


# Patterns already reported on the literal-compare fallback above.
_fallback_logged: set[str] = set()

# Director update command that signals a wipe/reset in instanced content
_WIPE_COMMAND = "4000000F"


class TimelineEngine(QObject):
    """Drives a cactbot-format fight timeline against incoming log lines."""
    tts          = pyqtSignal(str)   # label to speak
    phase_update = pyqtSignal(str, float)  # label, fight_time, for UI

    def __init__(self, parent=None):
        super().__init__(parent)
        self._entries: list["TimelineEntry"] = []
        self._active = False
        self._t0: float = 0.0
        self._fired: set[int] = set()

        self._timer = QTimer(self)
        self._timer.setInterval(50)
        self._timer.timeout.connect(self._tick)

    # ── Public API ────────────────────────────────────────────────────────

    def load(self, entries: list["TimelineEntry"]) -> None:
        self.reset()
        self._entries = entries
        # An entry whose type is missing from _SYNC_TYPES never syncs. Name
        # them once at load instead of leaving a silently drifting clock.
        unsupported = sorted({e.event_type for e in entries
                              if e.event_type and e.event_type not in _SYNC_TYPES})
        if unsupported:
            log_drop("timeline-sync",
                     "unsupported sync types, those entries never sync: "
                     + ", ".join(unsupported))
        # Old-style sync /regex/ entries parse as display-only. Name them once
        # too, same silent-drift hazard as an unknown event type.
        legacy = sum(1 for e in entries if e.legacy_sync and not e.event_type)
        if legacy:
            log_drop("timeline-sync",
                     f"{legacy} entr{'y uses' if legacy == 1 else 'ies use'} "
                     "old-style sync /regex/, unsupported; they never sync")

    def clear(self) -> None:
        self.reset()
        self._entries = []

    def start(self) -> None:
        self._t0 = _time.monotonic()
        self._fired.clear()
        self._active = True
        self._timer.start()

    def reset(self) -> None:
        self._active = False
        self._timer.stop()
        self._fired.clear()

    def feed_status_changed(self, connected: bool, _msg: str = "") -> None:
        """Handler for the feed's status_changed signal. WSClient emits
        connected, message. A dead feed delivers no more sync lines and no
        wipe ActorControl, so without this the 50 ms clock keeps speaking
        stale entries and can carry into the next pull. The zone handler
        early-returns on an unchanged zone, so a reconnect in the same zone
        never reloads. Resetting on disconnect is the unambiguous half.
        No combat-end or death stop on purpose, since fights have
        out-of-combat intermissions."""
        if not connected:
            self.reset()

    def current_time(self) -> float:
        return (_time.monotonic() - self._t0) if self._active else 0.0

    def is_active(self) -> bool:
        """True while the fight clock runs. Started on the first non-player
        combat action, stopped by reset. Wipe, zone change, feed loss,
        local toggle-off. The plugin link gates its clock push on this."""
        return self._active

    def upcoming(self) -> list[tuple[float, str]]:
        """The full schedule as time, label pairs for a consumer that draws
        it, which filters past entries against its own clock."""
        return [(e.time, e.label) for e in self._entries
                if e.label and not e.is_internal]

    def process_line(self, fields: list[str]) -> None:
        if not self._entries or not fields:
            return

        if not self._active:
            if self._is_combat_start(fields):
                self.start()
                self._check_syncs(fields)
            return

        if self._is_wipe(fields):
            self.reset()
            return

        self._check_syncs(fields)

    # ── Matching ──────────────────────────────────────────────────────────

    def _is_combat_start(self, fields: list[str]) -> bool:
        # InCombat 260 means game combat flipped on. It's the only combat start
        # a non-casting target like a striking dummy ever produces.
        if fields[0] == "260":
            return len(fields) > 3 and fields[3] == "1"
        if fields[0] not in ("20", "21", "22"):
            return False
        src_id = fields[2] if len(fields) > 2 else ""
        # Player entity IDs begin with 1 in FFXIV. Skip players and empty IDs.
        return bool(src_id) and not src_id.upper().startswith("1")

    def _is_wipe(self, fields: list[str]) -> bool:
        # ActorControl 33 is type|ts|instance|command|data0|...
        # The wipe command is at index 3, the command field, not data0.
        return (
            fields[0] == "33"
            and len(fields) > 3
            and fields[3].upper() == _WIPE_COMMAND
        )

    def _entry_matches(self, entry: "TimelineEntry", fields: list[str]) -> bool:
        spec = _SYNC_TYPES.get(entry.event_type)
        if spec is None:
            return False
        allowed, idx_map = spec
        if fields[0] not in allowed:
            return False
        for key, pattern in entry.event_fields.items():
            idx = idx_map.get(key)
            if idx is None:
                continue
            if len(fields) <= idx or not _field_matches(pattern, fields[idx]):
                return False
        return True

    # ── Clock / sync ──────────────────────────────────────────────────────

    def _check_syncs(self, fields: list[str]) -> None:
        # Does NOT skip already-fired entries on purpose. An entry is fired
        # by the tick the moment its nominal time passes, but its sync must
        # stay armed for the whole window so a line arriving late can still
        # snap the clock backwards. Otherwise a slow-running fight drifts
        # permanently ahead.
        #
        # Among ALL matching in-window entries pick the one whose nominal time is
        # closest to the current clock, not the first. First-match-wins let an
        # already-fired earlier entry with an overlapping window steal a line
        # meant for a later entry sharing the same ability id, snapping the
        # clock backwards and re-speaking the earlier callout.
        t = self.current_time()
        best_i = best_entry = None
        best_dist = None
        for i, entry in enumerate(self._entries):
            if not entry.event_type:
                continue
            lo = entry.time - entry.window_before
            hi = entry.time + entry.window_after
            if not (lo <= t <= hi):
                continue
            if not self._entry_matches(entry, fields):
                continue
            dist = abs(entry.time - t)
            if best_dist is None or dist < best_dist:
                best_dist, best_i, best_entry = dist, i, entry
        if best_entry is None:
            return
        target = best_entry.jump if best_entry.jump is not None else best_entry.time
        # Fire BEFORE snapping. A forward jump marks every entry before `target`,
        # this one included, as already-spoken, which would otherwise swallow
        # this entry's own phase-change callout. _fire is idempotent, so a line
        # that arrives after the tick already spoke this entry is a no-op.
        self._fire(best_i, best_entry)
        # cactbot stops the timeline when a sync jumps to time 0, instead of
        # replaying the whole file from the top, and a synced forcejump is no
        # exception. Only the tick path loops a forcejump 0, firing on time
        # alone when no sync line ever arrives.
        if best_entry.jump is not None and best_entry.jump == 0:
            self.reset()
            return
        self._snap(target, keep_fired=best_i)

    def _snap(self, target: float, keep_fired: "int | None" = None) -> None:
        """Shift the clock so current_time == target, preserving what has
        already been spoken. Entries before `target` are marked spoken, since
        a forward jump or sync must not suddenly announce a burst of skipped-
        past callouts. A backward jump, meaning a phase loop to an earlier
        time, re-arms every entry later than `target` so they speak again as
        the clock re-crosses them. On a backward snap entries at or before
        `target` stay or become spoken, the same cutoff cactbot's SyncTo
        uses when it skips text at or below the sync point. A duplicate
        sync line, say the second target of a multi-target AoE landing
        after the clock passed the entry, can then no longer re-speak
        companion callouts sharing its timestamp.

        `keep_fired` is the entry just matched and spoken by _check_syncs. A
        jump points before its own entry, so the re-arm would drop it and a
        repeat of the jump line would speak it again. Keeping it fired is the
        difference between a genuine loop and a re-sync on the entry we just
        handled."""
        old_t = self.current_time()
        self._t0 = _time.monotonic() - target
        if target < old_t:
            self._fired = {i for i in self._fired if self._entries[i].time <= target}
            # cactbot's SyncTo skips texts at or below the sync point, so an
            # unspoken entry exactly at the target counts as spoken too.
            # Left armed, the next tick would speak it the moment the clock
            # lands there.
            self._fired |= {i for i, e in enumerate(self._entries)
                            if e.time == target}
        elif target > old_t:
            skipped = [e.label for i, e in enumerate(self._entries)
                       if e.time < target and i not in self._fired
                       and i != keep_fired and not e.is_internal]
            if skipped:
                log_drop("timeline-sync",
                         f"forward snap {old_t:.1f}s -> {target:.1f}s skipped "
                         f"{len(skipped)} entr{'y' if len(skipped) == 1 else 'ies'}: "
                         + ", ".join(skipped[:4])[:120])
        self._fired |= {i for i, e in enumerate(self._entries) if e.time < target}
        if keep_fired is not None:
            self._fired.add(keep_fired)

    # ── Tick ──────────────────────────────────────────────────────────────

    def _tick(self) -> None:
        if not self._active:
            return
        t = self.current_time()
        for i, entry in enumerate(self._entries):
            if i in self._fired or entry.time > t:
                continue
            self._fire(i, entry)
            if entry.force_jump and entry.jump is not None:
                # forcejump means jump when the timeline reaches this point
                # even if no sync line arrived. _snap rewrote _fired, so stop
                # iterating with the stale clock. The next tick continues from
                # the target. A backward forcejump re-arms this entry so the
                # loop repeats every pass like cactbot's. A jump to its own
                # time or later keeps it fired, else it re-fires every tick.
                self._snap(entry.jump,
                           keep_fired=i if entry.jump >= entry.time else None)
                return

    def _fire(self, idx: int, entry: "TimelineEntry") -> None:
        if idx in self._fired:
            return                       # idempotent, never speak an entry twice
        self._fired.add(idx)
        if not entry.is_internal and entry.label:
            self.tts.emit(entry.label)
        self.phase_update.emit(entry.label, entry.time)
