"""Regression test for the cactbot relay payload guard.

A page on a user configured cactbot_url can call the harvest bridge with
arbitrary JSON. A truthy non-string text raised AttributeError in the Qt
slot, which lost the callout and wrote a CRASH log line. The slot now
coerces non-strings to empty and drops the payload.

Run directly:  python test_cactbot_reader.py   (exit 0 = all pass)
"""
import json
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PyQt6.QtCore import QCoreApplication

from cactbot_reader import CactbotReader

FAILS = []


def check(name, cond):
    print(("PASS  " if cond else "FAIL  ") + name)
    if not cond:
        FAILS.append(name)


_app = QCoreApplication.instance() or QCoreApplication(sys.argv)
r = CactbotReader()
callouts, spoken = [], []
r.callout.connect(lambda text, tier: callouts.append((text, tier)))
r.tts.connect(spoken.append)

r._on_message("popup", json.dumps({"text": 5}))
r._on_message("say", json.dumps({"text": ["a"]}))
check("non-string popup text is dropped without raising", callouts == [])
check("non-string say text is dropped without raising", spoken == [])

r._on_message("popup", json.dumps({"text": "  Tank buster  ", "tier": "alert"}))
check("string popup text still lands", callouts == [("Tank buster", "alert")])
r._on_message("say", json.dumps({"text": "Spread"}))
check("string say text still lands", spoken == ["Spread"])

r._on_message("popup", json.dumps({"text": None}))
check("null text is dropped", callouts == [("Tank buster", "alert")])

# ── non subscribe status dicts go to the drop log, not stderr ────────────
# The injected JS reports bridge ready, hook results and observer state as
# status events. Printing each one spammed stderr on every page load.
import cactbot_reader

drops = []
_real_drop = cactbot_reader.log_drop
cactbot_reader.log_drop = lambda site, detail, *a, **k: drops.append((site, detail))
try:
    statuses = []
    r.status.connect(lambda ok, msg: statuses.append((ok, msg)))
    r._on_message("status", json.dumps({"event": "hook", "result": "ok"}))
    check("non subscribe status goes to the drop log",
          drops and drops[-1][0] == "cactbot")
    check("a non subscribe status emits no signal", statuses == [])
    r._on_message("status", json.dumps({"event": "subscribe"}))
    check("subscribe still emits the connected status",
          statuses == [(True, "Connected to IINACT")])
    check("subscribe logs no drop", len(drops) == 1)
finally:
    cactbot_reader.log_drop = _real_drop

print()
if FAILS:
    print(f"{len(FAILS)} FAILED: {', '.join(FAILS)}")
    sys.exit(1)
print("all tests passed")
