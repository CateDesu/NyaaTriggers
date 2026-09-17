#!/usr/bin/env python3
"""Replay captured JSONL feeds through the engine and report callouts, chain failures and
expected text matches. Run python3 tools/replay_pull.py pull.jsonl with optional
--expect text. Pace by ACT timestamps so delayed sequences behave as in the original
pull. --speed shortens runtime at the cost of timing fidelity. Exit zero only when the
engine runs and all expectations match.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_DEFAULT_JAR = _REPO / "triggevent-core" / "target" / "triggevent-core.jar"

# Allow extra time for engine startup and shutdown beyond the paced feed duration.
_TIMEOUT_MARGIN_S = 120.0


def _java_cmd() -> "list[str]":
    java = shutil.which("java")
    if java is None and os.environ.get("JAVA_HOME"):
        candidate = Path(os.environ["JAVA_HOME"]) / "bin" / "java"
        if candidate.is_file():
            java = str(candidate)
    if java is None:
        return []
    # Use a temporary display because the engine creates Swing overlays during startup.
    if shutil.which("xvfb-run"):
        return ["xvfb-run", "-a", "-s", "-screen 0 1024x768x24", java]
    return [java]


def _line_time(line: str):
    """Read an ACT log timestamp. Other messages use the preceding timestamp."""
    try:
        msg = json.loads(line)
    except (ValueError, RecursionError):
        return None
    if not isinstance(msg, dict) or msg.get("type") != "LogLine":
        return None
    try:
        fields = msg["line"]
        if not isinstance(fields, list):
            return None
        stamp = datetime.fromisoformat(str(fields[1]))
        # Captures without an offset use UTC throughout duration and pacing.
        return stamp.replace(tzinfo=timezone.utc) if stamp.tzinfo is None else stamp
    except (KeyError, IndexError, TypeError, ValueError):
        return None


def _kill_tree(proc: subprocess.Popen) -> None:
    """Kill the whole replay process group, including the JVM behind xvfb-run. Fall back to
    the direct child if needed.
    """
    try:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGKILL)
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
    if not math.isfinite(args.speed) or args.speed <= 0:
        print("ERROR: --speed must be positive", file=sys.stderr)
        return 1
    if not math.isfinite(args.hold) or args.hold < 0 or args.timeout < 0:
        print("ERROR: --hold and --timeout must be finite and nonnegative", file=sys.stderr)
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
    duration = max(((t - stamps[0]).total_seconds() for t in stamps), default=0.0)
    try:
        paced_duration = duration / args.speed
        budget = float(args.timeout) or (paced_duration + args.hold + _TIMEOUT_MARGIN_S)
        valid_budget = math.isfinite(budget) and math.isfinite(paced_duration)
    except OverflowError:
        valid_budget = False
    if not valid_budget:
        print("ERROR: replay duration exceeds the supported time budget", file=sys.stderr)
        return 1
    print(f"replaying {args.capture.name}: {len(lines)} raw lines, "
          f"{duration:.0f}s of pull at {args.speed:g}x through {args.jar.name}")
    if args.speed != 1.0:
        print("note: wall clock timers compress above 1x, delayed callouts "
              "and chain waits can go missing")

    popen_kwargs = dict(stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE, text=True,
                        encoding="utf-8", errors="replace")
    if os.name == "posix":
        # Create a process group so timeout cleanup reaches the JVM through xvfb-run.
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
    stopped = threading.Event()
    feed_errors = []

    def feed():
        first = None
        try:
            for line in lines:
                if stopped.is_set():
                    return
                t = _line_time(line)
                if t is not None:
                    if first is None:
                        first = t
                    due = (t - first).total_seconds() / args.speed
                    wait = due - (time.monotonic() - start)
                    if wait > 0 and stopped.wait(wait):
                        return
                proc.stdin.write(line + "\n")
                proc.stdin.flush()
            stopped.wait(args.hold)
        except (OSError, ValueError) as exc:
            feed_errors.append(str(exc))
        finally:
            try:
                proc.stdin.close()
            except OSError:
                pass

    # Keep the deadline independent of pipe writes and timestamp waits.
    writer = threading.Thread(target=feed, daemon=True)
    writer.start()
    try:
        writer.join(timeout=max(0.0, deadline - time.monotonic()))
        if writer.is_alive():
            raise subprocess.TimeoutExpired(proc.args, budget)
        proc.wait(timeout=max(0.0, deadline - time.monotonic()))
        if feed_errors:
            failed = f"engine died mid-replay, exit {proc.returncode}: {feed_errors[0]}"
        elif time.monotonic() > deadline:
            raise subprocess.TimeoutExpired(proc.args, budget)
    except subprocess.TimeoutExpired:
        failed = f"replay did not finish within {budget:.0f}s"
    finally:
        stopped.set()
        if failed or proc.poll() is None:
            _kill_tree(proc)
            proc.wait()
        writer.join(timeout=5)
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
        if isinstance(msg, dict) and msg.get("t") == "callout" and not msg.get("expired"):
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
