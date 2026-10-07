import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from nyaatriggers.umad_chains import (
    AWAY1, AWAY2, LOOK1, LOOK2, BURST_GAP_S, CURSED_SHRIEK, DEFAULT_GAZE_MARKERS,
    FAKE_GAZE_VFX, GAZE_IDS, REAL_GAZE_VFX, STALE_S,
    CursedShriekPairs,
)

FAILS = []


def check(name, cond):
    print(("PASS  " if cond else "FAIL  ") + name)
    if not cond:
        FAILS.append(name)


# First and second wave durations do not identify gaze polarity.
A, B, C, D = "10000001", "10000002", "10000003", "10000004"
SET1, SET2 = 60.0, 69.0
FAKE, REAL = FAKE_GAZE_VFX, REAL_GAZE_VFX
IGN1, IGN2 = DEFAULT_GAZE_MARKERS[AWAY1], DEFAULT_GAZE_MARKERS[AWAY2]
BND1, BND2 = DEFAULT_GAZE_MARKERS[LOOK1], DEFAULT_GAZE_MARKERS[LOOK2]


def eng(**kw):
    return CursedShriekPairs(**kw)


def gain(e, actor, dur, t):
    return e.on_gain(CURSED_SHRIEK, actor, dur, t)


def wave(e, actors, dur, t, vfx=None, vfx_time=None):
    acts = []
    if vfx is not None:
        acts += e.on_vfx(vfx, t if vfx_time is None else vfx_time)
    for a in actors:
        acts += gain(e, a, dur, t)
    return acts


def marks(acts):
    return {a[1]: a[2] for a in acts if a[0] == "mark"}


e = eng()
m = marks(wave(e, [A, B], SET1, 10.0, vfx=FAKE, vfx_time=6.0)
          + wave(e, [C, D], SET2, 25.0, vfx=REAL, vfx_time=21.0))
check("fake wave marks its pair with the look-at binds",
      m[A] == BND1 and m[B] == BND2)
check("real wave marks its pair with the look-away ignores",
      m[C] == IGN1 and m[D] == IGN2)
check("all four signs are outstanding together",
      set(e.outstanding()) == {A, B, C, D})

e = eng()
m = marks(wave(e, [A, B], SET1, 10.0, vfx=REAL, vfx_time=6.0)
          + wave(e, [C, D], SET2, 25.0, vfx=FAKE, vfx_time=21.0))
check("swapped pulls mark the other way, real VFX first",
      m[A] == IGN1 and m[B] == IGN2 and m[C] == BND1 and m[D] == BND2)

for cast in ("BB1E", "BB1F", "BB20", "BB21"):
    check(f"{cast} cannot arm gaze signs", eng().on_vfx(cast, 1.0) == [])

e = eng()
half = e.on_vfx(FAKE, 6.0) + gain(e, A, SET1, 10.0) + gain(e, A, SET1, 10.0)
check("one gain and a duplicate mark nothing", half == [])
last = gain(e, B, SET1, 10.0)
check("the partner gain completes the assignment", marks(last) == {A: BND1, B: BND2})

e = eng()
check("a pair whose wave's vfx never arrived marks nothing",
      wave(e, [A, B], SET1, 10.0) == [])
check("the unarmed pair waits within the burst window without assigning signs",
      e._set == [A, B] and e._sets_done == 0)
check("a missing tell expires the unmarked pair",
      e.flush(10.0 + BURST_GAP_S + 0.1) == [] and e._set == [])
m = marks(wave(e, [C, D], SET2, 25.0, vfx=REAL, vfx_time=21.0))
check("the next armed wave still marks", m == {C: IGN1, D: IGN2})

e = eng()
wave(e, [B, A], SET1, 10.0)
check("a reordered tell completes the waiting pair with exact signs",
      e.on_vfx(REAL, 10.1) == [("mark", A, IGN1), ("mark", B, IGN2)])
check("duplicate gains after a reordered tell cannot repeat marks",
      gain(e, A, SET1, 10.2) == [] and gain(e, B, SET1, 10.2) == [])

e = eng()
wave(e, [A, B], SET1, 10.0)
check("a tell arriving after the burst cannot mark the old pair",
      e.on_vfx(REAL, 10.0 + BURST_GAP_S + 0.1) == [] and e._assigned == {})

