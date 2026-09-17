"""Sync locale catalogs with literal translation calls in program sources. Preserve
existing translations and add empty entries for new keys. --prune removes stale keys.
--check reports drift without writing. Run python tools/extract_strings.py with an
optional --locale.
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
# Scan program modules while excluding tests, tools, vendored code and build
# environments at every directory level.
_SKIP_DIRS = {"tools", "tests", "triggevent-core", "triggernometry-core", ".git", "jre",
              ".venv", "venv", "env", "site-packages", "node_modules",
              "__pycache__", "build", "dist", "local"}


def _iter_py_files() -> list[Path]:
    out = []
    for p in sorted(_REPO.rglob("*.py")):
        rel = p.relative_to(_REPO)
        if rel.name.startswith("test_") or any(part in _SKIP_DIRS for part in rel.parts):
            continue
        out.append(p)
    return out


def _keys_in(path: Path) -> set[str] | None:
    """Collect literal _ and N_ calls. Return None for unreadable or invalid sources so
    pruning cannot remove their live keys.
    """
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
            # Fail on unreadable sources so their keys cannot be mistaken for stale
            # translations.
            print(f"cannot parse {f.relative_to(_REPO)}, catalog left untouched",
                  file=sys.stderr)
            return 1
        found |= keys

    cat_path = _REPO / "lang" / f"{args.locale}.json"
    existing: dict[str, str] = {}
    if cat_path.exists():
        try:
            # Accept a BOM from editors without changing translated values.
            raw = json.loads(cat_path.read_text(encoding="utf-8-sig"))
            if not isinstance(raw, dict):
                raise ValueError("catalog must be an object")
            existing = {k: v for k, v in raw.items() if isinstance(v, str)}
        except (OSError, ValueError) as exc:
            print(f"cannot read {cat_path}: {exc}, catalog left untouched",
                  file=sys.stderr)
            return 1

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
        # Report both missing and stale keys.
        return 1 if (new_keys or stale_keys) else 0

    cat_path.parent.mkdir(parents=True, exist_ok=True)
    # Replace through a sibling temporary file to preserve previous output if
    # interrupted.
    tmp = cat_path.with_name(cat_path.name + ".tmp")
    tmp.write_text(json.dumps(merged, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    os.replace(tmp, cat_path)
    print(f"wrote {cat_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
