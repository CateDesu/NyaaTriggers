"""Java callout declarations, repository mapping and conversion."""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nyaatriggers import convert_event_trigger
from nyaatriggers.convert_event_trigger import REPO_TO_FIGHT, convert_file

FAILS = []


def check(name, cond):
    print(("PASS  " if cond else "FAIL  ") + name)
    if not cond:
        FAILS.append(name)


def convert(src: str):
    with tempfile.TemporaryDirectory() as td:
        java = Path(td) / "fixture.java"
        java.write_text(src, encoding="utf-8")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            res = convert_file(java)
    return res, err.getvalue()


_HEADER = 'package test;\n\n@CalloutRepo(name = "M1S")\npublic class Fixture {\n'
_FOOTER = '\n}\n'

for repo, fight, ident in (("EX1", "Valigarmanda EX", "8FF0"),
                         ("EX2", "Zoraal Ja EX", "9398")):
    res, _ = convert(f'@CalloutRepo(name = "{repo}")\nclass Trial {{\n'
                     f'@NpcCastCallout(0x{ident})\n'
                     'private final ModifiableCallout<AbilityCastStart> cast = '
                     'ModifiableCallout.durationBasedCall("Ability", "Raidwide");\n}')
    check(f"{repo} maps to {fight}", len(res) == 1 and res[0]["fight"] == fight)

# the classic adjacent-line form still converts
res, _ = convert(_HEADER + '''
    @NpcCastCallout(0x8C01)
    private final ModifiableCallout<DurationBasedCallout> a =
        ModifiableCallout.durationBasedCall("Stack Label", "Stack");
''' + _FOOTER)
check("classic adjacent-line form converts",
      len(res) == 1 and res[0]["tts_text"] == "Stack"
      and res[0]["ability_id"] == "8C01" and res[0]["fight"] == "M1S")

# Same line declaration.
res, _ = convert(_HEADER + '''
    @NpcCastCallout(0x8C02) private final ModifiableCallout<DurationBasedCallout> b = ModifiableCallout.durationBasedCall("Spread Label", "Spread");
''' + _FOOTER)
check("same-line declaration converts",
      len(res) == 1 and res[0]["tts_text"] == "Spread")

# An intervening annotation.
res, _ = convert(_HEADER + '''
    @NpcCastCallout(0x8C03)
    @SuppressWarnings("unchecked")
    private final ModifiableCallout<DurationBasedCallout> c = ModifiableCallout.durationBasedCall("Out Label", "Out");
''' + _FOOTER)
check("intervening annotation converts",
      len(res) == 1 and res[0]["tts_text"] == "Out")

# a trailing line comment after the annotation still converts
res, _ = convert(_HEADER + '''
    @NpcCastCallout(0x8C05) // some note
    private final ModifiableCallout<DurationBasedCallout> e = ModifiableCallout.durationBasedCall("Knockback Label", "Knockback");
''' + _FOOTER)
check("trailing comment after the annotation converts",
      len(res) == 1 and res[0]["tts_text"] == "Knockback")

res, _ = convert(_HEADER + '''
    @NpcCastCallout(0x8C05)
    ////////////////////////////////////////
    private final ModifiableCallout<Object> e = new ModifiableCallout<>("Label", "Move");
''' + _FOOTER)
check("slash comments still allow the following callout", len(res) == 1)

with tempfile.TemporaryDirectory() as td:
    java = Path(td) / "slashes.java"
    java.write_text(_HEADER + '@NpcCastCallout(0x1234)\n'
                    + '/' * 10000 + '\nnot_a_callout;\n' + _FOOTER)
    code = ('from pathlib import Path; import sys; '
            'from nyaatriggers.convert_event_trigger import convert_file; '
            'assert convert_file(Path(sys.argv[1])) == []')
    try:
        result = subprocess.run([sys.executable, "-c", code, str(java)],
                                cwd=Path(__file__).resolve().parents[1],
                                capture_output=True, text=True, timeout=3)
        check("slash comments with no field finish within the deadline", result.returncode == 0)
    except subprocess.TimeoutExpired:
        check("slash comments with no field finish within the deadline", False)

# Nested generic arguments.
res, _ = convert(_HEADER + '''
    @NpcCastCallout(0x8C04)
    private final ModifiableCallout<List<DurationBasedCallout>> d = ModifiableCallout.durationBasedCall("In Label", "In");
''' + _FOOTER)
check("nested generic converts",
      len(res) == 1 and res[0]["tts_text"] == "In")

# the constructor RHS form still converts
res, _ = convert(_HEADER + '''
    @NpcCastCallout(0x8C06)
    private final ModifiableCallout<DurationBasedCallout> f = new ModifiableCallout<>("Draw Label", "Draw In");
''' + _FOOTER)
check("constructor RHS form converts",
      len(res) == 1 and res[0]["tts_text"] == "Draw In")