for sweep in (False, True):
    e = eng()
    e.on_vfx(FAKE, 1.0)
    if sweep:
        e.flush(STALE_S + 2)
    check("an old tell without any gains cannot mark a later pair",
          wave(e, [A, B], SET1, STALE_S * 2) == [])

e = eng()
e.on_vfx(FAKE, 1.0)
for t in (30.0, 60.0, 90.0):
    e.flush(t)
check("intervening flushes cannot keep an old tell usable",
      wave(e, [A, B], SET1, 120.0) == [])
check("a fresh tell after expiry still marks the correct kind",
      marks(wave(e, [C, D], SET2, 130.0, vfx=REAL, vfx_time=126.0))
      == {C: IGN1, D: IGN2})

slots = {A: 2, B: 1, C: 4, D: 3}
e = eng(slot_of=lambda a: slots.get(a))
m = marks(wave(e, [A, B], SET1, 10.0, vfx=FAKE, vfx_time=6.0)
          + wave(e, [C, D], SET2, 25.0, vfx=FAKE, vfx_time=21.0))
check("bind 1 goes to the lower party slot (B), not the lower id",
      m[B] == BND1 and m[A] == BND2)
check("the later pair cannot steal the first pair signs",
      C not in m and D not in m)
m = marks(e.on_loss(CURSED_SHRIEK, A, 70.0) + e.on_loss(CURSED_SHRIEK, B, 70.0))
check("the second pair inherits the signs in party order",
      m == {D: BND1, C: BND2})

e = eng(slot_of=lambda a: {A: 5}.get(a))
m = marks(wave(e, [A, B], SET1, 10.0, vfx=REAL, vfx_time=6.0))
check("a slot-known player sorts ahead of a slot-unknown partner",
      m[A] == IGN1 and m[B] == IGN2)

e = eng()
acts = e.on_vfx(FAKE, 6.0) + gain(e, A, SET1, 10.0)
check("a lone first gain marks nothing", acts == [])
lost = e.on_loss(CURSED_SHRIEK, A, 11.0)
check("the lone carrier losing it clears quietly", lost == [])
m = marks(wave(e, [B, C], SET1, 30.0, vfx=FAKE, vfx_time=26.0))
check("a fresh wave after the discard still marks", m == {B: BND1, C: BND2})

e = eng()
e.on_vfx(FAKE, 6.0)
gain(e, A, SET1, 10.0)
check("flush inside the burst window keeps the open set",
      e.flush(11.0) == [] and e._set == [A])
e.flush(10.0 + BURST_GAP_S + 1)
check("flush after the burst gap discards the orphaned set",
      e._set == [] and e._polarity is None)

e = eng()
e.on_vfx(FAKE, 6.0)
gain(e, A, SET1, 10.0)
e.on_vfx(REAL, 21.0)
acts = gain(e, C, SET2, 25.0) + gain(e, D, SET2, 25.1)
check("a wave 2 tell survives the wave 1 orphan discard",
      marks(acts) == {C: IGN1, D: IGN2})

e = eng()
e.on_vfx(FAKE, 6.0)
gain(e, A, SET1, 10.0)
e.on_vfx(REAL, 21.0)
loss = e.on_loss(CURSED_SHRIEK, A, 22.0)
acts = gain(e, C, SET2, 25.0) + gain(e, D, SET2, 25.1)
check("a late orphan loss clears nothing and keeps the armed tell",
      loss == [] and marks(acts) == {C: IGN1, D: IGN2})

e = eng()
e.on_vfx(FAKE, 6.0)
gain(e, A, SET1, 10.0)
acts = gain(e, C, SET2, 25.0) + gain(e, D, SET2, 25.1)
check("an orphaned wave's own tell dies with it, no bleed",
      marks(acts) == {})

e = eng()
wave(e, [A, B], SET1, 10.0, vfx=FAKE, vfx_time=6.0)
acts = gain(e, C, SET1, 11.0) + gain(e, D, SET1, 11.1)
check("a stray pair with no armed tell marks nothing and clears nothing",
      acts == [] and set(e.outstanding()) == {A, B})
