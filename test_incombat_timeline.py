#!/usr/bin/env python3
"""InCombat wiring: timeline combat start via the synthetic 260 line, and the
opt-in leave-combat reset used by the striking-dummy sample fight.

Run: QT_QPA_PLATFORM=offscreen python3 test_incombat_timeline.py
"""
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PyQt6.QtWidgets import QApplication

import timeline_parser
from dps_meter import DpsMeter
from main_window import MainWindow
from timeline_engine import TimelineEngine
from umad_chains import BlackHoleChains, CursedShriekPairs, StatusPairs

_app = QApplication.instance() or QApplication(sys.argv)

PASS = 0
FAIL = 0


def check(name: str, ok: bool) -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"PASS  {name}")
    else:
        FAIL += 1
        print(f"FAIL  {name}")


SAMPLE = """# reset-on-combat-end

hideall "--Reset--"
hideall "--sync--"

0.0 "--Reset--" ActorControl { command: "4000000F" } window 0,100000 jump 0
0.0 "--sync--" InCombat { inGameCombat: "1" } window 0,1
3.0 "Tankbuster"
"""


def sample_entries():
    return timeline_parser.parse(SAMPLE)


class FakeLink:
    def __init__(self):
        self.clears = 0
        self.schedules = []

    def send_clear(self):
        self.clears += 1

    def send_timeline(self, rows):
        self.schedules.append(list(rows))


def make_window():
    """Duck-typed host for MainWindow._on_in_combat (unbound method call)."""
    class W:
        pass

    w = W()
    w._in_game_combat = False
    w._timeline_reset_on_combat_end = False
    w._timeline = TimelineEngine()
    w._plugin_link = FakeLink()
    # _on_in_combat also feeds the DPS meter (settings-gated). Provide both.
    w._settings = {"dps_meter_enabled": True}
    w._dps_meter = DpsMeter()
    w._push_timeline_to_plugin = lambda: MainWindow._push_timeline_to_plugin(w)
    return w


def call(w, act, game):
    MainWindow._on_in_combat(w, act, game)


# ── Engine: combat start via the 260 InCombat line ───────────────────────────

eng = TimelineEngine()
eng.load(sample_entries())
spoken = []
eng.tts.connect(spoken.append)

# A player-sourced ability must NOT start the clock (player ids begin with 1).
eng.process_line(["21", "", "10000001", "10000001", "Test", "00", "", ""])
check("player ability does not start the clock", not eng.is_active())

# The synthetic 260 with game combat on starts the clock and arms the sync.
eng.process_line(["260", "", "1", "1"])
check("260 inGameCombat=1 starts the clock", eng.is_active())

# _tick runs on a QTimer: drive the event loop past the entry's 3s mark.
# Generous slack, a loaded CI runner can stall the event loop well past it.
deadline = time.monotonic() + 6.0
while time.monotonic() < deadline:
    _app.processEvents()
    time.sleep(0.02)
check("sample entry speaks after its time passes", "Tankbuster" in spoken)

# A 260 with game combat OFF does not start anything by itself.
eng2 = TimelineEngine()
eng2.load(sample_entries())
eng2.process_line(["260", "", "0", "0"])
check("260 inGameCombat=0 does not start the clock", not eng2.is_active())

# A non-player ability still starts the clock the classic way.
eng3 = TimelineEngine()
eng3.load(sample_entries())
eng3.process_line(["21", "", "40001234", "40001234", "Test", "00", "", ""])
check("non-player ability starts the clock", eng3.is_active())

# ── Window glue: leave-combat reset is opt-in via the file marker ────────────

w = make_window()
w._timeline.load(sample_entries())
w._timeline_reset_on_combat_end = True
call(w, True, True)   # engage
check("window: engage starts the clock", w._timeline.is_active())
call(w, True, False)  # leave combat
check("window: leave with marker resets the clock", not w._timeline.is_active())
check("window: leave with marker clears the plugin once", w._plugin_link.clears == 1)
check("window: schedule re-armed after reset", len(w._plugin_link.schedules) == 1)

