"""Wait for follow-up events and the callout delay before firing a trigger."""

import math
import re
import time

from PyQt6.QtCore import QObject, QTimer, Qt

from nyaatriggers.drop_log import log_drop
from nyaatriggers.trigger_engine import (
    _ABILITY_IDX, _ID_IDX, _SOURCE_IDX, _TARGET_IDX, _id_set, _safe_search,
    _str_or, compile_user_regex,
)


class SequentialRunner(QObject):

    def __init__(self, trigger, captured: dict,
                 on_complete, on_expire, parent=None, cooldown_key=""):
        super().__init__(parent)
        self.trigger = trigger
        self._captured = dict(captured)
        self._on_complete = on_complete
        self._on_expire = on_expire
        self.cooldown_key = cooldown_key
        self._cancelled = False
        self._delay_deadline = None
        self._step_deadline = None
        self._step = 0  # index into trigger.sequence, step 0 is the first subsequent step
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._timer.timeout.connect(self._expire)
        self._arm_timer()

    def try_advance(self, fields: list[str]) -> bool:
        """Return True when the final step fires the callout without a delay."""
        if self._cancelled or not fields:
            return False
        if self._step >= len(self.trigger.sequence):
            return False
        if time.monotonic() >= self._step_deadline:
            self._expire()
            return False
        step = self.trigger.sequence[self._step]
        # Normalize step log types using the same default and whitespace handling as
        # Trigger.from_dict.
        log_type = _str_or(step.get("log_type"), "20").strip() or "20"
        # For alternative log types, select this line's layout before reading field
        # indices.
        if "|" in log_type:
            if fields[0] not in (p.strip() for p in log_type.split("|")):
                return False
            log_type = fields[0]
        elif fields[0] != log_type:
            return False

        ability_id = str(step.get("ability_id", "") or "")
        ability_regex = str(step.get("ability_regex", "") or "")
        # Ignore ability IDs for line types without an ID field and use regex matching
        # instead.
        if ability_id and log_type in _ID_IDX:
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
            if self.trigger.delay_s > 0:
                self._arm_delay()
                return False
            self._on_complete(self, self._captured)
            return True

        self._arm_timer()
        return False

    def cancel(self) -> None:
        self._cancelled = True
        self._timer.stop()

    def _arm_timer(self) -> None:
        if self._step >= len(self.trigger.sequence):
            self._arm_delay()
            return
        timeout_s = self.trigger.sequence[self._step].get("timeout_s")
        # Use ten seconds for invalid, nonfinite or sub-millisecond timeouts instead of
        # creating an immediately expiring timer.
        try:
            timeout_ms = 10000 if timeout_s is None else int(float(timeout_s) * 1000)
        except (TypeError, ValueError, OverflowError):
            timeout_ms = 10000
        if not 1 <= timeout_ms <= 2**31 - 1:
            timeout_ms = 10000
        self._step_deadline = time.monotonic() + timeout_ms / 1000
        self._start_timer(self._step_deadline)

    def _arm_delay(self) -> None:
        self._delay_deadline = time.monotonic() + self.trigger.delay_s
        self._start_timer(self._delay_deadline)

    def _start_timer(self, deadline: float) -> None:
        remaining = max(0.0, deadline - time.monotonic())
        self._timer.start(math.ceil(min(remaining * 1000, 2**31 - 1)))

    def _expire(self) -> None:
        if self._cancelled:
            return
        try:
            deadline = self._delay_deadline if self._delay_deadline is not None else self._step_deadline
            if time.monotonic() < deadline:
                self._start_timer(deadline)
            elif self._delay_deadline is not None:
                self._on_complete(self, self._captured)
            else:
                self._on_expire(self)
        except Exception as exc:
            self.cancel()
            log_drop("trigger-sequence", f"{self.trigger.name!r}: {exc!r}")
