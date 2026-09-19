"""Deadline enforcement and recovery from interrupted file operations."""

from contextlib import ExitStack
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import wave

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt6.QtWidgets import QApplication

from nyaatriggers import app_common as ac
from nyaatriggers.convert_triggernometry import convert_xml
from nyaatriggers.sequential import SequentialRunner
from nyaatriggers.trigger_engine import Trigger
from nyaatriggers import triggernometry_bridge
from nyaatriggers.ui import engines
from nyaatriggers.ui.dps_tab import DpsTabMixin
from nyaatriggers.ui.voice_tab import VoiceTabMixin

APP = QApplication.instance() or QApplication([])


class ExtendedBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))

    def test_late_sequence_followup_cannot_beat_an_overdue_timer(self):
        now = [100.0]
        done, expired = Mock(), Mock()
        trigger = Trigger(sequence=[{"log_type": "21", "ability_id": "ABCD", "timeout_s": 10}])
        with patch("time.monotonic", side_effect=lambda: now[0]):
            runner = SequentialRunner(trigger, {}, done, expired)
            self.addCleanup(runner.cancel)
            now[0] = 110.1
            matched = runner.try_advance(["21", "ts", "40000001", "Boss", "ABCD"])
            self.assertFalse(matched)
            done.assert_not_called()
            expired.assert_called_once_with(runner)

    def test_nested_inventory_caches_do_not_prevent_startup(self):
        for name in ("_TRIGGEVENT_INVENTORY_CACHE", "_TRIGGERNOMETRY_INVENTORY_CACHE",
                     "_TRIGGEVENT_INVENTORY_SEED"):
            self.stack.enter_context(patch.object(ac, name, self.root / name))
        deep = "[" * 2000 + "]" * 2000
        ac._TRIGGEVENT_INVENTORY_CACHE.write_text(deep)
        ac._TRIGGERNOMETRY_INVENTORY_CACHE.write_text(deep)
        ac._TRIGGEVENT_INVENTORY_SEED.write_text('[{"id":"seed","name":"Seed"}]')
        for source in ("triggevent", "triggernometry"):
            with self.subTest(source=source):
                host = SimpleNamespace(_engine_inventory=[], _record_engine_seen=Mock())
                getattr(engines.EnginesMixin, f"_load_cached_{source}_inventory")(host)
                self.assertEqual([row["id"] for row in host._engine_inventory],
                                 ["seed"] if source == "triggevent" else [])

    def test_invalid_inventory_rows_cannot_mask_the_bundled_seed(self):
        for name in ("_TRIGGEVENT_INVENTORY_CACHE", "_TRIGGEVENT_INVENTORY_SEED"):
            self.stack.enter_context(patch.object(ac, name, self.root / name))
        ac._TRIGGEVENT_INVENTORY_CACHE.write_text('[null, {}, {"id": false}]')
        ac._TRIGGEVENT_INVENTORY_SEED.write_text('[{"id":"seed","name":"Seed"}]')
        host = SimpleNamespace(_engine_inventory=[], _record_engine_seen=Mock())
        engines.EnginesMixin._load_cached_triggevent_inventory(host)
        self.assertEqual([row["id"] for row in host._engine_inventory], ["seed"])

    @unittest.skipUnless(os.name == "posix", "Needs a process file size limit")
    def test_interrupted_sound_import_preserves_the_previous_sound(self):
        source = self.root / "incoming" / "sound.wav"
        destination = self.root / "sounds" / "sound.wav"
        for path, sample in ((source, b"\x01\x00"), (destination, b"\x02\x00")):
            path.parent.mkdir()
            with wave.open(str(path), "wb") as sound:
                sound.setnchannels(1)
                sound.setsampwidth(2)
                sound.setframerate(8000)
                sound.writeframes(sample * 2000)
        previous = destination.read_bytes()
        script = '''
import resource
import signal
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from nyaatriggers import app_common as ac
from nyaatriggers.ui.voice_tab import VoiceTabMixin

source, destination = map(Path, sys.argv[1:])
ac._USER_SOUNDS_DIR = destination.parent
host = SimpleNamespace(_settings={}, _save_settings=Mock(), _populate_sound_combo=Mock(),
                       _select_alert_sound_in_combo=Mock(), _alert_sound_path=lambda: None)
with patch.object(ac.QFileDialog, "getOpenFileName", return_value=(str(source), "")), \\
        patch.object(ac.QMessageBox, "warning") as warning:
    signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
    resource.setrlimit(resource.RLIMIT_FSIZE, (128, 128))
    VoiceTabMixin._import_sfx(host)
    assert warning.call_count == 1
    assert not host._settings
'''
        result = subprocess.run([sys.executable, "-c", script, str(source), str(destination)],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(destination.read_bytes(), previous)
        self.assertEqual(list(destination.parent.iterdir()), [destination])

    def test_folder_creation_failures_are_reported_by_the_gui(self):
        blocked = self.root / "blocked"
        blocked.write_text("Existing file")
        for method, module in ((DpsTabMixin._open_dps_folder, "dps_tab"),
                               (VoiceTabMixin._open_voices_folder, "voice_tab")):
            with self.subTest(method=method.__name__), \
                    patch.object(ac, "_USER_VOICES_DIR", blocked), \
                    patch.object(ac.QMessageBox, "warning") as warning, \
                    patch(f"nyaatriggers.ui.{module}.QDesktopServices.openUrl") as opened:
                method(SimpleNamespace(_dps_dir=lambda: blocked))
                warning.assert_called_once()
                opened.assert_not_called()

    def test_invalid_xml_is_not_staged_as_an_imported_triggernometry_pack(self):
        source = self.root / "broken.xml"
        source.write_text("<TriggernometryExport>")
        packs = self.root / "packs"
        packs.mkdir()
        host = SimpleNamespace(_triggers=[], _triggers_enabled=False, _local_ids=set())
        with patch.object(ac.QFileDialog, "getOpenFileName", return_value=(str(source), "")), \
                patch.object(ac.QMessageBox, "critical") as failure, \
                patch.object(ac.QMessageBox, "information") as success, \
                patch.object(engines, "_tn_packs_dir", return_value=packs), \
                patch.object(engines, "TriggernometryBridge", SimpleNamespace(is_available=lambda: True)):
            engines.EnginesMixin._import_triggernometry(host)
        self.assertFalse(list(packs.iterdir()))
        failure.assert_called_once()
        success.assert_not_called()

    def import_pack(self, source, packs):
        host = SimpleNamespace(_triggers=[], _triggers_enabled=False, _local_ids=set())
        self.stack.enter_context(patch.object(ac.QFileDialog, "getOpenFileName",
                                             return_value=(str(source), "")))
        failure = self.stack.enter_context(patch.object(ac.QMessageBox, "critical"))
        success = self.stack.enter_context(patch.object(ac.QMessageBox, "information"))
        warning = self.stack.enter_context(patch.object(ac.QMessageBox, "warning"))
        self.stack.enter_context(patch.object(engines, "_tn_packs_dir", return_value=packs))
        self.stack.enter_context(patch.object(engines, "TriggernometryBridge",
                                             SimpleNamespace(is_available=lambda: True)))
        engines.EnginesMixin._import_triggernometry(host)
        return failure, success, warning

    def test_unrelated_xml_is_not_accepted_as_a_pack(self):
        source = self.root / "website.xml"
        source.write_text("<html><body>Not a trigger export</body></html>")
        packs = self.root / "packs"
        packs.mkdir()
        failure, success, _ = self.import_pack(source, packs)
        self.assertFalse(list(packs.iterdir()))
        failure.assert_called_once()
        success.assert_not_called()

    def test_pack_without_an_exported_folder_is_not_reported_as_imported(self):
        source = self.root / "empty.xml"
        source.write_text("<TriggernometryExport/>")
        packs = self.root / "packs"
        packs.mkdir()
        failure, success, _ = self.import_pack(source, packs)
        self.assertFalse(list(packs.iterdir()))
        failure.assert_called_once()
        success.assert_not_called()

    def test_scripted_pack_remains_available_with_no_convertible_triggers(self):
        source = Path("triggernometry-core/test/packs/script-error.xml")
        packs = self.root / "packs"
        packs.mkdir()
        self.assertEqual(convert_xml(source, {}), [])
        failure, success, warning = self.import_pack(source, packs)
        self.assertEqual((packs / source.name).read_bytes(), source.read_bytes())
        failure.assert_not_called()
        warning.assert_not_called()
        success.assert_called_once()

    def test_import_stages_the_bytes_that_were_validated(self):
        for managed in (False, True):
            with self.subTest(managed=managed):
                packs = self.root / str(managed)
                packs.mkdir()
                source = packs / "pack.xml" if managed else self.root / "pack.xml"
                content = b'<TriggernometryExport><ExportedFolder Name="Pack"/></TriggernometryExport>'
                source.write_bytes(content)
                def replaced(path, zone_map, **kwargs):
                    converted = convert_xml(path, zone_map, **kwargs)
                    path.write_text("<broken")
                    return converted
                with patch.object(engines, "_tn_convert_xml", side_effect=replaced):
                    failure, success, _ = self.import_pack(source, packs)
                failure.assert_not_called()
                success.assert_called_once()
                self.assertEqual((packs / source.name).read_bytes(), content)

    def test_pack_write_failure_is_visible_and_keeps_storage_unchanged(self):
        source = Path("triggernometry-core/test/packs/script-error.xml")
        packs = self.root / "packs"
        packs.mkdir()
        with patch("nyaatriggers.app_common.os.replace", side_effect=OSError("Disk unavailable")):
            failure, success, warning = self.import_pack(source, packs)
        self.assertFalse(list(packs.iterdir()))
        self.assertEqual(failure.call_count + warning.call_count, 1)
        success.assert_not_called()

    def test_atomic_replacement_preserves_old_bytes_when_flushing_fails(self):
        destination = self.root / "saved.wav"
        destination.write_bytes(b"Previous sound")
        with patch("nyaatriggers.app_common.os.fsync", side_effect=OSError("Disk unavailable")):
            with self.assertRaises(OSError):
                ac._atomic_write_bytes(destination, b"Replacement sound")
        self.assertEqual(destination.read_bytes(), b"Previous sound")
        self.assertEqual(list(self.root.iterdir()), [destination])

    def test_atomic_replacement_accepts_long_valid_filenames(self):
        destination = self.root / ("s" * 240 + ".wav")
        destination.write_bytes(b"Previous sound")
        ac._atomic_write_bytes(destination, b"Replacement sound")
        self.assertEqual(destination.read_bytes(), b"Replacement sound")
        self.assertEqual(list(self.root.iterdir()), [destination])

    def test_imported_pack_without_xml_suffix_is_discovered_by_the_engine(self):
        source = self.root / "downloaded-pack"
        source.write_text('<TriggernometryExport><ExportedFolder Name="Pack"/></TriggernometryExport>')
        packs = self.root / "packs"
        packs.mkdir()
        failure, success, warning = self.import_pack(source, packs)
        failure.assert_not_called()
        warning.assert_not_called()
        success.assert_called_once()
        with patch.object(triggernometry_bridge, "_packs_dir", return_value=packs):
            found = triggernometry_bridge._find_packs()
        self.assertEqual(len(found), 1)
        self.assertEqual(Path(found[0]).read_bytes(), source.read_bytes())

    def test_normalizing_a_pack_suffix_preserves_existing_exports(self):
        source = self.root / "pack.txt"
        source.write_text('<TriggernometryExport><ExportedFolder Name="New"/></TriggernometryExport>')
        packs = self.root / "packs"
        packs.mkdir()
        previous = packs / "pack.xml"
        previous.write_bytes(b"Previous export")
        failure, success, warning = self.import_pack(source, packs)
        failure.assert_not_called()
        warning.assert_not_called()
        success.assert_called_once()
        self.assertEqual(previous.read_bytes(), b"Previous export")
        self.assertEqual((packs / "pack_2.xml").read_bytes(), source.read_bytes())

    def test_pack_name_collision_preserves_both_exports(self):
        source = self.root / "pack.xml"
        source.write_text('<TriggernometryExport><ExportedFolder Name="New"/></TriggernometryExport>')
        packs = self.root / "packs"
        packs.mkdir()
        previous = packs / source.name
        previous.write_bytes(b"Previous export")
        failure, success, warning = self.import_pack(source, packs)
        self.assertEqual(previous.read_bytes(), b"Previous export")
        self.assertEqual((packs / "pack_2.xml").read_bytes(), source.read_bytes())
        failure.assert_not_called()
        warning.assert_not_called()
        success.assert_called_once()

    def test_xml_input_limits_apply_to_the_validated_snapshot(self):
        source = self.root / "pack.xml"
        source.write_bytes(b"A different file")
        declarations = '<!DOCTYPE x [<!ENTITY text "test">]><TriggernometryExport/>'
        for content in (declarations.encode(), declarations.encode("utf-16"),
                        b"x" * (16 * 1024 * 1024 + 1),
                        b'<?xml version="1.0" encoding="unknown"?><TriggernometryExport/>'):
            with self.subTest(size=len(content)):
                with self.assertRaises((ValueError, LookupError)):
                    convert_xml(source, {}, content=content, strict=True)
                self.assertEqual(convert_xml(source, {}, content=content), [])

    def test_sequence_followup_at_the_deadline_is_expired(self):
        now = [100.0]
        done, expired = Mock(), Mock()
        trigger = Trigger(sequence=[{"log_type": "21", "timeout_s": 10}])
        with patch("time.monotonic", side_effect=lambda: now[0]):
            runner = SequentialRunner(trigger, {}, done, expired)
            self.addCleanup(runner.cancel)
            now[0] = 110.0
            self.assertFalse(runner.try_advance(["21"]))
        done.assert_not_called()
        expired.assert_called_once_with(runner)

    def test_each_sequence_step_gets_its_own_deadline(self):
        now = [100.0]
        done, expired = Mock(), Mock()
        trigger = Trigger(sequence=[{"log_type": "21", "ability_id": "AAAA", "timeout_s": 10},
                                    {"log_type": "21", "ability_id": "BBBB", "timeout_s": 10}])
        with patch("time.monotonic", side_effect=lambda: now[0]):
            runner = SequentialRunner(trigger, {}, done, expired)
            self.addCleanup(runner.cancel)
            now[0] = 109.9
            self.assertFalse(runner.try_advance(["21", "ts", "40000001", "Boss", "AAAA"]))
            now[0] = 119.8
            self.assertTrue(runner.try_advance(["21", "ts", "40000001", "Boss", "BBBB"]))
        done.assert_called_once()
        expired.assert_not_called()


if __name__ == "__main__":
    unittest.main()
