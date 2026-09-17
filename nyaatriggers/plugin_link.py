"""Send timeline, callout and DPS frames to the companion overlay over loopback WebSocket.
One worker owns the socket and bounded queue. Reconnect after established sessions drop,
with backoff for failed connections. Discard stale schedules and ticks while retaining
alerts for delivery. Validate the greeting protocol and send no Origin header because
the plugin rejects browser connections.
"""

from __future__ import annotations

import json
import math
import queue
import socket
import threading
import time

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

try:
    from websockets.sync.client import connect as _ws_connect
    _HAVE_WS = True
except Exception:  # pragma: no cover - websockets is a declared dependency,
    _HAVE_WS = False

from nyaatriggers.drop_log import log_drop
from nyaatriggers.dps_meter import MAX_OVERLAY_ROWS


DEFAULT_PORT = 27080

# Must match BridgeHost.ProtocolVersion in the plugin.
PROTOCOL_VERSION = 1

# Bound the wait for the plugin greeting.
HELLO_TIMEOUT_S = 5.0

# Retry after five seconds, doubling up to sixty seconds.
RECONNECT_BASE_S = 5.0
RECONNECT_MAX_S = 60.0

# Probe replies are required even when fight traffic keeps sends busy.
# Keep the existing setting name for callers that tune the interval.
IDLE_PING_S = 15.0
PONG_TIMEOUT_S = 10.0

# Bound blocking sends so an unresponsive peer cannot stop delivery until the OS TCP
# timeout.
SEND_TIMEOUT_S = 10.0

# Bound queued frames without blocking the GUI. Preserve alerts when selecting an
# eviction.
OUTBOX_CAPACITY = 256

# Bound receive draining so continuous replies cannot starve sends or shutdown.
INBOUND_DRAIN_BATCH = 32

_SEVERITIES = ("info", "alert", "alarm")

_STOP = object()


# Frame builders
def tick_frame(seconds) -> dict:
    """Round valid fight time to centiseconds. Return None for invalid values."""
    try:
        ft = float(seconds)
        # Reject nonfinite values that the plugin JSON parser cannot read.
        if not math.isfinite(ft):
            raise ValueError("non-finite tick seconds")
        return {"c": "tick", "t": round(ft, 2)}
    except (TypeError, ValueError):
        log_drop("plugin-tx", f"tick dropped, bad seconds {seconds!r}")
        return None


def timeline_kind(label) -> str:
    """Infer a cue kind from its label. Use mechanic when no known pattern matches."""
    text = str(label).lower()
    if "tankbuster" in text or "tank buster" in text:
        return "tankbuster"
    if "raidwide" in text or "raid wide" in text or "raid-wide" in text:
        return "raidwide"
    return "mechanic"


def timeline_frame(entries) -> dict:
    """Tag timeline time and label pairs with cue kinds, skipping malformed entries."""
    clean = []
    for entry in entries:
        try:
            t, label = entry
            ft = float(t)
            if not math.isfinite(ft):
                raise ValueError("non-finite timeline time")
            label = str(label)
            clean.append([ft, label, timeline_kind(label)])
        except (TypeError, ValueError):
            log_drop("plugin-tx", f"timeline entry dropped: {entry!r}")
    return {"c": "timeline", "v": clean}


def alert_frame(text, severity="info") -> dict:
    """Build an alert with a valid severity. Omit ttl so plugin duration settings apply.
    """
    sev = str(severity)
    return {"c": "alert", "text": str(text),
            "sev": sev if sev in _SEVERITIES else "info"}


def clear_frame() -> dict:
    """Clear the schedule, alerts and meter state."""
    return {"c": "clear"}


def ping_frame() -> dict:
    return {"c": "ping"}


def dps_frame(enc, rows, show=True) -> dict:
    """Build a live or ended DPS frame. Live rows contain name, job, DPS, share, HPS, local
    flag and deaths. Supply defaults for missing trailing fields and reject invalid
    values.
    """
    if not show:
        return {"c": "dps", "show": False}
    enc = enc if isinstance(enc, dict) else {}
    try:
        dps = float(enc.get("dps", 0.0) or 0.0)
        if not math.isfinite(dps):
            raise ValueError("non-finite enc dps")
    except (TypeError, ValueError):
        dps = 0.0
    clean_rows = []
    for row in (rows or [])[:MAX_OVERLAY_ROWS]:
        try:
            name, job, encdps, share = row[0], row[1], row[2], row[3]
            hps = row[4] if len(row) > 4 else 0.0
            is_self = bool(row[5]) if len(row) > 5 else False
            deaths = int(row[6]) if len(row) > 6 else 0
            if deaths < 0:
                raise ValueError("negative deaths")
            vals = [float(encdps), float(share), float(hps)]
            if not all(math.isfinite(v) for v in vals):
                raise ValueError("non-finite dps row value")
            clean_rows.append([str(name), str(job), *vals, is_self, deaths])
        except (TypeError, ValueError, IndexError, OverflowError):
            # Skip rows whose integer fields cannot be converted.
            continue
    return {"c": "dps", "show": True,
            "enc": {"t": str(enc.get("t", "")), "d": str(enc.get("d", "")),
                    "dps": dps},
            "rows": clean_rows}


