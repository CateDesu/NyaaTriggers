"""Sequential trigger runner.

When a Trigger has a non-empty `sequence` list, this tracks one in-flight
instance. It waits for each subsequent step within its timeout window, then
fires the trigger's TTS on completion. Concurrent instances use REPLACE_OLD
semantics, so the caller cancels any existing runner before making a new one.
"""

import re

from PyQt6.QtCore import QObject, QTimer

from trigger_engine import (
    _ABILITY_IDX, _ID_IDX, _SOURCE_IDX, _TARGET_IDX, _id_set, _safe_search,
    _str_or, compile_user_regex,
)


class SequentialRunner(QObject):
    """Tracks one in-flight sequential trigger instance."""

    def __init__(self, trigger, captured: dict,
                 on_complete, on_expire, parent=None):
        super().__init__(parent)
        self.trigger = trigger
        self._captured = dict(captured)
        self._on_complete = on_complete
        self._on_expire = on_expire
        self._step = 0  # index into trigger.sequence, step 0 is the first subsequent step
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._expire)
        self._arm_timer()

    # ------------------------------------------------------------------
    def try_advance(self, fields: list[str]) -> bool:
        """Return True if all sequence steps are now complete."""
        if not fields:
            return False
        if self._step >= len(self.trigger.sequence):
            return False
        step = self.trigger.sequence[self._step]
        # Stripped the same way Trigger.from_dict strips the trigger's own
        # log_type. from_dict passes step dicts through untouched, so a hand
        # edited step with leading whitespace would never equal a line's
        # type field and the sequence would expire silently. Whitespace only
        # strips to the same "20" default the load path takes.
        log_type = _str_or(step.get("log_type"), "20").strip() or "20"
        # A step's log_type may be pipe-separated, "21|22", like
        # Trigger.matches. Bind the concrete type of THIS line so the
        # field-index lookups below resolve against its layout.
        if "|" in log_type:
            if fields[0] not in (p.strip() for p in log_type.split("|")):
                return False
            log_type = fields[0]
        elif fields[0] != log_type:
            return False

        ability_id = str(step.get("ability_id", "") or "")
        ability_regex = str(step.get("ability_regex", "") or "")
        # A log type with no _ID_IDX entry, a 00 chat line for one, has no
        # ID field to match. An ability_id there would read field 4, chat
        # text, as a hex ID and strand the step, so ignore it and let the
        # regex do the matching. Same rule Trigger.from_dict loads with.
        if ability_id and log_type in _ID_IDX:
            # Match the hex ID field for this log type, priority over regex.
            id_idx = _ID_IDX[log_type]
            if len(fields) <= id_idx:
                return False
            if fields[id_idx].upper() not in _id_set(ability_id):
                return False
        elif ability_regex:
            idx = _ABILITY_IDX.get(log_type)
            text = fields[idx] if idx is not None and idx < len(fields) else "|".join(fields)
            rx = compile_user_regex(ability_regex, re.IGNORECASE)
            if rx is None or not _safe_search(rx, text):
                return False
        # A step with neither id nor regex advances on any line of its log_type.

        self._timer.stop()
        src_idx = _SOURCE_IDX.get(log_type, 3)
        if len(fields) > src_idx:
            self._captured["source"] = fields[src_idx]
        tgt_idx = _TARGET_IDX.get(log_type, 7)
        if len(fields) > tgt_idx:
            self._captured["target"] = fields[tgt_idx]

        self._step += 1
        if self._step >= len(self.trigger.sequence):
            self._on_complete(self, self._captured)
            return True

        self._arm_timer()
        return False

    def cancel(self) -> None:
        self._timer.stop()

    # ------------------------------------------------------------------
    def _arm_timer(self) -> None:
        timeout_s = self.trigger.sequence[self._step].get("timeout_s")
        # A junk timeout from a hand-edited sequence falls back to 10s
        # instead of crashing the live dispatch loop. int of inf raises
        # OverflowError and Qt coerces a negative interval to 1ms, so an
        # out of range value falls back too. So does anything truncating
        # to 0ms, an instantly expiring timer would kill the sequence on
        # the spot instead of giving it the 10s fallback.
        try:
            timeout_ms = 10000 if timeout_s is None else int(float(timeout_s) * 1000)
        except (TypeError, ValueError, OverflowError):
            timeout_ms = 10000
        if not 1 <= timeout_ms <= 2**31 - 1:
            timeout_ms = 10000
        self._timer.start(timeout_ms)

    def _expire(self) -> None:
        self._on_expire(self)
