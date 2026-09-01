"""Push timeline bars and alert callouts to the Dalamud plugin.

The companion NyaaTriggers Dalamud plugin, the NyaaTriggers-Overlay repo,
draws the app's timeline and callouts inside the game. It serves a WebSocket on
loopback, default port 27080. This client connects to it, so the two can
start in either order. Wire protocol, one JSON object per text frame.

    app -> plugin  {"c":"tick","t":secs}            fight clock
                   {"c":"timeline","v":[[t,label,kind]]}  replace the schedule
                   {"c":"alert","text":...,"sev":...}  show a callout
                   {"c":"clear"}                    zone change / fight end
                   {"c":"dps","show":bool,...}       live DPS meter window
                   {"c":"ping"}                     liveness, answered by pong
    plugin -> app  {"ev":"hello","protocol":1,"plugin":"x.y.z"}  on connect
                   {"ev":"pong"}

The timeline kind is a tag derived from the label text, tankbuster or
raidwide or the mechanic default, so the plugin can colour bars by it. The
dps rows carry deaths as a trailing field. Both are additive: a plugin that
predates them reads the frames it already knew, and this client never hears
back either way.

The plugin refuses any handshake carrying an Origin header. The websockets
client sends none unless asked, so none gets asked for.

One daemon worker owns the socket and drains a bounded outbound queue. The
reconnect policy mirrors the IINACT client in ws_client.py, 5 s first retry,
doubled per failure up to 60 s, reset after a good connect. A connection
that drops after being established is retried right away. The backoff
throttles failing connects, not recovery. Sends never raise and never block
the caller. Frames queued while the link is down get dropped, a stale fight
clock or schedule is worse than none. Alerts are the exception, fire-once,
so they ride the queue until a connect can deliver them.
"""

from __future__ import annotations

import json
import math
import queue
import socket
import threading

try:
    from PyQt6.QtCore import QObject, pyqtSignal
    _HAVE_QT = True
except Exception:  # pragma: no cover
    _HAVE_QT = False

    class QObject:  # shim so the module imports without Qt, CI and tests
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
                    except Exception:  # match Qt, one bad slot doesn't stop emit
                        import traceback
                        traceback.print_exc()

        return _Dummy()

try:
    from websockets.sync.client import connect as _ws_connect
    _HAVE_WS = True
except Exception:  # pragma: no cover - websockets is a declared dependency,
    _HAVE_WS = False   # the link just reports it cannot run without it

from drop_log import log_drop
from dps_meter import MAX_OVERLAY_ROWS


DEFAULT_PORT = 27080

# Wire format version. Must match the plugin's BridgeHost.ProtocolVersion.
PROTOCOL_VERSION = 1

# A plugin that upgrades the socket but never greets is not the plugin. Do
# not wait on it forever. Same guard as test_bridge.py in the plugin repo.
HELLO_TIMEOUT_S = 5.0

# Reconnect policy, mirrored from ws_client.py. 5000 ms first, doubled to 60 s.
RECONNECT_BASE_S = 5.0
RECONNECT_MAX_S = 60.0

# Protocol-level liveness ping after this much send-idle time. Pongs are
# read and ignored. The ping exists so a half-dead link is noticed while no
# fight traffic flows.
IDLE_PING_S = 15.0

# A healthy loopback peer drains a frame instantly, so a send that takes this
# long means the plugin stopped reading its socket. websockets' send has no
# per call timeout and bottoms out in a blocking sendall, which without this
# deadline parks the worker until the kernel gives up, 13 to 15 minutes on a
# default Linux tcp_retries2, and no callout reaches the game meanwhile.
SEND_TIMEOUT_S = 10.0

# Outbound backlog before the oldest non-alert frames start dropping. Sends
# must never block the caller, so a wedged peer costs stale frames, not the
# GUI. Same bound and policy as the plugin's own outbox.
OUTBOX_CAPACITY = 256

# Inbound frames swallowed per drain pass before the worker returns to its
# loop. A peer flooding past the hello gate keeps recv fed forever, so an
# unbounded sweep would starve the outbox and stall stop requests.
INBOUND_DRAIN_BATCH = 32

# Severity vocabulary shared with the plugin. Anything else degrades to info.
_SEVERITIES = ("info", "alert", "alarm")

# Shutdown sentinel for the worker queue.
_STOP = object()


