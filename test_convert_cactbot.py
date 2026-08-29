"""Parser-level regression tests for convert_cactbot's netRegex call form
(L-5): `netRegex: NetRegex.ability({ ... })` must feed the object literal
inside the parens through the same id-parsing path as the plain block form,
so those triggers are no longer silently dropped (parse_netregex_ids used
to return [] and convert_file skipped them).

Run directly:  python test_convert_cactbot.py   (exit 0 = all pass)
"""
import os
import re
import sys
import tempfile
import contextlib
import io
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import convert_cactbot
from convert_cactbot import (
    OUTPUTS, RESPONSES, _array_at, _unescape_js, convert_file,
    extract_top_blocks, find_sub_block, parse_netregex_ids, resolve_output_key,
    strip_js_comments,
)

FAILS = []


def check(name, cond):
    print(("PASS  " if cond else "FAIL  ") + name)
    if not cond:
        FAILS.append(name)


# ── the plain block form is unchanged ─────────────────────────────────────
check("plain block form ids",
      parse_netregex_ids("netRegex: { id: '8B5F', capture: false },")
      == ["8B5F"])
check("plain find_sub_block unchanged",
      find_sub_block("x netRegex: { id: '8B5F' }, y", "netRegex")
      == "{ id: '8B5F' }")

# ── the call form feeds the object inside the parens to the same path ─────
check("call form scalar id",
      parse_netregex_ids(
          "netRegex: NetRegex.ability({ id: '8B5F', capture: false }),")
      == ["8B5F"])
check("call form array ids",
      parse_netregex_ids(
          "netRegex: NetRegex.ability({ id: ['8B5F', '8b60'] }),")
      == ["8B5F", "8B60"])
check("call form effectId",
      parse_netregex_ids(
          "netRegex: NetRegex.gainsEffect({ effectId: 'B7A' }),")
      == ["B7A"])
check("call form with a nested object",
      parse_netregex_ids(
          "netRegex: NetRegex.ability({ id: '8B5F', extra: { x: 1 } }),")
      == ["8B5F"])
check("call form split across lines",
      parse_netregex_ids(
          "netRegex: NetRegex.ability(\n  { id: '8B5F' },\n),")
      == ["8B5F"])
check("call form without ids returns []",
      parse_netregex_ids("netRegex: NetRegex.ability({ capture: true }),")
      == [])
check("no netRegex returns []",
      parse_netregex_ids("{ id: '8B5F' }") == [])

# ── end to end: a call-form trigger survives convert_file ─────────────────
FIXTURE = """\
const triggerSet = {
  triggers: [
    {
      id: 'Test Call Form',
      type: 'Ability',
      netRegex: NetRegex.ability({ id: '8B5F', source: 'Boss' }),
      alertText: 'Stack',
    },
    {
      id: 'Test Plain Form',
      type: 'StartsUsing',
      netRegex: { id: '8B60', source: 'Boss' },
      response: Responses.getOut(),
    },
  ],
};
"""

with tempfile.TemporaryDirectory() as td:
    ts = Path(td) / "fixture.ts"
    ts.write_text(FIXTURE, encoding="utf-8")
    res = convert_file(ts, "TST")

check("both fixture triggers convert (call form no longer dropped)",
      len(res) == 2)
if len(res) == 2:
    check("call form trigger keeps its id and pipe log type",
          res[0]["ability_id"] == "8B5F" and res[0]["log_type"] == "21|22")
    check("call form trigger keeps its callout",
          res[0]["tts_text"] == "Stack")
    check("plain form trigger still converts",
          res[1]["ability_id"] == "8B60" and res[1]["log_type"] == "20")

# ── disabled: true triggers skip, disabled: false still converts ──────────
FIXTURE = """\
const triggerSet = {
  triggers: [
    {
      id: 'Test Disabled',
      type: 'Ability',
      disabled: true,
      netRegex: { id: '8B61', source: 'Boss' },
      alertText: 'Stack',
    },
    {
      id: 'Test Explicitly Enabled',
      type: 'Ability',
      disabled: false,
      netRegex: { id: '8B62', source: 'Boss' },
      alertText: 'Spread',
    },
  ],
};
"""

with tempfile.TemporaryDirectory() as td:
    ts = Path(td) / "fixture.ts"
    ts.write_text(FIXTURE, encoding="utf-8")
    res = convert_file(ts, "TST")

