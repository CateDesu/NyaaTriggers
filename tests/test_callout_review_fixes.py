"""Callout timing, duty filters and saved choices after duplicate consolidation."""

from contextlib import ExitStack
from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QObject
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication

from nyaatriggers import app_common as ac
from nyaatriggers.main_window import MainWindow
from nyaatriggers.sequential import SequentialRunner
from nyaatriggers.trigger_dialog import TriggerDialog
from nyaatriggers.trigger_engine import Trigger
from nyaatriggers.trigger_profiles import apply_choices, merge_local_choices, preserve_default
from nyaatriggers.ui.instance_tab import InstanceTabMixin
from nyaatriggers.ui.dps_tab import DpsTabMixin
from nyaatriggers.ui.timeline_tab import TimelineTabMixin
from nyaatriggers.ui.triggers_tab import TriggersTabMixin
from tests.test_overlay_retention import OverlayHost

APP = QApplication.instance() or QApplication([])
ROOT = Path(__file__).resolve().parents[1]
ROWS = json.loads((ROOT / "assets/triggers.json").read_text())
RETIREMENTS = json.loads((ROOT / "assets/retired.json").read_text())["retired"]
REPLACEMENTS = {r["id"]: r["replaced_by"] for r in RETIREMENTS if "replaced_by" in r}
TEA_ID = "3ec40a20-4a56-5431-b648-3ab2dec80091"


class Host(QObject, InstanceTabMixin, TimelineTabMixin, TriggersTabMixin, DpsTabMixin):
    emit = OverlayHost.emit
    wipe = OverlayHost.wipe
    prepare_zone = OverlayHost.prepare_zone
    raw_zone = OverlayHost.raw_zone

    def __init__(self, now, rows=()):
        super().__init__()
        OverlayHost.__init__(self, now)
        del self._clear_seq_runners
        del self._clear_status_timers
        self._local_enabled = True
        self._local_ids = set()
        self._status_timers = []
        self._zone_aliases = ("The Epic of Alexander (Ultimate)",)
        self._me_name = "Player"
        self._triggers = [Trigger.from_dict({**r, "enabled": True}) for r in rows]
        self._save_settings = lambda: True
        self._save_triggers = lambda: True
        self._localized_callout = lambda t: t.tts_text
        self._reading_for = lambda text: text
        self._claim_callout = lambda text: None
        self._emit_alert = lambda *args: None
        self._fire = MainWindow._fire.__get__(self)

    def dispatch(self, ident, kind="21", source="40000001"):
        fields = [kind, "ts", source, "Boss", ident, "Ability", "10000001", "Player"] + ["0"] * 16
        self._dispatch_log_line(fields, "|".join(fields))


