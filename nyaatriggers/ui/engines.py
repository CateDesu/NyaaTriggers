"""Engine trigger rows and sidecar controls for MainWindow."""

from pathlib import Path
import html
import json
import os
import sys
from collections import deque

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QBrush, QColor
from PyQt6.QtWidgets import (
    QApplication, QDialog, QDialogButtonBox, QListWidget, QListWidgetItem, QMenu, QVBoxLayout, QHBoxLayout, QPushButton, QTableWidgetItem, QLineEdit, QLabel, QPlainTextEdit,
)

from nyaatriggers.trigger_engine import Trigger
from nyaatriggers.convert_event_trigger import REPO_TO_FIGHT
try:
    # Let the import button report converter failures without blocking startup.
    from nyaatriggers.convert_triggernometry import (
        MAX_XML_BYTES as _TN_MAX_XML_BYTES, convert_xml as _tn_convert_xml,
        load_zone_map as _tn_zone_map,
    )
except Exception:  # noqa: BLE001
    _tn_convert_xml = None
    _tn_zone_map = None
from nyaatriggers.tts import speak
from nyaatriggers.locale_util import _
from nyaatriggers.cactbot_reader import CactbotReader, DEFAULT_CACTBOT_URL
from nyaatriggers.triggevent_bridge import (
    TriggeventBridge, _log as _te_log, has_java as _te_has_java, has_jar as _te_has_jar,
)
from nyaatriggers.triggevent_recovery import TriggeventRecovery
from nyaatriggers.ui.custom_triggevent import CustomTriggeventMixin
from nyaatriggers.ui.triggernometry_editor import TriggernometryEditorMixin
try:
    from nyaatriggers.triggernometry_bridge import TriggernometryBridge, has_packs as _tn_has_packs, \
        packs_dir as _tn_packs_dir, _log as _tn_log
except Exception:  # noqa: BLE001
    TriggernometryBridge = None  # type: ignore
    _tn_has_packs = lambda: False  # noqa: E731
    _tn_packs_dir = None  # type: ignore
    _tn_log = lambda msg: None

from nyaatriggers import app_common as ac
from nyaatriggers.app_common import (
    _C_EN, _C_FIGHT, _C_NAME, _C_RE, _C_TTS, _C_TYPE, _C_ZONE, _SECTION_ROLE, _as_str,
    _as_strdict, _as_strset, _as_text_overrides, _atomic_write_json, _engine_preview_text,
    _stale_gen, cactbot_timeline_for_zone,
)