check("disabled: true trigger is skipped", len(res) == 1)
if len(res) == 1:
    check("disabled: false trigger still converts",
          res[0]["name"] == "Test Explicitly Enabled"
          and res[0]["ability_id"] == "8B62")

# ── output key lookup: a short key must not match inside a longer one ─────
check("object form: text does not resolve to context",
      resolve_output_key(
          "text",
          "{ context: { en: 'Avoid the eye' }, text: { en: 'Look away' } }")
      == "Look away")
check("Outputs form: text does not resolve to context",
      resolve_output_key(
          "text",
          "{ context: Outputs.getOut, text: Outputs.getIn }")
      == "In")
check("quoted key still resolves",
      resolve_output_key("text", "{ 'text': { en: 'Hi' } }") == "Hi")
check("bare key still resolves",
      resolve_output_key("text", "{ text: { en: 'Hi' } }") == "Hi")

FIXTURE = """\
const triggerSet = {
  triggers: [
    {
      id: 'Test Context Collision',
      type: 'Ability',
      netRegex: { id: '8B63', source: 'Boss' },
      alarmText: (_data, _matches, output) => output.text!(),
      outputStrings: {
        context: { en: 'Avoid the eye' },
        text: { en: 'Look away' },
      },
    },
  ],
};
"""

with tempfile.TemporaryDirectory() as td:
    ts = Path(td) / "fixture.ts"
    ts.write_text(FIXTURE, encoding="utf-8")
    res = convert_file(ts, "TST")

check("context defined before text still ships the text callout",
      len(res) == 1 and res[0]["tts_text"] == "Look away")

# ── a regex literal with an unbalanced brace must not corrupt extraction ──
FIXTURE = """\
const triggerSet = {
  triggers: [
    {
      id: 'Test Regex Brace',
      type: 'Ability',
      netRegex: /hit \\{ now/,
      alertText: 'Stack',
    },
    {
      id: 'Test After Regex One',
      type: 'Ability',
      netRegex: { id: '8B64', source: 'Boss' },
      alertText: 'Spread',
    },
    {
      id: 'Test After Regex Two',
      type: 'Ability',
      netRegex: { id: '8B65', source: 'Boss' },
      alertText: 'Out',
    },
  ],
};
"""

stripped = strip_js_comments(FIXTURE)
m = re.search(r"\btriggers\s*:\s*\[", stripped)
blocks = extract_top_blocks(_array_at(stripped, m.end() - 1))
check("regex literal with an unbalanced brace keeps all three blocks",
      len(blocks) == 3)

with tempfile.TemporaryDirectory() as td:
    ts = Path(td) / "fixture.ts"
    ts.write_text(FIXTURE, encoding="utf-8")
    res = convert_file(ts, "TST")

check("neighbors of a regex-literal trigger still convert",
      len(res) == 2
      and res[0]["name"] == "Test After Regex One"
      and res[1]["name"] == "Test After Regex Two")

# ── JS string escapes: \uXXXX and \xXX resolve to their char ──────────────
check("unicode escape resolves", _unescape_js(r"Don\u2019t") == "Don\u2019t")
check("hex escape resolves", _unescape_js(r"a\x41b") == "aAb")
check("single char escapes still work",
      _unescape_js(r"Boss\'s\nRight") == "Boss's Right")
check("escaped backslash keeps the u text literal",
      _unescape_js(r"\\u2019") == r"\u2019")
check("escaped backslash keeps the x text literal",
      _unescape_js(r"\\x41") == r"\x41")
check("escaped backslash then a real unicode escape",
      _unescape_js(r"\\\u2019") == "\\" + "\u2019")
check("astral pair still recombines",
      _unescape_js(r"\uD83D\uDE00") == "\U0001F600")

# ── trigger name: a nested netRegex id must not name the trigger ──────────
FIXTURE = """\
const triggerSet = {
  triggers: [
    {
      type: 'Ability',
      netRegex: {
        id: '8B5F',
        source: 'Boss',
      },
      id: 'Nested Id Leads',
      alertText: 'Stack',
    },
    {
      id: 'Own Id Leads',
      type: 'Ability',
      netRegex: {
        id: '8B66',
        source: 'Boss',
      },
      alertText: 'Spread',
    },
  ],
};
"""

with tempfile.TemporaryDirectory() as td:
    ts = Path(td) / "fixture.ts"
    ts.write_text(FIXTURE, encoding="utf-8")
    res = convert_file(ts, "TST")

