"""Build callouts_ja.json (id/text -> display, display -> kana reading) from the
phrase map.

Reads triggers.json + tools/callout_phrases_ja.json. The phrase map is
  "<english callout>": {"display": "<natural JP, kanji ok>", "reading": "<pure kana>"}
(a plain string is accepted too and used as both display and reading). Emits:
  callouts : {trigger-id -> display}      NyaaTriggers' own triggers (precise)
  phrases  : {english-text -> display}    free-form engine callouts (TE/cactbot/TN)
  readings : {display -> kana reading}    TTS form. espeak/SAPI can't read kanji
  names    : {trigger-id -> ja name}      trigger-list labels (display only, never
             spoken, so no reading needed). From tools/trigger_names_ja.json,
             a flat {"<english trigger name>": "<japanese>"} map (text-keyed so
             duplicate names translate consistently). Missing file -> empty map.

Tokens ({source}/{target}/{count}) must survive in display AND reading, else the
entry is skipped. Readings that still contain kanji are reported (they'd be spoken
as "Chinese letter" by espeak). Machine-assisted DRAFT.

Run:  python tools/build_callouts_ja.py
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_TRIGGERS = _REPO / "triggers.json"
_PHRASES = _REPO / "tools" / "callout_phrases_ja.json"
_NAMES = _REPO / "tools" / "trigger_names_ja.json"
_OUT = _REPO / "callouts_ja.json"
_MAIN = _REPO / "app_common.py"

# Only the tokens _fire() actually substitutes at runtime (.replace of
# {source}/{target}/{count}) must survive the translation. Other braces (simple
# Groovy vars like {longSpreadOn}, dotted engine tokens like {event.foo}) are
# raw engine-callout text the runtime wildcard-regex (_compile_phrase_patterns)
# matches with .*?, so a natural JP rendering legitimately drops them. Gating on
# all {\w+} tokens wrongly skipped ~150 finished translations that only differed
# by such non-substituted tokens, shipping them as English.
_SUBST_TOKENS = ("source", "target", "count")
# Count occurrences, not just membership. A translation keeping one {target}
# where English speaks two must not pass the gate, the extra mention would
# silently drop from the spoken callout.
_TOKENS = lambda s: {t: s.count("{" + t + "}") for t in _SUBST_TOKENS if "{" + t + "}" in s}
# Kanji range matches locale_util.has_japanese, CJK Unified plus Extensions A
# and B, Compatibility Ideographs, and the iteration mark 々.
_KANJI = re.compile(r"[㐀-鿿々豈-﫿𠀀-𪛟]")


def _app_version() -> str:
    m = re.search(r'^_VERSION\s*=\s*"([^"]+)"', _MAIN.read_text(encoding="utf-8"), re.M)
    return m.group(1) if m else "0.0.0"


def _str_field(v) -> str:
    # A hand edited json can park a truthy non-string under any of these
    # keys. Coerce to "" so the entry drops out like any blank one instead
    # of crashing the build.
    return v.strip() if isinstance(v, str) else ""


def _norm_phrase_map(raw: dict) -> dict:
    """english -> (display, reading). Accepts {"display","reading"} or a bare string."""
    out = {}
    for k, v in raw.items():
        k = k.strip()
        if not k or k.startswith("_"):
            continue
        if isinstance(v, dict):
            disp, read = _str_field(v.get("display")), _str_field(v.get("reading"))
        elif isinstance(v, str):
            disp = read = v.strip()
        else:
            continue
        if not disp:
            continue
        if k in out:
            # Trigger text is stripped before lookup so a padded key could never
            # match anyway. Keep the first entry on a strip collision.
            print(f"  WARNING: duplicate phrase key after stripping whitespace: "
                  f"{k!r}, keeping first", file=sys.stderr)
            continue
        out[k] = (disp, read or disp)
    return out


def main() -> int:
    # A hand edited json can be any shape. Degrade to empty maps instead of
    # crashing the build, same as the junk entry skips below.
    phrases_raw = json.loads(_PHRASES.read_text(encoding="utf-8"))
    phrase_map = _norm_phrase_map(phrases_raw if isinstance(phrases_raw, dict) else {})
    # Skip junk entries up front. A hand edited triggers.json can park a bare
    # string in the list, and every loop below calls .get on the entry.
    triggers_raw = json.loads(_TRIGGERS.read_text(encoding="utf-8"))
    triggers = ([t for t in triggers_raw if isinstance(t, dict)]
                if isinstance(triggers_raw, list) else [])

    valid, token_bad = {}, []
    for eng, (disp, read) in phrase_map.items():
        if _TOKENS(disp) != _TOKENS(eng) or _TOKENS(read) != _TOKENS(eng):
            token_bad.append(f"{eng!r} -> display {disp!r} / reading {read!r}")
            continue
        valid[eng] = (disp, read)

    callouts = {}
    for t in triggers:
        tid, text = t.get("id"), _str_field(t.get("tts_text"))
        if isinstance(tid, str) and tid and text in valid:
            # A reused id silently kept the last callout, and both triggers
            # stay live at runtime, so the earlier one would speak this text.
            # Warn like the duplicate phrase key check does.
            if tid in callouts and callouts[tid] != valid[text][0]:
                print(f"  WARNING: duplicate trigger id {tid!r} with a different "
                      f"callout, keeping last", file=sys.stderr)
            callouts[tid] = valid[text][0]
    phrases = {eng: disp for eng, (disp, _r) in valid.items()}
    # Several phrases can share one display. Last reading wins in `readings`,
    # warn when the dropped readings differ so the phrase map can be fixed.
    by_disp = {}
    for _e, (disp, read) in valid.items():
        if read and read != disp:
            by_disp.setdefault(disp, set()).add(read)
    read_clash = {d: sorted(rs) for d, rs in by_disp.items() if len(rs) > 1}
    readings = {disp: read for _e, (disp, read) in valid.items() if read and read != disp}

    # Trigger names: text-keyed source -> id-keyed map, mirroring `callouts`.
    name_map = {}
    if _NAMES.exists():
        # Strip keys too, like _norm_phrase_map does. Lookups here and the
        # runtime both use the stripped trigger name, so a padded key could
        # never match and shipped dead in names_text.
        names_raw = json.loads(_NAMES.read_text(encoding="utf-8"))
        name_map = {ks: v.strip() for k, v in names_raw.items()
                    if (ks := k.strip()) and not ks.startswith("_")
                    and isinstance(v, str) and v.strip()} if isinstance(names_raw, dict) else {}
    names, name_miss = {}, set()
    for t in triggers:
        tid, nm = t.get("id"), _str_field(t.get("name"))
        if not (isinstance(tid, str) and tid and nm):
            continue
        if nm in name_map:
            names[tid] = name_map[nm]
        else:
            name_miss.add(nm)

    # Scan every effective spoken form, including entries whose reading fell back to
    # the kanji display (read == disp, filtered out of `readings`), so those warn too.
    kanji_reading = sorted({r for _e, (d, r) in valid.items() if _KANJI.search(r)})

    total = sum(1 for t in triggers if _str_field(t.get("tts_text")))
    data = {
        "schema": 1,
        "app_version": _app_version(),
        "locale": "ja",
        "draft": True,
        "note": "Machine-assisted draft translations. Corrections welcome via the repo.",
        "callouts": dict(sorted(callouts.items())),
        "phrases": dict(sorted(phrases.items())),
        "readings": dict(sorted(readings.items())),
        "names": dict(sorted(names.items())),
        # Text-keyed (english name -> ja) so engine triggers (Triggevent/
        # Triggernometry), whose ids are Groovy classpaths not in the id map,
        # still localize by display name. Same source as `names`.
        "names_text": dict(sorted(name_map.items())),
    }
    # Sibling tmp + rename, so an interrupted run can't leave a truncated
    # file in place of the previous good output. Same idiom as the converters.
    tmp = _OUT.with_name(_OUT.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, _OUT)

    n_total = sum(1 for t in triggers if _str_field(t.get("name")))
    print(f"{len(valid)} phrases | {len(callouts)}/{total} triggers ({100*len(callouts)/(total or 1):.0f}%) "
          f"| {len(readings)} readings | {len(names)}/{n_total} names "
          f"| {len(name_map)} name-text -> {_OUT.name}")
    if name_miss:
        print(f"  {len(name_miss)} trigger name(s) missing from {_NAMES.name}: "
              f"{', '.join(map(repr, sorted(name_miss)[:10]))}", file=sys.stderr)
    if token_bad:
        print(f"  {len(token_bad)} SKIPPED (token mismatch):", file=sys.stderr)
        for b in token_bad[:15]:
            print("   " + b, file=sys.stderr)
    if read_clash:
        print(f"  WARNING: {len(read_clash)} display(s) have conflicting readings "
              f"(last wins):", file=sys.stderr)
        for d in sorted(read_clash)[:10]:
            print(f"   {d!r}: {', '.join(map(repr, read_clash[d]))}", file=sys.stderr)
    if kanji_reading:
        print(f"  WARNING: {len(kanji_reading)} reading(s) still contain kanji "
              f"(espeak will mis-speak): {', '.join(map(repr, kanji_reading[:10]))}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
