"""Regression tests for pull_capture.py.

Covers the pull segmentation and file layout of the raw feed recorder:
buffered pre-pull lines make the capture, a boss ability opens a pull, a
player ability does not, a wipe finalizes with the right outcome, and
recording off means nothing lands anywhere.

Run directly:  python test_pull_capture.py   (exit 0 = all pass)
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pull_capture import PullCapture

FAILS = []


def check(name, cond):
    print(("PASS  " if cond else "FAIL  ") + name)
    if not cond:
        FAILS.append(name)


def _pull_files(td):
    return sorted(Path(td).rglob("*.jsonl"))


def _meta_files(td):
    return sorted(Path(td).rglob("*.meta.json"))


_BOSS_CAST = "20|ts|40001234|Boss|BAB9|Tele-trouncing|40001234|Boss|"

with tempfile.TemporaryDirectory() as td:
    cap = PullCapture(Path(td))
    cap.context = lambda: ("DMU", "The Hole")

    # Not recording: nothing buffers, nothing writes.
    cap.on_raw_message('{"type":"LogLine"}')
    cap.on_log_line(_BOSS_CAST)
    check("nothing is captured while recording is off", _pull_files(td) == [])

    cap.set_recording(True)
    cap.on_raw_message('{"type":"pre"}')
    check("no pull file before a combat start", _pull_files(td) == [])

    # A player cast must not open a pull.
    cap.on_log_line("21|ts|10700001|Player|BAB9|Some Cast|10700001|Player|")
    check("a player ability does not open a pull", _pull_files(td) == [])

    # A boss cast opens the pull, the buffered pre-pull line comes along.
    cap.on_log_line(_BOSS_CAST)
    files = _pull_files(td)
    check("a boss ability opens the pull file", len(files) == 1)
    check("the pull file lands under the fight folder",
          bool(files) and files[0].parent.name == "DMU")
    cap.on_raw_message('{"type":"mid"}')
    cap.on_raw_message('{"type":"with\nnewline"}')
    content = files[0].read_text(encoding="utf-8")
    check("the pre-pull buffer and live lines are in the capture",
          '{"type":"pre"}' in content and '{"type":"mid"}' in content)
    check("raw newlines are flattened like the sidecar feed does",
          '{"type":"with newline"}' in content)

    # A wipe finalizes and writes the meta.
    cap.on_log_line("33|ts|40001234|4000000F|00|")
    metas = _meta_files(td)
    check("a wipe writes the meta file", len(metas) == 1)
    meta = json.loads(metas[0].read_text(encoding="utf-8"))
    check("the wipe outcome is recorded",
          meta.get("outcome") == "wipe" and meta.get("fight") == "DMU")
    check("the line count matches the capture",
          meta.get("lines") == len(files[0].read_text(encoding="utf-8").splitlines()))
    cap.on_raw_message('{"type":"after-wipe"}')
    check("nothing appends after the wipe",
          "after-wipe" not in files[0].read_text(encoding="utf-8"))

    # Second pull ends on combat end, a clear.
    cap.on_log_line(_BOSS_CAST)
    check("a second pull opens", len(_pull_files(td)) == 2)
    cap.on_in_combat(True, False)
    meta2 = json.loads(_meta_files(td)[1].read_text(encoding="utf-8"))
    check("combat end finalizes as a clear", meta2.get("outcome") == "clear")

    # Zone change resets, recording off ends.
    cap.on_log_line(_BOSS_CAST)
    cap.on_zone_changed(1234, "Elsewhere")
    meta3 = json.loads(_meta_files(td)[2].read_text(encoding="utf-8"))
    check("a zone change finalizes as a reset", meta3.get("outcome") == "reset")
    cap.on_log_line(_BOSS_CAST)
    cap.set_recording(False)
    meta4 = json.loads(_meta_files(td)[3].read_text(encoding="utf-8"))
    check("switching off finalizes as ended", meta4.get("outcome") == "ended")
    cap.on_log_line(_BOSS_CAST)
    check("off stays off", len(_pull_files(td)) == 4)

# Replay smoke, opt-in since it boots the real engine jar. The capture above
# is synthetic and fires no callouts, this only proves the jar takes the file.
jar = Path("triggevent-core/target/triggevent-core.jar")
if os.environ.get("NYAA_REPLAY_TEST") == "1" and jar.is_file():
    with tempfile.TemporaryDirectory() as td:
        cap = PullCapture(Path(td))
        cap.context = lambda: ("DMU", "The Hole")
        cap.set_recording(True)
        cap.on_log_line(_BOSS_CAST)
        cap.on_in_combat(True, False)
        capture = _pull_files(td)[0]
        r = subprocess.run([sys.executable, "tools/replay_pull.py", str(capture)],
                           capture_output=True, text=True, timeout=660)
        check("the capture replays through the engine jar cleanly", r.returncode == 0)
else:
    print("SKIP  engine replay smoke (set NYAA_REPLAY_TEST=1 with the jar built)")

print()
if FAILS:
    print(f"{len(FAILS)} FAILED: {', '.join(FAILS)}")
    sys.exit(1)
print("all tests passed")