class EnginesMixin(CustomTriggeventMixin, TriggernometryEditorMixin):
    def _init_engines(self) -> None:
        self._engine_inventory: list[dict] = []
        self._engine_text_overrides: dict = _as_text_overrides(self._settings.get("engine_text_overrides", {}))
        self._triggevent_callout_edits: dict = _as_strdict(self._settings.get("triggevent_callout_edits", {}))
        # Keep shipped wording separate so new defaults can take effect.
        self._shipped_callout_defaults: dict = self._load_callout_defaults()
        self._triggevent_disabled: set[str] = _as_strset(self._settings.get("triggevent_disabled_triggers", []))
        self._triggernometry: "TriggernometryBridge | None" = None
        self._triggernometry_mode: bool = False
        self._triggernometry_last_spoken: dict[str, float] = {}
        self._triggernometry_callout_edits: dict = _as_strdict(self._settings.get("triggernometry_callout_edits", {}))
        self._triggernometry_disabled: set[str] = _as_strset(self._settings.get("triggernometry_disabled_triggers", []))
        self._engine_disabled: dict = {"cactbot": self._cactbot_disabled, "triggevent": self._triggevent_disabled,
                                       "triggernometry": self._triggernometry_disabled}
        # Seen IDs are bookkeeping. Only disabled IDs mute new triggers.
        self._engine_seen: dict = {
            "cactbot":        _as_strset(self._settings.get("cactbot_seen_triggers", [])),
            "triggevent":     _as_strset(self._settings.get("triggevent_seen_triggers", [])),
            "triggernometry": _as_strset(self._settings.get("triggernometry_seen_triggers", [])),
        }
        self._init_custom_triggevent()

    def _build_cactbot_settings(self, layout) -> None:
        """The Cactbot switch gates its callouts and timelines together."""
        self._settings_header(layout, _("Cactbot"))

        desc = QLabel(
            _("Runs the real cactbot raidboss engine and speaks its callouts. "
            "Cactbot's triggers can't be edited."))
        desc.setWordWrap(True)
        desc.setStyleSheet("color:#8f8f9a;")
        layout.addWidget(desc)

        row = QHBoxLayout()
        self._cactbot_btn = QPushButton(_("Cactbot: OFF"))
        self._cactbot_btn.setCheckable(True)
        self._cactbot_btn.setMinimumWidth(150)
        self._cactbot_btn.setStyleSheet("font-weight:bold; color:#f38ba8;")
        row.addWidget(self._cactbot_btn)
        self._cactbot_status_lbl = QLabel(_("● Off"))
        self._cactbot_status_lbl.setStyleSheet("color:#8f8f9a; font-weight:bold;")
        row.addWidget(self._cactbot_status_lbl)
        row.addStretch()
        layout.addLayout(row)

        url_row = QHBoxLayout()
        url_row.addWidget(QLabel(_("Cactbot URL:")))
        cactbot_url = self._settings.get("cactbot_url")
        self._cactbot_url_edit = QLineEdit(
            cactbot_url if isinstance(cactbot_url, str) else DEFAULT_CACTBOT_URL)
        self._cactbot_url_edit.editingFinished.connect(self._on_cactbot_url_changed)
        url_row.addWidget(self._cactbot_url_edit)
        layout.addLayout(url_row)

        # Saved suppression works before the checklist arrives.
        self._cactbot_trig_hdr = QLabel(_("<b>Per-trigger overrides</b>  (uncheck to silence a cactbot trigger)"))
        self._cactbot_trig_hdr.setVisible(False)
        layout.addWidget(self._cactbot_trig_hdr)
        self._cactbot_trig_search = QLineEdit()
        self._cactbot_trig_search.setPlaceholderText(_("Search cactbot triggers..."))
        self._cactbot_trig_search.setClearButtonEnabled(True)
        self._cactbot_trig_search.setVisible(False)
        # Debounce filtering to avoid rebuilding hundreds of rows per keystroke.
        self._cactbot_trig_filter_timer = QTimer(self)
        self._cactbot_trig_filter_timer.setSingleShot(True)
        self._cactbot_trig_filter_timer.setInterval(150)
        self._cactbot_trig_filter_timer.timeout.connect(
            lambda: self._filter_cactbot_trig_list(self._cactbot_trig_search.text()))
        self._cactbot_trig_search.textChanged.connect(
            lambda _t: self._cactbot_trig_filter_timer.start())
        layout.addWidget(self._cactbot_trig_search)
        self._cactbot_trig_list = QListWidget()
        self._cactbot_trig_list.setVisible(False)
        self._cactbot_trig_list.setMaximumHeight(200)
        self._cactbot_trig_list.itemChanged.connect(self._on_cactbot_trig_item_changed)
        layout.addWidget(self._cactbot_trig_list)

        if not CactbotReader.is_available():
            self._cactbot_btn.setEnabled(False)
            if getattr(sys, "frozen", False):
                # Frozen builds already bundle WebEngine. Report missing system dependencies.
                self._cactbot_status_lbl.setText(_("● WebEngine failed to load"))
                note = QLabel(
                    _("This build ships PyQt6-WebEngine, but it could not be "
                      "loaded. On Linux this usually means missing system "
                      "libraries such as libnss3. Every other trigger source "
                      "works here."))
            else:
                self._cactbot_status_lbl.setText(_("● Needs PyQt6-WebEngine"))
                note = QLabel(
                    _("This feature needs the optional PyQt6-WebEngine package. "
                    "Install it and restart.\n"
                    "    Arch / CachyOS:  sudo pacman -S python-pyqt6-webengine\n"
                    "    Other / pip:     pip install PyQt6-WebEngine"))
            note.setWordWrap(True)
            note.setStyleSheet("color:#f9e2af;")
            layout.addWidget(note)
            return

        self._cactbot_btn.toggled.connect(self._on_cactbot_toggled)
        # Reader startup is deferred until after UI construction.
        self._set_cactbot_button(self._cactbot_mode)

    def _ensure_cactbot_reader(self) -> CactbotReader:
        if self._cactbot_reader is None:
            self._cactbot_reader = CactbotReader(self)
            self._cactbot_reader.callout.connect(self._on_cactbot_callout)
            self._cactbot_reader.tts.connect(self._on_cactbot_tts)
            self._cactbot_reader.status.connect(self._on_cactbot_status)
            self._cactbot_reader.triggers_enumerated.connect(self._on_cactbot_triggers_enumerated)
            self._apply_engine_overrides("cactbot")
        return self._cactbot_reader

    def _stop_cactbot_reader(self) -> None:
        """Mark requested stops so their synchronous status signal is not treated as a load
        failure.
        """
        if self._cactbot_reader is None:
            return
        self._cactbot_teardown = True
        try:
            self._cactbot_reader.stop()
        finally:
            self._cactbot_teardown = False

    def _set_cactbot_enabled(self, enabled: bool) -> None:
        """Start or stop cactbot and reload timelines when its mode changes."""
        prev_mode = self._cactbot_mode
        if enabled:
            try:
                reader = self._ensure_cactbot_reader()
                ws_url = self._url_edit.text().strip() or "ws://127.0.0.1:10501/ws"
                url = (self._cactbot_url_edit.text().strip()
                       if hasattr(self, "_cactbot_url_edit") else "")
                reader.start(ws_url, url or DEFAULT_CACTBOT_URL,
                              disabled_triggers=self._cactbot_disabled)
                self._cactbot_mode = True
            except Exception as exc:  # noqa: BLE001
                self._cactbot_mode = False
                self._stop_cactbot_reader()
                self._settings["cactbot_enabled"] = False
                self._save_settings()
                self._set_cactbot_button(False)
                if hasattr(self, "_cactbot_status_lbl"):
                    self._cactbot_status_lbl.setText(_("● Error: {error}").format(error=exc))
                print(f"[cactbot] failed to enable: {exc!r}", file=sys.stderr)
                # Stop cactbot timelines if the reader fails.
                if prev_mode:
                    self._load_timeline_for_zone(self._match_zone)
                return
        else:
            self._cactbot_mode = False
            self._stop_cactbot_reader()

        self._settings["cactbot_enabled"] = self._cactbot_mode
        self._save_settings()
        self._set_cactbot_button(self._cactbot_mode)
        # The Cactbot switch also selects timelines.
        if self._cactbot_mode != prev_mode:
            self._load_timeline_for_zone(self._match_zone)

    def _ensure_triggevent_bridge(self) -> TriggeventBridge:
        if self._triggevent is None:
            self._triggevent = TriggeventBridge(self)
            self._triggevent.callout.connect(self._on_triggevent_callout)
            self._triggevent.tts.connect(self._on_triggevent_tts)
            self._triggevent.inventory.connect(self._on_triggevent_inventory)
            self._triggevent.telesto.connect(self._on_telesto_status)
            self._triggevent.status.connect(
                lambda active, msg, gen: self._on_engine_sidecar_status("triggevent", active, msg, gen))
            self._triggevent.chain_failure.connect(self._on_engine_chain_failure)
            self._triggevent.ready.connect(self._on_custom_triggevent_ready)
            self._triggevent.custom_status.connect(self._on_custom_triggevent_status)
            self._triggevent_recovery = TriggeventRecovery(
                self._triggevent, self._ws, self._find_iinact_log_dir, self)
            self._triggevent.combatants_request.connect(self._on_triggevent_combatants_request)
            self._ws.set_engine_combatant_polling(True)
            self._apply_engine_overrides("triggevent")
            self._triggevent.set_disabled(self._triggevent_disabled)
        return self._triggevent

    def _on_triggevent_combatants_request(self, ids, generation):
        if not _stale_gen(self._triggevent, generation):
            self._ws.request_engine_combatants(ids)

    def _triggevent_engine_needed(self) -> bool:
        return TriggeventBridge.is_available()

    def _reconcile_triggevent_engine(self) -> None:
        """Run the sidecar independently of callout mode. Startup failure must not block the UI."""
        if not TriggeventBridge.is_available():
            return
        running = self._triggevent is not None and self._triggevent.is_active()
        want = self._triggevent_engine_needed()
        if want and not running:
            try:
                self._ensure_triggevent_bridge().start()
            except Exception as exc:  # noqa: BLE001
                if self._triggevent is not None:
                    self._triggevent.stop()
                print(f"[triggevent] failed to start: {exc!r}", file=sys.stderr)
        self._update_automark_status_label()

    def _set_triggevent_enabled(self, enabled: bool) -> None:
        """Change callouts without affecting engine lifecycle or native automarkers."""
        if enabled and not TriggeventBridge.is_available():
            self._triggevent_mode = False
            print("[triggevent] unavailable (need Java 17 + triggevent-core.jar)",
                  file=sys.stderr)
            return
        self._triggevent_mode = bool(enabled)
        self._reconcile_triggevent_engine()
        self._sync_custom_triggevent()

    def _on_engine_sidecar_status(self, src: str, active: bool, msg: str,
                                  gen: "int | None" = None) -> None:
        """Off means a requested stop."""
        # Reject queued status from an earlier engine generation.
        bridge = (getattr(self, "_triggevent", None) if src == "triggevent"
                  else getattr(self, "_triggernometry", None))
        if _stale_gen(bridge, gen):
            return
        state = "good" if active else ("unknown" if msg == "Off" else "bad")
        self._engine_sidecar_state[src] = (state, msg)
        (_te_log if src == "triggevent" else _tn_log)(f"status: active={active} {msg}")
        if src == "triggevent" and active:
            self._engine_chain_failures = deque(maxlen=50)
            self._engine_chain_failure_count = 0
            self._update_engine_chain_label()
        self._update_engine_status_label()

    def _on_engine_chain_failure(self, line: str, gen: "int | None" = None) -> None:
        # Ignore buffered errors from a sidecar generation that has already stopped.
        if _stale_gen(getattr(self, "_triggevent", None), gen):
            return
        failures = getattr(self, "_engine_chain_failures", None)
        if failures is None:
            failures = self._engine_chain_failures = deque(maxlen=50)
        # Keep only recent tooltip lines while preserving the total error count.
        failures.append(line)
        self._engine_chain_failure_count = getattr(
            self, "_engine_chain_failure_count", 0) + 1
        self._update_engine_chain_label()

    def _update_engine_chain_label(self) -> None:
        lbl = getattr(self, "_engine_chain_lbl", None)
        if lbl is None:
            return
        count = getattr(self, "_engine_chain_failure_count", 0)
        if not count:
            lbl.setVisible(False)
            return
        lbl.setText(_("● {n} chain failures").format(n=count))
        failures = getattr(self, "_engine_chain_failures", [])
        # Escape trigger names because Qt tooltips interpret markup.
        lbl.setToolTip("\n".join(html.escape(line) for line in list(failures)[-5:]))
        lbl.setVisible(True)

    def _note_triggevent_unavailable(self) -> None:
        if TriggeventBridge.is_available():
            return
        if not _te_has_jar():
            msg = _("Engine not installed. Open Settings > Program > Update Triggevent Engine.")
        elif not _te_has_java():
            msg = _("engine needs Java (Arch: sudo pacman -S jre-openjdk)")
        else:
            return
        self._engine_sidecar_state["triggevent"] = ("bad", msg)
        self._update_engine_status_label()
        # Offer installation once, except during offscreen tests.
        headless = QApplication.platformName() == "offscreen"
        if (not _te_has_jar() and not getattr(sys, "frozen", False) and not headless
                and not self._settings.get("te_engine_offer_declined")):
            if ac.QMessageBox.question(
                self, _("Install the Triggevent Engine?"),
                _("The Triggevent Engine is not installed, so none of its callouts "
                  "can run.\n\nDownload it now? It is about 80 MB, and takes effect "
                  "after a restart.")) == ac.QMessageBox.StandardButton.Yes:
                self._maybe_update_triggevent(manual=True)
            else:
                self._settings["te_engine_offer_declined"] = True
                self._save_settings()

    def _on_triggevent_callout(self, text: str, severity: str,
                               gen: "int | None" = None) -> None:
        # Speech arrives separately. Reject stale generations and disabled callouts.
        if _stale_gen(getattr(self, "_triggevent", None), gen):
            return
        if not self._triggevent_mode:
            return
        self._emit_alert(self._localize_text(text), severity)

    def _triggevent_speak(self, text: str) -> None:
        if not text:
            return
        if not self._dedup_speak_gate(self._triggevent_last_spoken, text, 0.3, 2.0):
            return
        speak(self._localize_text(text), reading=self._reading_for(self._localize_text(text)))

    def _ensure_triggernometry_bridge(self):
        if self._triggernometry is None:
            self._triggernometry = TriggernometryBridge(self)
            self._triggernometry.callout.connect(self._on_triggernometry_callout)
            self._triggernometry.tts.connect(self._on_triggernometry_tts)
            self._triggernometry.sound.connect(self._on_triggernometry_sound)
            self._triggernometry.inventory.connect(self._on_triggernometry_inventory)
            self._triggernometry.status.connect(
                lambda active, msg, gen: self._on_engine_sidecar_status("triggernometry", active, msg, gen))
            self._ws.log_line.connect(self._triggernometry.feed_log)
            self._ws.combatants.connect(self._triggernometry.feed_combatants)  # positions/HP for ${_me}
            self._ws.zone_changed.connect(self._triggernometry.feed_zone)       # zone changes -> ${_ffxivzoneid}
            self._apply_engine_overrides("triggernometry")
            self._triggernometry.set_disabled(self._triggernometry_disabled)
        return self._triggernometry

    def _set_triggernometry_enabled(self, enabled: bool) -> None:
        """Start or stop the engine for imported Triggernometry packs."""
        if enabled:
            if (TriggernometryBridge is None or not TriggernometryBridge.is_available()
                    or not self._has_triggernometry_packs()):
                self._triggernometry_mode = False
                return
            try:
                br = self._ensure_triggernometry_bridge()
                br.configure_telesto(self._settings.get("telesto_uri"),
                                     self._settings.get("telesto_enabled", False))
                br.start()
                # Zone filters need cached metadata after startup.
                if self._current_zone:
                    br.feed_zone(self._current_zone_id, self._current_zone)
                self._ws.set_combatant_polling(True)
                self._triggernometry_mode = True
            except Exception as exc:  # noqa: BLE001
                self._triggernometry_mode = False
                if self._triggernometry is not None:
                    self._triggernometry.stop()
                print(f"[triggernometry] failed to enable: {exc!r}", file=sys.stderr)
        else:
            self._triggernometry_mode = False
            if self._triggernometry is not None:
                self._triggernometry.stop()
            try:
                # Keep combatant polling active when UMAD chains need role information.
                self._ws.set_combatant_polling(bool(self._umad_chain_enabled))
            except Exception:  # noqa: BLE001
                pass

    def _on_triggernometry_callout(self, text: str, severity: str,
                                   gen: "int | None" = None) -> None:
        # Ignore callouts after disabling the engine or starting a newer generation.
        if _stale_gen(getattr(self, "_triggernometry", None), gen):
            return
        if not self._triggernometry_mode:
            return
        self._emit_alert(self._localize_text(text), severity)

    def _triggernometry_speak(self, text: str) -> None:
        if not text:
            return
        if not self._dedup_speak_gate(self._triggernometry_last_spoken, text, 0.3, 2.0):
            return
        speak(self._localize_text(text), reading=self._reading_for(self._localize_text(text)))

    def _has_triggernometry_packs(self) -> bool:
        try:
            return bool(_tn_has_packs())
        except Exception:  # noqa: BLE001
            return False

    def _on_triggernometry_inventory(self, payload: str, generation=None) -> None:
        if getattr(self, "_triggernometry_reload_pending", False):
            return
        if generation is not None and _stale_gen(getattr(self, "_triggernometry", None), generation):
            return
        try:
            items = json.loads(payload)
        except Exception as exc:  # noqa: BLE001
            ac.log_drop("tn-inventory", f"{exc!r} on {payload[:140]!r}")
            return
        if not isinstance(items, list):
            return
        self._engine_inventory = [e for e in self._engine_inventory if e.get("source") != "triggernometry"]
        for e in items:
            if not isinstance(e, dict):
                continue
            tid = _as_str(e.get("id"))
            if not tid:
                continue
            self._engine_inventory.append({
                "source": "triggernometry", "id": tid,
                "fight": _as_str(e.get("fight")), "group": _as_str(e.get("group")),
                "name": _as_str(e.get("name")) or tid, "text": _as_str(e.get("text")),
            })
        self._save_triggernometry_inventory_cache()
        self._record_engine_seen("triggernometry")
        self._apply_engine_disabled("triggernometry")
        self._refresh_table()
        self._replay_triggernometry_callout_edits()

    def _save_triggernometry_inventory_cache(self) -> None:
        """Remove empty inventory caches so stale rows cannot return at startup."""
        tn = [e for e in self._engine_inventory if e.get("source") == "triggernometry"]
        try:
            if tn:
                _atomic_write_json(ac._TRIGGERNOMETRY_INVENTORY_CACHE, tn)
            else:
                ac._TRIGGERNOMETRY_INVENTORY_CACHE.unlink(missing_ok=True)
        except OSError as exc:
            print(f"[NyaaTriggers] could not save the Triggernometry inventory cache: {exc}",
                  file=sys.stderr)
            ac.log_drop("save", f"triggernometry inventory cache: {exc}")

    def _load_cached_triggernometry_inventory(self) -> None:
        """Show cached Triggernometry rows until a live harvest replaces them."""
        try:
            parsed = json.loads(ac._TRIGGERNOMETRY_INVENTORY_CACHE.read_text(encoding="utf-8"))
        except (OSError, ValueError, RecursionError):
            return
        if not isinstance(parsed, list) or not parsed:
            return
        self._engine_inventory = [e for e in self._engine_inventory if e.get("source") != "triggernometry"]
        for e in parsed:
            if not isinstance(e, dict):
                continue
            tid = _as_str(e.get("id"))
            if not tid:
                continue
            self._engine_inventory.append({
                "source": "triggernometry", "id": tid,
                "fight": _as_str(e.get("fight")), "group": _as_str(e.get("group")),
                "name": _as_str(e.get("name")) or tid, "text": _as_str(e.get("text")),
            })
        self._record_engine_seen("triggernometry")

    def _replay_triggernometry_callout_edits(self) -> None:
        bridge = getattr(self, "_triggernometry", None)
        if bridge is None:
            return
        for tid, text in (self._callout_edits_for("triggernometry") or {}).items():
            bridge.set_callout(tid, tts=text, text=text)

    def _set_triggernometry_callout_edit(self, tid: str, text: str) -> None:
        self._triggernometry_callout_edits[tid] = text
        self._settings["triggernometry_callout_edits"] = self._triggernometry_callout_edits
        self._save_settings()
        if getattr(self, "_triggernometry", None) is not None:
            self._triggernometry.set_callout(tid, tts=text, text=text)
        self._refresh_table()

    def _reset_triggernometry_callout_edit(self, tid: str) -> None:
        self._triggernometry_callout_edits.pop(tid, None)
        self._settings["triggernometry_callout_edits"] = self._triggernometry_callout_edits
        self._save_settings()
        bridge = getattr(self, "_triggernometry", None)
        if bridge is not None:
            shipped = self._shipped_callout_defaults.get("triggernometry", {}).get(tid)
            if shipped is not None:
                bridge.set_callout(tid, tts=shipped, text=shipped)
            else:
                bridge.reset_callout(tid)
        self._refresh_table()

    def _on_triggevent_inventory(self, payload: str, generation=None) -> None:
        if generation is not None and _stale_gen(getattr(self, "_triggevent", None), generation):
            return
        try:
            items = json.loads(payload)
        except Exception as exc:  # noqa: BLE001
            ac.log_drop("te-inventory", f"{exc!r} on {payload[:140]!r}")
            return
        if not isinstance(items, list):
            return
        self._engine_inventory = [e for e in self._engine_inventory if e.get("source") != "triggevent"]
        for e in items:
            if not isinstance(e, dict):
                continue
            tid = _as_str(e.get("id"))
            if not tid:
                continue
            self._engine_inventory.append({
                "source": "triggevent", "id": tid,
                "fight": _as_str(e.get("fight")), "group": _as_str(e.get("group")),
                "name": _as_str(e.get("name")) or tid, "text": _as_str(e.get("text")),
            })
        self._save_triggevent_inventory_cache()
        self._record_engine_seen("triggevent")
        self._refresh_table()
        self._replay_triggevent_callout_edits()
        self._apply_automark_state()

    def _save_triggevent_inventory_cache(self) -> None:
        """Remove empty caches so the bundled seed remains available."""
        tv = [e for e in self._engine_inventory if e.get("source") == "triggevent"]
        try:
            if tv:
                _atomic_write_json(ac._TRIGGEVENT_INVENTORY_CACHE, tv)
            else:
                ac._TRIGGEVENT_INVENTORY_CACHE.unlink(missing_ok=True)
        except OSError as exc:
            print(f"[NyaaTriggers] could not save the Triggevent inventory cache: {exc}",
                  file=sys.stderr)
            ac.log_drop("save", f"triggevent inventory cache: {exc}")

    def _load_cached_triggevent_inventory(self) -> None:
        """Use the cache or bundled seed until a live harvest arrives."""
        tv = None
        for src in (ac._TRIGGEVENT_INVENTORY_CACHE, ac._TRIGGEVENT_INVENTORY_SEED):
            try:
                parsed = json.loads(src.read_text(encoding="utf-8"))
            except (OSError, ValueError, RecursionError):
                continue
            # A cache without usable rows must not mask the bundled seed.
            if not isinstance(parsed, list):
                continue
            valid = [e for e in parsed if isinstance(e, dict) and _as_str(e.get("id"))]
            if valid:
                tv = valid
                break
        if not isinstance(tv, list):
            return
        self._engine_inventory = [e for e in self._engine_inventory
                                  if e.get("source") != "triggevent"]
        for e in tv:
            if not isinstance(e, dict):
                continue
            tid = _as_str(e.get("id"))
            if not tid:
                continue
            self._engine_inventory.append({
                "source": "triggevent", "id": tid,
                "fight": _as_str(e.get("fight")), "group": _as_str(e.get("group")),
                "name": _as_str(e.get("name")) or tid, "text": _as_str(e.get("text")),
            })
        self._record_engine_seen("triggevent")

    def _set_cactbot_button(self, on: bool) -> None:
        if not hasattr(self, "_cactbot_btn"):
            return
        self._cactbot_btn.blockSignals(True)
        self._cactbot_btn.setChecked(on)
        self._cactbot_btn.blockSignals(False)
        self._cactbot_btn.setText(_("Cactbot: ON") if on else _("Cactbot: OFF"))
        self._cactbot_btn.setStyleSheet(
            "font-weight:bold; color:%s;" % ("#a6e3a1" if on else "#f38ba8"))

    def _on_cactbot_toggled(self, checked: bool) -> None:
        self._set_cactbot_enabled(checked)
        # Mute local callouts only after cactbot actually starts. Restore them after
        # stop or failure.
        self._set_triggers_enabled(not self._cactbot_mode)

    def _on_cactbot_url_changed(self) -> None:
        url = self._cactbot_url_edit.text().strip()
        saved = self._settings.get("cactbot_url")
        if url == (saved if isinstance(saved, str) else DEFAULT_CACTBOT_URL):
            return   # Ignore focus changes when the URL text is unchanged.
        self._settings["cactbot_url"] = url
        self._save_settings()
        if self._cactbot_mode and self._cactbot_reader is not None:
            self._stop_cactbot_reader()
            self._set_cactbot_enabled(True)
            if not self._cactbot_mode:
                # Restore local callouts after a failed cactbot restart.
                self._set_triggers_enabled(True)

    def _on_cactbot_callout(self, text: str, severity: str) -> None:
        self._emit_guest_callout(text, severity)

    def _on_cactbot_status(self, active: bool, msg: str) -> None:
        if hasattr(self, "_cactbot_status_lbl"):
            color = "#a6e3a1" if active else "#8f8f9a"
            self._cactbot_status_lbl.setStyleSheet(f"color:{color}; font-weight:bold;")
            self._cactbot_status_lbl.setText(f"● {msg}")
        if active or self._cactbot_teardown or not self._cactbot_mode:
            return
        reader = self._cactbot_reader
        if reader is not None and reader.is_active():
            return
        # Asynchronous failure must undo the mute and saved cactbot setting.
        self._set_triggers_enabled(True)
        self._show_cactbot_warning(msg)

    def _show_cactbot_warning(self, msg: str) -> None:
        if not hasattr(self, "_update_banner"):
            return
        self._update_banner_mode = "cactbot"
        self._upd_progress.setVisible(False)
        self._upd_install_btn.setVisible(False)
        self._upd_notes_btn.setVisible(False)
        self._upd_dismiss_btn.setVisible(True)
        self._upd_msg.setText(
            _("Cactbot failed to start ({msg}) - your callouts are back on.").format(msg=msg))
        self._update_banner.setVisible(True)

    def _on_cactbot_triggers_enumerated(self, payload: str) -> None:
        """Saved suppression works even before the checklist is available."""
        try:
            meta = json.loads(payload)
        except Exception as exc:  # noqa: BLE001
            ac.log_drop("cactbot-inventory", f"{exc!r} on {payload[:140]!r}")
            return
        if not isinstance(meta, list) or not meta:
            return
        self._cactbot_triggers_meta = meta
        self._engine_inventory = [e for e in self._engine_inventory if e.get("source") != "cactbot"]
        for entry in meta:
            if not isinstance(entry, dict):
                continue
            tid = _as_str(entry.get("id"))
            if not tid:
                continue
            self._engine_inventory.append({
                "source": "cactbot", "id": tid,
                "fight": _as_str(entry.get("zone")),
                "name": _as_str(entry.get("name")) or tid, "text": "",
            })
        self._record_engine_seen("cactbot")
        lst = self._cactbot_trig_list
        lst.blockSignals(True)
        lst.clear()
        for entry in meta:
            if not isinstance(entry, dict):
                continue
            tid = _as_str(entry.get("id"))
            if not tid:
                continue
            label = _as_str(entry.get("name")) or tid
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, tid)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(
                Qt.CheckState.Unchecked if tid in self._cactbot_disabled
                else Qt.CheckState.Checked
            )
            lst.addItem(item)
        lst.blockSignals(False)
        self._cactbot_trig_hdr.setVisible(True)
        self._cactbot_trig_search.setVisible(True)
        lst.setVisible(True)
        self._refresh_table()

    def _filter_cactbot_trig_list(self, text: str) -> None:
        q = text.strip().lower()
        for i in range(self._cactbot_trig_list.count()):
            it = self._cactbot_trig_list.item(i)
            it.setHidden(bool(q) and q not in it.text().lower()
                         and q not in (it.data(Qt.ItemDataRole.UserRole) or "").lower())

    def _on_cactbot_trig_item_changed(self, item: QListWidgetItem) -> None:
        tid = item.data(Qt.ItemDataRole.UserRole)
        if not tid:
            return
        if item.checkState() == Qt.CheckState.Unchecked:
            self._cactbot_disabled.add(tid)
        else:
            self._cactbot_disabled.discard(tid)
        self._settings["cactbot_disabled_triggers"] = sorted(self._cactbot_disabled)
        self._save_settings()
        # Cactbot rereads disabled IDs on each trigger.
        if self._cactbot_reader is not None:
            self._cactbot_reader.set_disabled_triggers(self._cactbot_disabled)
        self._refresh_table()

    def _engine_fight_tag(self, e: dict) -> str:
        """Group engine rows using repository or zone names."""
        if e.get("source") == "triggernometry" and e.get("fight") == "Unsorted":
            return ""
        if e.get("source") == "triggevent":
            tag = REPO_TO_FIGHT.get(e.get("group") or "")
            if tag:
                return tag
            f = e.get("fight") or ""
            return "" if f in ("", "None") else f
        return e.get("fight") or ""

    @staticmethod
    def _is_engine_key(key) -> bool:
        return isinstance(key, str) and key.split(":", 1)[0] in ("cactbot", "triggevent", "triggernometry")

    def _engine_entry_for_key(self, key: str):
        if not self._is_engine_key(key):
            return None
        src, tid = key.split(":", 1)
        return next((e for e in self._engine_inventory
                     if e.get("source") == src and e.get("id") == tid), None)

    def _append_engine_row(self, e: dict) -> None:
        """Key engine rows by source and ID so local handlers skip them."""
        src = e.get("source", "")
        tid = e.get("id", "")
        if not tid:
            return
        key = f"{src}:{tid}"
        disabled = tid in self._engine_disabled.get(src, set())
        row = self._table.rowCount()
        self._table.insertRow(row)

        def _ro(text: str) -> QTableWidgetItem:
            it = QTableWidgetItem(text)
            it.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            return it

        cb = QTableWidgetItem()
        cb.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsSelectable)
        cb.setCheckState(Qt.CheckState.Unchecked if disabled else Qt.CheckState.Checked)
        cb.setData(Qt.ItemDataRole.UserRole, key)
        cb.setData(_SECTION_ROLE, "triggernometry" if src == "triggernometry" else "engine")
        self._table.setItem(row, _C_EN, cb)
        self._table.setItem(row, _C_ZONE, _ro(""))
        # Engine rows use phrase translations because they have no translation by ID.
        self._table.setItem(row, _C_NAME, _ro(self._localize_text(e.get("name") or tid)))
        self._table.setItem(row, _C_FIGHT, _ro(self._engine_fight_tag(e)))
        self._table.setItem(row, _C_TYPE, _ro({"cactbot": "Cactbot", "triggevent": "Triggevent",
                                               "triggernometry": "Triggernometry"}.get(src, src)))
        self._table.setItem(row, _C_RE, _ro("(engine)"))
        txt = e.get("text") or ""
        _ce = self._callout_edits_for(src)
        edit = _ce.get(tid) if _ce is not None else None
        if edit is not None:
            txt = edit if edit.strip() else "(silenced)"
        else:
            over = self._engine_text_overrides.get(key)
            if over is not None:
                rep = over.get("replace", "")
                txt = rep if rep else "(silenced)"
        tts_item = _ro(self._localize_text(txt))
        self._table.setItem(row, _C_TTS, tts_item)
        brush = QBrush(QColor("#1f2a3a"))
        for col in range(self._table.columnCount()):
            cell = self._table.item(row, col)
            if cell is not None:
                cell.setBackground(brush)

    def _toggle_engine_row(self, key: str, enabled: bool) -> None:
        src, tid = key.split(":", 1)
        dset = self._engine_disabled.setdefault(src, set())
        if enabled:
            dset.discard(tid)
        else:
            dset.add(tid)
        self._persist_engine_disabled(src)
        self._apply_engine_disabled(src)
        if src == "cactbot":
            self._sync_cactbot_list_checkstate(tid)
        self._update_fight_controls()

    def _edit_engine_row(self, key: str) -> None:
        """Cactbot has no editable output text."""
        custom = self._custom_triggevent_for_key(key)
        if custom is not None:
            self._edit_custom_triggevent(custom)
            return
        src, tid = key.split(":", 1)
        inv = self._engine_entry_for_key(key) or {}
        edits = self._callout_edits_for(src)
        if edits is None:
            return
        cur = edits.get(tid, inv.get("text") or inv.get("name") or "")
        label = _("Triggernometry") if src == "triggernometry" else _("Triggevent")
        tokhint = (_("Triggernometry ${...} expressions still substitute")
                   if src == "triggernometry"
                   else _("Triggevent tokens such as {event.target} still work"))
        text, ok = self._edit_engine_text_dialog(
            _("Edit {engine} callout").format(engine=label),
            _("Spoken text ({hint}). The ▶ button speaks a sample with placeholder values.").format(hint=tokhint), cur)
        if ok:
            self._apply_callout_edit(src, tid, text)

    def _engine_row_context_menu(self, key: str, global_pos) -> None:
        if self._custom_triggevent_for_key(key) is not None:
            self._custom_triggevent_menu(key, global_pos)
            return
        src, tid = key.split(":", 1)
        menu = QMenu(self._table)
        definition_edit = None
        if src == "triggernometry":
            definition_edit = menu.addAction(_("Edit trigger and actions..."))
        edits = self._callout_edit_dict(src)
        if edits is not None:
            a_edit = menu.addAction(_("Edit spoken text..."))
            a_test = menu.addAction(_("Test TTS (example)"))
            a_reset = menu.addAction(_("Reset to default"))
            a_reset.setEnabled(tid in edits)
            chosen = menu.exec(global_pos)
            if definition_edit is not None and chosen is definition_edit:
                self._edit_triggernometry_definition(key)
            elif chosen is a_edit:
                self._edit_engine_row(key)
            elif chosen is a_test:
                self._test_engine_callout(key)
            elif chosen is a_reset:
                self._reset_callout_edit(src, tid)
            return
        # Cactbot inventory contains no editable text. Disable the row to silence it.
        a_test = menu.addAction(_("Test TTS (example)"))
        a_cl = menu.addAction(_("Clear override"))
        a_cl.setEnabled(key in self._engine_text_overrides)
        chosen = menu.exec(global_pos)
        if chosen is a_test:
            self._test_engine_callout(key)
        elif chosen is a_cl:
            self._engine_text_overrides.pop(key, None)
            self._settings["engine_text_overrides"] = self._engine_text_overrides
            self._save_settings()
            self._apply_engine_overrides(src)
            self._refresh_table()

    def _engine_text_for_key(self, key: str) -> str:
        src, tid = key.split(":", 1)
        inv = self._engine_entry_for_key(key) or {}
        edits = self._callout_edits_for(src)
        if edits is not None and tid in edits:
            return edits[tid]
        over = self._engine_text_overrides.get(key)
        if over is not None:
            return over.get("replace", "")
        return inv.get("text") or inv.get("name") or ""

    def _test_engine_callout(self, key: str) -> None:
        custom = self._custom_triggevent_for_key(key)
        if custom is not None:
            self._test_custom_triggevent(custom)
            return
        text = self._engine_text_for_key(key)
        if not _engine_preview_text(text):
            inv = self._engine_entry_for_key(key) or {}
            text = inv.get("name") or text
        self._speak_engine_preview(text)

    def _speak_engine_preview(self, text: str) -> None:
        preview = _engine_preview_text(text)
        if preview:
            # Use the same localization and kana reading as live callouts.
            localized = self._localize_text(preview)
            speak(localized, reading=self._reading_for(localized))

    def _edit_engine_text_dialog(self, title: str, label: str, initial: str):
        dlg = QDialog(self)
        dlg.setWindowTitle(title)
        dlg.setMinimumWidth(460)
        lay = QVBoxLayout(dlg)
        lbl = QLabel(label)
        lbl.setWordWrap(True)
        lay.addWidget(lbl)
        edit = QPlainTextEdit(initial)
        edit.setFixedHeight(80)
        lay.addWidget(edit)
        btn_row = QHBoxLayout()
        test_btn = QPushButton(_("▶ Test TTS"))
        test_btn.setMaximumWidth(120)
        test_btn.clicked.connect(lambda: self._speak_engine_preview(edit.toPlainText()))
        btn_row.addWidget(test_btn)
        btn_row.addStretch()
        lay.addLayout(btn_row)
        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(dlg.accept)
        btns.rejected.connect(dlg.reject)
        lay.addWidget(btns)
        accepted = dlg.exec() == QDialog.DialogCode.Accepted
        text = edit.toPlainText() if accepted else ""
        dlg.deleteLater()
        return text, accepted

    def _sync_cactbot_list_checkstate(self, tid: str) -> None:
        """Keep the engine table and cactbot checklist on the same disabled IDs."""
        lst = getattr(self, "_cactbot_trig_list", None)
        if lst is None:
            return
        disabled = tid in self._cactbot_disabled
        for i in range(lst.count()):
            it = lst.item(i)
            if it.data(Qt.ItemDataRole.UserRole) == tid:
                lst.blockSignals(True)
                it.setCheckState(Qt.CheckState.Unchecked if disabled else Qt.CheckState.Checked)
                lst.blockSignals(False)
                break

    def _persist_engine_disabled(self, src: str) -> None:
        key = {"cactbot": "cactbot_disabled_triggers",
               "triggevent": "triggevent_disabled_triggers",
               "triggernometry": "triggernometry_disabled_triggers"}.get(src, src + "_disabled_triggers")
        self._settings[key] = sorted(str(x) for x in self._engine_disabled.get(src, set()))
        self._save_settings()

    def _apply_engine_disabled(self, src: str) -> None:
        if src == "cactbot" and self._cactbot_reader is not None:
            self._cactbot_reader.set_disabled_triggers(self._engine_disabled["cactbot"])
        elif src == "triggevent" and getattr(self, "_triggevent", None) is not None:
            self._triggevent.set_disabled(self._engine_disabled["triggevent"])
            self._sync_custom_triggevent()
        elif src == "triggernometry" and getattr(self, "_triggernometry", None) is not None:
            self._triggernometry.set_disabled(self._engine_disabled["triggernometry"])

    def _record_engine_seen(self, src: str) -> None:
        """Newly discovered triggers stay enabled unless explicitly disabled."""
        if getattr(self, "_engine_seen", None) is None:
            return
        seen = self._engine_seen.setdefault(src, set())
        changed = False
        for e in self._engine_inventory:
            if e.get("source") != src:
                continue
            tid = e.get("id")
            if tid and tid not in seen:
                seen.add(tid)
                changed = True
        if changed:
            self._settings[f"{src}_seen_triggers"] = sorted(seen)
            self._save_settings()

    def _set_triggevent_callout_edit(self, tid: str, text: str) -> None:
        self._triggevent_callout_edits[tid] = text
        self._settings["triggevent_callout_edits"] = self._triggevent_callout_edits
        self._save_settings()
        if getattr(self, "_triggevent", None) is not None:
            self._triggevent.set_callout(tid, tts=text, text=text)
        self._refresh_table()

    def _reset_triggevent_callout_edit(self, tid: str) -> None:
        self._triggevent_callout_edits.pop(tid, None)
        self._settings["triggevent_callout_edits"] = self._triggevent_callout_edits
        self._save_settings()
        bridge = getattr(self, "_triggevent", None)
        if bridge is not None:
            shipped = self._shipped_callout_defaults.get("triggevent", {}).get(tid)
            if shipped is not None:
                bridge.set_callout(tid, tts=shipped, text=shipped)
            else:
                bridge.reset_callout(tid)
        self._refresh_table()

    def _replay_triggevent_callout_edits(self) -> None:
        """Replay saved Triggevent text edits after startup."""
        bridge = getattr(self, "_triggevent", None)
        if bridge is None:
            return
        for tid, text in (self._callout_edits_for("triggevent") or {}).items():
            bridge.set_callout(tid, tts=text, text=text)

    def _apply_engine_overrides(self, src: str) -> None:
        """Combine row overrides with manual replacement rules."""
        # Only cactbot and Triggevent support manual replacement tables.
        manual_key = {"cactbot": "cactbot_replacements",
                      "triggevent": "triggevent_replacements"}.get(src)
        raw = self._settings.get(manual_key, []) if manual_key else []
        manual = [r for r in raw if isinstance(r, dict)] if isinstance(raw, list) else []
        auto = [
            {"find": o["find"], "replace": o.get("replace", ""), "regex": False, "enabled": True}
            for k, o in self._engine_text_overrides.items()
            if k.startswith(src + ":") and o.get("find")
        ]
        combined = manual + auto
        if src == "cactbot" and self._cactbot_reader is not None:
            self._cactbot_reader.set_replacements(combined)
        elif src == "triggevent" and getattr(self, "_triggevent", None) is not None:
            self._triggevent.set_replacements(combined)
        elif src == "triggernometry" and getattr(self, "_triggernometry", None) is not None:
            self._triggernometry.set_replacements(combined)

    def _import_triggernometry(self) -> None:
        """Run imported packs in the sidecar, or fall back to disabled simple Local rows."""
        if _tn_convert_xml is None or _tn_zone_map is None:
            ac.QMessageBox.critical(
                self, _("Import Triggernometry"),
                _("The Triggernometry converter is unavailable in this build."))
            return
        path, _unused = ac.QFileDialog.getOpenFileName(
            self, _("Import Triggernometry"), "",
            _("Triggernometry export (*.xml)") + ";;" + _("All files (*)"))
        if not path:
            return
        try:
            zone_map  = _tn_zone_map([t.to_dict() for t in self._triggers])
            with Path(path).open("rb") as source:
                xml_bytes = source.read(_TN_MAX_XML_BYTES + 1)
            converted = _tn_convert_xml(Path(path), zone_map, content=xml_bytes, strict=True)
        except Exception as exc:  # noqa: BLE001
            ac.QMessageBox.critical(
                self, _("Import Triggernometry"),
                _("Could not read that file:\n{error}").format(error=exc))
            return
        staged = False
        stage_failed = False
        if TriggernometryBridge is not None and _tn_packs_dir is not None:
            try:
                packs = _tn_packs_dir()
                src = Path(path)
                suffix = src.suffix if src.suffix.lower() == ".xml" else ".xml"
                n = 1
                while True:
                    ending = suffix if n == 1 else f"_{n}{suffix}"
                    stem = src.stem
                    # Leave room for the suffix and counter without splitting Unicode.
                    while len(os.fsencode(stem + ending)) > 255:
                        stem = stem[:-1]
                    target = packs / (stem + ending)
                    if not target.exists() or target.resolve() == src.resolve():
                        break
                    n += 1
                ac._atomic_write_bytes(target, xml_bytes)
                staged = True
            except Exception as exc:  # noqa: BLE001
                stage_failed = True
                print(f"[triggernometry] could not stage pack for the engine: {exc!r}", file=sys.stderr)
                ac.QMessageBox.warning(self, _("Import Triggernometry"),
                                      _("Could not write file:\n{error}").format(error=exc))

        # Convert simple triggers only when the sidecar cannot run the pack.
        engine_available = (TriggernometryBridge is not None
                            and TriggernometryBridge.is_available())
        engine_path = staged and engine_available
        added: list[Trigger] = []
        if not engine_path:
            rows: list[Trigger] = []
            for d in converted:
                try:
                    rows.append(Trigger.from_dict(d))
                except Exception:  # noqa: BLE001
                    continue
            converted = rows
            # Check IDs as well as matchers, including repeats within this import.
            seen_ids = {x.id for x in self._triggers}
            for t in converted:
                if t.id in seen_ids:
                    continue
                if self._is_duplicate(t) is not None:
                    continue
                self._triggers.append(t)
                self._local_ids.add(t.id)
                seen_ids.add(t.id)
                added.append(t)
            if added:
                self._save_triggers()
                self._refresh_table()
                self._refresh_tree()

        engine_running = False
        if staged and self._triggers_enabled and engine_available:
            self._set_triggernometry_enabled(False)
            self._set_triggernometry_enabled(True)
            engine_running = self._triggernometry_mode

        if not added and not staged and not converted:
            if stage_failed:
                return
            ac.QMessageBox.information(
                self, _("Import Triggernometry"),
                _("No importable triggers were found in that file, and the Triggernometry "
                "engine is not available in this build to run the complex or scripted ones."))
            return
        parts: list[str] = []
        if staged:
            if engine_running:
                verb = _("is now running")
            elif engine_available and not self._triggers_enabled:
                verb = _("will run when you turn Cactbot off")
            elif engine_available:
                verb = _("was imported, but the engine could not start")
            else:
                verb = _("will run once the Triggernometry engine is available")
            parts.append(_("The pack {verb}. Find it under Triggernometry in the Triggers tab. "
                           "New engine triggers are enabled by default.").format(verb=verb))
        if added:
            skipped = len(converted) - len(added)
            parts.append(_("{count} simple trigger(s) were imported into Local, disabled by default"
                           "{skipped} - a fallback for running without the Triggernometry engine.").format(
                count=len(added),
                skipped=(_(" (skipped {n} already present)").format(n=skipped) if skipped else "")))
        elif not staged and converted:
            parts.append(_("All {count} convertible trigger(s) were already in your set.").format(
                count=len(converted)))
        ac.QMessageBox.information(self, _("Import Triggernometry"), "\n\n".join(parts))

    def _cactbot_zone_entry(self) -> "tuple[str, str]":
        """Cactbot timelines require Cactbot mode."""
        if not self._cactbot_mode:
            return ()
        return cactbot_timeline_for_zone(self._current_zone_id)

    def _stop_sidecar(self, attr: str, wait: bool = False) -> None:
        client = getattr(self, attr, None)
        if client is None:
            return
        if wait:
            client.stop(wait=True)
        else:
            client.stop()

    def _request_sidecar_stop(self, attr: str) -> None:
        """Request all stops before joining so shutdown waits overlap."""
        client = getattr(self, attr, None)
        if client is not None:
            client.request_stop()

    def _join_sidecar(self, attr: str) -> None:
        client = getattr(self, attr, None)
        if client is not None:
            client.join_stopped(2.0)
