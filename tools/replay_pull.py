#!/usr/bin/env python3
"""Replay a recorded pull through the sidecar engine jar.

Feeds a pull_logs/*.jsonl capture, raw IINACT WS lines written by
pull_capture.py, to triggevent-core.jar on stdin and summarizes what came
back: callouts fired, engine chain failures, and whether each --expect text
appeared in a callout. This is the regression harness the captures are for:
a guard patch that strands a chain shows up here before any raid night does.

  python3 tools/replay_pull.py pull_logs/DMU/2026-09-05_21-00-00-123456.jsonl
  python3 tools/replay_pull.py pull.jsonl --expect "Arrows" --expect "TT: Double"

Exit 0 when the engine ran and every --expect matched, 1 otherwise.
Repo tooling only, not shipped in the build.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_DEFAULT_JAR = _REPO / "triggevent-core" / "target" / "triggevent-core.jar"


def _java_cmd() -> "list[str]":
    java = shutil.which("java")
    if java is None and os.environ.get("JAVA_HOME"):
        candidate = Path(os.environ["JAVA_HOME"]) / "bin" / "java"
        if candidate.is_file():
            java = str(candidate)
    if java is None:
        return []
    # The engine builds Swing overlays at boot and dies on forced headless,
    # so give it a throwaway display when one is available.
    if shutil.which("xvfb-run"):
        return ["xvfb-run", "-a", "-s", "-screen 0 1024x768x24", java]
    return [java]


def main() -> int:
    ap = argparse.ArgumentParser(description="Replay a captured pull through the sidecar engine jar")
    ap.add_argument("capture", type=Path, help="pull .jsonl written by pull_capture.py")
    ap.add_argument("--expect", action="append", default=[],
                    help="text that must appear in a callout, repeatable")
    ap.add_argument("--jar", type=Path, default=Path(os.environ.get("NYAA_TRIGGEVENT_JAR", _DEFAULT_JAR)))
    ap.add_argument("--timeout", type=int, default=600, help="seconds before the replay counts as hung")
    args = ap.parse_args()

    if not args.capture.is_file():
        print(f"ERROR: no such capture: {args.capture}", file=sys.stderr)
        return 1
    if not args.jar.is_file():
        print(f"ERROR: engine jar not found: {args.jar}\n"
              f"build it with triggevent-core/build.sh or pass --jar", file=sys.stderr)
        return 1
    java = _java_cmd()
    if not java:
        print("ERROR: no java on PATH and JAVA_HOME unset", file=sys.stderr)
        return 1

    lines = args.capture.read_text(encoding="utf-8", errors="replace").splitlines()
    feed = "\n".join(l for l in lines if l.strip()) + "\n"
    print(f"replaying {args.capture.name}: {len(lines)} raw lines through {args.jar.name}")

    proc = subprocess.Popen(
        [*java, "-jar", str(args.jar)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, errors="replace")
    try:
        out, err = proc.communicate(input=feed, timeout=args.timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        print(f"ERROR: replay did not finish within {args.timeout}s", file=sys.stderr)
        return 1

    callouts = []
    for line in out.splitlines():
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if msg.get("t") == "callout" and not msg.get("expired"):
            callouts.append(msg)

    chain_failures = [l for l in err.splitlines() if "Error in sequential trigger" in l]

    print(f"engine exit {proc.returncode}, {len(callouts)} callouts fired, "
          f"{len(chain_failures)} chain failures on stderr")
    for c in callouts:
        print(f"  callout: {c.get('tts') or c.get('text') or ''}")
    for l in chain_failures[:10]:
        print(f"  chain failure: {l.strip()[-180:]}")

    ok = proc.returncode == 0
    for want in args.expect:
        hit = any(want in (c.get("tts") or "") or want in (c.get("text") or "")
                  for c in callouts)
        print(f"  {'PASS' if hit else 'FAIL'} expect: {want}")
        ok = ok and hit
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
