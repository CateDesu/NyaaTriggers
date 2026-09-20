"""Run cactbot format timelines against incoming log lines. Combat start or a nonplayer
ability starts the clock. A wipe, zone change or lost feed resets it. Matching sync
entries move the clock and mark callouts at or before the target as fired.
"""

import re
import time as _time
from typing import TYPE_CHECKING

from PyQt6.QtCore import QObject, QTimer, pyqtSignal

from nyaatriggers.trigger_engine import compile_user_regex, _safe_fullmatch
from nyaatriggers.drop_log import log_drop

if TYPE_CHECKING:
    from nyaatriggers.timeline_parser import TimelineEntry

# Map cactbot events and fields to ACT log columns. Reject entries with unmapped fields
# because their constraints cannot be checked.
_SYNC_TYPES: dict[str, tuple[tuple[str, ...], dict[str, int]]] = {
    "Ability":          (("21", "22"), {"id": 4, "source": 3}),
    "StartsUsing":      (("20",),      {"id": 4, "source": 3}),
    "ActorControl":     (("33",),      {"command": 3}),
    "GainsEffect":      (("26",),      {"effectId": 2, "effect": 3, "source": 6, "target": 8}),
    "LosesEffect":      (("30",),      {"effectId": 2, "effect": 3, "source": 6, "target": 8}),
    "InCombat":         (("260",),     {"inACTCombat": 2, "inGameCombat": 3}),
    "AddedCombatant":   (("03",),      {"id": 2, "name": 3}),
    "RemovedCombatant": (("04",),      {"id": 2, "name": 3}),
    "GameLog":          (("00",),      {"code": 2, "name": 3, "line": 4}),
    "NameToggle":       (("34",),      {"id": 2, "name": 3, "toggle": 6}),
    "SystemLogMessage": (("41",),      {"id": 3, "param1": 5}),
    "HeadMarker":       (("27",),      {"targetId": 2, "target": 3, "id": 6}),
    "MapEffect":        (("257",),     {"flags": 3, "location": 4}),
    "BattleTalk2":      (("267",),     {"instanceContentTextId": 5, "npcNameId": 4}),
}
SYNC_LOG_TYPES = frozenset(lt for types, _fields in _SYNC_TYPES.values() for lt in types)


def _check_sync_type_collisions() -> None:
    """Reject conflicting field maps for the same log type. Identical maps may share a
    type.
    """
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
    """Match cached cactbot regexes, falling back to literal comparison if compilation
    fails.
    """
    rx = compile_user_regex(pattern, re.IGNORECASE)
    if rx is None:
        # Log refused patterns once because a literal comparison cannot match regex
        # alternatives.
        if pattern not in _fallback_logged:
            _fallback_logged.add(pattern)
            log_drop("timeline-sync",
                     f"sync pattern {pattern!r} refused or regex module missing; "
                     "literal compare now, metachars never match")
        return pattern.upper() == value.upper()
    return _safe_fullmatch(rx, value) is not None


_fallback_logged: set[str] = set()

# Director update command for an instance wipe or reset.
_WIPE_COMMAND = "4000000F"


