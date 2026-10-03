#!/usr/bin/env python3
"""Capture cactbot display text and speech through optional QtWebEngine."""

from __future__ import annotations

import json
import re
import sys
import urllib.parse

from PyQt6.QtCore import QObject, pyqtSignal, pyqtSlot

from nyaatriggers.drop_log import log_drop
from nyaatriggers.trigger_engine import compile_user_regex, _safe_sub

DEFAULT_CACTBOT_URL = "https://overlayplugin.github.io/cactbot/ui/raidboss/raidboss.html"


def is_available() -> bool:
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
    else if (queue.length < 200) { queue.push([kind, payload]); }
  }

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

  // Hook sends before cactbot connects.
  try {
    var userRequests = new WeakMap(), listeners = new WeakMap();
    var origAdd = WebSocket.prototype.addEventListener;
    var origRemove = WebSocket.prototype.removeEventListener;
    WebSocket.prototype.addEventListener = function (kind, listener, options) {
      var callback = listener;
      if (kind === 'message' && listener &&
          (typeof listener === 'function' || typeof listener === 'object')) {
        callback = listeners.get(listener);
        if (!callback) {
          callback = function (event) {
            try {
              var requests = userRequests.get(this);
              var reply = requests && requests.size && JSON.parse(event.data);
              if (reply && requests.delete(String(reply.rseq)) && !reply.$error && reply.detail) {
                var files = reply.detail.localUserFiles;
                if (files == null) files = reply.detail.localUserFiles = {};
                if (files && typeof files === 'object' && !Array.isArray(files)) {
                  var path = 'raidboss/__nyaa_reader__.js';
                  while (Object.prototype.hasOwnProperty.call(files, path)) path = path.replace('.js', '_.js');
                  files[path] = 'window.__nyaaCaptureOptions(Options);';
                  Object.defineProperty(event, 'data', { value: JSON.stringify(reply) });
                }
              }
            } catch (e) {}
            if (typeof listener === 'function') return listener.apply(this, arguments);
            return listener.handleEvent(event);
          };
          listeners.set(listener, callback);
        }
      }
      return origAdd.call(this, kind, callback, options);
    };
    WebSocket.prototype.removeEventListener = function (kind, listener, options) {
      return origRemove.call(this, kind, kind === 'message' && listeners.get(listener) || listener, options);
    };
    var origSend = WebSocket.prototype.send;
    WebSocket.prototype.send = function (data) {
      try {
        if (typeof data === 'string') {
          var m = JSON.parse(data);
          if (m && m.call === 'cactbotSay' && m.text)
            report('say', JSON.stringify({ text: m.text }));
          else if (m && m.call === 'subscribe')
            report('status', JSON.stringify({ event: 'subscribe', events: m.events || [] }));
          else if (m && m.call === 'cactbotLoadUser' &&
                   (!m.overlayName || m.overlayName === 'raidboss') && m.rseq != null) {
            var requests = userRequests.get(this);
            if (!requests) {
              requests = new Set();
              userRequests.set(this, requests);
            }
            requests.add(String(m.rseq));
          }
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

  function findOptions() {
    var cands = [window.__nyaaCactbotOptions, window.Options, window.gOptions, window.options];
    for (var i = 0; i < cands.length; i++) {
      var o = cands[i];
      if (o && typeof o === 'object' && 'DisabledTriggers' in o) return o;
    }
    return null;
  }
  var disabledTarget = null, originals = new Map();
  window.__nyaaSetDisabledTriggers = function (disabled) {
    window.__nyaaDisabledTriggers = disabled;
    var o = findOptions();
    if (!o) return false;
    var target = o.DisabledTriggers || (o.DisabledTriggers = {});
    if (target !== disabledTarget) {
      disabledTarget = target;
      originals.clear();
    }
    originals.forEach(function (descriptor, id) {
      if (descriptor) Object.defineProperty(target, id, descriptor);
      else delete target[id];
    });
    originals.clear();
    Object.keys(disabled).forEach(function (id) {
      if (!disabled[id]) return;
      originals.set(id, Object.getOwnPropertyDescriptor(target, id));
      Object.defineProperty(target, id, {value: true, writable: true, configurable: true, enumerable: true});
    });
    return true;
  };
  window.__nyaaCaptureOptions = function (options) {
    window.__nyaaCactbotOptions = options;
    setTimeout(applyAndEnumerate, 0);
  };
  var bundledSets = null;
  function bundledTriggerSets() {
    if (bundledSets !== null) return bundledSets;
    bundledSets = [];
    var chunks = window.webpackChunkcactbot;
    if (!Array.isArray(chunks)) return bundledSets;
    var ids = new Set();
    chunks.forEach(function (chunk) {
      Object.keys(chunk[1] || {}).forEach(function (id) { ids.add(id); });
    });
    try {
      chunks.push([['nyaa-reader'], {}, function (load) {
        ids.forEach(function (id) {
          try {
            Object.values(load(id)).forEach(function (files) {
              if (!files || typeof files !== 'object') return;
              Object.keys(files).forEach(function (path) {
                var set = files[path];
                if (/\.(js|ts)$/.test(path) && set && Array.isArray(set.triggers))
                  bundledSets.push(Object.assign({filename: path}, set));
              });
            });
          } catch (e) {}
        });
      }]);
    } catch (e) {}
    return bundledSets;
  }
  var enumerated = false, tries = 0;
  function applyAndEnumerate() {
    tries++;
    var o = findOptions();
    if (o) {
      window.__nyaaSetDisabledTriggers(window.__nyaaDisabledTriggers || {});
      if (!enumerated) {
        var src = bundledTriggerSets().concat(window.__raidbossLoadedTriggers || [], o.Triggers || []);
        var sets = new Map(), triggers = new Map();
        src.forEach(function (set) { if (set && Array.isArray(set.triggers)) sets.set(set.id || set, set); });
        function add(trigger, set) {
          if (!trigger || !trigger.id) return;
          triggers.set(trigger.id, {
            id: trigger.id, name: trigger.id,
            zone: set.zoneName || set.__zone || set.id || (set.zoneId != null ? String(set.zoneId) : ''),
          });
        }
        src.forEach(function (set) {
          if (!set || (Array.isArray(set.triggers) && sets.get(set.id || set) !== set)) return;
          if (Array.isArray(set.triggers)) {
            set.triggers.concat(set.timelineTriggers || []).forEach(function (trigger) { add(trigger, set); });
          } else add(set, set);
        });
        var list = Array.from(triggers.values());
        if (list.length) { enumerated = true; report('triggers', JSON.stringify(list)); }
      }
    }
    if (tries < 40) setTimeout(applyAndEnumerate, 500);
  }
  applyAndEnumerate();
})();
"""


class _Bridge(QObject):
    relay = pyqtSignal(str, str)   # kind, json payload

    @pyqtSlot(str, str)
    def fromCactbot(self, kind: str, payload: str) -> None:
        self.relay.emit(kind, payload)


class CactbotReader(QObject):
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
        self._ws_url = ""
        self._replacements: list[dict] = []
        self._seen: dict[str, None] = {}      # ordered set of observed phrases
        self._disabled: set[str] = set()

    @staticmethod
    def is_available() -> bool:
        return is_available()

    def is_active(self) -> bool:
        return self._active

    def websocket_url(self) -> str:
        return self._ws_url

    def start(self, ws_url: str, cactbot_url: str = DEFAULT_CACTBOT_URL,
              disabled_triggers=None) -> None:
        """Start once with the IINACT URL and disabled trigger IDs. Requires WebEngine."""
        if self._active:
            return
        ws_url = ws_url.strip()

        if cactbot_url != DEFAULT_CACTBOT_URL:
            host = (urllib.parse.urlparse(cactbot_url).hostname or "").lower()
            if host and host not in ("localhost", "127.0.0.1", "::1"):
                print(f"[cactbot] warning: custom Cactbot URL loads content from "
                      f"remote host {host!r} with insecure content allowed; "
                      f"only point this at hosts you trust", file=sys.stderr)

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

        parts = urllib.parse.urlsplit(cactbot_url)
        query = [(key, value) for key, value in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
                 if key != "OVERLAY_WS"]
        query.append(("OVERLAY_WS", ws_url))
        load_url = urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query)))
        self._active = True
        self._ws_url = ws_url
        self.status.emit(True, "Loading cactbot...")
        self._page.load(QUrl(load_url))

    def stop(self) -> None:
        if not self._active and self._page is None:
            return
        self._teardown()
        self.status.emit(False, "Off")

    def _teardown(self) -> None:
        """Release browser objects so a later start can retry."""
        self._active = False
        self._ws_url = ""
        self._seen.clear()
        if self._page is not None:
            # Disconnect old page signals before starting another session.
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
        # Delete the page before its profile. The reader outlives both.
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
        new = {str(t) for t in (ids or [])}
        self._disabled = new
        if not self._active or self._page is None:
            return
        from PyQt6.QtWebEngineCore import QWebEngineScript
        payload = json.dumps({t: True for t in new})
        js = (
            "(function(m){"
            "  window.__nyaaDisabledTriggers=m;"
            "  return typeof window.__nyaaSetDisabledTriggers==='function'"
            "    &&window.__nyaaSetDisabledTriggers(m);"
            "})(" + payload + ");"
        )
        try:
            self._page.runJavaScript(js, QWebEngineScript.ScriptWorldId.MainWorld)
        except Exception:  # noqa: BLE001
            pass

    def set_replacements(self, rules: list) -> None:
        """Replace callout rules atomically. An empty result silences the callout."""
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
            self.set_disabled_triggers(self._disabled)
        else:
            # Clear active state so the program can unmute callouts and retry.
            self._teardown()
            self.status.emit(False, "Failed to load cactbot (check the URL / connection)")

    def _on_render_process_terminated(self, status, exit_code) -> None:
        if not self._active:
            return
        # A crashed renderer cannot call out even if the page appears loaded.
        self._teardown()
        self.status.emit(False, "Cactbot renderer crashed, local callouts are back")

    def _on_message(self, kind: str, payload: str) -> None:
        try:
            data = json.loads(payload)
        except (ValueError, RecursionError):
            return
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