class CalloutReviewFixTests(unittest.TestCase):
    def setUp(self):
        self.now = [1000.0]
        self.clock = patch("time.monotonic", side_effect=lambda: self.now[0])
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.speech = patch("nyaatriggers.main_window.speak").start()
        self.addCleanup(patch.stopall)
        tea = next(r for r in ROWS if r["id"] == TEA_ID)
        self.host = Host(self.now, [tea])
        self.addCleanup(self.host._clear_seq_runners)

    def test_tea_waits_six_seconds_and_duplicate_packets_do_not_postpone_it(self):
        self.host.dispatch("4879")
        runner, = self.host._seq_runners
        self.assertFalse(self.speech.called)
        self.now[0] += 5.1
        self.host.dispatch("4879", "22")
        self.assertEqual(self.host._seq_runners, [runner])
        runner._expire()
        self.assertFalse(self.speech.called)
        self.now[0] = 1006.0
        runner._expire()
        self.speech.assert_called_once_with("TANK LB!!", speed=1.0, reading="TANK LB!!")
        self.assertFalse(self.host._seq_runners)
        runner._expire()
        self.assertEqual(self.speech.call_count, 1)

    def test_cancelled_delays_cannot_return_after_wipe_zone_or_mode_change(self):
        for boundary in ("wipe", "zone", "mode", "fight", "trigger"):
            with self.subTest(boundary=boundary):
                self.host._triggers[0]._last_fired.clear()
                self.host._zone_aliases = ("The Epic of Alexander (Ultimate)",)
                self.host.dispatch("4879")
                runner, = self.host._seq_runners
                if boundary == "wipe":
                    self.host.wipe()
                elif boundary == "zone":
                    self.host.prepare_zone()
                    self.host.raw_zone()
                elif boundary == "mode":
                    self.host._set_local_enabled(False)
                    self.host._set_local_enabled(True)
                elif boundary == "fight":
                    self.host._set_fight_local("TEA", False)
                    self.host._set_fight_local("TEA", True)
                else:
                    self.host._set_trigger_enabled(self.host._triggers[0], False)
                    self.host._set_trigger_enabled(self.host._triggers[0], True)
                self.assertFalse(self.host._seq_runners)
                self.now[0] += 10
                runner._expire()
                self.assertFalse(self.speech.called)

    def test_delays_do_not_fire_for_disabled_deleted_or_replaced_triggers(self):
        original = self.host._triggers[0]
        for action in ("disabled", "deleted", "replaced"):
            with self.subTest(action=action):
                original.enabled = True
                original._last_fired.clear()
                self.host._triggers = [original]
                self.host.dispatch("4879")
                runner, = self.host._seq_runners
                if action == "disabled":
                    original.enabled = False
                elif action == "deleted":
                    self.host._triggers.clear()
                else:
                    self.host._triggers = [Trigger.from_dict(original.to_dict())]
                self.now[0] += 10
                runner._expire()
                self.assertFalse(self.speech.called)

    def test_delay_runs_after_the_last_followup_and_keeps_its_captures(self):
        t = Trigger(delay_s=6, sequence=[{"log_type": "20", "ability_id": "ABCD"}])
        done, expired = [], []
        runner = SequentialRunner(t, {}, lambda r, c: done.append(c), expired.append)
        self.addCleanup(runner.cancel)
        runner.try_advance(["20", "ts", "40000001", "Other Boss", "ABCD", "Cast", "10000002", "Other Player"])
        self.assertFalse(done)
        self.now[0] += 6
        runner._expire()
        self.assertEqual(done, [{"source": "Other Boss", "target": "Other Player"}])
        self.assertFalse(expired)

    def test_real_qt_timer_obeys_the_delay(self):
        self.clock.stop()
        done = []
        start = time.monotonic()
        runner = SequentialRunner(Trigger(delay_s=0.06), {},
                                  lambda r, c: done.append(time.monotonic()), lambda r: None)
        self.addCleanup(runner.cancel)
        QTest.qWait(15)
        self.assertFalse(done)
        QTest.qWait(100)
        self.assertEqual(len(done), 1)
        self.assertGreaterEqual(done[0] - start, 0.06)

    def test_timer_callback_failure_is_contained(self):
        def broken_callback(runner, captured):
            raise RuntimeError("speech unavailable")

        runner = SequentialRunner(Trigger(delay_s=6), {}, broken_callback, lambda r: None)
        self.addCleanup(runner.cancel)
        self.now[0] += 6
        with patch("nyaatriggers.sequential.log_drop") as log:
            runner._expire()
            log.assert_called_once()
        self.assertFalse(runner._timer.isActive())

    def test_different_sources_keep_independent_delays(self):
        trigger = self.host._triggers[0]
        trigger.cooldown_scope = "source"
        self.host.dispatch("4879", source="40000001")
        self.host.dispatch("4879", source="40000002")
        self.assertEqual(len(self.host._seq_runners), 2)
        self.now[0] += 6
        for runner in list(self.host._seq_runners):
            runner._expire()
        self.assertEqual(self.speech.call_count, 2)

    def test_ignored_pending_events_do_not_extend_the_cooldown(self):
        self.host.dispatch("4879")
        runner, = self.host._seq_runners
        self.now[0] += 5.1
        self.host.dispatch("4879", "22")
        self.now[0] = 1006.0
        runner._expire()
        self.assertEqual(self.speech.call_count, 1)
        self.now[0] += 0.1
        self.host.dispatch("4879")
        self.assertEqual(len(self.host._seq_runners), 1)
        self.now[0] += 6
        self.host._seq_runners[0]._expire()
        self.assertEqual(self.speech.call_count, 2)

    def test_delay_survives_serialization_and_editor_save(self):
        trigger = self.host._triggers[0]
        restored = Trigger.from_dict(trigger.to_dict())
        dlg = TriggerDialog(restored)
        self.addCleanup(dlg.deleteLater)
        self.assertEqual(dlg.get_trigger(restored.id).delay_s, 6)
        for invalid in (None, "bad", float("nan"), float("inf"), -6):
            self.assertEqual(Trigger.from_dict({"delay_s": invalid}).delay_s, 0)
        status = Trigger.from_dict({"log_type": "26", "expiry_warn_s": 5, "delay_s": 6})
        self.assertEqual(status.delay_s, 0)

    def test_shiva_knockback_only_matches_the_savage_duty(self):
        row = next(r for r in ROWS if r["id"] == "f4521cc5-8d54-48f2-9116-c2e05da7bb65")
        self.assertEqual(row["fight"], "E8S")
        self.host._triggers = [Trigger.from_dict({**row, "enabled": True})]
        self.host._zone_aliases = ("Eden's Verse: Refulgence",)
        self.host.dispatch("4D77", "20")
        self.assertFalse(self.speech.called)
        self.host._zone_aliases = ("Eden's Verse: Refulgence (Savage)",)
        self.host.dispatch("4D77", "20")
        self.speech.assert_called_once_with("Prepare for mirror knockbacks", speed=1.0,
                                            reading="Prepare for mirror knockbacks")

    def test_consolidated_mechanics_each_submit_one_callout(self):
        for zone, ident in (("Aglaia", "70B4"), ("Aglaia", "709F"), ("Aglaia", "711D"),
                            ("Everkeep (Extreme)", "9374"), ("Everkeep (Extreme)", "9398"),
                            ("Everkeep (Extreme)", "9397"), ("Everkeep (Extreme)", "939C"),
                            ("Anabaseios: The Ninth Circle", "8116"),
                            ("Anabaseios: The Ninth Circle", "8136"), ("Alphascape V3.0 (Savage)", "326D")):
            with self.subTest(zone=zone, ability=ident):
                self.speech.reset_mock()
                self.host._triggers = [Trigger.from_dict({**r, "enabled": True}) for r in ROWS]
                self.host._zone_aliases = (zone,)
                self.host.dispatch(ident, "20")
                self.assertEqual(self.speech.call_count, 1)