class TimelineEngine(QObject):
    tts          = pyqtSignal(str)   # label to speak
    phase_update = pyqtSignal(str, float)  # label, fight_time, for UI

    def __init__(self, parent=None):
        super().__init__(parent)
        self._entries: list["TimelineEntry"] = []
        self._active = False
        self._t0: float = 0.0
        self._fired: set[int] = set()
        self._replay_now = None

        self._timer = QTimer(self)
        self._timer.setInterval(50)
        self._timer.timeout.connect(self._tick)


    def load(self, entries: list["TimelineEntry"], *, preserve_time: bool = False) -> None:
        if preserve_time and self._active:
            now = self.current_time()
            spoken = [self._entries[i] for i in self._fired]
            self._fired = {i for i, entry in enumerate(entries)
                           if entry.time <= now or entry in spoken}
        else:
            self.reset()
        self._entries = entries
        # Report unsupported event types once when loading the timeline.
        unsupported = sorted({e.event_type for e in entries
                              if e.event_type and e.event_type not in _SYNC_TYPES})
        if unsupported:
            log_drop("timeline-sync",
                     "unsupported sync types, those entries never sync: "
                     + ", ".join(unsupported))
        # Unmapped fields prevent an entry from syncing. Report them at load time.
        unmapped = sorted({f"{e.event_type}.{key}"
                           for e in entries if e.event_type in _SYNC_TYPES
                           for key in e.event_fields
                           if key not in _SYNC_TYPES[e.event_type][1]})
        if unmapped:
            log_drop("timeline-sync",
                     "unsupported sync fields, those entries never sync: "
                     + ", ".join(unmapped))
        # Legacy regex syncs are display only, so report those too.
        legacy = sum(1 for e in entries if e.legacy_sync and not e.event_type)
        if legacy:
            log_drop("timeline-sync",
                     f"{legacy} entr{'y uses' if legacy == 1 else 'ies use'} "
                     "old-style sync /regex/, unsupported; they never sync")

    def clear(self) -> None:
        self.reset()
        self._entries = []

    def start(self) -> None:
        self._t0 = self._now()
        self._fired.clear()
        self._active = True
        self._timer.start()

    def reset(self) -> None:
        self._active = False
        self._timer.stop()
        self._fired.clear()

    def resume(self, events: list[tuple[float, list[str]]]) -> None:
        """Replay timing without speaking missed cues."""
        blocked = self.blockSignals(True)
        try:
            self.reset()
            for observed, fields in events:
                self._advance_replay(observed)
                self.process_line(fields)
            self._advance_replay(_time.monotonic())
        finally:
            self._replay_now = None
            self.blockSignals(blocked)

    def _advance_replay(self, now: float) -> None:
        seen = {}
        while self._active:
            jump = next((i for i, entry in enumerate(self._entries)
                         if i not in self._fired and entry.force_jump
                         and entry.jump is not None and self._t0 + entry.time <= now), None)
            if jump is None:
                break
            self._replay_now = max(self._now(), self._t0 + self._entries[jump].time)
            state = (jump, frozenset(self._fired))
            previous = seen.get(state)
            if previous is not None:
                period = self._replay_now - previous
                if period <= 0:
                    break
                skipped = ((now - self._replay_now) // period) * period
                self._replay_now += skipped
                self._t0 += skipped
            seen[state] = self._replay_now
            self._tick()
        self._replay_now = now
        self._tick()

    def _now(self) -> float:
        return _time.monotonic() if self._replay_now is None else self._replay_now

    def feed_status_changed(self, connected: bool, _msg: str = "") -> None:
        """Reset on feed loss to stop stale callouts. Combat ending does not reset the
        clock because fights can have intermissions.
        """
        if not connected:
            self.reset()

    def current_time(self) -> float:
        return (self._now() - self._t0) if self._active else 0.0

    def is_active(self) -> bool:
        """Whether the fight clock is running."""
        return self._active

    def has_schedule(self) -> bool:
        return bool(self._entries)

    def upcoming(self) -> list[tuple[float, str]]:
        """Return the full display schedule as time and label pairs."""
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


    @staticmethod
    def _is_combat_start(fields: list[str]) -> bool:
        # InCombat 260 also starts the clock for targets such as striking dummies that
        # never cast.
        if fields[0] == "260":
            return len(fields) > 3 and fields[3] == "1"
        if fields[0] not in ("20", "21", "22"):
            return False
        src_id = fields[2] if len(fields) > 2 else ""
        # Player entity IDs begin with 1. Skip those and empty IDs.
        return bool(src_id) and not src_id.upper().startswith("1")

    def _is_wipe(self, fields: list[str]) -> bool:
        # ActorControl stores the wipe command at index 3, before data0.
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
                # Reject constraints whose fields cannot be checked.
                return False
            if len(fields) <= idx or not _field_matches(pattern, fields[idx]):
                return False
        return True


    def _check_syncs(self, fields: list[str]) -> None:
        # Keep syncs armed for their full window after a callout fires so late lines can
        # correct the clock. Choose the nearest matching entry when windows overlap, or
        # an earlier use of the same ability could pull the clock back.
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
        # Fire the callout before a forward snap marks it as skipped. _fire ignores
        # entries already spoken.
        self._fire(best_i, best_entry)
        # A sync jump to zero stops the timeline. Only a forcejump reached by the clock
        # loops to zero.
        if best_entry.jump is not None and best_entry.jump == 0:
            self.reset()
            return
        self._snap(target, keep_fired=best_i)

    def _snap(self, target: float, keep_fired: "int | None" = None) -> None:
        """Move the clock to target. Mark entries at or before target as fired to skip
        missed callouts. A backward jump rearms later entries. keep_fired preserves the
        entry that caused the jump so duplicate sync lines cannot speak it again.
        """
        old_t = self.current_time()
        self._t0 = self._now() - target
        if target < old_t:
            self._fired = {i for i in self._fired if self._entries[i].time <= target}
            # Cactbot skips callouts at the sync point as well as those before it.
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


    def _tick(self) -> None:
        if not self._active:
            return
        t = self.current_time()
        for i, entry in enumerate(self._entries):
            if i in self._fired or entry.time > t:
                continue
            self._fire(i, entry)
            if entry.force_jump and entry.jump is not None:
                # The jump changes the clock, so resume iteration on the next tick.
                # Backward jumps rearm this entry for another loop. Other jumps keep it
                # fired to avoid repeating every tick.
                self._snap(entry.jump,
                           keep_fired=i if entry.jump >= entry.time else None)
                return

    def _fire(self, idx: int, entry: "TimelineEntry") -> None:
        if idx in self._fired:
            return
        self._fired.add(idx)
        if not entry.is_internal and entry.label:
            self.tts.emit(entry.label)
        self.phase_update.emit(entry.label, entry.time)
