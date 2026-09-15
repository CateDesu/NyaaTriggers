"""Meter attribution, saved wording and cancellation across voice backends."""

from copy import deepcopy
import json
import os
from pathlib import Path
from queue import Queue
import sys
import tempfile
import threading
import time
from types import MethodType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import wave

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from nyaatriggers import dps_store, main_window, tts
from nyaatriggers.death_recap import DeathRecap
from nyaatriggers.dps_meter import DpsMeter
from nyaatriggers.trigger_engine import Trigger
from nyaatriggers.trigger_profiles import apply_choices
from nyaatriggers.ui.settings_tab import SettingsTabMixin
from nyaatriggers.ui.triggers_tab import TriggersTabMixin
from tests.test_session_features import PLAYER, BOSS, Clock, ability, area_ability


class MeterRecoveryTests(unittest.TestCase):
    def meter(self):
        clock = Clock()
        meter = DpsMeter(clock)
        meter.process(["02", "ts", PLAYER, "Player"])
        return meter, clock

    def test_reflection_agrees_with_recap_in_both_directions(self):
        for make_line in (ability, area_ability):
            for source, target in ((PLAYER, BOSS), (BOSS, PLAYER)):
                with self.subTest(kind=make_line.__name__, source=source):
                    meter, clock = self.meter()
                    recap = DeathRecap(clock)
                    line = make_line(source=source, target=target,
                                     pairs=[("03", "10000"), ("1D", "50000"), ("6003", "50000")])
                    meter.process(line)
                    recap.process(line)
                    recap.process(["25", "ts", PLAYER, "Player"])
                    row = next(iter(meter.full_snapshot()["Combatant"].values()))
                    outgoing = 1 if source == PLAYER else 5
                    incoming = 5 if source == PLAYER else 1
                    self.assertEqual((row["damage"], row["damagetaken"]), (outgoing, incoming))
                    self.assertEqual(row["hits"], 1)
                    self.assertEqual(row["swings"], 1)
                    self.assertEqual(row["crithits"], int(source == BOSS))
                    self.assertEqual(row["DirectHitPct"], 100 if source == BOSS else 0)
                    self.assertEqual(row["maxhit"], f"Ability-{outgoing}")
                    self.assertEqual(sum(e["amount"] for e in recap.deaths[0]["events"]), incoming)

    def test_reflection_does_not_spill_into_the_next_area_target(self):
        meter, _clock = self.meter()
        meter.process(area_ability(source=PLAYER, target=BOSS, index=0, count=2,
                                   pairs=[("03", "10000"), ("1D", "50000"), ("03", "50000")]))
        meter.process(area_ability(source=PLAYER, target="40005678", index=1, count=2,
                                   pairs=[("03", "70000")]))
        row = next(iter(meter.full_snapshot()["Combatant"].values()))
        self.assertEqual((row["damage"], row["damagetaken"], row["hits"]), (8, 5, 2))

    def test_pet_reflection_keeps_owner_damage_and_excludes_pet_damage_taken(self):
        pet = "40000002"
        for source, target, expected in ((pet, BOSS, 1), (BOSS, pet, 5)):
            with self.subTest(source=source):
                meter, _clock = self.meter()
                meter.process(["03", "ts", pet, "Pet", "0", "0", PLAYER])
                meter.process(ability(source=source, target=target,
                                      pairs=[("03", "10000"), ("1D", "50000"), ("03", "50000")]))
                row = next(iter(meter.full_snapshot()["Combatant"].values()))
                self.assertEqual(row["name"], "Player")
                self.assertEqual((row["damage"], row["damagetaken"]), (expected, 0))

    def test_unrelated_abilities_and_ticks_do_not_extend_or_reset_the_meter(self):
        dot = ["24", "ts", "40005678", "NPC", "DoT", "0", "64"] + ["0"] * 10 + [BOSS, "NPC"]
        hot = dot.copy()
        hot[4] = "HoT"
        for fields in (ability(source=BOSS, target="40005678"), dot, hot):
            with self.subTest(kind=fields[0], effect=fields[4]):
                meter, clock = self.meter()
                meter.set_idle_timeout(15)
                meter.process(ability(source=PLAYER, target=BOSS))
                clock.value += 10
                meter.process(ability(source=PLAYER, target=BOSS))
                clock.value += 90
                meter.process(fields)
                self.assertEqual(meter.snapshot()["Encounter"]["damage"], 2000)
                meter.finalize()
                encounter = meter.snapshot()["Encounter"]
                self.assertEqual((encounter["damage"], encounter["DURATION"], encounter["dps"]),
                                 (2000, 10, 200))
                meter.process(fields)
                self.assertIsNone(meter.current)

    def test_player_ticks_still_start_and_extend_combat(self):
        meter, clock = self.meter()
        outgoing = ["24", "ts", BOSS, "NPC", "DoT", "0", "64"] + ["0"] * 10 + [PLAYER, "Player"]
        meter.process(outgoing)
        self.assertIsNotNone(meter.current)
        incoming = outgoing.copy()
        incoming[2], incoming[17] = PLAYER, BOSS
        clock.value += 10
        meter.process(incoming)
        meter.finalize()
        row = next(iter(meter.snapshot()["Combatant"].values()))
        self.assertEqual((row["damage"], row["damagetaken"]), (100, 100))
        self.assertEqual(meter.snapshot()["Encounter"]["DURATION"], 10)

    def test_nested_active_log_preserves_old_bytes_and_accepts_new_pulls(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "2026-01-01_00-00-00.jsonl"
            original = "[" * 200000 + "0" + "]" * 200000
            path.write_text(original)
            for title in ("First", "Second"):
                with patch.object(dps_store, "_last_written", None):
                    self.assertEqual(dps_store.write_pull(folder, {"title": title}), path)
            lines = path.read_text().splitlines()
            self.assertEqual(lines[0], original)
            self.assertEqual([json.loads(line)["title"] for line in lines[1:]], ["First", "Second"])


class CalloutWordingTests(unittest.TestCase):
    def test_profiles_edits_reset_and_phrase_fallback_reach_speech(self):
        root = Path(__file__).resolve().parents[1]
        rows = json.loads((root / "assets/triggers.json").read_text())
        overlay = json.loads((root / "assets/callouts_ja.json").read_text())
        row = next(row for row in rows if row.get("tts_text") and row["id"] in overlay["callouts"])
        trigger = Trigger.from_dict(row)
        trigger.interrupt = False
        trigger.sound_file = ""
        host = SimpleNamespace(_triggers=[trigger], _local_ids=set(), _engine_disabled={},
                               _settings={"callouts_localized": True},
                               _official_triggers={trigger.id: deepcopy(trigger)},
                               _callouts_ja=overlay["callouts"], _callouts_phrases_ja=overlay["phrases"],
                               _callouts_readings=overlay["readings"],
                               _claim_callout=Mock(), _emit_alert=Mock())
        host._localized_callout = MethodType(TriggersTabMixin._localized_callout, host)
        host._reading_for = MethodType(SettingsTabMixin._reading_for, host)
        fields = {"source": "Caster", "target": "Target", "count": "2"}

        def substitute(text):
            for key, value in fields.items():
                text = text.replace("{" + key + "}", value)
            return text

        def fire():
            with patch.object(main_window, "speak") as speak:
                main_window.MainWindow._fire(host, trigger, fields)
            return speak.call_args.args[0], speak.call_args.kwargs["reading"]

        original_display = overlay["callouts"][trigger.id]
        self.assertEqual(fire()[0], substitute(original_display))
        for text in ("Custom instruction {target}", "ここで待機 {target}", "Raidwide", row["tts_text"]):
            with self.subTest(text=text):
                apply_choices(host, {"name": "Saved wording", "engines": {}, "local": {
                    trigger.id: {"enabled": True, "text": text}}})
                expected = original_display if text == row["tts_text"] else overlay["phrases"].get(text, text)
                self.assertEqual(fire(), (substitute(expected),
                                         substitute(overlay["readings"].get(expected, expected))))
                host._settings["callouts_localized"] = False
                self.assertEqual(fire()[0], substitute(text))
                host._settings["callouts_localized"] = True
        trigger.id = "custom-id"
        trigger.tts_text = "ここで待機 {target}"
        self.assertEqual(fire()[0], "ここで待機 Target")


class VoiceCancellationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "sound.wav"
        with wave.open(str(self.path), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(8000)
            wav.writeframes(b"\0\0" * 80)
        for name, value in (("_generation", 100), ("_current_proc", None),
                            ("_queue", Queue(maxsize=64)), ("_master_volume", 1.0)):
            self.enterContext(patch.object(tts, name, value))
        self.enterContext(patch.object(tts, "log_drop"))
        self.play = Mock()
        win = SimpleNamespace(SND_FILENAME=0x20000, SND_NODEFAULT=2, PlaySound=self.play)
        self.enterContext(patch.dict(sys.modules, {"winsound": win}))

    def test_cancelled_dequeued_wav_is_dropped_on_both_platforms(self):
        for system in ("Windows", "Linux"):
            for cancel in (False, True):
                with self.subTest(system=system, cancel=cancel), \
                        patch.object(tts.platform, "system", return_value=system), \
                        patch.object(tts.subprocess, "Popen") as spawn:
                    spawn.return_value.communicate.return_value = (b"", b"")
                    spawn.return_value.returncode = 0
                    self.play.reset_mock()
                    tts._enqueue(("wav", str(self.path), 1.0))
                    item = tts._queue.get_nowait()
                    if cancel:
                        tts.interrupt()
                    tts._play_wav_file(item[1], item[2], item.gen)
                    self.assertEqual(self.play.call_count + spawn.call_count, int(not cancel))

    def test_windows_checks_cancellation_when_its_audio_thread_starts(self):
        class DeferredThread:
            def __init__(self, target, **kwargs):
                self.target = target

            def start(self):
                tts.interrupt()
                self.target()

            def join(self, timeout):
                pass

            def is_alive(self):
                return False

        with patch.object(tts.platform, "system", return_value="Windows"), \
                patch.object(tts.threading, "Thread", DeferredThread):
            tts._play_wav(str(self.path), tts._generation)
        self.play.assert_not_called()

    def test_interrupt_does_not_wait_for_windows_audio_to_finish(self):
        entered, release = threading.Event(), threading.Event()

        def play(*args):
            entered.set()
            release.wait(2)

        self.play.side_effect = play
        with patch.object(tts.platform, "system", return_value="Windows"):
            worker = threading.Thread(target=tts._play_wav, args=(str(self.path), tts._generation))
            worker.start()
            try:
                self.assertTrue(entered.wait(1))
                started = time.monotonic()
                tts.interrupt()
                self.assertLess(time.monotonic() - started, .5)
            finally:
                release.set()
                worker.join(2)
        self.assertFalse(worker.is_alive())


if __name__ == "__main__":
    unittest.main()
