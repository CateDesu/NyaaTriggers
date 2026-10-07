import os
import sys
import types
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from nyaatriggers import main_window as mw
from nyaatriggers.umad_chains import (
    AWAY1, AWAY2, LOOK1, LOOK2, CURSED_SHRIEK, DEFAULT_GAZE_MARKERS,
    CursedShriekPairs, StatusPairs, GAZE_VFX_STATUS, FAKE_GAZE_VFX, REAL_GAZE_VFX,
)

FAILS = []


def check(name, cond):
    print(("PASS  " if cond else "FAIL  ") + name)
    if not cond:
        FAILS.append(name)


A, B, C, D = "10000001", "10000002", "10000003", "10000004"
IGN1, IGN2 = DEFAULT_GAZE_MARKERS[AWAY1], DEFAULT_GAZE_MARKERS[AWAY2]
BND1, BND2 = DEFAULT_GAZE_MARKERS[LOOK1], DEFAULT_GAZE_MARKERS[LOOK2]
FAKE, REAL = FAKE_GAZE_VFX, REAL_GAZE_VFX


class FakeWindow:
    _norm_hex = staticmethod(mw.MainWindow._norm_hex)
    _umad_gaze_line = mw.MainWindow._umad_gaze_line
    _umad_gaze_cast = mw.MainWindow._umad_gaze_cast
    _umad_gaze_reset = mw.MainWindow._umad_gaze_reset
    _dispatch_umad_gaze_actions = mw.MainWindow._dispatch_umad_gaze_actions
    _dispatch_mark_actions = mw.MainWindow._dispatch_mark_actions
    _umad_name_of = mw.MainWindow._umad_name_of
    _retry_umad_gaze_pending = mw.MainWindow._retry_umad_gaze_pending
    _on_umad_gaze_flush = mw.MainWindow._on_umad_gaze_flush
    _match_automark_rules = mw.MainWindow._match_automark_rules

    def __init__(self, gaze_on=True, fight="UMAD", telesto=True, slots=None,
                 mark_ok=True, rules=None):
        self._settings = {"telesto_enabled": telesto}
        self.now = 1000.0
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
        with patch("nyaatriggers.ui.automarkers_tab.time.monotonic", return_value=self.now):
            self._umad_gaze_line(fields)

    def cast(self, eff, src="4000722B"):
        fields = ["20", "ts", src, "Chaos", eff, "Inferno"]
        self._umad_gaze_cast(fields)

    def vfx(self, value, target="4000722B", ltype="26"):
        self.now += 15.0
        fields = [ltype, str(self.now), GAZE_VFX_STATUS, "VFX", "9999", "E0000000", "", target, "Neo Exdeath", value]
        with patch("nyaatriggers.ui.automarkers_tab.time.monotonic", return_value=self.now):
            self._umad_gaze_line(fields)

    def gaze(self, order):
        for actor, dur in order:
            self.feed("26", CURSED_SHRIEK, actor, dur=dur)


def markmap(w):
    return {a: m for a, m in w.marks}


w = FakeWindow(slots={A: 1, B: 2, C: 3, D: 4})
w.vfx(FAKE)
w.gaze([(A, "60.00"), (B, "60.00")])
w.vfx(REAL)
w.gaze([(C, "69.00"), (D, "69.00")])
mm = markmap(w)
check("host marks all four gaze carriers", len(w.marks) == 4)
check("fake wave gets the bind signs (by slot)",
      mm[A] == BND1 and mm[B] == BND2)
check("real wave gets the ignore signs (by slot)",
      mm[C] == IGN1 and mm[D] == IGN2)
w.feed("26", CURSED_SHRIEK, A, dur="60.00")
check("duplicate gaze gain after both pairs sends no clears", w.clears == [])

for cast in ("BB1E", "BB1F", "BB20", "BB21"):
    w = FakeWindow()
    w.cast(cast)
    w.gaze([(A, "60.00"), (B, "60.00")])
    check(f"{cast} alone cannot select gaze signs", w.marks == [])

w = FakeWindow()
w.gaze([(A, "60.00"), (B, "60.00"), (C, "69.00"), (D, "69.00")])
check("gains without a VFX tell mark nothing", w.marks == [])

