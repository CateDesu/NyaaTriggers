"""The shipped cactbot timeline set must match the generated zone-id index.

tools/gen_cactbot_timelines.py rebuilds cactbot_timelines.json from upstream.
When cactbot adds a dungeon the shipped timelines/ set has to follow, or a
fresh install silently misses the new fight's bars. Two fights sharing a
filename stem would shadow each other in the one flat cache. And the runtime
download cache must never write under a shipped name: on a source checkout
TIMELINES_DIR is the git tree, where that stray file blocks the next pull.
This pins all of it.

Run directly:  python test_bundled_timelines.py   (exit 0 = all pass)
"""
import json
import os
import re
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(os.path.dirname(os.path.abspath(__file__)))

FAILS = []


def check(name, cond):
    print(("PASS  " if cond else "FAIL  ") + name)
    if not cond:
        FAILS.append(name)


index = json.loads((ROOT / "cactbot_timelines.json").read_text(encoding="utf-8"))
shipped = {p.name[:-len(".cactbot.txt")] for p in (ROOT / "timelines").glob("*.cactbot.txt")}

# Every timeline serves from one flat namespace, <tag>.cactbot(.cache).txt, so
# two fights whose txt files share a stem would shadow each other.
stem_paths = defaultdict(set)
for entry in index.values():
    stem_paths[entry["tag"]].add(entry["txt_path"])
clashes = {s: sorted(p) for s, p in stem_paths.items() if len(p) > 1}
check("no two cactbot timelines share a filename stem", not clashes)
for stem, paths in clashes.items():
    print(f"  {stem}: {', '.join(paths)}")

# The dungeon set is the one the build promises on a fresh install.
dungeon_tags = {e["tag"] for e in index.values() if "/dungeon/" in e["txt_path"]}
missing = sorted(dungeon_tags - shipped)
check("every dungeon timeline in the index ships", not missing)
if missing:
    print("  missing:", ", ".join(missing))

# A shipped file the index never resolves is dead weight at best.
dead = sorted(shipped - {e["tag"] for e in index.values()})
check("every shipped cactbot timeline resolves to an index tag", not dead)
if dead:
    print("  dead:", ", ".join(dead))

empty = [p.name for p in (ROOT / "timelines").glob("*.cactbot.txt") if p.stat().st_size == 0]
check("no shipped timeline is empty", not empty)
for name in empty:
    print(f"  empty: {name}")

# The runtime cache writes <tag>.cactbot.cache.txt, never a shipped name. A
# bare .cactbot.txt write into TIMELINES_DIR lands in the git tree on source
# checkouts, untracked, and the next pull that tracks that name deadlocks.
# Pre-bundle checkouts stranded exactly this way once already. The word
# boundary keeps _BUNDLE_TIMELINES_DIR reads out of the match.
tl_src = (ROOT / "ui" / "timeline_tab.py").read_text(encoding="utf-8")
bare_writes = [
    ln.strip() for ln in tl_src.splitlines()
    if not ln.strip().startswith("#")
    and re.search(r"\bTIMELINES_DIR\b", ln)
    and ".cactbot.txt" in ln
]
check("runtime never writes a cactbot timeline under a shipped name",
      not bare_writes)
for ln in bare_writes:
    print(f"  {ln}")

print()
if FAILS:
    print(f"{len(FAILS)} FAILED: {', '.join(FAILS)}")
    sys.exit(1)
