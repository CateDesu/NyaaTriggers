from functools import lru_cache

BASE     = "#0a0a0c"   # window background
PANEL    = "#101013"   # Sidebar and list backgrounds
MANTLE   = "#18181d"   # Inputs and hovered controls
SURFACE2 = "#26262e"   # Raised surfaces and borders
EDGE     = "#26262e"
OVERLAY0 = "#3a3a44"   # Muted hover colour

ACCENT    = "#ff8399"
ACCENT2   = "#e66c82"
ON_ACCENT = "#2b1017"   # dark text on accent fills

TEXT     = "#e8e8ec"
SUBTEXT1 = "#8f8f9a"
SUBTEXT0 = "#6a6a74"
SUBTEXT_SOFT = "#b0b0be"   # Inactive navigation, kana and version labels

OK       = "#a6e3a1"
ERR      = "#f38ba8"
MAUVE    = "#cba6f7"

GRAD_FROM = ACCENT
GRAD_TO   = MAUVE

PILL = 14
SOFT = 6


STYLESHEET = f"""

QWidget {{
    background-color: {BASE};
    color: {TEXT};
    font-size: 10pt;
}}
QMainWindow, QDialog {{
    background-color: {BASE};
}}

/* Transparent containers reveal the window scenery. */
QWidget#root,
QWidget#auroraPage,
QWidget#contentCol,
QStackedWidget#pageStack,
QTabWidget::pane {{
    background-color: transparent;
    border: none;
}}


QFrame#sidebar {{
    background-color: {PANEL};
    border-right: 1px solid {EDGE};
}}
QLabel#brandKana {{
    color: {SUBTEXT_SOFT};
    font-family: "Kosugi Maru";
    font-size: 10pt;
    margin-left: 2px;
}}
QLabel#brand {{
    color: {ACCENT};
    font-family: "Kosugi Maru";
    font-size: 17pt;
    font-weight: bold;
}}
QLabel#brandVer {{
    color: {SUBTEXT_SOFT};
    font-family: "Kosugi Maru";
    font-size: 11pt;
}}
QPushButton#navItem {{
    background-color: transparent;
    color: {SUBTEXT1};
    font-family: "Kosugi Maru";
    border: 1px solid transparent;
    border-radius: 8px;
    padding: 8px 12px;
    text-align: left;
    font-size: 15pt;
}}
QPushButton#navItem:hover:!checked {{
    background-color: rgba(255, 255, 255, 10);
    color: {TEXT};
}}
QPushButton#navItem:checked {{
    background-color: rgba(255, 131, 153, 10);
    color: {ACCENT};
    font-weight: bold;
    border: 1px solid transparent;
}}


QTabBar {{ background: transparent; }}
QTabWidget::pane {{
    border-top: 1px solid {EDGE};
}}
QTabBar::tab {{
    background-color: transparent;
    color: {SUBTEXT1};
    padding: 8px 18px;
    border: none;
    border-bottom: 2px solid transparent;
    margin: 0 2px;
}}
QTabBar::tab:selected {{
    color: {ACCENT};
    border-bottom: 2px solid {ACCENT};
}}
QTabBar::tab:hover:!selected {{
    color: {TEXT};
}}


QPushButton {{
    background-color: {MANTLE};
    color: {TEXT};
    border: 1px solid {EDGE};
    border-radius: {PILL}px;
    padding: 5px 14px;
}}
QPushButton:hover {{
    border-color: {OVERLAY0};
    color: {TEXT};
}}
QPushButton:pressed {{
    background-color: {SURFACE2};
}}
QPushButton:disabled {{
    background-color: {PANEL};
    color: {OVERLAY0};
    border-color: {PANEL};
}}

QPushButton#primary {{
    background-color: qlineargradient(x1:0, y1:0, x2:1, y2:1,
        stop:0 {GRAD_FROM}, stop:1 {GRAD_TO});
    color: {ON_ACCENT};
    border: none;
    font-weight: bold;
    padding: 6px 16px;
}}
QPushButton#primary:hover {{
    background-color: qlineargradient(x1:0, y1:0, x2:1, y2:1,
        stop:0 {ACCENT2}, stop:1 {GRAD_TO});
}}
QPushButton#primary:pressed {{
    background-color: {ACCENT2};
}}


QLineEdit, QPlainTextEdit, QTextEdit {{
    background-color: {MANTLE};
    color: {TEXT};
    border: 1px solid {EDGE};
    border-radius: {PILL}px;
    padding: 5px 12px;
    selection-background-color: {ACCENT};
    selection-color: {ON_ACCENT};
}}
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus {{
    border-color: {ACCENT};
}}


QDoubleSpinBox, QSpinBox {{
    background-color: {MANTLE};
    color: {TEXT};
    border: 1px solid {EDGE};
    border-radius: {PILL}px;
    padding: 4px 10px;
}}
QDoubleSpinBox:focus, QSpinBox:focus {{
    border-color: {ACCENT};
}}
QDoubleSpinBox::up-button, QSpinBox::up-button,
QDoubleSpinBox::down-button, QSpinBox::down-button {{
    background-color: transparent;
    border: none;
    width: 16px;
}}
QDoubleSpinBox::up-button:hover, QSpinBox::up-button:hover,
QDoubleSpinBox::down-button:hover, QSpinBox::down-button:hover {{
    background-color: {SURFACE2};
}}


QComboBox {{
    background-color: {MANTLE};
    color: {TEXT};
    border: 1px solid {EDGE};
    border-radius: {PILL}px;
    padding: 4px 12px;
}}
QComboBox:focus {{
    border-color: {ACCENT};
}}
QComboBox::drop-down {{
    background-color: {SURFACE2};
    border: none;
    border-top-right-radius: {PILL}px;
    border-bottom-right-radius: {PILL}px;
    width: 22px;
}}
QComboBox QAbstractItemView {{
    background-color: {PANEL};
    color: {TEXT};
    border: 1px solid {EDGE};
    selection-background-color: {MANTLE};
    selection-color: {ACCENT};
    outline: none;
    padding: 4px;
}}


QTableWidget {{
    background-color: {PANEL};
    alternate-background-color: {MANTLE};
    color: {TEXT};
    gridline-color: {EDGE};
    border: 1px solid {EDGE};
    border-radius: {SOFT}px;
    outline: 0;
}}
QTableWidget::item {{
    padding: 4px 6px;
}}
QTableWidget::item:selected {{
    background-color: {MANTLE};
    color: {ACCENT};
    border-left: 2px solid {ACCENT};
}}
QTableWidget::item:hover {{
    background-color: {MANTLE};
}}
QHeaderView::section {{
    background-color: {MANTLE};
    color: {SUBTEXT1};
    border: none;
    border-right: 1px solid {EDGE};
    border-bottom: 1px solid {EDGE};
    padding: 6px 8px;
    font-weight: bold;
    font-size: 9pt;
}}


QTreeWidget {{
    background-color: {PANEL};
    color: {TEXT};
    border: 1px solid {EDGE};
    border-radius: {SOFT}px;
    outline: 0;
}}
QTreeWidget::item {{
    padding: 6px 4px;
    border: 0;
}}
QTreeWidget::item:selected {{
    background-color: {MANTLE};
    color: {ACCENT};
    border-left: 2px solid {ACCENT};
}}
QTreeWidget::item:hover:!selected {{
    background-color: {MANTLE};
    color: {TEXT};
}}
QTreeWidget::branch {{
    background-color: {PANEL};
}}


QListWidget {{
    background-color: {PANEL};
    color: {TEXT};
    border: 1px solid {EDGE};
    border-radius: {SOFT}px;
    outline: 0;
}}
QListWidget::item {{
    padding: 6px 8px;
}}
QListWidget::item:selected {{
    background-color: {MANTLE};
    color: {ACCENT};
    border-left: 2px solid {ACCENT};
}}
QListWidget::item:hover:!selected {{
    background-color: {MANTLE};
}}


QCheckBox {{
    color: {TEXT};
    spacing: 8px;
    background-color: transparent;
}}
QCheckBox::indicator {{
    width: 16px;
    height: 16px;
    border: 1px solid {SURFACE2};
    border-radius: 4px;
    background-color: {MANTLE};
}}
QCheckBox::indicator:checked {{
    background-color: qlineargradient(x1:0, y1:0, x2:1, y2:1,
        stop:0 {GRAD_FROM}, stop:1 {GRAD_TO});
    border-color: transparent;
}}
QCheckBox::indicator:hover {{
    border-color: {ACCENT};
}}


QScrollBar:vertical {{
    background-color: transparent;
    width: 10px;
    margin: 2px;
}}
QScrollBar::handle:vertical {{
    background-color: {SURFACE2};
    border-radius: 4px;
    min-height: 30px;
}}
QScrollBar::handle:vertical:hover {{
    background-color: {OVERLAY0};
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0;
}}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
    background: transparent;
}}
QScrollBar:horizontal {{
    background-color: transparent;
    height: 10px;
    margin: 2px;
}}
QScrollBar::handle:horizontal {{
    background-color: {SURFACE2};
    border-radius: 4px;
    min-width: 30px;
}}
QScrollBar::handle:horizontal:hover {{
    background-color: {OVERLAY0};
}}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
    width: 0;
}}
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {{
    background: transparent;
}}


QSplitter::handle {{
    background-color: {EDGE};
}}
QSplitter::handle:hover {{
    background-color: {ACCENT};
}}
QSplitter::handle:horizontal {{ width: 1px; }}
QSplitter::handle:vertical {{ height: 1px; }}


QMenu {{
    background-color: {PANEL};
    color: {TEXT};
    border: 1px solid {EDGE};
    border-radius: {SOFT}px;
    padding: 6px;
}}
QMenu::item {{
    padding: 6px 24px 6px 12px;
    border-radius: 4px;
}}
QMenu::item:selected {{
    background-color: {MANTLE};
    color: {ACCENT};
}}
QMenu::separator {{
    height: 1px;
    background-color: {EDGE};
    margin: 4px 8px;
}}


QLabel {{
    background-color: transparent;
    color: {TEXT};
}}
QFormLayout QLabel {{
    color: {SUBTEXT1};
}}


QDialogButtonBox QPushButton {{
    min-width: 80px;
}}


QGroupBox {{
    color: {ACCENT};
    border: 1px solid {EDGE};
    border-radius: {SOFT}px;
    margin-top: 12px;
    padding-top: 8px;
    font-weight: bold;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    subcontrol-position: top left;
    padding: 0 6px;
    left: 10px;
}}


QProgressBar {{
    background-color: {MANTLE};
    border: 1px solid {EDGE};
    border-radius: {PILL}px;
    text-align: center;
    color: {TEXT};
}}
QProgressBar::chunk {{
    background-color: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 {GRAD_FROM}, stop:1 {GRAD_TO});
    border-radius: {PILL - 1}px;
}}


QScrollArea {{
    background-color: transparent;
    border: none;
}}
QScrollArea > QWidget > QWidget {{
    background-color: transparent;
}}


QFrame#updateBanner {{
    background-color: {MANTLE};
    border: 1px solid {ACCENT};
    border-radius: {SOFT}px;
}}
"""