# Re-engaging re-syncs from zero.
call(w, True, True)
check("window: re-engage restarts the clock", w._timeline.is_active())

# Without the marker, leaving combat must NOT reset (raid intermissions).
w2 = make_window()
w2._timeline.load(sample_entries())
call(w2, True, True)
call(w2, True, False)
check("window: leave without marker keeps the clock", w2._timeline.is_active())
check("window: leave without marker sends no clear", w2._plugin_link.clears == 0)

# ── Full chain: ws_client parses the event and the handler drives the engine ─

import json

from ws_client import WSClient

wc = WSClient()
w3 = make_window()
w3._timeline.load(sample_entries())
w3._timeline_reset_on_combat_end = True
wc.in_combat.connect(lambda act, game: MainWindow._on_in_combat(w3, act, game))

wc._on_message(json.dumps({"type": "InCombat", "inACTCombat": True, "inGameCombat": True}))
check("chain: InCombat message starts the clock", w3._timeline.is_active())
check("chain: game combat state tracked", w3._in_game_combat is True)

wc._on_message(json.dumps({"type": "InCombat", "inACTCombat": False, "inGameCombat": False}))
check("chain: combat end resets + clears", not w3._timeline.is_active() and w3._plugin_link.clears == 1)

wc._on_message(json.dumps({"type": "InCombat", "inACTCombat": True, "inGameCombat": True}))
check("chain: re-engage restarts the clock", w3._timeline.is_active())

# ── Engine: forcejump loop re-arms the run while still in combat ─────────────

LOOP = """hideall "--sync--"
hideall "--loop--"

0.0 "--sync--" InCombat { inGameCombat: "1" } window 0,1
1.0 "Tankbuster"
2.0 "--loop--" forcejump 0
"""

eng4 = TimelineEngine()
eng4.load(timeline_parser.parse(LOOP))
spoken4 = []
eng4.tts.connect(spoken4.append)
eng4.process_line(["260", "", "1", "1"])
check("loop: clock starts", eng4.is_active())

deadline = time.monotonic() + 7.5
while time.monotonic() < deadline:
    _app.processEvents()
    time.sleep(0.02)
# 1s entry fires, 2s loop snaps back to 0, the 1s entry fires again.
check("loop: entry fires twice around the forcejump", spoken4.count("Tankbuster") >= 2)

# ── Window glue: a pull starting with an empty schedule re-arms the timeline ──

w4 = make_window()
loads = []
w4._match_zone = "Dancing Mad (Ultimate)"
w4._load_timeline_for_zone = lambda zone: (loads.append(zone), w4._timeline.load(sample_entries()))
call(w4, True, True)   # engage with nothing loaded (mid-instance restart)
check("empty schedule on engage re-arms from the current zone",
      loads == ["Dancing Mad (Ultimate)"])
check("re-armed timeline starts its clock on the same engage", w4._timeline.is_active())

# A loaded schedule must not be re-armed (that would reset a running clock).
w5 = make_window()
w5._timeline.load(sample_entries())
w5._match_zone = "Dancing Mad (Ultimate)"
def _no_reload(zone):
    raise AssertionError("must not reload")
w5._load_timeline_for_zone = _no_reload
call(w5, True, True)
check("loaded schedule is left alone on engage", w5._timeline.is_active())

# Out-of-combat flips never re-arm.
w6 = make_window()
w6._match_zone = "Dancing Mad (Ultimate)"
w6._load_timeline_for_zone = _no_reload
call(w6, False, False)
check("no engage, no re-arm", not w6._timeline.is_active())

# ── Window glue: a wipe re-pushes the schedule it just cleared ─────────────