m = marks(wave(e, [C, D], SET2, 25.0, vfx=REAL, vfx_time=21.0))
check("the real wave after the strays re-arms and assigns",
      m == {C: IGN1, D: IGN2})

e = eng()
wave(e, [A, B], SET1, 10.0, vfx=FAKE, vfx_time=6.0)
loss = e.on_loss(CURSED_SHRIEK, A, 20.0)
check("losing the gaze clears that player's sign", loss == [("clear", A)])
check("cleared player drops out of outstanding", set(e.outstanding()) == {B})
check("a second loss for the same player is a no-op",
      e.on_loss(CURSED_SHRIEK, A, 20.1) == [])

e = eng()
wave(e, [A, B], SET1, 10.0, vfx=FAKE, vfx_time=6.0)
refire = gain(e, A, SET1, 11.0)
check("a re-gain after assignment does not re-fire or wipe marks",
      refire == [] and set(e.outstanding()) == {A, B})

e = eng()
wave(e, [A, B], SET1, 10.0, vfx=FAKE, vfx_time=6.0)
wave(e, [C, D], SET2, 25.0, vfx=REAL, vfx_time=21.0)
check("second pair duplicate keeps all four gaze signs",
      gain(e, A, SET1, 25.1) == [] and set(e.outstanding()) == {A, B, C, D})
check("the other pair's duplicate also leaves signs in place",
      gain(e, D, SET2, 25.2) == [] and set(e.outstanding()) == {A, B, C, D})

late = eng()
wave(late, [A, B], SET1, 10.0, vfx=FAKE, vfx_time=6.0)
wave(late, [C, D], SET2, 25.0, vfx=REAL, vfx_time=21.0)
check("a later gain before status expiry still keeps the signs",
      gain(late, C, SET2, 31.0) == [] and set(late.outstanding()) == {A, B, C, D})
check("late gains clear expired signs without rearming the phase",
      gain(late, C, SET2, 95.0) == [("clear", a) for a in (A, B, C, D)]
      and late.outstanding() == [])

e.on_vfx(FAKE, 36.0)
acts = gain(e, A, SET1, 36.1) + gain(e, B, SET1, 36.2)
check("the third Grand Cross cannot rearm existing gaze carriers",
      acts == [] and set(e.outstanding()) == {A, B, C, D})

e = eng()
wave(e, [A, B], SET1, 10.0, vfx=FAKE, vfx_time=6.0)
wave(e, [C, D], SET2, 25.0, vfx=REAL, vfx_time=21.0)
for who in (A, B, C, D):
    e.on_loss(CURSED_SHRIEK, who, 45.0)
check("field is clear after every gaze resolves", e.outstanding() == [])
e.reset()
m = marks(wave(e, [A, B], SET1, 100.0, vfx=REAL, vfx_time=96.0)
          + wave(e, [C, D], SET2, 115.0, vfx=FAKE, vfx_time=111.0))
check("the next phase after full resolution assigns again",
      m == {A: IGN1, B: IGN2, C: BND1, D: BND2})

e = eng()
wave(e, [A, B], SET1, 10.0, vfx=FAKE, vfx_time=6.0)
wave(e, [C, D], SET2, 25.0, vfx=REAL, vfx_time=21.0)
e.reset()
acts = wave(e, [A, B], SET1, 100.0, vfx=REAL, vfx_time=96.0)
check("a reset phase has no stale signs to clear",
      [a for a in acts if a[0] == "clear"] == [])
m = marks(acts)
check("the fresh deal assigns off the new tell, kept across the reset",
      m == {A: IGN1, B: IGN2})

e = eng()
wave(e, [A, B], SET1, 10.0, vfx=FAKE, vfx_time=6.0)
wave(e, [C, D], SET2, 25.0, vfx=REAL, vfx_time=21.0)
acts = gain(e, A, SET1, 100.0) + gain(e, B, SET1, 100.1)
check("a glued deal with no fresh tell clears the signs and marks nothing",
      marks(acts) == {}
      and [a for a in acts if a[0] == "clear"] == [("clear", a) for a in (A, B, C, D)])

e = eng()
wave(e, [A, B], SET1, 10.0, vfx=FAKE, vfx_time=6.0)
late = wave(e, [C, D], SET2, 10.0 + STALE_S + 5, vfx=REAL,
            vfx_time=10.0 + STALE_S + 1)
