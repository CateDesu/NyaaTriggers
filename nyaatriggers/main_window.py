"""Main window construction and signal wiring."""

import math
import sys
import threading
import time
from collections import deque
from pathlib import Path

from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QFrame, QListWidget, QMainWindow, QScrollArea, QSlider, QButtonGroup,
    QStackedWidget, QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QTableWidget,
    QTableWidgetItem, QHeaderView, QLineEdit, QLabel, QPlainTextEdit, QSplitter, QTreeWidget,
    QAbstractItemView, QStyle, QStyleOptionButton, QStylePainter,
)
from PyQt6.QtCore import Qt, QSize, QTimer, QUrl, QPointF, QRectF, QEvent, pyqtSignal, pyqtSlot
from PyQt6.QtGui import (
    QBrush, QColor, QDesktopServices, QFont, QIcon, QPainter,
    QFontMetricsF, QLinearGradient, QRadialGradient, QPainterPath, QPen,
)

from nyaatriggers.trigger_engine import Trigger
from nyaatriggers.ws_client import WSClient
from nyaatriggers.pull_capture import PullCapture
from nyaatriggers.tts import (
    speak, play_sound, interrupt as tts_interrupt, set_venv_path, set_master_volume, set_engine,
    default_engine, set_jp_voice, set_jp_auto, set_jp_neural, kokoro_ready, _ensure_worker,
    _load_piper,
)
from nyaatriggers.locale_util import _, effective_locale, set_locale, active_locale
from nyaatriggers.sequential import SequentialRunner
from nyaatriggers.status_timer import StatusTimerRunner
from nyaatriggers.timeline_engine import TimelineEngine
from nyaatriggers.cactbot_reader import CactbotReader
from nyaatriggers.triggevent_bridge import TriggeventBridge
from nyaatriggers.telesto_client import _actor_int
from nyaatriggers.plugin_link import DEFAULT_PORT, PluginLink, parse_port
from nyaatriggers.umad_chains import parse_compound as _parse_compound
from nyaatriggers import theme
from nyaatriggers.ui.ambient_fx import AmbientFxMixin
from nyaatriggers import app_common as ac
from nyaatriggers.ui.dps_tab import DpsTabMixin
from nyaatriggers.death_recap import DeathRecap
from nyaatriggers.prog_session import ProgSessions
from nyaatriggers.ui.death_recap_tab import DeathRecapTabMixin
from nyaatriggers.ui.prog_tab import ProgTab
from nyaatriggers.ui.session_tracking import SessionTrackingMixin
from nyaatriggers.ui.profiles import ProfilesMixin
from nyaatriggers.ui.timeline_tab import TimelineTabMixin
from nyaatriggers.ui.connection import ConnectionMixin
from nyaatriggers.ui.settings_tab import SettingsTabMixin
from nyaatriggers.ui.voice_tab import VoiceTabMixin
from nyaatriggers.ui.automarkers_tab import AutomarkersTabMixin
from nyaatriggers.updater_ui import UpdaterUiMixin
from nyaatriggers.ui.instance_tab import InstanceTabMixin
from nyaatriggers.ui.engines import EnginesMixin
from nyaatriggers.ui.triggers_tab import TriggersTabMixin

from nyaatriggers.app_common import (
    MAX_ABILITY_LINES, MAX_RAW_CAPTURE, _CALLOUT_CLAIM_S, _C_EN, _C_FIGHT, _C_NAME, _C_RE,
    _C_TTS, _C_TYPE, _C_ZONE, _DATA_DIR, _DISCORD_URL, _DISPLAY_VERSION, _GITHUB_URL, _HEADERS,
    _PIPER_VOICES_URL, _UMAD_AUTOMARK_PRESET, _VERSION, _as_strdict, _as_strset,
    _as_text_overrides, _compile_phrase_patterns, _next_bad_name, _sweep_stale_update_parts,
    canonical_zone_name,
)

