"""Tests for the 30 s self-heal tick: zone/fight re-detect and trigger
hot-reload (MainWindow._poll_zone_and_triggers and friends).

(a) _redetect_zone_fight calls the timeline loader exactly once when the
    resolved fight changes and not while it stays the same.
(b) a rename-swapped triggers.json produces exactly one merged reload with
    the startup merge semantics intact (local full-copy override survives,
    slim toggle survives, a removed shipped trigger disappears, a custom
    local trigger survives).
(c) no crash on "" zone, missing trigger files, or a torn (half-saved) file.

Drives the real methods unbound on duck-typed windows (no QApplication, no
event loop), the way test_umad_gaze_wiring.py and test_zone_patterns.py do.

Run directly:  python test_zone_redetect.py   (exit 0 = all pass)
"""
import json
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import app_common
import main_window as mw
from trigger_engine import Trigger

FAILS = []


def check(name, cond):
    print(("PASS  " if cond else "FAIL  ") + name)
    if not cond:
        FAILS.append(name)


# ── isolate the trigger/timeline stores in a temp dir ─────────────────────
TMP = Path(tempfile.mkdtemp(prefix="nyaa_redetect_"))
SHIPPED = TMP / "triggers.json"
LOCAL = TMP / "triggers.local.json"
app_common.TRIGGERS_FILE = SHIPPED
app_common.TRIGGERS_LOCAL_FILE = LOCAL
app_common._REPO_TRIGGERS_FILE = TMP / "triggers.repo.json"
app_common._REPO_RETIRED_FILE = TMP / "retired.repo.json"
app_common._REPO_TRIGGERS_VERSION = TMP / "triggers.repo.version"
app_common.RETIRED_FILE = TMP / "retired.json"
app_common.TIMELINES_DIR = TMP / "timelines"
app_common._BUNDLE_TIMELINES_DIR = TMP / "bundle_timelines"
app_common.CACTBOT_TIMELINES_FILE = TMP / "cactbot_timelines.json"


def write_shipped(triggers):
    SHIPPED.write_text(json.dumps([t.to_dict() for t in triggers]), encoding="utf-8")


def swap_shipped(triggers):
    """Atomic replace, mirroring _atomic_write_json: sibling .tmp + rename."""
    tmp = SHIPPED.with_suffix(SHIPPED.suffix + ".tmp")
    tmp.write_text(json.dumps([t.to_dict() for t in triggers]), encoding="utf-8")
    os.replace(tmp, SHIPPED)


def write_local(records, deleted=()):
    LOCAL.write_text(json.dumps({
        "triggers": [r.to_dict() if isinstance(r, Trigger) else r for r in records],
        "deleted": sorted(deleted),
        "folders": [],
    }), encoding="utf-8")


class _TrigWin:
    """The trigger-store half of MainWindow, minus Qt: the real _load_triggers
    and hot-reload poll, with a counting stand-in for the table/tree refresh."""
    _load_triggers = mw.MainWindow._load_triggers
    _load_retired_ids = mw.MainWindow._load_retired_ids
    _trigger_files_stamp = mw.MainWindow._trigger_files_stamp
    _maybe_reload_triggers = mw.MainWindow._maybe_reload_triggers

    def __init__(self):
        self._triggers = []
        self._triggers_mtime = ()
        self.refreshes = 0

    def _refresh_table(self):
        self.refreshes += 1


class _ZoneWin:
    """The zone/fight half of MainWindow, minus Qt: real fight resolution and
    re-detect, with a recording stand-in for the timeline loader."""
    _fight_tag_for_zone = mw.MainWindow._fight_tag_for_zone
    _cactbot_zone_entry = mw.MainWindow._cactbot_zone_entry
    _timeline_fight_tag = mw.MainWindow._timeline_fight_tag
    _redetect_zone_fight = mw.MainWindow._redetect_zone_fight

    def __init__(self):
        self._triggers = []
        self._match_zone = ""
        self._timeline_fight = ""
        self._current_fight_tag = ""
        self._cactbot_mode = False
        self._current_zone_id = 0
        self.loads = []

    def _load_timeline_for_zone(self, zone):
        # Mimic the real loader's bookkeeping: record what was loaded.
        self.loads.append(zone)
        self._timeline_fight = self._timeline_fight_tag(zone)


