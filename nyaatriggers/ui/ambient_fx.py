"""Sakura scenery and drifting petals for MainWindow. Pause animation while unfocused and
resume from the same frame.
"""

import time

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QColor, QPainter

from nyaatriggers import theme


class AmbientFxMixin:
    def _init_ambient_fx(self) -> None:
        # Scale the tree stroke to match the sidebar while placing it at the right edge.
        self._sakura = theme.make_tree(41, 380, 900, inward=-1, crisp=True,
                                       stroke_scale=216 / 380, lift=0.02)
        self._petals = theme.make_petals(11, 14)
        self._fx_active = False
        self._fx_start = time.monotonic()
        self._fx_freeze_t = None   # petal clock value while parked
        self._fx_last_t = None   # petal clock value at the previous tick
        self._fx_timer = QTimer(self)
        self._fx_timer.setInterval(50)   # About 20 frames per second.
        self._fx_timer.timeout.connect(self._fx_tick)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        self._start_fx()

    def _start_fx(self) -> None:
        if not self._fx_active:
            self._fx_active = True
            if self._fx_freeze_t is not None:
                self._fx_start = time.monotonic() - self._fx_freeze_t
                self._fx_freeze_t = None
            else:
                self._fx_start = time.monotonic()
            self._fx_last_t = None
            self._fx_timer.start()
            if hasattr(self, "_sidebar"):
                side = self._sidebar
                side.awake = True
                side._last_t = None
                if side.freeze_t is not None:
                    side._t0 = time.monotonic() - side.freeze_t
                    side.freeze_t = None
                side.update()
            self.update()

    def _stop_fx(self) -> None:
        if self._fx_active:
            self._fx_active = False
            self._fx_freeze_t = time.monotonic() - self._fx_start
            self._fx_timer.stop()
            if hasattr(self, "_sidebar"):
                side = self._sidebar
                if side.awake:
                    side.freeze_t = time.monotonic() - side._t0
                side.awake = False
                side.update()
            self.update()

    def _fx_tick(self) -> None:
        # Repaint only the old and new petal areas to avoid updating every widget each
        # frame.
        t = time.monotonic() - self._fx_start
        prev = self._fx_last_t
        if prev is None:
            self.update()
        else:
            for r in theme.petal_rects(self.width(), self.height(), prev, self._petals):
                self.update(r)
            for r in theme.petal_rects(self.width(), self.height(), t, self._petals):
                self.update(r)
        self._fx_last_t = t
        if hasattr(self, "_sidebar"):
            self._sidebar.tick()

    def paintEvent(self, _ev):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(theme.BASE))
        w = self.width()
        h = self.height()
        tree = self._sakura
        p.save()
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        s = h / tree.height()
        p.translate(w - tree.width() * s, 0)
        p.scale(s, s)
        p.drawPixmap(0, 0, tree)
        p.restore()
        if self._fx_active:
            t = time.monotonic() - self._fx_start
        else:
            t = self._fx_freeze_t
        if t is not None:
            theme.paint_petals(p, w, h, t, self._petals)
