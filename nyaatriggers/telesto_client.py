"""Queue marker commands through the Telesto HTTP endpoint. Resolve actor IDs to party
slots using sorted GetPartyMembers responses and skip unknown targets. A worker sends
commands serially with configured delays and reports connection failures.
"""

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
from nyaatriggers.locale_util import N_   # Translate marker labels when rendering the selector.

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


# Protocol constants, TelestoMain.java and DoodleProcessor.java.
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


_STOP = object()

# Marker tokens follow the English client. Ignore markers use localized tokens on other
# clients.
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
    """Build the Telesto envelope with an empty dictionary as the default payload."""
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


def _slot_token(slot) -> str:
    """Normalize slot numbers and named placeholders to angle brackets."""
    s = str(slot).strip()
    if s.startswith("<") and s.endswith(">"):
        return s
    return f"<{s}>"


def _actor_int(actor_id) -> "int | None":
    """Parse numeric or hexadecimal actor IDs with decimal fallback. Reject invalid IDs and
    no-target sentinels.
    """
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
    """Use the next attack marker for empty or unknown tokens. Log unknown tokens so
    invalid rules are visible.
    """
    m = (str(marker).strip() if marker is not None else "")
    if m not in MARKER_TOKENS:
        if m:
            log_drop("telesto-mark", f"unknown marker {m!r}, using next-attack")
        m = "attack"
    return f"/mk {m} {_slot_token(target)}"


