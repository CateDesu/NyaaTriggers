"""Telesto client. Places FFXIV head-sign markers through the Telesto Dalamud plugin.

Telesto - https://github.com/paissaheavyindustries/Telesto - serves HTTP on
http://localhost:45678/ and runs in-game /mk commands on request. Wire format
mirrors Triggevent's telesto-support source.

Two targeting modes.
  * on you. /mk <marker> <me>, a direct POST.
  * on a party member. /mk <marker> <N> where <N> is the game party-list slot,
    1..8, resolved from the GetPartyMembers response. That response is a list
    of {order, actor} entries sorted by order, and the 1-based position is <N>.
    Unknown slot means fail closed, skip the mark rather than guess.

stdlib urllib only, no requests dep. A single daemon worker drains a queue
serially with a base+random delay because the game throttles rapid /mk spam.
Telesto is optional. Send errors never crash, they flip a de-duped status
signal.
"""

from __future__ import annotations

import http.client
import json
import queue
import random
import threading
import urllib.error
import urllib.request

from drop_log import log_drop
from locale_util import N_   # noop mark, combo labels are translated at render via _

try:
    from PyQt6.QtCore import QObject, pyqtSignal
    _HAVE_QT = True
except Exception:  # pragma: no cover
    _HAVE_QT = False

    class QObject:  # shim so the module imports without Qt, for CI and tests
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


# Protocol constants, TelestoMain.java and DoodleProcessor.java.
VERSION = 1
GAME_CMD_ID = 1_000_000
PARTY_UPDATE_ID = 1_000_001

DEFAULT_URI = "http://localhost:45678/"

# Shutdown sentinel for the worker queue.
_STOP = object()

# Head-sign markers as UI label plus /mk token. English-client tokens. The
# attack*/bind*/shapes are identical on every client, but "ignore1"/"ignore2"
# are localized, DE "ignor", JP/KR "stop", and silently no-op on non-English clients.
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


# ----------------------------------------------------------------------------
# Pure message builders, for exact-byte unit tests.
# ----------------------------------------------------------------------------
def make_message(msg_id: int, msg_type: str, payload=None) -> dict:
    """Telesto POST envelope, mirrors TelestoMain.makeMessage. payload defaults
    to {} like the Java side."""
    return {
        "version": VERSION,
        "id": int(msg_id),
        "type": str(msg_type),
        "payload": {} if payload is None else payload,
    }


def game_command_message(command: str) -> dict:
    """ExecuteCommand envelope for one game text command, e.g. '/mk attack1 <me>'."""
    return make_message(GAME_CMD_ID, "ExecuteCommand", {"command": str(command)})


def party_members_message() -> dict:
    """GetPartyMembers envelope. Side-effect-free. Doubles as a reachability probe."""
    return make_message(PARTY_UPDATE_ID, "GetPartyMembers", None)


def _slot_token(slot) -> str:
    """Wrap a target in FFXIV placeholder syntax. 2 becomes '<2>', 'me' stays '<me>'.
    Already-wrapped '<t>' stays as-is."""
    s = str(slot).strip()
    if s.startswith("<") and s.endswith(">"):
        return s
    return f"<{s}>"


def _actor_int(actor_id) -> "int | None":
    """Parse an actor/object id to an int. Accepts int or hex string, log lines
    and Telesto both use hex, e.g. '10FF1234', with decimal fallback. Returns
    None for blank/invalid ids and the no-target sentinels 0 and E0000000."""
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
    """Build a `/mk` command. Empty marker falls back to generic next-attack,
    '/mk attack <target>'. target is a slot id or placeholder like 'me'.
    An unknown non-empty token gets the same fallback, but logged. A typo in
    a hand-edited rule must not turn into a wrong sign with no trace."""
    m = (str(marker).strip() if marker is not None else "")
    if m not in MARKER_TOKENS:
        if m:
            log_drop("telesto-mark", f"unknown marker {m!r}, using next-attack")
        m = "attack"
    return f"/mk {m} {_slot_token(target)}"


