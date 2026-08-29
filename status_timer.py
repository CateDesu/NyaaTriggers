"""Status expiry warning timer runner.

A GainsEffect 26 trigger with an ``expiry_warn_s`` lead time gets its callout
scheduled that many seconds before the effect runs out. The "reapply soon"
nudge for an effect you keep up on the target, like Death's Design.

One runner per live effect, keyed by trigger id, effect id, source id, target
id. A matching refresh re-arms the timer with the new duration, REPLACE_OLD.
An early LosesEffect 30 on the same key cancels the pending warning so it
never speaks about an effect that's already gone.
"""

import traceback

from PyQt6.QtCore import QObject, QTimer


class StatusTimerRunner(QObject):
    """Tracks one in-flight status effect and its pending reapply warning."""

    def __init__(self, trigger, captured: dict, effect_id: str, source_id: str,
                 target_id: str, delay_ms: float, on_complete, parent=None):
        super().__init__(parent)
        self.trigger = trigger
        # Hex ids, upper-cased by the caller so refresh/loss matching is
        # case-insensitive.
        self.effect_id = effect_id
        self.source_id = source_id
        self.target_id = target_id
        self.key = (trigger.id, effect_id, source_id, target_id)
        self._captured = dict(captured)
        self._on_complete = on_complete
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._fire)
        try:
            delay = max(0, int(delay_ms))
        except (TypeError, ValueError, OverflowError):
            # an infinite delay overflows the int conversion, treat as fire-now.
            delay = 0
        self._timer.start(delay)

    def matches_loss(self, effect_id: str, source_id: str, target_id: str) -> bool:
        """True when this LosesEffect should cancel the pending warning."""
        return (self.effect_id == effect_id
                and self.source_id == source_id
                and self.target_id == target_id)

    def cancel(self) -> None:
        self._timer.stop()

    def _fire(self) -> None:
        try:
            self._on_complete(self, self._captured)
        except Exception:  # noqa: BLE001 - a bad callout must not abort the app
            traceback.print_exc()