class _PollWin(_TrigWin):
    """Both halves: the real 30 s tick end to end."""
    _fight_tag_for_zone = mw.MainWindow._fight_tag_for_zone
    _cactbot_zone_entry = mw.MainWindow._cactbot_zone_entry
    _timeline_fight_tag = mw.MainWindow._timeline_fight_tag
    _redetect_zone_fight = mw.MainWindow._redetect_zone_fight
    _poll_zone_and_triggers = mw.MainWindow._poll_zone_and_triggers

    def __init__(self):
        super().__init__()
        self._match_zone = ""
        self._timeline_fight = ""
        self._current_fight_tag = ""
        self._cactbot_mode = False
        self._current_zone_id = 0
        self.loads = []

    def _load_timeline_for_zone(self, zone):
        self.loads.append(zone)
        self._timeline_fight = self._timeline_fight_tag(zone)


class _TL:
    def __init__(self):
        self.entries = None
    def reset(self):
        pass
    def load(self, entries):
        self.entries = entries
    def clear(self):
        self.entries = None
    def upcoming(self):
        return []


class _PL:
    def __init__(self):
        self.pushes = 0
    def send_timeline(self, _up):
        self.pushes += 1


class _FightWin:
    """Real _load_timeline_for_zone with stub timeline engine + plugin link."""
    _fight_tag_for_zone = mw.MainWindow._fight_tag_for_zone
    _cactbot_zone_entry = mw.MainWindow._cactbot_zone_entry
    _timeline_fight_tag = mw.MainWindow._timeline_fight_tag
    _load_timeline_for_zone = mw.MainWindow._load_timeline_for_zone
    _push_timeline_to_plugin = mw.MainWindow._push_timeline_to_plugin

    def __init__(self):
        self._triggers = []
        self._timeline = _TL()
        self._plugin_link = _PL()
        self._cactbot_mode = False
        self._current_zone_id = 0
        self._timeline_fight = ""


# ── (a) re-detect reloads once per change, never while unchanged ───────────
z = _ZoneWin()
z._triggers = [Trigger(fight="F1", zone_regex="Zone One")]
z._match_zone = "Zone One"

z._redetect_zone_fight()
check("first detect loads the timeline once", z.loads == ["Zone One"])
check("loaded fight recorded", z._timeline_fight == "F1")
check("cached fight tag follows", z._current_fight_tag == "F1")

z._redetect_zone_fight()
z._redetect_zone_fight()
check("unchanged fight is a strict no-op", z.loads == ["Zone One"])

# Resolution moves to another fight (e.g. a hot-reloaded zone_regex edit).
z._triggers = [Trigger(fight="F1", zone_regex="Somewhere Else"),
               Trigger(fight="F2", zone_regex="Zone One")]
z._redetect_zone_fight()
check("changed fight reloads exactly once", z.loads == ["Zone One", "Zone One"])
check("new fight recorded", z._timeline_fight == "F2" and z._current_fight_tag == "F2")
z._redetect_zone_fight()
check("steady again after the reload", len(z.loads) == 2)

# Resolution lost entirely (no trigger matches the zone). Clears once.
z._triggers = [Trigger(fight="F1", zone_regex="Somewhere Else")]
z._redetect_zone_fight()
check("lost resolution clears once", len(z.loads) == 3 and z._timeline_fight == "")
z._redetect_zone_fight()
check("staying unresolved is a no-op", len(z.loads) == 3)