class _SidebarFrame(QFrame):
    """Paint sidebar scenery behind the navigation controls. MainWindow schedules repaints
    and pauses animation when the window loses focus.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._petals = theme.make_petals(23, 5)
        self._tree = theme.make_tree(17, 216, 900, inward=1)
        self._t0 = time.monotonic()
        self._last_t = None
        self.awake = True
        self.freeze_t = None   # petal clock value while parked

    def tick(self) -> None:
        # Repaint only the strips the petals touch, old frame and new.
        t = time.monotonic() - self._t0
        if self._last_t is None:
            self.update()
        else:
            for r in theme.petal_rects(self.width(), self.height(), self._last_t, self._petals):
                self.update(r)
            for r in theme.petal_rects(self.width(), self.height(), t, self._petals):
                self.update(r)
        self._last_t = t

    def paintEvent(self, ev):
        super().paintEvent(ev)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        s = self.height() / self._tree.height()
        p.save()
        p.scale(s, s)
        p.drawPixmap(0, 0, self._tree)
        p.restore()
        # Dim the scenery behind navigation and branding to keep text readable.
        h = self.height()
        band = QLinearGradient(0, h * 0.05, 0, h * 0.64)
        band.setColorAt(0.0, QColor(7, 7, 11, 0))
        band.setColorAt(0.20, QColor(7, 7, 11, 120))
        band.setColorAt(0.74, QColor(7, 7, 11, 120))
        band.setColorAt(1.0, QColor(7, 7, 11, 0))
        p.fillRect(QRectF(0, h * 0.05, self.width(), h * 0.59), QBrush(band))
        bg = QRadialGradient(QPointF(self.width() * 0.42, h * 0.065), h * 0.16)
        bg.setColorAt(0.0, QColor(7, 7, 11, 130))
        bg.setColorAt(0.6, QColor(7, 7, 11, 80))
        bg.setColorAt(1.0, QColor(7, 7, 11, 0))
        p.fillRect(QRectF(0, 0, self.width(), h * 0.22), QBrush(bg))
        if self.awake:
            t = time.monotonic() - self._t0
        else:
            t = self.freeze_t
        if t is not None:
            theme.paint_petals(p, self.width(), self.height(), t, self._petals)
        p.end()

class _NavButton(QPushButton):
    """Paint navigation borders and text effects that QSS cannot provide. QSS still
    supplies the background and font.
    """

    def paintEvent(self, _ev):
        opt = QStyleOptionButton()
        self.initStyleOption(opt)
        p = QStylePainter(self)
        # Let QSS draw the background before painting the custom border and label.
        p.drawControl(QStyle.ControlElement.CE_PushButtonBevel, opt)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        round_ = (Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
                  Qt.PenJoinStyle.RoundJoin)
        if self.isChecked():
            r = QRectF(self.rect()).adjusted(3.5, 3.5, -3.5, -3.5)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(255, 131, 153, 14))
            p.drawRoundedRect(r, 7, 7)
            p.setBrush(Qt.BrushStyle.NoBrush)
            for pen_w, alpha in ((6.0, 17), (4.0, 40), (2.0, 92)):
                p.setPen(QPen(QColor(255, 131, 153, alpha), pen_w, *round_))
                p.drawRoundedRect(r, 7, 7)
            p.setPen(QPen(QColor(255, 176, 192), 1.3, *round_))
            p.drawRoundedRect(r, 7, 7)

        icon_size = self.iconSize()
        icon_x = 8
        pm = self.icon().pixmap(icon_size)
        if not pm.isNull():
            iy = (self.height() - icon_size.height()) // 2
            p.drawPixmap(icon_x, iy, icon_size.width(), icon_size.height(), pm)
        text_x = icon_x + icon_size.width() + 4

        f = QFont(self.font())   # Keep the font supplied by QSS.
        if self.isChecked():
            f.setLetterSpacing(QFont.SpacingType.PercentageSpacing, 105)
        fm = QFontMetricsF(f)
        text = fm.elidedText(self.text(), Qt.TextElideMode.ElideRight,
                             self.width() - text_x - 4)
        baseline = (self.height() + fm.ascent() - fm.descent()) / 2
        if self.isChecked():
            color = QColor(theme.ACCENT)
        elif self.underMouse():
            color = QColor(theme.TEXT)
        else:
            color = QColor(theme.SUBTEXT_SOFT)

        path = QPainterPath()
        path.addText(text_x, baseline, f, text)
        p.fillPath(path.translated(0.7, 1.0), QBrush(QColor(4, 4, 7, 190)))
        if self.isChecked():
            p.strokePath(path, QPen(QColor(255, 131, 153, 42), 6.0, *round_))
            p.strokePath(path, QPen(QColor(255, 131, 153, 62), 4.0, *round_))
            p.strokePath(path, QPen(QColor(6, 6, 9, 245), 2.6, *round_))
        else:
            p.strokePath(path, QPen(QColor(6, 6, 9, 130), 5.0, *round_))
            p.strokePath(path, QPen(QColor(6, 6, 9, 245), 2.8, *round_))
        p.fillPath(path, QBrush(color))
        p.end()

class _BrandLabel(QLabel):
    """Paint branding with a text halo and optional glow. Use the QSS font with custom
    colour and spacing.
    """

    def __init__(self, text, color, glow=False, spacing=100.0, parent=None):
        super().__init__(text, parent)
        self._color = QColor(color)
        self._glow = glow
        self._spacing = spacing

    def paintEvent(self, _ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        f = self.font()
        if self._spacing != 100.0:
            f.setLetterSpacing(QFont.SpacingType.PercentageSpacing, self._spacing)
        fm = QFontMetricsF(f)
        r = self.contentsRect()
        text = fm.elidedText(self.text(), Qt.TextElideMode.ElideRight, r.width())
        x = float(r.x())
        if self.alignment() & Qt.AlignmentFlag.AlignHCenter:
            x = r.x() + (r.width() - fm.horizontalAdvance(text)) / 2
        baseline = r.y() + (r.height() + fm.ascent() - fm.descent()) / 2
        path = QPainterPath()
        path.addText(x, baseline, f, text)
        round_ = (Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
                  Qt.PenJoinStyle.RoundJoin)
        p.fillPath(path.translated(0.6, 0.9), QBrush(QColor(4, 4, 7, 180)))
        if self._glow:
            p.strokePath(path, QPen(QColor(255, 131, 153, 38), 5.0, *round_))
        p.strokePath(path, QPen(QColor(6, 6, 9, 235), 2.6, *round_))
        p.fillPath(path, QBrush(self._color))
        p.end()

class MainWindow(ProfilesMixin, SessionTrackingMixin, DeathRecapTabMixin, AmbientFxMixin, DpsTabMixin, TimelineTabMixin, ConnectionMixin, SettingsTabMixin, VoiceTabMixin, AutomarkersTabMixin, UpdaterUiMixin, InstanceTabMixin, EnginesMixin, TriggersTabMixin, QMainWindow):
    _trig_update_signal    = pyqtSignal(object, str)   # button that started the fetch, plus an ok or err payload
    _upd_available_signal  = pyqtSignal(object)    # updater.Release, or None if up to date
    _upd_checkmsg_signal   = pyqtSignal(bool, str) # run was manual, plus feedback text, "" means none
    _upd_progress_signal   = pyqtSignal(int, str)  # percent, -1 indeterminate, plus status
    _upd_done_signal       = pyqtSignal(bool, str) # installed_ok and message
    _cactbot_tl_signal     = pyqtSignal(str)       # fight tag whose cactbot timeline just downloaded
    _te_update_signal      = pyqtSignal(bool, str, bool)  # changed, message, manual, from the bg Triggevent update
    _callouts_ja_signal    = pyqtSignal(bool)      # background callouts_ja refresh finished, arg is changed
    _kokoro_dl_signal       = pyqtSignal(str)       # Kokoro setup finished, status string for the UI
    _fflogs_signal          = pyqtSignal(int, object)    # Request number and FFLogs result.

    def __init__(self):
        super().__init__()
        self.setWindowTitle("NyaaTriggers")
        _icon = ac._ASSETS_DIR / "icon_nyaa.png"
        if _icon.exists():
            self.setWindowIcon(QIcon(str(_icon)))
        self.resize(1280, 720)
        self.setMinimumSize(900, 600)

        self._save_warned = False
        self._triggers: list[Trigger] = []
        self._official_ids: set[str] = set()            # IDs loaded from triggers.json
        self._official_triggers: dict[str, Trigger] = {} # id to original trigger, for reset
        self._local_ids: set[str] = set()               # IDs that belong in triggers.local.json
        self._deleted_ids: set[str] = set()             # official IDs the user has deleted
        self._retired_ids: set[str] = set()
        self._folders: list[dict] = []                  # [{id, name, parent_id}]
        self._current_zone: str = ""
        self._current_zone_id: int = 0   # retained for the Triggernometry zone replay
        self._awaiting_zone_metadata = True
        # Use the canonical English zone for trigger matching when available. Keep the
        # reported name in _current_zone for display.
        self._match_zone: str = ""
        self._zone_aliases: tuple = ()
        # Resolve local identity from line 02, with the saved name as fallback.
        self._me_name: str = ""
        self._me_id: str = ""     # Hex actor ID from line 02 distinguishes players with the same name.
        self._seq_runners: list[SequentialRunner] = []
        self._status_timers: list[StatusTimerRunner] = []
        self._ability_buffer: deque = deque(maxlen=MAX_ABILITY_LINES)  # dicts of log_type, is_player, line, color, ability_name, ability_id
        # Retain the full feed for export because the displayed log omits some effects.
        self._raw_capture: deque = deque(maxlen=MAX_RAW_CAPTURE)

        self._current_fight_tag: str = ""
        # Reload the timeline only when periodic detection resolves a different fight.
        self._timeline_fight: str = ""
        # File timestamps recorded by the last trigger load.
        self._triggers_mtime: tuple = ()

        self._timeline = TimelineEngine(self)
        # Route timeline callouts through guest deduplication so local triggers take
        # priority.
        self._timeline.tts.connect(self._on_timeline_tts)

        self._cactbot_reader: CactbotReader | None = None
        self._cactbot_mode: bool = False
        # Distinguish deliberate reader shutdown from asynchronous page load failure.
        self._cactbot_teardown: bool = False
        # Local triggers claim their callout text so duplicate guest callouts can be
        # suppressed.
        self._callout_claimed: dict[str, float] = {}   # key to monotonic expiry
        # Deferred guest timers and severity. A popup may raise the severity of an
        # earlier cactbotSay message.
        self._pending_guests: dict[str, tuple] = {}
        # Track severity only for guest claims. Guests must not alter local trigger
        # claims.
        self._guest_claim_sev: dict[str, str] = {}

        self._triggevent: "TriggeventBridge | None" = None
        self._triggevent_mode: bool = False
        self._triggevent_last_spoken: dict[str, float] = {}
        # Engine name to status and message for the connection indicator.
        self._engine_sidecar_state: dict = {}
        # Sequential trigger failures for the current engine session, newest last.
        self._engine_chain_failures: list = []
        self._telesto_status: str = "unknown"   # Telesto reachability, good, bad or unknown
        self._telesto_client = None
        self._cactbot_disabled: set[str] = set()
        self._cactbot_triggers_meta: list[dict] = []       # {id, name} rows from enumeration, if any

        # Cactbot timelines follow the Cactbot switch alone. There is no separate
        # timeline toggle.
        self._local_enabled: bool = False
        self._cactbot_tl_fetching: set[str] = set()
        # Fetch state is updated from both the GUI and worker threads.
        self._cactbot_tl_lock = threading.Lock()

        self._ws = WSClient(self)
        self._ws.log_line.connect(self._on_log_line)
        self._ws.in_combat.connect(self._on_in_combat)
        self._ws.status_changed.connect(self._on_status_changed)
        # Stop the timeline clock on feed loss so disconnected sessions cannot fire
        # cues.
        self._ws.status_changed.connect(self._timeline.feed_status_changed)
        self._ws.primary_player.connect(self._on_ws_primary_player)
        self._ws.zone_changed.connect(self._on_ws_zone_changed)
        self._connected = False
        self._pending_timeline_start = None
        self._in_game_combat = False
        # Only timelines marked reset-on-combat-end reset on combat exit. Other fights
        # may have intermissions outside combat.
        self._timeline_reset_on_combat_end = False
        # Cactbot schedules draw bars only because the reader already speaks their
        # callouts.
        self._timeline_from_cactbot = False

        self._settings: dict = {}
        self._settings_load_warning = None
        self._load_settings()
        # Capture raw pulls for replay when enabled in Settings.
        self._pull_capture = PullCapture(_DATA_DIR / "pull_logs", self,
                                         state_snapshot=self._ws.state_snapshot)
        self._pull_capture.context = lambda: (self._current_fight_tag, self._current_zone)
        self._ws.log_line.connect(self._pull_capture.on_log_line)
        self._ws.raw_message.connect(self._pull_capture.on_raw_message)
        self._ws.in_combat.connect(self._pull_capture.on_in_combat)
        self._ws.zone_changed.connect(self._pull_capture.on_zone_changed)
        self._ws.status_changed.connect(self._pull_capture.on_status_changed)
        self._pull_capture.set_recording(bool(self._settings.get("triggevent_record_pulls", False)))
        # Set the locale and load callout translations before constructing widgets or
        # firing triggers.
        set_locale(effective_locale(self._settings.get("ui_language", "auto")))
        if self._settings_load_warning is not None:
            # Deferred from _load_settings so the dialog follows the locale.
            err, backup = self._settings_load_warning
            where = _("A copy was kept at:\n{path}").format(path=backup) if backup else ""
            ac.QMessageBox.warning(
                self, _("Settings Unreadable"),
                _("Your settings file could not be read ({err}), so defaults "
                  "are in use.").format(err=err) + ("\n\n" + where if where else ""))
        self._callouts_ja: dict = {}
        self._callouts_phrases_ja: dict = {}
        self._callouts_phrases_ja_patterns: list = []   # Matchers and translations for tokenized keys.
        self._callouts_readings: dict = {}
        self._callouts_names_ja: dict = {}
        self._callouts_names_text_ja: dict = {}          # english name to ja, engine triggers
        self._load_cached_callouts_ja()
        self._init_automarkers()
        # Keep the overlay connection active with automatic reconnect. The configured
        # port supports a second game client.
        port = parse_port(self._settings.get("plugin_port"))
        self._plugin_link = PluginLink(
            port=port if port is not None else DEFAULT_PORT, enabled=True)
        self._plugin_link.status_changed.connect(self._on_plugin_link_status)
        self._plugin_link.start()
        # The plugin interpolates between ticks, so four clock updates per second are
        # sufficient.
        self._plugin_tick_timer = QTimer(self)
        self._plugin_tick_timer.setInterval(250)
        self._plugin_tick_timer.timeout.connect(self._push_plugin_tick)
        self._plugin_tick_timer.start()
        # Poll for updated triggers and fight detection so changes apply during the
        # session. Unchanged files and fight tags require no reload.
        self._zone_redetect_timer = QTimer(self)
        self._zone_redetect_timer.setInterval(30_000)
        self._zone_redetect_timer.timeout.connect(self._poll_zone_and_triggers)
        self._zone_redetect_timer.start()
        # Reject null or non-string character names from edited settings.
        char_name = self._settings.get("char_name")
        self._me_name = char_name if isinstance(char_name, str) else ""
        self._local_enabled = bool(self._settings.get("local_enabled", False))
        self._cactbot_disabled = _as_strset(self._settings.get("cactbot_disabled_triggers", []))
        # Cactbot and editable callouts are mutually exclusive. Triggevent still runs
        # for automarkers when its callouts are disabled.
        self._triggers_enabled = not bool(self._settings.get("cactbot_enabled", False))
        self._init_engines()
        self._src_collapsed: dict = {"general": True, "dot": True, "local": True,
                                     "engine": True, "triggernometry": True}
        self._fight_cur: str = ""
        # Track each global toggle independently of individual triggers. Local defaults
        # on so existing configurations keep their timeline and feed behavior.
        self._global_local_on_flag: bool = bool(self._settings.get("global_local_on", True))
        self._global_tv_on_flag: bool = bool(self._settings.get("global_tv_on", False))
        # Load cached engine inventory before sidecars start.
        self._load_cached_triggevent_inventory()
        self._load_cached_triggernometry_inventory()

        self._init_dps()
        self._combat_known = False
        self._death_recap = DeathRecap()
        # Recap death history follows the full attempt across meter segments.
        self._dps_meter.is_duplicate_death = self._death_recap.is_duplicate_death
        self._prog_sessions = ProgSessions(ac._DATA_DIR / "prog_sessions")
        self._dps_meter.on_pull_start = self._prog_pull_started
        self._dps_meter.on_pull_finish = self._prog_pull_finished

        # Apply slider changes immediately and debounce settings writes. Flush pending
        # writes on exit.
        self._settings_save_timer = QTimer(self)
        self._settings_save_timer.setSingleShot(True)
        self._settings_save_timer.setInterval(400)
        self._settings_save_timer.timeout.connect(self._save_settings)

        self._trig_update_signal.connect(self._on_trig_update_result)
        self._upd_available_signal.connect(self._on_update_available)
        self._upd_checkmsg_signal.connect(self._on_update_checkmsg)
        self._upd_progress_signal.connect(self._on_update_progress)
        self._upd_done_signal.connect(self._on_update_done)
        self._cactbot_tl_signal.connect(self._on_cactbot_timeline_ready)
        self._te_update_signal.connect(self._on_te_update_done)
        self._callouts_ja_signal.connect(self._on_callouts_ja_refreshed)
        self._kokoro_dl_signal.connect(self._on_kokoro_dl_done)
        self._fflogs_signal.connect(self._on_fflogs_result)
        self._init_update_flow()

        self._build_ui()
        self._ws.status_changed.connect(self._track_activity_connection)
        self._load_triggers()
        self._restore_missing_profile()

        # Restore the saved voice environment before preloading models or installing
        # Kokoro.
        venv_path = self._settings.get("venv_path")
        if isinstance(venv_path, str) and venv_path:
            set_venv_path(venv_path)
        # Preload Piper only when selected to avoid an unused ONNX import.
        if self._settings.get("tts_engine", default_engine()) == "piper":
            threading.Thread(target=_load_piper, daemon=True).start()
        # Warm Kokoro imports off the GUI thread to avoid a pause on voice selection.
        threading.Thread(target=kokoro_ready, daemon=True).start()
        _ensure_worker()
        # Use the default volume if the saved value cannot be converted.
        try:
            vol = float(self._settings.get("master_volume", 1.0))
        except (TypeError, ValueError, OverflowError):
            vol = 1.0
        # Reject nonfinite JSON numbers before clamping volume.
        if not math.isfinite(vol):
            vol = 1.0
        set_master_volume(vol)
        set_engine(self._settings.get("tts_engine", default_engine()))
        set_jp_auto(True)   # Japanese text always routes to a Japanese voice
        set_jp_voice("")    # OS fallback voice is auto picked, espeak -v ja or SAPI ja-JP
        set_jp_neural(self._settings.get("jp_neural_enabled", False),
                      self._settings.get("jp_neural_voice", "jf_alpha"))

        if self._settings.get("auto_connect"):
            # Connect without toggling because the user may have already connected
            # manually.
            QTimer.singleShot(
                500, lambda: None if self._connected else self._toggle_connection())

        # Start engines after UI construction. Triggevent runs for automarkers
        # regardless of the selected callout source.
        if TriggeventBridge.is_available():
            QTimer.singleShot(900, self._reconcile_triggevent_engine)
        else:
            # Report missing engine dependencies instead of leaving the trigger section
            # unexplained.
            QTimer.singleShot(1200, self._note_triggevent_unavailable)
        # Restore the selected callout source without stopping the automarker engine.
        if self._settings.get("cactbot_enabled") and CactbotReader.is_available():
            QTimer.singleShot(950, lambda: self._on_cactbot_toggled(True))
        else:
            QTimer.singleShot(950, lambda: self._set_triggers_enabled(True))

        # Schedule updates after construction. main.py retains rollback backups until
        # boot verification succeeds.
        try:
            if self._settings.get("auto_check_updates", True):
                QTimer.singleShot(2500, lambda: self._start_update_check(manual=False))
        except Exception as exc:  # noqa: BLE001
            print(f"[NyaaTriggers] update check scheduling failed: "
                  f"{exc!r}", file=sys.stderr)

        # Refresh translations in the background. Keep bundled or cached data if the
        # fetch fails.
        QTimer.singleShot(2800, self._refresh_callouts_ja_async)

        # Source builds can update the engine in the background for the next launch.
        if self._settings.get("triggevent_auto_update", False):
            QTimer.singleShot(3000, self._maybe_update_triggevent)

        self._init_ambient_fx()

    # Ambient animation

    def event(self, ev):
        # Pause on focus loss and resume from the same animation position.
        t = ev.type()
        if t == QEvent.Type.WindowActivate:
            self._start_fx()
        elif t == QEvent.Type.WindowDeactivate:
            self._stop_fx()
        return super().event(ev)

    def hideEvent(self, ev):
        self._stop_fx()
        super().hideEvent(ev)

    def showEvent(self, ev):
        # Showing an inactive window must not restart animation.
        if self.isActiveWindow():
            self._start_fx()
        else:
            self._stop_fx()
        super().showEvent(ev)

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self.update()

    # UI construction

    def _build_ui(self) -> None:
        root_widget = QWidget()
        root_widget.setObjectName("root")
        self.setCentralWidget(root_widget)
        root = QVBoxLayout(root_widget)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        shell = QHBoxLayout()
        shell.setContentsMargins(0, 0, 0, 0)
        shell.setSpacing(0)
        root.addLayout(shell)

        sidebar = _SidebarFrame()
        self._sidebar = sidebar
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(216)
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(6, 16, 6, 12)
        side.setSpacing(6)

        brand_kana = _BrandLabel("んて", theme.SUBTEXT_SOFT)
        brand_kana.setObjectName("brandKana")
        side.addWidget(brand_kana)
        brand = _BrandLabel("NyaaTriggers", theme.ACCENT, glow=True, spacing=106.0)
        brand.setObjectName("brand")
        side.addWidget(brand)
        brand_ver = _BrandLabel(f"Version {_DISPLAY_VERSION}", theme.SUBTEXT_SOFT)
        brand_ver.setObjectName("brandVer")
        side.addWidget(brand_ver)
        side.addSpacing(18)

        # Keep page insertion order consistent with navigation order.
        self._stack = QStackedWidget()
        self._stack.setObjectName("pageStack")
        self._nav_group = QButtonGroup(self)
        self._nav_group.setExclusive(True)
        self._nav_buttons: list[QPushButton] = []
        # Settings is the last page but its navigation button sits above the sidebar
        # footer.
        self._nav_icon_names = ["triggers", "current", "dps", "recap", "prog", "automarkers", "settings"]
        settings_btn = None
        for idx, (icon_name, label) in enumerate(zip(self._nav_icon_names, (
            _("Triggers"), _("Current Instance"), _("DPS"), _("Death Recap"),
            _("Prog"), _("Automarkers"), _("Settings"),
        ))):
            b = _NavButton(label)
            b.setObjectName("navItem")
            b.setCheckable(True)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setIconSize(QSize(17, 17))
            b.setShortcut(f"Ctrl+{idx + 1}")
            self._nav_group.addButton(b, idx)
            if icon_name == "settings":
                settings_btn = b
            else:
                side.addWidget(b)
            self._nav_buttons.append(b)
        self._nav_group.idClicked.connect(self._on_nav_clicked)
        side.addStretch()
        side.addWidget(settings_btn)
        shell.addWidget(sidebar)

        content = QWidget()
        content.setObjectName("contentCol")
        content_col = QVBoxLayout(content)
        content_col.setContentsMargins(12, 10, 12, 10)
        content_col.setSpacing(8)
        shell.addWidget(content, 1)

        # Connection status
        conn = QHBoxLayout()
        conn.addWidget(QLabel(_("WebSocket:")))
        ws_url = self._settings.get("ws_url")
        self._url_edit = QLineEdit(
            ws_url if isinstance(ws_url, str) else "ws://127.0.0.1:10501/ws")
        self._url_edit.setMinimumWidth(260)
        conn.addWidget(self._url_edit)

        self._conn_btn = QPushButton(_("Connect"))
        self._conn_btn.setMinimumWidth(120)   # "Disconnect" must fit unclipped
        self._conn_btn.clicked.connect(self._toggle_connection)
        conn.addWidget(self._conn_btn)

        self._status_lbl = QLabel(_("● Disconnected"))
        self._status_lbl.setStyleSheet("color:#f38ba8; font-weight:bold;")
        conn.addWidget(self._status_lbl)
        # Keep engine status hidden until a bridge reports.
        self._engine_status_lbl = QLabel("")
        self._engine_status_lbl.setStyleSheet("color:#8f8f9a; font-weight:bold;")
        self._engine_status_lbl.setVisible(False)
        conn.addWidget(self._engine_status_lbl)
        # Show chain failures separately from engine disconnection.
        self._engine_chain_lbl = QLabel("")
        self._engine_chain_lbl.setStyleSheet("color:#f9e2af; font-weight:bold;")
        self._engine_chain_lbl.setVisible(False)
        conn.addWidget(self._engine_chain_lbl)
        conn.addStretch()
        content_col.addLayout(conn)

        self._build_update_banner(content_col)

        content_col.addWidget(self._stack, 1)

        settings_tab = QWidget()
        settings_tab.setObjectName("auroraPage")
        _s_outer = QVBoxLayout(settings_tab)
        _s_outer.setContentsMargins(0, 0, 0, 0)
        _s_scroll = QScrollArea()
        _s_scroll.setWidgetResizable(True)
        _s_scroll.setStyleSheet("QScrollArea { border: none; }")
        _s_content = QWidget()
        settings_layout = QVBoxLayout(_s_content)
        settings_layout.setContentsMargins(20, 16, 20, 16)
        settings_layout.setSpacing(8)
        _s_scroll.setWidget(_s_content)
        _s_outer.addWidget(_s_scroll)

        self._settings_header(settings_layout, _("Program"))
        ver_lbl = QLabel(f"NyaaTriggers Version {_DISPLAY_VERSION}")
        ver_lbl.setStyleSheet("color: #8f8f9a;")
        settings_layout.addWidget(ver_lbl)
        upd_row = QHBoxLayout()
        self._chk_updates_btn = QPushButton(_("Check for Updates"))
        self._chk_updates_btn.setMaximumWidth(160)
        self._chk_updates_btn.clicked.connect(self._check_for_updates)
        upd_row.addWidget(self._chk_updates_btn)
        # Source builds rebuild the engine. Frozen builds download the published jar.
        self._te_update_btn = QPushButton(_("Update Triggevent Engine"))
        self._te_update_btn.clicked.connect(self._on_te_update_clicked)
        upd_row.addWidget(self._te_update_btn)
        upd_row.addStretch()
        settings_layout.addLayout(upd_row)

        self._auto_update_cb = QCheckBox(_("Check for updates on startup"))
        self._auto_update_cb.setChecked(bool(self._settings.get("auto_check_updates", True)))
        self._auto_update_cb.stateChanged.connect(self._on_auto_update_changed)
        settings_layout.addWidget(self._auto_update_cb)

        # Language changes require restart because widget text is constructed once.
        lang_row = QHBoxLayout()
        _lang_lbl = QLabel(_("Language:"))
        _lang_lbl.setStyleSheet("color: #8f8f9a;")
        lang_row.addWidget(_lang_lbl)
        self._ui_lang_combo = QComboBox()
        self._ui_lang_combo.addItem(_("Automatic"), "auto")
        self._ui_lang_combo.addItem(_("English"), "en")
        self._ui_lang_combo.addItem(_("日本語 (Japanese)"), "ja")
        _lang_idx = self._ui_lang_combo.findData(self._settings.get("ui_language", "auto"))
        self._ui_lang_combo.setCurrentIndex(_lang_idx if _lang_idx >= 0 else 0)
        self._ui_lang_combo.currentIndexChanged.connect(self._on_ui_language_changed)
        lang_row.addWidget(self._ui_lang_combo)
        lang_row.addStretch()
        settings_layout.addLayout(lang_row)

        self._callouts_ja_cb = QCheckBox(_("Speak and show callouts in Japanese"))
        self._callouts_ja_cb.setChecked(bool(self._settings.get(
            "callouts_localized", active_locale() == "ja")))
        self._callouts_ja_cb.stateChanged.connect(self._on_callouts_localized_changed)
        settings_layout.addWidget(self._callouts_ja_cb)

        # Automatic source rebuilds require a local toolchain.
        if not getattr(sys, "frozen", False):
            self._te_auto_update_cb = QCheckBox(_("Update Triggevent Engine on startup (pull + rebuild)"))
            self._te_auto_update_cb.setChecked(bool(self._settings.get("triggevent_auto_update", False)))
            self._te_auto_update_cb.stateChanged.connect(self._on_te_auto_update_changed)
            settings_layout.addWidget(self._te_auto_update_cb)

        link_row = QHBoxLayout()
        github_btn = QPushButton(_("GitHub"))
        github_btn.setMaximumWidth(160)
        github_btn.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(_GITHUB_URL)))
        link_row.addWidget(github_btn)
        discord_btn = QPushButton(_("Discord"))
        discord_btn.setMaximumWidth(160)
        discord_btn.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(_DISCORD_URL)))
        link_row.addWidget(discord_btn)
        link_row.addStretch()
        settings_layout.addLayout(link_row)

        settings_layout.addSpacing(6)

        self._settings_header(settings_layout, _("Connection"))
        self._auto_connect_cb = QCheckBox(_("Auto-connect on startup"))
        self._auto_connect_cb.setChecked(bool(self._settings.get("auto_connect", False)))
        self._auto_connect_cb.stateChanged.connect(self._on_auto_connect_changed)
        settings_layout.addWidget(self._auto_connect_cb)
        self._record_pulls_cb = QCheckBox(_("Record pulls to pull_logs for engine replay"))
        self._record_pulls_cb.setChecked(bool(self._settings.get("triggevent_record_pulls", False)))
        self._record_pulls_cb.stateChanged.connect(self._on_record_pulls_changed)
        settings_layout.addWidget(self._record_pulls_cb)

        char_row = QHBoxLayout()
        char_row.addWidget(QLabel(_("My character:")))
        self._char_edit = QLineEdit(self._me_name)
        self._char_edit.setPlaceholderText(_("Auto-detected on connect"))
        self._char_edit.editingFinished.connect(self._on_char_name_changed)
        char_row.addWidget(self._char_edit, stretch=1)
        settings_layout.addLayout(char_row)

        settings_layout.addSpacing(6)

        self._build_cactbot_settings(settings_layout)

        settings_layout.addSpacing(6)

        self._build_plugin_link_settings(settings_layout)

        settings_layout.addSpacing(6)

        self._build_fflogs_settings(settings_layout)

        settings_layout.addSpacing(6)

        self._build_alert_sound_settings(settings_layout)

        settings_layout.addSpacing(6)

        self._settings_header(settings_layout, _("Voice"))

        engine_row = QHBoxLayout()
        engine_row.addWidget(QLabel(_("Engine:")))
        self._tts_engine_combo = QComboBox()
        self._tts_engine_combo.addItem(_("System (OS default voice)"), userData="system")
        self._tts_engine_combo.addItem(_("Piper (offline neural)"), userData="piper")
        _eng = self._settings.get("tts_engine", default_engine())
        self._tts_engine_combo.setCurrentIndex(0 if _eng == "system" else 1)
        self._tts_engine_combo.setMaximumWidth(220)
        self._tts_engine_combo.currentIndexChanged.connect(self._on_tts_engine_changed)
        engine_row.addWidget(self._tts_engine_combo)
        engine_row.addStretch()
        settings_layout.addLayout(engine_row)

        # The voice list includes Piper and Kokoro. Japanese selections route Japanese
        # callouts to that voice, with setup on first selection.
        voice_row = QHBoxLayout()
        voice_row.addWidget(QLabel(_("Model:")))
        self._voice_combo = QComboBox()
        self._voice_combo.setMaximumWidth(360)
        self._populate_voice_combo()
        self._restore_voice_model()
        self._voice_combo.currentIndexChanged.connect(self._on_voice_changed)
        voice_row.addWidget(self._voice_combo)
        test_tts_btn = QPushButton(_("Test TTS"))
        test_tts_btn.setMaximumWidth(90)
        test_tts_btn.clicked.connect(self._test_tts_settings)
        voice_row.addWidget(test_tts_btn)
        self._kokoro_dl_btn = QPushButton(_("Download"))
        self._kokoro_dl_btn.setMaximumWidth(110)
        self._kokoro_dl_btn.clicked.connect(self._on_kokoro_download)
        # Frozen builds include Kokoro dependencies. Only the voice model needs
        # downloading.
        voice_row.addWidget(self._kokoro_dl_btn)
        voice_row.addStretch()
        settings_layout.addLayout(voice_row)
        neural_hint = QLabel(_(
            "Select a Japanese voice to download it, about 330 MB. "
            "Callouts use espeak until it is ready. Click Download to retry."))
        neural_hint.setWordWrap(True)
        neural_hint.setStyleSheet("color:#8f8f9a; font-size:11px;")
        settings_layout.addWidget(neural_hint)

        settings_layout.addSpacing(6)

        lib_lbl = QLabel(
            _('Download a Piper model and its matching <code>.onnx.json</code> from '
              '<a href="{url}">{link}</a>. Put both in your voices folder, then refresh the list.').format(
                url=_PIPER_VOICES_URL,
                link=_("the Piper voice samples page"))
        )
        lib_lbl.setWordWrap(True)
        lib_lbl.setOpenExternalLinks(True)
        lib_lbl.setStyleSheet("color: #8f8f9a; font-size: 11px;")
        settings_layout.addWidget(lib_lbl)

        lib_btns = QHBoxLayout()
        open_voices_btn = QPushButton(_("Open voices folder"))
        open_voices_btn.clicked.connect(self._open_voices_folder)
        lib_btns.addWidget(open_voices_btn)
        refresh_voices_btn = QPushButton(_("Refresh list"))
        refresh_voices_btn.setMaximumWidth(130)
        refresh_voices_btn.clicked.connect(self._refresh_voice_combo)
        lib_btns.addWidget(refresh_voices_btn)
        lib_btns.addStretch()
        settings_layout.addLayout(lib_btns)

        settings_layout.addSpacing(6)

        if not getattr(sys, 'frozen', False):
            venv_row = QHBoxLayout()
            venv_row.addWidget(QLabel(_("Piper venv:")))
            venv_path = self._settings.get("venv_path")
            self._venv_edit = QLineEdit(
                venv_path if isinstance(venv_path, str) else str(Path.home() / ".venv" / "ffxiv"))
            self._venv_edit.editingFinished.connect(self._on_venv_changed)
            venv_row.addWidget(self._venv_edit)
            venv_browse = QPushButton(_("Browse"))
            venv_browse.setMaximumWidth(70)
            venv_browse.clicked.connect(self._browse_venv)
            venv_row.addWidget(venv_browse)
            settings_layout.addLayout(venv_row)
            settings_layout.addSpacing(6)

        self._settings_header(settings_layout, _("Data"))
        trig_update_row = QHBoxLayout()
        self._update_trig_btn = QPushButton(_("Update Triggers"))
        self._update_trig_btn.setMaximumWidth(160)
        self._update_trig_btn.clicked.connect(self._update_triggers_data)
        trig_update_row.addWidget(self._update_trig_btn)
        self._restore_trig_btn = QPushButton(_("Restore from Repo"))
        self._restore_trig_btn.clicked.connect(self._restore_triggers_from_repo)
        trig_update_row.addWidget(self._restore_trig_btn)
        save_log_btn = QPushButton(_("Save log…"))
        save_log_btn.setMaximumWidth(160)
        save_log_btn.clicked.connect(self._save_raw_log)
        trig_update_row.addWidget(save_log_btn)
        trig_update_row.addStretch()
        settings_layout.addLayout(trig_update_row)
        trig_update_note = QLabel(_("Update Triggers and Restore from Repo pull the bundled trigger set from the GitHub repo and reload it. Your own triggers are never removed."))
        trig_update_note.setWordWrap(True)
        trig_update_note.setStyleSheet("color: #8f8f9a; font-size: 11px;")
        settings_layout.addWidget(trig_update_note)
        settings_layout.addSpacing(6)

        data_row = QHBoxLayout()
        export_btn = QPushButton(_("Export Triggers"))
        export_btn.setMaximumWidth(160)
        export_btn.clicked.connect(self._export_triggers)
        data_row.addWidget(export_btn)
        import_btn = QPushButton(_("Import Triggers"))
        import_btn.setMaximumWidth(160)
        import_btn.clicked.connect(self._import_triggers)
        data_row.addWidget(import_btn)
        tn_import_btn = QPushButton(_("Import Triggernometry"))
        tn_import_btn.setMaximumWidth(200)
        tn_import_btn.clicked.connect(self._import_triggernometry)
        data_row.addWidget(tn_import_btn)
        data_row.addStretch()
        settings_layout.addLayout(data_row)

        settings_layout.addStretch()

        # Trigger editor
        triggers_tab = QWidget()
        triggers_tab.setObjectName("auroraPage")
        triggers_layout = QVBoxLayout(triggers_tab)
        triggers_layout.setContentsMargins(0, 4, 0, 0)
        self._stack.addWidget(triggers_tab)

        h_split = QSplitter(Qt.Orientation.Horizontal)
        triggers_layout.addWidget(h_split, 1)

        self._tree = QTreeWidget()
        self._tree.setHeaderHidden(True)
        self._tree.setMinimumWidth(180)
        self._tree.setIndentation(16)
        # Disable tree animation temporarily when restoring expansion after a rebuild.
        self._tree.setAnimated(True)
        self._tree.setFont(QFont("Sans", 10))
        self._tree.setStyleSheet(
            "QTreeWidget::item { padding-top: 8px; padding-bottom: 8px; }"
        )
        self._tree.currentItemChanged.connect(
            lambda cur, _prev: (self._apply_tab_filter(cur), self._update_fight_controls())
        )
        self._tree.itemClicked.connect(self._on_tree_item_clicked)
        # Update arrow prefixes for clicks, double clicks and branch indicators.
        self._tree.itemExpanded.connect(lambda item: self._set_tree_arrow(item, True))
        self._tree.itemCollapsed.connect(lambda item: self._set_tree_arrow(item, False))
        self._tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._tree.customContextMenuRequested.connect(self._on_tree_context_menu)
        h_split.addWidget(self._tree)

        trig_widget = QWidget()
        trig_layout = QVBoxLayout(trig_widget)
        trig_layout.setContentsMargins(4, 0, 0, 0)
        h_split.addWidget(trig_widget)
        h_split.setStretchFactor(0, 0)
        h_split.setStretchFactor(1, 1)
        h_split.setSizes([260, 9999])

        btn_bar = QHBoxLayout()
        self._add_btn  = QPushButton(_("Add"))
        self._edit_btn = QPushButton(_("Edit"))
        self._dup_btn  = QPushButton(_("Duplicate"))
        self._del_btn  = QPushButton(_("Delete"))
        self._test_btn = QPushButton(_("Test Fire"))
        theme.apply_primary(self._add_btn)
        for b in (self._add_btn, self._edit_btn, self._dup_btn, self._del_btn, self._test_btn):
            b.setMinimumWidth(80)
            b.setMaximumWidth(120)
            btn_bar.addWidget(b)
        btn_bar.addStretch()
        self._add_btn.clicked.connect(self._add_trigger)
        self._edit_btn.clicked.connect(self._edit_trigger)
        self._dup_btn.clicked.connect(self._duplicate_trigger)
        self._del_btn.clicked.connect(self._delete_trigger)
        self._test_btn.clicked.connect(self._test_trigger)

        bulk_bar = QHBoxLayout()
        # Global toggles update every fight and refresh their source controls.
        self._enable_btn  = QPushButton(_("Global - Triggevent On/Off"))
        self._disable_btn = QPushButton(_("Global - Local On/Off"))
        for b in (self._disable_btn, self._enable_btn):
            b.setMinimumWidth(170)
            bulk_bar.addWidget(b)
        bulk_bar.addStretch()
        self._reset_all_btn = QPushButton(_("Reset to Default"))
        self._reset_all_btn.setMinimumWidth(130)
        self._reset_all_btn.setMaximumWidth(160)
        self._reset_all_btn.setStyleSheet("color: #f38ba8;")
        self._reset_all_btn.clicked.connect(self._reset_all_to_default)
        bulk_bar.addWidget(self._reset_all_btn)
        self._enable_btn.clicked.connect(self._toggle_global_tv)
        self._disable_btn.clicked.connect(self._toggle_global_local)
        trig_layout.addLayout(btn_bar)
        trig_layout.addLayout(bulk_bar)

        search_bar = QHBoxLayout()
        self._search_edit = QLineEdit()
        self._search_edit.setPlaceholderText(_("Search triggers (all fights)..."))
        self._search_edit.setClearButtonEnabled(True)
        # Debounce searches to avoid scanning all trigger rows on every keystroke.
        self._tab_filter_timer = QTimer(self)
        self._tab_filter_timer.setSingleShot(True)
        self._tab_filter_timer.setInterval(150)
        self._tab_filter_timer.timeout.connect(self._apply_tab_filter)
        self._search_edit.textChanged.connect(lambda _: self._tab_filter_timer.start())
        search_bar.addWidget(self._search_edit)
        self._search_count_lbl = QLabel("")
        self._search_count_lbl.setStyleSheet("color: #8f8f9a;")
        search_bar.addWidget(self._search_count_lbl)
        trig_layout.addLayout(search_bar)

        self._table = QTableWidget(0, len(_HEADERS))
        self._table.setHorizontalHeaderLabels([_(h) for h in _HEADERS])
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setAlternatingRowColors(True)
        self._table.verticalHeader().setVisible(False)
        self._table.doubleClicked.connect(lambda _: self._edit_trigger())

        hdr = self._table.horizontalHeader()
        hdr.setSectionResizeMode(_C_EN,    QHeaderView.ResizeMode.Fixed); self._table.setColumnWidth(_C_EN,    28)
        hdr.setSectionResizeMode(_C_ZONE,  QHeaderView.ResizeMode.Fixed); self._table.setColumnWidth(_C_ZONE,  44)
        hdr.setSectionResizeMode(_C_FIGHT, QHeaderView.ResizeMode.Fixed); self._table.setColumnWidth(_C_FIGHT, 60)
        hdr.setSectionResizeMode(_C_TYPE,  QHeaderView.ResizeMode.Fixed); self._table.setColumnWidth(_C_TYPE,  44)
        for col in (_C_NAME, _C_RE, _C_TTS):
            hdr.setSectionResizeMode(col, QHeaderView.ResizeMode.Stretch)

        self._table.itemChanged.connect(self._on_item_changed)
        self._table.cellClicked.connect(self._on_table_cell_clicked)
        self._table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._table.customContextMenuRequested.connect(self._on_table_context_menu)

        # Fight controls toggle sources independently and update their section
        # expansion.
        self._fight_bar = QWidget()
        _fb = QHBoxLayout(self._fight_bar)
        _fb.setContentsMargins(2, 4, 2, 4)
        self._fight_bar_lbl = QLabel("")
        self._fight_bar_lbl.setStyleSheet("color:#ff8399; font-weight:bold;")
        _fb.addWidget(self._fight_bar_lbl)
        _fb.addStretch(1)
        _scope_note = QLabel(_("these apply to this fight only:"))
        _scope_note.setStyleSheet("color:#8f8f9a; font-size:8pt; padding-right:4px;")
        _fb.addWidget(_scope_note)
        self._cb_local = QCheckBox(_("Local"))
        self._cb_tv    = QCheckBox(_("Triggevent"))
        for _c in (self._cb_local, self._cb_tv):
            _fb.addWidget(_c)
        self._cb_local.toggled.connect(self._on_fight_local_only_toggled)
        self._cb_tv.toggled.connect(self._on_fight_tv_only_toggled)
        self._fight_bar.setVisible(False)
        trig_layout.addWidget(self._fight_bar)
        trig_layout.addWidget(self._table, 1)
        self._build_profiles(triggers_layout)

        # Current instance
        fight_tab = QWidget()
        fight_tab.setObjectName("auroraPage")
        fight_layout = QVBoxLayout(fight_tab)
        fight_layout.setContentsMargins(4, 4, 4, 4)
        self._stack.addWidget(fight_tab)

        self._zone_lbl = QLabel(_("◉  No instance"))
        self._zone_lbl.setStyleSheet(
            "font-size:11pt; font-weight:bold; padding:6px 12px;"
            "background:#18181d; border:1px solid #26262e; border-radius:6px;"
        )
        fight_layout.addWidget(self._zone_lbl)

        inst_hint = QLabel(
            _("Right-click a line in the Easy-to-Read Log below to add a trigger. "
            "If you give it a zone it sorts to that zone; otherwise it lands in Unsorted."))
        inst_hint.setWordWrap(True)
        inst_hint.setStyleSheet("color:#8f8f9a; padding:2px;")
        fight_layout.addWidget(inst_hint)

        ability_widget = QWidget()
        ability_layout = QVBoxLayout(ability_widget)
        ability_layout.setContentsMargins(0, 4, 0, 0)

        ab_bar = QHBoxLayout()
        ab_bar.addWidget(QLabel(_("Easy-to-Read Log:")))
        self._cb_players   = QCheckBox(_("Players"));   self._cb_players.setChecked(True)
        self._cb_enemies   = QCheckBox(_("Enemies"));   self._cb_enemies.setChecked(True)
        ab_bar.addWidget(self._cb_players)
        ab_bar.addWidget(self._cb_enemies)
        sep = QLabel("|"); sep.setStyleSheet("padding:0 4px;")
        ab_bar.addWidget(sep)
        self._cb_casts     = QCheckBox(_("Casts"));     self._cb_casts.setChecked(True)
        self._cb_abilities = QCheckBox(_("Abilities")); self._cb_abilities.setChecked(True)
        self._cb_cancels   = QCheckBox(_("Cancels"));   self._cb_cancels.setChecked(False)
        self._cb_statuses  = QCheckBox(_("Statuses"));  self._cb_statuses.setChecked(False)
        ab_bar.addWidget(self._cb_casts)
        ab_bar.addWidget(self._cb_abilities)
        ab_bar.addWidget(self._cb_cancels)
        ab_bar.addWidget(self._cb_statuses)
        for cb in (self._cb_players, self._cb_enemies,
                   self._cb_casts, self._cb_abilities, self._cb_cancels,
                   self._cb_statuses):
            cb.stateChanged.connect(self._refilter_ability_log)
        ab_bar.addStretch()
        self._ability_filter_edit = QLineEdit()
        self._ability_filter_edit.setPlaceholderText(_("Filter..."))
        self._ability_filter_edit.setClearButtonEnabled(True)
        self._ability_filter_edit.setMaximumWidth(160)
        # Debounce log filtering to avoid rebuilding the panel on every keystroke.
        self._ability_filter_timer = QTimer(self)
        self._ability_filter_timer.setSingleShot(True)
        self._ability_filter_timer.setInterval(150)
        self._ability_filter_timer.timeout.connect(self._refilter_ability_log)
        self._ability_filter_edit.textChanged.connect(
            lambda _t: self._ability_filter_timer.start())
        ab_bar.addWidget(self._ability_filter_edit)
        ability_layout.addLayout(ab_bar)

        self._ability_log = QPlainTextEdit()
        self._ability_log.setReadOnly(True)
        self._ability_log.setMaximumBlockCount(MAX_ABILITY_LINES)
        self._ability_log.setFont(QFont("Monospace", 10))
        self._ability_log.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._ability_log.customContextMenuRequested.connect(self._on_ability_context_menu)
        ability_layout.addWidget(self._ability_log)
        fight_layout.addWidget(ability_widget)

        # DPS meter
        dps_tab = QWidget()
        dps_tab.setObjectName("auroraPage")
        dps_lay = QVBoxLayout(dps_tab)

        dps_controls = QHBoxLayout()
        self._dps_record_cb = QCheckBox(_("Record encounters"))
        self._dps_record_cb.setChecked(bool(self._settings.get("dps_enabled", False)))
        self._dps_record_cb.toggled.connect(self._on_dps_record_toggled)
        dps_controls.addWidget(self._dps_record_cb)
        dps_note = QLabel(_("Logs every pull to dps_logs/"))
        dps_note.setStyleSheet(f"color: {theme.SUBTEXT0}; font-size: 8pt;")
        dps_controls.addWidget(dps_note)
        dps_controls.addSpacing(16)
        dps_controls.addWidget(QLabel(_("Reset display after:")))
        self._dps_idle_combo = QComboBox()
        self._dps_idle_combo.setMinimumWidth(110)
        for label, secs in (("15s", 15), ("30s", 30), ("1m", 60), ("2m", 120),
                            ("3m", 180), ("4m", 240), ("5m", 300), ("10m", 600)):
            self._dps_idle_combo.addItem(label, userData=secs)
        self._dps_idle_combo.setToolTip(
            _("Pause and reset the on-screen meter after this much damage "
              "downtime. The recorded pull always keeps its full length."))
        # Use the same validated idle timeout as the meter.
        idx = self._dps_idle_combo.findData(self._dps_idle_timeout)
        self._dps_idle_combo.setCurrentIndex(idx if idx >= 0 else 3)
        self._dps_idle_combo.currentIndexChanged.connect(self._on_dps_idle_changed)
        dps_controls.addWidget(self._dps_idle_combo)
        dps_controls.addStretch()
        dps_open = QPushButton(_("Open folder"))
        dps_open.clicked.connect(self._open_dps_folder)
        dps_controls.addWidget(dps_open)
        dps_lay.addLayout(dps_controls)
        dps_plugin_note = QLabel(
            _("Requires plugin for in-game overlay; see settings page"))
        dps_plugin_note.setStyleSheet(
            f"color: {theme.SUBTEXT0}; font-size: 8pt;")
        dps_lay.addWidget(dps_plugin_note)

        # Keep the final encounter visible until another starts.
        live_header = QWidget()
        live_header_lay = QHBoxLayout(live_header)
        live_header_lay.setContentsMargins(0, 0, 0, 0)
        self._dps_live_title = QLabel(_("No active encounter"))
        self._dps_live_title.setStyleSheet(
            f"color: {theme.ACCENT}; font-weight: bold; padding: 2px 4px;")
        live_header_lay.addWidget(self._dps_live_title)
        self._dps_live_encdps = QLabel("")
        self._dps_live_encdps.setStyleSheet(f"color: {theme.SUBTEXT0}; padding: 2px 4px;")
        live_header_lay.addWidget(self._dps_live_encdps)
        live_header_lay.addStretch(1)
        self._fflogs_lbl = QLabel("")
        self._fflogs_lbl.setStyleSheet(f"color: {theme.SUBTEXT0}; padding: 2px 4px;")
        live_header_lay.addWidget(self._fflogs_lbl)
        self._fflogs_btn = QPushButton(_("FFLogs"))
        self._fflogs_btn.setMaximumWidth(80)
        self._fflogs_btn.setToolTip(_("Re-fetch your FFLogs best for the last fight"))
        self._fflogs_btn.clicked.connect(self._on_fflogs_refresh)
        live_header_lay.addWidget(self._fflogs_btn)
        dps_lay.addWidget(live_header)
        self._dps_live_table = QTableWidget(0, 9)
        self._dps_live_table.setHorizontalHeaderLabels(
            [_("Name"), _("Job"), _("DPS"), _("DMG %"), _("HPS"), _("Crit %"),
             _("DH %"), _("Max Hit"), _("Deaths")])
        self._dps_live_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch)
        self._dps_live_table.verticalHeader().setVisible(False)
        self._dps_live_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        dps_lay.addWidget(self._dps_live_table, 1)

        # Review mode ends automatically when a new pull begins.
        self._dps_back_btn = QPushButton(_("<- Back to live"))
        self._dps_back_btn.setMaximumWidth(160)
        self._dps_back_btn.setVisible(False)
        self._dps_back_btn.clicked.connect(self._on_dps_back_to_live)
        dps_lay.addWidget(self._dps_back_btn)

        hist_header = QLabel(_("Recent pulls"))
        hist_header.setStyleSheet(
            f"color: {theme.SUBTEXT1}; font-weight: bold; padding: 4px 0 0 0;")
        dps_lay.addWidget(hist_header)
        self._dps_history_list = QListWidget()
        self._dps_history_list.setMaximumHeight(150)
        self._dps_history_list.itemClicked.connect(self._on_dps_history_click)
        dps_lay.addWidget(self._dps_history_list)

        self._update_fflogs_visibility()
        self._dps_timer = QTimer(self)
        self._dps_timer.setInterval(1000)
        self._dps_timer.timeout.connect(self._dps_tick)
        self._dps_timer.start()
        self._stack.addWidget(dps_tab)

        self._death_recap_tab = self._build_death_recap_tab()
        self._stack.addWidget(self._death_recap_tab)
        self._prog_tab = ProgTab(self, self._prog_sessions)
        self._stack.addWidget(self._prog_tab)

        # Automarkers
        automark_tab = QWidget()
        automark_tab.setObjectName("auroraPage")
        _am_outer = QVBoxLayout(automark_tab)
        _am_outer.setContentsMargins(0, 0, 0, 0)
        _am_scroll = QScrollArea()
        _am_scroll.setWidgetResizable(True)
        _am_scroll.setStyleSheet("QScrollArea { border: none; }")
        _am_content = QWidget()
        automark_layout = QVBoxLayout(_am_content)
        automark_layout.setContentsMargins(20, 16, 20, 16)
        automark_layout.setSpacing(8)
        self._build_automark_settings(automark_layout)
        automark_layout.addStretch(1)
        _am_scroll.setWidget(_am_content)
        _am_outer.addWidget(_am_scroll)
        self._stack.addWidget(automark_tab)

        self._stack.addWidget(settings_tab)

        vol_corner = QWidget()
        vol_corner_layout = QHBoxLayout(vol_corner)
        vol_corner_layout.setContentsMargins(0, 0, 0, 0)
        vol_corner_layout.setSpacing(6)
        # Left click toggles mute. Right click offers timed mutes.
        self._mute_until_zone = False
        self._mute_timer = QTimer(self)
        self._mute_timer.setSingleShot(True)
        self._mute_timer.timeout.connect(lambda: self._mute_btn.setChecked(False))
        self._mute_btn = QPushButton("🔊")
        self._mute_btn.setCheckable(True)
        self._mute_btn.setFixedWidth(48)
        self._mute_btn.setStyleSheet("font-size:15px;")
        self._mute_btn.toggled.connect(self._on_mute_toggled)
        self._mute_btn.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._mute_btn.customContextMenuRequested.connect(self._on_mute_context_menu)
        self._vol_slider = QSlider(Qt.Orientation.Horizontal)
        self._vol_slider.setRange(0, 200)
        try:
            cur_vol = float(self._settings.get("master_volume", 1.0))
        except (TypeError, ValueError, OverflowError):
            cur_vol = 1.0
        # Reject nonfinite values before converting to an integer.
        if not math.isfinite(cur_vol):
            cur_vol = 1.0
        self._vol_slider.setValue(int(max(0.0, min(2.0, cur_vol)) * 100))
        self._vol_slider.setMinimumWidth(60)
        self._vol_label = QLabel(f"{self._vol_slider.value()}%")
        self._vol_label.setMinimumWidth(36)
        self._vol_label.setStyleSheet("color: #ff8399;")
        self._vol_slider.valueChanged.connect(self._on_master_volume_changed)
        vol_corner_layout.addWidget(self._mute_btn)
        vol_corner_layout.addWidget(self._vol_slider, 1)
        vol_corner_layout.addWidget(self._vol_label)
        nav_sep = QFrame()
        nav_sep.setFixedHeight(1)
        nav_sep.setStyleSheet(f"background-color: {theme.EDGE}; border: none;")
        side.addSpacing(8)
        side.addWidget(nav_sep)
        side.addSpacing(8)
        side.addWidget(vol_corner)

        self._nav_buttons[0].setChecked(True)
        self._refresh_nav_icons()
        self._stack.setCurrentIndex(0)


    def _on_nav_clicked(self, idx: int) -> None:
        self._stack.setCurrentIndex(idx)
        self._refresh_nav_icons()

    def _refresh_nav_icons(self) -> None:
        # Generate icon colours per state because QSS cannot recolour them.
        for i, b in enumerate(self._nav_buttons):
            color = theme.ACCENT if b.isChecked() else theme.SUBTEXT1
            b.setIcon(theme.nav_icon(self._nav_icon_names[i], color))


    @pyqtSlot(QTableWidgetItem)
    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        if item.column() != _C_EN:
            return
        trigger_id = item.data(Qt.ItemDataRole.UserRole)
        if self._is_engine_key(trigger_id):
            self._toggle_engine_row(trigger_id, item.checkState() == Qt.CheckState.Checked)
            return
        enabled = item.checkState() == Qt.CheckState.Checked
        for t in self._triggers:
            if t.id == trigger_id:
                self._set_trigger_enabled(t, enabled)
                self._local_ids.add(trigger_id)
                break
        self._save_triggers()
        self._update_fight_controls()

    def _dedup_speak_gate(self, last_spoken: dict, text: str,
                          window_s: float, prune_s: float) -> bool:
        """Return true and record the text when an engine callout is allowed. Suppress only
        immediate duplicates and prune expired claims.
        """
        now = time.monotonic()
        key = text.casefold()
        if now - last_spoken.get(key, 0.0) < window_s:
            return False
        if len(last_spoken) > 64:
            for k in [k for k, v in last_spoken.items() if now - v >= prune_s]:
                del last_spoken[k]
        last_spoken[key] = now
        return True

    # Defer guest callouts briefly so local triggers can claim matching text first. Emit
    # guests immediately when local callouts are disabled.

    def _flush_guest(self, loc: str, severity: str, key: str) -> None:
        """Emit localized guest text and record its claim."""
        speak(loc, reading=self._reading_for(loc))
        self._emit_alert(loc, severity)
        self._callout_claimed[key] = time.monotonic() + _CALLOUT_CLAIM_S
        self._guest_claim_sev[key] = severity

    def _flush_guest_deferred(self, loc: str, key: str,
                              timer: "QTimer") -> None:
        entry = self._pending_guests.pop(key, None)
        timer.deleteLater()
        if entry is None:    # claimed or cleared while the timer was in flight
            return
        _timer, severity = entry
        if self._callout_claimed.get(key, 0.0) > time.monotonic():
            return
        self._flush_guest(loc, severity, key)


    def _fire(self, t: Trigger, fields: dict | None = None) -> None:
        if not t.tts_text and not t.sound_file:
            return
        if t.interrupt:
            tts_interrupt()
        if t.tts_text:
            # Localize before substituting tokens. Test calls use sample fields so
            # placeholders are never spoken literally.
            template = self._localized_callout(t)      # display template, kanji
            reading = self._reading_for(template)       # kana reading for TTS
            src = fields.get("source", "") if fields else ""
            tgt = fields.get("target", "") if fields else ""
            cnt = fields.get("count", "") if fields else ""
            text = (template.replace("{source}", src)
                    .replace("{target}", tgt)
                    .replace("{count}", cnt))
            spoken = (reading.replace("{source}", src)
                      .replace("{target}", tgt)
                      .replace("{count}", cnt))
            # Display kanji and provide a kana reading for speech engines that need it.
            speak(text, speed=t.speed, reading=spoken)
            self._claim_callout(text)
            self._emit_alert(text, "alarm" if t.interrupt else "info")
        if t.sound_file:
            play_sound(t.sound_file)

    def _teardown_step(self, label: str, fn) -> None:
        """Run cleanup steps independently so one failure cannot skip the remaining
        resources.
        """
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            print(f"[NyaaTriggers] teardown {label} failed: {exc!r}",
                  file=sys.stderr)

    def _stop_background_timers(self) -> None:
        """Stop periodic and pending timers before tearing down their targets. Also used by
        update restarts that bypass closeEvent.
        """
        step = self._teardown_step
        step("telesto party timer stop", lambda: self._telesto_party_timer.stop())
        step("mute timer stop", lambda: self._mute_timer.stop())
        step("plugin tick timer stop", lambda: self._plugin_tick_timer.stop())
        step("zone redetect timer stop", lambda: self._zone_redetect_timer.stop())
        step("fx timer stop", lambda: self._fx_timer.stop())
        step("dps timer stop", lambda: self._dps_timer.stop())
        step("chain flush timer stop", lambda: self._umad_chain_flush_timer.stop())
        step("gaze flush timer stop", lambda: self._umad_gaze_flush_timer.stop())
        step("ability filter timer stop", lambda: self._ability_filter_timer.stop())
        step("tab filter timer stop", lambda: self._tab_filter_timer.stop())
        step("cactbot filter timer stop", lambda: self._cactbot_trig_filter_timer.stop())

    def closeEvent(self, event) -> None:
        # Use deferred lookups so missing attributes on a partially constructed window
        # cannot stop later cleanup.
        step = self._teardown_step
        try:
            step("ws disconnect", lambda: self._ws.disconnect_from())
            step("pull capture finalize", lambda: self._pull_capture.close())
            self._stop_background_timers()
            # Keep these stops here for the closeEvent source regression check. Repeated
            # stops are harmless.
            step("chain flush timer stop", lambda: self._umad_chain_flush_timer.stop())
            step("gaze flush timer stop", lambda: self._umad_gaze_flush_timer.stop())
            step("ability filter timer stop", lambda: self._ability_filter_timer.stop())
            step("clear status timers", lambda: self._clear_status_timers())
            step("clear seq runners", lambda: self._clear_seq_runners())
            # Flush debounced writes before the event loop exits.
            step("settings save flush", lambda: self._flush_pending_settings_save())
            # Record an open encounter when closing during a fight.
            step("meter encounter finalize", lambda: self._finalize_live_encounter())
            step("cactbot reader stop", lambda: self._stop_cactbot_reader())
            step("triggevent stop", lambda: self._stop_sidecar("_triggevent"))
            step("triggernometry stop", lambda: self._stop_sidecar("_triggernometry", wait=True))
            # Request both clients to stop before joining so their shutdown waits
            # overlap.
            step("telesto stop request", lambda: self._request_sidecar_stop("_telesto_client"))
            step("plugin link stop request", lambda: self._request_sidecar_stop("_plugin_link"))
            step("telesto stop join", lambda: self._join_sidecar("_telesto_client"))
            step("plugin link stop join", lambda: self._join_sidecar("_plugin_link"))
        finally:
            super().closeEvent(event)
