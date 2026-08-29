"""Every shipped zone_regex must match a real zone, and localized clients must
still fire Local triggers.

A zone pattern that matches no zone in the game is a whole fight's worth of
triggers that can never fire, with nothing to see in the UI: the rows are there,
the toggle is on, and the callout simply never comes. Six of them shipped at
once (Queen EX, Enuo EX, Zelenia EX, Doomtrain EX, Zeromus EX, Ridorana), so
this guards the class rather than the instances.

Run directly:  python test_zone_patterns.py   (exit 0 = all pass)
"""
import json
import os
import re
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

FAILS = []


def check(name, cond):
    print(("PASS  " if cond else "FAIL  ") + name)
    if not cond:
        FAILS.append(name)


ZONES = json.loads((HERE / "zone_names.json").read_text(encoding="utf-8"))
TRIGGERS = json.loads((HERE / "triggers.json").read_text(encoding="utf-8"))

check("zone_names.json carries a full zone table", len(ZONES) > 500)

# ── every shipped zone pattern hits at least one real zone ─────────────────
names = list(ZONES.values())
dead, uncompilable = [], []
patterns = sorted({t["zone_regex"] for t in TRIGGERS if t.get("zone_regex")})
for pat in patterns:
    try:
        rx = re.compile(pat, re.IGNORECASE)
    except re.error as exc:
        uncompilable.append(f"{pat!r}: {exc}")
        continue
    if not any(rx.search(n) for n in names):
        fights = sorted({t.get("fight", "") for t in TRIGGERS
                         if t.get("zone_regex") == pat})
        rows = sum(1 for t in TRIGGERS if t.get("zone_regex") == pat)
        dead.append(f"{pat!r} ({rows} triggers, fights={fights})")

check("every shipped zone_regex compiles", not uncompilable)
for u in uncompilable:
    print("        " + u)
check("every shipped zone_regex matches a real zone", not dead)
for d in dead:
    print("        DEAD: " + d)

# ── one pattern per fight. A stray variant is how dead ones creep in ───────
by_fight = {}
for t in TRIGGERS:
    if t.get("fight") and t.get("zone_regex"):
        by_fight.setdefault(t["fight"], set()).add(t["zone_regex"])
split = {f: sorted(p) for f, p in by_fight.items() if len(p) > 1}
check("each fight tag uses a single zone pattern", not split)
for f, p in split.items():
    print(f"        {f}: {p}")

# ── a localized client still fires Local triggers ──────────────────────────
import main_window as mw

check("canonical_zone_name resolves a known id",
      mw.canonical_zone_name(1226) == "AAC Light-heavyweight M1 (Savage)")
check("canonical_zone_name is empty for an unknown id",
      mw.canonical_zone_name(999999) == "")
check("canonical_zone_name survives junk", mw.canonical_zone_name("nope") == "")


class _Zoned:
    """Just the zone-alias half of MainWindow."""
    _set_zone_aliases = mw.MainWindow._set_zone_aliases
    _zone_matches = mw.MainWindow._zone_matches

    def __init__(self):
        self._match_zone = ""
        self._zone_aliases = ()


z = _Zoned()
rx_m1s = re.compile(r"Light-heavyweight M1 \(Savage\)", re.IGNORECASE)

z._set_zone_aliases("AAC Light-heavyweight M1 (Savage)", 1226)
check("English client still matches", z._zone_matches(rx_m1s))

z._set_zone_aliases("Poids mi-lourds CCA - match 1 (sadique)", 1226)
check("French client matches via the zone id", z._zone_matches(rx_m1s))
check("French client keeps its own name for display",
      z._zone_aliases[0] == "Poids mi-lourds CCA - match 1 (sadique)")

z._set_zone_aliases("至天の座アルカディア零式：ライトヘビー級1", 1226)
check("Japanese client matches via the zone id", z._zone_matches(rx_m1s))

z._set_zone_aliases("Some Unknown Duty", 0)
check("no id: falls back to the reported name",
      z._zone_matches(re.compile("Unknown Duty")) and z._match_zone == "Some Unknown Duty")

z._set_zone_aliases("", 0)
check("empty zone matches nothing", not z._zone_matches(re.compile(".")))

# ── the six fights that were dead now point at their real zones ───────────
EXPECTED = {
    "Queen EX":                "The Minstrel's Ballad: Sphene's Burden",
    "Enuo EX":                 "The Unmaking (Extreme)",
    "Zelenia EX":              "Recollection (Extreme)",
    "Doomtrain EX":            "Hell on Rails (Extreme)",
    "Zeromus EX":              "The Abyssal Fracture (Extreme)",
    "The Ridorana Lighthouse": "The Ridorana Lighthouse",
}
for fight, zone in EXPECTED.items():
    pats = by_fight.get(fight, set())
    ok = bool(pats) and all(re.search(p, zone, re.IGNORECASE) for p in pats)
    check(f"{fight} matches {zone!r}", ok)

print("\n" + (f"{len(FAILS)} FAILED: {FAILS}" if FAILS else "all passed"))
sys.exit(1 if FAILS else 0)
