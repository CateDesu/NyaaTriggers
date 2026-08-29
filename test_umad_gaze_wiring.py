"""Tests for the MainWindow wiring of the Cursed Shriek gaze engine.

Drives the real _umad_gaze_line / _umad_gaze_cast / dispatch / retry and the
_match_automark_rules suspend guard unbound on a duck-typed window (no
QApplication), the way test_automark_rules.py drives the compound matcher.
Confirms the host routes the followup casts and the 26/30 gaze lines, gates
correctly, marks through the shared transport, retries slot-unknown marks, and
suppresses the plain 15A7 rule while the gaze toggle owns it.

Run directly:  python test_umad_gaze_wiring.py   (exit 0 = all pass)
"""
import os
import sys
import types

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import main_window as mw
from umad_chains import (
    AWAY1, AWAY2, LOOK1, LOOK2, CURSED_SHRIEK, DEFAULT_GAZE_MARKERS,
    CursedShriekPairs, StatusPairs,
)

FAILS = []


def check(name, cond):
    print(("PASS  " if cond else "FAIL  ") + name)
    if not cond:
        FAILS.append(name)


A, B, C, D = "10000001", "10000002", "10000003", "10000004"
IGN1, IGN2 = DEFAULT_GAZE_MARKERS[AWAY1], DEFAULT_GAZE_MARKERS[AWAY2]
BND1, BND2 = DEFAULT_GAZE_MARKERS[LOOK1], DEFAULT_GAZE_MARKERS[LOOK2]
INFERNO, TSUNAMI = "BB1E", "BB1F"


class FakeWindow:
    """Composes the real gaze wiring with fake leaf collaborators."""

    _norm_hex = staticmethod(mw.MainWindow._norm_hex)
    _umad_gaze_line = mw.MainWindow._umad_gaze_line
    _umad_gaze_cast = mw.MainWindow._umad_gaze_cast
    _umad_gaze_reset = mw.MainWindow._umad_gaze_reset
    _dispatch_umad_gaze_actions = mw.MainWindow._dispatch_umad_gaze_actions
    _dispatch_mark_actions = mw.MainWindow._dispatch_mark_actions
    _umad_name_of = mw.MainWindow._umad_name_of
    _retry_umad_gaze_pending = mw.MainWindow._retry_umad_gaze_pending
    _match_automark_rules = mw.MainWindow._match_automark_rules

    def __init__(self, gaze_on=True, fight="UMAD", telesto=True, slots=None,
                 mark_ok=True, rules=None):
        self._settings = {"telesto_enabled": telesto}
        self._current_fight_tag = fight
        self._umad_gaze_enabled = gaze_on
        self._umad_chain_enabled = False
        self._umad_actor_names = {}
        self._umad_gaze_pending = []
        self.slots = slots or {}
        self._umad_gaze = CursedShriekPairs(slot_of=lambda a: self.slots.get(a))
        self._umad_gaze_flush_timer = types.SimpleNamespace(start=lambda: None)
        self._telesto_client = object()
        self._mark_ok = mark_ok
        self.marks = []
        self.clears = []
        self._automark_rules = rules if rules is not None else []
        self._automark_cooldowns = {}
        self._automark_active = {}
        self._automark_pairs = StatusPairs([])

    def _mark_player(self, actor, marker, name="", is_me=False):
        if not self._mark_ok:
            return False
        self.marks.append((actor, marker))
        return True

    def _clear_player(self, actor, name="", force=False):
        self.clears.append(actor)
        return True

    def _is_me_actor(self, tid, name=""):
        return False

    def feed(self, ltype, eff, tgt, dur="20.00", name="n"):
        fields = [ltype, "ts", eff, name, dur, "src", "srcn", tgt, "tgtn"]
        self._umad_gaze_line(fields)

    def cast(self, eff, src="4000722B"):
        fields = ["20", "ts", src, "Chaos", eff, "Inferno"]
        self._umad_gaze_cast(fields)

    def gaze(self, order):
        for actor, dur in order:
            self.feed("26", CURSED_SHRIEK, actor, dur=dur)


def markmap(w):
    return {a: m for a, m in w.marks}


# ── happy path: the labeled pull shape, Inferno fake then Tsunami real ──
w = FakeWindow(slots={A: 1, B: 2, C: 3, D: 4})
w.cast(INFERNO)
w.gaze([(A, "60.00"), (B, "60.00")])
w.cast(TSUNAMI)
w.gaze([(C, "69.00"), (D, "69.00")])
mm = markmap(w)
check("host marks all four gaze carriers", len(w.marks) == 4)
check("inferno wave gets the bind signs (by slot)",
      mm[A] == BND1 and mm[B] == BND2)
