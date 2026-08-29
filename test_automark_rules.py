"""Tests for MainWindow._match_automark_rules (compound automark rules).

Drives the real matcher unbound on a duck-typed window (no QApplication, no
settings file), mirroring the _on_log_line wiring. Pair tracker fed first,
then the 26-line match.

Run directly:  python test_automark_rules.py   (exit 0 = all pass)
"""
import os
import sys
import time
import types

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import main_window as mw
from umad_chains import CursedShriekPairs, StatusPairs

FAILS = []


def check(name, cond):
    print(("PASS  " if cond else "FAIL  ") + name)
    if not cond:
        FAILS.append(name)


P1, P2 = "10AAA111", "10BBB222"


class FakeWindow:
    """Just enough of MainWindow for _match_automark_rules: rules, cooldowns,
    pair tracker, fight tag, and a recording _mark_player."""

    _norm_hex = staticmethod(mw.MainWindow._norm_hex)

    def __init__(self, chains_on=False, fight="UMAD", rules=None, mark_ok=True):
        self._telesto_client = object()
        self._current_fight_tag = fight
        self._umad_chain_enabled = chains_on
        self._umad_gaze_enabled = False           # gaze pairing off for these cases
        self._umad_gaze = CursedShriekPairs()
        self._automark_rules = rules if rules is not None else [
            {"fight": "UMAD", "status": "644+BBC", "marker": "attack1",
             "scope": "party", "enabled": True},
            {"fight": "UMAD", "status": "644+BBD", "marker": "attack2",
             "scope": "party", "enabled": True},
        ]
        self._automark_pairs = StatusPairs(
            p for token in ([h for h, _ in mw._UMAD_AUTOMARK_PRESET]
                            + [str(r.get("status") or "") for r in self._automark_rules])
            for p in (mw._parse_compound(token) or ()))
        self._automark_cooldowns = {}
        self._automark_active = {}                # placed-by map for clear-on-loss
        self._automark_pending = []               # queued retries on a cold slot map
        self._automark_clear_on_loss = True
        self._mark_ok = mark_ok
        self.marks = []
        self.clears = []

    def _is_me_actor(self, tid, name=""):
        return False

    def _umad_name_of(self, actor_id):
        return ""

    def _mark_player(self, tid, marker, name, is_me=False):
        if not self._mark_ok:
            return False
        self.marks.append((tid, marker))
        return True

    def _clear_player(self, tid, name=""):
        self.clears.append(tid)
        return True

    def feed(self, ltype, eff, tgt, name="name"):
        """Mirror the _on_log_line wiring: tracker first, then the 26 match /
        the 30 unmark."""
        fields = [ltype, "ts", eff, name, "10", "src", "srcn", tgt, "tgtn"]
        if ltype in ("26", "30") and tgt.startswith("10"):
            n = self._norm_hex(eff)
            if n in self._automark_pairs.tracked:
                if ltype == "26":
                    self._automark_pairs.on_gain(n, tgt, time.monotonic())
                else:
                    self._automark_pairs.on_loss(n, tgt)
        if ltype == "26":
            mw.MainWindow._match_automark_rules(self, fields)
        elif self._automark_clear_on_loss:
            mw.MainWindow._match_automark_unmark(self, fields)


# ── Compound pair fires exactly once, either arrival order ──
w = FakeWindow()
w.feed("26", "BBC", P1)
check("in-Line alone marks nothing", w.marks == [])
w.feed("26", "644", P1)
check("644 completing the pair fires the 1st-in-line sign", w.marks == [(P1, "attack1")])
w.feed("26", "644", P2)
w.feed("26", "BBD", P2)
check("reverse order fires the 2nd-in-line sign", w.marks[-1] == (P2, "attack2"))
n = len(w.marks)
w.feed("26", "644", P1)        # refresh while the pair is still held
check("compound cooldown blocks a same-pair double fire", len(w.marks) == n)

# ── Losses break the pair. A fresh application re-fires ──
w.feed("30", "644", P1)
w.feed("30", "BBC", P1)
w._automark_cooldowns.clear()
w.feed("26", "BBC", P1)
w.feed("26", "644", P1)
check("re-fires after losses (next black hole)", w.marks[-1] == (P1, "attack1"))

# ── Guards ──
w = FakeWindow(chains_on=True)
w.feed("26", "BBC", P1)
w.feed("26", "644", P1)
check("suspended while the black-hole chains toggle is on", w.marks == [])

w = FakeWindow(fight="")
w.feed("26", "BBC", P1)
w.feed("26", "644", P1)
check("unknown fight (app started mid-instance) still fires", w.marks == [(P1, "attack1")])

w = FakeWindow(fight="fru")
w.feed("26", "BBC", P1)
w.feed("26", "644", P1)
check("a known different fight excludes UMAD rules", w.marks == [])

w = FakeWindow()
for r in w._automark_rules:
    r["marker"] = ""
w.feed("26", "BBC", P1)
w.feed("26", "644", P1)
check("unassigned compound rules are inert", w.marks == [])

# ── Non-preset compound rules are tracked from the loaded rules ──
w = FakeWindow(rules=[{"fight": "", "status": "8D1+8D2", "marker": "circle",
                       "scope": "party", "enabled": True}])
w.feed("26", "8D1", P2)
w.feed("26", "8D2", P2)
check("hand-added compound rule fires", w.marks == [(P2, "circle")])

