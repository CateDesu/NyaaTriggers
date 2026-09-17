"""Track one active trigger sequence. The caller replaces existing runners before starting
another. Fire only after all steps complete within their timeouts.
"""

import re

from PyQt6.QtCore import QObject, QTimer

from nyaatriggers.trigger_engine import (
    _ABILITY_IDX, _ID_IDX, _SOURCE_IDX, _TARGET_IDX, _id_set, _safe_search,
    _str_or, compile_user_regex,
)


class SequentialRunner(QObject):

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

    def try_advance(self, fields: list[str]) -> bool:
        """Return True if all sequence steps are now complete."""
        if not fields:
            return False
        if self._step >= len(self.trigger.sequence):
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
            self._on_complete(self, self._captured)
            return True

        self._arm_timer()
        return False

    def cancel(self) -> None:
        self._timer.stop()

    def _arm_timer(self) -> None:
        timeout_s = self.trigger.sequence[self._step].get("timeout_s")
        # Use ten seconds for invalid, nonfinite or sub-millisecond timeouts instead of
        # creating an immediately expiring timer.
        try:
            timeout_ms = 10000 if timeout_s is None else int(float(timeout_s) * 1000)
        except (TypeError, ValueError, OverflowError):
            timeout_ms = 10000
        if not 1 <= timeout_ms <= 2**31 - 1:
            timeout_ms = 10000
        self._timer.start(timeout_ms)

    def _expire(self) -> None:
        self._on_expire(self)
