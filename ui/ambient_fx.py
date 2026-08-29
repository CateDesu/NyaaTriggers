"""Ambient effects, sakura tree plus drifting petals overlay.

The right edge tree is static scenery. The petals animate on a timer that
parks the instant the window loses focus and resumes from the frozen frame
when focus comes back. Mixin for MainWindow, all state rides on self.
"""

import time

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QColor, QPainter

import theme


class AmbientFxMixin:
    def _init_ambient_fx(self) -> None:
        # ── Ambient effects, sakura tree plus drifting petals ──
        # The right edge tree uses a crisp crown. Behind the page panels its
        # trunk barely shows, and the soft wash crown read as a big blur.
        # Distinct blossom dots keep it tree like. The tree is static scenery.
        # The petals animate on a ~30fps timer that parks the instant the
        # window loses focus. A parked drift keeps painting the same frame,
        # petals locked in place, and focus resumes it from there.
        # Hugs the right edge. Stroke widths scale with canvas w inside
        # make_tree, so this 380-wide canvas would paint a trunk ~1.8x thicker
        # than the sidebar's 216-wide tree; 216/380 evens them out. The lift
        # nudges the crown slightly higher, level with the sidebar tree's.
        self._sakura = theme.make_tree(41, 380, 900, inward=-1, crisp=True,
                                       stroke_scale=216 / 380, lift=0.02)
        self._petals = theme.make_petals(11, 14)
        self._fx_active = False
        self._fx_start = time.monotonic()
        self._fx_freeze_t = None   # petal clock value while parked
        self._fx_last_t = None   # petal clock value at the previous tick
        self._fx_timer = QTimer(self)
        self._fx_timer.setInterval(50)   # ~20fps. The drift is slow, more frames add nothing
        self._fx_timer.timeout.connect(self._fx_tick)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        self._start_fx()

    def _start_fx(self) -> None:
        if not self._fx_active:
            self._fx_active = True
            # Resume the petal clock where the park left it so the drift
            # continues from the frozen positions instead of jumping.
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
        # Park the drift where it is. The petals keep painting at the frozen
        # clock value, locked in place instead of vanishing.
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
        # Repaint only the strips the petals touch, old frame and new. A full
        # window update here restyles every child widget at 30fps and pins a
        # whole core.
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
        # Paint base first, then the tree and petals on top. The central widget
        # and the page pane are transparent in the stylesheet, so the scenery
        # bleeds through every gap. Solid widgets, table, tree, inputs, sit on
        # top.
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(theme.BASE))
        w = self.width()
        h = self.height()
        tree = self._sakura
        p.save()
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        s = h / tree.height()   # scale to fill the window height, anchored right
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