w7 = make_window()
w7._timeline.load(sample_entries())
# The 33 branch of _dispatch_log_line also tears down automark and chain
# state. Cold real engines and lists, stubs where the UI would listen.
w7._status_timers = []
w7._seq_runners = []
w7._umad_chains = BlackHoleChains(role_of=lambda aid: None)
w7._umad_chain_pending = []
w7._umad_actor_names = {}
w7._umad_gaze = CursedShriekPairs()
w7._umad_gaze_pending = []
w7._automark_pairs = StatusPairs([])
w7._automark_pending = []
w7._automark_active = {}
w7._automark_rules = []
w7._local_enabled = True
w7._triggers = []
w7._clear_status_timers = lambda: MainWindow._clear_status_timers(w7)
w7._clear_seq_runners = lambda: MainWindow._clear_seq_runners(w7)
w7._umad_chain_reset = lambda clear_marks=False: \
    MainWindow._umad_chain_reset(w7, clear_marks=clear_marks)
w7._umad_gaze_reset = lambda clear_marks=False: \
    MainWindow._umad_gaze_reset(w7, clear_marks=clear_marks)
w7._clear_player = lambda actor, name="": True
w7._append_ability_line = lambda fields: None
MainWindow._dispatch_log_line(w7, ["33", "ts", "0", "4000000F"], "33|ts|0|4000000F")
check("wipe clears the plugin once", w7._plugin_link.clears == 1)
check("wipe re-pushes the schedule right after the clear",
      len(w7._plugin_link.schedules) == 1
      and w7._plugin_link.schedules[0] == w7._timeline.upcoming())
check("wipe re-push is the loaded schedule, not an empty one",
      len(w7._plugin_link.schedules[0]) > 0)

# ── Window glue: re-enabling local callouts re-pushes the schedule ─────────

w8 = make_window()
w8._timeline.load(sample_entries())
w8._clear_status_timers = lambda: None
w8._save_settings = lambda: None
MainWindow._set_local_enabled(w8, False)
check("local off clears the plugin", w8._plugin_link.clears == 1)
check("local off pushes no schedule", w8._plugin_link.schedules == [])
MainWindow._set_local_enabled(w8, True)
check("local re-enable re-pushes the schedule",
      len(w8._plugin_link.schedules) == 1
      and w8._plugin_link.schedules[0] == w8._timeline.upcoming())

# ── Parser: array syntax inside a quoted scalar fabricates no sync key ─────

e = timeline_parser.parse(
    "1.0 \"x\" AddedCombatant { name: \"id: ['9D00', '9D01']\", source: \"Boss\" }"
)[0]
check("array syntax inside a quoted scalar fabricates no id",
      "id" not in e.event_fields)
check("the quoted scalar value itself is kept whole",
      e.event_fields.get("name") == "id: ['9D00', '9D01']")

e = timeline_parser.parse(
    '1.0 "x" StartsUsing { id: ["9D00", "9D01"], source: "Boss" }'
)[0]
check("real array field still folds to an alternation",
      e.event_fields.get("id") == "(?:9D00|9D01)")

# ── Parser: hideall on a plain label silences it like cactbot ──────────────

hid = timeline_parser.parse(
    'hideall "Secret Tech"\n'
    '0.0 "--sync--" InCombat { inGameCombat: "1" } window 0,1\n'
    '5.0 "Secret Tech"\n'
    '6.0 "Loud Tech"\n'
)
check("hideall on a plain label marks the entry internal",
      next(e for e in hid if e.label == "Secret Tech").is_internal)
check("a label no hideall names stays visible",
      not next(e for e in hid if e.label == "Loud Tech").is_internal)

eng5 = TimelineEngine()
eng5.load(hid)
check("hidden entries stay off the bar schedule",
      [lbl for _t, lbl in eng5.upcoming()] == ["Loud Tech"])

# ── Parser: an apostrophe outside quotes must not save the comment ─────────

e = timeline_parser.parse(
    "10.0 \"adds\" sync /Boss's Add/ # window 9 jump 0"
)[0]
check("comment after a stray apostrophe still strips, no clauses leak",
      e.jump is None and e.window_before == 2.5 and e.window_after == 2.5)
check("the legacy sync itself is still lifted", e.legacy_sync)

e = timeline_parser.parse(
    "1.0 \"x\" StartsUsing { id: '8B42' } # window 9"
)[0]
check("single-quoted value parses and its trailing comment strips",
      e.event_fields.get("id") == "8B42" and e.window_before == 2.5)

# ── Parser: clauses inside a quoted jump target stay inside it ─────────────