# the next trigger's annotation is never eaten by the gap scan
res, _ = convert(_HEADER + '''
    @NpcCastCallout(0x8C07)
    private final ModifiableCallout<DurationBasedCallout> g = ModifiableCallout.durationBasedCall("First", "First");
    @NpcCastCallout(0x8C08)
    private final ModifiableCallout<DurationBasedCallout> h = ModifiableCallout.durationBasedCall("Second", "Second");
''' + _FOOTER)
check("back to back triggers both convert with their own ids",
      [t["ability_id"] for t in res] == ["8C07", "8C08"])

# an unmapped repo warns instead of vanishing
res, err = convert('package test;\n\n@CalloutRepo(name = "Brand New Repo")\n'
                   'public class Unknown {\n}\n')
check("unmapped repo converts nothing", res == [])
check("unmapped repo warns with the repo name",
      "Brand New Repo" in err and "WARN" in err)

# An empty repository mapping is deliberately skipped.
res, err = convert('package test;\n\n@CalloutRepo(name = "Titan Gaols")\n'
                   'public class Jails {\n}\n')
check("intentional skip converts nothing", res == [])
check("intentional skip stays silent", "WARN" not in err)
check("skip entries are mapped empty",
      REPO_TO_FIGHT.get("Titan Gaols") == ""
      and REPO_TO_FIGHT.get("Dummy (/e c:testcall)") == "")

# overlapping id sets in one file collapse to the first
res, err = convert(_HEADER + '''
    @NpcCastCallout(0x8C01)
    private final ModifiableCallout<DurationBasedCallout> a =
        ModifiableCallout.durationBasedCall("Stack Label", "Stack");
    @NpcCastCallout(0x8C01, 0x8C02)
    private final ModifiableCallout<DurationBasedCallout> b =
        ModifiableCallout.durationBasedCall("Stack Or Spread Label", "Stack or Spread");
    @NpcCastCallout(0x8C02, 0x8C01)
    private final ModifiableCallout<DurationBasedCallout> c =
        ModifiableCallout.durationBasedCall("Reordered Label", "Reordered");
''' + _FOOTER)
check("subset superset and reordered id sets collapse to the first",
      [t["ability_id"] for t in res] == ["8C01"])
check("each in file dedup drop warns",
      err.count("WARN") == 2 and "8C01|8C02" in err and "8C02|8C01" in err)

# disjoint ids in one file still all convert
res, err = convert(_HEADER + '''
    @NpcCastCallout(0x8C01, 0x8C02)
    private final ModifiableCallout<DurationBasedCallout> a =
        ModifiableCallout.durationBasedCall("Stack Label", "Stack");
    @NpcCastCallout(0x8C03)
    private final ModifiableCallout<DurationBasedCallout> b =
        ModifiableCallout.durationBasedCall("Spread Label", "Spread");
''' + _FOOTER)
check("disjoint id sets all convert",
      [t["ability_id"] for t in res] == ["8C01|8C02", "8C03"]
      and "WARN" not in err)

# two files sharing one repo name warn and drop the duplicate
with tempfile.TemporaryDirectory() as td:
    _src = _HEADER + '''
    @NpcCastCallout(0x99FF)
    private final ModifiableCallout<DurationBasedCallout> z =
        ModifiableCallout.durationBasedCall("Raidwide Label", "Raidwide");
''' + _FOOTER
    (Path(td) / "a.java").write_text(_src, encoding="utf-8")
    (Path(td) / "b.java").write_text(_src, encoding="utf-8")
    _out = Path(td) / "out.json"
    _err = io.StringIO()
    _argv = sys.argv
    sys.argv = ["nyaatriggers/convert_event_trigger.py", td, str(_out)]
    try:
        with contextlib.redirect_stderr(_err):
            convert_event_trigger.main()
    finally:
        sys.argv = _argv
    _rows = json.loads(_out.read_text(encoding="utf-8"))
    check("cross-file duplicate drops the second file's row", len(_rows) == 1)
    check("cross-file duplicate warns", "duplicate callout" in _err.getvalue())

# a non-list shipped triggers.json warns instead of crashing main
with tempfile.TemporaryDirectory() as td:
    _bad = Path(td) / "triggers.json"
    _old_json = convert_event_trigger.EXISTING_JSON
    convert_event_trigger.EXISTING_JSON = _bad
    _argv = sys.argv
    sys.argv = ["nyaatriggers/convert_event_trigger.py", td]
    try:
        for _scalar in ("5", "null"):
            _bad.write_text(_scalar, encoding="utf-8")
            _err = io.StringIO()
            with contextlib.redirect_stderr(_err), \
                    contextlib.redirect_stdout(io.StringIO()):
                convert_event_trigger.main()
            check(f"scalar top level {_scalar} warns and does not crash",
                  "cannot check against" in _err.getvalue())
        _bad.write_text("[]", encoding="utf-8")
        _err = io.StringIO()
        with contextlib.redirect_stderr(_err), \
                contextlib.redirect_stdout(io.StringIO()):
            convert_event_trigger.main()
        check("list top level still loads with no cannot-check WARN",
              "cannot check against" not in _err.getvalue())
    finally:
        convert_event_trigger.EXISTING_JSON = _old_json
        sys.argv = _argv

print()
if FAILS:
    print(f"{len(FAILS)} FAILED: {', '.join(FAILS)}")
    sys.exit(1)
print("all passed")