class MigrationTests(unittest.TestCase):
    def test_existing_default_migrates_before_new_choices_are_preserved(self):
        retired, targets = next(iter(REPLACEMENTS.items()))
        previous = {"name": "Default", "local": {
            retired: {"enabled": False, "text": "Default wording"}}, "engines": {}}
        original = deepcopy(previous)
        target = {"name": "Next profile", "local": {
            targets[0]: {"enabled": True, "text": "Next wording"}}, "engines": {}}
        trigger = Trigger(id=targets[0], enabled=True, tts_text="Current named wording")
        window = SimpleNamespace(
            _triggers=[trigger], _local_ids=set(), _settings={},
            _trigger_replacements=REPLACEMENTS, _engine_seen={},
            _engine_disabled={s: set() for s in ("cactbot", "triggevent", "triggernometry")},
            _engine_inventory=[])
        default = preserve_default(window, previous, target)
        apply_choices(window, target)
        apply_choices(window, default)
        self.assertFalse(trigger.enabled)
        self.assertEqual(trigger.tts_text, "Default wording")
        self.assertEqual(previous, original)

    def test_retired_toggle_and_full_copies_migrate_and_survive_save_reload(self):
        for full in (False, True):
            with self.subTest(full=full), tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
                for name in ("TRIGGERS_LOCAL_FILE", "_REPO_TRIGGERS_FILE", "_REPO_RETIRED_FILE", "_REPO_TRIGGERS_VERSION"):
                    stack.enter_context(patch.object(ac, name, Path(directory) / name))
                records = [{"id": ident, "enabled": True, **({"tts_text": "Saved text"} if full else {})}
                           for ident in REPLACEMENTS]
                records.extend([{"id": "custom", "enabled": False, "tts_text": "Keep my edit"},
                                {"id": "custom", "enabled": True}])
                ac.TRIGGERS_LOCAL_FILE.write_text(json.dumps({"triggers": records, "deleted": []}))

                class Window(TriggersTabMixin):
                    def _refresh_table(self):
                        pass

                host = Window()
                host._load_triggers()
                survivors = {t.id: t for t in host._triggers}
                self.assertFalse(survivors["custom"].enabled)
                self.assertEqual(survivors["custom"].tts_text, "Keep my edit")
                for retired, targets in REPLACEMENTS.items():
                    self.assertNotIn(retired, survivors)
                    self.assertTrue(all(survivors[t].enabled for t in targets))
                    for ident in targets:
                        bundled = next(r for r in ROWS if r["id"] == ident)["tts_text"]
                        expected = "Saved text" if full else bundled
                        self.assertEqual(survivors[ident].tts_text, expected)
                        self.assertEqual(host._official_triggers[ident].tts_text, bundled)
                self.assertTrue(host._save_triggers())
                saved = json.loads(ac.TRIGGERS_LOCAL_FILE.read_text())
                self.assertFalse({r["id"] for r in saved["triggers"]} & REPLACEMENTS.keys())
                host._load_triggers()
                survivors = {t.id: t for t in host._triggers}
                self.assertTrue(all(survivors[t].enabled for targets in REPLACEMENTS.values() for t in targets))
                for targets in REPLACEMENTS.values():
                    for ident in targets:
                        bundled = next(r for r in ROWS if r["id"] == ident)["tts_text"]
                        self.assertEqual(survivors[ident].tts_text, "Saved text" if full else bundled)

    def test_retired_wording_survives_slim_toggles_but_keeps_survivor_edits(self):
        retired, targets = next(iter(REPLACEMENTS.items()))
        ident = targets[0]
        bundled = next(r for r in ROWS if r["id"] == ident)
        for survivor_kind, survivor_text in (("slim", None), ("legacy", None),
                                             ("custom", "Survivor wording"), ("blank", "")):
            with self.subTest(survivor_kind=survivor_kind), tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
                for name in ("TRIGGERS_LOCAL_FILE", "_REPO_TRIGGERS_FILE", "_REPO_RETIRED_FILE", "_REPO_TRIGGERS_VERSION"):
                    stack.enter_context(patch.object(ac, name, Path(directory) / name))
                retired_row = {**bundled, "id": retired, "enabled": True, "tts_text": "Join my group"}
                survivor = {"id": ident, "enabled": False}
                if survivor_kind == "legacy":
                    survivor = {**bundled, **survivor}
                elif survivor_text is not None:
                    survivor = {**bundled, **survivor, "tts_text": survivor_text, "cooldown_s": 31}
                ac.TRIGGERS_LOCAL_FILE.write_text(json.dumps({"triggers": [retired_row, survivor]}))

                class Window(TriggersTabMixin):
                    def _refresh_table(self):
                        pass

                host = Window()
                host._load_triggers()
                self.assertTrue(host._save_triggers())
                host._load_triggers()
                trigger = next(t for t in host._triggers if t.id == ident)
                self.assertTrue(trigger.enabled)
                expected = "Join my group" if survivor_text is None else survivor_text
                self.assertEqual(trigger.tts_text, expected)
                if survivor_text is not None:
                    self.assertEqual(trigger.cooldown_s, 31)
                else:
                    dispatch = Host([1000.0])
                    self.addCleanup(dispatch._clear_seq_runners)
                    dispatch._triggers = host._triggers
                    dispatch._zone_aliases = ("Everkeep (Extreme)",)
                    with patch("nyaatriggers.main_window.speak") as speech:
                        dispatch.dispatch("9374", "20")
                        speech.assert_called_once_with(expected, speed=1.0, reading=expected)

    def test_profiles_merge_enabled_choices_without_rewriting_the_saved_profile(self):
        retired, targets = next(iter(REPLACEMENTS.items()))
        profile = {"name": "Old profile", "local": {retired: {"enabled": True, "text": "Duplicate"},
                                                    targets[0]: {"enabled": False, "text": "My text"}},
                   "engines": {}}
        original = deepcopy(profile)
        trigger = Trigger(id=targets[0], enabled=False, tts_text="Before")
        window = SimpleNamespace(_triggers=[trigger], _local_ids=set(), _settings={}, _trigger_replacements=REPLACEMENTS,
                                 _engine_seen={}, _engine_disabled={s: set() for s in ("cactbot", "triggevent", "triggernometry")},
                                 _engine_inventory=[])
        default = preserve_default(window, None, profile)
        apply_choices(window, profile)
        self.assertTrue(trigger.enabled)
        self.assertEqual(trigger.tts_text, "My text")
        apply_choices(window, default)
        self.assertFalse(trigger.enabled)
        self.assertEqual(trigger.tts_text, "Before")
        self.assertEqual(profile, original)

    def test_fanout_and_disabled_duplicates_do_not_disable_existing_choices(self):
        retired = next(k for k, targets in REPLACEMENTS.items() if len(targets) == 3)
        targets = REPLACEMENTS[retired]
        merged = merge_local_choices({retired: {"enabled": True, "text": "Shared"}}, REPLACEMENTS)
        self.assertTrue(all(merged[t]["enabled"] for t in targets))
        choices = {retired: {"enabled": False}, targets[0]: {"enabled": True}}
        merged = merge_local_choices(choices, REPLACEMENTS)
        self.assertTrue(merged[targets[0]]["enabled"])


if __name__ == "__main__":
    unittest.main()