check("nested netRegex id never names a trigger",
      all(t["name"] != "8B5F" for t in res))
check("trigger whose own id is not the first field skips instead",
      len(res) == 1)
if len(res) == 1:
    check("id-first trigger keeps its name and the nested id as ability",
          res[0]["name"] == "Own Id Leads" and res[0]["ability_id"] == "8B66")

# ── RESPONSES map: real cactbot names resolve, unknown names warn ──────────
# Names checked against cactbot main's resources/responses.ts. The map once
# held names that were never Responses functions, getFront for goFront and
# inThenOut for getInThenOut, so response-only triggers dropped silently.
check("renamed responses use the real cactbot names",
      RESPONSES.get("goFront") == "Go Front"
      and RESPONSES.get("getInThenOut") == "In => Out"
      and RESPONSES.get("getOutThenIn") == "Out => In"
      and RESPONSES.get("lookTowards") == "Look Towards Boss"
      and RESPONSES.get("preyOn") == "Prey on you")
check("common modern response names are mapped",
      all(k in RESPONSES for k in
          ("stackMarker", "stackMarkerOn", "stackPartner", "getTogether",
           "goLeft", "goRight", "killAdds", "meteorOnYou")))
check("dead names that never were Responses calls are gone",
      all(k not in RESPONSES for k in
          ("getFront", "inThenOut", "outThenIn", "lookTowardsBoss",
           "preyOnYou", "stack", "stacks", "healingRequired", "defamation")))
check("Outputs-only spellings stay reachable through OUTPUTS",
      OUTPUTS.get("sharedTankbuster") == "Shared Tank Buster"
      and OUTPUTS.get("stackMarker") == "Stack"
      and OUTPUTS.get("baitPuddles") == "Bait Puddles"
      and OUTPUTS.get("avoidTankCleave") == "Avoid Tank Cleave"
      and OUTPUTS.get("stacks") == "Stacks")

FIXTURE = """\
const triggerSet = {
  triggers: [
    {
      id: 'Test Stack Marker Response',
      type: 'Ability',
      netRegex: { id: '8B70', source: 'Boss' },
      response: Responses.stackMarker(),
    },
    {
      id: 'Test Renamed Response',
      type: 'Ability',
      netRegex: { id: '8B71', source: 'Boss' },
      response: Responses.goFront(),
    },
    {
      id: 'Test Unknown Response',
      type: 'Ability',
      netRegex: { id: '8B72', source: 'Boss' },
      response: Responses.notARealResponse(),
    },
  ],
};
"""

with tempfile.TemporaryDirectory() as td:
    ts = Path(td) / "fixture.ts"
    ts.write_text(FIXTURE, encoding="utf-8")
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        res = convert_file(ts, "TST")

check("response-only triggers with mapped names convert",
      [t["tts_text"] for t in res] == ["Stack", "Go Front"])
check("unknown response name drops with a WARN naming the trigger",
      "notARealResponse" in err.getvalue()
      and "Test Unknown Response" in err.getvalue())

# ── a non-list shipped triggers.json warns instead of crashing main ───────
with tempfile.TemporaryDirectory() as td:
    (Path(td) / "ui" / "raidboss" / "data").mkdir(parents=True)
    bad = Path(td) / "triggers.json"
    _old_json = convert_cactbot.EXISTING_JSON
    convert_cactbot.EXISTING_JSON = bad
    _argv = sys.argv
    sys.argv = ["convert_cactbot.py", td]
    try:
        for scalar in ("5", "null"):
            bad.write_text(scalar, encoding="utf-8")
            err = io.StringIO()
            with contextlib.redirect_stderr(err), \
                    contextlib.redirect_stdout(io.StringIO()):
                convert_cactbot.main()
            check(f"scalar top level {scalar} warns and does not crash",
                  "cannot check against" in err.getvalue())
        bad.write_text("[]", encoding="utf-8")
        err = io.StringIO()
        with contextlib.redirect_stderr(err), \
                contextlib.redirect_stdout(io.StringIO()):
            convert_cactbot.main()
        check("list top level still loads with no cannot-check WARN",
              "cannot check against" not in err.getvalue())
    finally:
        convert_cactbot.EXISTING_JSON = _old_json
        sys.argv = _argv

print()
if FAILS:
    print(f"{len(FAILS)} FAILED: {', '.join(FAILS)}")
    sys.exit(1)
print("all tests passed")