# ── (a2) the real loader records _timeline_fight and pushes once ───────────
fw = _FightWin()
fw._triggers = [Trigger(fight="ZZTestFight", zone_regex="ZZ Test Zone")]
app_common.TIMELINES_DIR.mkdir(parents=True, exist_ok=True)
(app_common.TIMELINES_DIR / "ZZTestFight.txt").write_text('10.0 "Beeg"\n', encoding="utf-8")
fw._load_timeline_for_zone("ZZ Test Zone")
check("real loader records the resolved fight", fw._timeline_fight == "ZZTestFight")
check("real loader parsed the timeline file", bool(fw._timeline.entries))
check("real loader pushes the schedule once", fw._plugin_link.pushes == 1)

fw._load_timeline_for_zone("Unrecognised Place")
check("unrecognised zone records empty fight", fw._timeline_fight == "")
check("clear still pushes exactly once", fw._plugin_link.pushes == 2)

fw._triggers = [Trigger(fight="ZZTestFight", zone_regex="ZZ Test Zone")]
fw._load_timeline_for_zone("")
check('"" zone records empty fight', fw._timeline_fight == "")

# ── (a3) the zone-id cactbot index is the primary timeline source ──────────
app_common.CACTBOT_TIMELINES_FILE.write_text(json.dumps({
    "4242": {"tag": "cb_index_fight", "txt_path": "06-ew/raid/p9s.txt"},
    "4243": {"tag": "cb_nocache", "txt_path": "06-ew/raid/p10s.txt"},
    "4244": {"tag": "cb_localfirst", "txt_path": "06-ew/raid/p11s.txt"},
    "4245": {"tag": "cb_bundle_fight", "txt_path": "07-dt/dungeon/x.txt"},
    "4246": {"tag": "cb_cachewins", "txt_path": "07-dt/dungeon/y.txt"},
}), encoding="utf-8")
app_common._cactbot_tl_cache = None


class _IndexWin(_FightWin):
    """_FightWin with the Cactbot switch ON and a recording fetch stub (no
    network). The real loader/redetect against the generated index."""
    _redetect_zone_fight = mw.MainWindow._redetect_zone_fight

    def __init__(self):
        super().__init__()
        self._cactbot_mode = True
        self._match_zone = ""
        self._current_fight_tag = ""
        self.fetches = []

    def _fetch_cactbot_timeline(self, tag, rel):
        self.fetches.append((tag, rel))


# A cached index hit needs no local trigger file at all (the P1S case).
iw = _IndexWin()
iw._current_zone_id = 4242
(app_common.TIMELINES_DIR / "cb_index_fight.cactbot.cache.txt").write_text(
    '12.0 "Index Beeg"\n', encoding="utf-8")
iw._load_timeline_for_zone("Zone With No Local Triggers")
check("index hit loads the cached cactbot timeline", bool(iw._timeline.entries))
check("index hit records the index tag", iw._timeline_fight == "cb_index_fight")
check("index hit pushes once", iw._plugin_link.pushes == 1)
check("fresh cache kicks no fetch", iw.fetches == [])

# Cache missing: fetch is kicked with the index path, timeline clears for now.
iw2 = _IndexWin()
iw2._current_zone_id = 4243
iw2._load_timeline_for_zone("Zone With No Local Triggers")
check("missing cache kicks the index fetch",
      iw2.fetches == [("cb_nocache", "06-ew/raid/p10s.txt")])
check("missing cache clears meanwhile", iw2._timeline.entries is None)
check("missing cache still records the index tag",
      iw2._timeline_fight == "cb_nocache")

# Cache missing but a local custom <Fight>.txt exists (UMAD pre-upstream):
# the local file serves while the download runs.
iw3 = _IndexWin()
iw3._current_zone_id = 4244
iw3._triggers = [Trigger(fight="LocalCustom", zone_regex="Custom Zone")]
(app_common.TIMELINES_DIR / "LocalCustom.txt").write_text('7.0 "Local Beeg"\n', encoding="utf-8")
iw3._load_timeline_for_zone("Custom Zone")
check("local custom file serves while the fetch runs", bool(iw3._timeline.entries))
check("local custom file does not block the fetch",
      iw3.fetches == [("cb_localfirst", "06-ew/raid/p11s.txt")])