# ----------------------------------------------------------------------------
# Pure frame builders, for exact-byte unit tests.
# ----------------------------------------------------------------------------
def tick_frame(seconds) -> dict:
    """Fight clock. Rounded like the plugin repo's test_bridge.py. The plugin
    interpolates between ticks, so centisecond precision is ample. Junk
    seconds return None and the senders skip the frame, sends never raise."""
    try:
        ft = float(seconds)
        # json.dumps writes inf as bare Infinity, which the plugin's
        # strict parser rejects wholesale. Non-finite is junk too.
        if not math.isfinite(ft):
            raise ValueError("non-finite tick seconds")
        return {"c": "tick", "t": round(ft, 2)}
    except (TypeError, ValueError):
        log_drop("plugin-tx", f"tick dropped, bad seconds {seconds!r}")
        return None


def timeline_kind(label) -> str:
    """The kind tag for one timeline label. Timeline sources, cactbot's txt
    files included, carry no kind of their own, so the label text is matched
    against the words timeline authors actually write in them. Anything
    without a match is a plain mechanic, which the plugin draws with the
    shared bar colour anyway."""
    text = str(label).lower()
    if "tankbuster" in text or "tank buster" in text:
        return "tankbuster"
    if "raidwide" in text or "raid wide" in text or "raid-wide" in text:
        return "raidwide"
    return "mechanic"


def timeline_frame(entries) -> dict:
    """Replace the schedule. `entries` is TimelineEngine.upcoming's shape,
    timeline second and label pairs; each leaves here tagged with its kind
    from timeline_kind. Junk entries drop out rather than raise, the
    sends-never-raise contract covers the frame builders too."""
    clean = []
    for entry in entries:
        try:
            t, label = entry
            ft = float(t)
            # json.dumps writes inf as bare Infinity, which the plugin's
            # strict parser rejects wholesale. Non-finite is junk too.
            if not math.isfinite(ft):
                raise ValueError("non-finite timeline time")
            label = str(label)
            clean.append([ft, label, timeline_kind(label)])
        except (TypeError, ValueError):
            log_drop("plugin-tx", f"timeline entry dropped: {entry!r}")
    return {"c": "timeline", "v": clean}


def alert_frame(text, severity="info") -> dict:
    """One callout. Unknown severities become info rather than being sent
    outside the documented vocabulary. The plugin would read them as info
    anyway. ttl is omitted so the plugin's configured per severity times
    apply."""
    sev = str(severity)
    return {"c": "alert", "text": str(text),
            "sev": sev if sev in _SEVERITIES else "info"}


def clear_frame() -> dict:
    """Drop the schedule and any live alerts, zone change, fight end or wipe."""
    return {"c": "clear"}


def ping_frame() -> dict:
    return {"c": "ping"}


def dps_frame(enc, rows, show=True) -> dict:
    """DPS meter state for the in-game meter window. `show` False hides it
    encounter ended. Otherwise `enc` is {"t": title, "d": "mm:ss",
    "dps": party encdps} and `rows` are [name, job, encdps, damage%, enchps,
    is_self, deaths] entries, capped at MAX_OVERLAY_ROWS. The trailing three
    fields are optional on the way in, older callers send the 4-field shape,
    and default to 0.0/False/0. Values are coerced so a sloppy caller can't
    break the plugin's JSON contract."""
    if not show:
        return {"c": "dps", "show": False}
    enc = enc if isinstance(enc, dict) else {}
    try:
        dps = float(enc.get("dps", 0.0) or 0.0)
        # json.dumps writes inf as bare Infinity, which the plugin's
        # strict parser rejects wholesale. Non-finite is junk too.
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
            # json.dumps writes inf as bare Infinity, which the plugin's
            # strict parser rejects wholesale. Non-finite is junk too.
            if not all(math.isfinite(v) for v in vals):
                raise ValueError("non-finite dps row value")
            clean_rows.append([str(name), str(job), *vals, is_self, deaths])
        except (TypeError, ValueError, IndexError, OverflowError):
            # int of an infinite float raises OverflowError, that junk drops
            # the row like any other bad field instead of escaping.
            continue
    return {"c": "dps", "show": True,
            "enc": {"t": str(enc.get("t", "")), "d": str(enc.get("d", "")),
                    "dps": dps},
            "rows": clean_rows}


def parse_port(value) -> "int | None":
    """A plugin port from the settings file or the Settings field, both hand
    editable so the value can be anything. Only a number inside the plugin's
    own clamp range counts, anything else returns None and the caller falls
    back to the default. bool is an int in Python but never a port."""
    if isinstance(value, bool):
        return None
    try:
        port = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return port if 1024 <= port <= 65535 else None


