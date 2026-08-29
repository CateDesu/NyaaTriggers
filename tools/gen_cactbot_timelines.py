#!/usr/bin/env python3
"""Regenerate cactbot_timelines.json (zone id -> cactbot timeline) from cactbot.

cactbot ships a .txt timeline for hundreds of fights, but builds its own
zone -> timeline manifest at build time (webpack manifest-loader), so there
is no ready-made index to download. This walks the GitHub tree of
ui/raidboss/data, parses every trigger .ts for zoneId / zoneRegex /
timelineFile (the same declarations the webpack build reads), and resolves
ZoneId constants via resources/zone_id.ts and zoneRegex patterns against
the English names in resources/zone_info.ts. Keyed on the numeric zone id
so the runtime lookup works whatever language the client reports.

Run:  python tools/gen_cactbot_timelines.py   (writes ../cactbot_timelines.json)
"""
import concurrent.futures
import json
import os
import re
import urllib.request
from pathlib import Path, PurePosixPath

API_TREE = ("https://api.github.com/repos/OverlayPlugin/cactbot/git/trees/"
            "main?recursive=1")
RAW = "https://raw.githubusercontent.com/OverlayPlugin/cactbot/main/"
DATA_PREFIX = "ui/raidboss/data/"
OUT = Path(__file__).resolve().parent.parent / "cactbot_timelines.json"

_UA = {"User-Agent": "NyaaTriggers"}

# Bounds a single response. The repo tree listing is the largest fetch by
# far, everything else is trigger source files well under 1 MiB.
_MAX_FETCH = 64 << 20


def _fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        data = r.read(_MAX_FETCH + 1)
    if len(data) > _MAX_FETCH:
        raise ValueError(f"{url} exceeds {_MAX_FETCH} bytes")
    return data


def _zone_id_consts() -> dict:
    """'ThePraetorium' -> 1044, from resources/zone_id.ts."""
    src = _fetch(RAW + "resources/zone_id.ts").decode("utf-8")
    return {m.group(1): int(m.group(2))
            for m in re.finditer(r"^\s*'([A-Za-z0-9_]+)': (\d+),?$", src, re.M)}


def _zone_names_en() -> dict:
    """zone id -> English name, from resources/zone_info.ts (same parse as
    tools/gen_zone_names.py)."""
    src = _fetch(RAW + "resources/zone_info.ts").decode("utf-8")
    names = {}
    for m in re.finditer(r"^  (\d+): \{(.*?)^  \},", src, re.S | re.M):
        en = re.search(r"'en': '((?:[^'\\]|\\.)*)'", m.group(2))
        if en:
            names[int(m.group(1))] = en.group(1).replace("\\'", "'").replace("\\\\", "\\")
    return names


def _js_regex(lit: str, flags: str) -> "re.Pattern":
    """Compile a JS regex literal body the way cactbot's .test() would."""
    return re.compile(lit.replace("\\/", "/"), re.I if "i" in flags else 0)


def _zone_ids_for(src: str, consts: dict, names_en: dict, rel: str) -> list:
    """Numeric zone ids a trigger file declares, via zoneId (const, list or
    literal) or a zoneRegex matched against every English zone name."""
    m = re.search(r"zoneId:\s*ZoneId\.([A-Za-z0-9_]+)", src)
    if m:
        z = consts.get(m.group(1))
        return [z] if z is not None else []
    m = re.search(r"zoneId:\s*\[([^\]]+)\]", src, re.S)
    if m:
        return [consts[c] for c in re.findall(r"ZoneId\.([A-Za-z0-9_]+)", m.group(1))
                if c in consts]
    m = re.search(r"zoneId:\s*(\d+)", src)
    if m:
        return [int(m.group(1))]
    # zoneRegex: bare literal or a locale object. cactbot .test()s the zone
    # name, so match the English pattern against every known zone name.
    m = (re.search(r"zoneRegex:\s*\{[^}]*?en:\s*/((?:[^/\\]|\\.)*)/([a-z]*)", src, re.S)
         or re.search(r"zoneRegex:\s*/((?:[^/\\]|\\.)*)/([a-z]*)", src))
    if m:
        try:
            rx = _js_regex(m.group(1), m.group(2))
        except re.error:
            print(f"  warn: unparseable zoneRegex in {rel}")
            return []
        return [z for z, name in names_en.items() if rx.search(name)]
    return []


def main() -> None:
    tree = json.loads(_fetch(API_TREE).decode("utf-8"))
    if tree.get("truncated"):
        raise SystemExit("GitHub tree response truncated - cannot trust the walk")
    paths = [t["path"] for t in tree["tree"]
             if t.get("type") == "blob" and t["path"].startswith(DATA_PREFIX)]
    ts_files = sorted(p for p in paths if p.endswith(".ts"))
    txt_set = {p[len(DATA_PREFIX):] for p in paths if p.endswith(".txt")}

    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as ex:
        bodies = list(ex.map(lambda p: _fetch(RAW + p).decode("utf-8", "replace"),
                             ts_files))

    consts = _zone_id_consts()
    names_en = _zone_names_en()

    index: dict[int, dict] = {}
    skipped = 0
    for path, src in zip(ts_files, bodies):
        rel = path[len(DATA_PREFIX):]
        tl = re.search(r"timelineFile:\s*'([^']+)'", src)
        if not tl:
            continue                      # no timeline: triggers-only file
        # cactbot resolves timelineFile against the trigger file's directory
        # (popup-text.ts) and loads nothing when it is absent.
        txt = str(PurePosixPath(rel).parent / tl.group(1))
        if txt not in txt_set:
            print(f"  warn: {rel} names missing timeline {txt}")
            skipped += 1
            continue
        zids = _zone_ids_for(src, consts, names_en, rel)
        if not zids:
            print(f"  warn: {rel} has a timeline but no resolvable zone")
            skipped += 1
            continue
        for z in zids:
            if z in index and index[z]["txt_path"] != txt:
                print(f"  warn: zone {z} claimed by both "
                      f"{index[z]['txt_path']} and {txt} - keeping the first")
                continue
            index[z] = {"tag": PurePosixPath(txt).stem, "txt_path": txt}

    if len(index) < 250:            # a parse that silently degraded
        raise SystemExit(f"only {len(index)} timelines mapped - refusing to write")
    if skipped:
        print(f"  ({skipped} files skipped, see warnings)")

    # One entry per line, numerically sorted. Readable diffs when a patch lands.
    body = ",\n".join(
        f'  "{z}": {{"tag": {json.dumps(e["tag"])}, '
        f'"txt_path": {json.dumps(e["txt_path"])}}}'
        for z, e in sorted(index.items()))
    # Sibling tmp + rename, so an interrupted run can't leave a truncated
    # file in place of the previous good output. Same idiom as the converters.
    tmp = OUT.with_name(OUT.name + ".tmp")
    tmp.write_text("{\n" + body + "\n}\n", encoding="utf-8")
    os.replace(tmp, OUT)
    print(f"wrote {OUT} ({len(index)} zone timelines, {OUT.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
