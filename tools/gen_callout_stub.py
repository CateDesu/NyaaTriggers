"""Generate a callout translation template from shipped trigger IDs and English text.
Translate its values while preserving source, target and count tokens. Skip silent
triggers. Run python tools/gen_callout_stub.py with optional --locale and --out
arguments.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_TRIGGERS = _REPO / "assets" / "triggers.json"
_MAIN = _REPO / "nyaatriggers/app_common.py"


def _app_version() -> str:
    """Read the source version from app_common.py."""
    m = re.search(r'^_VERSION\s*=\s*"([^"]+)"', _MAIN.read_text(encoding="utf-8"), re.M)
    return m.group(1) if m else "0.0.0"


def build_stub(triggers: list[dict], locale: str) -> dict:
    callouts: dict[str, str] = {}
    for t in triggers:
        if not isinstance(t, dict):
            continue
        tid, text = t.get("id"), t.get("tts_text")
        if isinstance(tid, str) and tid and isinstance(text, str) and text.strip():
            if tid in callouts:
                print(f"warning: duplicate trigger ID {tid!r}, keeping the last text",
                      file=sys.stderr)
            callouts[tid] = text
    return {"schema": 1, "app_version": _app_version(), "locale": locale,
            "callouts": callouts}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Generate a callout translation stub.")
    ap.add_argument("--locale", default="ja", help="locale code (default: ja)")
    ap.add_argument("--out", type=Path, default=None,
                    help="output path, defaults to assets/callouts_<locale>.template.json")
    args = ap.parse_args(argv)

    triggers = json.loads(_TRIGGERS.read_text(encoding="utf-8"))
    if not isinstance(triggers, list):
        print("triggers.json is not a list", file=sys.stderr)
        return 1
    stub = build_stub(triggers, args.locale)
    out = args.out or (_REPO / "assets" / f"callouts_{args.locale}.template.json")
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8",
                                         dir=out.parent, prefix=out.name + ".",
                                         suffix=".tmp", delete=False) as fh:
            tmp = Path(fh.name)
            fh.write(json.dumps(stub, ensure_ascii=False, indent=2) + "\n")
        os.replace(tmp, out)
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)
    print(f"{len(stub['callouts'])} callouts -> {out}  (app_version {stub['app_version']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
