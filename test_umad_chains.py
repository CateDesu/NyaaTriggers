"""Tests for the UMAD P3 black-hole marker chain engine (umad_chains.py).

Uses the assignment the mechanic actually deals: 8 players, 3 non-Accretion
DPS, 3 non-Accretion supports, the Accretion DPS+healer pair. Everyone gets
Primordial Crust plus First/Second/Third in Line.

Run directly:  python test_umad_chains.py   (exit 0 = all pass)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from umad_chains import ACC, ACCRETION, BURST_GAP_S, CRUST, DPS, SUPPORT, \
    STALE_S, BlackHoleChains, StatusPairs, canon_status_key, parse_compound, \
    role_for_job

FAILS = []


def check(name, cond):
    print(("PASS  " if cond else "FAIL  ") + name)
    if not cond:
        FAILS.append(name)


# The cast: actor id -> ClassJob. D4 + H2 are the Accretion pair.
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
LINE1, LINE2, LINE3 = "BBC", "BBD", "BBE"

# (actor, order-status) per queue: DPS D1<D2<D3, supports T1<T2<H1, acc D4<H2.
ORDERS = [(D1, LINE1), (D2, LINE2), (D3, LINE3),
          (T1, LINE1), (T2, LINE2), (H1, LINE3),
          (D4, LINE1), (H2, LINE2)]


def engine(jobs=JOBS):
    return BlackHoleChains(role_of=lambda aid: role_for_job(jobs.get(aid)))


def feed_assignment(eng, now=10.0, accretion_last_for=None, skip=(), skip_order=()):
    """Send the full debuff burst. Returns all actions the fast path emitted.
    accretion_last_for: that player's 644 arrives after everything else (the
    line-order hazard). skip: players who get NO lines at all (dead).
    skip_order: players whose in-Line order line is dropped (crust still lands)."""
    acts = []
    early_acc = [a for a in (D4, H2) if a != accretion_last_for and a not in skip]
    for actor in early_acc:
        acts += eng.on_gain(ACCRETION, actor, now)
    for actor, line in ORDERS:
        if actor in skip:
            continue
        if actor not in skip_order:
            acts += eng.on_gain(line, actor, now)
        acts += eng.on_gain(CRUST, actor, now)
    if accretion_last_for and accretion_last_for not in skip:
        acts += eng.on_gain(ACCRETION, accretion_last_for, now)
    return acts


def marks(actions):
    return [(a[1], a[2]) for a in actions if a[0] == "mark"]


def clears(actions):
    return [a[1] for a in actions if a[0] == "clear"]


# ── Happy path: assignment marks each queue's 1st, cleanses walk the signs ──
eng = engine()
acts = feed_assignment(eng)
check("assignment marks all three queue heads",
      sorted(marks(acts)) == sorted([(D1, "attack1"), (T1, "attack2"), (D4, "attack3")]))
check("assignment emits no clears", clears(acts) == [])
check("flush after fast path is a no-op", eng.flush(11.5) == [])

acts = eng.on_loss(CRUST, D1, 30.0)
check("DPS cleanse 1 hands attack1 to 2nd in line", marks(acts) == [(D2, "attack1")])
acts = eng.on_loss(CRUST, D4, 31.0)
check("Accretion cleanse 1 hands attack3 to the 2nd Accretion", marks(acts) == [(H2, "attack3")])
acts = eng.on_loss(CRUST, T1, 32.0)
check("support cleanse 1 hands attack2 to 2nd in line", marks(acts) == [(T2, "attack2")])
acts = eng.on_loss(CRUST, D2, 33.0)
check("DPS cleanse 2 hands attack1 to 3rd in line", marks(acts) == [(D3, "attack1")])
acts = eng.on_loss(CRUST, D3, 34.0)
check("last DPS cleanse clears the sign off them",
      marks(acts) == [] and clears(acts) == [D3])
acts = eng.on_loss(CRUST, T2, 35.0) + eng.on_loss(CRUST, H1, 36.0) \
    + eng.on_loss(CRUST, H2, 37.0)
check("remaining queues finish with their own clears",
      marks(acts) == [(H1, "attack2")] and sorted(clears(acts)) == sorted([H1, H2]))

# ── outstanding(): current holders exposed so a wipe/toggle-off can clear them ──
eng = engine()
feed_assignment(eng)
check("outstanding lists each queue's current holder (dps, support, acc)",
      eng.outstanding() == [D1, T1, D4])
eng.on_loss(CRUST, D1, 30.0)   # attack1 hops D1 -> D2 mid-mechanic
check("outstanding follows a hop", eng.outstanding() == [D2, T1, D4])
eng.reset()
check("reset empties outstanding (no stale holders survive a wipe)", eng.outstanding() == [])

# ── has_open_queues: live unresolved state for the job-backfill re-arm ──
eng = engine()
check("cold engine has no open queues", not eng.has_open_queues())
feed_assignment(eng)
check("all queues started leaves nothing open", not eng.has_open_queues())
eng = engine(jobs={})                      # roles unknown: role queues can't start
feed_assignment(eng)
check("unstarted role queues read as open", eng.has_open_queues())
eng.reset()
check("reset closes everything", not eng.has_open_queues())

# ── Losing Accretion itself (its cleanse step) moves nothing ──
eng = engine()
feed_assignment(eng)
check("Accretion loss is not a hand-off", eng.on_loss(ACCRETION, D4, 20.0) == [])

# ── Boundary normalization: raw '0644' / '0x154E' forms still land ──
eng = engine()
acts = eng.on_gain("0644", D4, 10.0)
check("leading-zero effect id is normalized on gain",
      eng.on_gain("644", H2, 10.0) is not None and acts == [])
feed_assignment(eng)
holder_moved = eng.on_loss("0x154E", D1, 30.0)
check("prefixed effect id is normalized on loss", marks(holder_moved) == [(D2, "attack1")])

# ── Out-of-order cleanse: a junior's Crust drops before the holder's ──
eng = engine()
feed_assignment(eng)
acts = eng.on_loss(CRUST, D2, 30.0)
check("junior cleansing early moves nothing", acts == [])
acts = eng.on_loss(CRUST, D1, 31.0)
check("holder cleanse then skips the already-cleansed junior",
      marks(acts) == [(D3, "attack1")])

# ── Line-order hazard: an Accretion player's 644 arrives last ──
eng = engine()
acts = feed_assignment(eng, accretion_last_for=D4)
ok = sorted(marks(acts)) == sorted([(D1, "attack1"), (T1, "attack2"), (D4, "attack3")])
check("late 644 cannot leak the Accretion player into the DPS queue", ok)
check("late 644: no player ever got a second (wrong) sign",
      len(marks(acts)) == 3)

# ── Staggered 644 after a 1-member flush start: the sign reseats ──
eng = engine()
acts = []
acts += eng.on_gain(ACCRETION, H2, 10.0)          # only H2's 644 arrives on time
for actor, line in ORDERS:
    acts += eng.on_gain(line, actor, 10.0)
    acts += eng.on_gain(CRUST, actor, 10.0)
flush_acts = eng.flush(11.2)                       # debounce fires before D4's 644
check("flush starts the 1-member Accretion queue on its lone member",
      (H2, "attack3") in marks(flush_acts))
check("flush withholds role queues until both 644s are known (audit C7)",
      all(m[1] == "attack3" for m in marks(acts + flush_acts)))
late = eng.on_gain(ACCRETION, D4, 12.0)            # D4 is 1st in line of the pair
check("late 644 reseats the Accretion sign onto the true head (audit C2)",
      (D4, "attack3") in marks(late))
check("late 644 also unblocks the role queues",
      sorted(m for m in marks(late) if m[1] != "attack3")
      == sorted([(D1, "attack1"), (T1, "attack2")]))

# ── Missing 644 forever: role queues stay silent all mechanic ──
eng = engine()
acts = feed_assignment(eng, skip=(D4,))            # Accretion DPS dead: one 644 total
acts += eng.flush(11.2)
check("missing second 644: only the lone Accretion player is ever marked",
      marks(acts) == [(H2, "attack3")])

# ── Unknown roles: role queues stay silent, Accretion queue still works ──
eng = engine(jobs={})
acts = feed_assignment(eng)
check("no jobs: only the Accretion queue is marked", marks(acts) == [(D4, "attack3")])
check("no jobs: flush still refuses the role queues", eng.flush(12.0) == [])
acts = eng.on_loss(CRUST, D4, 30.0)
check("no jobs: Accretion hand-off still works", marks(acts) == [(H2, "attack3")])

# ── Future job id fails closed, not open as DPS ──
future_jobs = dict(JOBS, **{D1: 43})               # 43 = job that doesn't exist yet
eng = engine(jobs=future_jobs)
acts = feed_assignment(eng) + eng.flush(11.2)
check("unknown future job: DPS queue stays silent instead of mis-marking",
      all(m[1] != "attack1" for m in marks(acts)))
check("unknown future job: other queues unaffected",
      sorted(marks(acts)) == sorted([(T1, "attack2"), (D4, "attack3")]))

# ── Missing player (died pre-assignment): flush best-effort starts the queue ──
eng = engine()
acts = feed_assignment(eng, skip=(D3,))
check("2-of-3 DPS queue is not fast-path marked",
      (D1, "attack1") not in marks(acts) and (D2, "attack1") not in marks(acts))
acts = eng.flush(12.0)
check("flush marks the incomplete DPS queue's head", marks(acts) == [(D1, "attack1")])
acts = eng.on_loss(CRUST, D1, 30.0) + eng.on_loss(CRUST, D2, 31.0)
check("incomplete queue still walks and clears",
      marks(acts) == [(D2, "attack1")] and clears(acts) == [D2])

# ── Missing order line: flush refuses to guess the head ──
eng = engine()
acts = feed_assignment(eng, skip_order=(D1,))      # D1 crusted but order unknown
acts += eng.flush(11.2)
check("crusted member with unknown order blocks that queue's flush",
      all(m[1] != "attack1" for m in marks(acts)))
check("unknown order blocks only the affected queue",
      sorted(marks(acts)) == sorted([(T1, "attack2"), (D4, "attack3")]))
late_order = eng.on_gain(LINE1, D1, 12.0)          # the missing line finally arrives
check("late order line completes and marks the correct head",
      marks(late_order) == [(D1, "attack1")])

# ── Second black hole: finished instance re-arms inside the stale window ──
eng = engine()
feed_assignment(eng, now=10.0)
for actor in (D1, D2, D3, D4, H2, T1, T2, H1):
    eng.on_loss(CRUST, actor, 30.0)
acts = feed_assignment(eng, now=60.0)     # well inside STALE_S
check("finished instance: a fresh burst starts a new one",
      sorted(marks(acts)) == sorted([(D1, "attack1"), (T1, "attack2"), (D4, "attack3")]))

# ── Partial instance must not re-arm while Crust is still held ──
eng = engine(jobs={})                      # roles unknown: only ACC ever starts
feed_assignment(eng, now=10.0)
eng.on_loss(CRUST, D4, 30.0)
eng.on_loss(CRUST, H2, 31.0)               # ACC queue walked out. 6 players crusted
eng.on_gain(CRUST, D1, 32.0)               # a refreshed line mid-mechanic
check("mid-mechanic line does not wipe crusted players' state",
      eng._players[T1]["crust"] and eng._players[T1]["order"] == 1)
for actor in (D1, D2, D3, T1, T2, H1):
    eng.on_loss(CRUST, actor, 40.0)        # now everyone is cleansed
acts = feed_assignment(eng, now=60.0)      # next black hole re-arms
check("after all Crusts resolve a fresh burst re-arms (still audit C6)",
      (D4, "attack3") in marks(acts))

# ── Missed Crust 30: the next black hole still re-arms ──
eng = engine()
feed_assignment(eng, now=10.0)
for actor in (D1, D2, D4, H2, T1, T2, H1):
    eng.on_loss(CRUST, actor, 30.0)          # D3's loss line never arrives
check("missed Crust loss leaves the sign stranded on the last head",
      eng.outstanding() == [D3])
acts = feed_assignment(eng, now=60.0)        # next burst inside the stale window
check("missed Crust loss: the stranded sign clears before the new marks",
      acts and acts[0] == ("clear", D3))
check("missed Crust loss: the next black hole re-arms all three queues",
      sorted(marks(acts)) == sorted([(D1, "attack1"), (T1, "attack2"), (D4, "attack3")]))
acts = eng.on_loss(CRUST, D1, 80.0)
check("missed Crust loss: the re-armed queue walks normally",
      marks(acts) == [(D2, "attack1")])

# ── the stalled-queue reset waits for a quiet gap ──
eng = engine()
feed_assignment(eng, now=10.0)
for actor in (D1, D2, D4, H2, T1, T2, H1):
    eng.on_loss(CRUST, actor, 30.0)          # D3's loss line never arrives
acts = feed_assignment(eng, now=33.0)        # too soon, mid-cleanse timing
check("a burst right after the last event does not reset a stalled queue",
      acts == [] and eng.outstanding() == [D3])

# ── Burst-gap reset: a quiet pre-Crust burst is dropped on the next gain ──
eng = engine()
acts = []
acts += eng.on_gain(ACCRETION, D4, 10.0)
acts += eng.on_gain(ACCRETION, H2, 10.0)
for actor, line in ORDERS:
    acts += eng.on_gain(line, actor, 10.0)   # both 644s and every order, no Crust
check("644s and orders without Crust start no queue",
      acts == [] and eng._holder == {})
late = eng.on_gain(CRUST, D1, 10.0 + BURST_GAP_S + 1.0)
check("a lone Crust after the quiet gap resets the dead burst, fail closed",
      late == [] and list(eng._players) == [D1])
acts = feed_assignment(eng, now=10.0 + BURST_GAP_S + 2.0)
check("the next burst re-arms off clean state, no stale heads or clears",
      sorted(marks(acts)) == sorted([(D1, "attack1"), (T1, "attack2"), (D4, "attack3")])
      and clears(acts) == [])

# ── Stale re-arm: an unresolved instance is dropped after STALE_S ──
eng = engine()
eng.on_gain(CRUST, D1, 10.0)              # lone stray line, never resolves
acts = feed_assignment(eng, now=10.0 + STALE_S + 5.0)
check("stale leftovers don't block the next instance",
      sorted(marks(acts)) == sorted([(D1, "attack1"), (T1, "attack2"), (D4, "attack3")]))

# ── Stale flush: a debounce firing ages after the last event marks nothing,
# but the signs the dead instance still holds come down with the reset ──
eng = engine()
feed_assignment(eng, skip=(D3,))            # DPS queue left open for the flush
check("stale flush clears the held signs before resetting",
      eng.flush(10.0 + STALE_S + 5.0) == [("clear", T1), ("clear", D4)])
check("stale flush resets the instance",
      not eng._players and eng._last_event == 0.0)
acts = feed_assignment(eng, now=10.0 + STALE_S + 6.0)
check("the next burst after a stale flush starts clean",
      sorted(marks(acts)) == sorted([(D1, "attack1"), (T1, "attack2"), (D4, "attack3")]))

# ── Stale Crust loss: the held signs come down with the reset too ──
eng = engine()
feed_assignment(eng)
acts = eng.on_loss(CRUST, D1, 10.0 + STALE_S + 5.0)
check("stale loss clears every held sign before resetting",
      acts == [("clear", D1), ("clear", T1), ("clear", D4)])
check("stale loss resets the instance",
      not eng._players and eng._last_event == 0.0)

# ── Stale re-arm with signs still up: the fresh burst clears them first ──
eng = engine()
feed_assignment(eng)
acts = feed_assignment(eng, now=10.0 + STALE_S + 5.0)
check("stale re-arm clears the old signs before marking the new heads",
      acts[:3] == [("clear", D1), ("clear", T1), ("clear", D4)]
      and sorted(marks(acts)) == sorted([(D1, "attack1"), (T1, "attack2"), (D4, "attack3")]))

# ── reset() drops everything ──
eng = engine()
feed_assignment(eng)
eng.reset()
check("after reset a Crust loss does nothing", eng.on_loss(CRUST, D1, 30.0) == [])

# ── role_for_job mapping ──
check("role_for_job: tanks and healers are supports",
      role_for_job(19) == SUPPORT and role_for_job(21) == SUPPORT
      and role_for_job(24) == SUPPORT and role_for_job(40) == SUPPORT)
check("role_for_job: known DPS jobs are dps",
      role_for_job(34) == DPS and role_for_job(25) == DPS and role_for_job(41) == DPS)
check("role_for_job: unknown fails closed",
      role_for_job(None) is None and role_for_job(0) is None
      and role_for_job(43) is None and role_for_job(999) is None)

# ── custom markers apply (dict API) ──
eng = engine()
eng.set_markers({DPS: "circle", SUPPORT: "square", ACC: "triangle"})
acts = feed_assignment(eng)
check("set_markers changes the signs used",
      sorted(marks(acts)) == sorted([(D1, "circle"), (T1, "square"), (D4, "triangle")]))
eng2 = BlackHoleChains(role_of=lambda aid: role_for_job(JOBS.get(aid)),
                       markers={ACC: "cross"})
acts = feed_assignment(eng2)
check("constructor markers override only the given queues",
      sorted(marks(acts)) == sorted([(D1, "attack1"), (T1, "attack2"), (D4, "cross")]))

# ── parse_compound / canon_status_key: "A+B" tokens for compound automark rules ──
check("plain token is not compound", parse_compound("644") is None)
check("compound splits and normalizes", parse_compound("0x644+bbc") == ("644", "BBC"))
check("malformed compound (trailing +) is None", parse_compound("644+") is None)
check("3-part compound is None", parse_compound("644+BBC+BBD") is None)
check("exact-name token with '+' is NOT compound (stays a name match)",
      parse_compound("Damage Up+") is None and parse_compound("Real+Fake Gaze") is None)
check("canon key: spelling variants collapse to one identity",
      canon_status_key("644+0bbc") == canon_status_key("BBC+644")
      == canon_status_key("0x644+BBC") == "644+BBC")
check("canon key: plain ids normalize like _norm_hex", canon_status_key("0x08d1") == "8D1")
check("canon key: names pass through _norm_id shape", canon_status_key("Damage Up+") == "DAMAGE UP+")

# ── StatusPairs: per-actor held-status tracker behind compound rules ──
P1, P2 = "10111111", "10222222"
pairs = StatusPairs(["644", "BBC", "BBD"])
pairs.on_gain("644", P1, 10.0)
check("one status held is not the pair", not pairs.holds_all(P1, ("644", "BBC"), 10.0))
pairs.on_gain("BBC", P1, 10.5)
check("both held fires regardless of order", pairs.holds_all(P1, ("644", "BBC"), 11.0))
check("other actor's statuses don't leak", not pairs.holds_all(P2, ("644", "BBC"), 11.0))
pairs.on_gain("0BBD", P2, 11.0)    # un-normalized input at the boundary
pairs.on_gain("644", P2, 11.0)
check("2nd-in-line pair resolves on the other actor", pairs.holds_all(P2, ("644", "BBD"), 11.5))
check("P2 does not satisfy the 1st-in-line pair", not pairs.holds_all(P2, ("644", "BBC"), 11.5))
pairs.on_gain("154E", P1, 12.0)    # untracked id (Crust) is ignored
check("untracked gains are not recorded", not pairs.holds_all(P1, ("644", "154E"), 12.0))
pairs.on_loss("644", P1)
check("loss breaks the pair", not pairs.holds_all(P1, ("644", "BBC"), 12.0))
pairs.on_loss("BBC", P1)
pairs.on_loss("BBC", P1)           # double-loss is harmless
pairs.on_gain("644", P1, 13.0)
pairs.on_gain("BBC", P1, 13.0)
check("re-gain after full loss re-fires", pairs.holds_all(P1, ("644", "BBC"), 13.5))
check("stale entries do not satisfy the pair (missed loss line)",
      not pairs.holds_all(P1, ("644", "BBC"), 13.0 + STALE_S + 1))
pairs.on_gain("BBD", P1, 200.0)    # fresh gain next to stale 644 must not pair up
check("a fresh status next to a stale one is still not a pair",
      not pairs.holds_all(P1, ("644", "BBD"), 200.0))
pairs.reset()
check("reset drops everything", not pairs.holds_all(P1, ("644", "BBC"), 13.5)
      and not pairs.holds_all(P2, ("644", "BBD"), 13.5))

print()
if FAILS:
    print(f"{len(FAILS)} FAILED")
    sys.exit(1)
print("all tests passed")