@lru_cache(maxsize=2)
def load_sakura(side: str) -> "QPixmap":
    from PyQt6.QtGui import QPixmap
    from nyaatriggers.paths import bundle_root

    image = QPixmap(str(bundle_root() / "assets" / "sakura_trees.png"))
    if image.isNull():
        return image
    half = image.width() // 2
    x = 0 if side == "left" else half
    return image.copy(x, 0, half, image.height())


class SakuraBackground:
    """Cache the scenery at the current size and display scale."""

    def __init__(self, side: str):
        self._side = side
        self._tree = load_sakura(side)
        self._cache_key = None
        self._pixmap = None

    def paint(self, painter: "QPainter", w: int, h: int) -> None:
        if w <= 0 or h <= 0:
            return
        ratio = painter.device().devicePixelRatioF()
        key = (w, h, ratio)
        if key != self._cache_key:
            import math
            from PyQt6.QtCore import Qt, QPointF, QRectF
            from PyQt6.QtGui import QBrush, QColor, QLinearGradient, QPainter, QPixmap, QRadialGradient

            pixmap = QPixmap(math.ceil(w * ratio), math.ceil(h * ratio))
            pixmap.setDevicePixelRatio(ratio)
            pixmap.fill(QColor(BASE) if self._side == "right" else Qt.GlobalColor.transparent)
            p = QPainter(pixmap)
            p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
            tree = self._tree
            if not tree.isNull():
                p.save()
                if self._side == "left":
                    scale = h / tree.height()
                    p.setOpacity(0.42)
                else:
                    scale = h * 1.08 / tree.height()
                    p.setOpacity(0.34)
                    p.translate(w - tree.width() * scale, -h * 0.08)
                p.scale(scale, scale)
                p.drawPixmap(0, 0, tree)
                p.restore()
            if self._side == "left":
                # Dim the tree behind navigation and branding.
                band = QLinearGradient(0, h * 0.05, 0, h * 0.64)
                band.setColorAt(0.0, QColor(7, 7, 11, 0))
                band.setColorAt(0.20, QColor(7, 7, 11, 120))
                band.setColorAt(0.74, QColor(7, 7, 11, 120))
                band.setColorAt(1.0, QColor(7, 7, 11, 0))
                p.fillRect(QRectF(0, h * 0.05, w, h * 0.59), QBrush(band))
                bg = QRadialGradient(QPointF(w * 0.42, h * 0.065), h * 0.16)
                bg.setColorAt(0.0, QColor(7, 7, 11, 130))
                bg.setColorAt(0.6, QColor(7, 7, 11, 80))
                bg.setColorAt(1.0, QColor(7, 7, 11, 0))
                p.fillRect(QRectF(0, 0, w, h * 0.22), QBrush(bg))
            p.end()
            self._pixmap = pixmap
            self._cache_key = key
        painter.drawPixmap(0, 0, self._pixmap)


