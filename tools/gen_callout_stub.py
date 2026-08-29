"""Generate a per-callout translation stub from the shipped triggers.

Reads triggers.json (a flat list of triggers, each with a stable `id` and an
English `tts_text` template) and emits a callouts_<loc>.template.json overlay:

    { "schema": 1, "app_version": "<_VERSION>", "locale": "<loc>",
      "callouts": { "<trigger-id>": "<english tts_text>" } }

Translators copy the template to callouts_<loc>.json and replace each value with
the localized template, keeping the {source}/{target}/{count} tokens intact. The
map is keyed by trigger id (not English text) so wording changes never break it.
Triggers with an empty tts_text are skipped (nothing to speak, nothing to
translate). Repo tooling only, not shipped in the build.

Run:  python tools/gen_callout_stub.py [--locale ja] [--out PATH]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_TRIGGERS = _REPO / "triggers.json"
_MAIN = _REPO / "app_common.py"


def _app_version() -> str:
    """Base _VERSION string from app_common.py (matches the release scheme)."""
    m = re.search(r'^_VERSION\s*=\s*"([^"]+)"', _MAIN.read_text(encoding="utf-8"), re.M)
    return m.group(1) if m else "0.0.0"


def build_stub(triggers: list[dict], locale: str) -> dict:
    callouts: dict[str, str] = {}
    for t in triggers:
        if not isinstance(t, dict):
            # A hand edited triggers.json can park a bare string in the list.
            continue
        tid, text = t.get("id"), t.get("tts_text")
        if isinstance(tid, str) and tid and isinstance(text, str) and text.strip():
            callouts[tid] = text
    return {"schema": 1, "app_version": _app_version(), "locale": locale,
            "callouts": callouts}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Generate a callout translation stub.")
    ap.add_argument("--locale", default="ja", help="locale code (default: ja)")
    ap.add_argument("--out", type=Path, default=None,
                    help="output path (default: callouts_<locale>.template.json in repo root)")
    args = ap.parse_args(argv)

    triggers = json.loads(_TRIGGERS.read_text(encoding="utf-8"))
    if not isinstance(triggers, list):
        print("triggers.json is not a list", file=sys.stderr)
        return 1
    stub = build_stub(triggers, args.locale)
    out = args.out or (_REPO / f"callouts_{args.locale}.template.json")
    out.write_text(json.dumps(stub, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"{len(stub['callouts'])} callouts -> {out}  (app_version {stub['app_version']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
