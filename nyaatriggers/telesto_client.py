"""Send marker commands serially through Telesto, resolving actor IDs to GetPartyMembers slots."""

from __future__ import annotations

import http.client
import ipaddress
import json
import queue
import random
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from nyaatriggers.drop_log import log_drop
from nyaatriggers.diagnostics import record
from nyaatriggers.locale_util import N_

try:
    from PyQt6.QtCore import QObject, pyqtSignal
    _HAVE_QT = True
except Exception:  # pragma: no cover
    _HAVE_QT = False

    class QObject:  # Allow tests to import without Qt.
        def __init__(self, *a, **k):
            pass

    def pyqtSignal(*a, **k):  # noqa: N802
        class _Dummy:
            def __init__(self):
                self._slots = []

            def connect(self, fn):
                self._slots.append(fn)

            def emit(self, *args):
                for fn in list(self._slots):
                    try:
                        fn(*args)
                    except Exception:  # Continue emitting after a failing slot.
                        import traceback
                        traceback.print_exc()

        return _Dummy()


VERSION = 1
GAME_CMD_ID = 1_000_000
PARTY_UPDATE_ID = 1_000_001

DEFAULT_URI = "http://localhost:45678/"


def _is_loopback_uri(uri: str) -> bool:
    host = (urllib.parse.urlsplit(uri).hostname or "").rstrip(".").lower()
    if host == "localhost":
        return True
    try:
        address = ipaddress.ip_address(host)
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
            address = address.ipv4_mapped
        return address.is_loopback
    except ValueError:
        return False


def read_telesto_response(request, timeout: float, stopping: threading.Event,
                          *, use_proxy=True, max_body=1 << 20, is_current=None) -> tuple[int, bytes]:
    """Bound Telesto requests through headers and body and interrupt them on shutdown."""
    done = threading.Event()
    cancelled = threading.Event()
    sockets = []
    deadline = time.monotonic() + timeout

    def request_cancelled():
        return stopping.is_set() or (is_current is not None and not is_current())

    def check_cancelled():
        if request_cancelled() or cancelled.is_set() or time.monotonic() >= deadline:
            raise TimeoutError("Telesto request cancelled" if request_cancelled()
                               else "Telesto request deadline exceeded")

    def connection_type(base):
        class Connection(base):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                create_connection = self._create_connection

                def connect_socket(*args, **kwargs):
                    check_cancelled()
                    sock = create_connection(*args, **kwargs)
                    try:
                        # Keep a handle while TLS takes ownership of the socket.
                        sockets.append(sock.dup())
                        check_cancelled()
                    except BaseException:
                        sock.close()
                        raise
                    return sock

                self._create_connection = connect_socket

            def connect(self):
                check_cancelled()
                super().connect()
                try:
                    check_cancelled()
                except TimeoutError:
                    self.close()
                    raise

            def send(self, data):
                check_cancelled()
                super().send(data)

        return Connection

    http_connection = connection_type(http.client.HTTPConnection)
    https_connection = connection_type(http.client.HTTPSConnection)

    class HttpHandler(urllib.request.HTTPHandler):
        def http_open(self, req):
            return self.do_open(http_connection, req)

    class HttpsHandler(urllib.request.HTTPSHandler):
        def https_open(self, req):
            return self.do_open(https_connection, req, context=self._context)

    handlers = [HttpHandler(), HttpsHandler()]
    if not use_proxy or _is_loopback_uri(request.full_url):
        handlers.append(urllib.request.ProxyHandler({}))
    opener = urllib.request.build_opener(*handlers)

    def watch():
        while not done.wait(0.02):
            if not request_cancelled() and time.monotonic() < deadline:
                continue
            cancelled.set()
            for sock in list(sockets):
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    watcher = threading.Thread(target=watch, daemon=True, name="TelestoDeadline")
    watcher.start()
    try:
        check_cancelled()
        with opener.open(request, timeout=timeout) as response:
            check_cancelled()
            body = response.read(max_body + 1)
            check_cancelled()
            if len(body) > max_body:
                raise ValueError("Telesto response too large")
            return response.getcode(), body
    finally:
        done.set()
        watcher.join()
        for sock in sockets:
            sock.close()


