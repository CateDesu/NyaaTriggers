"""Tests for the pull-log store (dps_store.py): chunked JSONL logs. A log is
full after 25 pulls of one fight or 5 distinct fights. Once 5 full logs
sit in the folder the oldest are culled, and the active log never counts.
The caps are patched small so the tests stay fast. Temp dirs only. No Qt,
no game.

Run:  python test_dps_store.py   (exit 0 = all pass)
"""
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import dps_store

FAILS = []


def check(name, cond):
    print(("PASS  " if cond else "FAIL  ") + name)
    if not cond:
        FAILS.append(name)


def pull(title, stamp="2026-08-05_22-00-00"):
    return {"title": title, "zone": title, "duration": "03:12",
            "encdps": 1234.5, "started": stamp, "updated": stamp,
            "combatants": []}


def read_lines(path):
    return [l for l in Path(path).read_text(encoding="utf-8").splitlines()
            if l.strip()]


def titles_in(path):
    return [json.loads(l)["title"] for l in read_lines(path)]


def logs_in(d):
    return sorted(d.glob("*.jsonl"))


BASE = datetime(2026, 8, 5, 22, 0, 0)


def at(sec):
    return BASE + timedelta(seconds=sec)


# ── basic layout: one active log, fights mixed inside ─────────────────────
with tempfile.TemporaryDirectory() as tmp:
    d = Path(tmp) / "logs"
    p1 = dps_store.write_pull(d, pull("Everkeep"), when=at(0))
    check("log file created",
          p1.name == "2026-08-05_22-00-00.jsonl" and p1.exists())
    p2 = dps_store.write_pull(d, pull("The Voidcast Dais"), when=at(1))
    check("fights share the active log", p2 == p1 and len(read_lines(p1)) == 2)
    check("lines carry their own fight titles",
          titles_in(p1) == ["Everkeep", "The Voidcast Dais"])
    dps_store.write_pull(d, pull("極ゼロムス討滅戦"), when=at(2))
    check("unicode title round-trips",
          titles_in(p1)[-1] == "極ゼロムス討滅戦")

# ── roll-over and retention, with small patched caps ──────────────────────
saved = (dps_store.MAX_PULLS_PER_LOG, dps_store.MAX_FIGHTS_PER_LOG,
         dps_store.MAX_LOGS)
dps_store.MAX_PULLS_PER_LOG = 3
dps_store.MAX_FIGHTS_PER_LOG = 2
dps_store.MAX_LOGS = 3
try:
    # Pull cap: a log fills at 3 pulls of the same fight, then rolls.
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp) / "logs"
        for i in range(3):
            dps_store.write_pull(d, pull("Everkeep"), when=at(i))
        check("three pulls fit one log", len(logs_in(d)) == 1)
        dps_store.write_pull(d, pull("Everkeep"), when=at(3))
        files = logs_in(d)
        check("the pull past the cap rolls a new log", len(files) == 2)
        check("the full log holds exactly the cap",
              len(read_lines(files[0])) == 3)
        check("the new log starts the fight over",
              titles_in(files[1]) == ["Everkeep"])

        # Fight cap on the fresh log: 2 distinct fights fit, the 3rd rolls,
        # and a roll in the same second still gets an ordered name.
        dps_store.write_pull(d, pull("The Voidcast Dais"), when=at(3))
        check("second fight joins the active log",
              titles_in(files[1]) == ["Everkeep", "The Voidcast Dais"])
        rolled = dps_store.write_pull(d, pull("Everkeep EX"), when=at(3))
        check("a new fight past the cap rolls a new log",
              rolled.name == "2026-08-05_22-00-03_001.jsonl")
        check("same-second roll sorts after the base name",
              logs_in(d)[-1] == rolled)
        check("the rolled log holds the new fight",
              titles_in(rolled) == ["Everkeep EX"])

    # Retention: only 3 full logs survive. The active one never counts.
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp) / "logs"
        # Ten one-pull fights: with cap 2 per log this fills 5 logs.
        for i in range(10):
            dps_store.write_pull(d, pull(f"Fight {i:02d}"), when=at(i))
        files = logs_in(d)
        check("four logs remain after culling", len(files) == 4)
        check("oldest full log culled at the cap",
              not (d / "2026-08-05_22-00-00.jsonl").exists())
        check("retired full logs kept at the cap",
              len(files) == dps_store.MAX_LOGS + 1)
        dps_store.write_pull(d, pull("Fight 10"), when=at(10))
        files = logs_in(d)
        check("culling keeps pace as logs fill",
              len(files) == dps_store.MAX_LOGS + 1
              and not (d / "2026-08-05_22-00-02.jsonl").exists())
finally:
    (dps_store.MAX_PULLS_PER_LOG, dps_store.MAX_FIGHTS_PER_LOG,
     dps_store.MAX_LOGS) = saved

# ── robustness: corrupt lines, foreign files, under-cap no-op ─────────────
with tempfile.TemporaryDirectory() as tmp:
    d = Path(tmp) / "logs"
    d.mkdir(parents=True)
    (d / "2026-08-01_10-00-00.jsonl").write_text("not json at all\n",
                                                 encoding="utf-8")
    (d / "legacy.json").write_text("{}\n", encoding="utf-8")
    (d / "notes.txt").write_text("hello\n", encoding="utf-8")
    p = dps_store.write_pull(d, pull("Everkeep"), when=at(2))
    check("corrupt lines survive as Unknown",
          "not json at all" in p.read_text(encoding="utf-8"))
    check("non-jsonl files untouched",
          (d / "legacy.json").exists() and (d / "notes.txt").exists())
    before = p.read_text(encoding="utf-8")
    dps_store.enforce_retention(d)
    check("under-cap retention is a no-op",
          p.read_text(encoding="utf-8") == before)

# ── a pre-existing 0644 log is tightened to owner-only on the next write ──
with tempfile.TemporaryDirectory() as tmp:
    d = Path(tmp) / "logs"
    p = dps_store.write_pull(d, pull("Everkeep"), when=at(7200))
    os.chmod(p, 0o644)   # a restored backup or a pre-hardening file
    dps_store._perms_tightened = False
    dps_store.write_pull(d, pull("Everkeep"), when=at(7201))
    check("0644 log tightened to 0600 on append",
          (p.stat().st_mode & 0o777) == 0o600)

print()
if FAILS:
    print(f"{len(FAILS)} FAILED: {', '.join(FAILS)}")
    sys.exit(1)
print("all tests passed")
