"""Shared colours, widget styles and decorative drawing helpers."""

# Surface colours
BASE     = "#0a0a0c"   # window background
PANEL    = "#101013"   # Sidebar and list backgrounds
MANTLE   = "#18181d"   # Inputs and hovered controls
SURFACE2 = "#26262e"   # Raised surfaces and borders
EDGE     = "#26262e"
OVERLAY0 = "#3a3a44"   # Muted hover colour

# Accents
ACCENT    = "#ff8399"
ACCENT2   = "#e66c82"
ON_ACCENT = "#2b1017"   # dark text on accent fills

# Text
TEXT     = "#e8e8ec"
SUBTEXT1 = "#8f8f9a"
SUBTEXT0 = "#6a6a74"
SUBTEXT_SOFT = "#b0b0be"   # Inactive navigation, kana and version labels

# Status colours
OK       = "#a6e3a1"
ERR      = "#f38ba8"
MAUVE    = "#cba6f7"

# Brand gradient
GRAD_FROM = ACCENT
GRAD_TO   = MAUVE

PILL = 14
# Corner radius for larger surfaces.
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


def make_tree(seed: int, w: int, h: int, inward: int = 1, crisp: bool = False,
              stroke_scale: float = 1.0, lift: float = 0.0) -> "QPixmap":
    """Render a deterministic cherry tree from a seed. inward selects the window edge.
    crisp omits the background wash, stroke_scale controls branch widths, and lift moves
    the crown upward as a fraction of height.
    """
    import math
    import random
    from PyQt6.QtCore import Qt, QPointF
    from PyQt6.QtGui import QColor, QGuiApplication, QPainter, QPen, QPixmap

    rnd = random.Random(seed)
    # Render at the display pixel ratio without setting the pixmap ratio. Callers scale
    # by pixel height, so a device ratio tag would shrink the tree twice.
    screen = QGuiApplication.primaryScreen()
    dpr = screen.devicePixelRatio() if screen else 1.0
    pm = QPixmap(round(w * dpr), round(h * dpr))
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    p.scale(dpr, dpr)

    blossoms = [
        QColor(255, 224, 231),
        QColor(255, 205, 216),
        QColor(255, 185, 200),
        QColor(255, 238, 242),
    ]
    bark = QColor(96, 66, 78, 200)

    def stroke(x1, y1, x2, y2, width):
        pen = QPen(bark)
        pen.setWidthF(width)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        p.drawLine(QPointF(x1, y1), QPointF(x2, y2))

    tips = []
    x0 = 3.0 if inward > 0 else w - 3.0   # Center the trunk on the window edge.

    # Draw the trunk in one stroke to avoid a visible joint.
    top_y = h * (0.24 - lift)
    stroke(x0, h, x0, top_y, w * 0.045 * stroke_scale)

    def branch(x, y, angle, length, width, depth):
        x2 = x + math.cos(angle) * length
        y2 = y + math.sin(angle) * length
        stroke(x, y, x2, y2, width)
        if depth <= 0:
            tips.append((x2, y2))
            return
        for _ in range(2):
            branch(x2, y2,
                   angle + rnd.uniform(-0.25, 0.25) + inward * 0.08,
                   length * rnd.uniform(0.55, 0.70), width * 0.62, depth - 1)

    n_br = rnd.randint(3, 4)
    for i in range(n_br):
        by = top_y + (h * 0.04) * (i / max(1, n_br - 1))
        branch(x0, by, -math.pi / 2 + inward * rnd.uniform(0.75, 1.15),
               rnd.uniform(h * 0.04, h * 0.07), w * 0.018 * stroke_scale, 2)
    tips.append((x0 + inward * w * 0.02, top_y - h * 0.02))

    cx = sum(t[0] for t in tips) / len(tips)
    cy = sum(t[1] for t in tips) / len(tips)
    lo, hi = (w * 0.04, w * 0.22) if inward > 0 else (w * 0.78, w * 0.96)
    cx = max(lo, min(hi, cx))
    cy = max(h * (0.10 - lift), min(h * (0.15 - lift), cy))
    # Fit the blossom crown to the branch tips.
    max_dx = max(abs(t[0] - cx) for t in tips)
    max_dy = max(abs(t[1] - cy) for t in tips)
    cover_rx = min(w * 0.45, max(w * 0.30, max_dx + w * 0.06))
    cover_ry = min(h * 0.13, max(h * 0.06, max_dy + h * 0.03))

    p.setPen(Qt.PenStyle.NoPen)

    # Skip the background wash for crisp crowns.
    for _ in range(0 if crisp else 3):
        px = cx + rnd.uniform(-0.08, 0.08) * cover_rx
        py = cy + rnd.uniform(-0.08, 0.08) * cover_ry
        c = QColor(rnd.choice(blossoms))
        c.setAlpha(rnd.randint(10, 16))
        p.setBrush(c)
        p.drawEllipse(QPointF(px, py), cover_rx * 0.9, cover_ry * 0.9)

    if crisp:
        r_lo, r_hi, a_lo, a_hi = w * 0.011, w * 0.024, 60, 115
    else:
        r_lo, r_hi, a_lo, a_hi = w * 0.016, w * 0.042, 35, 85
    for _ in range(6):
        px = cx + rnd.uniform(-0.28, 0.28) * cover_rx
        py = cy + rnd.uniform(-0.28, 0.28) * cover_ry
        rx = rnd.uniform(0.55, 0.85) * cover_rx
        ry = rnd.uniform(0.55, 0.85) * cover_ry
        for _ in range(rnd.randint(45, 60)):
            a = rnd.uniform(0.0, 2.0 * math.pi)
            rr = math.sqrt(rnd.random())
            bx = px + rx * rr * math.cos(a)
            by = py + ry * rr * math.sin(a)
            r = rnd.uniform(r_lo, r_hi)
            c = QColor(rnd.choice(blossoms))
            c.setAlpha(rnd.randint(a_lo, a_hi))
            p.setBrush(c)
            p.drawEllipse(QPointF(bx, by), r, r)

    for tx, ty in tips:
        for _ in range(rnd.randint(4, 6)):
            ox = rnd.uniform(-w * 0.035, w * 0.035)
            oy = rnd.uniform(-h * 0.020, h * 0.020)
            r = rnd.uniform(w * (0.010 if crisp else 0.014), w * (0.022 if crisp else 0.030))
            c = QColor(rnd.choice(blossoms))
            c.setAlpha(rnd.randint(70 if crisp else 45, 120 if crisp else 90))
            p.setBrush(c)
            p.drawEllipse(QPointF(tx + ox, ty + oy), r, r)

    for _ in range(rnd.randint(25, 35)):
        a = rnd.uniform(0.0, 2.0 * math.pi)
        rr = math.sqrt(rnd.random())
        bx = cx + cover_rx * 0.9 * rr * math.cos(a)
        by = cy + cover_ry * 0.9 * rr * math.sin(a)
        r = rnd.uniform(w * 0.006, w * 0.014)
        c = QColor(blossoms[3])
        c.setAlpha(rnd.randint(80, 130))
        p.setBrush(c)
        p.drawEllipse(QPointF(bx, by), r, r)
    p.end()
    return pm


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
    """Create petal textures and deterministic motion parameters for paint_petals."""
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
    """Return petal bounds at time t so callers can repaint only the affected areas."""
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
    """Draw a tinted icon on a 24 by 24 grid. Generate each colour separately because QSS
    cannot recolour icons.
    """
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

    if name == "triggers":           # bolt
        pts = [(13, 2), (3, 14), (12, 14), (11, 22), (21, 10), (12, 10), (13, 2)]
        path = QPainterPath(QPointF(*pts[0]))
        for x, y in pts[1:]:
            path.lineTo(x, y)
        p.drawPath(path)
    elif name == "current":          # activity pulse
        path = QPainterPath(QPointF(22, 12))
        for x, y in [(18, 12), (15, 21), (9, 3), (6, 12), (2, 12)]:
            path.lineTo(x, y)
        p.drawPath(path)
    elif name == "dps":              # bar chart
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
    elif name == "automarkers":      # Map pin
        path = QPainterPath(QPointF(21, 10))
        path.cubicTo(QPointF(21, 17), QPointF(12, 23), QPointF(12, 23))
        path.cubicTo(QPointF(12, 23), QPointF(3, 17), QPointF(3, 10))
        path.arcTo(QRectF(3, 1, 18, 18), 180, -180)
        path.closeSubpath()
        p.drawPath(path)
        p.drawEllipse(QPointF(12, 10), 3, 3)
    elif name == "settings":         # gear
        p.drawEllipse(QPointF(12, 12), 3.2, 3.2)
        p.drawEllipse(QPointF(12, 12), 8.2, 8.2)
        for k in range(8):
            a = math.radians(k * 45)
            p.drawLine(QPointF(12 + 8.2 * math.cos(a), 12 + 8.2 * math.sin(a)),
                       QPointF(12 + 10.6 * math.cos(a), 12 + 10.6 * math.sin(a)))
    p.end()
    return QIcon(pm)


def apply_primary(button) -> None:
    """Apply the primary button style and a glow. Keep the shadow radius small to limit
    rendering artifacts.
    """
    from PyQt6.QtCore import Qt
    from PyQt6.QtGui import QColor
    from PyQt6.QtWidgets import QGraphicsDropShadowEffect

    button.setObjectName("primary")
    button.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
    # Refresh styles after changing objectName.
    button.style().unpolish(button)
    button.style().polish(button)
    glow = QGraphicsDropShadowEffect(button)
    glow.setBlurRadius(18)
    glow.setColor(QColor(255, 131, 153, 180))
    glow.setOffset(0, 0)
    button.setGraphicsEffect(glow)