def parse_port(value) -> "int | None":
    """Accept integer ports in the plugin range. Reject booleans and invalid settings
    values.
    """
    if isinstance(value, bool):
        return None
    try:
        port = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return port if 1024 <= port <= 65535 else None


def plugin_supports_dps(version: str) -> bool:
    """DPS requires plugin version 0.2.0 but shares protocol 1 with older builds. Treat
    unknown version formats as supported to avoid false warnings.
    """
    try:
        parts = [int(piece) for piece in str(version).split(".")]
    except (TypeError, ValueError):
        return True
    return (parts + [0, 0])[:2] >= [0, 2]


class PluginLink(QObject):
    """WebSocket client for the companion plugin. Thread-safe public API."""

    # Connected flag and user-facing status. Detailed failures go to the log.
    status_changed = pyqtSignal(bool, str)

    def __init__(self, port: int = DEFAULT_PORT, enabled: bool = True,
                 idle_ping_s: float = IDLE_PING_S, parent=None) -> None:
        super().__init__(parent)
        self._port = int(port)
        self._enabled = bool(enabled)
        self._idle_ping_s = max(0.5, float(idle_ping_s))
        self._queue: "queue.Queue" = queue.Queue(maxsize=OUTBOX_CAPACITY)
        self._thread: "threading.Thread | None" = None
        self._stopping = threading.Event()
        self._wake = threading.Event()   # Wake the worker during backoff or disabled waits.
        self._lock = threading.RLock()
        self._connected = False
        self._reported: "tuple | None" = None
        self._plugin_version = ""

    def set_enabled(self, enabled: bool) -> None:
        with self._lock:
            self._enabled = bool(enabled)
        self._wake.set()

    def is_enabled(self) -> bool:
        with self._lock:
            return self._enabled

    def is_connected(self) -> bool:
        with self._lock:
            return self._connected

    def last_status(self) -> tuple:
        """Return the last connection status, defaulting to Off before the worker reports.
        """
        with self._lock:
            return self._reported or (False, "Off")

    def plugin_version(self) -> str:
        """Version from the latest greeting, used for feature support checks."""
        with self._lock:
            return self._plugin_version

    def set_port(self, port: int) -> None:
        """Change the destination port and wake the worker to reconnect."""
        with self._lock:
            self._port = int(port)
        self._wake.set()

    def start(self) -> None:
        with self._lock:
            t = self._thread
            if t and t.is_alive() and not self._stopping.is_set():
                return
            # Give replacement workers separate queues because an older worker may still
            # be connecting.
            if self._stopping.is_set():
                self._queue = queue.Queue(maxsize=OUTBOX_CAPACITY)
            self._stopping = threading.Event()
            self._wake = threading.Event()
            self._connected = False
            self._plugin_version = ""
            self._reported = None
            self._thread = threading.Thread(
                target=self._run, args=(self._queue, self._stopping, self._wake),
                name="PluginLink", daemon=True)
            self._thread.start()

    def request_stop(self) -> None:
        """Request shutdown without joining so callers can overlap client shutdown waits.
        """
        with self._lock:
            self._stopping.set()
            self._wake.set()
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
                # Retain the handle if the worker is still blocked so later stops can
                # join it.
                return
        with self._lock:
            if self._thread is t:
                self._thread = None

    def stop(self, join_timeout: float = 2.0) -> None:
        self.request_stop()
        self.join_stopped(join_timeout)

    def send_alert(self, text, severity: str = "info") -> None:
        log_drop("plugin-tx", f"alert[{severity}] {str(text)[:80]!r}", 0)
        self._enqueue(alert_frame(text, severity))

    def send_tick(self, seconds) -> None:
        frame = tick_frame(seconds)
        if frame is None:
            return
        log_drop("plugin-tx-tick", f"tick {frame['t']:.1f}s", 5.0)
        self._enqueue(frame)

    def send_timeline(self, entries) -> None:
        entries = list(entries)
        log_drop("plugin-tx", f"timeline {len(entries)} entries", 0)
        self._enqueue(timeline_frame(entries))

    def send_clear(self) -> None:
        log_drop("plugin-tx", "clear", 0)
        self._enqueue(clear_frame())

    def send_dps(self, enc, rows, show: bool = True) -> None:
        log_drop("plugin-tx-dps",
                 f"dps show={bool(show)} rows={len(rows or [])}", 5.0)
        self._enqueue(dps_frame(enc, rows, show))

    def _enqueue(self, msg: dict) -> None:
        # Keep the enabled check and eviction atomic. Queue operations remain
        # nonblocking.
        with self._lock:
            if not self._enabled:
                return
            try:
                self._queue.put_nowait(msg)
            except queue.Full:
                # Evict a non-alert frame because alerts cannot be reconstructed after
                # reconnect.
                dropped = self._evict_oldest(self._queue)
                if dropped is not None:
                    kind = dropped.get("c") if isinstance(dropped, dict) else "sentinel"
                    log_drop("plugin-drop",
                             f"outbox full, dropped the oldest {kind} frame", 5.0)
                try:
                    self._queue.put_nowait(msg)
                except queue.Full:
                    # Drop the new frame if only alerts remain or another producer took
                    # the slot.
                    log_drop("plugin-drop",
                             f"outbox full, dropped the new {msg.get('c', '?')} frame",
                             5.0)

    @staticmethod
    def _evict_oldest(q: "queue.Queue") -> "dict | None":
        """Remove the oldest non-alert frame while preserving retained order. Return None
        if every frame is an alert.
        """
        keep = []
        dropped = None
        while True:
            try:
                msg = q.get_nowait()
            except queue.Empty:
                break
            if dropped is None and not (isinstance(msg, dict)
                                        and msg.get("c") == "alert"):
                dropped = msg
            else:
                keep.append(msg)
        for msg in keep:
            try:
                q.put_nowait(msg)
            except queue.Full:
                # Stop and retry producers may fill the queue without taking the enqueue
                # lock.
                log_drop("plugin-drop", "outbox refill overflowed; frame dropped")
        return dropped

    def _set_connected(self, connected: bool, stopping=None) -> None:
        with self._lock:
            if stopping is not None and (self._stopping is not stopping
                                        or connected and stopping.is_set()):
                return
            self._connected = connected

    def _report(self, connected: bool, msg: str, stopping=None) -> None:
        # Report only connection state transitions.
        state = (connected, msg)
        with self._lock:
            if stopping is not None and (self._stopping is not stopping
                                        or stopping.is_set() and msg != "Off"):
                return
            if self._reported == state:
                return
            self._reported = state
            log_drop("plugin-link", f"{'connected' if connected else 'down'}: {msg}", 0)
            self.status_changed.emit(connected, msg)

    def _connect(self):
        """Connect and validate the greeting, allowing failures to trigger backoff. Omit
        Origin because the plugin rejects it.
        """
        if not _HAVE_WS:
            raise RuntimeError("the websockets package is not installed "
                               "(source runs: pip install -r requirements.txt)")
        with self._lock:
            port = self._port
        ws = _ws_connect(
            f"ws://127.0.0.1:{port}/",
            open_timeout=HELLO_TIMEOUT_S,
            close_timeout=1.0,
            ping_interval=None,   # Use protocol ping messages for liveness.
            logger=None)
        try:
            raw = ws.recv(timeout=HELLO_TIMEOUT_S)
            hello = json.loads(raw)
        except Exception:
            ws.close()
            raise
        if not isinstance(hello, dict) or hello.get("protocol") != PROTOCOL_VERSION:
            ws.close()
            raise RuntimeError(
                f"plugin speaks protocol "
                f"{hello.get('protocol') if isinstance(hello, dict) else hello!r}, "
                f"this app speaks {PROTOCOL_VERSION}")
        return ws, str(hello.get("plugin") or "")

    @staticmethod
    def _discard_queued(q: "queue.Queue", keep_alerts: bool = False) -> None:
        # Discard frames that will be rebuilt on reconnect. Preserve alerts because they
        # are emitted only once.
        keep = []
        discarded = 0
        while True:
            try:
                msg = q.get_nowait()
            except queue.Empty:
                break
            if keep_alerts and isinstance(msg, dict) and msg.get("c") == "alert":
                keep.append(msg)
            else:
                discarded += 1
        if discarded:
            log_drop("plugin-drop", f"discarded {discarded} queued frames while offline")
        for msg in keep:
            try:
                q.put_nowait(msg)
            except queue.Full:
                log_drop("plugin-drop", "alert re-queue overflowed; callout dropped")

    @staticmethod
    def _drain_inbound(ws, stopping: threading.Event) -> bool:
        """Read a bounded batch and report whether a pong arrived."""
        pong = False
        for _ in range(INBOUND_DRAIN_BATCH):
            if stopping.is_set():
                break
            try:
                raw = ws.recv(timeout=0)
            except TimeoutError:
                break
            try:
                msg = json.loads(raw)
            except (ValueError, RecursionError):
                continue
            if isinstance(msg, dict) and msg.get("ev") == "pong":
                pong = True
        return pong

    @staticmethod
    def _kill_socket(ws, done: threading.Event) -> None:
        """Abort the raw socket when send stalls. Connection.close could block on the send
        mutex. Recheck completion after a short grace to avoid closing a send that just
        finished.
        """
        if done.is_set():
            return
        if done.wait(0.1):
            return
        try:
            ws.socket.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def _send(self, ws, msg: dict) -> None:
        """Apply a deadline to blocking WebSocket sends."""
        done = threading.Event()
        killer = threading.Timer(SEND_TIMEOUT_S, self._kill_socket, args=(ws, done))
        killer.daemon = True
        killer.start()
        try:
            ws.send(json.dumps(msg))
        finally:
            done.set()
            killer.cancel()

    @staticmethod
    def _close_quietly(ws) -> None:
        try:
            ws.close()
        except Exception:
            pass

    def _run(self, q: "queue.Queue", stopping: threading.Event,
             wake: threading.Event) -> None:
        ws = None
        dialed = 0   # port the live socket connected to, 0 while down
        delay = RECONNECT_BASE_S
        next_ping = 0.0
        pong_deadline = None
        try:
            while not stopping.is_set():
                if not self.is_enabled():
                    if ws is not None:
                        # Closing the connection makes the plugin reset its state.
                        self._close_quietly(ws)
                        ws = None
                        self._set_connected(False, stopping)
                    # Report Off even if disabling occurred during backoff.
                    self._report(False, "Off", stopping)
                    wake.wait(0.5)
                    wake.clear()
                    continue

                if ws is None:
                    self._discard_queued(q, keep_alerts=True)
                    if stopping.is_set():
                        break
                    try:
                        with self._lock:
                            dialed = self._port
                        ws, plugin_version = self._connect()
                    except Exception as exc:  # noqa: BLE001
                        log_drop("plugin-link", f"connect failed: {exc}")
                        # Show actionable configuration errors. Connection refusal
                        # usually means the game is unavailable.
                        self._report(False, str(exc) if isinstance(exc, RuntimeError)
                                     else "Waiting for the game plugin", stopping)
                        wake.wait(delay)
                        wake.clear()
                        delay = min(delay * 2, RECONNECT_MAX_S)
                        continue
                    if stopping.is_set():
                        break
                    delay = RECONNECT_BASE_S
                    next_ping = time.monotonic() + self._idle_ping_s
                    pong_deadline = None
                    # Discard stale ticks and schedules accumulated during the greeting
                    # wait, retaining alerts.
                    self._discard_queued(q, keep_alerts=True)
                    with self._lock:
                        if self._stopping is not stopping or stopping.is_set():
                            break
                        self._plugin_version = plugin_version
                    self._set_connected(True, stopping)
                    self._report(True, f"Connected (plugin {plugin_version})"
                                       if plugin_version else "Connected", stopping)

                if dialed != self._port:
                    # Reconnect if settings changed the port during connection.
                    self._close_quietly(ws)
                    ws = None
                    self._set_connected(False, stopping)
                    self._report(False, "Waiting for the game plugin", stopping)
                    continue

                try:
                    due = pong_deadline if pong_deadline is not None else next_ping
                    msg = q.get(timeout=max(0.0, min(1.0, due - time.monotonic())))
                except queue.Empty:
                    msg = None
                if stopping.is_set():
                    break
                if msg is _STOP:
                    continue

                try:
                    # Read replies even while fight traffic keeps the outbox busy.
                    pong = self._drain_inbound(ws, stopping)
                    now = time.monotonic()
                    if pong and pong_deadline is not None:
                        pong_deadline = None
                        next_ping = now + self._idle_ping_s
                    if pong_deadline is not None and now >= pong_deadline:
                        raise TimeoutError("game plugin did not answer its ping")
                    if msg is not None:
                        self._send(ws, msg)
                        msg = None
                    if pong_deadline is None and time.monotonic() >= next_ping:
                        self._send(ws, ping_frame())
                        pong_deadline = time.monotonic() + PONG_TIMEOUT_S
                except Exception as exc:  # Keep the worker running after send failures.
                    log_drop("plugin-link", f"connection lost: {exc}")
                    # Retry failed alert sends because reconnect does not recreate them.
                    if isinstance(msg, dict) and msg.get("c") == "alert":
                        try:
                            q.put_nowait(msg)
                        except queue.Full:
                            log_drop("plugin-drop",
                                     "alert re-queue overflowed; callout dropped")
                    self._close_quietly(ws)
                    ws = None
                    self._set_connected(False, stopping)
                    self._report(False, "Waiting for the game plugin", stopping)
        finally:
            if ws is not None:
                self._close_quietly(ws)
            # A worker that outlives shutdown must not overwrite its replacement's
            # status.
            if self._stopping is stopping:
                self._set_connected(False, stopping)
                self._report(False, "Off", stopping)