_STOP = object()

# Ignore marker tokens vary by client language. Other markers use English tokens.
MARKERS: list[tuple[str, str]] = [
    (N_("Attack 1"), "attack1"), (N_("Attack 2"), "attack2"), (N_("Attack 3"), "attack3"),
    (N_("Attack 4"), "attack4"), (N_("Attack 5"), "attack5"), (N_("Attack 6"), "attack6"),
    (N_("Attack 7"), "attack7"), (N_("Attack 8"), "attack8"),
    (N_("Bind 1"), "bind1"), (N_("Bind 2"), "bind2"), (N_("Bind 3"), "bind3"),
    (N_("Ignore 1"), "ignore1"), (N_("Ignore 2"), "ignore2"),
    (N_("Circle"), "circle"), (N_("Cross"), "cross"),
    (N_("Triangle"), "triangle"), (N_("Square"), "square"),
]
MARKER_TOKENS = frozenset(tok for _label, tok in MARKERS)


def make_message(msg_id: int, msg_type: str, payload=None) -> dict:
    return {
        "version": VERSION,
        "id": int(msg_id),
        "type": str(msg_type),
        "payload": {} if payload is None else payload,
    }


def game_command_message(command: str) -> dict:
    return make_message(GAME_CMD_ID, "ExecuteCommand", {"command": str(command)})


def party_members_message() -> dict:
    """Request party members and probe reachability without changing game state."""
    return make_message(PARTY_UPDATE_ID, "GetPartyMembers", None)


def _record_marker_transport(msg, result):
    payload = msg.get("payload")
    if msg.get("type") != "ExecuteCommand" or not isinstance(payload, dict):
        return
    parts = str(payload.get("command", "")).split()
    if (len(parts) != 3 or parts[0] != "/mk"
            or parts[1] not in MARKER_TOKENS | {"clear", "attack"}
            or parts[2] not in {"<me>", *(f"<{slot}>" for slot in range(1, 9))}):
        return
    record("marker_transport", kind="clear" if parts[1] == "clear" else "mark",
           marker=parts[1], slot=0 if parts[2] == "<me>" else int(parts[2][1:-1]),
           result=result)


def _slot_token(slot) -> str:
    s = str(slot).strip()
    if s.startswith("<") and s.endswith(">"):
        return s
    return f"<{s}>"


def _actor_int(actor_id) -> "int | None":
    """Normalize actor IDs, rejecting invalid IDs and no-target sentinels."""
    if actor_id is None:
        return None
    if isinstance(actor_id, bool):          # bool is an int subclass
        return None
    if isinstance(actor_id, int):
        v = actor_id
    else:
        s = str(actor_id).strip()
        if not s:
            return None
        if s.lower().startswith("0x"):
            s = s[2:]
        try:
            v = int(s, 16)
        except ValueError:
            try:
                v = int(s)
            except ValueError:
                return None
    if v <= 0 or v == 0xE0000000:
        return None
    return v


def mark_command(marker, target) -> str:
    """Empty or unknown tokens use the next attack marker. Log unknown tokens."""
    m = (str(marker).strip() if marker is not None else "")
    if m not in MARKER_TOKENS:
        if m:
            log_drop("telesto-mark", f"unknown marker {m!r}, using next-attack")
        m = "attack"
    return f"/mk {m} {_slot_token(target)}"