check("tsunami wave gets the ignore signs (by slot)",
      mm[C] == IGN1 and mm[D] == IGN2)

# ── the second cast id of each element routes too ──
w = FakeWindow()
w.cast("BB20")
w.gaze([(A, "60.00"), (B, "60.00")])
check("BB20 Inferno arms the fake kind",
      markmap(w) == {A: BND1, B: BND2})
w = FakeWindow()
w.cast("BB21")
w.gaze([(A, "60.00"), (B, "60.00")])
check("BB21 Tsunami arms the real kind",
      markmap(w) == {A: IGN1, B: IGN2})

# ── no followup cast, no marks ──
w = FakeWindow()
w.gaze([(A, "60.00"), (B, "60.00"), (C, "69.00"), (D, "69.00")])
check("gains without a followup tell mark nothing", w.marks == [])

# ── gating ──
w = FakeWindow(gaze_on=False)
w.cast(INFERNO)
w.gaze([(A, "60.00"), (B, "60.00")])
check("toggle off marks nothing", w.marks == [])

w = FakeWindow(fight="FRU")
w.cast(INFERNO)
w.gaze([(A, "60.00"), (B, "60.00")])
check("a different known fight marks nothing", w.marks == [])

w = FakeWindow(telesto=False)
w.cast(INFERNO)
w.gaze([(A, "60.00"), (B, "60.00")])
check("Telesto disabled marks nothing", w.marks == [])

w = FakeWindow(fight="")
w.cast(INFERNO)
w.gaze([(A, "60.00"), (B, "60.00")])
check("unknown fight (started mid-instance) still marks",
      markmap(w) == {A: BND1, B: BND2})

w = FakeWindow()
w.feed("26", "BA94", A)          # a non-gaze status id is not routed
w.cast("BA94")                    # Mystery Magic is not a followup id
w.gaze([(A, "60.00"), (B, "60.00")])
check("unrelated cast ids arm nothing, the set fails closed", w.marks == [])

# ── a non-numeric duration field is not load-bearing anymore ──
w = FakeWindow()
w.cast(INFERNO)
w.gaze([(A, "bad"), (B, "60.00")])
check("an unparseable duration still marks, the tell is the cast",
      markmap(w) == {A: BND1, B: BND2})

# ── slot-unknown marks are queued and retried, not lost ──
w = FakeWindow(mark_ok=False)
w.cast(INFERNO)
w.gaze([(A, "60.00"), (B, "60.00")])
check("marks that can't send yet are held pending", w.marks == []
      and len(w._umad_gaze_pending) == 2)
w._mark_ok = True
w._retry_umad_gaze_pending()
check("the party-refresh retry sends the held gaze marks", len(w.marks) == 2)

# ── a loss clears the sign through the transport ──
w = FakeWindow()
w.cast(INFERNO)
w.gaze([(A, "60.00"), (B, "60.00")])
w.feed("30", CURSED_SHRIEK, A)
check("losing the gaze clears that player's sign", w.clears == [A])

# ── wipe reset clears outstanding signs ──
w = FakeWindow()
w.cast(INFERNO)
w.gaze([(A, "60.00"), (B, "60.00")])
w.cast(TSUNAMI)
w.gaze([(C, "69.00"), (D, "69.00")])
w._umad_gaze_reset(clear_marks=True)
check("wipe/abort clears all four outstanding signs", sorted(w.clears) == [A, B, C, D])

# ── the plain 15A7 rule is suspended while the gaze toggle is on ──
rule15a7 = [{"fight": "UMAD", "status": "15A7", "marker": "circle",
             "scope": "party", "enabled": True}]
w = FakeWindow(gaze_on=True, rules=rule15a7)
w._match_automark_rules(["26", "ts", "15A7", "Cursed Shriek", "20.00",
                         "src", "srcn", A, "tgtn"])
check("gaze on: the plain 15A7 rule does not fire", w.marks == [])

w = FakeWindow(gaze_on=False, rules=rule15a7)
w._match_automark_rules(["26", "ts", "15A7", "Cursed Shriek", "20.00",
                         "src", "srcn", A, "tgtn"])
check("gaze off: the plain 15A7 rule fires as before", w.marks == [(A, "circle")])

print()
if FAILS:
    print(f"{len(FAILS)} FAILED: {FAILS}")
    sys.exit(1)
print("all tests passed")
