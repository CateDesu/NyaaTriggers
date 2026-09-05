"""Engine rows for cactbot, Triggevent and Triggernometry, plus the
sidecar lifecycle UI. Mixin for MainWindow, all state rides on self.
"""

from pathlib import Path
import json
import re
import shutil
import sys
import time
import uuid

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QBrush, QColor
from PyQt6.QtWidgets import (
    QApplication, QDialog, QDialogButtonBox, QListWidget, QListWidgetItem, QMenu, QVBoxLayout, QHBoxLayout, QPushButton, QTableWidgetItem, QLineEdit, QLabel, QPlainTextEdit,
)

from trigger_engine import Trigger
from convert_event_trigger import REPO_TO_FIGHT
try:
    # The Import Triggernometry button. If the converter fails the button shows
    # a friendly error instead of blocking startup.
    from convert_triggernometry import convert_xml as _tn_convert_xml, load_zone_map as _tn_zone_map
except Exception:  # noqa: BLE001 - converter is optional at runtime
    _tn_convert_xml = None
    _tn_zone_map = None
from tts import speak
from locale_util import _
from cactbot_reader import CactbotReader, DEFAULT_CACTBOT_URL
from triggevent_bridge import (
    TriggeventBridge, _log as _te_log, has_java as _te_has_java, has_jar as _te_has_jar,
)
try:
    from triggernometry_bridge import TriggernometryBridge, has_packs as _tn_has_packs, \
        packs_dir as _tn_packs_dir, _log as _tn_log
except Exception:  # noqa: BLE001 - never block app load on the optional sidecar bridge
    TriggernometryBridge = None  # type: ignore
    _tn_has_packs = lambda: False  # noqa: E731
    _tn_packs_dir = None  # type: ignore
    _tn_log = lambda msg: None

import app_common as ac
from app_common import (
    _C_EN, _C_FIGHT, _C_NAME, _C_RE, _C_TTS, _C_TYPE, _C_ZONE, _SECTION_ROLE, _as_str,
    _as_strdict, _as_strset, _as_text_overrides, _atomic_write_json, _engine_preview_text,
    cactbot_timeline_for_zone,
)


