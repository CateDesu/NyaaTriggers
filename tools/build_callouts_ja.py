"""Build Japanese callout display text, kana readings and names from the phrase maps and
shipped triggers. Preserve every source, target and count token in display and speech.
Report readings containing kanji. Run python tools/build_callouts_ja.py.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_TRIGGERS = _REPO / "assets" / "triggers.json"
_PHRASES = _REPO / "tools" / "callout_phrases_ja.json"
_NAMES = _REPO / "tools" / "trigger_names_ja.json"
_OUT = _REPO / "assets" / "callouts_ja.json"
_MAIN = _REPO / "nyaatriggers/app_common.py"

# Preserve runtime substitution tokens. Other engine tokens are already resolved before
# phrase matching and may be omitted from translations.
_SUBST_TOKENS = ("source", "target", "count")
# Preserve token counts as well as names.
_TOKENS = lambda s: {t: s.count("{" + t + "}") for t in _SUBST_TOKENS if "{" + t + "}" in s}
_KANJI = re.compile(r"[\u3005\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\U00020000-\U0002a6df\U0002a700-\U0002ceaf]")


def _app_version() -> str:
    m = re.search(r'^_VERSION\s*=\s*"([^"]+)"', _MAIN.read_text(encoding="utf-8"), re.M)
    return m.group(1) if m else "0.0.0"


def _str_field(v) -> str:
    return v.strip() if isinstance(v, str) else ""


def _norm_phrase_map(raw: dict) -> dict:
    """Normalize English phrase entries into display and reading pairs."""
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
            # Trim keys as the runtime does. Keep the first entry if trimmed keys
            # collide.
            print(f"  WARNING: duplicate phrase key after stripping whitespace: "
                  f"{k!r}, keeping first", file=sys.stderr)
            continue
        out[k] = (disp, read or disp)
    return out


def main() -> int:
    phrases_raw = json.loads(_PHRASES.read_text(encoding="utf-8"))
    phrase_map = _norm_phrase_map(phrases_raw if isinstance(phrases_raw, dict) else {})
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
            # Warn when a reused ID would replace an earlier callout.
            if tid in callouts and callouts[tid] != valid[text][0]:
                print(f"  WARNING: duplicate trigger id {tid!r} with a different "
                      f"callout, keeping last", file=sys.stderr)
            callouts[tid] = valid[text][0]
    phrases = {eng: disp for eng, (disp, _r) in valid.items()}
    # Warn when shared display text has conflicting readings.
    by_disp = {}
    for _e, (disp, read) in valid.items():
        if read and read != disp:
            by_disp.setdefault(disp, set()).add(read)
    read_clash = {d: sorted(rs) for d, rs in by_disp.items() if len(rs) > 1}
    readings = {disp: read for _e, (disp, read) in valid.items() if read and read != disp}

    name_map = {}
    if _NAMES.exists():
        # Trim name keys to match runtime lookup.
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

    # Check all spoken forms, including display text used as a fallback reading.
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
        # Include translations by name for engine rows without known IDs.
        "names_text": dict(sorted(name_map.items())),
    }
    # Replace through a sibling temporary file to preserve previous output if
    # interrupted.
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