# ----------------------------------------------------------------------------
class TelestoClient(QObject):
    """Queued HTTP client for the Telesto plugin. Thread-safe public API."""

    # reachable bool, message str, degraded bool. Drives the connection
    # indicator. degraded marks "answers but errors", persistent HTTP non-2xx.
    # Not down, but not healthy either.
    status_changed = pyqtSignal(bool, str, bool)
    # message str. One-off non-fatal error worth surfacing.
    error = pyqtSignal(str)

    def __init__(self, uri: str = DEFAULT_URI, enabled: bool = False,
                 delay_base_ms: int = 100, delay_plus_ms: int = 100,
                 timeout: float = 4.0, max_queue: int = 1000, parent=None) -> None:
        super().__init__(parent)
        # Raw settings values land here. A truthy non-string, say a number
        # from a hand edited settings file, would raise inside
        # urllib.request.Request on every send and kill the transport.
        self._uri = uri if isinstance(uri, str) and uri else DEFAULT_URI
        self._enabled = bool(enabled)
        self._delay_base = max(0, int(delay_base_ms))
        self._delay_plus = max(0, int(delay_plus_ms))
        self._timeout = float(timeout)
        self._max_queue = max(1, int(max_queue))
        self._queue: "queue.Queue" = queue.Queue(maxsize=self._max_queue)
        self._thread: "threading.Thread | None" = None
        self._stopping = threading.Event()
        self._lock = threading.Lock()
        self._reachable: "bool | None" = None  # last reported reachability, de-duped
        self._warned_sends: set = set()        # unexpected send failures already logged
        # actor id as int -> 1-based party slot <N>, rebuilt per GetPartyMembers
        # response. Empty until the first list, mark_actor fails closed.
        self._slot_by_actor: "dict[int, int]" = {}

    # -- configuration ------------------------------------------------------
    def configure(self, uri: "str | None" = None, enabled: "bool | None" = None,
                  delay_base_ms: "int | None" = None,
                  delay_plus_ms: "int | None" = None) -> None:
        """Update settings live. Safe from the GUI thread anytime."""
        with self._lock:
            if uri is not None:
                # Same non-string guard as __init__, raw settings reach here too.
                self._uri = uri if isinstance(uri, str) and uri else DEFAULT_URI
            if enabled is not None:
                self._enabled = bool(enabled)
            if delay_base_ms is not None:
                self._delay_base = max(0, int(delay_base_ms))
            if delay_plus_ms is not None:
                self._delay_plus = max(0, int(delay_plus_ms))

    def set_enabled(self, enabled: bool) -> None:
        with self._lock:
            self._enabled = bool(enabled)

    def is_enabled(self) -> bool:
        with self._lock:
            return self._enabled

    @property
    def uri(self) -> str:
        with self._lock:
            return self._uri

    # -- lifecycle ----------------------------------------------------------
    def start(self) -> None:
        t = self._thread
        if t and t.is_alive():
            if not self._stopping.is_set():
                return                         # already running
            # stop timed out joining this worker. It's finishing one blocking
            # HTTP call, exits on its own, its stopping event is set, and never
            # takes from the queue again, so spawning the next one is safe.
        # Fresh stopping event per generation, bound to the worker via args.
        # Clearing a shared event could revive an old worker that outlived
        # stop's join timeout. The queue is created once in __init__ and kept,
        # so commands enqueued before start aren't orphaned. The worker
        # discards any stale _STOP left by a previous generation.
        self._stopping = threading.Event()
        self._thread = threading.Thread(
            target=self._run, args=(self._queue, self._stopping),
            name="TelestoClient", daemon=True)
        self._thread.start()

    def request_stop(self) -> None:
        """Signal the worker out and queue the sentinel, without joining.
        closeEvent requests both clients first and joins after, so their
        shutdown waits overlap instead of adding up."""
        self._stopping.set()
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
                # Still inside a blocking HTTP call, urlopen's timeout can exceed
                # the timeout. Keep the handle so start sees the lingering
                # worker and a repeated stop can join it again.
                return
        self._thread = None

    def stop(self, join_timeout: float = 2.0) -> None:
        self.request_stop()
        self.join_stopped(join_timeout)

    # -- high-level command API, head-signs --------------------------------
    # These all report whether the command actually landed on the queue.
    # False means it went nowhere, so the caller can retry instead of
    # burning a cooldown on a mark that never left.
    def send_game_command(self, command: str, force: bool = False) -> bool:
        """Queue a raw game text command, delayed. force=True bypasses the
        enabled gate, the Test button."""
        if not command:
            return False
        return self._enqueue(game_command_message(command), delay=True, force=force)

    def mark(self, marker, target, force: bool = False) -> bool:
        """Place a head-sign, e.g. marker='attack1', target='me' -> /mk attack1 <me>."""
        return self.send_game_command(mark_command(marker, target), force=force)

    def mark_self(self, marker, force: bool = False) -> bool:
        """Place a sign on you, `/mk <marker> <me>`."""
        return self.mark(marker, "me", force=force)

    def mark_slot(self, marker, slot, force: bool = False) -> bool:
        """Place a sign on a party slot 1..8, `/mk <marker> <N>`."""
        return self.mark(marker, int(slot), force=force)

    def mark_actor(self, actor_id, marker, force: bool = False) -> bool:
        """Place a sign on the party member with this actor id via their resolved
        slot. Returns False, nothing queued, when the slot is unknown or the
        command was dropped. Never guesses a slot, so a stale/empty party
        list skips the mark."""
        slot = self.slot_of_actor(actor_id)
        if not slot:
            return False
        return self.mark_slot(marker, slot, force=force)

    def slot_of_actor(self, actor_id) -> "int | None":
        """Current 1-based party slot for an actor id, or None if unknown."""
        aid = _actor_int(actor_id)
        if aid is None:
            return None
        with self._lock:
            return self._slot_by_actor.get(aid)

    def party_slot_count(self) -> int:
        """Resolved party slot count, 0 until the first party list."""
        with self._lock:
            return len(self._slot_by_actor)

    def clear_self(self, force: bool = False) -> bool:
        """Remove the sign on you, `/mk clear <me>`."""
        return self.send_game_command("/mk clear <me>", force=force)

    def clear_actor(self, actor_id, force: bool = False) -> bool:
        """Remove the sign from this actor, failing closed like mark_actor."""
        slot = self.slot_of_actor(actor_id)
        if not slot:
            return False
        return self.send_game_command(f"/mk clear <{int(slot)}>", force=force)

    def clear_all(self, force: bool = False) -> None:
        """Remove head-signs from every party slot, `/mk clear <1>` through `<8>`."""
        for n in range(1, 9):
            self.send_game_command(f"/mk clear <{n}>", force=force)

    def request_party_members(self, force: bool = False) -> None:
        """Ask Telesto for the party list. Every response also refreshes the
        actor->slot map, so this doubles as a reachability probe."""
        self._enqueue(party_members_message(), delay=False, force=force)

    def ping(self) -> None:
        """Probe reachability now, regardless of enabled state."""
        self.request_party_members(force=True)

    # -- internals ----------------------------------------------------------
    def _enqueue(self, msg: dict, delay: bool, force: bool = False) -> bool:
        """Put the message on the worker queue. Returns False when it went
        nowhere, gated off or a full queue, so senders can report the drop."""
        if not force:
            with self._lock:
                if not self._enabled:
                    return False
        try:
            self._queue.put_nowait((msg, delay))
        except queue.Full:
            log_drop("telesto-queue", "command queue full, dropping message")
            self.error.emit("Telesto command queue full; command dropped")
            return False
        return True

    def _run(self, q: "queue.Queue", stopping: "threading.Event") -> None:
        while not stopping.is_set():
            try:
                # Poll with a timeout, like plugin_link, so the worker re-checks
                # `stopping` each second even if the _STOP sentinel never lands.
                # A full-queue race in stop can strand a blocking get.
                item = q.get(timeout=1.0)
            except queue.Empty:
                continue
            except Exception:  # pragma: no cover
                stopping.wait(0.05)            # never hot-loop if get misbehaves
                continue
            if item is _STOP:
                if stopping.is_set():
                    break
                continue                       # stale sentinel from a previous generation
            msg, delay = item
            if delay:
                self._sleep_command_delay(stopping)
            # Both paths re-check after dequeue so a queued POST cannot fire
            # during shutdown.
            if stopping.is_set():
                break
            try:
                self._post(msg)
            except Exception as exc:  # never let one bad send kill the worker
                key = f"{type(exc).__name__}: {exc}"[:200]
                if key not in self._warned_sends and len(self._warned_sends) < 32:
                    self._warned_sends.add(key)
                    log_drop("telesto-send", f"send failed: {exc}", 0)

    def _sleep_command_delay(self, stopping: "threading.Event") -> None:
        with self._lock:
            base, plus = self._delay_base, self._delay_plus
        # base + random * plus ms, like TelestoMain.
        delay_s = (base + (random.random() * plus)) / 1000.0
        if delay_s > 0:
            # Interruptible so stop doesn't block for the full delay.
            stopping.wait(delay_s)

    def _post(self, msg: dict) -> None:
        with self._lock:
            uri, timeout = self._uri, self._timeout
        body = json.dumps(msg).encode("utf-8")
        req = urllib.request.Request(
            uri, data=body, method="POST",
            headers={"Content-Type": "application/json",
                     "User-Agent": "NyaaTriggers"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                code = resp.getcode()
                # Cap the body. A buggy/hostile loopback peer must not be able to
                # OOM the app. 1 MiB dwarfs any real GetPartyMembers response.
                body = resp.read(1 << 20)
            self._report_reachable(True, f"Connected (HTTP {code})")
            if msg.get("id") == PARTY_UPDATE_ID:
                self._update_party_slots(body)
        except urllib.error.HTTPError as exc:
            # Reached the endpoint but got non-2xx. Reachable, yet every command
            # is failing. Report degraded, amber, not healthy green. Not a
            # reachability flip. A transient per-command error must not read as
            # "Telesto gone".
            # The error is a response object, close it before moving on.
            exc.close()
            self._report_reachable(True, f"Telesto error: HTTP {exc.code}", degraded=True)
            log_drop("telesto-http", f"HTTP {exc.code} for {msg.get('type')}")
        except (urllib.error.URLError, OSError, ValueError,
                http.client.HTTPException) as exc:
            # Connection refused, timeout, bad URI, or a peer that is not HTTP
            # at all. Telesto absent, not fatal.
            self._report_reachable(False, f"Telesto unreachable: {exc}")
            log_drop("telesto-http", f"unreachable: {exc}")

    def _update_party_slots(self, body: bytes) -> None:
        """Rebuild the actor-id -> slot map from a GetPartyMembers response.

        Body looks like {"id":..,"response":[{"order":<hex>,"actor":<hex objectId>}]}.
        Sort by `order`. The 1-based position is FFXIV's <N> party-list slot.
        Never raises. A malformed body keeps the previous map intact."""
        try:
            data = json.loads(body.decode("utf-8", "replace"))
        except (ValueError, AttributeError):
            return
        members = data.get("response") if isinstance(data, dict) else data
        if not isinstance(members, list):
            return                              # parse/shape failure, keep the good map
        if not members:
            # Well-formed empty party, disbanded or solo. Clear the map so
            # mark_actor fails closed instead of resolving a defunct slot.
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
        # Entries with no parseable actor are compressed out, no gap, matching
        # the game's gapless party list.
        slot = 0
        for entry in ordered:
            aid = _actor_int(entry.get("actor"))
            if aid is None:
                continue
            slot += 1
            if slot > 8:
                break
            slots[aid] = slot
        # Authoritative, well-formed response. Install even when nothing parsed,
        # so mark_actor fails closed instead of resolving a slot from a stale
        # map. Marking the wrong player is worse than not marking.
        with self._lock:
            self._slot_by_actor = slots

    def _report_reachable(self, reachable: bool, message: str, degraded: bool = False) -> None:
        # Emit only on a transition so we don't spam the UI per command. The
        # degraded flag is part of the state. Reachable-but-erroring must
        # re-emit even though the boolean didn't move.
        state = (reachable, degraded)
        with self._lock:
            changed = self._reachable != state
            self._reachable = state
        if changed:
            self.status_changed.emit(reachable, message, degraded)
