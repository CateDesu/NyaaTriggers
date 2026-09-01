"""Tests for the ACT-structure DPS meter (dps_meter.py).

Covers the effect-pair decode against the cactbot LogGuide examples, a
scripted mini-fight end to end (per-combatant totals, crit/DH/CDH rates,
maxhit, deaths, damage taken, pet merge, enemy exclusion), encounter
lifecycle (combat flag, lazy aggression start, wipe, zone change, empty
pulls), the display view's idle pause/reset vs the always-complete recorded
pull, overlay rows, and the plugin dps_frame contract.

Run:  python test_dps_meter.py   (exit 0 = all pass)

No game, Qt, or display needed: dps_meter and plugin_link import headless.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dps_meter import DpsMeter, _actor_int, _unpack_effect
import plugin_link as pl

FAILS = []


def check(name, cond):
    print(("PASS  " if cond else "FAIL  ") + name)
    if not cond:
        FAILS.append(name)


class Clock:
    """Injectable monotonic clock. Tests advance it explicitly."""

    def __init__(self, base=1000.0):
        self.t = base

    def __call__(self):
        return self.t

    def set(self, t):
        self.t = 1000.0 + t


# ── line builders ────────────────────────────────────────────────────────
def dmg(amount):
    """Plain damage field: the high word is the amount."""
    return f"{amount << 16:X}"


def ability(lt, sid, sname, aname, tid, tname, pairs,
            target_index=0, target_count=1, owner="00", owner_name=""):
    """A 21/22 line: header fields, eight [flags, damage] pairs, then the
    trailing block with targetIndex/targetCount/ownerId/ownerName at the
    documented wire positions 45-48."""
    f = [lt, "2026-08-05T00:00:00", sid, sname, "A1", aname, tid, tname]
    for i in range(8):
        if i < len(pairs):
            f += [pairs[i][0], pairs[i][1]]
        else:
            f += ["0", "0"]
    f += ["0"] * (45 - len(f))
    f += [str(target_index), str(target_count), owner, owner_name]
    return f


def dot(which, tid, tname, amount_hex, applier_id, applier_name):
    """A 24 DoT/HoT line: amount at 6, applier id/name at 17/18."""
    f = ["24", "2026-08-05T00:00:00", tid, tname, which, "0", amount_hex]
    f += ["0"] * (17 - len(f))
    f += [applier_id, applier_name]
    return f


ME = "10FF0001"
ME_NAME = "Tini Poutini"
P2 = "10FF0002"
P2_NAME = "Potato Chippy"
PET = "4000A001"
PET2 = "4000A002"
BOSS = "40012345"
BOSS2 = "40012346"


def roster(m):
    """Zone + me + two players (one lowercase id, to prove id normalization),
    a pet with an 03 ownerId, and the boss (job 00 = NPC)."""
    m.process(["01", "ts", "4B0", "Everkeep"], "")
    m.process(["02", "ts", ME, ME_NAME], "")
    m.process(["03", "ts", ME, ME_NAME, "21", "5A", "0000"], "")      # 0x21 = 33 AST
    m.process(["03", "ts", P2.lower(), P2_NAME, "1F", "5A", "0000"], "")  # 0x1F = 31 MCH
    m.process(["03", "ts", PET, "Eos", "00", "5A", P2], "")
    m.process(["03", "ts", BOSS, "Zeromus", "00", "5A", "0000"], "")
    m.process(["03", "ts", BOSS2, "Zeromus", "00", "5A", "0000"], "")


# ── effect decode: doc examples and edge kinds ───────────────────────────
check("unpack basic damage (doc: Grand Cross Alpha 18216)",
      _unpack_effect("750003", "47280000") == ("damage", 18216, False, False))
# The LogGuide's Hyperdrive caption says 82538, but that caption predates the
# doc's current "D A B" formula, which decodes 426B4001 as 0x01426B = 82539
# (the doc's own formula example 423F400F -> 999999 agrees). We follow the
# formula, so 82539 is the correct value here.
check("unpack big damage (doc formula: D A B)",
      _unpack_effect("750003", "426B4001") == ("damage", 82539, False, False))
check("unpack big damage (doc: 999999)",
      _unpack_effect("750003", "423F400F") == ("damage", 999999, False, False))
check("unpack hallowed is zero damage",
      _unpack_effect("750003", f"{(5000 << 16) | 0x0100:X}")[1] == 0)
check("unpack miss kind",
      _unpack_effect("750001", dmg(10000))[0] == "miss")
check("unpack heal kind, never DH",
      _unpack_effect("754004", dmg(10000)) == ("heal", 10000, False, False))
check("unpack crit + DH severity",
      _unpack_effect("756003", dmg(10000)) == ("damage", 10000, True, True))
check("unpack status application is none",
      _unpack_effect("1E00000E", "320000")[0] == "none")

# ── _actor_int: parity with telesto_client._actor_int ────────────────────
# Normal hex/decimal ids resolve to one int; blank/invalid ids, the
# no-target sentinels 0 / E0000000, negatives, and bools are all None.
check("actor int plain hex", _actor_int("10FF0001") == 0x10FF0001)
check("actor int lowercase hex", _actor_int("10ff0001") == 0x10FF0001)
check("actor int int passthrough", _actor_int(0x10FF0001) == 0x10FF0001)
check("actor int zero sentinel", _actor_int("0") is None)
check("actor int padded zero sentinel", _actor_int("0000") is None)
check("actor int E0000000 sentinel", _actor_int("E0000000") is None)
check("actor int negative rejected", _actor_int(-5) is None)
check("actor int negative hex rejected", _actor_int("-5") is None)
check("actor int bool rejected (bool is an int subclass)",
      _actor_int(True) is None and _actor_int(False) is None)
check("actor int blank", _actor_int("") is None)
check("actor int None", _actor_int(None) is None)
check("actor int garbage", _actor_int("ZZZ") is None)

# ── the scripted mini-fight ──────────────────────────────────────────────
clk = Clock()
m = DpsMeter(clock=clk)
ended = []
m.on_encounter_end = ended.append
roster(m)

clk.set(0)
m.set_in_combat(True, True)
check("encounter begins on inACTCombat 0->1", m.current is not None)

clk.set(1)
m.process(ability("21", ME, ME_NAME, "Glare", BOSS, "Zeromus",
                  [("750003", dmg(10000))]), "")
clk.set(2)
m.process(ability("21", ME, ME_NAME, "Malefic", BOSS, "Zeromus",
                  [("752003", dmg(20000))]), "")                     # crit
clk.set(3)
m.process(ability("21", ME, ME_NAME, "Gravity", BOSS, "Zeromus",
                  [("756003", dmg(30000))]), "")                     # crit + DH
clk.set(3.5)
m.process(ability("21", ME, ME_NAME, "Glare", BOSS, "Zeromus",
                  [("750003", f"{(9000 << 16) | 0x0100:X}")]), "")   # hallowed: 0
clk.set(4)
m.process(ability("21", ME, ME_NAME, "Glare", BOSS, "Zeromus",
                  [("750001", "0")]), "")                            # miss
clk.set(5)
# One AoE, two targets. One 22 line per target, lowercase source id on
# purpose, and it must still resolve to Potato.
m.process(ability("22", P2.lower(), P2_NAME, "Auto Crossbow", BOSS, "Zeromus",
                  [("750003", dmg(5000))], target_index=0, target_count=2), "")
m.process(ability("22", P2.lower(), P2_NAME, "Auto Crossbow", BOSS2, "Zeromus",
                  [("750003", dmg(5000))], target_index=1, target_count=2), "")
clk.set(6)
m.process(ability("21", PET, "Eos", "Rock Buster", BOSS, "Zeromus",
                  [("750003", dmg(3000))]), "")                      # pet via 03
clk.set(6.5)
# Second pet: no 03 line at all. Ownership comes from the 21 line's [47].
m.process(ability("21", PET2, "Carbuncle", "Gouge", BOSS, "Zeromus",
                  [("750003", dmg(1500))], owner=ME, owner_name=ME_NAME), "")
clk.set(7)
m.process(dot("DoT", BOSS, "Zeromus", "3E8", ME, ME_NAME), "")       # 1000 to me
clk.set(8)
m.process(dot("DoT", P2, P2_NAME, "1F4", BOSS, "Zeromus"), "")       # 500 taken
clk.set(9)
m.process(dot("HoT", ME, ME_NAME, "2BC", P2, P2_NAME), "")           # 700 healed
clk.set(10)
m.process(["25", "ts", P2, P2_NAME], "")                             # potato dies
clk.set(11)
m.process(ability("21", BOSS, "Zeromus", "Void Bolt", ME, ME_NAME,
                  [("750003", dmg(4000))]), "")                      # 4000 taken

clk.set(12)
snap = m.snapshot()

check("snapshot active while fighting", snap["isActive"] is True)
enc = snap["Encounter"]
check("encounter title is the zone", enc["title"] == "Everkeep")
check("encounter zone name", enc["CurrentZoneName"] == "Everkeep")
check("encounter duration mm:ss", enc["duration"] == "00:12")
check("encounter DURATION secs", enc["DURATION"] == 12)
check("encounter total damage", enc["damage"] == 75500)
check("encounter deaths", enc["deaths"] == 1)
check("encounter maxhit", enc["maxhit"] == "Gravity-30000")
check("encounter encdps", abs(enc["encdps"] - 75500 / 12) < 1e-6)

comb = snap["Combatant"]
check("only players are listed", set(comb) == {ME_NAME, P2_NAME})

me = comb[ME_NAME]
check("me damage (incl. dot + carbuncle merge)", me["damage"] == 62500)
check("me job acronym", me["Job"] == "AST")
check("me swings (miss + hallowed count)", me["swings"] == 6)
check("me hits (miss/hallowed excluded)", me["hits"] == 4)
check("me crits", me["crithits"] == 2)
check("me crit pct", abs(me["crithit%"] - 50.0) < 1e-6)
check("me dh pct", abs(me["DirectHitPct"] - 25.0) < 1e-6)
check("me crit-dh pct", abs(me["CritDirectHitPct"] - 25.0) < 1e-6)
check("me maxhit", me["maxhit"] == "Gravity-30000")
check("me damagetaken", me["damagetaken"] == 4000)
check("me dps uses own activity window", abs(me["dps"] - 62500 / 6) < 1e-6)
check("me encdps uses encounter length", abs(me["encdps"] - 62500 / 12) < 1e-6)

p2 = comb[P2_NAME]
check("p2 damage (aoe + eos merge)", p2["damage"] == 13000)
check("p2 job acronym", p2["Job"] == "MCH")
check("p2 swings (2 aoe lines + pet line)", p2["swings"] == 3)
check("p2 hits", p2["hits"] == 3)
check("p2 healed from HoT tick", p2["healed"] == 700)
check("p2 deaths", p2["deaths"] == 1)
check("p2 damagetaken from enemy dot", p2["damagetaken"] == 500)
check("p2 dps", abs(p2["dps"] - 3250.0) < 1e-6)
check("p2 enchps", abs(p2["enchps"] - 700 / 12) < 1e-6)

check("damage shares sum to 100",
      abs(me["damage%"] + p2["damage%"] - 100.0) < 1e-6)

rows = m.overlay_rows()
check("overlay rows sorted by encdps desc",
      [r[0] for r in rows] == [ME_NAME, P2_NAME])
check("overlay row shape",
      rows[0] == [ME_NAME, "AST", round(62500 / 12, 1),
                  round(62500 / 75500 * 100, 1), 0.0, True, 0])
check("overlay row shape (hps + not self + deaths)",
      rows[1] == [P2_NAME, "MCH", round(13000 / 12, 1),
                  round(13000 / 75500 * 100, 1), round(700 / 12, 1), False, 1])

m.set_in_combat(False, True)
check("finalize on combat flag drop fires once", len(ended) == 1)
check("encounter cleared after finalize", m.current is None)
final = ended[0]
check("final snapshot is inactive", final["isActive"] is False)
check("final snapshot totals match live",
      final["Encounter"]["damage"] == 75500
      and final["Combatant"][ME_NAME]["damage"] == 62500)
check("final duration trims to last combat action (t=11, not finalize t=12)",
      final["Encounter"]["DURATION"] == 11
      and final["Encounter"]["duration"] == "00:11")

# ── lifecycle edges ──────────────────────────────────────────────────────
# Empty encounter: no callback.
clk2 = Clock()
m2 = DpsMeter(clock=clk2)
ended2 = []
m2.on_encounter_end = ended2.append
roster(m2)
m2.set_in_combat(True, True)
m2.set_in_combat(False, False)
check("empty encounter produces no callback", ended2 == [])

# Combat-flag edges: either flag starts, either flag ends. inGameCombat alone
# (a striking-dummy parse ACT may not count) still bounds an encounter, and
# game drops split pulls even when ACT holds its own flag high.
m2b = DpsMeter(clock=Clock())
ended2b = []
m2b.on_encounter_end = ended2b.append
roster(m2b)
m2b.set_in_combat(False, True)                       # game combat only
m2b.process(ability("21", ME, ME_NAME, "Glare", BOSS, "Zeromus",
                    [("750003", dmg(1000))]), "")
m2b.set_in_combat(False, False)
check("game-only combat bounds an encounter",
      len(ended2b) == 1
      and ended2b[0]["Combatant"][ME_NAME]["damage"] == 1000)
m2c = DpsMeter(clock=Clock())
ended2c = []
m2c.on_encounter_end = ended2c.append
roster(m2c)
for _ in range(2):                                   # act pinned high both pulls
    m2c.set_in_combat(True, True)
    m2c.process(ability("21", ME, ME_NAME, "Glare", BOSS, "Zeromus",
                        [("750003", dmg(1000))]), "")
    m2c.set_in_combat(True, False)
check("game drop splits pulls while ACT flag stays high", len(ended2c) == 2)

# A mixed message, one flag falling while the other rises, finalizes the open
# encounter before the new begin. The two pulls never merge.
m2d = DpsMeter(clock=Clock())
ended2d = []
m2d.on_encounter_end = ended2d.append
roster(m2d)
m2d.set_in_combat(True, False)                       # act combat only
m2d.process(ability("21", ME, ME_NAME, "Glare", BOSS, "Zeromus",
                    [("750003", dmg(1000))]), "")
m2d.set_in_combat(False, True)                       # act falls as game rises
check("mixed edge finalizes the old encounter",
      len(ended2d) == 1
      and ended2d[0]["Combatant"][ME_NAME]["damage"] == 1000)
check("mixed edge opens a fresh encounter", m2d.current is not None)
m2d.process(ability("21", ME, ME_NAME, "Glare", BOSS, "Zeromus",
                    [("750003", dmg(2000))]), "")
m2d.set_in_combat(False, False)
check("the post-edge pull stands alone",
      len(ended2d) == 2
      and ended2d[1]["Combatant"][ME_NAME]["damage"] == 2000)

# Pre-pull buffs (status-only 21) must not open an encounter. The first real
# combat effect does (lazy ACT-style aggression start, no InCombat needed).
m3 = DpsMeter(clock=Clock())
ended3 = []
m3.on_encounter_end = ended3.append
roster(m3)
m3.process(ability("21", ME, ME_NAME, "Sprint", ME, ME_NAME,
                   [("1E00000E", "320000")]), "")
check("status-only ability does not start an encounter", m3.current is None)
m3.process(ability("21", ME, ME_NAME, "Physick", P2, P2_NAME,
                   [("750004", dmg(900))]), "")
check("a pre-pull heal does not start an encounter", m3.current is None)
m3.process(dot("HoT", ME, ME_NAME, "2BC", P2, P2_NAME), "")
check("a pre-pull HoT tick does not start an encounter", m3.current is None)
m3.process(ability("21", ME, ME_NAME, "Glare", BOSS, "Zeromus",
                   [("750003", dmg(1000))]), "")
check("first combat effect starts encounter lazily", m3.current is not None)

# Wipe finalizes.
m3.process(["33", "ts", "80034E52", "4000000F", "00", "00", "00", "00"], "")
check("wipe (33/4000000F) finalizes", len(ended3) == 1 and m3.current is None)

# Zone change finalizes and resets actor knowledge. Every 01 counts. A
# same-name re-entry is how repeated pulls of one instance actually arrive,
# and a resubscribe replay only reaches a fresh app (nothing open to split).
m4 = DpsMeter(clock=Clock())
ended4 = []
m4.on_encounter_end = ended4.append
roster(m4)
m4.process(ability("21", ME, ME_NAME, "Glare", BOSS, "Zeromus",
                   [("750003", dmg(1000))]), "")
m4.process(["01", "ts", "4B0", "Everkeep"], "")
check("even a same-name 01 finalizes (instance re-entry = new pull)",
      len(ended4) == 1 and m4.current is None)
# The zone change also cleared the local player id. A damage line from the
# stale id before the fresh 02 must not open a phantom encounter.
m4.process(ability("21", ME, ME_NAME, "Glare", BOSS, "Zeromus",
                   [("750003", dmg(1000))]), "")
check("a line from the stale me id opens nothing after zoning",
      m4.current is None)
# Real feeds send a fresh 02 after zoning, pinning the local player again.
m4.process(["02", "ts", ME, ME_NAME], "")
m4.process(ability("21", ME, ME_NAME, "Glare", BOSS, "Zeromus",
                   [("750003", dmg(1000))]), "")
m4.process(["01", "ts", "4B1", "The Voidcast Dais"], "")
check("zone change finalizes", len(ended4) == 2 and m4.current is None)
check("zone change title was the old zone",
      ended4[1]["Encounter"]["title"] == "Everkeep")
# Jobs were cleared. The same player id without a fresh 03 is no longer a
# player, so its line involves no player and opens nothing.
m4.process(ability("21", P2, P2_NAME, "Auto Crossbow", BOSS, "Zeromus",
                   [("750003", dmg(5000))]), "")
check("zone change clears job knowledge", m4.current is None)

# Malformed lines never raise and never corrupt state.
m5 = DpsMeter(clock=Clock())
for junk in (["21"], ["21", "only"], ["24", "ts"], ["03", "ts", "ZZ", "Name"],
             ["", ""], []):
    m5.process(junk, "junk")
m5.set_in_combat(True, False)
m5.set_in_combat(False, False)
check("malformed lines are survived", m5.current is None)

# A truncated bare 01 is junk, not a zone change. With a pull open it must
# not finalize the encounter or wipe actor knowledge.
m5b = DpsMeter(clock=Clock())
roster(m5b)
m5b.process(ability("21", ME, ME_NAME, "Glare", BOSS, "Zeromus",
                    [("750003", dmg(1000))]), "")
m5b.process(["01"], "")
check("a bare 01 keeps the open pull and the roster",
      m5b.current is not None and len(m5b._jobs) == 2)

# A pet's line must never name the owner's row after the pet (real logs start
# a pull with the pet acting before its owner). The name comes from the 03
# roster, or from the ownerName trailing the 21 line when there is no 03.
m6 = DpsMeter(clock=Clock())
roster(m6)
m6.process(ability("21", PET, "Eos", "Rock Buster", BOSS, "Zeromus",
                   [("750003", dmg(3000))]), "")
snap6 = m6.snapshot()
check("pet-first row is named after the owner (03 roster)",
      P2_NAME in snap6["Combatant"]
      and snap6["Combatant"][P2_NAME]["damage"] == 3000)
m7 = DpsMeter(clock=Clock())
m7.process(["01", "ts", "4B0", "Everkeep"], "")
m7.process(["02", "ts", ME, ME_NAME], "")
m7.process(ability("21", PET2, "Carbuncle", "Gouge", BOSS, "Zeromus",
                   [("750003", dmg(1500))], owner=ME, owner_name=ME_NAME), "")
snap7 = m7.snapshot()
check("pet-first row is named after the owner (21 ownerName)",
      list(snap7["Combatant"]) == [ME_NAME]
      and snap7["Combatant"][ME_NAME]["damage"] == 1500)

# Enemy damage on a pet must not land on the owner as damage taken. ACT
# credits pet damage taken to no one, same as pet deaths. Both the ability
# path and the DoT path.
m8 = DpsMeter(clock=Clock())
roster(m8)
m8.set_in_combat(True, True)
m8.process(ability("21", BOSS, "Zeromus", "Void Bolt", PET, "Eos",
                   [("750003", dmg(2000))]), "")
m8.process(dot("DoT", PET, "Eos", "1F4", BOSS, "Zeromus"), "")
m8.process(ability("21", BOSS, "Zeromus", "Void Bolt", P2, P2_NAME,
                   [("750003", dmg(700))]), "")
check("enemy hits on a pet do not inflate the owner's damage taken",
      m8.snapshot()["Combatant"][P2_NAME]["damagetaken"] == 700)

# A DoT tick the applier lands on itself credits damage only, never damage
# taken. ACT excludes self damage from taken, same rule as the ability path.
m9 = DpsMeter(clock=Clock())
roster(m9)
m9.set_in_combat(True, True)
m9.process(dot("DoT", ME, ME_NAME, "1F4", ME, ME_NAME), "")    # 500 self tick
me9 = m9.snapshot()["Combatant"][ME_NAME]
check("a self dot tick credits damage but never damage taken",
      me9["damage"] == 500 and me9["damagetaken"] == 0)

# ── damage-idle pause, display reset, full-pull capture ──────────────────
# The on-screen view pauses after the idle timeout and resets on the next
# hit. The encounter itself always keeps the whole pull.
pclk = Clock()
mp = DpsMeter(clock=pclk)
endedp = []
mp.on_encounter_end = endedp.append
roster(mp)
pclk.set(0)
mp.set_in_combat(True, True)
mp.process(ability("21", ME, ME_NAME, "Glare", BOSS, "Zeromus",
                   [("750003", dmg(10000))]), "")
pclk.set(10)
mp.process(ability("21", ME, ME_NAME, "Glare", BOSS, "Zeromus",
                   [("750003", dmg(10000))]), "")
pclk.set(100)
check("clock runs while damage flows",
      mp.snapshot()["Encounter"]["DURATION"] == 100)
pclk.set(130)
check("clock still running at the pause edge",
      mp.snapshot()["Encounter"]["DURATION"] == 130)
pclk.set(5000)
snap = mp.snapshot()
check("clock pauses at the idle timeout",
      snap["Encounter"]["DURATION"] == 130)
check("paused clock freezes encdps",
      abs(snap["Encounter"]["encdps"] - 20000 / 130) < 1e-6)
check("overlay rows show the frozen segment",
      mp.overlay_rows()[0][2] == round(20000 / 130, 1))
# Damage resumes past the gap. The display resets to a fresh segment.
pclk.set(5001)
mp.process(ability("21", ME, ME_NAME, "Glare", BOSS, "Zeromus",
                   [("750003", dmg(10000))]), "")
snap = mp.snapshot()
check("view resets on damage after the gap",
      snap["Encounter"]["damage"] == 10000)
check("new segment clock starts at zero",
      snap["Encounter"]["DURATION"] == 0)
pclk.set(5002)
mp.process(ability("21", ME, ME_NAME, "Glare", BOSS, "Zeromus",
                   [("750003", dmg(10000))]), "")
snap = mp.snapshot()
check("new segment ticks", snap["Encounter"]["DURATION"] == 1)
check("new segment totals", snap["Encounter"]["damage"] == 20000)
# The pull itself was never split. The final log keeps everything,
# downtime included.
pclk.set(5003)
mp.set_in_combat(False, False)
check("finalize fires for the gapped fight", len(endedp) == 1)
final = endedp[0]
check("the log keeps the whole pull",
      final["Encounter"]["damage"] == 40000
      and final["Encounter"]["DURATION"] == 5002)
preserved = mp.snapshot()
check("preserved display is the full pull",
      preserved["isActive"] is False
      and preserved["Encounter"]["damage"] == 40000)
check("no overlay rows while idle between pulls", mp.overlay_rows() == [])
pclk.set(6000)
mp.process(ability("21", ME, ME_NAME, "Glare", BOSS, "Zeromus",
                   [("750003", dmg(1000))]), "")
snap_new = mp.snapshot()
check("a new pull replaces the preserved one",
      snap_new["isActive"] is True
      and snap_new["Encounter"]["damage"] == 1000)

# The timeout is configurable: 30s here.
sclk = Clock()
ms = DpsMeter(clock=sclk)
ms.set_idle_timeout(30)
roster(ms)
sclk.set(0)
ms.set_in_combat(True, True)
ms.process(ability("21", ME, ME_NAME, "Glare", BOSS, "Zeromus",
                   [("750003", dmg(10000))]), "")
sclk.set(10)
ms.process(ability("21", ME, ME_NAME, "Glare", BOSS, "Zeromus",
                   [("750003", dmg(10000))]), "")
sclk.set(41)
check("custom timeout pauses the view",
      ms.snapshot()["Encounter"]["DURATION"] == 40)
sclk.set(100)
ms.process(ability("21", ME, ME_NAME, "Glare", BOSS, "Zeromus",
                   [("750003", dmg(5000))]), "")
check("custom timeout resets the view",
      ms.snapshot()["Encounter"]["damage"] == 5000)

# Heals are not damage. A heal-only stretch neither pauses-early nor resets.
hclk = Clock()
mh = DpsMeter(clock=hclk)
roster(mh)
hclk.set(0)
mh.set_in_combat(True, True)
mh.process(ability("21", ME, ME_NAME, "Glare", BOSS, "Zeromus",
                   [("750003", dmg(10000))]), "")
hclk.set(100)
mh.process(dot("HoT", ME, ME_NAME, "2BC", P2, P2_NAME), "")
hclk.set(500)
check("a HoT neither holds nor resets the view",
      mh.snapshot()["Encounter"]["DURATION"] == 120
      and mh.snapshot()["Encounter"]["damage"] == 10000)

# ── dps_frame contract (plugin_link) ─────────────────────────────────────
frame = pl.dps_frame({"t": "Everkeep", "d": "00:12", "dps": 6291.7}, rows)
check("dps frame command + show", frame["c"] == "dps" and frame["show"] is True)
check("dps frame enc shape",
      frame["enc"] == {"t": "Everkeep", "d": "00:12", "dps": 6291.7})
check("dps frame rows shape",
      frame["rows"][0] == [ME_NAME, "AST", round(62500 / 12, 1),
                           round(62500 / 75500 * 100, 1), 0.0, True, 0])
big = [["n" + str(i), "JOB", 1.0, 1.0] for i in range(30)]
check("dps frame caps rows at MAX_OVERLAY_ROWS",
      len(pl.dps_frame({}, big)["rows"]) == 24)
check("dps frame defaults the trailing fields for old 4-field rows",
      pl.dps_frame({}, big)["rows"][0] == ["n0", "JOB", 1.0, 1.0, 0.0, False, 0])
check("dps frame hide", pl.dps_frame(None, [], show=False)
      == {"c": "dps", "show": False})

# ── roster feeds from outside the log stream ───────────────────────────────
# A mid-instance connect gets no 03 burst. The WS PartyChanged jobs and the
# ChangePrimaryPlayer id pin the party instead, so its damage credits.
m10 = DpsMeter(clock=Clock())
m10.process(["01", "ts", "4B0", "Everkeep"], "")
m10.set_me(int(ME, 16))
m10.note_job(int(P2, 16), 31)          # decimal job id, the WS feed's shape
m10.process(ability("21", P2, P2_NAME, "Auto Crossbow", BOSS, "Zeromus",
                    [("750003", dmg(5000))]), "")
snap10 = m10.snapshot()
check("ws roster feed credits the party without any 03",
      P2_NAME in snap10["Combatant"]
      and snap10["Combatant"][P2_NAME]["damage"] == 5000)
check("ws roster feed jobs show on the row",
      snap10["Combatant"][P2_NAME]["Job"] == "MCH")
check("ws primary player pins the self id", m10._me_id == int(ME, 16))
m10.set_me(0)
m10.set_me("not an id")
check("a blank or malformed ws id never clobbers the self id",
      m10._me_id == int(ME, 16))
m10.note_job(int(BOSS, 16), 0)
check("a zero job notes nothing", int(BOSS, 16) not in m10._jobs)

# ── actor-map trim evicts the stalest, not the first arrival ───────────────
m11 = DpsMeter(clock=Clock())
m11._note(m11._jobs, 1, 33)
for i in range(2, 1100):
    m11._note(m11._jobs, i, 1)
check("the cap evicts a never re-noted actor", 1 not in m11._jobs)

m12 = DpsMeter(clock=Clock())
m12._note(m12._jobs, 1, 33)
for i in range(2, 600):
    m12._note(m12._jobs, i, 1)
m12._note(m12._jobs, 1, 33)            # seen again, fresh again
for i in range(600, 1200):
    m12._note(m12._jobs, i, 1)
check("a re-noted actor survives the flood", m12._jobs.get(1) == 33)
check("the trim still holds the cap", len(m12._jobs) == 1024)

# End to end: an early-noted party member keeps crediting damage through a
# passer-by flood because the roster burst re-noted them in the middle.
m13 = DpsMeter(clock=Clock())
m13.process(["01", "ts", "4B0", "Everkeep"], "")
m13.process(["02", "ts", ME, ME_NAME], "")
m13.process(["03", "ts", P2, P2_NAME, "1F", "5A", "0000"], "")
strangers = [f"{0x20000000 + i:X}" for i in range(1100)]
for s in strangers[:550]:
    m13.process(["03", "ts", s, "Stranger", "01", "5A", "0000"], "")
m13.note_job(int(P2, 16), 31)          # the WS burst re-notices the party
for s in strangers[550:]:
    m13.process(["03", "ts", s, "Stranger", "01", "5A", "0000"], "")
m13.set_in_combat(True, True)
m13.process(ability("21", P2, P2_NAME, "Auto Crossbow", BOSS, "Zeromus",
                    [("750003", dmg(5000))]), "")
check("a re-noted party member still credits after the flood",
      m13.snapshot()["Combatant"].get(P2_NAME, {}).get("damage") == 5000)

# The roster burst can land after a pet line already opened the owner's row
# at job 0, a mid-instance connect where the 03 lines never came. note_job
# must re-run the late upgrade, or the job cell stays blank until the owner
# personally acts.
m14 = DpsMeter(clock=Clock())
m14.process(["01", "ts", "4B0", "Everkeep"], "")
m14.set_me(int(ME, 16))
m14.process(ability("21", PET, "Eos", "Rock Buster", BOSS, "Zeromus",
                    [("750003", dmg(3000))], owner=ME, owner_name=ME_NAME), "")
check("a pet-opened row of a jobless self starts at job 0",
      m14.current.combatants[int(ME, 16)].job == 0)
m14.note_job(int(ME, 16), 33)          # the WS burst lands late
check("a late roster job upgrades the pet-opened row",
      m14.current.combatants[int(ME, 16)].job == 33)
check("the view row upgrades too",
      m14._view.combatants[int(ME, 16)].job == 33)
check("the late job shows on the snapshot row",
      m14.snapshot()["Combatant"][ME_NAME]["Job"] == "AST")

# The upgrade fills blanks only. A row that already carries a job keeps it,
# even when a stale roster note disagrees.
m15 = DpsMeter(clock=Clock())
roster(m15)
m15.set_in_combat(True, True)
m15.process(ability("21", P2, P2_NAME, "Auto Crossbow", BOSS, "Zeromus",
                    [("750003", dmg(5000))]), "")
m15.note_job(int(P2, 16), 36)
check("a roster note never clobbers a known job",
      m15.current.combatants[int(P2, 16)].job == 31)

# ── Trust NPCs with real ClassJob ids are not noted as players ────────────
# Duty support and Trust NPCs carry real ClassJob hex on their 03 lines, so
# the meter filters the line on the '10' player prefix like main_window does,
# or their damage lands as rows in the meter and overlay.
m16 = DpsMeter(clock=Clock())
m16.process(["01", "ts", "4B0", "Everkeep"], "")
m16.process(["03", "ts", "4000C001", "Thancred", "17", "5A", "0000"], "")
m16.process(["03", "ts", ME, ME_NAME, "21", "5A", "0000"], "")
check("a trust NPC job is not noted", int("4000C001", 16) not in m16._jobs)
check("a player job is still noted", m16._jobs.get(int(ME, 16)) == 33)

# ── a blank or garbage 02 id keeps the pinned self id ──────────────────────
# from_dict and the WS feed both ignore unusable ids on the self pin. The
# next valid 02 line can still correct the pin and notes the name.
m17 = DpsMeter(clock=Clock())
m17.process(["01", "ts", "4B0", "Everkeep"], "")
m17.process(["02", "ts", ME, ME_NAME], "")
m17.process(["02", "ts", "", ""], "")
check("a blank 02 id keeps the pinned self id",
      m17._me_id == int(ME, 16))
m17.process(["02", "ts", "zzz", "Garbage Player"], "")
check("a garbage 02 id keeps the pinned self id too",
      m17._me_id == int(ME, 16))
m17.process(["02", "ts", P2, P2_NAME], "")
check("a valid 02 line corrects the pin",
      m17._me_id == int(P2, 16))
check("the corrected pin notes the new name",
      m17._names.get(int(P2, 16)) == P2_NAME)

print()
if FAILS:
    print(f"{len(FAILS)} FAILED: {', '.join(FAILS)}")
    sys.exit(1)
print("all tests passed")
