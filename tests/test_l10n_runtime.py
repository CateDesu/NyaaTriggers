import os
import sys
import json
import tempfile
from types import SimpleNamespace
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from nyaatriggers import app_common
from nyaatriggers import main_window
from nyaatriggers.locale_util import set_locale

MW = main_window.MainWindow
FAILS = []


def check(name, cond):
    print(("PASS  " if cond else "FAIL  ") + name)
    if not cond:
        FAILS.append(name)


def trig(tid, text):
    return SimpleNamespace(id=tid, tts_text=text)


def loc(settings, callouts, t, phrases=None):
    me = SimpleNamespace(_settings=settings, _callouts_ja=callouts,
                         _callouts_phrases_ja=phrases or {},
                         _official_triggers={ID1: trig(ID1, "Stack {target}")})
    return MW._localized_callout(me, t)


def loc_text(settings, phrases, text):
    me = SimpleNamespace(_settings=settings, _callouts_phrases_ja=phrases,
                         _callouts_phrases_ja_patterns=main_window._compile_phrase_patterns(phrases))
    return MW._localize_text(me, text)


ID1 = "id-1"
MAP = {ID1: "頭割り {target}"}
PHRASES = {"Raidwide": "全体攻撃", "Stack": "頭割り"}

set_locale("ja")
check("ja default localizes a known id", loc({}, MAP, trig(ID1, "Stack {target}")) == "頭割り {target}")
check("ja default: unknown id falls back to English", loc({}, MAP, trig("x", "Stack")) == "Stack")
check("tokens preserved for later substitution", "{target}" in loc({}, MAP, trig(ID1, "Stack {target}")))
check("explicit False under ja stays English", loc({"callouts_localized": False}, MAP, trig(ID1, "Stack")) == "Stack")
set_locale("en")
check("en default stays English", loc({}, MAP, trig(ID1, "Stack {target}")) == "Stack {target}")
check("explicit True under en localizes", loc({"callouts_localized": True}, MAP, trig(ID1, "Stack {target}")) == "頭割り {target}")
check("empty overlay -> English", loc({"callouts_localized": True}, {}, trig(ID1, "Stack")) == "Stack")
check("id miss -> phrase-map hit by tts_text",
      loc({"callouts_localized": True}, {}, trig("x", "Raidwide"), phrases=PHRASES) == "全体攻撃")

set_locale("ja")
check("engine callout localizes by exact text", loc_text({}, PHRASES, "Raidwide") == "全体攻撃")
check("engine callout unknown text -> English", loc_text({}, PHRASES, "Custom TE line") == "Custom TE line")
check("engine callout empty -> empty", loc_text({}, PHRASES, "") == "")
check("engine callout explicit False -> English", loc_text({"callouts_localized": False}, PHRASES, "Raidwide") == "Raidwide")
set_locale("en")
check("engine callout en default -> English", loc_text({}, PHRASES, "Raidwide") == "Raidwide")

set_locale("ja")
_RX = {"Away from Tank ({event.dur})": "タンクから離れる"}
check("engine callout localizes via the wildcard-regex path (real _localize_text)",
      loc_text({}, _RX, "Away from Tank (5.0s)") == "タンクから離れる")
check("regex path leaves a non-matching engine line in English",
      loc_text({}, _RX, "Stand on the tank") == "Stand on the tank")
set_locale("en")
check("regex path respects the en gate (no localization)",
      loc_text({}, _RX, "Away from Tank (5.0s)") == "Away from Tank (5.0s)")

def loc_name(settings, names, t):
    me = SimpleNamespace(_settings=settings, _callouts_names_ja=names,
                         _callouts_names_text_ja={})
    return MW._localized_name(me, t)


NAMES = {ID1: "頭割りタワー"}
ntrig = SimpleNamespace(id=ID1, name="Stack Tower", tts_text="")
set_locale("ja")
check("name: ja default localizes a known id", loc_name({}, NAMES, ntrig) == "頭割りタワー")
check("name: unknown id (user copy) -> English",
      loc_name({}, NAMES, SimpleNamespace(id="x", name="Stack Tower (copy)")) == "Stack Tower (copy)")
check("name: explicit False under ja stays English",
      loc_name({"callouts_localized": False}, NAMES, ntrig) == "Stack Tower")
