#!/usr/bin/env python3
"""Capture live cactbot callouts from a headless QtWebEngine page. A DOM observer captures
display text and severity. A WebSocket send hook captures cactbotSay speech. WebEngine
is optional and imported only when starting the reader.
"""

from __future__ import annotations

import json
import re
import sys
import urllib.parse

from PyQt6.QtCore import QObject, pyqtSignal, pyqtSlot

from nyaatriggers.drop_log import log_drop
from nyaatriggers.trigger_engine import compile_user_regex, _safe_sub

# The Cactbot URL setting can override this hosted build.
DEFAULT_CACTBOT_URL = "https://overlayplugin.github.io/cactbot/ui/raidboss/raidboss.html"


def is_available() -> bool:
    """True if PyQt6-WebEngine and QtWebChannel can be imported."""
    try:
        import PyQt6.QtWebChannel     # noqa: F401
        import PyQt6.QtWebEngineCore  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


# Inject into MainWorld before cactbot loads.
_HARVEST_JS = r"""
(function () {
  'use strict';
  var bridge = null;
  var queue = [];
  function report(kind, payload) {
    if (bridge) { try { bridge.fromCactbot(kind, payload); } catch (e) {} }
    // Bound queued events while waiting for the bridge.
    else if (queue.length < 200) { queue.push([kind, payload]); }
  }

  // Back off while waiting for the injected transport.
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

  // Hook sends before cactbot connects to capture speech and subscriptions.
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
    // Report failure if the popup container does not appear within 30 seconds.
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

  // Look for optional trigger enumeration and suppression controls.
  // The host keeps the checklist hidden if enumeration is unavailable.
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
      // Reread the map because live toggles replace it.
      var disabled = (window.__nyaaDisabledTriggers || {});
      for (var k in disabled) { if (disabled[k]) o.DisabledTriggers[k] = true; }
      if (!enumerated) {
        var src = window.__raidbossLoadedTriggers || o.Triggers || null;
        if (Array.isArray(src)) {
          var list = src.map(function (t) {
            return {
              id: t.id || '',
              name: t.id || '',
              // Prefer a zone name for grouping, falling back to its ID.
              zone: (t.zoneName || t.__zone || (t.zoneId != null ? String(t.zoneId) : '')),
            };
          }).filter(function (t) { return t.id; });
          if (list.length) { enumerated = true; report('triggers', JSON.stringify(list)); }
        }
      }
    }
    if (tries < 40) setTimeout(applyAndEnumerate, 500);
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
    """Capture headless cactbot callouts through the TriggeventBridge signal interface.
    """

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
        # Replace the rule list atomically so readers need no lock.
        self._replacements: list[dict] = []
        self._seen: dict[str, None] = {}      # ordered set of observed phrases
        self._disabled: set[str] = set()

    @staticmethod
    def is_available() -> bool:
        return is_available()

    def is_active(self) -> bool:
        return self._active

    def start(self, ws_url: str, cactbot_url: str = DEFAULT_CACTBOT_URL,
              disabled_triggers=None) -> None:
        """Start cactbot with the IINACT URL and disabled trigger IDs. Requires WebEngine.
        Repeated starts while active do nothing.
        """
        if self._active:
            return

        # Custom URLs share the mixed content allowance needed for the local IINACT
        # feed. Warn for remote hosts and load the configured URL.
        if cactbot_url != DEFAULT_CACTBOT_URL:
            host = (urllib.parse.urlparse(cactbot_url).hostname or "").lower()
            if host and host not in ("localhost", "127.0.0.1", "::1"):
                print(f"[cactbot] warning: custom Cactbot URL loads content from "
                      f"remote host {host!r} with insecure content allowed; "
                      f"only point this at hosts you trust", file=sys.stderr)

        # Defer imports so the program can run without WebEngine.
        from PyQt6.QtCore import QFile, QIODevice, QUrl
        from PyQt6.QtWebChannel import QWebChannel
        from PyQt6.QtWebEngineCore import (
            QWebEnginePage, QWebEngineProfile, QWebEngineScript, QWebEngineSettings,
        )

        f = QFile(":/qtwebchannel/qwebchannel.js")
        if not f.open(QIODevice.OpenModeFlag.ReadOnly):
            raise RuntimeError("could not load :/qtwebchannel/qwebchannel.js")
        try:
            qwebchannel_js = bytes(f.readAll()).decode("utf-8")
        finally:
            f.close()

        # Use an off the record profile and a page without a view.
        self._profile = QWebEngineProfile(self)
        self._page = QWebEnginePage(self._profile, self)
        # The hosted HTTPS page needs mixed content enabled to reach IINACT over
        # ws://127.0.0.1.
        self._page.settings().setAttribute(
            QWebEngineSettings.WebAttribute.AllowRunningInsecureContent, True)

        self._bridge = _Bridge(self)
        self._bridge.relay.connect(self._on_message)
        self._channel = QWebChannel(self._page)
        self._channel.registerObject("harvester", self._bridge)
        self._page.setWebChannel(self._channel)   # transport -> MainWorld

        # Seed disabled triggers before cactbot loads.
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
        """Clear active state and delete the browser objects so a later start can retry.
        """
        self._active = False
        # Clear observed phrases so a new session can emit them again.
        self._seen.clear()
        if self._page is not None:
            # Disconnect before deletion so queued signals from the old page cannot
            # reach the new session.
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
        # Delete the page before its profile. Both need explicit deletion because their
        # parent reader remains alive.
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

    def set_disabled_triggers(self, ids) -> None:
        """Update disabled trigger IDs without reloading. The caller saves the setting.
        """
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
        except Exception:  # noqa: BLE001
            pass

    def set_replacements(self, rules: list) -> None:
        """Replace find and replace rules atomically. An empty replacement result silences
        the callout.
        """
        self._replacements = list(rules or [])

    def seen_phrases(self) -> list:
        return list(self._seen.keys())

    def _apply_replacements(self, s: str) -> str:
        rules = self._replacements
        if not rules or not s:
            return s.strip()
        out = s
        for r in rules:
            if not r.get("enabled", True):
                continue
            # Coerce saved values so a malformed rule cannot interrupt callouts.
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
            # Leave text unchanged when a regex times out or has invalid backreferences.
            out = _safe_sub(rx, repl, out)
        return out.strip()

    def _record_seen(self, phrase: str) -> None:
        if phrase and phrase not in self._seen:
            self._seen[phrase] = None
            if len(self._seen) > 300:
                self._seen.pop(next(iter(self._seen)))
            self.phrase_seen.emit(phrase)

    def _on_load_finished(self, ok: bool) -> None:
        if not self._active:
            return
        if ok:
            self.status.emit(True, "Reading cactbot")
            # Restore the current disabled set after a reload.
            self.set_disabled_triggers(self._disabled)
        else:
            # Clear active state before reporting failure so the program can unmute
            # callouts and a later start can retry.
            self._teardown()
            self.status.emit(False, "Failed to load cactbot (check the URL / connection)")

    def _on_render_process_terminated(self, status, exit_code) -> None:
        if not self._active:
            return
        # A renderer crash leaves the page loaded but unable to call out. Tear it down
        # as for a failed load.
        self._teardown()
        self.status.emit(False, "Cactbot renderer crashed, local callouts are back")

    def _on_message(self, kind: str, payload: str) -> None:
        try:
            data = json.loads(payload)
        except (json.JSONDecodeError, ValueError):
            return
        # Only the triggers message accepts a list payload.
        if kind == "triggers":
            if isinstance(data, list) and data:
                self.triggers_enumerated.emit(payload)
            return
        if not isinstance(data, dict):
            return
        if kind == "popup":
            # Custom pages can send arbitrary JSON through the bridge.
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
                log_drop("cactbot", f"status event: {data}")
