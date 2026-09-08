#!/usr/bin/env python3
"""Replay a recorded pull through the sidecar engine jar.

Feeds a pull_logs/*.jsonl capture, raw IINACT WS lines written by
pull_capture.py, to triggevent-core.jar on stdin and summarizes what came
back: callouts fired, engine chain failures, and whether each --expect text
appeared in a callout. This is the regression harness the captures are for:
a guard patch that strands a chain shows up here before any raid night does.

  python3 tools/replay_pull.py pull_logs/DMU/2026-09-05_21-00-00-123456.jsonl
  python3 tools/replay_pull.py pull.jsonl --expect "Arrows" --expect "TT: Double"

The feed is paced by the ACT timestamps in the capture. The engine schedules
delayed callouts and sequential chain waits on the wall clock, so dumping
minutes of log in milliseconds strands exactly the chains this harness
exists to regression test. At the default speed a replay takes about as long
as the pull did. --speed trades that fidelity for time.

Exit 0 when the engine ran and every --expect matched, 1 otherwise.
Repo tooling only, not shipped in the build.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_DEFAULT_JAR = _REPO / "triggevent-core" / "target" / "triggevent-core.jar"

# Margin over the paced duration for the auto timeout: engine boot, drain,
# and the exit grace after stdin closes.
_TIMEOUT_MARGIN_S = 120.0


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


def _line_time(line: str):
    """ACT timestamp of a LogLine message, else None. Other message types
    ride at the previous message's time."""
    try:
        msg = json.loads(line)
    except json.JSONDecodeError:
        return None
    if msg.get("type") != "LogLine":
        return None
    try:
        return datetime.fromisoformat(str(msg["line"][1]))
    except (KeyError, IndexError, TypeError, ValueError):
        return None


def _kill_tree(proc: subprocess.Popen) -> None:
    """SIGKILL the replay's whole process group, xvfb-run AND its JVM child.
    A lone proc.kill on the wrapper orphans the JVM, the same lesson the
    bridge's _signal_group encodes. Falls back to the direct child when the
    group is already gone."""
    try:
        if os.name == "posix":
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        else:
            proc.kill()
    except (OSError, ProcessLookupError):
        try:
            proc.kill()
        except OSError:
            pass


def _drain(stream, sink: "list[str]") -> None:
    for line in stream:
        sink.append(line)


def main() -> int:
    ap = argparse.ArgumentParser(description="Replay a captured pull through the sidecar engine jar")
    ap.add_argument("capture", type=Path, help="pull .jsonl written by pull_capture.py")
    ap.add_argument("--expect", action="append", default=[],
                    help="text that must appear in a callout, repeatable")
    ap.add_argument("--jar", type=Path, default=Path(os.environ.get("NYAA_TRIGGEVENT_JAR", _DEFAULT_JAR)))
    ap.add_argument("--speed", type=float, default=1.0,
                    help="feed rate multiplier. Above 1 the engine's wall clock timers "
                         "compress and delayed callouts can go missing")
    ap.add_argument("--hold", type=float, default=10.0,
                    help="seconds to hold stdin open after the last line so trailing "
                         "timers can fire before the engine's exit grace")
    ap.add_argument("--timeout", type=int, default=0,
                    help="hard budget in seconds, default derives from the capture duration")
    ap.add_argument("--fail-on-chain-failure", action="store_true",
                    help="exit 1 when any engine chain failure appears on stderr")
    args = ap.parse_args()

    if not args.capture.is_file():
        print(f"ERROR: no such capture: {args.capture}", file=sys.stderr)
        return 1
    if not args.jar.is_file():
        print(f"ERROR: engine jar not found: {args.jar}\n"
              f"build it with triggevent-core/build.sh or pass --jar", file=sys.stderr)
        return 1
    if args.speed <= 0:
        print("ERROR: --speed must be positive", file=sys.stderr)
        return 1
    java = _java_cmd()
    if not java:
        print("ERROR: no java on PATH and JAVA_HOME unset", file=sys.stderr)
        return 1

    lines = [l for l in args.capture.read_text(
        encoding="utf-8", errors="replace").splitlines() if l.strip()]
    if not lines:
        print(f"ERROR: capture has no feed lines: {args.capture}", file=sys.stderr)
        return 1

    stamps = [t for t in (_line_time(l) for l in lines) if t is not None]
    duration = (stamps[-1] - stamps[0]).total_seconds() if len(stamps) > 1 else 0.0
    budget = args.timeout or (duration / args.speed + args.hold + _TIMEOUT_MARGIN_S)
    print(f"replaying {args.capture.name}: {len(lines)} raw lines, "
          f"{duration:.0f}s of pull at {args.speed:g}x through {args.jar.name}")
    if args.speed != 1.0:
        print("note: wall clock timers compress above 1x, delayed callouts "
              "and chain waits can go missing")

    popen_kwargs = dict(stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE, text=True,
                        encoding="utf-8", errors="replace")
    if os.name == "posix":
        # Own process group so a timeout kill reaches the JVM through the
        # xvfb-run wrapper, see _kill_tree.
        popen_kwargs["start_new_session"] = True
    proc = subprocess.Popen([*java, "-jar", str(args.jar)], **popen_kwargs)

    out_lines: "list[str]" = []
    err_lines: "list[str]" = []
    t_out = threading.Thread(target=_drain, args=(proc.stdout, out_lines), daemon=True)
    t_err = threading.Thread(target=_drain, args=(proc.stderr, err_lines), daemon=True)
    t_out.start()
    t_err.start()

    start = time.monotonic()
    deadline = start + budget
    failed = None
    try:
        first = None
        for line in lines:
            if time.monotonic() > deadline:
                raise TimeoutError
            t = _line_time(line)
            if t is not None:
                if first is None:
                    first = t
                try:
                    due = (t - first).total_seconds() / args.speed
                except TypeError:
                    due = 0.0  # mixed aware and naive stamps, just keep going
                wait = due - (time.monotonic() - start)
                if wait > 0:
                    time.sleep(wait)
            proc.stdin.write(line + "\n")
        if args.hold > 0:
            time.sleep(min(args.hold, max(0.0, deadline - time.monotonic())))
        proc.stdin.close()
        proc.wait(timeout=max(1.0, deadline - time.monotonic()))
    except BrokenPipeError:
        failed = f"engine died mid-replay, exit {proc.poll()}"
    except (TimeoutError, subprocess.TimeoutExpired):
        failed = f"replay did not finish within {budget:.0f}s"
    finally:
        if proc.poll() is None:
            _kill_tree(proc)
            proc.wait()
        t_out.join(timeout=5)
        t_err.join(timeout=5)
    if failed:
        print(f"ERROR: {failed}", file=sys.stderr)
        return 1

    out = "".join(out_lines)
    err = "".join(err_lines)
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
    if args.fail_on_chain_failure and chain_failures:
        ok = False
    for want in args.expect:
        hit = any(want in (c.get("tts") or "") or want in (c.get("text") or "")
                  for c in callouts)
        print(f"  {'PASS' if hit else 'FAIL'} expect: {want}")
        ok = ok and hit
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