set_locale("en")
check("name: en default stays English", loc_name({}, NAMES, ntrig) == "Stack Tower")
check("name: explicit True under en localizes",
      loc_name({"callouts_localized": True}, NAMES, ntrig) == "頭割りタワー")

def read_for(readings, text):
    return MW._reading_for(SimpleNamespace(_callouts_readings=readings), text)

READ = {"全体攻撃": "ぜんたいこうげき", "頭割り": "あたまわり"}
check("reading maps display kanji -> kana", read_for(READ, "全体攻撃") == "ぜんたいこうげき")
check("reading passes through unknown (names/English)", read_for(READ, "Alice") == "Alice")
check("reading passes through katakana display (no entry)", read_for(READ, "タンクバスター") == "タンクバスター")

_oc, _ob = app_common._CALLOUTS_JA_CACHE, app_common._CALLOUTS_JA_BUNDLE


def load_with(cache_text, bundle_text):
    d = Path(tempfile.mkdtemp())
    cpath, bpath = d / "cache.json", d / "bundle.json"
    if cache_text is not None:
        cpath.write_text(cache_text, encoding="utf-8")
    if bundle_text is not None:
        bpath.write_text(bundle_text, encoding="utf-8")
    app_common._CALLOUTS_JA_CACHE = cpath
    app_common._CALLOUTS_JA_BUNDLE = bpath
    ns = SimpleNamespace()
    MW._load_cached_callouts_ja(ns)
    return ns._callouts_ja


GOOD = json.dumps({"schema": 1, "callouts": {ID1: "あ"}})
try:
    check("cache wins over bundle", load_with(GOOD, json.dumps({"callouts": {ID1: "BUNDLE"}})) == {ID1: "あ"})
    check("bundle used when no cache", load_with(None, GOOD) == {ID1: "あ"})
    check("both absent -> empty map", load_with(None, None) == {})
    check("corrupt cache falls through to bundle", load_with("{ not json", GOOD) == {ID1: "あ"})
    check("missing callouts key -> empty map", load_with(json.dumps({"schema": 1}), None) == {})
    check("non-str/empty values filtered out",
          load_with(json.dumps({"callouts": {ID1: "あ", "b": 5, "c": ""}}), None) == {ID1: "あ"})
    rich = json.dumps({"app_version": "1.1.4", "callouts": {ID1: "あ", "x2": "い", "x3": "う"}})
    stale = json.dumps({"app_version": "1.1.4", "callouts": {ID1: "OLD"}})
    check("richer bundle wins over a smaller stale cache",
          load_with(stale, rich) == {ID1: "あ", "x2": "い", "x3": "う"})
    check("newer app_version cache wins over bundle",
          load_with(json.dumps({"app_version": "1.2.0", "callouts": {ID1: "NEW"}}), rich) == {ID1: "NEW"})
    dd = Path(tempfile.mkdtemp())
    (dd / "cache.json").write_bytes(b"\xff\xfe not valid utf-8")
    app_common._CALLOUTS_JA_CACHE = dd / "cache.json"
    app_common._CALLOUTS_JA_BUNDLE = dd / "absent.json"
    ns = SimpleNamespace()
    MW._load_cached_callouts_ja(ns)
    check("bad-UTF-8 source skipped, no crash", ns._callouts_ja == {})
finally:
    app_common._CALLOUTS_JA_CACHE, app_common._CALLOUTS_JA_BUNDLE = _oc, _ob

_oc2, _ob2 = app_common._CALLOUTS_JA_CACHE, app_common._CALLOUTS_JA_BUNDLE
try:
    d = Path(tempfile.mkdtemp())
    (d / "b.json").write_text(json.dumps(
        {"callouts": {ID1: "あ"}, "names": {ID1: "名前", "bad": 7, "e": ""}}), encoding="utf-8")
    app_common._CALLOUTS_JA_CACHE = d / "absent.json"
    app_common._CALLOUTS_JA_BUNDLE = d / "b.json"
    ns = SimpleNamespace()
    MW._load_cached_callouts_ja(ns)
    check("loader parses names (non-str/empty filtered)", ns._callouts_names_ja == {ID1: "名前"})
    (d / "b.json").write_text(json.dumps({"callouts": {ID1: "あ"}}), encoding="utf-8")
    ns = SimpleNamespace()
    MW._load_cached_callouts_ja(ns)
    check("loader: missing names key -> empty map", ns._callouts_names_ja == {})
