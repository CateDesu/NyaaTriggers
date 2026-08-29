"""Regression test for the ja callout builder's hand-edit tolerance.

tools/build_callouts_ja.py reads triggers.json and the phrase/name maps
assuming dicts of strings. A hand edited file with a truthy non-string
field crashed the build with AttributeError instead of dropping the junk
entry, and a non-dict or non-list top level crashed it outright. The
builder now coerces junk fields to empty and degrades bad top levels to
empty maps.

Run directly:  python test_build_callouts_ja.py   (exit 0 = all pass)
"""
import json
import os
import sys
import tempfile
from pathlib import Path

_REPO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _REPO)
sys.path.insert(0, os.path.join(_REPO, "tools"))

import build_callouts_ja as b

FAILS = []


def check(name, cond):
    print(("PASS  " if cond else "FAIL  ") + name)
    if not cond:
        FAILS.append(name)


check("_str_field strips a string", b._str_field("  x ") == "x")
check("_str_field coerces junk to empty",
      b._str_field(5) == "" and b._str_field(None) == "" and b._str_field(["a"]) == "")

m = b._norm_phrase_map({"Good": {"display": "いい", "reading": "いい"},
                        "Junk": {"display": 5, "reading": "x"},
                        "Bare": "テスト",
                        "List": [1, 2]})
check("junk phrase entries drop out", "Junk" not in m and "List" not in m)
check("good phrase entries survive",
      m.get("Good") == ("いい", "いい") and m.get("Bare") == ("テスト", "テスト"))


def run_main(triggers, phrases):
    """main() against temp inputs, with every path constant redirected."""
    tmp = Path(tempfile.mkdtemp())
    (tmp / "triggers.json").write_text(json.dumps(triggers), encoding="utf-8")
    (tmp / "phrases.json").write_text(json.dumps(phrases), encoding="utf-8")
    saved = (b._TRIGGERS, b._PHRASES, b._NAMES, b._OUT)
    b._TRIGGERS = tmp / "triggers.json"
    b._PHRASES = tmp / "phrases.json"
    b._NAMES = tmp / "missing_names.json"
    b._OUT = tmp / "out.json"
    try:
        rc = b.main()
        out = json.loads(b._OUT.read_text(encoding="utf-8"))
    finally:
        b._TRIGGERS, b._PHRASES, b._NAMES, b._OUT = saved
    return rc, out


rc, out = run_main(
    [{"id": "t1", "name": "Rampart", "tts_text": "Good"},
     {"id": "t2", "name": 5, "tts_text": ["x"]},
     "a bare string"],
    {"Good": {"display": "いい", "reading": "いい"},
     "Junk": {"display": 5, "reading": "x"}})
check("junk fields do not crash the build", rc == 0)
check("only the well-formed callout is built", out["callouts"] == {"t1": "いい"})

rc, out = run_main(5, [1, 2])
check("scalar and list top levels degrade to empty maps",
      rc == 0 and out["callouts"] == {} and out["phrases"] == {})

print()
if FAILS:
    print(f"{len(FAILS)} FAILED: {', '.join(FAILS)}")
    sys.exit(1)
print("all tests passed")