check("recorded tag is the index tag", iw3._timeline_fight == "cb_localfirst")

# A late-arriving zone id re-resolves through the redetect tick, and the
# cached fight tag never takes the index tag (UMAD rules key on the local one).
iw4 = _IndexWin()
iw4._match_zone = "Zone With No Local Triggers"
iw4._redetect_zone_fight()
check("unmapped id is a no-op",
      iw4._plugin_link.pushes == 0 and iw4._timeline_fight == "")
iw4._current_zone_id = 4242
iw4._redetect_zone_fight()
check("late id loads the index timeline",
      iw4._timeline_fight == "cb_index_fight" and iw4._plugin_link.pushes == 1)
check("cached fight tag stays the local one", iw4._current_fight_tag == "")
iw4._redetect_zone_fight()
check("steady state after the late id", iw4._plugin_link.pushes == 1)

# Cactbot off: the index is ignored and an unmapped zone falls back to the
# name-regex resolution exactly as before.
iw5 = _IndexWin()
iw5._cactbot_mode = False
iw5._current_zone_id = 4242
iw5._load_timeline_for_zone("Zone With No Local Triggers")
check("cactbot off ignores the index",
      iw5._timeline_fight == "" and iw5.fetches == [])

# ── (a5) the bundled copy is the read-only fallback, the cache wins ────────
BUNDLE = app_common._BUNDLE_TIMELINES_DIR
BUNDLE.mkdir(parents=True, exist_ok=True)

# No cache but a bundled copy: the shipped file serves, no fetch while fresh.
iw6 = _IndexWin()
iw6._current_zone_id = 4245
(BUNDLE / "cb_bundle_fight.cactbot.txt").write_text('9.0 "Bundle Beeg"\n', encoding="utf-8")
iw6._load_timeline_for_zone("Zone With No Local Triggers")
check("bundled timeline serves with no cache", bool(iw6._timeline.entries))
check("bundled timeline records the index tag", iw6._timeline_fight == "cb_bundle_fight")
check("fresh bundled copy kicks no fetch", iw6.fetches == [])

# A download must never be shadowed by the bundled copy it refreshed.
iw7 = _IndexWin()
iw7._current_zone_id = 4246
(BUNDLE / "cb_cachewins.cactbot.txt").write_bytes(b"\xff\xfe binary junk")
(app_common.TIMELINES_DIR / "cb_cachewins.cactbot.cache.txt").write_text(
    '8.0 "Cache Beeg"\n', encoding="utf-8")
iw7._load_timeline_for_zone("Zone With No Local Triggers")
check("cache wins over the bundled copy", bool(iw7._timeline.entries))
check("cache win records the index tag", iw7._timeline_fight == "cb_cachewins")

# A local fight whose file only exists in the bundle: the shipped copy
# serves, the UMAD.txt on a frozen build case.
fw4 = _FightWin()
fw4._triggers = [Trigger(fight="ZZBundleLocal", zone_regex="ZZ Bundle Local Zone")]
(BUNDLE / "ZZBundleLocal.txt").write_text('6.0 "Bundle Local Beeg"\n', encoding="utf-8")
fw4._load_timeline_for_zone("ZZ Bundle Local Zone")
check("bundled local timeline serves", bool(fw4._timeline.entries))
check("bundled local records the fight", fw4._timeline_fight == "ZZBundleLocal")

# ── (a4) a separator-bearing fight tag loads empty once, then stays steady ─
# Imported triggers carry whatever the file said. The loader blanks such a
# tag before any path is built, so the re-detect comparison must blank it
# the same way, or the tick would reload, reset and re-push every 30 s.
class _SepWin(_FightWin):
    _redetect_zone_fight = mw.MainWindow._redetect_zone_fight

    def __init__(self):
        super().__init__()
        self._match_zone = ""
        self._current_fight_tag = ""