finally:
    app_common._CALLOUTS_JA_CACHE, app_common._CALLOUTS_JA_BUNDLE = _oc2, _ob2

import re as _re
_ja_cat = json.loads((Path(__file__).resolve().parents[1] / "lang" / "ja.json").read_text(encoding="utf-8"))
_toks = lambda s: set(_re.findall(r"{(\w+)}", s))
_tok_bad = sorted(k for k, v in _ja_cat.items() if v and _toks(k) != _toks(v))
check("ja.json: every translation preserves its key's {tokens}", _tok_bad == [])
if _tok_bad:
    print("   token-mismatched keys:", _tok_bad[:5])

_comp = main_window._compile_phrase_patterns
def _matched(phrases, text):
    ja = phrases.get(text)
    if ja:
        return ja
    for pat, ja_val in _comp(phrases):
        translated = pat.render(text, ja_val, phrases)
        if translated is not None:
            return translated
    return None
_P = {
    "Away from {event.source} ({event.estimatedRemainingDuration})": "離れる",
    "Behind": "後ろ",
    "Buster on {target}": "{target} バスター",
    "{safe}": "安全",
    "{firstQuadrant} then {secondQuadrant}": "ギミック",
}
_compiled = _comp(_P)
_compiled_en = {en for _pat, en in _compiled}
check("complex-token key (token-free JA) compiles",
      "離れる" in _compiled_en)
check("engine tokens are captured instead of leaking placeholders",
      _matched(_P, "Buster on Alice Example") == "Alice Example バスター")
check("static key does NOT compile (stays in the exact dict)",
      "後ろ" not in _compiled_en)
check("no-literal key does NOT compile (would hijack unrelated callouts)",
      "安全" not in _compiled_en and "ギミック" not in _compiled_en)
check("regex matches post-substitution engine text", _matched(_P, "Away from Tank (5.0s)") == "離れる")
check("regex does NOT hijack an unrelated string", _matched(_P, "Triangle, far from buddy (3.0s)") is None)
check("exact dict still serves static keys", _matched(_P, "Behind") == "後ろ")
check("translated engine callouts preserve target names and directions",
      loc_text({"callouts_localized": True},
               {"Stack on {event.target} then {safe}": "{event.target}に頭割り、その後{safe}",
                "North": "北"}, "Stack on Alice Example then North")
      == "Alice Exampleに頭割り、その後北")
check("adjacent placeholders cannot guess where names end",
      _comp({"Spread {first}{second}": "{first}と{second}に散開"}) == [])
check("unknown replacement tokens cannot leak into speech",
      _comp({"Spread {first}": "{unknown}に散開"}) == [])
check("nested engine expressions are captured without evaluation",
      loc_text({"callouts_localized": True},
               {"Start from {{ clockwise ? 'North' : 'South' }}": "開始は{{ clockwise ? 'North' : 'South' }}",
                "North": "北"}, "Start from North") == "開始は北")
check("conditional templates only accept their declared choices",
      _matched({"Center {right ? 'Right' : 'Left'}": "中央 {right ? 'Right' : 'Left'}"},
               "Center to South, North, Northwest") is None)

def loc_name_text(id_map, text_map, t):
    me = SimpleNamespace(_settings={"callouts_localized": True},
                         _callouts_names_ja=id_map, _callouts_names_text_ja=text_map)
    return MW._localized_name(me, t)
check("engine name: id-miss falls through to text-keyed map",
      loc_name_text({}, {"Buster": "バスター"}, SimpleNamespace(id="gg.x.Buster", name="Buster")) == "バスター")
check("engine name: id-map still wins over text-map when present",
      loc_name_text({"gg.x.Buster": "IDバスター"}, {"Buster": "TEXTバスター"},
                    SimpleNamespace(id="gg.x.Buster", name="Buster")) == "IDバスター")
check("engine name: neither map -> English fallback",
      loc_name_text({}, {}, SimpleNamespace(id="gg.x.X", name="Unknown")) == "Unknown")

print()
if FAILS:
    print(f"{len(FAILS)} FAILED: {FAILS}")
    sys.exit(1)
print("all tests passed")
