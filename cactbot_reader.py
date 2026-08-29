#!/usr/bin/env python3
"""Read live cactbot callouts into NyaaTriggers.

Runs the real cactbot raidboss in a headless QtWebEngine page that subscribes
to IINACT itself via the OVERLAY_WS url param and harvests callouts over a
QWebChannel. Nothing is reimplemented, cactbot runs its own JS and state.

Two capture points, both outside cactbot's module scope so they survive
cactbot updates.
  1. MutationObserver on #popup-text-container gives the on-screen text plus
     the severity tier, info/alert/alarm.
  2. Hook on WebSocket.prototype.send catches the cactbotSay TTS message,
     plus the initial subscribe which confirms it connected.

PyQt6-WebEngine is optional, so call is_available first. Only QtCore is
imported at module load. The heavy imports wait until start.
"""

from __future__ import annotations

import json
import re
import sys
import urllib.parse

from PyQt6.QtCore import QObject, pyqtSignal, pyqtSlot

from drop_log import log_drop
from trigger_engine import compile_user_regex, _safe_sub

# Hosted cactbot raidboss build. The "Cactbot URL" setting can point at a local one.
DEFAULT_CACTBOT_URL = "https://overlayplugin.github.io/cactbot/ui/raidboss/raidboss.html"


def is_available() -> bool:
    """True if PyQt6-WebEngine and QtWebChannel can be imported."""
    try:
        import PyQt6.QtWebChannel     # noqa: F401
        import PyQt6.QtWebEngineCore  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


# JS injected into cactbot's page, MainWorld, at document creation.
_HARVEST_JS = r"""
(function () {
  'use strict';
  var bridge = null;
  var queue = [];
  function report(kind, payload) {
    if (bridge) { try { bridge.fromCactbot(kind, payload); } catch (e) {} }
    // Bounded so a transport that never appears can't pile every harvested
    // event into the queue for the whole session.
    else if (queue.length < 200) { queue.push([kind, payload]); }
  }

  // Connect the QWebChannel bridge (retry until the transport is injected,
  // backing off so a transport that never appears doesn't busy-poll forever).
  var connectDelay = 50;
  function connect() {
    if (typeof qt === 'undefined' || !qt.webChannelTransport) {
      connectDelay = Math.min(connectDelay * 2, 1000);
      setTimeout(connect, connectDelay);
      return;
    }
    new QWebChannel(qt.webChannelTransport, function (channel) {
      bridge = channel.objects.harvester;
      report('status', JSON.stringify({ event: 'bridge-ready' }));
      while (queue.length) {
        var it = queue.shift();
        try { bridge.fromCactbot(it[0], it[1]); } catch (e) {}
      }
    });
  }
  connect();

  // (2) Hook outgoing websocket sends BEFORE cactbot opens its socket.
  //     Catches the exact cactbotSay TTS text and the initial subscribe.
  try {
    var origSend = WebSocket.prototype.send;
    WebSocket.prototype.send = function (data) {
      try {
        if (typeof data === 'string') {
          var m = JSON.parse(data);
          if (m && m.call === 'cactbotSay' && m.text)
            report('say', JSON.stringify({ text: m.text }));
          else if (m && m.call === 'subscribe')
            report('status', JSON.stringify({ event: 'subscribe', events: m.events || [] }));
        }
      } catch (e) {}
      return origSend.apply(this, arguments);
    };
    report('status', JSON.stringify({ event: 'ws-send-hooked' }));
  } catch (e) {
    report('status', JSON.stringify({ event: 'ws-hook-failed', err: String(e) }));
  }

  // (1) Observe the raidboss popup container for on-screen callouts + tier.
  function tierFromClass(cls) {
    cls = cls || '';
    if (cls.indexOf('alarm') !== -1) return 'alarm';
    if (cls.indexOf('alert') !== -1) return 'alert';
    return 'info';
  }
  function harvest(node) {
    if (!node || node.nodeType !== 1) return;
    var text = (node.innerText || node.textContent || '').trim();
    if (!text) return;
    report('popup', JSON.stringify({
      text: text,
      tier: tierFromClass(node.className),
    }));
  }
  var attachTries = 0;
  function attach() {
    var container = document.getElementById('popup-text-container');
    // Give up after ~30s and say so. A page that never renders the container
    // must not re-poll for the whole session with no signal to the host.
    if (!container) {
      attachTries++;
      if (attachTries < 100) { setTimeout(attach, 300); }
      else { report('status', JSON.stringify({ event: 'observer-gave-up' })); }
      return;
    }
    new MutationObserver(function (muts) {
      muts.forEach(function (mu) {
        for (var i = 0; i < mu.addedNodes.length; i++) harvest(mu.addedNodes[i]);
      });
    }).observe(container, { childList: true, subtree: true });
    report('status', JSON.stringify({ event: 'observer-attached' }));
  }
  attach();

  // (3) Best-effort: apply per-trigger disables + enumerate loaded triggers.
  //     cactbot suppresses a trigger whose id is in Options.DisabledTriggers.
  //     Neither the options object nor the trigger list are reliably exposed on
  //     raidboss.html, so this polls for them and silently gives up if absent
  //     (the host hides the per-trigger UI unless an enumeration arrives).
  function findOptions() {
    var cands = [window.Options, window.gOptions, window.options];
    for (var i = 0; i < cands.length; i++) {
      var o = cands[i];
      if (o && typeof o === 'object' && 'DisabledTriggers' in o) return o;
    }
    return null;
  }
  var enumerated = false, tries = 0;
  function applyAndEnumerate() {
    tries++;
    var o = findOptions();
    if (o) {
      o.DisabledTriggers = o.DisabledTriggers || {};
      // read fresh each tick, a live toggle replaces the whole map
      var disabled = (window.__nyaaDisabledTriggers || {});
      for (var k in disabled) { if (disabled[k]) o.DisabledTriggers[k] = true; }
      if (!enumerated) {
        var src = window.__raidbossLoadedTriggers || o.Triggers || null;
        if (Array.isArray(src)) {
          var list = src.map(function (t) {
            return {
              id: t.id || '',
              name: t.id || '',
              // best-effort owning zone for grouping; prefer a readable name over
              // the numeric zoneId. Any of these may be absent.
              zone: (t.zoneName || t.__zone || (t.zoneId != null ? String(t.zoneId) : '')),
            };
          }).filter(function (t) { return t.id; });
          if (list.length) { enumerated = true; report('triggers', JSON.stringify(list)); }
        }
      }
    }
    if (tries < 40) setTimeout(applyAndEnumerate, 500);   // ~20s of polling
  }
  applyAndEnumerate();
})();
"""