def make_petal(size: int, tint: "QColor") -> "QPixmap":
    """Prerender a notched petal for reuse during animation."""
    from PyQt6.QtCore import Qt, QPointF
    from PyQt6.QtGui import QPainter, QPainterPath, QPixmap

    H = float(size)
    W = H * 0.72
    pm = QPixmap(int(W) + 6, int(H) + 6)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(tint)
    p.translate(pm.width() / 2, pm.height() / 2)
    path = QPainterPath(QPointF(0, H / 2))                    # bottom tip
    path.cubicTo(QPointF(-W * 0.72, H * 0.22),
                 QPointF(-W * 0.56, -H * 0.30),
                 QPointF(-W * 0.15, -H * 0.44))               # left edge, up
    path.lineTo(QPointF(0, -H * 0.28))                        # notch
    path.lineTo(QPointF(W * 0.15, -H * 0.44))                 # right lobe top
    path.cubicTo(QPointF(W * 0.56, -H * 0.30),
                 QPointF(W * 0.72, H * 0.22),
                 QPointF(0, H / 2))                           # right edge, down
    p.drawPath(path)
    p.end()
    return pm


def make_petals(seed: int, count: int) -> list:
    """Create petal textures with deterministic motion."""
    import random
    from PyQt6.QtGui import QColor

    rnd = random.Random(seed)
    tints = [
        QColor(255, 170, 186),
        QColor(255, 200, 213),
        QColor(255, 228, 234),
    ]
    petals = []
    for _ in range(count):
        petals.append({
            "pm":      make_petal(rnd.randint(10, 22), rnd.choice(tints)),
            "fx":      rnd.uniform(0.02, 0.98),       # x anchor, width fraction
            "y0":      rnd.uniform(-0.1, 1.0),        # phase offset, height fraction
            "fall":    rnd.uniform(0.014, 0.040),     # fall speed, heights/second
            "sway":    rnd.uniform(8.0, 26.0),        # sway amplitude, px
            "sway_T":  rnd.uniform(3.0, 6.5),         # sway period, seconds
            "phase":   rnd.uniform(0.0, 6.283),
            "rot0":    rnd.uniform(0.0, 360.0),
            "rot_v":   rnd.uniform(-24.0, 24.0),      # spin, degrees/second
            "op":      rnd.uniform(0.30, 0.52),
        })
    return petals


