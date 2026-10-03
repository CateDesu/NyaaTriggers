#!/usr/bin/env python3
"""Build English zone names so local patterns work across client languages."""
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from nyaatriggers.http_fetch import fetch_bytes

SRC = ("https://raw.githubusercontent.com/OverlayPlugin/cactbot/main/"
       "resources/zone_info.ts")
OUT = Path(__file__).resolve().parent.parent / "assets" / "zone_names.json"
_MAX_BYTES = 16 << 20


def main() -> None:
    src = fetch_bytes(SRC, _MAX_BYTES, timeout=30).decode("utf-8")

    zones: dict[str, str] = {}
    for m in re.finditer(r"^  (\d+): \{(.*?)^  \},", src, re.S | re.M):
        zid, body = m.group(1), m.group(2)
        en = re.search(r"'en': '((?:[^'\\]|\\.)*)'", body)
        if en:
            zones[zid] = en.group(1).replace("\\'", "'").replace("\\\\", "\\")

    if len(zones) < 500:
        raise SystemExit(f"only {len(zones)} zones parsed - refusing to write")

    body = ",\n".join(f'  "{zid}": {json.dumps(zones[zid], ensure_ascii=False)}'
                      for zid in sorted(zones, key=int))
    # A sibling temporary file preserves the previous output if interrupted.
    tmp = OUT.with_name(OUT.name + ".tmp")
    tmp.write_text("{\n" + body + "\n}\n", encoding="utf-8")
    os.replace(tmp, OUT)
    print(f"wrote {OUT} ({len(zones)} zones, {OUT.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