class _Bridge(QObject):
    """The QWebChannel-exposed object the injected JS talks to."""

    relay = pyqtSignal(str, str)   # kind, json payload

    @pyqtSlot(str, str)
    def fromCactbot(self, kind: str, payload: str) -> None:
        self.relay.emit(kind, payload)


class CactbotReader(QObject):
    """Headless cactbot raidboss harvester. Mirrors the TriggeventBridge signal
    interface. start takes the IINACT ws url."""

    callout = pyqtSignal(str, str)   # text, severity in {info, alert, alarm}
    tts     = pyqtSignal(str)        # exact spoken cactbotSay text
    status  = pyqtSignal(bool, str)  # active, message
    triggers_enumerated = pyqtSignal(str)  # JSON [{id, name, zone}] if the page exposes its triggers
    phrase_seen = pyqtSignal(str)    # a callout phrase observed for the override UI

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._profile = None
        self._page = None
        self._channel = None
        self._bridge = None
        self._active = False
        # find->replace overrides applied before emit. Atomic list swap, no locks.
        self._replacements: list[dict] = []
        self._seen: dict[str, None] = {}      # ordered set of observed phrases
        # Live disabled-trigger set. Pushed into the running page without a reload.
        self._disabled: set[str] = set()

    @staticmethod
    def is_available() -> bool:
        return is_available()

    def is_active(self) -> bool:
        return self._active

    # ------------------------------------------------------------------
    def start(self, ws_url: str, cactbot_url: str = DEFAULT_CACTBOT_URL,
              disabled_triggers=None) -> None:
        """Load cactbot headlessly and begin harvesting. Idempotent.

        `disabled_triggers` is cactbot trigger ids to suppress, best
        effort, seeded into Options.DisabledTriggers on load. Raises if
        PyQt6-WebEngine is unavailable. Guard with is_available.
        """
        if self._active:
            return

        # A custom cactbot URL on a remote host runs its JS in our page with
        # AllowRunningInsecureContent on, load-bearing for the ws://127.0.0.1
        # IINACT feed, see below. Warn but load it. The URL is user config.
        if cactbot_url != DEFAULT_CACTBOT_URL:
            host = (urllib.parse.urlparse(cactbot_url).hostname or "").lower()
            if host and host not in ("localhost", "127.0.0.1", "::1"):
                print(f"[cactbot] warning: custom Cactbot URL loads content from "
                      f"remote host {host!r} with insecure content allowed; "
                      f"only point this at hosts you trust", file=sys.stderr)

        # Deferred imports so the app runs without PyQt6-WebEngine installed.
        from PyQt6.QtCore import QFile, QIODevice, QUrl
        from PyQt6.QtWebChannel import QWebChannel
        from PyQt6.QtWebEngineCore import (
            QWebEnginePage, QWebEngineProfile, QWebEngineScript, QWebEngineSettings,
        )

        # Qt's bundled qwebchannel.js.
        f = QFile(":/qtwebchannel/qwebchannel.js")
        if not f.open(QIODevice.OpenModeFlag.ReadOnly):
            raise RuntimeError("could not load :/qtwebchannel/qwebchannel.js")
        try:
            qwebchannel_js = bytes(f.readAll()).decode("utf-8")
        finally:
            f.close()

        # Off-the-record profile + viewless page = headless. DOM, JS, and
        # WebSocket run with no window or GL surface.
        self._profile = QWebEngineProfile(self)
        self._page = QWebEnginePage(self._profile, self)
        # Load-bearing, not a leftover. The hosted cactbot page is https but it
        # subscribes to IINACT over ws://127.0.0.1. Mixed content Chromium
        # blocks by default, which would kill the whole feed.
        self._page.settings().setAttribute(
            QWebEngineSettings.WebAttribute.AllowRunningInsecureContent, True)

        self._bridge = _Bridge(self)
        self._bridge.relay.connect(self._on_message)
        self._channel = QWebChannel(self._page)
        self._channel.registerObject("harvester", self._bridge)
        self._page.setWebChannel(self._channel)   # transport -> MainWorld

        # The prelude runs before cactbot and seeds the disable set the
        # harvest script merges into Options.DisabledTriggers.
        self._disabled = {str(t) for t in (disabled_triggers or [])}
        disabled_map = {t: True for t in self._disabled}
        prelude = f"window.__nyaaDisabledTriggers = {json.dumps(disabled_map)};\n"

        script = QWebEngineScript()
        script.setName("nyaa-cactbot-harvest")
        script.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentCreation)
        script.setWorldId(QWebEngineScript.ScriptWorldId.MainWorld)
        script.setRunsOnSubFrames(False)
        script.setSourceCode(prelude + qwebchannel_js + "\n" + _HARVEST_JS)
        self._page.scripts().insert(script)

        self._page.loadFinished.connect(self._on_load_finished)
        self._page.renderProcessTerminated.connect(self._on_render_process_terminated)

        ws_param = urllib.parse.quote(ws_url, safe="")
        sep = "&" if "?" in cactbot_url else "?"
        load_url = f"{cactbot_url}{sep}OVERLAY_WS={ws_param}"
        self._active = True
        self.status.emit(True, "Loading cactbot...")
        self._page.load(QUrl(load_url))

    def stop(self) -> None:
        if not self._active and self._page is None:
            return
        self._teardown()
        self.status.emit(False, "Off")

    def _teardown(self) -> None:
        """Mark inactive and destroy the page/channel/bridge/profile. Shared by
        stop, the load-failure path and the renderer-crash path. A failed load
        must not leave _active set, since start early-returns while it is and
        a later toggle could never retry the load."""
        self._active = False
        # Forget observed phrases with the session. A phrase seen by a previous
        # cactbot instance must re-emit phrase_seen on the next one.
        self._seen.clear()
        if self._page is not None:
            # Disconnect before deleteLater, or a stop/start cycle over a URL
            # change lets the dying page's queued signals land on the slots
            # while the fresh page is already up.
            try:
                self._page.loadFinished.disconnect(self._on_load_finished)
            except TypeError:
                pass
            try:
                self._page.renderProcessTerminated.disconnect(
                    self._on_render_process_terminated)
            except TypeError:
                pass
            try:
                self._page.setWebChannel(None)
            except Exception:  # noqa: BLE001
                pass
            self._page.deleteLater()
        # The profile and bridge are parented to this long-lived reader, so
        # nulling the Python references alone kept the C++ objects and the
        # profile's browser-engine resources alive for the life of the app.
        # One leaked off-the-record profile per start/stop cycle. deleteLater
        # order matters. The page's delete event is queued first, so the
        # profile still outlives its page.
        if self._bridge is not None:
            try:
                self._bridge.relay.disconnect(self._on_message)
            except TypeError:
                pass
            self._bridge.deleteLater()
        if self._profile is not None:
            self._profile.deleteLater()
        self._page = None
        self._channel = None
        self._bridge = None
        self._profile = None

    # ------------------------------------------------------------------
    def set_disabled_triggers(self, ids) -> None:
        """Update suppressed cactbot trigger ids mid-session, no reload. cactbot
        re-reads Options.DisabledTriggers on every trigger evaluation, so a live
        push applies immediately. The caller owns persistence."""
        new = {str(t) for t in (ids or [])}
        self._disabled = new
        if not self._active or self._page is None:
            return
        from PyQt6.QtWebEngineCore import QWebEngineScript
        payload = json.dumps({t: True for t in new})   # full replace also re-enables removed ids
        js = (
            "(function(m){"
            "  var c=[window.Options,window.gOptions,window.options];"
            "  for(var i=0;i<c.length;i++){var o=c[i];"
            "    if(o&&typeof o==='object'&&'DisabledTriggers' in o){"
            "      o.DisabledTriggers=m;window.__nyaaDisabledTriggers=m;return true;}}"
            "  window.__nyaaDisabledTriggers=m;return false;"
            "})(" + payload + ");"
        )
        try:
            self._page.runJavaScript(js, QWebEngineScript.ScriptWorldId.MainWorld)
        except Exception:  # noqa: BLE001 - never let a UI toggle brick the app
            pass

    def set_replacements(self, rules: list) -> None:
        """Set find->replace overrides, dicts of find, replace, regex,
        enabled. An empty result silences the callout. Atomic list swap."""
        self._replacements = list(rules or [])

    def seen_phrases(self) -> list:
        return list(self._seen.keys())

    def _apply_replacements(self, s: str) -> str:
        rules = self._replacements
        if not rules or not s:
            return s.strip()   # same whitespace handling as the rules path
        out = s
        for r in rules:
            if not r.get("enabled", True):
                continue
            # A hand edited settings entry can hold values that are not
            # strings. Coerce so one bad rule cannot raise in this Qt slot
            # and mute every callout while it is installed.
            find = r.get("find") or ""
            if not isinstance(find, str):
                find = str(find)
            if not find:
                continue
            repl = r.get("replace", "") or ""
            if not isinstance(repl, str):
                repl = str(repl)
            pat = find if r.get("regex") else re.escape(find)
            rx = compile_user_regex(pat, re.IGNORECASE)
            if rx is None:
                continue
            # A bad backreference, \1 to a group that doesn't exist, or a
            # catastrophic pattern that timed out leaves the callout text
            # unchanged rather than dropping the whole callout from the Qt slot.
            out = _safe_sub(rx, repl, out)
        return out.strip()

    def _record_seen(self, phrase: str) -> None:
        if phrase and phrase not in self._seen:
            self._seen[phrase] = None
            if len(self._seen) > 300:
                self._seen.pop(next(iter(self._seen)))
            self.phrase_seen.emit(phrase)

    # ------------------------------------------------------------------
    def _on_load_finished(self, ok: bool) -> None:
        if not self._active:
            return
        if ok:
            self.status.emit(True, "Reading cactbot")
            # Re-assert the live disabled set so a reload can't revert it to the
            # start-time seed in the prelude.
            self.set_disabled_triggers(self._disabled)
        else:
            # Tear down as stop does so a later toggle re-runs start
            # instead of hitting its "already active" early-out, and report
            # with the reader marked inactive so the app can undo the mute it
            # applied when start returned.
            self._teardown()
            self.status.emit(False, "Failed to load cactbot (check the URL / connection)")

    def _on_render_process_terminated(self, status, exit_code) -> None:
        if not self._active:
            return
        # A dead renderer kills every callout while the page still looks
        # loaded. Tear down and report as the load-failure path does, so a
        # later toggle re-runs start and the app undoes the callout mute.
        self._teardown()
        self.status.emit(False, "Cactbot renderer crashed, local callouts are back")

    def _on_message(self, kind: str, payload: str) -> None:
        try:
            data = json.loads(payload)
        except (json.JSONDecodeError, ValueError):
            return
        # 'triggers' is the only list payload. Guard non-dicts so .get can't
        # raise in this Qt slot.
        if kind == "triggers":
            if isinstance(data, list) and data:
                self.triggers_enumerated.emit(payload)
            return
        if not isinstance(data, dict):
            return
        if kind == "popup":
            # A page on a custom cactbot_url can call the bridge with any
            # JSON. A truthy non-string text would raise in this Qt slot.
            text = data.get("text")
            raw = text.strip() if isinstance(text, str) else ""
            if raw:
                self._record_seen(raw)
                text = self._apply_replacements(raw)
                if text:
                    tier = data.get("tier", "info")
                    self.callout.emit(text, tier if tier in ("info", "alert", "alarm") else "info")
        elif kind == "say":
            text = data.get("text")
            raw = text.strip() if isinstance(text, str) else ""
            if raw:
                self._record_seen(raw)
                text = self._apply_replacements(raw)
                if text:
                    self.tts.emit(text)
        elif kind == "status":
            if data.get("event") == "subscribe":
                self.status.emit(True, "Connected to IINACT")
            else:
                # Lifecycle noise from the injected JS, bridge ready, hook
                # results, observer state. Goes to the drop log instead of
                # printing on every status event.
                log_drop("cactbot", f"status event: {data}")