class EnginesMixin:
    def _init_engines(self) -> None:
        self._engine_inventory: list[dict] = []          # read-only cactbot/triggevent rows
        # Shape filtered like _as_strset. A hand edited entry that is not a
        # dict of str fields would raise in the table build on every launch.
        self._engine_text_overrides: dict = _as_text_overrides(self._settings.get("engine_text_overrides", {}))
        # Per trigger output edits for Triggevent callouts, id to new spoken
        # text. Applied live, the sidecar rewrites its own TTS setting. Replayed
        # on each sidecar boot. Distinct from the cactbot find and replace text
        # overrides. Junk entries are dropped at load, the table build calls
        # str methods on the values.
        self._triggevent_callout_edits: dict = _as_strdict(self._settings.get("triggevent_callout_edits", {}))
        # Shipped wording fixes for engine callouts, keyed source then id then
        # text. Kept apart from the user dicts above so a later change to a
        # shipped default isn't shadowed by a copy baked into someone's
        # settings. _callout_edits_for merges the two.
        self._shipped_callout_defaults: dict = self._load_callout_defaults()
        self._triggevent_disabled: set[str] = _as_strset(self._settings.get("triggevent_disabled_triggers", []))
        # Headless Triggernometry engine sidecar, runs complex C# script triggers
        # one to one. Driven by the master Triggers switch. Only runs once a
        # pack is imported.
        self._triggernometry: "TriggernometryBridge | None" = None
        self._triggernometry_mode: bool = False
        self._triggernometry_last_spoken: dict[str, float] = {}
        # Per callout text edits plus suppression for Triggernometry rows. Same
        # model as Triggevent, the sidecar rewrites the live UseTTS text by id.
        # Same junk filter as the Triggevent edits above.
        self._triggernometry_callout_edits: dict = _as_strdict(self._settings.get("triggernometry_callout_edits", {}))
        self._triggernometry_disabled: set[str] = _as_strset(self._settings.get("triggernometry_disabled_triggers", []))
        # Unified per-source suppression sets. Cactbot reuses its existing attribute.
        self._engine_disabled: dict = {"cactbot": self._cactbot_disabled, "triggevent": self._triggevent_disabled,
                                       "triggernometry": self._triggernometry_disabled}
        # Engine triggers fire by default. An id joins the disabled set only
        # when the user unchecks it. The seen lists just track which ids have
        # ever appeared, kept for the rows' bookkeeping. Nothing is auto muted.
        self._engine_seen: dict = {
            "cactbot":        _as_strset(self._settings.get("cactbot_seen_triggers", [])),
            "triggevent":     _as_strset(self._settings.get("triggevent_seen_triggers", [])),
            "triggernometry": _as_strset(self._settings.get("triggernometry_seen_triggers", [])),
        }

    def _build_cactbot_settings(self, layout) -> None:
        """Cactbot controls. The Cactbot on/off, mutually exclusive with your
        Triggers. It is also the only switch for cactbot's timeline files:
        on, they drive the current fight's bars; off, your own timelines
        stand alone."""
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

        # ── Per-trigger overrides, best-effort ────────────────────────────────
        # stays hidden until cactbot reports its loaded trigger list. Suppressing
        # by id works off the saved set even if this list never fills in.
        self._cactbot_trig_hdr = QLabel(_("<b>Per-trigger overrides</b>  (uncheck to silence a cactbot trigger)"))
        self._cactbot_trig_hdr.setVisible(False)
        layout.addWidget(self._cactbot_trig_hdr)
        self._cactbot_trig_search = QLineEdit()
        self._cactbot_trig_search.setPlaceholderText(_("Search cactbot triggers..."))
        self._cactbot_trig_search.setClearButtonEnabled(True)
        self._cactbot_trig_search.setVisible(False)
        # Debounced like the ability filter above. A per keystroke walk over
        # several hundred rows stutters.
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
        self._cactbot_trig_list.setMaximumHeight(200)   # so it doesn't hog the Settings page
        self._cactbot_trig_list.itemChanged.connect(self._on_cactbot_trig_item_changed)
        layout.addWidget(self._cactbot_trig_list)

        if not CactbotReader.is_available():
            self._cactbot_btn.setEnabled(False)
            if getattr(sys, "frozen", False):
                # Packaged builds bundle PyQt6-WebEngine, see the spec
                # hiddenimports, so landing here means it failed to load.
                # On Linux that is usually missing system libraries. pip
                # install advice can't apply to a frozen exe.
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
        # the reader boots on a timer in __init__ so a failure can't block UI
        # construction. Reflect live state here, not the saved flag.
        self._set_cactbot_button(self._cactbot_mode)

    def _ensure_cactbot_reader(self) -> CactbotReader:
        if self._cactbot_reader is None:
            self._cactbot_reader = CactbotReader(self)
            self._cactbot_reader.callout.connect(self._on_cactbot_callout)
            self._cactbot_reader.tts.connect(self._on_cactbot_tts)
            self._cactbot_reader.status.connect(self._on_cactbot_status)
            self._cactbot_reader.triggers_enumerated.connect(self._on_cactbot_triggers_enumerated)
            self._apply_engine_overrides("cactbot")   # seed the override layer from settings
        return self._cactbot_reader

    def _stop_cactbot_reader(self) -> None:
        """stop emits its False status synchronously. Flag user/lifecycle stops
        so _on_cactbot_status doesn't read them as a load failure."""
        if self._cactbot_reader is None:
            return
        self._cactbot_teardown = True
        try:
            self._cactbot_reader.stop()
        finally:
            self._cactbot_teardown = False

    def _set_cactbot_enabled(self, enabled: bool) -> None:
        """Start/stop reading cactbot. Mutually exclusive with the master Triggers
        switch since their callouts double up, so the toggle wiring turns the
        other source off."""
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
            except Exception as exc:  # noqa: BLE001 - never let it brick the app
                self._cactbot_mode = False
                self._stop_cactbot_reader()
                self._settings["cactbot_enabled"] = False
                self._save_settings()
                self._set_cactbot_button(False)
                if hasattr(self, "_cactbot_status_lbl"):
                    self._cactbot_status_lbl.setText(_("● Error: {error}").format(error=exc))
                print(f"[cactbot] failed to enable: {exc!r}", file=sys.stderr)
                # A mode that died mid flight must not leave cactbot's
                # timeline driving the bars with the reader dead.
                if prev_mode:
                    self._load_timeline_for_zone(self._match_zone)
                return
        else:
            self._cactbot_mode = False
            self._stop_cactbot_reader()

        self._settings["cactbot_enabled"] = self._cactbot_mode
        self._save_settings()
        self._set_cactbot_button(self._cactbot_mode)
        # The Cactbot switch governs the timeline source too. Every mode
        # change re-resolves the current zone so the bars follow: cactbot's
        # own .txt files while it is on, your local timelines once it is off.
        if self._cactbot_mode != prev_mode:
            self._load_timeline_for_zone(self._match_zone)

    def _ensure_triggevent_bridge(self) -> TriggeventBridge:
        if self._triggevent is None:
            self._triggevent = TriggeventBridge(self)
            self._triggevent.callout.connect(self._on_triggevent_callout)
            self._triggevent.tts.connect(self._on_triggevent_tts)
            self._triggevent.inventory.connect(self._on_triggevent_inventory)
            self._triggevent.telesto.connect(self._on_telesto_status)   # automark link status
            self._triggevent.status.connect(
                lambda active, msg: self._on_engine_sidecar_status("triggevent", active, msg))
            self._triggevent.chain_failure.connect(self._on_engine_chain_failure)
            self._ws.raw_message.connect(self._triggevent.feed)   # the tee
            # the sidecar boots ~10s after we connect, so it misses the zone/party
            # state IINACT sends once on subscribe. When it signals ready, replay
            # the cached state so callouts arm even when the app got restarted
            # mid-instance, no reconnect or zone change needed.
            self._triggevent.ready.connect(self._ws.replay_state)
            self._apply_engine_overrides("triggevent")            # seed override layer from settings
            self._triggevent.set_disabled(self._triggevent_disabled)
        return self._triggevent

    def _triggevent_engine_needed(self) -> bool:
        """True whenever the engine is available. The sidecar is callout
        infrastructure that runs the whole session, ready the instant Cactbot is
        off. Not used for automarkers, those are native. Callouts get gated
        separately by _triggevent_mode, so a background run stays silent."""
        return TriggeventBridge.is_available()

    def _reconcile_triggevent_engine(self) -> None:
        """Start the Triggevent sidecar when it is available and not yet
        running, without touching _triggevent_mode. The callout/tts handlers
        gate on it, so the engine stays silent while Cactbot is on. Once
        available it runs the whole session, there is no stop path here.
        Best-effort, never bricks the app."""
        if not TriggeventBridge.is_available():
            return
        running = self._triggevent is not None and self._triggevent.is_active()
        want = self._triggevent_engine_needed()
        if want and not running:
            try:
                self._ensure_triggevent_bridge().start()
            except Exception as exc:  # noqa: BLE001 - never brick the app
                if self._triggevent is not None:
                    self._triggevent.stop()
                print(f"[triggevent] failed to start: {exc!r}", file=sys.stderr)
        self._update_automark_status_label()

    def _set_triggevent_enabled(self, enabled: bool) -> None:
        """Enable/disable Triggevent callouts, driven by the master Triggers
        switch. Doesn't itself start or stop the engine, that's
        _reconcile_triggevent_engine's job. Automarkers are native and unaffected."""
        if enabled and not TriggeventBridge.is_available():
            self._triggevent_mode = False
            self._settings["triggevent_enabled"] = False
            self._save_settings()
            print("[triggevent] unavailable (need Java 17 + triggevent-core.jar)",
                  file=sys.stderr)
            return
        self._triggevent_mode = bool(enabled)
        self._settings["triggevent_enabled"] = self._triggevent_mode
        self._save_settings()
        self._reconcile_triggevent_engine()

    def _on_engine_sidecar_status(self, src: str, active: bool, msg: str) -> None:
        """Lifecycle status from a sidecar bridge, connected in _ensure_*_bridge.
        Drives the top-bar engine indicator and leaves a copy in the engine's
        log so a frozen windowless build keeps a trace. A plain "Off" is a
        requested stop, not a failure."""
        state = "good" if active else ("unknown" if msg == "Off" else "bad")
        self._engine_sidecar_state[src] = (state, msg)
        (_te_log if src == "triggevent" else _tn_log)(f"status: active={active} {msg}")
        if src == "triggevent" and active:
            # A fresh engine generation starts the chain failure count over.
            self._engine_chain_failures = []
            self._update_engine_chain_label()
        self._update_engine_status_label()

    def _on_engine_chain_failure(self, line: str) -> None:
        """A Triggevent chain died, connected in _ensure_triggevent_bridge.
        Count it on the top-bar badge so a silently dead callout chain gets
        noticed mid-fight instead of in a postmortem."""
        self._engine_chain_failures.append(line)
        self._update_engine_chain_label()

    def _update_engine_chain_label(self) -> None:
        # mirrors _update_engine_status_label. Hidden until the first dead
        # chain, then amber with a session count and the latest lines on the
        # tooltip.
        lbl = getattr(self, "_engine_chain_lbl", None)
        if lbl is None:
            return
        failures = getattr(self, "_engine_chain_failures", [])
        if not failures:
            lbl.setVisible(False)
            return
        lbl.setText(_("● {n} chain failures").format(n=len(failures)))
        lbl.setToolTip("\n".join(failures[-5:]))
        lbl.setVisible(True)

    def _note_triggevent_unavailable(self) -> None:
        """Explain an empty Triggevent section. A source install ships no engine
        jar, it gets built or downloaded, and the jar needs a JVM to run. Either
        way nothing appears and nothing says why."""
        if TriggeventBridge.is_available():
            return
        if not _te_has_jar():
            msg = _("engine not installed - Settings > Update Triggevent Engine")
        elif not _te_has_java():
            msg = _("engine needs Java (Arch: sudo pacman -S jre-openjdk)")
        else:
            return
        self._engine_sidecar_state["triggevent"] = ("bad", msg)
        self._update_engine_status_label()
        # packaged builds always carry the engine, so a missing jar means a
        # source checkout, where it has to be fetched. Offer that once rather
        # than leave the section empty and hope the Settings button is found.
        # Never on the offscreen platform, a modal there is a test run hanging
        # on a dialog nobody can answer
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

    def _on_triggevent_callout(self, text: str, severity: str) -> None:
        # on-screen only. TTS comes via the separate tts signal so we never
        # double-speak when the spoken phrase differs from the on-screen text.
        # While Cactbot is on the engine still runs but must stay silent.
        if not self._triggevent_mode:
            return
        self._emit_alert(self._localize_text(text), severity)

    def _triggevent_speak(self, text: str) -> None:
        # The bridge emits at most one tts per CalloutEvent and real
        # Triggevent speaks every callout, so speak each as-is.
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
                lambda active, msg: self._on_engine_sidecar_status("triggernometry", active, msg))
            self._ws.log_line.connect(self._triggernometry.feed_log)           # log tee, no-op until started
            self._ws.combatants.connect(self._triggernometry.feed_combatants)  # positions/HP for ${_me}
            self._ws.zone_changed.connect(self._triggernometry.feed_zone)       # zone changes -> ${_ffxivzoneid}
            self._apply_engine_overrides("triggernometry")   # seed the override layer from settings
            self._triggernometry.set_disabled(self._triggernometry_disabled)
        return self._triggernometry

    def _set_triggernometry_enabled(self, enabled: bool) -> None:
        """Start or stop the headless Triggernometry sidecar. Driven by the
        master Triggers switch. It runs the complex or scripted imported
        triggers the converter cannot make Local. No-op when unavailable or
        when no Triggernometry pack has been imported. Never bricks the app
        on failure."""
        if enabled:
            if (TriggernometryBridge is None or not TriggernometryBridge.is_available()
                    or not self._has_triggernometry_packs()):
                self._triggernometry_mode = False
                return
            try:
                br = self._ensure_triggernometry_bridge()
                br.start()
                # The sidecar boots with no zone, and feed_zone only fires
                # on a ChangeZone the bridge existed for. Replay the cached
                # zone so zone-filtered triggers arm on a mid-instance
                # restart.
                if self._current_zone:
                    br.feed_zone(self._current_zone_id, self._current_zone)
                self._ws.set_combatant_polling(True)
                self._triggernometry_mode = True
            except Exception as exc:  # noqa: BLE001 - never brick the app
                self._triggernometry_mode = False
                if self._triggernometry is not None:
                    self._triggernometry.stop()
                print(f"[triggernometry] failed to enable: {exc!r}", file=sys.stderr)
        else:
            self._triggernometry_mode = False
            if self._triggernometry is not None:
                self._triggernometry.stop()
            try:
                # The combatants snapshots also feed the UMAD chain job
                # backfill, so polling stays on while that feature is on.
                self._ws.set_combatant_polling(bool(self._umad_chain_enabled))
            except Exception:  # noqa: BLE001
                pass

    def _on_triggernometry_callout(self, text: str, severity: str) -> None:
        # Same teardown guard as the Triggevent siblings. Callouts already
        # in flight when the engine was switched off must not speak.
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
        """True if at least one Triggernometry pack has been imported."""
        try:
            return bool(_tn_has_packs())
        except Exception:  # noqa: BLE001
            return False

    def _on_triggernometry_inventory(self, payload: str) -> None:
        """Sidecar reported its editable UseTTS callouts as JSON,
        [{id,name,fight,text}]. Merge into the unified engine inventory as
        Triggernometry rows."""
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
        self._save_triggernometry_inventory_cache()      # so the rows list on the next open, before boot
        self._record_engine_seen("triggernometry")
        self._apply_engine_disabled("triggernometry")   # push the user's disabled set to the sidecar
        self._refresh_table()
        self._replay_triggernometry_callout_edits()      # re-apply saved text edits to the fresh sidecar

    def _save_triggernometry_inventory_cache(self) -> None:
        """Persist the harvested Triggernometry inventory so rows list on
        the next launch. Only a non-empty harvest is authoritative. An
        empty one drops the stale cache rather than masking a good one."""
        tn = [e for e in self._engine_inventory if e.get("source") == "triggernometry"]
        try:
            if tn:
                _atomic_write_json(ac._TRIGGERNOMETRY_INVENTORY_CACHE, tn)
            else:
                ac._TRIGGERNOMETRY_INVENTORY_CACHE.unlink(missing_ok=True)
        except OSError as exc:
            # Regenerable, so no UI warning, but a read-only install losing
            # the harvest every run must leave a trace, same as other saves.
            print(f"[NyaaTriggers] could not save the Triggernometry inventory cache: {exc}",
                  file=sys.stderr)
            ac.log_drop("save", f"triggernometry inventory cache: {exc}")

    def _load_cached_triggernometry_inventory(self) -> None:
        """Seed the engine inventory from the last Triggernometry harvest
        so rows list on open. Replaced by a real harvest when the sidecar
        boots."""
        try:
            parsed = json.loads(ac._TRIGGERNOMETRY_INVENTORY_CACHE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(parsed, list) or not parsed:
            return
        self._engine_inventory = [e for e in self._engine_inventory if e.get("source") != "triggernometry"]
        # Same field coercion as the live harvest, so a hand edited cache
        # can't park a non-string id or name where a sorted call would raise.
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
        # Register the cached ids as seen. New ids fire by default, only a
        # manual uncheck disables one, see _record_engine_seen.
        self._record_engine_seen("triggernometry")

    def _replay_triggernometry_callout_edits(self) -> None:
        """Re-send all stored Triggernometry callout text edits to a freshly
        booted sidecar."""
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
            # "Default" means our shipped wording where we ship one. Only fall through
            # to the engine's own text when we don't.
            shipped = self._shipped_callout_defaults.get("triggernometry", {}).get(tid)
            if shipped is not None:
                bridge.set_callout(tid, tts=shipped, text=shipped)
            else:
                bridge.reset_callout(tid)
        self._refresh_table()

    def _on_triggevent_inventory(self, payload: str) -> None:
        """Sidecar reported its loadable callouts as JSON,
        [{id,name,fight,text}]. Merge into the unified engine inventory and
        rebuild the Engine Triggers table."""
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
        self._save_triggevent_inventory_cache()   # so the rows list on the next open
        self._record_engine_seen("triggevent")
        self._refresh_table()
        # Fresh sidecar. Re-apply saved output edits and re-sync the Telesto client.
        self._replay_triggevent_callout_edits()
        self._apply_automark_state()

    def _save_triggevent_inventory_cache(self) -> None:
        """Persist the harvested Triggevent inventory so rows list on the
        next launch. Only a non-empty harvest is authoritative. Persisting
        "[]" would mask the shipped first-run seed on every later launch, so
        drop any stale cache instead until a real harvest succeeds."""
        tv = [e for e in self._engine_inventory if e.get("source") == "triggevent"]
        try:
            if tv:
                _atomic_write_json(ac._TRIGGEVENT_INVENTORY_CACHE, tv)
            else:
                ac._TRIGGEVENT_INVENTORY_CACHE.unlink(missing_ok=True)
        except OSError as exc:
            # Regenerable, so no UI warning, but a read-only install losing
            # the harvest every run must leave a trace, same as other saves.
            print(f"[NyaaTriggers] could not save the Triggevent inventory cache: {exc}",
                  file=sys.stderr)
            ac.log_drop("save", f"triggevent inventory cache: {exc}")

    def _load_cached_triggevent_inventory(self) -> None:
        """Seed the engine inventory so triggers list on open. Prefers the writable
        cache. A fresh install falls back to the bundled snapshot. Either is
        replaced by a real harvest once the sidecar runs."""
        tv = None
        for src in (ac._TRIGGEVENT_INVENTORY_CACHE, ac._TRIGGEVENT_INVENTORY_SEED):
            try:
                parsed = json.loads(src.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            # A non-empty list wins. An empty "[]" cache falls through to the
            # shipped seed instead of masking it.
            if isinstance(parsed, list) and parsed:
                tv = parsed
                break
        if not isinstance(tv, list):
            return
        self._engine_inventory = [e for e in self._engine_inventory
                                  if e.get("source") != "triggevent"]
        # Same field coercion as the live harvest, so a hand edited cache
        # can't park a non-string id or name where a sorted call would raise.
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
        # Register the cached ids as seen. New ids fire by default, only a
        # manual uncheck disables one, see _record_engine_seen.
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
        # When Cactbot actually starts, silence your callouts. When it stops
        # or fails to start, bring them back. Gate on _cactbot_mode, not
        # `checked`, so a failed start doesn't mute you.
        self._set_triggers_enabled(not self._cactbot_mode)

    def _on_cactbot_url_changed(self) -> None:
        url = self._cactbot_url_edit.text().strip()
        saved = self._settings.get("cactbot_url")
        if url == (saved if isinstance(saved, str) else DEFAULT_CACTBOT_URL):
            return   # unchanged text, a bare focus-out must not re-save or restart the reader
        self._settings["cactbot_url"] = url
        self._save_settings()
        if self._cactbot_mode and self._cactbot_reader is not None:
            self._stop_cactbot_reader()
            self._set_cactbot_enabled(True)
            if not self._cactbot_mode:
                # The restart failed, so cactbot is off while your own
                # callouts are still muted from when it ran. Bring them
                # back like _on_cactbot_toggled does on a failed start.
                self._set_triggers_enabled(True)

    def _on_cactbot_callout(self, text: str, severity: str) -> None:
        # Guest. Own triggers win, so route through the cross-source dedup.
        # The popup and cactbotSay events cactbot emits for one trigger
        # collapse to a single callout there too.
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
        # The page load failed asynchronously after start returned, so the
        # mute of every other callout source applied on _cactbot_mode = True
        # still stands while cactbot itself says nothing. Undo it, which
        # also clears the persisted cactbot_enabled so the next launch
        # doesn't re-mute, and say why.
        self._set_triggers_enabled(True)
        self._show_cactbot_warning(msg)

    def _show_cactbot_warning(self, msg: str) -> None:
        """Surface a cactbot startup failure in the banner. The update
        buttons are irrelevant here, the next update event re-shows them."""
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
        """cactbot reported its loaded trigger list. Populate the override list.
        If nothing ever arrives, the list stays hidden and suppression still
        works from the saved set."""
        try:
            meta = json.loads(payload)
        except Exception as exc:  # noqa: BLE001
            ac.log_drop("cactbot-inventory", f"{exc!r} on {payload[:140]!r}")
            return
        if not isinstance(meta, list) or not meta:
            return
        self._cactbot_triggers_meta = meta
        # Feed the unified Engine Triggers table first and record never-seen
        # ids BEFORE the checklist derives its check states from the disabled
        # set, so a trigger appearing for the first time renders ticked, it
        # fires by default, rather than dangling between the two views.
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
        # Apply live. cactbot re-reads Options.DisabledTriggers per trigger, so this
        # takes effect mid-session with no page reload.
        if self._cactbot_reader is not None:
            self._cactbot_reader.set_disabled_triggers(self._cactbot_disabled)
        self._refresh_table()

    def _engine_fight_tag(self, e: dict) -> str:
        """Map an engine trigger to a NyaaTriggers fight tag for tree
        grouping. For Triggevent the repo name maps via REPO_TO_FIGHT, e.g.
        'DMU Triggers' -> 'UMAD', else its duty name when that already
        matches a fight tag. For Cactbot, the best-effort zone string."""
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
        """Add one read-only engine-trigger row to the main table. Keyed
        'src:id' so the local-trigger paths, which key on a uuid in
        self._triggers, skip it."""
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
        # Engine rows have no per-id translation. The text-keyed phrase map
        # catches common names and callouts, "Raidwide", "Stack" and so on,
        # and passes the rest through.
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
        brush = QBrush(QColor("#1f2a3a"))   # distinct tint from local rows
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
        self._update_fight_controls()   # re-derive the per-fight box for this change

    def _edit_engine_row(self, key: str) -> None:
        """Open the editor for an engine row. Triggevent and Triggernometry
        get real output editing. A cactbot row carries no callout text, the
        enumeration payload has only id, name and zone, so there is nothing
        to edit. Unchecking the row is the silence."""
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
        src, tid = key.split(":", 1)
        menu = QMenu(self._table)
        edits = self._callout_edit_dict(src)
        if edits is not None:
            a_edit = menu.addAction(_("Edit spoken text..."))
            a_test = menu.addAction(_("Test TTS (example)"))
            a_reset = menu.addAction(_("Reset to default"))
            a_reset.setEnabled(tid in edits)
            chosen = menu.exec(global_pos)
            if chosen is a_edit:
                self._edit_engine_row(key)
            elif chosen is a_test:
                self._test_engine_callout(key)
            elif chosen is a_reset:
                self._reset_callout_edit(src, tid)
            return
        # A cactbot row carries no callout text, the enumeration payload has
        # only id, name and zone, so there is nothing to rewrite or blank.
        # Unchecking the row is the silence.
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
        """The text a row will currently speak. The user's edit or override
        if any, else the engine's default callout text."""
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
        text = self._engine_text_for_key(key)
        if not _engine_preview_text(text):
            # Text is blank or purely dynamic tokens, {groovy} or unhandled
            # {...}, that preview strips to nothing. Fall back to the
            # callout's name so the test still gives audible confirmation
            # the voice path works.
            inv = self._engine_entry_for_key(key) or {}
            text = inv.get("name") or text
        self._speak_engine_preview(text)

    def _speak_engine_preview(self, text: str) -> None:
        preview = _engine_preview_text(text)
        if preview:
            # Localize like a live engine callout, same path as
            # _on_triggevent_line, so the list's Test TTS speaks the
            # Japanese display with its kana reading, not the raw English,
            # when a Japanese callout is set up.
            localized = self._localize_text(preview)
            speak(localized, reading=self._reading_for(localized))

    def _edit_engine_text_dialog(self, title: str, label: str, initial: str):
        """A small text editor with a Test-TTS preview, for engine callout edits."""
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
        dlg.deleteLater()   # see _add_trigger
        return text, accepted

    def _sync_cactbot_list_checkstate(self, tid: str) -> None:
        """Keep the legacy CactEvent Watcher checklist in sync with the
        engine table. Both drive the same _cactbot_disabled set."""
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
        # str-coerced. A hand edited settings file can park mixed-type ids in
        # the set, and a bare sorted would raise TypeError on every persist.
        self._settings[key] = sorted(str(x) for x in self._engine_disabled.get(src, set()))
        self._save_settings()

    def _apply_engine_disabled(self, src: str) -> None:
        if src == "cactbot" and self._cactbot_reader is not None:
            self._cactbot_reader.set_disabled_triggers(self._engine_disabled["cactbot"])
        elif src == "triggevent" and getattr(self, "_triggevent", None) is not None:
            self._triggevent.set_disabled(self._engine_disabled["triggevent"])
        elif src == "triggernometry" and getattr(self, "_triggernometry", None) is not None:
            self._triggernometry.set_disabled(self._engine_disabled["triggernometry"])

    def _record_engine_seen(self, src: str) -> None:
        """Record engine callout ids as they first appear. New ids fire by
        default. An id lands in the disabled set only when the user unchecks
        it. They used to be seeded disabled here, opt-in, which muted every
        new trigger silently until it was checked by hand, so new mechanics
        "dropped" their callouts mid-prog."""
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
            # "Default" means our shipped wording where we ship one. Only fall through
            # to the engine's own text when we don't.
            shipped = self._shipped_callout_defaults.get("triggevent", {}).get(tid)
            if shipped is not None:
                bridge.set_callout(tid, tts=shipped, text=shipped)
            else:
                bridge.reset_callout(tid)
        self._refresh_table()

    def _replay_triggevent_callout_edits(self) -> None:
        """Re-send all stored Triggevent output edits to a freshly booted sidecar."""
        bridge = getattr(self, "_triggevent", None)
        if bridge is None:
            return
        for tid, text in (self._callout_edits_for("triggevent") or {}).items():
            bridge.set_callout(tid, tts=text, text=text)

    def _apply_engine_overrides(self, src: str) -> None:
        """Push the per-row engine overrides plus the manual replacement
        table into the source reader or bridge as one combined
        find->replace list."""
        # The manual table exists for cactbot and triggevent only. Anything
        # else, triggernometry, gets the per-row overrides alone. Reading the
        # triggevent table for it would apply the wrong source's rules.
        manual_key = {"cactbot": "cactbot_replacements",
                      "triggevent": "triggevent_replacements"}.get(src)
        raw = self._settings.get(manual_key, []) if manual_key else []
        # Element check. A hand-edited settings value, say a plain string,
        # would iterate into junk entries the readers then choke on.
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
        """Import a Triggernometry XML export. Simple triggers, literal
        ability id plus plain TTS, convert to Local, deduplicated and
        disabled by default. The whole pack is also staged for the headless
        engine, which runs the scripted triggers the converter cannot
        represent."""
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
            converted = _tn_convert_xml(Path(path), zone_map)
        except Exception as exc:  # noqa: BLE001 - surface any read/parse failure to the user
            ac.QMessageBox.critical(
                self, _("Import Triggernometry"),
                _("Could not read that file:\n{error}").format(error=exc))
            return
        # Stage the raw pack for the headless engine so the scripted triggers
        # still run. This managed store is why the user never hand-drops files.
        staged = False
        if TriggernometryBridge is not None and _tn_packs_dir is not None:
            try:
                packs = _tn_packs_dir()
                src = Path(path)
                target = packs / src.name
                if target.exists() and target.resolve() != src.resolve():
                    # A different export already staged under this basename.
                    # Keep both by suffixing a counter before the extension,
                    # or the new pack would silently replace the old one.
                    n = 2
                    while (packs / f"{src.stem}_{n}{src.suffix}").exists():
                        n += 1
                    target = packs / f"{src.stem}_{n}{src.suffix}"
                if target.resolve() != src.resolve():
                    shutil.copy2(path, target)
                # else the picked file already lives in the packs dir under
                # its own basename. Re-importing it is a no-op success, not
                # a SameFileError staging failure.
                staged = True
            except Exception as exc:  # noqa: BLE001 - staging is best-effort
                print(f"[triggernometry] could not stage pack for the engine: {exc!r}", file=sys.stderr)

        # The engine lists pack triggers in their own Triggernometry
        # section. Only when it can't run the pack, no engine build, no
        # Mono, staging failure, fall back to converting the simple subset
        # into Local, keeping Triggernometry out of Local on
        # engine-capable builds.
        engine_available = (TriggernometryBridge is not None
                            and TriggernometryBridge.is_available())
        engine_path = staged and engine_available
        added: list[Trigger] = []
        if not engine_path:
            # Parse up front. A malformed converted entry is broken, not
            # "already present", so it must not inflate the skipped count
            # the summary reports below.
            rows: list[Trigger] = []
            for d in converted:
                try:
                    rows.append(Trigger.from_dict(d))
                except Exception:  # noqa: BLE001 - skip any malformed converted entry, keep the rest
                    continue
            converted = rows
            # Skip rows whose id is already present. A converted pack can
            # carry ids colliding with existing triggers, uuid5 over
            # log_type plus id only, and _is_duplicate alone can't catch a
            # bare id collision, different matcher. Track seen ids too, so
            # intra-pack repeats are skipped.
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

        # Start or restart the staged engine so the new pack loads now.
        # Rows appear once the sidecar harvests its inventory.
        engine_running = False
        if staged and self._triggers_enabled and engine_available:
            self._set_triggernometry_enabled(False)
            self._set_triggernometry_enabled(True)
            engine_running = self._triggernometry_mode

        if not added and not staged and not converted:
            ac.QMessageBox.information(
                self, _("Import Triggernometry"),
                _("No importable triggers were found in that file, and the Triggernometry "
                "engine is not available in this build to run the complex or scripted ones."))
            return
        parts: list[str] = []
        if staged:
            if engine_running:
                verb = _("is now running")
            elif engine_available:
                verb = _("will run when you turn the master Triggers switch on")
            else:
                verb = _("will run once the Triggernometry engine is available")
            parts.append(_("The pack was added to the Triggernometry engine and {verb}. Its "
                           "triggers list under their own Triggernometry section in the Triggers tab, "
                           "disabled by default and kept separate from Local.").format(verb=verb))
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
        """The tag, txt_relpath pair for the current zone from the
        generated cactbot zone-id index, empty tuple when Cactbot is off
        or the zone is unmapped. Keyed on the numeric zone id, so a fight
        with no local trigger file still resolves, in any client language.
        The Cactbot switch is the single gate: its timelines only ever load
        while the switch is on, never alone next to your own triggers. They
        drive the bars silently, the reader does the talking."""
        if not self._cactbot_mode:
            return ()
        return cactbot_timeline_for_zone(self._current_zone_id)

    def _stop_sidecar(self, attr: str, wait: bool = False) -> None:
        """Stop one optional sidecar by attribute name. Missing or None is
        fine, fresh sessions and duck-typed test windows do not always have
        them all."""
        client = getattr(self, attr, None)
        if client is None:
            return
        if wait:
            client.stop(wait=True)
        else:
            client.stop()

    def _request_sidecar_stop(self, attr: str) -> None:
        """Signal one sidecar to begin stopping without joining its worker.
        Pairs with _join_sidecar so two client joins at quit overlap."""
        client = getattr(self, attr, None)
        if client is not None:
            client.request_stop()

    def _join_sidecar(self, attr: str) -> None:
        """Join one sidecar's worker after _request_sidecar_stop. Same 2 s
        ceiling stop() used."""
        client = getattr(self, attr, None)
        if client is not None:
            client.join_stopped(2.0)