def plugin_supports_dps(version: str) -> bool:
    """Whether a connected plugin build draws the DPS meter. The meter arrived
    with plugin 0.2.0 and the wire stayed at protocol 1, so an older plugin
    connects cleanly and just never shows it. The hello carries a three or
    four part version, 0.1.0 or 0.2.0.8. An unparseable string answers True,
    an unknown build should not raise a false warning."""
    try:
        parts = [int(piece) for piece in str(version).split(".")]
    except (TypeError, ValueError):
        return True
    return (parts + [0, 0])[:2] >= [0, 2]


# ----------------------------------------------------------------------------
class PluginLink(QObject):
    """WebSocket client for the companion plugin. Thread-safe public API."""

    # Emitted as connected, message. Mirrors WSClient.status_changed. The
    # message is a short user-facing state, "Connected", "Off" and so on.
    # The detail behind a failure goes to the log.
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
        self._wake = threading.Event()   # interrupts backoff / the disabled wait
        self._lock = threading.Lock()
        self._connected = False
        self._reported: "tuple | None" = None   # last connected, msg pair emitted
        self._plugin_version = ""   # last hello's version, "" before any connect

    # -- configuration ------------------------------------------------------
    def set_enabled(self, enabled: bool) -> None:
        with self._lock:
            self._enabled = bool(enabled)
        self._wake.set()   # connect promptly when enabling while backing off

    def is_enabled(self) -> bool:
        with self._lock:
            return self._enabled

    def is_connected(self) -> bool:
        with self._lock:
            return self._connected

    def last_status(self) -> tuple:
        """Last reported connected, message pair, or the False/"Off" default
        before the worker has reported anything. The Settings indicator seeds
        from this. Signal-less consumers, meaning tests, can poll it."""
        with self._lock:
            return self._reported or (False, "Off")

    def plugin_version(self) -> str:
        """Version string from the last hello, "" before the first connect.
        Drives the too old for the meter hint on the status label."""
        with self._lock:
            return self._plugin_version

    def set_port(self, port: int) -> None:
        """Retarget the port from the Settings field. The worker notices the
        drift within a beat and re-dials, and the wake cuts any backoff so a
        down link tries the new port right away."""
        with self._lock:
            self._port = int(port)
        self._wake.set()

    # -- lifecycle ----------------------------------------------------------
    def start(self) -> None:
        t = self._thread
        if t and t.is_alive():
            if not self._stopping.is_set():
                return                         # already running
            # stop timed out joining this worker. It's finishing one blocking
            # call, exits on its own since its stopping event is set, and
            # never takes from the queue again, so spawning the next is safe.
        # Fresh events per generation, bound to the worker via args. Clearing
        # shared events could revive an old worker that outlived the join
        # timeout in stop. The queue is created once and kept. The worker
        # discards any stale _STOP left by a previous generation.
        self._stopping = threading.Event()
        self._wake = threading.Event()
        self._thread = threading.Thread(
            target=self._run, args=(self._queue, self._stopping, self._wake),
            name="PluginLink", daemon=True)
        self._thread.start()

    def request_stop(self) -> None:
        """Signal the worker out and queue the sentinel, without joining.
        closeEvent requests both clients first and joins after, so their
        shutdown waits overlap instead of adding up."""
        self._stopping.set()
        self._wake.set()
        try:
            self._queue.put_nowait(_STOP)
        except queue.Full:
            # Drain one slot so the sentinel lands. The worker checks _stopping anyway.
            try:
                self._queue.get_nowait()
                self._queue.put_nowait(_STOP)
            except (queue.Empty, queue.Full):
                pass

    def join_stopped(self, timeout: float = 2.0) -> None:
        """Join the worker after request_stop."""
        t = self._thread
        if t and t.is_alive():
            t.join(timeout=timeout)
            if t.is_alive():
                # Still inside a blocking connect/recv. Keep the handle so
                # start sees the lingering worker and a repeated stop can
                # join it again.
                return
        self._thread = None

    def stop(self, join_timeout: float = 2.0) -> None:
        self.request_stop()
        self.join_stopped(join_timeout)

    # -- outbound API, all no-raise, no-block ---------------------------------
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
        # Throttled like the tick. This fires once a second while a fight runs.
        log_drop("plugin-tx-dps",
                 f"dps show={bool(show)} rows={len(rows or [])}", 5.0)
        self._enqueue(dps_frame(enc, rows, show))

    # -- internals ----------------------------------------------------------
    def _enqueue(self, msg: dict) -> None:
        # One lock run for the gate and the drop-oldest sequence, the same
        # shape as tts._enqueue. Every op is nonblocking, so holding the
        # lock here never parks a caller.
        with self._lock:
            if not self._enabled:
                return
            try:
                self._queue.put_nowait(msg)
            except queue.Full:
                # Bounded and drop-oldest, like the plugin's own outbox. A
                # wedged peer costs a stale frame, never a blocked caller.
                # Alerts are fire-once riders, so eviction drops the oldest
                # non-alert frame and leaves them queued.
                dropped = self._evict_oldest(self._queue)
                if dropped is not None:
                    kind = dropped.get("c") if isinstance(dropped, dict) else "sentinel"
                    log_drop("plugin-drop",
                             f"outbox full, dropped the oldest {kind} frame", 5.0)
                try:
                    self._queue.put_nowait(msg)
                except queue.Full:
                    # The outbox held nothing but alerts, or a stop sentinel
                    # took the freed slot first. The new frame is the casualty.
                    log_drop("plugin-drop",
                             f"outbox full, dropped the new {msg.get('c', '?')} frame",
                             5.0)

    @staticmethod
    def _evict_oldest(q: "queue.Queue") -> "dict | None":
        """Free one outbox slot by dropping the oldest non-alert frame. Alerts
        are fire-once and keep their slots, the same predicate the offline
        sweep re-queues them by. The drain and refill keeps the surviving
        order exact. Returns the dropped frame, None when the outbox held
        nothing but alerts."""
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
                # A stop sentinel or a re-queued alert can grab a slot mid
                # refill. Neither producer holds the enqueue lock.
                log_drop("plugin-drop", "outbox refill overflowed; frame dropped")
        return dropped

    def _set_connected(self, connected: bool) -> None:
        with self._lock:
            self._connected = connected

    def _report(self, connected: bool, msg: str) -> None:
        # Emit only on a transition so a reconnect loop doesn't spam the UI.
        state = (connected, msg)
        with self._lock:
            if self._reported == state:
                return
            self._reported = state
        log_drop("plugin-link", f"{'connected' if connected else 'down'}: {msg}", 0)
        self.status_changed.emit(connected, msg)

    def _connect(self):
        """Open the socket and validate the hello. Raises on any failure. The
        worker turns that into backoff. Sends no Origin header, websockets
        only sends one when explicitly given. The plugin refuses handshakes
        that carry one."""
        if not _HAVE_WS:
            raise RuntimeError("the websockets package is not installed "
                               "(source runs: pip install -r requirements.txt)")
        with self._lock:
            port = self._port   # set_port can move it between loop iterations
        ws = _ws_connect(
            f"ws://127.0.0.1:{port}/",
            open_timeout=HELLO_TIMEOUT_S,
            close_timeout=1.0,
            ping_interval=None,   # liveness is protocol-level, the ping frame
            logger=None)
        try:
            raw = ws.recv(timeout=HELLO_TIMEOUT_S)
            hello = json.loads(raw)
        except Exception:
            ws.close()
            raise
        # Gate rather than warn. Driving a plugin whose wire format we do not
        # understand produces confusing in-game behaviour, not a clean failure.
        if not isinstance(hello, dict) or hello.get("protocol") != PROTOCOL_VERSION:
            ws.close()
            raise RuntimeError(
                f"plugin speaks protocol "
                f"{hello.get('protocol') if isinstance(hello, dict) else hello!r}, "
                f"this app speaks {PROTOCOL_VERSION}")
        return ws, str(hello.get("plugin") or "")

    @staticmethod
    def _discard_queued(q: "queue.Queue", keep_alerts: bool = False) -> None:
        # Frames queued while the link was down are stale before they can be
        # sent. The schedule is re-pushed by the main window on connect.
        # Alerts are fire-once and nothing re-pushes them, so the sweeps
        # re-queue them instead of dropping them, bounded by the outbox cap.
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
    def _drain_inbound(ws, stopping: threading.Event) -> None:
        """Swallow whatever the plugin sent, pongs answer our pings. The
        content carries nothing the app acts on. Reading is what notices a
        plugin-side close promptly instead of at the next send. The sweep is
        bounded per pass and honors stopping, so a flooding peer cannot park
        the worker here while the outbox waits."""
        for _ in range(INBOUND_DRAIN_BATCH):
            if stopping.is_set():
                return
            try:
                ws.recv(timeout=0.05)
            except TimeoutError:
                return

    @staticmethod
    def _kill_socket(ws, done: threading.Event) -> None:
        """Send watchdog target. A parked sendall holds websockets' protocol
        mutex, so Connection.close would block behind it forever. Shutting
        the raw socket down fails the send instead, and the worker's except
        path then tears the connection down and reconnects. `done` is set the
        moment the send returns, but the timer can land in the few bytecodes
        between the send returning and that stamp. Wait a short grace and
        re-check, so a firing in that window leaves a healthy socket alone."""
        if done.is_set():
            return
        if done.wait(0.1):
            return
        try:
            ws.socket.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def _send(self, ws, msg: dict) -> None:
        """ws.send under a hard deadline, the send itself has none."""
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
        except Exception:  # peer already gone, the socket close says the rest
            pass

    def _run(self, q: "queue.Queue", stopping: threading.Event,
             wake: threading.Event) -> None:
        ws = None
        dialed = 0   # port the live socket connected to, 0 while down
        delay = RECONNECT_BASE_S
        idle = 0.0
        try:
            while not stopping.is_set():
                if not self.is_enabled():
                    if ws is not None:
                        # The plugin drops its state when the app disconnects,
                        # so a plain close is the whole teardown.
                        self._close_quietly(ws)
                        ws = None
                        self._set_connected(False)
                    # Off is reported even with no socket: a disable during
                    # backoff must replace the standing Waiting report.
                    # Deduped, so the idle loop stays quiet.
                    self._report(False, "Off")
                    wake.wait(0.5)
                    wake.clear()
                    continue

                if ws is None:
                    # Between connects nothing can be delivered, but alerts
                    # are fire-once so they stay queued for the next attempt.
                    self._discard_queued(q, keep_alerts=True)
                    if stopping.is_set():
                        break
                    try:
                        with self._lock:
                            dialed = self._port
                        ws, plugin_version = self._connect()
                    except Exception as exc:  # noqa: BLE001 - any failure backs off
                        log_drop("plugin-link", f"connect failed: {exc}")
                        # Our own gate errors, protocol mismatch and missing
                        # dependency, are user-actionable and shown as is.
                        # A plain refusal just means the game isn't up.
                        self._report(False, str(exc) if isinstance(exc, RuntimeError)
                                     else "Waiting for the game plugin")
                        wake.wait(delay)
                        wake.clear()
                        delay = min(delay * 2, RECONNECT_MAX_S)
                        continue
                    delay = RECONNECT_BASE_S   # backoff resets after a good connect
                    idle = 0.0
                    # The hello wait can last seconds. Ticks and schedules
                    # queued in that window are as stale as the ones dropped
                    # before connect, but alerts are fire-once and get
                    # re-queued instead.
                    self._discard_queued(q, keep_alerts=True)
                    with self._lock:
                        self._plugin_version = plugin_version
                    self._set_connected(True)
                    self._report(True, f"Connected (plugin {plugin_version})"
                                       if plugin_version else "Connected")

                if dialed != self._port:
                    # set_port moved the target after this socket connected.
                    # Re-dial rather than keep feeding the old plugin.
                    self._close_quietly(ws)
                    ws = None
                    self._set_connected(False)
                    self._report(False, "Waiting for the game plugin")
                    continue

                try:
                    msg = q.get(timeout=1.0)
                except queue.Empty:
                    msg = None
                if msg is _STOP:
                    if stopping.is_set():
                        break
                    continue                   # stale sentinel from a previous generation

                try:
                    if msg is not None:
                        self._send(ws, msg)
                        idle = 0.0
                    else:
                        self._drain_inbound(ws, stopping)
                        idle += 1.0
                        if idle >= self._idle_ping_s:
                            self._send(ws, ping_frame())
                            idle = 0.0
                except Exception as exc:  # never let one bad send kill the worker
                    log_drop("plugin-link", f"connection lost: {exc}")
                    # An alert whose send failed would vanish with the socket,
                    # it is fire-once and nothing re-pushes it. Re-queue it
                    # for the reconnect, the same keep the offline sweep gives.
                    if isinstance(msg, dict) and msg.get("c") == "alert":
                        try:
                            q.put_nowait(msg)
                        except queue.Full:
                            log_drop("plugin-drop",
                                     "alert re-queue overflowed; callout dropped")
                    self._close_quietly(ws)
                    ws = None
                    self._set_connected(False)
                    self._report(False, "Waiting for the game plugin")
        finally:
            if ws is not None:
                self._close_quietly(ws)
            # A worker that outlived the join timeout in stop leaves the
            # shared status to the generation that replaced it.
            if self._stopping is stopping:
                self._set_connected(False)
                # Stop used to leave last_status stale at Connected. De-duped,
                # so this is silent when the loop already reported Off on
                # disable.
                self._report(False, "Off")
