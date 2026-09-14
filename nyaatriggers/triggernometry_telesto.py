"""Telesto drawings and callbacks owned by one Triggernometry engine run."""

from __future__ import annotations

import copy
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import urllib.error
import urllib.request
import uuid

from nyaatriggers.telesto_client import DEFAULT_URI
from nyaatriggers.trigger_engine import compile_user_regex

MAX_BODY = 1 << 20
MAX_RESOURCES = 512


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False

    def __init__(self, owner):
        self.owner = owner
        self.slots = threading.BoundedSemaphore(16)
        super().__init__(("127.0.0.1", 0), _Handler)

    def process_request(self, request, address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self.slots.release()


class _Handler(BaseHTTPRequestHandler):
    def setup(self):
        self.request.settimeout(5)
        super().setup()

    def log_message(self, *_args):
        pass

    def do_POST(self):
        owner = self.server.owner
        try:
            if self.headers.get("Origin") or self.headers.get("Transfer-Encoding"):
                self.send_error(403)
                return
            if self.headers.get_content_type() != "application/json":
                self.send_error(415)
                return
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= MAX_BODY:
                self.send_error(413)
                return
            raw = self.rfile.read(length)
            if len(raw) != length:
                self.send_error(400)
                return
            message = json.loads(raw)
            if self.path.startswith(owner.callback_path):
                code, body = owner.receive(message, self.path), b""
            elif self.path == "/":
                code, body = owner.forward(message)
            else:
                code, body = 404, b""
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (ValueError, TypeError, KeyError, RecursionError) as exc:
            owner.report(f"Invalid Telesto request: {exc}")
            self.send_error(400)
        except (OSError, http.client.HTTPException):
            pass


class TriggernometryTelesto:
    def __init__(self, callback, report, uri=DEFAULT_URI, commands_enabled=False):
        self.callback = callback
        self.report = report
        self.uri = uri if isinstance(uri, str) and uri else DEFAULT_URI
        self.commands_enabled = bool(commands_enabled)
        self.prefix = "nyaa-tn-" + uuid.uuid4().hex + "-"
        self.callback_path = "/callback/" + uuid.uuid4().hex + "/"
        self._lock = threading.RLock()
        self._sending = threading.Lock()
        self._closed = threading.Event()
        self._subscriptions = {}
        self._drawings = {}
        self._drawing_notifications = set()
        self._drawing_expiry = {}
        self._owned_subscriptions = set()
        self._owned_drawings = set()
        self._serial = 0
        self._server = None
        self._cleanup = None
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def start(self):
        self._server = _Server(self)
        self.url = f"http://127.0.0.1:{self._server.server_port}/"
        self.callback_url = self.url.rstrip("/") + self.callback_path
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        kwargs={"poll_interval": 0.1}, daemon=True,
                                        name="tn-telesto")
        self._thread.start()

    def configure(self, commands_enabled):
        with self._lock:
            self.commands_enabled = bool(commands_enabled)

    def _name(self, payload, key):
        name = payload.get(key)
        if not isinstance(name, str) or not name or len(name) > 1024:
            raise ValueError(f"Invalid {key}")
        return name

    def _prepare(self, message, depth=0):
        if depth > 16 or not isinstance(message, dict):
            raise ValueError("Invalid Telesto envelope")
        kind = str(message.get("type", "")).lower()
        payload = message.get("payload")
        if kind == "bundle":
            if not isinstance(payload, list) or len(payload) > 512:
                raise ValueError("Invalid Telesto bundle")
            message["payload"] = [self._prepare(item, depth + 1) for item in payload]
            return message
        if kind in ("executecommand", "macro") and not self.commands_enabled:
            self.report("Triggernometry command skipped because automarkers are off")
            return {"version": 1, "id": message.get("id", 0), "type": "Bundle", "payload": []}
        if kind in ("subscribe", "unsubscribe", "enabledoodle", "disabledoodle", "disabledoodleregex"):
            if not isinstance(payload, dict):
                raise ValueError("Invalid Telesto payload")
        if kind == "subscribe":
            name = self._name(payload, "id").lower()
            if len(self._owned_subscriptions) >= MAX_RESOURCES:
                raise ValueError("Too many memory subscriptions")
            previous = self._subscriptions.get(name)
            self._serial += 1
            qualified = self.prefix + str(self._serial)
            self._subscriptions[name] = qualified
            self._owned_subscriptions.add(qualified)
            payload["id"] = qualified
            payload["endpoint"] = self.callback_url
            if previous:
                return self._bundle([self._disable("Unsubscribe", "id", previous), message])
        elif kind == "unsubscribe":
            name = self._name(payload, "id").lower()
            payload["id"] = self._subscriptions.pop(name, self.prefix + "absent")
        elif kind == "enabledoodle":
            name = self._name(payload, "name")
            if len(self._owned_drawings) >= MAX_RESOURCES:
                raise ValueError("Too many drawings")
            self._serial += 1
            qualified = self.prefix + name
            self._drawings[name] = qualified
            self._owned_drawings.add(qualified)
            payload["name"] = qualified
            self._drawing_notifications.discard(qualified)
            if payload.get("notifyonexpiry"):
                self._drawing_notifications.add(qualified)
            path = self.callback_path + f"drawing/{self._serial}/"
            self._drawing_expiry[qualified] = path
            payload["notifyonexpiry"] = self.url.rstrip("/") + path
            self._drawing_references(payload)
        elif kind == "disabledoodle":
            name = self._name(payload, "name")
            payload["name"] = self._drawings.pop(name, self.prefix + name)
        elif kind == "disabledoodleregex":
            pattern = compile_user_regex(self._name(payload, "regex"))
            if pattern is None:
                raise ValueError("Invalid drawing expression")
            names = [name for name in self._drawings if pattern.search(name, timeout=0.02)]
            return self._bundle([self._disable("DisableDoodle", "name", self._drawings.pop(name))
                                 for name in names])
        return message

    def _drawing_references(self, value):
        if isinstance(value, dict):
            if value.get("coords") == "doodle" and isinstance(value.get("name"), str):
                value["name"] = self.prefix + value["name"]
            for child in value.values():
                self._drawing_references(child)
        elif isinstance(value, list):
            for child in value:
                self._drawing_references(child)

    @staticmethod
    def _disable(kind, key, value):
        return {"version": 1, "id": 0, "type": kind, "payload": {key: value}}

    @staticmethod
    def _bundle(items):
        return {"version": 1, "id": 0, "type": "Bundle", "payload": items}

    def _post(self, message):
        request = urllib.request.Request(
            self.uri, json.dumps(message, ensure_ascii=False).encode("utf-8"),
            {"Content-Type": "application/json"}, method="POST")
        try:
            with self._opener.open(request, timeout=4) as response:
                body = response.read(MAX_BODY + 1)
                if len(body) > MAX_BODY:
                    raise ValueError("Telesto response exceeds the size limit")
                return response.status, body
        except urllib.error.HTTPError as exc:
            code = exc.code
            exc.close()
            self.report(f"Telesto returned HTTP {code}")
            return code, b""
        except (OSError, ValueError, http.client.HTTPException) as exc:
            self.report(f"Telesto request failed: {exc}")
            return 502, b""

    def forward(self, message):
        with self._sending:
            with self._lock:
                if self._closed.is_set():
                    return 410, b""
                fields = ("_subscriptions", "_drawings", "_drawing_expiry", "_drawing_notifications",
                          "_owned_subscriptions", "_owned_drawings")
                previous = {field: getattr(self, field).copy() for field in fields}
                try:
                    message = self._prepare(copy.deepcopy(message))
                except Exception:
                    for field, value in previous.items():
                        setattr(self, field, value)
                    raise
            code, body = self._post(message)
            if 200 <= code < 300:
                with self._lock:
                    self._release_removed(message)
            return code, body

    def _release_removed(self, message):
        kind = str(message.get("type", "")).lower()
        payload = message.get("payload", {})
        if kind == "bundle":
            for item in payload:
                self._release_removed(item)
        elif kind == "unsubscribe":
            self._owned_subscriptions.discard(payload["id"])
        elif kind == "disabledoodle":
            self._owned_drawings.discard(payload["name"])
            self._drawing_notifications.discard(payload["name"])
            self._drawing_expiry.pop(payload["name"], None)

    def receive(self, message, path):
        if not isinstance(message, dict):
            return 400
        with self._lock:
            if self._closed.is_set():
                return 410
            notification = message.get("notificationid")
            resources = (self._drawings if message.get("notificationtype") == "doodleexpired"
                         else self._subscriptions)
            name = next((key for key, value in resources.items() if value == notification), None)
            if name is None:
                return 410
            expected_path = (self._drawing_expiry.get(notification) if resources is self._drawings
                             else self.callback_path)
            if path != expected_path:
                return 410
            message["notificationid"] = name
            if resources is self._drawings:
                resources.pop(name, None)
                self._drawing_expiry.pop(notification, None)
                self._owned_drawings.discard(notification)
                notify = notification in self._drawing_notifications
                self._drawing_notifications.discard(notification)
                if not notify:
                    return 200
            self.callback(json.dumps(message, ensure_ascii=False, separators=(",", ":")))
        return 200

    def close(self, wait=False):
        with self._lock:
            if not self._closed.is_set():
                self._closed.set()
                self._cleanup = threading.Thread(target=self._finish, daemon=True, name="tn-telesto-cleanup")
                self._cleanup.start()
        if wait and self._cleanup:
            self._cleanup.join(timeout=10)

    def is_finished(self):
        return self._closed.is_set() and self._cleanup is not None and not self._cleanup.is_alive()

    def _finish(self):
        if self._server:
            self._server.shutdown()
            self._server.server_close()
        with self._sending:
            with self._lock:
                items = [self._disable("Unsubscribe", "id", value)
                         for value in self._owned_subscriptions]
                items += [self._disable("DisableDoodle", "name", value)
                          for value in self._owned_drawings]
                self._subscriptions.clear()
                self._drawings.clear()
                self._owned_subscriptions.clear()
                self._owned_drawings.clear()
                self._drawing_notifications.clear()
                self._drawing_expiry.clear()
            if items:
                self._post(self._bundle(items))