sw = _SepWin()
sw._triggers = [Trigger(fight="A/B../C", zone_regex="Sep Zone")]
sw._match_zone = "Sep Zone"
sw._load_timeline_for_zone("Sep Zone")
check("separator tag loads empty, no path walk", sw._timeline_fight == "")
check("separator tag pushes the clear once", sw._plugin_link.pushes == 1)
sw._redetect_zone_fight()
sw._redetect_zone_fight()
check("separator tag stays steady, no 30 s reload loop",
      sw._plugin_link.pushes == 1 and sw._timeline_fight == "")

# ── (a5) an unreadable timeline clears, stays unstamped, and is logged ─────
drops = []
real_log_drop = app_common.log_drop
app_common.log_drop = lambda site, detail, *a, **k: drops.append((site, detail))
try:
    fw2 = _FightWin()
    fw2._triggers = [Trigger(fight="ZZBroken", zone_regex="ZZ Broken Zone")]
    (app_common.TIMELINES_DIR / "ZZBroken.txt").write_bytes(b"\xff\xfe binary junk")
    fw2._load_timeline_for_zone("ZZ Broken Zone")
    check("unreadable timeline clears and records empty",
          fw2._timeline_fight == "" and fw2._timeline.entries is None)
    check("unreadable timeline leaves a drop-log trace",
          any(site == "timeline" for site, _ in drops))
finally:
    app_common.log_drop = real_log_drop

# ── (b) rename-swap -> exactly one merged reload, merge semantics intact ───
ship_a = Trigger(id="aaa", fight="F1", zone_regex="Zone One", tts_text="shipped A")
ship_b = Trigger(id="bbb", fight="F1", zone_regex="Zone One", tts_text="shipped B")
ship_d = Trigger(id="ddd", fight="F1", zone_regex="Zone One", tts_text="shipped D")
local_a = Trigger.from_dict(ship_a.to_dict())
local_a.tts_text = "user edit A"          # full-copy override: content diverged
local_c = Trigger(id="ccc", tts_text="custom C")

write_shipped([ship_a, ship_b, ship_d])
write_local([local_a, local_c, {"id": "ddd", "enabled": False}])   # slim toggle

w = _TrigWin()
w._load_triggers()          # startup path. Re-baselines the mtime snapshot
check("initial load merges shipped + local",
      [t.id for t in w._triggers] == ["aaa", "bbb", "ddd", "ccc"])
check("initial refresh ran once", w.refreshes == 1)

w._maybe_reload_triggers()
check("no on-disk change -> no reload", w.refreshes == 1)

# The fix lands while the app runs: B is withdrawn upstream.
swap_shipped([ship_a, ship_d])
w._maybe_reload_triggers()
check("rename-swap triggers exactly one reload", w.refreshes == 2)
ids = [t.id for t in w._triggers]
check("removed shipped trigger disappears", "bbb" not in ids)
check("local full-copy override survives",
      next(t for t in w._triggers if t.id == "aaa").tts_text == "user edit A")
check("slim toggle override survives",
      next(t for t in w._triggers if t.id == "ddd").enabled is False)
check("custom local trigger survives", "ccc" in ids)

w._maybe_reload_triggers()
check("reload re-baselines: a second poll does nothing", w.refreshes == 2)

# ── (b2) the full tick: a zone_regex fix self-heals without a zone change ──
p = _PollWin()
p._match_zone = "Zone One"
ship_a2 = Trigger.from_dict(ship_a.to_dict())
ship_a2.zone_regex = "Somewhere Else"     # shipped with a dead pattern
write_shipped([ship_a2])
LOCAL.unlink(missing_ok=True)
p._load_triggers()
p._poll_zone_and_triggers()
check("unmatched zone loads nothing", p.loads == [] and p._timeline_fight == "")

ship_a2.zone_regex = "Zone One"           # the fix lands on disk
swap_shipped([ship_a2])
p._poll_zone_and_triggers()
check("fix hot-reloads triggers", p.refreshes == 2)
check("and re-detects without a zone change", p.loads == ["Zone One"])
check("fight tag self-healed", p._current_fight_tag == "F1")
p._poll_zone_and_triggers()
check("steady state: no reload, no re-detect", p.refreshes == 2 and len(p.loads) == 1)

