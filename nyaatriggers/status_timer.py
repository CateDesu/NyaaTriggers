"""Schedule status warnings before expiry. Track each trigger, effect, source and target
separately. Refresh events reset the timer, and matching loss events cancel it.
"""

import traceback

from PyQt6.QtCore import QObject, QTimer


class StatusTimerRunner(QObject):

    def __init__(self, trigger, captured: dict, effect_id: str, source_id: str,
                 target_id: str, delay_ms: float, on_complete, parent=None):
        super().__init__(parent)
        self.trigger = trigger
        # Callers normalize hex IDs to uppercase for refresh and loss matching.
        self.effect_id = effect_id
        self.source_id = source_id
        self.target_id = target_id
        self.key = (trigger.id, effect_id, source_id, target_id)
        self._captured = dict(captured)
        self._on_complete = on_complete
        self._cancelled = False
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._fire)
        try:
            delay = max(0, int(delay_ms))
        except (TypeError, ValueError, OverflowError):
            # Treat nonfinite delays as immediate warnings.
            delay = 0
        self._timer.start(delay)

    def matches_loss(self, effect_id: str, source_id: str, target_id: str) -> bool:
        """True when this LosesEffect should cancel the pending warning."""
        return (self.effect_id == effect_id
                and self.source_id == source_id
                and self.target_id == target_id)

    def cancel(self) -> None:
        self._cancelled = True
        self._timer.stop()

    def _fire(self) -> None:
        if self._cancelled:
            return
        self.cancel()
        try:
            self._on_complete(self, self._captured)
        except Exception:  # noqa: BLE001
            traceback.print_exc()