check("a stale phase's signs come down before the new wave assigns",
      [a for a in late if a[0] == "clear"] == [("clear", A), ("clear", B)]
      and marks(late) == {C: IGN1, D: IGN2})

e = eng()
wave(e, [A, B], SET1, 10.0, vfx=FAKE, vfx_time=6.0)
acts = e.on_vfx(REAL, 10.0 + STALE_S + 5)
check("a stale vfx clears the dead signs and arms the new phase",
      acts == [("clear", A), ("clear", B)]
      and e._sets_done == 0 and e._polarity == "away1")

check("a non-gaze status id is ignored",
      eng().on_gain("644", A, 20.0, 10.0) == [])
check("an unrelated VFX id is ignored",
      eng().on_vfx("BA94", 10.0) == [])
check("default gaze id set is just Cursed Shriek",
      GAZE_IDS == frozenset({CURSED_SHRIEK}))

e = eng()
e.set_markers({AWAY1: "circle", AWAY2: "square", LOOK1: "cross", LOOK2: "triangle"})
m = marks(wave(e, [A, B], SET1, 10.0, vfx=FAKE, vfx_time=6.0)
          + wave(e, [C, D], SET2, 25.0, vfx=REAL, vfx_time=21.0))
check("set_markers swaps the signs used",
      m[A] == "cross" and m[B] == "triangle"
      and m[C] == "circle" and m[D] == "square")

e = eng()
wave(e, [A, B], SET1, 10.0, vfx=FAKE, vfx_time=6.0)
e.reset()
check("reset clears everything",
      e.outstanding() == [] and e._sets_done == 0)

e = eng()
e.on_vfx(REAL, 1.0, event_id="wave1")
wave(e, [A, B], SET1, 10.0)
e.on_vfx(REAL, 10.1, event_id="wave1")
check("a repeated VFX does not rearm the consumed wave",
      wave(e, [C, D], SET2, 10.2) == [])

e = eng()
e.on_vfx(REAL, 1.0, event_id="wave1")
first = wave(e, [A, B], SET1, 1.0)
e.on_vfx(FAKE, 1.0, event_id="wave2")
second = wave(e, [C, D], SET2, 1.0)
check("distinct VFX survive delivery in the same processing tick",
      marks(first + second) == {A: IGN1, B: IGN2, C: BND1, D: BND2})

e = eng()
e.on_vfx(REAL, 1.0)
check("a missing wave cannot lend its VFX to the next wave",
      wave(e, [A, B], SET2, 25.0) == [])

for kind, signs in ((REAL, (IGN1, IGN2)), (FAKE, (BND1, BND2))):
    e = eng()
    wave(e, [A, B], SET1, 10.0, vfx=kind, vfx_time=1.0)
    queued = wave(e, [C, D], SET2, 25.0, vfx=kind, vfx_time=16.0)
    check("a matching later pair waits for the current carriers", queued == [])
    actions = e.flush(70.1)
    check("expiry transfers signs when the first pair loss lines are missing",
          actions == [("clear", A), ("clear", B),
                      ("mark", C, signs[0]), ("mark", D, signs[1])])
    check("late losses cannot remove the later pair signs",
          e.on_loss(CURSED_SHRIEK, A, 70.2) == []
          and e.on_loss(CURSED_SHRIEK, B, 70.2) == []
          and set(e.outstanding()) == {C, D})
    check("late gains cannot reset the pair now holding the signs",
          gain(e, A, SET1, 70.3) == [] and set(e.outstanding()) == {C, D})
    check("the final pair expires even without loss lines",
          e.flush(94.1) == [("clear", C), ("clear", D)] and not e.needs_flush())

e = eng()
wave(e, [A, B], SET1, 10.0, vfx=REAL, vfx_time=1.0)
wave(e, [C, D], SET2, 25.0, vfx=REAL, vfx_time=16.0)
check("an unmarked carrier loss does not clear a sign",
      e.on_loss(CURSED_SHRIEK, C, 30.0) == [])
check("a lost waiting carrier never receives the released sign",
      marks(e.flush(70.1)) == {D: IGN2})

print()
if FAILS:
    print(f"{len(FAILS)} FAILED")
    sys.exit(1)
print("all tests passed")