# ── (c) edge cases: "" zone, missing files, torn write ─────────────────────
z2 = _ZoneWin()
z2._redetect_zone_fight()                 # "" zone, nothing loaded
check('"" zone with nothing loaded is a no-op', z2.loads == [])
z2._timeline_fight = "F1"                 # stale entry, zone now unknown
z2._redetect_zone_fight()
check('"" zone clears a stale timeline once', z2.loads == [""] and z2._timeline_fight == "")

SHIPPED.unlink(missing_ok=True)
LOCAL.unlink(missing_ok=True)
check("stamp tolerates all files missing",
      _TrigWin()._trigger_files_stamp() == (None, None, None))

w2 = _TrigWin()
write_shipped([ship_a, ship_d])
write_local([local_a, local_c])
w2._load_triggers()
LOCAL.unlink()                            # local file momentarily gone
w2._maybe_reload_triggers()
check("missing local file reloads without crash", w2.refreshes == 2)
check("merge degrades to shipped-only",
      [t.id for t in w2._triggers] == ["aaa", "ddd"])

SHIPPED.unlink()
w2._maybe_reload_triggers()
check("all files missing: no crash", w2.refreshes == 3)
check("merged set is just empty", w2._triggers == [])

# Torn write (non-atomic editor mid-save): must not touch the live set.
write_shipped([ship_a, ship_d])
w2._maybe_reload_triggers()               # baseline again after valid write
check("valid file reloads after the missing phase", w2.refreshes == 4)
before = [t.id for t in w2._triggers]
SHIPPED.write_text('{"not json', encoding="utf-8")
w2._maybe_reload_triggers()
check("torn write skips the reload", w2.refreshes == 4)
check("live set untouched by the torn write",
      [t.id for t in w2._triggers] == before)
# Recover with content of a DIFFERENT size than the pre-torn baseline. The
# hot-reload stamp is (mtime_ns, size), and on filesystems with coarse mtime
# granularity the rewrite can share the baseline's timestamp; identical bytes
# would then produce the baseline stamp and the poll would (correctly) see no
# change. CI runners have hit exactly that.
swap_shipped([ship_a])
w2._maybe_reload_triggers()
check("recovers once the file is whole again", w2.refreshes == 5)

# ── (d) the WS zone event lets _apply_zone own the id ──────────────────────
class _WSZoneWin:
    _on_ws_zone_changed = mw.MainWindow._on_ws_zone_changed

    def __init__(self):
        self._current_zone_id = 111
        self.applied = []

    def _apply_zone(self, zone, zone_id=0):
        self.applied.append((zone, zone_id))


zw = _WSZoneWin()
zw._on_ws_zone_changed(222, "Zone One")
check("named WS zone event delegates without pre-assigning the id",
      zw.applied == [("Zone One", 222)] and zw._current_zone_id == 111)
zw._on_ws_zone_changed(333, "")
check("nameless WS zone event still retains the id for the sidecar replay",
      zw._current_zone_id == 333 and len(zw.applied) == 1)

# ── (a6) a UTF-8 BOM does not eat the first timeline line ───────────────────
# A BOM survives strip under plain utf-8, and the parser's anchored entry
# regex then misses the first line. The loader reads with utf-8-sig instead.
fw3 = _FightWin()
fw3._triggers = [Trigger(fight="ZZBom", zone_regex="ZZ Bom Zone")]
(app_common.TIMELINES_DIR / "ZZBom.txt").write_bytes(b'\xef\xbb\xbf10.0 "Bom Beeg"\n')
fw3._load_timeline_for_zone("ZZ Bom Zone")
check("BOM timeline still parses its first line", bool(fw3._timeline.entries))
check("BOM timeline records the fight and pushes once",
      fw3._timeline_fight == "ZZBom" and fw3._plugin_link.pushes == 1)

print()
if FAILS:
    print(f"{len(FAILS)} FAILED: {', '.join(FAILS)}")
    sys.exit(1)
print("all passed")