class TelestoClient(QObject):
    """Queued HTTP client for the Telesto plugin. Thread-safe public API."""

    # Reachable, message and degraded flags. HTTP errors indicate a reachable but
    # failing endpoint.
    status_changed = pyqtSignal(bool, str, bool)
    error = pyqtSignal(str)

    def __init__(self, uri: str = DEFAULT_URI, enabled: bool = False,
                 delay_base_ms: int = 100, delay_plus_ms: int = 100,
                 timeout: float = 4.0, max_queue: int = 1000, parent=None) -> None:
        super().__init__(parent)
        # Validate endpoint types because settings may contain arbitrary JSON values.
        self._uri = uri if isinstance(uri, str) and uri else DEFAULT_URI
        self._enabled = bool(enabled)
        self._command_epoch = 0
        self._endpoint_epoch = 0
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
        # Actor IDs to party slots. Keep empty until a valid roster arrives and skip
        # unknown actors.
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

    def is_enabled(self) -> bool:
        with self._lock:
            return self._enabled

    def last_status(self) -> "tuple[bool, bool] | None":
        """Current reachability and degraded flags, or None before the endpoint is checked."""
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
            # A replacement must not replay old commands or share its queue with a
            # worker that is still finishing a request.
            if self._stopping.is_set():
                self._queue = queue.Queue(maxsize=self._max_queue)
            self._stopping = threading.Event()
            self._thread = threading.Thread(
                target=self._run, args=(self._queue, self._stopping),
                name="TelestoClient", daemon=True)
            self._thread.start()

    def request_stop(self) -> None:
        """Request shutdown without joining so callers can overlap client shutdown waits.
        """
        with self._lock:
            self._stopping.set()
            q = self._queue
        try:
            q.put_nowait(_STOP)
        except queue.Full:
            # Free a slot for the stop sentinel. The worker also checks its stop event.
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
                # Retain the handle if HTTP is still blocked so later stops can join the
                # worker.
                return
        with self._lock:
            if self._thread is t:
                self._thread = None

    def stop(self, join_timeout: float = 2.0) -> None:
        self.request_stop()
        self.join_stopped(join_timeout)

    # Return whether the command was queued so callers consume cooldown only on success.
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
        """Resolve the actor's current party slot and queue a mark. Return false when the
        slot is unknown or queueing fails.
        """
        slot = self.slot_of_actor(actor_id)
        if not slot:
            return False
        return self.mark_slot(marker, slot, force=force)

    def slot_of_actor(self, actor_id) -> "int | None":
        """Return the actor's current party slot, or None."""
        aid = _actor_int(actor_id)
        if aid is None:
            return None
        with self._lock:
            return self._slot_by_actor.get(aid)

    def party_slot_count(self) -> int:
        with self._lock:
            return len(self._slot_by_actor)

    def clear_self(self, force: bool = False) -> bool:
        return self.send_game_command("/mk clear <me>", force=force)

    def clear_actor(self, actor_id, force: bool = False) -> bool:
        """Clear the actor's marker only when its party slot is known."""
        slot = self.slot_of_actor(actor_id)
        if not slot:
            return False
        return self.send_game_command(f"/mk clear <{int(slot)}>", force=force)

    def clear_all(self, force: bool = False) -> None:
        for n in range(1, 9):
            self.send_game_command(f"/mk clear <{n}>", force=force)

    def request_party_members(self, force: bool = False) -> None:
        """Refresh the party mapping and connection status."""
        self._enqueue(party_members_message(), delay=False, force=force)

    def ping(self) -> None:
        """Probe reachability even while marking is disabled."""
        self.request_party_members(force=True)

    def _enqueue(self, msg: dict, delay: bool, force: bool = False) -> bool:
        """Return false if disabled or the queue is full."""
        try:
            with self._lock:
                if not force and not self._enabled:
                    return False
                self._queue.put_nowait((msg, delay, force, self._command_epoch,
                                       self._endpoint_epoch))
        except queue.Full:
            log_drop("telesto-queue", "command queue full, dropping message")
            self.error.emit("Telesto command queue full; command dropped")
            return False
        return True

    def _can_send(self, force: bool, epoch: int, endpoint: int) -> bool:
        with self._lock:
            return (endpoint == self._endpoint_epoch
                    and (force or self._enabled and epoch == self._command_epoch))

    def _discard_cancelled_commands(self) -> None:
        # The caller holds the settings lock. Hold the queue mutex too so the worker
        # cannot overtake forced commands while they are being retained.
        q = self._queue
        with q.mutex:
            keep = [item for item in q.queue
                    if item is _STOP or self._can_send(item[2], item[3], item[4])]
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
                # Poll the stop event even if the queue cannot accept its sentinel.
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
            msg, delay, force, epoch, endpoint = item
            if not self._can_send(force, epoch, endpoint):
                continue
            if delay:
                self._sleep_command_delay(stopping)
            # Recheck shutdown after dequeue before issuing a request.
            if stopping.is_set():
                break
            if not self._can_send(force, epoch, endpoint):
                continue
            try:
                self._request_context.endpoint = endpoint
                self._post(msg)
            except Exception as exc:  # Continue processing after a failed command.
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
            if endpoint != self._endpoint_epoch:
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
                if msg.get("id") == PARTY_UPDATE_ID:
                    self._update_party_slots(body)
        except urllib.error.HTTPError as exc:
            # HTTP errors mean the endpoint is reachable but degraded. Close the
            # response before continuing.
            exc.close()
            with self._lock:
                if not self._response_current(endpoint):
                    return
                self._report_reachable(True, f"Telesto error: HTTP {exc.code}", degraded=True)
            log_drop("telesto-http", f"HTTP {exc.code} for {msg.get('type')}")
        except (urllib.error.URLError, OSError, ValueError,
                http.client.HTTPException) as exc:
            # Report transport failures without stopping the client.
            with self._lock:
                if not self._response_current(endpoint):
                    return
                self._report_reachable(False, f"Telesto unreachable: {exc}")
            log_drop("telesto-http", f"unreachable: {exc}")

    def _response_current(self, endpoint: int) -> bool:
        return (endpoint == self._endpoint_epoch
                and not getattr(self._request_context, "stopping", self._stopping).is_set())

    def _read_response(self, request, timeout: float) -> tuple[int, bytes]:
        """Abort a stalled request even when its peer keeps sending bytes."""
        stopping = getattr(self._request_context, "stopping", self._stopping)
        done = threading.Event()
        cancelled = threading.Event()
        connections = []
        responses = []
        deadline = time.monotonic() + timeout

        def check_cancelled():
            if stopping.is_set() or cancelled.is_set() or time.monotonic() >= deadline:
                raise TimeoutError("Telesto request cancelled" if stopping.is_set()
                                   else "Telesto request deadline exceeded")

        def connection_type(base):
            class Connection(base):
                def __init__(self, *args, **kwargs):
                    super().__init__(*args, **kwargs)
                    connections.append(self)

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

                def getresponse(self):
                    response = super().getresponse()
                    # Redirect handlers can read the body before open returns.
                    responses.append(response)
                    return response
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
        if _is_loopback_uri(request.full_url):
            handlers.append(urllib.request.ProxyHandler({}))
        opener = urllib.request.build_opener(*handlers)

        def watch():
            while not done.wait(0.02):
                if not stopping.is_set() and time.monotonic() < deadline:
                    continue
                cancelled.set()
                sockets = [sock for conn in list(connections) if (sock := conn.sock) is not None]
                for response in list(responses):
                    try:
                        sockets.append(response.fp.raw._sock)
                    except AttributeError:
                        pass
                for sock in sockets:
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
                body = response.read((1 << 20) + 1)
                check_cancelled()
                if len(body) > 1 << 20:
                    raise ValueError("Telesto response too large")
                return response.getcode(), body
        finally:
            done.set()
            watcher.join()

    def _update_party_slots(self, body: bytes) -> None:
        """Sort valid party entries by order and map actors to consecutive slots. Keep the
        previous mapping when the response is malformed.
        """
        try:
            data = json.loads(body.decode("utf-8", "replace"))
        except (ValueError, AttributeError):
            return
        members = data.get("response") if isinstance(data, dict) else data
        if not isinstance(members, list):
            return
        if not members:
            # Clear stale slots on a valid empty roster.
            with self._lock:
                self._slot_by_actor = {}
            return

        def _order(entry):
            try:
                return int(str(entry.get("order")).strip(), 16)
            except (ValueError, AttributeError, TypeError):
                return 1 << 30  # unparseable order sorts last

        ordered = sorted((m for m in members if isinstance(m, dict)), key=_order)
        slots: "dict[int, int]" = {}
        # Skip invalid actors without leaving gaps in slot numbers.
        slot = 0
        for entry in ordered:
            aid = _actor_int(entry.get("actor"))
            if aid is None:
                continue
            slot += 1
            if slot > 8:
                break
            slots[aid] = slot
        # Replace the map for every valid roster, even if no actor parsed. Stale slots
        # could mark the wrong player.
        with self._lock:
            self._slot_by_actor = slots

    def _report_reachable(self, reachable: bool, message: str, degraded: bool = False) -> None:
        # Report transitions in both reachability and degraded state.
        state = (reachable, degraded)
        with self._lock:
            changed = self._reachable != state
            self._reachable = state
        if changed:
            self.status_changed.emit(reachable, message, degraded)
