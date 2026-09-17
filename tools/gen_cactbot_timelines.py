#!/usr/bin/env python3
"""Build assets/cactbot_timelines.json from cactbot raidboss declarations. Resolve zone
constants and English zone patterns into numeric IDs so runtime lookup works across
client languages. Run python tools/gen_cactbot_timelines.py.
"""
import concurrent.futures
import json
import os
import re
import sys
import urllib.request
from pathlib import Path, PurePosixPath

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from nyaatriggers.http_fetch import fetch_bytes

API_TREE = ("https://api.github.com/repos/OverlayPlugin/cactbot/git/trees/"
            "main?recursive=1")
RAW = "https://raw.githubusercontent.com/OverlayPlugin/cactbot/main/"
DATA_PREFIX = "ui/raidboss/data/"
OUT = Path(__file__).resolve().parent.parent / "assets" / "cactbot_timelines.json"

_UA = {"User-Agent": "NyaaTriggers"}

# Bound tree listings and source responses.
_MAX_FETCH = 64 << 20


def _fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers=_UA)
    return fetch_bytes(req, _MAX_FETCH, timeout=30)


def _zone_id_consts() -> dict:
    """Read zone constants from cactbot zone_id.ts."""
    src = _fetch(RAW + "resources/zone_id.ts").decode("utf-8")
    return {m.group(1): int(m.group(2))
            for m in re.finditer(r"^\s*'([A-Za-z0-9_]+)': (\d+),?$", src, re.M)}


def _zone_names_en() -> dict:
    """Read English names by zone ID from zone_info.ts."""
    src = _fetch(RAW + "resources/zone_info.ts").decode("utf-8")
    names = {}
    for m in re.finditer(r"^  (\d+): \{(.*?)^  \},", src, re.S | re.M):
        en = re.search(r"'en': '((?:[^'\\]|\\.)*)'", m.group(2))
        if en:
            names[int(m.group(1))] = en.group(1).replace("\\'", "'").replace("\\\\", "\\")
    return names


def _js_regex(lit: str, flags: str) -> "re.Pattern":
    """Compile a cactbot JavaScript regex body."""
    return re.compile(lit.replace("\\/", "/"), re.I if "i" in flags else 0)


def _zone_ids_for(src: str, consts: dict, names_en: dict, rel: str) -> list:
    """Resolve declared zone IDs and zone patterns to numeric IDs."""
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
    # Match zoneRegex declarations against known English zone names.
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
    if not isinstance(tree, dict) or not isinstance(tree.get("tree"), list):
        raise SystemExit("GitHub response does not contain a file tree")
    if tree.get("truncated"):
        raise SystemExit("GitHub tree response truncated - cannot trust the walk")
    paths = [t["path"] for t in tree["tree"]
             if isinstance(t, dict) and t.get("type") == "blob"
             and isinstance(t.get("path"), str) and t["path"].startswith(DATA_PREFIX)]
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
            continue
        # Resolve timelineFile relative to the trigger source directory.
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

    if len(index) < 250:
        raise SystemExit(f"only {len(index)} timelines mapped - refusing to write")
    if skipped:
        print(f"  ({skipped} files skipped, see warnings)")

    # Sort entries by numeric ID for readable diffs.
    body = ",\n".join(
        f'  "{z}": {{"tag": {json.dumps(e["tag"])}, '
        f'"txt_path": {json.dumps(e["txt_path"])}}}'
        for z, e in sorted(index.items()))
    # Replace through a sibling temporary file to preserve previous output if
    # interrupted.
    tmp = OUT.with_name(OUT.name + ".tmp")
    tmp.write_text("{\n" + body + "\n}\n", encoding="utf-8")
    os.replace(tmp, OUT)
    print(f"wrote {OUT} ({len(index)} zone timelines, {OUT.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
