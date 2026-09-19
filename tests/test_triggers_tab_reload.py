"""Trigger reloads preserve unchanged object identity, pending timers and cooldowns."""
import json
import os
import sys
import tempfile
import time
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt6.QtCore import QObject
from PyQt6.QtWidgets import QApplication

from nyaatriggers import app_common as ac
from nyaatriggers.status_timer import StatusTimerRunner
from nyaatriggers.sequential import SequentialRunner
from nyaatriggers.ui.triggers_tab import TriggersTabMixin
from nyaatriggers.ui.instance_tab import InstanceTabMixin
from nyaatriggers.ui.timeline_tab import TimelineTabMixin

FAILS = []


def check(name, cond):
    print(("PASS  " if cond else "FAIL  ") + name)
    if not cond:
        FAILS.append(name)


_app = QApplication.instance() or QApplication(sys.argv)


class Win(QObject, TriggersTabMixin, InstanceTabMixin, TimelineTabMixin):
    """Bare host for the trigger mixins, only the state their paths touch."""

    def __init__(self):
        super().__init__()
        self.fired = []
        self._local_enabled = True
        self._settings = {}
        self._triggers = []
        self._status_timers = []
        self._seq_runners = []

    def _refresh_table(self):
        pass

    def _fire(self, trigger, captured):
        self.fired.append(trigger.id)


def arm_status(win, trigger, effect_id):
    runner = StatusTimerRunner(trigger, {}, effect_id, "10FF0001", "40000001",
                               0, win._on_status_timer, win)
    win._status_timers.append(runner)
    return runner


WARNING = {"id": "warning", "name": "warning", "log_type": "26",
           "ability_id": "ABC", "expiry_warn_s": 5, "cooldown_s": 120,
           "enabled": True}
SEQ = {"id": "seq", "name": "seq", "log_type": "20", "ability_id": "8F00",
       "sequence": [{"log_type": "21", "ability_id": "8F01"}], "enabled": True}
UNRELATED = {"id": "unrelated", "name": "unrelated", "enabled": True}
# 21 is type|ts|sourceId|source|id|ability|targetId|target.
SEQ_STEP_LINE = ["21", "ts", "40001234", "Boss", "8F01", "Some Ability",
                 "10FF0001", "Target"]

with tempfile.TemporaryDirectory() as td, ExitStack() as stack:
    clock = stack.enter_context(patch("time.monotonic", return_value=0.0))
    td = Path(td)
    for key in ("TRIGGERS_FILE", "TRIGGERS_LOCAL_FILE", "RETIRED_FILE",
                "_REPO_TRIGGERS_FILE", "_REPO_RETIRED_FILE",
                "_REPO_TRIGGERS_VERSION"):
        stack.enter_context(patch.object(ac, key, td / key))
    ac.TRIGGERS_FILE.write_text("[]", encoding="utf-8")
    ac.TRIGGERS_LOCAL_FILE.write_text(
        json.dumps({"triggers": [WARNING, SEQ, UNRELATED]}), encoding="utf-8")

    w = Win()
    w._load_triggers()
    warning, seq, unrelated = w._triggers

    # the program's own save is not an external change
    warn_runner = arm_status(w, warning, "ABC")
    seq_runner = SequentialRunner(seq, {}, w._on_seq_complete,
                                  w._on_seq_expire, w)
    w._seq_runners.append(seq_runner)
    warning._last_fired["old-source"] = time.monotonic()

    unrelated.enabled = False
    w._save_triggers()
    check("own save re-baselines the watcher stamp",
          w._trigger_files_stamp() == w._triggers_mtime)
    w._maybe_reload_triggers()
    check("own save does not swap trigger objects",
          w._triggers[0] is warning and w._triggers[1] is seq)

    warn_runner._fire()
    check("armed status warning survives an unrelated save",
          w.fired == ["warning"])
    check("cooldown history survives an unrelated save",
          "old-source" in w._triggers[0]._last_fired)
    check("armed sequence survives an unrelated save",
          seq_runner.try_advance(SEQ_STEP_LINE) and w.fired == ["warning", "seq"])

    # an external edit reloads only what it touched
    warn_runner2 = arm_status(w, warning, "ABD")
    stale_runner = arm_status(w, unrelated, "ABE")

    data = json.loads(ac.TRIGGERS_LOCAL_FILE.read_text(encoding="utf-8"))
    next(r for r in data["triggers"] if r["id"] == "unrelated")["name"] = "renamed"
    ac.TRIGGERS_LOCAL_FILE.write_text(json.dumps(data), encoding="utf-8")
    w._maybe_reload_triggers()

    check("external edit keeps an unchanged trigger's object",
          w._triggers[0] is warning)
    check("external edit keeps the cooldown history with it",
          "old-source" in w._triggers[0]._last_fired)
    check("external edit swaps the changed trigger's object",
          w._triggers[2] is not unrelated
          and w._triggers[2].name == "renamed")

    warn_runner2._fire()
    check("warning armed before an external reload still fires",
          w.fired == ["warning", "seq", "warning"])
    stale_runner._fire()
    check("work bound to the changed trigger is invalidated",
          w.fired == ["warning", "seq", "warning"])

    # editing the armed trigger itself invalidates its pending work
    warn_runner3 = arm_status(w, warning, "ABF")
    data = json.loads(ac.TRIGGERS_LOCAL_FILE.read_text(encoding="utf-8"))
    next(r for r in data["triggers"] if r["id"] == "warning")["name"] = "edited"
    ac.TRIGGERS_LOCAL_FILE.write_text(json.dumps(data), encoding="utf-8")
    w._maybe_reload_triggers()

    check("editing the armed trigger swaps its object",
          w._triggers[0] is not warning and w._triggers[0].name == "edited")
    warn_runner3._fire()
    check("the edited trigger's pending warning is invalidated",
          w.fired == ["warning", "seq", "warning"])

    for path in (ac.TRIGGERS_FILE, ac._REPO_TRIGGERS_FILE):
        path.write_text(json.dumps([
            {"id": "official", "name": "changed outside the program"}]),
            encoding="utf-8")
        local = next(t for t in w._triggers if t.id == "warning")
        local.name = "local edit while a reload is pending"
        w._save_triggers()
        check("local save keeps other files pending",
              w._triggers_mtime != w._trigger_files_stamp())
        w._maybe_reload_triggers()
        check("poll loads the pending external change",
              any(t.id == "official" and t.name == "changed outside the program"
                  for t in w._triggers))
        check("pending external reload preserves the saved local object",
              next(t for t in w._triggers if t.id == "warning") is local)

    w.fired.clear()
    arm_status(w, local, "AC0")._fire()
    check("a new effect can warn at clock zero", w.fired == ["warning"])
    clock.return_value = 119.0
    arm_status(w, local, "AC0")._fire()
    check("a warning at clock zero still starts its cooldown",
          w.fired == ["warning"])
    clock.return_value = 120.0
    arm_status(w, local, "AC0")._fire()
    check("the same effect warns again when its cooldown ends",
          w.fired == ["warning", "warning"])

print()
if FAILS:
    print(f"{len(FAILS)} FAILED: {', '.join(FAILS)}")
    sys.exit(1)
print("all tests passed")
