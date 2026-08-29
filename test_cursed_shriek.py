"""Tests for the UMAD P4 Cursed Shriek gaze-pairing engine (umad_chains.py).

Two Grand Cross waves each deal a pair of Cursed Shriek 15A7, 15s apart, and
each wave's Inferno or Tsunami followup cast tells whether its pair is the
fake look-at gaze or the real look-away one. Inferno arms fake, Tsunami arms
real. The pair marks the moment its second gain lands, fail-closed when the
followup never arrived.

Run directly:  python test_cursed_shriek.py   (exit 0 = all pass)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from umad_chains import (
    AWAY1, AWAY2, LOOK1, LOOK2, BURST_GAP_S, CURSED_SHRIEK, DEFAULT_GAZE_MARKERS,
    FAKE_FOLLOWUP_IDS, GAZE_IDS, REAL_FOLLOWUP_IDS, STALE_S,
    CursedShriekPairs,
)

FAILS = []


def check(name, cond):
    print(("PASS  " if cond else "FAIL  ") + name)
    if not cond:
        FAILS.append(name)


# Set 1 carriers A/B, set 2 carriers C/D. Durations read from the logs,
# 60s on set 1, 69s on set 2, but carry no real or fake meaning.
A, B, C, D = "10000001", "10000002", "10000003", "10000004"
SET1, SET2 = 60.0, 69.0
INFERNO, TSUNAMI = "BB1E", "BB1F"
IGN1, IGN2 = DEFAULT_GAZE_MARKERS[AWAY1], DEFAULT_GAZE_MARKERS[AWAY2]   # ignore1/2
BND1, BND2 = DEFAULT_GAZE_MARKERS[LOOK1], DEFAULT_GAZE_MARKERS[LOOK2]   # bind1/2


def eng(**kw):
    return CursedShriekPairs(**kw)


def gain(e, actor, dur, t):
    return e.on_gain(CURSED_SHRIEK, actor, dur, t)


def wave(e, actors, dur, t, followup=None, t_fu=None):
    """Feed one wave: optional followup cast then the pair's gains, same
    timestamp. Returns all actions."""
    acts = []
    if followup is not None:
        acts += e.on_followup(followup, t if t_fu is None else t_fu)
    for a in actors:
        acts += gain(e, a, dur, t)
    return acts


def marks(acts):
    """{actor: marker} from mark actions."""
    return {a[1]: a[2] for a in acts if a[0] == "mark"}


# ── the labeled pull shape: Inferno fake first, Tsunami real second ──
e = eng()
m = marks(wave(e, [A, B], SET1, 10.0, followup=INFERNO, t_fu=6.0)
          + wave(e, [C, D], SET2, 25.0, followup=TSUNAMI, t_fu=21.0))
check("inferno wave marks its pair with the look-at binds",
      m[A] == BND1 and m[B] == BND2)
check("tsunami wave marks its pair with the look-away ignores",
      m[C] == IGN1 and m[D] == IGN2)
check("all four signs are outstanding together",
      set(e.outstanding()) == {A, B, C, D})

# the swap: Tsunami first, Inferno second
e = eng()
m = marks(wave(e, [A, B], SET1, 10.0, followup=TSUNAMI, t_fu=6.0)
          + wave(e, [C, D], SET2, 25.0, followup=INFERNO, t_fu=21.0))
check("swapped pulls mark the other way, tsunami real first",
      m[A] == IGN1 and m[B] == IGN2 and m[C] == BND1 and m[D] == BND2)

# BB20/BB21, the second cast id of each element, arm the same way
check("BB20 is a fake followup and BB21 a real one",
      "BB20" in FAKE_FOLLOWUP_IDS and "BB21" in REAL_FOLLOWUP_IDS)

# ── nothing fires before the pair completes ──
e = eng()
half = e.on_followup(INFERNO, 6.0) + gain(e, A, SET1, 10.0) + gain(e, A, SET1, 10.0)
check("one gain and a duplicate mark nothing", half == [])
last = gain(e, B, SET1, 10.0)
check("the partner gain completes the assignment", marks(last) == {A: BND1, B: BND2})

# ── fail-closed: no followup, no marks ──
e = eng()
check("a pair whose wave's followup never arrived marks nothing",
      wave(e, [A, B], SET1, 10.0) == [])
check("the unarmed set is discarded, not held",
      e._set == [] and e._sets_done == 1)
m = marks(wave(e, [C, D], SET2, 25.0, followup=TSUNAMI, t_fu=21.0))
check("the next armed wave still marks", m == {C: IGN1, D: IGN2})

# ── party-slot ordering wins over actor id ──
# Slots reversed vs id order: B is slot 1, A slot 2, so B must get the 1 sign.
slots = {A: 2, B: 1, C: 4, D: 3}
e = eng(slot_of=lambda a: slots.get(a))
m = marks(wave(e, [A, B], SET1, 10.0, followup=INFERNO, t_fu=6.0)
          + wave(e, [C, D], SET2, 25.0, followup=INFERNO, t_fu=21.0))
check("bind 1 goes to the lower party slot (B), not the lower id",
      m[B] == BND1 and m[A] == BND2)
check("the second pair orders by slot too",
      m[D] == BND1 and m[C] == BND2)

# a known slot sorts ahead of an unknown one
e = eng(slot_of=lambda a: {A: 5}.get(a))
m = marks(wave(e, [A, B], SET1, 10.0, followup=TSUNAMI, t_fu=6.0))
check("a slot-known player sorts ahead of a slot-unknown partner",
      m[A] == IGN1 and m[B] == IGN2)

# ── incomplete sets ──
e = eng()
acts = e.on_followup(INFERNO, 6.0) + gain(e, A, SET1, 10.0)
check("a lone first gain marks nothing", acts == [])
lost = e.on_loss(CURSED_SHRIEK, A, 11.0)
check("the lone carrier losing it clears quietly", lost == [])
m = marks(wave(e, [B, C], SET1, 30.0, followup=INFERNO, t_fu=26.0))
check("a fresh wave after the discard still marks", m == {B: BND1, C: BND2})

# a partner that never comes is dropped after the burst gap
e = eng()
e.on_followup(INFERNO, 6.0)
gain(e, A, SET1, 10.0)
check("flush inside the burst window keeps the open set",
      e.flush(11.0) == [] and e._set == [A])
e.flush(10.0 + BURST_GAP_S + 1)
check("flush after the burst gap discards the orphaned set",
      e._set == [] and e._polarity is None)

# ── the next wave's tell survives an orphaned set's discard ──
# Wave 1's partner 26 never came, wave 2's followup armed cleanly. The
# discard of wave 1's leftover must not eat wave 2's armed tell.
e = eng()
e.on_followup(INFERNO, 6.0)
gain(e, A, SET1, 10.0)               # orphaned
e.on_followup(TSUNAMI, 21.0)         # wave 2's tell
acts = gain(e, C, SET2, 25.0) + gain(e, D, SET2, 25.1)
check("a wave 2 tell survives the wave 1 orphan discard",
      marks(acts) == {C: IGN1, D: IGN2})

# same via the late loss path, the orphan's 30 line empties the set after
# the new tell already armed
e = eng()
e.on_followup(INFERNO, 6.0)
gain(e, A, SET1, 10.0)
e.on_followup(TSUNAMI, 21.0)
loss = e.on_loss(CURSED_SHRIEK, A, 22.0)
acts = gain(e, C, SET2, 25.0) + gain(e, D, SET2, 25.1)
check("a late orphan loss clears nothing and keeps the armed tell",
      loss == [] and marks(acts) == {C: IGN1, D: IGN2})

# counterpart, no fresh tell: the dead wave's polarity must not bleed
e = eng()
e.on_followup(INFERNO, 6.0)
gain(e, A, SET1, 10.0)               # orphaned, no wave 2 followup ever
acts = gain(e, C, SET2, 25.0) + gain(e, D, SET2, 25.1)
check("an orphaned wave's own tell dies with it, no bleed",
      marks(acts) == {})

# ── a stray pair between waves marks nothing, the real wave self-heals ──
e = eng()
wave(e, [A, B], SET1, 10.0, followup=INFERNO, t_fu=6.0)
acts = gain(e, C, SET1, 11.0) + gain(e, D, SET1, 11.1)
check("a stray pair with no armed tell marks nothing and clears nothing",
      acts == [] and set(e.outstanding()) == {A, B})
m = marks(wave(e, [C, D], SET2, 25.0, followup=TSUNAMI, t_fu=21.0))
check("the real wave after the strays re-arms and assigns",
      m == {C: IGN1, D: IGN2})

# ── loss clears that player's sign, per set ──
e = eng()
wave(e, [A, B], SET1, 10.0, followup=INFERNO, t_fu=6.0)
loss = e.on_loss(CURSED_SHRIEK, A, 20.0)
check("losing the gaze clears that player's sign", loss == [("clear", A)])
check("cleared player drops out of outstanding", set(e.outstanding()) == {B})
check("a second loss for the same player is a no-op",
      e.on_loss(CURSED_SHRIEK, A, 20.1) == [])

# ── refresh keeps the assignment ──
e = eng()
wave(e, [A, B], SET1, 10.0, followup=INFERNO, t_fu=6.0)
refire = gain(e, A, SET1, 11.0)
check("a re-gain after assignment does not re-fire or wipe marks",
      refire == [] and set(e.outstanding()) == {A, B})

# ── a fully resolved phase resets quietly, the next phase assigns ──
e = eng()
wave(e, [A, B], SET1, 10.0, followup=INFERNO, t_fu=6.0)
wave(e, [C, D], SET2, 25.0, followup=TSUNAMI, t_fu=21.0)
for who in (A, B, C, D):
    e.on_loss(CURSED_SHRIEK, who, 45.0)
check("field is clear after every gaze resolves", e.outstanding() == [])
m = marks(wave(e, [A, B], SET1, 100.0, followup=TSUNAMI, t_fu=96.0)
          + wave(e, [C, D], SET2, 115.0, followup=INFERNO, t_fu=111.0))
check("the next phase after full resolution assigns again",
      m == {A: IGN1, B: IGN2, C: BND1, D: BND2})

# ── both sets dealt with the 30s missed: a new gain starts clean ──
e = eng()
wave(e, [A, B], SET1, 10.0, followup=INFERNO, t_fu=6.0)
wave(e, [C, D], SET2, 25.0, followup=TSUNAMI, t_fu=21.0)
# every loss line missed, next pull deals a fresh first wave
acts = wave(e, [A, B], SET1, 100.0, followup=TSUNAMI, t_fu=96.0)
check("a gain after a closed phase drops the stale signs first",
      [a for a in acts if a[0] == "clear"] == [("clear", a) for a in (A, B, C, D)])
m = marks(acts)
check("the fresh deal assigns off the new tell, kept across the reset",
      m == {A: IGN1, B: IGN2})

# same glue but the new pull's followup never arrived, so nothing marks
e = eng()
wave(e, [A, B], SET1, 10.0, followup=INFERNO, t_fu=6.0)
wave(e, [C, D], SET2, 25.0, followup=TSUNAMI, t_fu=21.0)
acts = gain(e, A, SET1, 100.0) + gain(e, B, SET1, 100.1)
check("a glued deal with no fresh tell clears the signs and marks nothing",
      marks(acts) == {}
      and [a for a in acts if a[0] == "clear"] == [("clear", a) for a in (A, B, C, D)])

# ── staleness: an event long after the last one is a new phase ──
e = eng()
wave(e, [A, B], SET1, 10.0, followup=INFERNO, t_fu=6.0)
late = wave(e, [C, D], SET2, 10.0 + STALE_S + 5, followup=TSUNAMI,
            t_fu=10.0 + STALE_S + 1)
check("a stale phase's signs come down before the new wave assigns",
      [a for a in late if a[0] == "clear"] == [("clear", A), ("clear", B)]
      and marks(late) == {C: IGN1, D: IGN2})

# a stale followup drops the dead phase's signs before arming
e = eng()
wave(e, [A, B], SET1, 10.0, followup=INFERNO, t_fu=6.0)
acts = e.on_followup(TSUNAMI, 10.0 + STALE_S + 5)
check("a stale followup clears the dead signs and arms the new phase",
      acts == [("clear", A), ("clear", B)]
      and e._sets_done == 0 and e._polarity == "away1")

# ── misc ──
check("a non-gaze status id is ignored",
      eng().on_gain("644", A, 20.0, 10.0) == [])
check("a non-followup cast id is ignored",
      eng().on_followup("BA94", 10.0) == [])
check("default gaze id set is just Cursed Shriek",
      GAZE_IDS == frozenset({CURSED_SHRIEK}))

e = eng()
e.set_markers({AWAY1: "circle", AWAY2: "square", LOOK1: "cross", LOOK2: "triangle"})
m = marks(wave(e, [A, B], SET1, 10.0, followup=INFERNO, t_fu=6.0)
          + wave(e, [C, D], SET2, 25.0, followup=TSUNAMI, t_fu=21.0))
check("set_markers swaps the signs used",
      m[A] == "cross" and m[B] == "triangle"
      and m[C] == "circle" and m[D] == "square")

e = eng()
wave(e, [A, B], SET1, 10.0, followup=INFERNO, t_fu=6.0)
e.reset()
check("reset clears everything",
      e.outstanding() == [] and e._sets_done == 0)

print()
if FAILS:
    print(f"{len(FAILS)} FAILED")
    sys.exit(1)
print("all tests passed")
