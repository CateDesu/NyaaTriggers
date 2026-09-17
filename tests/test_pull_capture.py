"""Pull capture boundaries, feed contents and recording controls."""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nyaatriggers.pull_capture import PullCapture
from nyaatriggers import drop_log
from nyaatriggers import pull_capture

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

with tempfile.TemporaryDirectory() as td:
    cap = PullCapture(Path(td))
    cap.context = lambda: ("..", "The Hole")
    cap.set_recording(True)

    # An all-dots fight tag cannot escape the capture tree.
    cap.on_log_line(_BOSS_CAST)
    files = _pull_files(td)
    check("an all dots fight tag lands in Unknown",
          len(files) == 1 and files[0].parent.name == "Unknown")

    # A raw zone line finalizes like the WS zone event does.
    cap.on_log_line("01|ts|1234|The Dead-End|")
    meta = json.loads(_meta_files(td)[0].read_text(encoding="utf-8"))
    check("a raw zone line finalizes as a reset", meta.get("outcome") == "reset")

    # Feed loss closes the pull and reconnect starts a separate capture.
    cap.on_log_line(_BOSS_CAST)
    cap.on_status_changed(False, "Disconnected")
    meta = json.loads(_meta_files(td)[1].read_text(encoding="utf-8"))
    check("a feed drop finalizes as feed-lost", meta.get("outcome") == "feed-lost")
    cap.on_status_changed(True, "Connected")
    cap.on_log_line(_BOSS_CAST)
    check("the next pull opens its own file after a feed drop",
          len(_pull_files(td)) == 3)
    cap.set_recording(False)

with tempfile.TemporaryDirectory() as td:
    cap = PullCapture(Path(td))
    cap.context = lambda: ("Cap", "Zone")
    cap.set_recording(True)

    # The pre-pull ring is bounded in count, not only in seconds.
    for i in range(pull_capture._PRE_PULL_MAX_MESSAGES + 100):
        cap.on_raw_message(f'{{"n":{i}}}')
    cap.on_log_line(_BOSS_CAST)
    content = _pull_files(td)[0].read_text(encoding="utf-8")
    check("the pre-pull ring keeps only the newest messages",
          '{"n":0}' not in content
          and f'{{"n":{pull_capture._PRE_PULL_MAX_MESSAGES + 99}}}' in content)

    # Duration and size caps truncate a pull that never closes on its own.
    orig_s = pull_capture._MAX_PULL_SECONDS
    orig_b = pull_capture._MAX_PULL_BYTES
    try:
        pull_capture._MAX_PULL_SECONDS = 0
        cap.on_raw_message('{"type":"slow"}')
        meta = json.loads(_meta_files(td)[0].read_text(encoding="utf-8"))
        check("an overlong pull is truncated", meta.get("outcome") == "truncated")
        cap.on_log_line(_BOSS_CAST)
        pull_capture._MAX_PULL_BYTES = 100
        cap.on_raw_message('{"type":"' + "x" * 200 + '"}')
        meta = json.loads(_meta_files(td)[1].read_text(encoding="utf-8"))
        check("an oversized pull is truncated", meta.get("outcome") == "truncated")
    finally:
        pull_capture._MAX_PULL_SECONDS = orig_s
        pull_capture._MAX_PULL_BYTES = orig_b

with tempfile.TemporaryDirectory() as td:
    cap = PullCapture(Path(td))
    cap.context = lambda: ("Keep", "Zone")
    cap.set_recording(True)
    orig_k = pull_capture._KEEP_CAPTURES
    pull_capture._KEEP_CAPTURES = 3
    try:
        for _ in range(6):
            cap.on_log_line(_BOSS_CAST)
            cap.on_in_combat(True, False)
        check("old captures are pruned to the keep count",
              len(_pull_files(td)) == 3)
        check("their metas are pruned too", len(_meta_files(td)) == 3)
    finally:
        pull_capture._KEEP_CAPTURES = orig_k

with tempfile.TemporaryDirectory() as td:
    blocker = Path(td) / "not-a-dir"
    blocker.write_text("x", encoding="utf-8")
    dlog = Path(td) / "nyaatriggers.log"
    orig_log = drop_log._LOG_FILE
    drop_log._LOG_FILE = dlog
    try:
        cap = PullCapture(blocker)
        cap.context = lambda: ("DMU", "Zone")
        cap.set_recording(True)
        cap.on_log_line(_BOSS_CAST)
        cap.on_log_line(_BOSS_CAST)
        check("an unwritable folder records nothing",
              _pull_files(td) == [])
        check("and leaves exactly one drop log line",
              dlog.read_text(encoding="utf-8").count("[pull-capture]") == 1)
    finally:
        drop_log._LOG_FILE = orig_log

# Optional replay against the real engine jar using actual feed lines.
REPO = Path(__file__).resolve().parents[1]
jar = REPO / "triggevent-core" / "target" / "triggevent-core.jar"
if os.environ.get("NYAA_REPLAY_TEST") == "1" and jar.is_file():
    with tempfile.TemporaryDirectory() as td:
        cap = PullCapture(Path(td))
        cap.context = lambda: ("DMU", "The Hole")
        cap.set_recording(True)
        cap.on_raw_message('{"type":"ChangeZone","zoneID":1234,"zoneName":"The Hole"}')
        cap.on_log_line(_BOSS_CAST)
        cap.on_raw_message('{"type":"LogLine","line":["20","2026-09-05T21:00:00.0000000-05:00",'
                           '"40001234","Boss","BAB9","Tele-trouncing","40001234","Boss"]}')
        cap.on_raw_message('{"type":"LogLine","line":["21","2026-09-05T21:00:02.0000000-05:00",'
                           '"10700001","Player","BABA","Some Cast","10700001","Player"]}')
        cap.on_in_combat(True, False)
        capture = _pull_files(td)[0]
        check("the smoke capture holds raw feed lines",
              len(capture.read_text(encoding="utf-8").splitlines()) >= 3)
        r = subprocess.run([sys.executable, str(REPO / "tools" / "replay_pull.py"),
                            str(capture), "--hold", "2"],
                           capture_output=True, text=True, timeout=660, cwd=REPO)
        check("the capture replays through the engine jar cleanly", r.returncode == 0)
        check("the engine's chain machinery survived the feed",
              "0 chain failures" in r.stdout)
else:
    print("SKIP  engine replay smoke (set NYAA_REPLAY_TEST=1 with the jar built)")

print()
if FAILS:
    print(f"{len(FAILS)} FAILED: {', '.join(FAILS)}")
    sys.exit(1)
print("all tests passed")