w = FakeWindow(gaze_on=False)
w.vfx(FAKE)
w.gaze([(A, "60.00"), (B, "60.00")])
check("toggle off marks nothing", w.marks == [])

w = FakeWindow(fight="FRU")
w.vfx(FAKE)
w.gaze([(A, "60.00"), (B, "60.00")])
check("a different known fight marks nothing", w.marks == [])

w = FakeWindow(telesto=False)
w.vfx(FAKE)
w.gaze([(A, "60.00"), (B, "60.00")])
check("Telesto disabled marks nothing", w.marks == [])

w = FakeWindow(fight="")
w.vfx(FAKE)
w.gaze([(A, "60.00"), (B, "60.00")])
check("unknown fight (started mid-instance) still marks",
      markmap(w) == {A: BND1, B: BND2})

w = FakeWindow()
w.feed("26", "BA94", A)
w.cast("BA94")
w.gaze([(A, "60.00"), (B, "60.00")])
check("unrelated cast ids arm nothing, the set fails closed", w.marks == [])

w = FakeWindow()
w.vfx(FAKE)
w.gaze([(A, "bad"), (B, "60.00")])
check("an unparseable duration still marks, the tell is the VFX",
      markmap(w) == {A: BND1, B: BND2})

w = FakeWindow(mark_ok=False)
w.vfx(FAKE)
w.gaze([(A, "60.00"), (B, "60.00")])
check("marks that can't send yet are held pending", w.marks == []
      and len(w._umad_gaze_pending) == 2)
w._mark_ok = True
with patch("nyaatriggers.ui.automarkers_tab.time.monotonic", return_value=w.now):
    w._retry_umad_gaze_pending()
check("the party-refresh retry sends the held gaze marks", len(w.marks) == 2)

w = FakeWindow()
w.vfx(FAKE)
w.gaze([(A, "60.00"), (B, "60.00")])
w.feed("30", CURSED_SHRIEK, A)
check("losing the gaze clears that player's sign", w.clears == [A])

w = FakeWindow()
w.vfx(FAKE)
w.gaze([(A, "60.00"), (B, "60.00")])
w.vfx(REAL)
w.gaze([(C, "69.00"), (D, "69.00")])
w._umad_gaze_reset(clear_marks=True)
check("wipe/abort clears all four outstanding signs", sorted(w.clears) == [A, B, C, D])

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

for value, target, ltype in (("45F", "40000001", "26"),
                             ("460", "40000001", "26"),
                             (REAL, A, "26"), (REAL, "40000001", "30"),
                             ("bad", "40000001", "26")):
    w = FakeWindow()
    w.vfx(value, target, ltype)
    w.gaze([(A, "60"), (B, "60")])
    check("unrelated VFX and status removals cannot arm gaze signs", not w.marks)

w = FakeWindow()
w.vfx(REAL)
w.gaze([(A, "60"), (B, "60")])
w.vfx(REAL)
w.gaze([(C, "69"), (D, "69")])
w.cast("C2DC")
check("Kefka Says clears active signs and forgets the waiting pair",
      w.clears == [A, B] and not w._umad_gaze.needs_flush())
w.vfx(FAKE)
w.gaze([(A, "60"), (B, "60")])
check("the next phase accepts fresh fake gaze evidence",
      w.marks[-2:] == [(A, BND1), (B, BND2)])

w = FakeWindow(mark_ok=False)
w.vfx(REAL)
w.gaze([(A, "60"), (B, "60")])
w.vfx(REAL)
w.gaze([(C, "69"), (D, "69")])
w._mark_ok = True
w.now += 46
with patch("nyaatriggers.ui.automarkers_tab.time.monotonic", return_value=w.now):
    w._on_umad_gaze_flush()
check("expiry cancels unsent first pair marks before retrying the later pair",
      w.marks == [(C, IGN1), (D, IGN2)] and not w._umad_gaze_pending)

print()
if FAILS:
    print(f"{len(FAILS)} FAILED: {FAILS}")
    sys.exit(1)
print("all tests passed")
