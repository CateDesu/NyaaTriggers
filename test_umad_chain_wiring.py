"""Tests for the MainWindow wiring of the black-hole chain job backfill.

The debounced chain flush fails closed while roles are unknown, so when the
PartyChanged or getCombatants job feeds finally land they must re-arm that
flush. A flush right after the backfill emits the role marks, and a cold
engine re-arms nothing.

Drives the real handlers unbound on a duck-typed window (no QApplication),
the way test_umad_gaze_wiring.py drives the gaze wiring.

Run directly:  python test_umad_chain_wiring.py   (exit 0 = all pass)
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import main_window as mw
from umad_chains import ACCRETION, CRUST, BlackHoleChains, role_for_job

FAILS = []


def check(name, cond):
    print(("PASS  " if cond else "FAIL  ") + name)
    if not cond:
        FAILS.append(name)


# The cast mirrors test_umad_chains.py, D4 and H2 carry Accretion.
JOBS = {
    "10000001": 34,   # D1 SAM
    "10000002": 38,   # D2 DNC
    "10000003": 42,   # D3 PCT
    "10000004": 41,   # D4 VPR   (Accretion)
    "10000011": 19,   # T1 PLD
    "10000012": 21,   # T2 WAR
    "10000021": 24,   # H1 WHM
    "10000022": 40,   # H2 SGE   (Accretion)
}
D1, D2, D3, D4 = "10000001", "10000002", "10000003", "10000004"
T1, T2, H1, H2 = "10000011", "10000012", "10000021", "10000022"
ORDERS = [(D1, "BBC"), (D2, "BBD"), (D3, "BBE"),
          (T1, "BBC"), (T2, "BBD"), (H1, "BBE"),
          (D4, "BBC"), (H2, "BBD")]


class FakeTimer:
    def __init__(self):
        self.armed = 0

    def start(self):
        self.armed += 1


class FakeWindow:
    """The chain half of MainWindow with fake leaf collaborators. Jobs start
    empty, the mid-instance restart the backfill exists for."""

    _norm_hex = staticmethod(mw.MainWindow._norm_hex)
    _umad_chain_line = mw.MainWindow._umad_chain_line
    _dispatch_umad_chain_actions = mw.MainWindow._dispatch_umad_chain_actions
    _dispatch_mark_actions = mw.MainWindow._dispatch_mark_actions
    _umad_name_of = mw.MainWindow._umad_name_of
    _note_actor_job = mw.MainWindow._note_actor_job
    _rearm_umad_chain_flush = mw.MainWindow._rearm_umad_chain_flush
    _on_umad_chain_flush = mw.MainWindow._on_umad_chain_flush
    _retry_umad_chain_pending = mw.MainWindow._retry_umad_chain_pending
    _on_ws_party_jobs = mw.MainWindow._on_ws_party_jobs
    _on_ws_combatants_jobs = mw.MainWindow._on_ws_combatants_jobs

    def __init__(self):
        self._settings = {"telesto_enabled": True}
        self._current_fight_tag = "UMAD"
        self._umad_chain_enabled = True
        self._actor_jobs = {}
        self._umad_actor_names = {}
        self._umad_chains = BlackHoleChains(
            role_of=lambda aid: role_for_job(self._actor_jobs.get(mw._actor_int(aid))))
        self._umad_chain_pending = []
        self._umad_chain_flush_timer = FakeTimer()
        self._telesto_client = object()
        self._automark_active = {}
        self.marks = []
        self.clears = []

    def _mark_player(self, actor, marker, name="", is_me=False):
        self.marks.append((actor, marker))
        return True

    def _clear_player(self, actor, name=""):
        self.clears.append(actor)
        return True

    def _is_me_actor(self, tid, name=""):
        return False

    def feed(self, ltype, eff, tgt):
        fields = [ltype, "ts", eff, "n", "10", "src", "srcn", tgt, "tgtn"]
        self._umad_chain_line(fields)

    def feed_assignment(self):
        self.feed("26", ACCRETION, D4)
        self.feed("26", ACCRETION, H2)
        for actor, line in ORDERS:
            self.feed("26", line, actor)
            self.feed("26", CRUST, actor)


def markmap(w):
    return {a: m for a, m in w.marks}


# ── roles unknown at the burst: only the Accretion queue marks ──
w = FakeWindow()
w.feed_assignment()
check("unknown roles: the fast path marks only the Accretion head",
      w.marks == [(D4, "attack3")])
w._on_umad_chain_flush()
check("unknown roles: the debounce flush stays closed", len(w.marks) == 1)

# ── the PartyChanged backfill lands late: it re-arms the flush ──
before = w._umad_chain_flush_timer.armed
w._on_ws_party_jobs({int(a, 16): j for a, j in JOBS.items()})
check("party jobs re-arm the flush while queues are open",
      w._umad_chain_flush_timer.armed == before + 1)
w._on_umad_chain_flush()
mm = markmap(w)
check("flush after the backfill marks the role heads",
      mm.get(D1) == "attack1" and mm.get(T1) == "attack2")

# ── the getCombatants backfill re-arms it the same way ──
w = FakeWindow()
w.feed_assignment()
w._on_umad_chain_flush()
check("combatants setup: only the Accretion head is marked", len(w.marks) == 1)
before = w._umad_chain_flush_timer.armed
w._on_ws_combatants_jobs(
    {"list": [{"id": int(a, 16), "job": j} for a, j in JOBS.items()]})
check("combatants jobs re-arm the flush while queues are open",
      w._umad_chain_flush_timer.armed == before + 1)
w._on_umad_chain_flush()
check("flush after the combatants backfill marks the role heads",
      len(w.marks) == 3)

# ── a cold engine re-arms nothing ──
w = FakeWindow()
before = w._umad_chain_flush_timer.armed
w._on_ws_party_jobs({int(D1, 16): 34})
w._on_ws_combatants_jobs({"list": [{"id": int(D2, 16), "job": 38}]})
check("no live instance: job feeds leave the flush timer alone",
      w._umad_chain_flush_timer.armed == before)
w._on_umad_chain_flush()
check("flushing a cold engine is a no-op", w.marks == [])

print()
if FAILS:
    print(f"{len(FAILS)} FAILED: {FAILS}")
    sys.exit(1)
print("all tests passed")