# ── Exact-name rules containing '+' stay on the name-match path ──
w = FakeWindow(rules=[{"fight": "", "status": "Damage Up+", "marker": "cross",
                       "scope": "party", "enabled": True}])
w.feed("26", "FFF", P1, name="Damage Up+")
check("name rule with '+' fires via name match, not as a dead compound",
      w.marks == [(P1, "cross")])

# ── Clear-on-loss: a rule sign falls with the debuff that placed it ──
w = FakeWindow()
w.feed("26", "BBC", P1)
w.feed("26", "644", P1)
w.feed("26", "644", P2)
w.feed("26", "BBD", P2)
check("setup: both accretion carriers marked", len(w.marks) == 2)
w.feed("30", "154E", P1)     # Primordial Crust is not part of the pair
check("losing an unrelated debuff keeps the sign", w.clears == [])
w.feed("30", "644", P1)      # cleansed: Accretion falls off
check("losing Accretion clears that player's sign", w.clears == [P1])
w.feed("30", "BBC", P1)      # the other half of the pair falls right after
check("the rest of the burst does not re-clear", w.clears == [P1])
w.feed("30", "644", P2)
check("the second carrier clears on their own cleanse", w.clears == [P1, P2])

# ── Toggle off: losses leave signs alone ──
w = FakeWindow()
w._automark_clear_on_loss = False
w.feed("26", "BBC", P1)
w.feed("26", "644", P1)
w.feed("30", "644", P1)
check("clear-on-loss off leaves the sign up", w.clears == [])

# ── An engine mark (chains/gaze) invalidates the rule's placed-by entry ──
w = FakeWindow()
w.feed("26", "BBC", P1)
w.feed("26", "644", P1)
mw.MainWindow._dispatch_mark_actions(
    w, [("mark", P1, "attack3")], [])
w.feed("30", "644", P1)
check("a chain sign on the same player is not cleared by the rule's loss",
      w.clears == [])

# ── Lower-case feed ids: the rule path normalizes like the engines ──
P1L = P1.lower()
w = FakeWindow()
w.feed("26", "BBC", P1L)
w.feed("26", "644", P1L)
check("lower-case feed: the rule fires and stores the canonical id",
      w.marks == [(P1, "attack1")] and list(w._automark_active) == [P1])
mw.MainWindow._dispatch_mark_actions(
    w, [("mark", P1, "attack3")], [])
w.feed("30", "644", P1L)
check("lower-case feed: an engine sign is not cleared by the rule's loss",
      w.clears == [])

w = FakeWindow()
w.feed("26", "BBC", P1L)
w.feed("26", "644", P1L)
w.feed("30", "644", P1L)
check("lower-case feed: clear-on-loss still finds the placed sign",
      w.clears == [P1])

# ── A loss with nothing placed purges the queued retry for that debuff ──
w = FakeWindow(mark_ok=False)
w.feed("26", "BBC", P1)
w.feed("26", "644", P1)
check("cold slot map queues the party mark",
      w.marks == [] and len(w._automark_pending) == 1)
w.feed("30", "154E", P1)     # an unrelated debuff falling off keeps the retry
check("unrelated loss keeps the queued retry", len(w._automark_pending) == 1)
w.feed("30", "644", P1)      # the debuff that queued it is gone
check("losing the debuff purges its queued retry", w._automark_pending == [])
w._mark_ok = True
mw.MainWindow._retry_automark_pending(w)
check("the 10s retry can no longer place the stale mark", w.marks == [])

# ── Zone change tears down the placed-by bookkeeping too ──
class ZoneWin:
    """Just enough of MainWindow for _apply_zone: zone state, the automark
    bookkeeping, and stubbed UI and plugin link."""
    _apply_zone = mw.MainWindow._apply_zone

    def __init__(self):
        self._current_zone = "Old Zone"
        self._current_zone_id = 111
        self._zone_aliases = ["Old Zone"]
        self._automark_pairs = StatusPairs([])
        self._automark_pending = [(P1, "attack1", "n", 0.0, "644")]
        self._automark_active = {P1: "644"}
        self._actor_jobs = {1: 2}
        self._umad_actor_names = {1: "n"}
        self._umad_chain_enabled = False
        self._current_fight_tag = ""
        self._match_zone = ""
        self._mute_until_zone = False
        self._plugin_link = types.SimpleNamespace(send_clear=lambda: None)
        self._zone_lbl = types.SimpleNamespace(setText=lambda s: None)

    def _set_zone_aliases(self, zone, zone_id):
        self._zone_aliases = [zone]

    def _clear_status_timers(self):
        pass

    def _clear_seq_runners(self):
        pass

    def _umad_chain_reset(self, clear_marks=False):
        pass

    def _umad_gaze_reset(self, clear_marks=False):
        pass

    def _fight_tag_for_zone(self, zone):
        return ("", "")

    def _zone_banner_text(self):
        return ""

    def _append_zone_to_ability_log(self, zone):
        pass

    def _refresh_zone_column(self):
        pass

    def _load_timeline_for_zone(self, zone):
        pass

    def _refresh_telesto_party(self):
        pass


z = ZoneWin()
z._apply_zone("New Zone", 222)
check("zone change clears the placed-by bookkeeping", z._automark_active == {})
check("zone change still clears the queued rule marks", z._automark_pending == [])
check("zone change tracks the new zone id", z._current_zone_id == 222)

print()
if FAILS:
    print(f"{len(FAILS)} FAILED: {FAILS}")
    sys.exit(1)
print("all tests passed")