def petal_rects(w: int, h: int, t: float, petals: list) -> list:
    """Return petal bounds for partial repainting."""
    import math
    from PyQt6.QtCore import QRect

    rects = []
    for pt in petals:
        y = ((pt["y0"] + pt["fall"] * t) % 1.15 - 0.075) * h
        x = pt["fx"] * w + math.sin(t / pt["sway_T"] * 2.0 * math.pi + pt["phase"]) * pt["sway"]
        pm = pt["pm"]
        r = math.hypot(pm.width(), pm.height()) / 2 + 2   # Allow for rotation and antialiasing.
        rects.append(QRect(round(x - r), round(y - r), round(2 * r), round(2 * r)))
    return rects


def paint_petals(p: "QPainter", w: int, h: int, t: float, petals: list) -> None:
    """Place petals from time in seconds using wrapped fall, sway and rotation."""
    import math
    from PyQt6.QtCore import QPointF

    for pt in petals:
        pm = pt["pm"]
        y = ((pt["y0"] + pt["fall"] * t) % 1.15 - 0.075) * h
        x = pt["fx"] * w + math.sin(t / pt["sway_T"] * 2.0 * math.pi + pt["phase"]) * pt["sway"]
        p.save()
        p.setOpacity(pt["op"])
        p.translate(x, y)
        p.rotate(pt["rot0"] + pt["rot_v"] * t)
        p.drawPixmap(QPointF(-pm.width() / 2, -pm.height() / 2), pm)
        p.restore()


