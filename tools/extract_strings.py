"""Keep lang/<loc>.json in sync with the _() call sites in the source.

Parses every tracked .py file (via ast, so it ignores _() calls inside comments
or strings and only picks up real calls with a string-literal first argument),
collects the English keys, and merges them into lang/<loc>.json:

  * new keys are added with an empty "" stub for a translator to fill,
  * existing translations are preserved untouched,
  * keys no longer present in the source are reported (and pruned with --prune).

An empty value reads as English at runtime (see locale_util._), so an un-filled
stub is harmless. Repo tooling only, not shipped in the build.

Run:  python tools/extract_strings.py [--locale ja] [--prune] [--check]
  --check : exit 1 if the catalog is out of sync (for CI), write nothing.
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
# _() is defined in locale_util itself and re-imported widely. Scan app modules,
# skip tests, this tooling, vendored engine trees, and any virtualenv/build dir
# (checked at every path level, so a repo-root .venv can't leak _() from deps).
_SKIP_DIRS = {"tools", "triggevent-core", "triggernometry-core", ".git", "jre",
              ".venv", "venv", "env", "site-packages", "node_modules",
              "__pycache__", "build", "dist"}


def _iter_py_files() -> list[Path]:
    out = []
    for p in sorted(_REPO.rglob("*.py")):
        rel = p.relative_to(_REPO)
        if rel.name.startswith("test_") or any(part in _SKIP_DIRS for part in rel.parts):
            continue
        out.append(p)
    return out


def _keys_in(path: Path) -> set[str] | None:
    """English keys from `_("literal")` and `N_("literal")` calls in one file.
    Handles quoting edge cases. A non-literal first arg (e.g. _(var)) is skipped. N_ is the
    noop mark for strings defined in data and translated later via _(value), so the
    catalog keeps their keys too. Only static keys translate. Returns None when the
    file cannot be read or parsed: an unreadable file must not look like zero keys,
    or a broken checkout marks every one of its keys stale and --prune deletes
    live translations."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, ValueError):
        return None
    keys: set[str] = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id in ("_", "N_") and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)):
            keys.add(node.args[0].value)
    return keys


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Sync lang/<loc>.json with _() call sites.")
    ap.add_argument("--locale", default="ja")
    ap.add_argument("--prune", action="store_true",
                    help="drop catalog keys no longer present in the source")
    ap.add_argument("--check", action="store_true",
                    help="exit 1 if out of sync; write nothing (for CI)")
    args = ap.parse_args(argv)

    found: set[str] = set()
    for f in _iter_py_files():
        keys = _keys_in(f)
        if keys is None:
            # Fail the whole run, --check included. The drift report would
            # blame the keys instead of the file that will not parse.
            print(f"cannot parse {f.relative_to(_REPO)}, catalog left untouched",
                  file=sys.stderr)
            return 1
        found |= keys

    cat_path = _REPO / "lang" / f"{args.locale}.json"
    existing: dict[str, str] = {}
    if cat_path.exists():
        try:
            # utf-8-sig tolerates a BOM a hand edited catalog may carry. Plain
            # utf-8 raises below and reads the whole catalog as empty, so every
            # key reports new and a --prune rewrites every translation as "".
            raw = json.loads(cat_path.read_text(encoding="utf-8-sig"))
            if isinstance(raw, dict):
                existing = {k: v for k, v in raw.items() if isinstance(v, str)}
        except ValueError:
            pass

    new_keys = sorted(found - existing.keys())
    stale_keys = sorted(existing.keys() - found)

    merged = dict(existing)
    for k in new_keys:
        merged[k] = ""
    if args.prune:
        for k in stale_keys:
            merged.pop(k, None)
    merged = dict(sorted(merged.items()))

    untranslated = sum(1 for v in merged.values() if not v)
    print(f"{len(found)} keys in source | {len(merged)} in catalog | "
          f"{untranslated} untranslated | +{len(new_keys)} new | {len(stale_keys)} stale"
          + (" (pruned)" if args.prune else ""))
    if new_keys:
        print("  new:  " + ", ".join(repr(k) for k in new_keys[:20])
              + (" ..." if len(new_keys) > 20 else ""))
    if stale_keys and not args.prune:
        print("  stale (use --prune): " + ", ".join(repr(k) for k in stale_keys[:20])
              + (" ..." if len(stale_keys) > 20 else ""))

    if args.check:
        # Fail on EITHER drift direction: new keys (wrapped-but-untranslated) OR
        # stale keys (translated-but-orphaned, a catalog entry with no _() site).
        # Bare-stale used to pass, which let orphaned translations ship unnoticed.
        return 1 if (new_keys or stale_keys) else 0

    cat_path.parent.mkdir(parents=True, exist_ok=True)
    # Sibling tmp + rename, so an interrupted run can't leave a truncated
    # file in place of the previous good output. Same idiom as the converters.
    tmp = cat_path.with_name(cat_path.name + ".tmp")
    tmp.write_text(json.dumps(merged, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    os.replace(tmp, cat_path)
    print(f"wrote {cat_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
