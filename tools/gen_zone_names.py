#!/usr/bin/env python3
"""Regenerate zone_names.json (zone id -> English zone name) from cactbot.

The local engine matches a trigger's zone_regex against the zone name the feed
reports, which is localized by the game client, so on a non-English client no
shipped pattern ever matches and every Local trigger goes silent (the sidecars
are unaffected. They key on the numeric zone id). Shipping the id -> English
name map lets the app match the English name whatever the client speaks.

Run:  python tools/gen_zone_names.py        (writes ../zone_names.json)
"""
import json
import os
import re
import urllib.request
from pathlib import Path

SRC = ("https://raw.githubusercontent.com/OverlayPlugin/cactbot/main/"
       "resources/zone_info.ts")
OUT = Path(__file__).resolve().parent.parent / "zone_names.json"
# Bounds a hostile or broken response. The real file is well under 1 MiB.
_MAX_BYTES = 16 << 20


def main() -> None:
    with urllib.request.urlopen(SRC, timeout=30) as r:
        data = r.read(_MAX_BYTES + 1)
    if len(data) > _MAX_BYTES:
        raise SystemExit(f"response exceeds {_MAX_BYTES} bytes - refusing to parse")
    src = data.decode("utf-8")

    zones: dict[str, str] = {}
    # Entries are `  <id>: {\n ... 'name': { ... 'en': '<name>', ... },\n  },`
    for m in re.finditer(r"^  (\d+): \{(.*?)^  \},", src, re.S | re.M):
        zid, body = m.group(1), m.group(2)
        en = re.search(r"'en': '((?:[^'\\]|\\.)*)'", body)
        if en:
            zones[zid] = en.group(1).replace("\\'", "'").replace("\\\\", "\\")

    if len(zones) < 500:            # a parse that silently degraded
        raise SystemExit(f"only {len(zones)} zones parsed - refusing to write")

    # One entry per line, numerically sorted. Readable diffs when a patch lands.
    body = ",\n".join(f'  "{zid}": {json.dumps(zones[zid], ensure_ascii=False)}'
                      for zid in sorted(zones, key=int))
    # Sibling tmp + rename, so an interrupted run can't leave a truncated
    # file in place of the previous good output. Same idiom as the converters.
    tmp = OUT.with_name(OUT.name + ".tmp")
    tmp.write_text("{\n" + body + "\n}\n", encoding="utf-8")
    os.replace(tmp, OUT)
    print(f"wrote {OUT} ({len(zones)} zones, {OUT.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