def nav_icon(name: str, color: str, size: int = 18) -> "QIcon":
    """QSS cannot recolour icons, so generate each colour separately."""
    import math
    from PyQt6.QtCore import Qt, QRectF, QPointF
    from PyQt6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap

    pm = QPixmap(size * 2, size * 2)   # 2x supersample keeps the strokes crisp
    pm.setDevicePixelRatio(2.0)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    pen = QPen(QColor(color))
    pen.setWidthF(2.0)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.scale(size / 24.0, size / 24.0)

    if name == "triggers":
        pts = [(13, 2), (3, 14), (12, 14), (11, 22), (21, 10), (12, 10), (13, 2)]
        path = QPainterPath(QPointF(*pts[0]))
        for x, y in pts[1:]:
            path.lineTo(x, y)
        p.drawPath(path)
    elif name == "current":
        path = QPainterPath(QPointF(22, 12))
        for x, y in [(18, 12), (15, 21), (9, 3), (6, 12), (2, 12)]:
            path.lineTo(x, y)
        p.drawPath(path)
    elif name == "dps":
        for x, top in ((18, 10), (12, 4), (6, 14)):
            p.drawLine(QPointF(x, 20), QPointF(x, top))
    elif name == "recap":
        p.drawRoundedRect(QRectF(5, 3, 14, 18), 2, 2)
        path = QPainterPath(QPointF(8, 12))
        for x, y in [(10, 12), (11, 8), (13, 16), (14, 12), (16, 12)]:
            path.lineTo(x, y)
        p.drawPath(path)
    elif name == "prog":
        path = QPainterPath(QPointF(3, 19))
        for x, y in [(3, 14), (9, 14), (9, 9), (15, 9), (15, 4), (21, 4)]:
            path.lineTo(x, y)
        p.drawPath(path)
    elif name == "automarkers":
        path = QPainterPath(QPointF(21, 10))
        path.cubicTo(QPointF(21, 17), QPointF(12, 23), QPointF(12, 23))
        path.cubicTo(QPointF(12, 23), QPointF(3, 17), QPointF(3, 10))
        path.arcTo(QRectF(3, 1, 18, 18), 180, -180)
        path.closeSubpath()
        p.drawPath(path)
        p.drawEllipse(QPointF(12, 10), 3, 3)
    elif name == "settings":
        p.drawEllipse(QPointF(12, 12), 3.2, 3.2)
        p.drawEllipse(QPointF(12, 12), 8.2, 8.2)
        for k in range(8):
            a = math.radians(k * 45)
            p.drawLine(QPointF(12 + 8.2 * math.cos(a), 12 + 8.2 * math.sin(a)),
                       QPointF(12 + 10.6 * math.cos(a), 12 + 10.6 * math.sin(a)))
    p.end()
    return QIcon(pm)


def apply_primary(button) -> None:
    """Style a primary button with a small glow to limit rendering artifacts."""
    from PyQt6.QtCore import Qt
    from PyQt6.QtGui import QColor
    from PyQt6.QtWidgets import QGraphicsDropShadowEffect

    button.setObjectName("primary")
    button.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
    button.style().unpolish(button)
    button.style().polish(button)
    glow = QGraphicsDropShadowEffect(button)
    glow.setBlurRadius(18)
    glow.setColor(QColor(255, 131, 153, 180))
    glow.setOffset(0, 0)
    button.setGraphicsEffect(glow)