class TelestoClient(QObject):
    """Thread-safe queued HTTP client for Telesto."""

    # reachable, message, degraded
    status_changed = pyqtSignal(bool, str, bool)
    error = pyqtSignal(str)

    def __init__(self, uri: str = DEFAULT_URI, enabled: bool = False,
                 delay_base_ms: int = 100, delay_plus_ms: int = 100,
                 timeout: float = 4.0, max_queue: int = 1000, parent=None) -> None:
        super().__init__(parent)
        self._uri = uri if isinstance(uri, str) and uri else DEFAULT_URI
        self._enabled = bool(enabled)
        self._command_epoch = 0
        self._endpoint_epoch = 0
        self._encounter_epoch = 0
        self._cleanup_epoch = 0
        self._delay_base = max(0, int(delay_base_ms))
        self._delay_plus = max(0, int(delay_plus_ms))
        self._timeout = float(timeout)
        self._max_queue = max(1, int(max_queue))
        self._queue: "queue.Queue" = queue.Queue(maxsize=self._max_queue)
        self._thread: "threading.Thread | None" = None
        self._stopping = threading.Event()
        self._lock = threading.RLock()
        self._reachable: "tuple[bool, bool] | None" = None
        self._warned_sends: set = set()        # unexpected send failures already logged
        self._slot_by_actor: "dict[int, int]" = {}
        self._request_context = threading.local()

    def configure(self, uri: "str | None" = None, enabled: "bool | None" = None,
                  delay_base_ms: "int | None" = None,
                  delay_plus_ms: "int | None" = None) -> None:
        with self._lock:
            invalidated = False
            if uri is not None:
                destination = uri if isinstance(uri, str) and uri else DEFAULT_URI
                if destination != self._uri:
                    self._uri = destination
                    self._endpoint_epoch += 1
                    self._slot_by_actor = {}
                    self._reachable = None
                    invalidated = True
            if enabled is not None:
                if self._enabled and not enabled:
                    self._command_epoch += 1
                    invalidated = True
                self._enabled = bool(enabled)
            if invalidated:
                self._discard_cancelled_commands()
            if delay_base_ms is not None:
                self._delay_base = max(0, int(delay_base_ms))
            if delay_plus_ms is not None:
                self._delay_plus = max(0, int(delay_plus_ms))

    def set_enabled(self, enabled: bool) -> None:
        self.configure(enabled=enabled)

    def cancel_pending(self, *, clear_party: bool = False) -> None:
        """Cancel work from the previous encounter before queueing its cleanup."""
        with self._lock:
            self._encounter_epoch += 1
            if clear_party:
                self._cleanup_epoch += 1
                self._slot_by_actor.clear()
            self._discard_cancelled_commands()

    def is_enabled(self) -> bool:
        with self._lock:
            return self._enabled

    def last_status(self) -> "tuple[bool, bool] | None":
        with self._lock:
            return self._reachable

    @property
    def uri(self) -> str:
        with self._lock:
            return self._uri

    def start(self) -> None:
        with self._lock:
            t = self._thread
            if t and t.is_alive() and not self._stopping.is_set():
                return
            # Replacement workers need separate queues without old commands.
            if self._stopping.is_set():
                self._queue = queue.Queue(maxsize=self._max_queue)
            self._stopping = threading.Event()
            self._thread = threading.Thread(
                target=self._run, args=(self._queue, self._stopping),
                name="TelestoClient", daemon=True)
            self._thread.start()

    def request_stop(self) -> None:
        """Request shutdown without joining so callers can overlap client waits."""
        with self._lock:
            self._stopping.set()
            q = self._queue
        try:
            q.put_nowait(_STOP)
        except queue.Full:
            try:
                q.get_nowait()
                q.put_nowait(_STOP)
            except (queue.Empty, queue.Full):
                pass

    def join_stopped(self, timeout: float = 2.0) -> None:
        with self._lock:
            t = self._thread
        if t and t.is_alive():
            t.join(timeout=timeout)
            if t.is_alive():
                # Retain blocked worker handles for later joins.
                return
        with self._lock:
            if self._thread is t:
                self._thread = None

    def stop(self, join_timeout: float = 2.0) -> None:
        self.request_stop()
        self.join_stopped(join_timeout)

    def send_game_command(self, command: str, force: bool = False) -> bool:
        """Queue a delayed command. force bypasses the enabled setting for testing."""
        if not command:
            return False
        return self._enqueue(game_command_message(command), delay=True, force=force)

    def mark(self, marker, target, force: bool = False) -> bool:
        return self.send_game_command(mark_command(marker, target), force=force)

    def mark_self(self, marker, force: bool = False) -> bool:
        return self.mark(marker, "me", force=force)

    def mark_slot(self, marker, slot, force: bool = False) -> bool:
        return self.mark(marker, int(slot), force=force)

    def mark_actor(self, actor_id, marker, force: bool = False) -> bool:
        """Mark the current party slot. Return false for unknown slots or queue failure."""
        with self._lock:
            slot = self.slot_of_actor(actor_id)
            if not slot:
                return False
            return self._enqueue(game_command_message(mark_command(marker, slot)),
                                 delay=True, force=force, actor=_actor_int(actor_id))

    def slot_of_actor(self, actor_id) -> "int | None":
        aid = _actor_int(actor_id)
        if aid is None:
            return None
        with self._lock:
            return self._slot_by_actor.get(aid)

    def party_slot_count(self) -> int:
        with self._lock:
            return len(self._slot_by_actor)

    def clear_self(self, force: bool = False) -> bool:
        return self._enqueue(game_command_message("/mk clear <me>"),
                             delay=True, force=force, cleanup=True)

    def clear_actor(self, actor_id, force: bool = False) -> bool:
        with self._lock:
            slot = self.slot_of_actor(actor_id)
            if not slot:
                return False
            return self._enqueue(game_command_message(f"/mk clear <{int(slot)}>"),
                                 delay=True, force=force, cleanup=True, actor=_actor_int(actor_id))

    def clear_all(self, force: bool = False) -> None:
        with self._lock:
            for n in range(1, 9):
                self._enqueue(game_command_message(f"/mk clear <{n}>"),
                              delay=True, force=force, cleanup=True)

    def request_party_members(self, force: bool = False) -> None:
        self._enqueue(party_members_message(), delay=False, force=force)

    def ping(self) -> None:
        self.request_party_members(force=True)

    def _enqueue(self, msg: dict, delay: bool, force: bool = False, cleanup: bool = False,
                 actor: int | None = None) -> bool:
        """Return false if disabled or full."""
        try:
            with self._lock:
                if not force and not self._enabled:
                    return False
                self._queue.put_nowait((msg, delay, force, self._command_epoch,
                                       self._endpoint_epoch, self._encounter_epoch,
                                       self._cleanup_epoch if cleanup else None, actor))
        except queue.Full:
            log_drop("telesto-queue", "command queue full, dropping message")
            self.error.emit("Telesto command queue full; command dropped")
            return False
        return True

    def _can_send(self, force: bool, epoch: int, endpoint: int, encounter: int,
                  cleanup: int | None) -> bool:
        with self._lock:
            return (endpoint == self._endpoint_epoch
                    and (encounter == self._encounter_epoch or cleanup == self._cleanup_epoch)
                    and (force or self._enabled and epoch == self._command_epoch))

    def _discard_cancelled_commands(self) -> None:
        q = self._queue
        with q.mutex:
            keep = [item for item in q.queue
                    if item is _STOP or self._can_send(*item[2:7])]
            removed = len(q.queue) - len(keep)
            if not removed:
                return
            q.queue.clear()
            q.queue.extend(keep)
            q.unfinished_tasks -= removed
            if not q.unfinished_tasks:
                q.all_tasks_done.notify_all()
            q.not_full.notify_all()

    def _run(self, q: "queue.Queue", stopping: "threading.Event") -> None:
        self._request_context.stopping = stopping
        while not stopping.is_set():
            try:
                item = q.get(timeout=1.0)
            except queue.Empty:
                continue
            except Exception:  # pragma: no cover
                stopping.wait(0.05)
                continue
            if item is _STOP:
                if stopping.is_set():
                    break
                continue                       # stale sentinel from a previous generation
            msg, delay, force, epoch, endpoint, encounter, cleanup, actor = item
            if not self._can_send(force, epoch, endpoint, encounter, cleanup):
                _record_marker_transport(msg, "cancelled")
                continue
            if delay:
                self._sleep_command_delay(stopping)
            if stopping.is_set():
                break
            if not self._can_send(force, epoch, endpoint, encounter, cleanup):
                _record_marker_transport(msg, "cancelled")
                continue
            if actor is not None:
                slot = self.slot_of_actor(actor)
                if slot is None:
                    _record_marker_transport(msg, "unknown_slot")
                    continue
                command = msg["payload"]["command"].rsplit(" ", 1)[0]
                msg = game_command_message(f"{command} <{slot}>")
            try:
                self._request_context.command = (force, epoch, endpoint, encounter, cleanup)
                self._request_context.endpoint = endpoint
                self._post(msg)
            except Exception as exc:
                key = f"{type(exc).__name__}: {exc}"[:200]
                if key not in self._warned_sends and len(self._warned_sends) < 32:
                    self._warned_sends.add(key)
                    log_drop("telesto-send", f"send failed: {exc}", 0)

    def _sleep_command_delay(self, stopping: "threading.Event") -> None:
        with self._lock:
            base, plus = self._delay_base, self._delay_plus
        delay_s = (base + (random.random() * plus)) / 1000.0
        if delay_s > 0:
            stopping.wait(delay_s)

    def _post(self, msg: dict) -> None:
        with self._lock:
            endpoint = getattr(self._request_context, "endpoint", self._endpoint_epoch)
            if not self._response_current(endpoint):
                return
            uri, timeout = self._uri, self._timeout
        body = json.dumps(msg).encode("utf-8")
        try:
            req = urllib.request.Request(
                uri, data=body, method="POST",
                headers={"Content-Type": "application/json",
                         "User-Agent": "NyaaTriggers"})
            code, body = self._read_response(req, timeout)
            with self._lock:
                if not self._response_current(endpoint):
                    return
                self._report_reachable(True, f"Connected (HTTP {code})")
                _record_marker_transport(msg, "accepted")
                if msg.get("id") == PARTY_UPDATE_ID:
                    self._update_party_slots(body)
        except urllib.error.HTTPError as exc:
            # HTTP errors mean the endpoint is reachable but degraded.
            exc.close()
            with self._lock:
                if not self._response_current(endpoint):
                    return
                self._report_reachable(True, f"Telesto error: HTTP {exc.code}", degraded=True)
                _record_marker_transport(msg, "failed")
            log_drop("telesto-http", f"HTTP {exc.code} for {msg.get('type')}")
        except (urllib.error.URLError, OSError, ValueError,
                http.client.HTTPException) as exc:
            with self._lock:
                if not self._response_current(endpoint):
                    return
                self._report_reachable(False, f"Telesto unreachable: {exc}")
                _record_marker_transport(msg, "failed")
            log_drop("telesto-http", f"unreachable: {exc}")

    def _response_current(self, endpoint: int) -> bool:
        command = getattr(self._request_context, "command", None)
        return (endpoint == self._endpoint_epoch
                and (command is None or self._can_send(*command))
                and not getattr(self._request_context, "stopping", self._stopping).is_set())

    def _read_response(self, request, timeout: float) -> tuple[int, bytes]:
        stopping = getattr(self._request_context, "stopping", self._stopping)
        command = getattr(self._request_context, "command", None)
        is_current = (lambda: self._can_send(*command)) if command is not None else None
        return read_telesto_response(request, timeout, stopping, is_current=is_current)

    def _update_party_slots(self, body: bytes) -> None:
        """Use valid party order as the slot. Keep the prior map on an invalid roster."""
        try:
            data = json.loads(body.decode("utf-8", "replace"))
        except (ValueError, AttributeError):
            return
        members = data.get("response") if isinstance(data, dict) else data
        if not isinstance(members, list):
            return
        if not members:
            with self._lock:
                self._slot_by_actor = {}
            return

        slots: "dict[int, int]" = {}
        seen_orders: set[int] = set()
        for entry in members:
            if not isinstance(entry, dict):
                return
            aid = _actor_int(entry.get("actor"))
            try:
                order = int(str(entry.get("order")).strip(), 16)
            except (ValueError, AttributeError, TypeError):
                return
            if aid is None or not 1 <= order <= 8 or order in seen_orders or aid in slots:
                return
            seen_orders.add(order)
            slots[aid] = order
        with self._lock:
            self._slot_by_actor = slots

    def _report_reachable(self, reachable: bool, message: str, degraded: bool = False) -> None:
        state = (reachable, degraded)
        with self._lock:
            changed = self._reachable != state
            self._reachable = state
        if changed:
            self.status_changed.emit(reachable, message, degraded)