ent = timeline_parser.parse(
    '10.0 "X" jump "the window 14 door"\n'
    '3.0 label "the window 14 door"\n'
)
x = next(e for e in ent if e.label == "X")
check("window text inside a jump label sets no window",
      x.window_before == 2.5 and x.window_after == 2.5)
check("the jump label itself survives and resolves", x.jump == 3.0)

x = timeline_parser.parse(
    '10.0 "X" jump "the window 14 door" window 8,2'
)[0]
check("an explicit window beats window text in a jump label",
      x.window_before == 8.0 and x.window_after == 2.0)

x = timeline_parser.parse(
    "10.0 \"X\" jump \"StartsUsing { id: '8B42' }\""
)[0]
check("a brace block inside a jump label fakes no event",
      x.event_type == "" and x.event_fields == {}
      and x.jump_label == "StartsUsing { id: '8B42' }")

x = timeline_parser.parse('10.0 "X" jump "sync /foo/"')[0]
check("a sync regex inside a jump label fakes no legacy flag",
      not x.legacy_sync and x.jump_label == "sync /foo/")

# ── Parser: jump text inside quoted values and sync bodies arms nothing ─────

x = timeline_parser.parse(
    '100.0 "Chat call" GameLog { line: ".*jump 5.*" } code: "0038"'
)[0]
check("jump text inside a quoted event value arms no jump",
      x.jump is None and not x.force_jump)
check("the quoted event value itself survives whole",
      x.event_fields.get("line") == ".*jump 5.*")

x = timeline_parser.parse(
    '5.0 "x" StartsUsing { name: "jump 5" }'
)[0]
check("a jump token as an event value stays the value",
      x.event_fields.get("name") == "jump 5" and x.jump is None)

x = timeline_parser.parse(
    "5.0 \"x\" StartsUsing { name: 'jump 5' }"
)[0]
check("a single-quoted jump value arms no jump either",
      x.event_fields.get("name") == "jump 5" and x.jump is None)

x = timeline_parser.parse(
    '10.0 "adds" sync /.*jump 5.*/'
)[0]
check("jump text inside a legacy sync body arms no jump",
      x.jump is None and x.legacy_sync)

x = timeline_parser.parse(
    '5.0 "x" GameLog { line: "forcejump 12" }'
)[0]
check("forcejump inside a quoted value arms nothing",
      x.jump is None and not x.force_jump
      and x.event_fields.get("line") == "forcejump 12")

x = timeline_parser.parse('10.0 "x" jump 5')[0]
check("a real jump clause still parses", x.jump == 5.0)

ent = timeline_parser.parse(
    '10.0 "x" jump "the door"\n'
    '3.0 label "the door"\n'
)
x = next(e for e in ent if e.label == "x")
check("a real jump label still resolves", x.jump == 3.0)

x = timeline_parser.parse('10.0 "x" forcejump 12')[0]
check("a real forcejump still parses", x.jump == 12.0 and x.force_jump)

# ── Parser: an escaped quote stays inside its quoted event value ───────────

x = timeline_parser.parse(
    '1.0 "x" StartsUsing { name: "say \\"hi\\"", id: "8B42" }'
)[0]
check("an escaped quote does not end a double-quoted value",
      x.event_fields.get("name") == 'say \\"hi\\"'
      and x.event_fields.get("id") == "8B42")

x = timeline_parser.parse(
    "1.0 \"x\" StartsUsing { name: 'it\\'s', other: \"a\\\\b\" }"
)[0]
check("escaped quote and backslash survive their quoted values",
      x.event_fields.get("name") == "it\\'s"
      and x.event_fields.get("other") == "a\\\\b")

x = timeline_parser.parse(
    '1.0 "x" StartsUsing { id: ["9D\\"0", "9D01"] }'
)[0]
check("an escaped quote inside an array item folds like the rest",
      x.event_fields.get("id") == '(?:9D\\"0|9D01)')

import re as _re
check("the kept escape still matches the literal text downstream",
      _re.fullmatch(x.event_fields["id"], '9D"0') is not None)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
