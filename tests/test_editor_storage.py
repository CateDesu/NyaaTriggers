"""Edits and background results must apply to the state that is still current."""

from contextlib import ExitStack
from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt6.QtWidgets import QApplication, QDialog, QLabel

from nyaatriggers import app_common as ac
from nyaatriggers.trigger_engine import Trigger
from nyaatriggers.ui import triggers_tab
from nyaatriggers.ui.dps_tab import DpsTabMixin
from nyaatriggers.ui.instance_tab import InstanceTabMixin
from nyaatriggers.ui.triggers_tab import TriggersTabMixin
from nyaatriggers.updater_ui import UpdaterUiMixin
from tests.test_callout_review_fixes import Host
from tests.test_data_safety import TriggerHost, ability

APP = QApplication.instance() or QApplication([])


class FflogsHost(SimpleNamespace, DpsTabMixin):
    pass


class EditorStorageTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(patch("nyaatriggers.drop_log._LOG_FILE", self.root / "drops.log"))

    def folder_host(self):
        return SimpleNamespace(
            _folders=[{"id": "a", "name": "First", "parent_id": None},
                      {"id": "b", "name": "Second", "parent_id": "a"}],
            _triggers=[Trigger(id="t", fight="Second")], _local_ids={"t"},
            _official_ids=set(), _save_triggers=Mock(), _refresh_tree=Mock(), _refresh_table=Mock())

    def test_folder_rename_survives_a_reload_during_the_dialog(self):
        host = self.folder_host()
        def answer(*args, **kwargs):
            host._folders = deepcopy(host._folders)
            return "Renamed", True
        with patch.object(triggers_tab.QInputDialog, "getText", side_effect=answer):
            TriggersTabMixin._rename_folder(host, "b")
        self.assertEqual(host._folders[1]["name"], "Renamed")
        self.assertEqual(host._triggers[0].fight, "Renamed")

    def test_folder_delete_preserves_children_moved_out_during_confirmation(self):
        host = self.folder_host()
        def answer(*args, **kwargs):
            host._folders = deepcopy(host._folders)
            host._folders[1]["parent_id"] = None
            return ac.QMessageBox.StandardButton.Yes
        with patch.object(ac.QMessageBox, "question", side_effect=answer):
            TriggersTabMixin._delete_folder(host, "a")
        self.assertEqual([folder["id"] for folder in host._folders], ["b"])
        self.assertEqual([trigger.id for trigger in host._triggers], ["t"])

    @unittest.skipUnless(os.name == "posix" and os.geteuid() != 0, "Needs file permissions")
    def test_read_only_local_triggers_can_still_be_exported(self):
        source = self.root / "triggers.local.json"
        source.write_text('{"triggers":[{"id":"kept"}]}')
        source.chmod(0o400)
        self.addCleanup(source.chmod, 0o600)
        destination = self.root / "export.json"
        dialog = Mock()
        dialog.exec.return_value = QDialog.DialogCode.Accepted
        dialog.selectedFiles.return_value = [str(destination)]
        with patch.object(ac, "TRIGGERS_LOCAL_FILE", source), \
                patch.object(ac, "QFileDialog", return_value=dialog), \
                patch.object(ac.QMessageBox, "critical") as failure, \
                patch.object(ac.QMessageBox, "information"):
            TriggersTabMixin._export_triggers(SimpleNamespace())
        failure.assert_not_called()
        self.assertEqual(destination.read_bytes(), source.read_bytes())
        self.assertEqual(source.stat().st_mode & 0o777, 0o400)

    def isolate_trigger_paths(self):
        for name in ("TRIGGERS_FILE", "TRIGGERS_LOCAL_FILE", "RETIRED_FILE",
                     "_REPO_TRIGGERS_FILE", "_REPO_RETIRED_FILE", "_REPO_TRIGGERS_VERSION"):
            self.stack.enter_context(patch.object(ac, name, self.root / name))

    def import_local_triggers(self, destination):
        source = self.root / "import.json"
        source.write_bytes(b'{"triggers":[{"id":"imported"}]}')
        host = SimpleNamespace(_load_triggers=Mock())
        with patch.object(ac, "TRIGGERS_LOCAL_FILE", destination), \
                patch.object(ac.QFileDialog, "getOpenFileName", return_value=(str(source), "")), \
                patch.object(ac.QMessageBox, "question", return_value=ac.QMessageBox.StandardButton.Yes), \
                patch.object(ac.QMessageBox, "critical") as failure, \
                patch.object(ac.QMessageBox, "information") as success:
            TriggersTabMixin._import_triggers(host)
        return host, failure, success, source.read_bytes()

    def test_import_aborts_when_the_backup_cannot_be_replaced(self):
        destination = self.root / "triggers.local.json"
        original = b'{"triggers":[{"id":"original"}]}'
        destination.write_bytes(original)
        backup = destination.with_name(destination.name + ".bak")
        backup.mkdir()
        blocked_copy = backup / destination.name
        blocked_copy.mkdir()
        host, failure, success, _ = self.import_local_triggers(destination)
        self.assertEqual(destination.read_bytes(), original)
        self.assertEqual(list(backup.iterdir()), [blocked_copy])
        failure.assert_called_once()
        success.assert_not_called()
        host._load_triggers.assert_not_called()
        self.assertFalse(list(self.root.glob("*.tmp")))

    def test_import_preserves_an_older_backup_when_sync_fails(self):
        destination = self.root / "triggers.local.json"
        original = b'{"triggers":[{"id":"original"}]}'
        destination.write_bytes(original)
        backup = destination.with_name(destination.name + ".bak")
        backup.write_bytes(b"older backup")
        with patch.object(os, "fsync", side_effect=OSError("disk full")):
            host, failure, success, _ = self.import_local_triggers(destination)
        self.assertEqual(destination.read_bytes(), original)
        self.assertEqual(backup.read_bytes(), b"older backup")
        failure.assert_called_once()
        success.assert_not_called()
        host._load_triggers.assert_not_called()
        self.assertFalse(list(self.root.glob("*.tmp")))

    def test_failed_import_preserves_the_current_file_and_its_backup(self):
        destination = self.root / "triggers.local.json"
        original = b'{"triggers":[{"id":"original"}]}'
        destination.write_bytes(original)
        replace = os.replace

        def fail_replacement(source, target):
            if Path(target) == destination:
                raise OSError("cannot replace local triggers")
            return replace(source, target)

        with patch.object(os, "replace", side_effect=fail_replacement):
            host, failure, success, _ = self.import_local_triggers(destination)
        self.assertEqual(destination.read_bytes(), original)
        self.assertEqual(destination.with_name(destination.name + ".bak").read_bytes(), original)
        failure.assert_called_once()
        success.assert_not_called()
        host._load_triggers.assert_not_called()
        self.assertFalse(list(self.root.glob("*.tmp")))

    def test_import_keeps_the_exact_previous_bytes_before_reloading(self):
        destination = self.root / "triggers.local.json"
        original = b'{"triggers": [], "folders": [{"id": "mine", "name": "Mine"}]}\n'
        destination.write_bytes(original)
        host, failure, success, imported = self.import_local_triggers(destination)
        self.assertEqual(destination.read_bytes(), imported)
        self.assertEqual(destination.with_name(destination.name + ".bak").read_bytes(), original)
        failure.assert_not_called()
        success.assert_called_once()
        host._load_triggers.assert_called_once()

    def test_import_without_a_previous_file_needs_no_backup(self):
        destination = self.root / "triggers.local.json"
        host, failure, success, imported = self.import_local_triggers(destination)
        self.assertEqual(destination.read_bytes(), imported)
        self.assertFalse(destination.with_name(destination.name + ".bak").exists())
        failure.assert_not_called()
        success.assert_called_once()
        host._load_triggers.assert_called_once()

    def edit_host(self):
        self.isolate_trigger_paths()
        original = Trigger(id="edited", name="Original", ability_id="AAAA", tts_text="Original speech")
        ac.TRIGGERS_LOCAL_FILE.write_text(json.dumps({"triggers": [original.to_dict()]}))
        host = TriggerHost()
        host._match_zone = ""
        host._pick_fight_folder = Mock()
        host._load_triggers()
        original = host._triggers[0]
        host._selected_row_key = lambda: original.id
        host._is_engine_key = lambda key: False
        host._selected_trigger = lambda: (original, 0)
        return host, original

    def test_local_editor_cannot_overwrite_a_changed_trigger_after_reload(self):
        for method in ("_edit_trigger", "_open_trigger_for_edit"):
            with self.subTest(method=method):
                host, original = self.edit_host()
                updated = Trigger.from_dict(original.to_dict())
                updated.name = "Dialog edit"
                external = {**original.to_dict(), "ability_id": "BBBB", "tts_text": "External speech"}

                def accept():
                    ac.TRIGGERS_LOCAL_FILE.write_text(json.dumps({"triggers": [external]}))
                    host._load_triggers()
                    return QDialog.DialogCode.Accepted

                dialog = Mock()
                dialog.exec.side_effect = accept
                dialog.get_trigger.return_value = updated
                with patch.object(triggers_tab, "TriggerDialog", return_value=dialog), \
                        patch.object(ac.QMessageBox, "warning") as warning:
                    getattr(host, method)(*([original] if method == "_open_trigger_for_edit" else []))
                self.assertEqual(host._triggers[0].to_dict(), external)
                self.assertEqual(json.loads(ac.TRIGGERS_LOCAL_FILE.read_text())["triggers"], [external])
                warning.assert_called_once()

    def test_local_editor_keeps_unrelated_reload_changes_and_runtime_cooldowns(self):
        for method in ("_edit_trigger", "_open_trigger_for_edit"):
            with self.subTest(method=method):
                host, original = self.edit_host()
                updated = Trigger.from_dict(original.to_dict())
                updated.name = "Dialog edit"
                other = Trigger(id="added", ability_id="BBBB", tts_text="New trigger")

                def accept():
                    original._last_fired["boss"] = 123
                    ac.TRIGGERS_LOCAL_FILE.write_text(json.dumps({
                        "triggers": [original.to_dict(), other.to_dict()]}))
                    host._load_triggers()
                    return QDialog.DialogCode.Accepted

                dialog = Mock()
                dialog.exec.side_effect = accept
                dialog.get_trigger.return_value = updated
                with patch.object(triggers_tab, "TriggerDialog", return_value=dialog), \
                        patch.object(ac.QMessageBox, "warning") as warning:
                    getattr(host, method)(*([original] if method == "_open_trigger_for_edit" else []))
                self.assertEqual([t.to_dict() for t in host._triggers], [updated.to_dict(), other.to_dict()])
                self.assertEqual(json.loads(ac.TRIGGERS_LOCAL_FILE.read_text())["triggers"],
                                 [updated.to_dict(), other.to_dict()])
                warning.assert_not_called()

    def test_save_cannot_replace_external_edits_before_the_next_reload(self):
        host, original = self.edit_host()
        external = {"triggers": [{**original.to_dict(), "name": "External edit"}],
                    "folders": [{"id": "external", "name": "New folder"}]}
        ac.TRIGGERS_LOCAL_FILE.write_text(json.dumps(external))
        original.name = "Unsaved edit"
        with patch.object(ac.QMessageBox, "warning") as warning:
            self.assertFalse(host._save_triggers())
        self.assertEqual(json.loads(ac.TRIGGERS_LOCAL_FILE.read_text()), external)
        warning.assert_called_once()
        host._maybe_reload_triggers()
        host._triggers[0].name = "Retried edit"
        self.assertTrue(host._save_triggers())
        saved = json.loads(ac.TRIGGERS_LOCAL_FILE.read_text())
        self.assertEqual(saved["triggers"][0]["name"], "Retried edit")
        self.assertEqual(saved["folders"], external["folders"])

    def test_poll_retains_live_callouts_until_local_file_has_valid_trigger_rows(self):
        for invalid in ([], {"triggers": "unfinished"}, {"triggers": [None]}):
            with self.subTest(invalid=invalid):
                host, original = self.edit_host()
                original._last_fired["boss"] = 123
                original_info = ac.TRIGGERS_LOCAL_FILE.stat()
                original_stamp = host._triggers_mtime
                ac.TRIGGERS_LOCAL_FILE.write_text(json.dumps(invalid))
                with patch.object(ac.QMessageBox, "warning") as warning:
                    host._maybe_reload_triggers()
                self.assertEqual(host._triggers, [original])
                self.assertIs(host._triggers[0], original)
                self.assertEqual(original._last_fired["boss"], 123)
                warning.assert_not_called()
                fixed = {"triggers": [{**original.to_dict(), "tts_text": "Repaired speech"}]}
                ac.TRIGGERS_LOCAL_FILE.write_text(json.dumps(fixed))
                os.utime(ac.TRIGGERS_LOCAL_FILE,
                         ns=(original_info.st_atime_ns, original_info.st_mtime_ns))
                self.assertEqual(host._trigger_files_stamp(), original_stamp)
                host._maybe_reload_triggers()
                self.assertEqual(host._triggers[0].tts_text, "Repaired speech")

    def test_save_detects_external_creation_and_removal(self):
        for created in (False, True):
            with self.subTest(created=created):
                host, original = self.edit_host()
                if created:
                    ac.TRIGGERS_LOCAL_FILE.unlink()
                    host._load_triggers()
                    ac.TRIGGERS_LOCAL_FILE.write_text(json.dumps({"triggers": [original.to_dict()]}))
                else:
                    ac.TRIGGERS_LOCAL_FILE.unlink()
                with patch.object(ac.QMessageBox, "warning"):
                    self.assertFalse(host._save_triggers())
                self.assertEqual(ac.TRIGGERS_LOCAL_FILE.exists(), created)

    def test_conflicting_same_size_same_time_edit_reloads_before_retry(self):
        host, original = self.edit_host()
        stamp = ac.TRIGGERS_LOCAL_FILE.stat()
        external = ac.TRIGGERS_LOCAL_FILE.read_text().replace('"Original"', '"External"')
        ac.TRIGGERS_LOCAL_FILE.write_text(external)
        os.utime(ac.TRIGGERS_LOCAL_FILE, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        original.name = "Unsaved"
        with patch.object(ac.QMessageBox, "warning"):
            self.assertFalse(host._save_triggers())
        host._maybe_reload_triggers()
        self.assertEqual(host._triggers[0].name, "External")
        host._triggers[0].name = "Retried"
        self.assertTrue(host._save_triggers())

    def test_conflict_warning_cannot_rebaseline_a_callers_rollback(self):
        host, original = self.edit_host()
        external = {"triggers": [{**original.to_dict(), "tts_text": "External speech"}]}
        ac.TRIGGERS_LOCAL_FILE.write_text(json.dumps(external))
        original.tts_text = "Profile speech"
        with patch.object(ac.QMessageBox, "warning", side_effect=lambda *args: host._maybe_reload_triggers()) as warning:
            self.assertFalse(host._save_triggers())
            host._triggers[0].tts_text = "Original speech"
            self.assertFalse(host._save_triggers())
            warning.assert_called_once()
        self.assertEqual(json.loads(ac.TRIGGERS_LOCAL_FILE.read_text()), external)
        host._maybe_reload_triggers()
        self.assertEqual(host._triggers[0].tts_text, "External speech")

    def test_own_save_updates_the_baseline_without_aliasing_sequence_edits(self):
        host, original = self.edit_host()
        original.sequence = [{"ability_id": "BBBB", "tts_text": "First"}]
        self.assertTrue(host._save_triggers())
        host._load_triggers()
        host._triggers[0].sequence[0]["tts_text"] = "Second"
        self.assertTrue(host._save_triggers())
        host._triggers[0].sequence[0]["tts_text"] = "Third"
        self.assertTrue(host._save_triggers())
        saved = json.loads(ac.TRIGGERS_LOCAL_FILE.read_text())
        self.assertEqual(saved["triggers"][0]["sequence"][0]["tts_text"], "Third")

    def test_invalid_downloaded_rows_use_the_bundled_triggers(self):
        self.isolate_trigger_paths()
        ac.TRIGGERS_FILE.write_text('[{"id":"bundled","ability_id":"ABCD"}]')
        ac._REPO_TRIGGERS_FILE.write_text('[null, "bad"]')
        with patch.object(triggers_tab, "_repo_download_version", return_value=ac._VERSION):
            host = TriggerHost()
            host._load_triggers()
        self.assertEqual([trigger.id for trigger in host._triggers], ["bundled"])

    def test_invalid_trigger_download_cannot_replace_the_previous_cache(self):
        self.isolate_trigger_paths()
        previous = b'[{"id":"previous","ability_id":"ABCD"}]'
        ac._REPO_TRIGGERS_FILE.write_bytes(previous)
        host = SimpleNamespace(_trig_dl_in_flight={}, _trig_update_signal=Mock())
        button = Mock()
        with patch("nyaatriggers.updater_ui.fetch_bytes", return_value=b"[null]"), \
                patch("nyaatriggers.updater_ui.threading.Thread",
                      side_effect=lambda target, daemon: SimpleNamespace(start=target)):
            UpdaterUiMixin._download_repo_triggers(host, button, "Restore")
        self.assertEqual(ac._REPO_TRIGGERS_FILE.read_bytes(), previous)
        self.assertFalse(ac._REPO_TRIGGERS_VERSION.exists())
        self.assertTrue(host._trig_update_signal.emit.call_args.args[1].startswith("err:"))

    def test_changed_zone_ids_clear_old_sequences_without_a_new_name(self):
        for name in ("", "Old Arena"):
            with self.subTest(name=name):
                host = Host([1000.0])
                host.prepare_zone()
                trigger = Trigger(ability_id="ABCD", delay_s=10)
                host._triggers = [trigger]
                self.addCleanup(host._clear_seq_runners)
                host.dispatch("ABCD", kind="20")
                self.assertEqual(len(host._seq_runners), 1)
                with patch("nyaatriggers.ui.instance_tab.canonical_zone_name", return_value="New Arena"):
                    InstanceTabMixin._on_ws_zone_changed(host, 200, name)
                self.assertFalse(host._seq_runners)
                self.assertEqual(host._match_zone, "New Arena")

    def test_old_fflogs_result_cannot_replace_a_newer_comparison(self):
        workers = []
        host = FflogsHost(
            _settings={"fflogs_client_id": "old", "fflogs_client_secret": "secret",
                       "fflogs_server": "server", "fflogs_region": "NA"},
            _me_name="Player", _fflogs_lbl=QLabel(), _fflogs_configured=lambda: True)
        host._fflogs_signal = SimpleNamespace(
            emit=lambda *args: DpsTabMixin._on_fflogs_result(host, *args))
        client = SimpleNamespace(fetch_best=lambda char, server, region, title:
                                 {"amount": 1000 if title == "First" else 2000, "percent": 50})
        with patch("nyaatriggers.ui.dps_tab.FflogsClient", return_value=client), \
                patch("nyaatriggers.ui.dps_tab.threading.Thread",
                      side_effect=lambda target, daemon: SimpleNamespace(start=lambda: workers.append(target))):
            DpsTabMixin._maybe_fetch_fflogs(host, "First")
            host._settings["fflogs_client_id"] = "new"
            DpsTabMixin._maybe_fetch_fflogs(host, "Second")
        workers[1]()
        latest = host._fflogs_lbl.text()
        self.assertIn("2.0k", latest)
        workers[0]()
        self.assertEqual(host._fflogs_lbl.text(), latest)

    def test_zone_id_change_finishes_a_pull_even_if_the_name_repeats(self):
        host = Host([1000.0])
        host.prepare_zone()
        meter = host._dps_meter
        ended = []
        meter.on_encounter_end = ended.append
        meter.set_in_combat(True, True)
        meter.process(ability("0003", 1000))
        self.assertIsNotNone(meter.current)
        InstanceTabMixin._on_ws_zone_changed(host, 200, "Old Arena")
        self.assertIsNone(meter.current)
        self.assertEqual(len(ended), 1)
        self.assertEqual(ended[0]["Encounter"]["end_reason"], "duty-left")

    def test_late_first_zone_id_preserves_a_live_sequence(self):
        host = Host([1000.0])
        host.prepare_zone()
        host._current_zone_id = 0
        host._triggers = [Trigger(ability_id="ABCD", delay_s=10)]
        self.addCleanup(host._clear_seq_runners)
        host.dispatch("ABCD", kind="20")
        runner, = host._seq_runners
        with patch("nyaatriggers.ui.instance_tab.canonical_zone_name", return_value="Canonical Arena"):
            InstanceTabMixin._on_ws_zone_changed(host, 100, "")
        self.assertEqual(host._seq_runners, [runner])
        self.assertEqual(host._match_zone, "Canonical Arena")

    def test_fflogs_result_does_not_follow_a_changed_player(self):
        workers = []
        host = FflogsHost(_settings={"fflogs_client_id": "id", "fflogs_client_secret": "secret",
                                    "fflogs_server": "server"},
                          _me_name="First Player", _fflogs_lbl=QLabel())
        host._fflogs_signal = SimpleNamespace(emit=lambda *args: host._on_fflogs_result(*args))
        client = SimpleNamespace(fetch_best=lambda *args: {"amount": 1000, "percent": 50})
        with patch("nyaatriggers.ui.dps_tab.FflogsClient", return_value=client), \
                patch("nyaatriggers.ui.dps_tab.threading.Thread",
                      side_effect=lambda target, daemon: SimpleNamespace(start=lambda: workers.append(target))):
            host._maybe_fetch_fflogs("Fight")
        host._me_name = "Second Player"
        workers[0]()
        self.assertEqual(host._fflogs_lbl.text(), "FFLogs: no data")

    def test_failed_fflogs_worker_start_returns_to_a_finished_state(self):
        host = FflogsHost(_settings={"fflogs_client_id": "id", "fflogs_client_secret": "secret",
                                    "fflogs_server": "server"},
                          _me_name="Player", _fflogs_lbl=QLabel())
        with patch("nyaatriggers.ui.dps_tab.threading.Thread.start", side_effect=RuntimeError("No thread")):
            host._maybe_fetch_fflogs("Fight")
        self.assertEqual(host._fflogs_lbl.text(), "FFLogs: no data")

    def test_zero_fflogs_percentile_is_visible(self):
        host = FflogsHost(_fflogs_request_id=1, _fflogs_lbl=QLabel())
        host._on_fflogs_result(1, {"amount": 1000, "percent": 0})
        self.assertIn("(0%)", host._fflogs_lbl.text())

    def test_explicit_fflogs_character_survives_a_live_player_change(self):
        workers = []
        host = FflogsHost(_settings={"fflogs_client_id": "id", "fflogs_client_secret": "secret",
                                    "fflogs_server": "server", "fflogs_name": "Chosen Player"},
                          _me_name="First Player", _fflogs_lbl=QLabel())
        host._fflogs_signal = SimpleNamespace(emit=lambda *args: host._on_fflogs_result(*args))
        client = SimpleNamespace(fetch_best=lambda *args: {"amount": 1000, "percent": 0})
        with patch("nyaatriggers.ui.dps_tab.FflogsClient", return_value=client), \
                patch("nyaatriggers.ui.dps_tab.threading.Thread",
                      side_effect=lambda target, daemon: SimpleNamespace(start=lambda: workers.append(target))):
            host._maybe_fetch_fflogs("Fight")
        host._me_name = "Second Player"
        workers[0]()
        self.assertIn("1.0k", host._fflogs_lbl.text())
        self.assertIn("(0%)", host._fflogs_lbl.text())

    def test_folder_removed_during_rename_is_not_restored(self):
        host = self.folder_host()
        def answer(*args, **kwargs):
            host._folders.pop()
            return "Renamed", True
        with patch.object(triggers_tab.QInputDialog, "getText", side_effect=answer):
            TriggersTabMixin._rename_folder(host, "b")
        self.assertEqual([folder["id"] for folder in host._folders], ["a"])
        self.assertEqual(host._triggers[0].fight, "Second")
        host._save_triggers.assert_not_called()

    def test_missing_name_on_a_known_zone_change_finishes_the_old_pull(self):
        host = Host([1000.0])
        host.prepare_zone()
        meter = host._dps_meter
        ended = []
        meter.on_encounter_end = ended.append
        meter.set_in_combat(True, True)
        meter.process(ability("0003", 1000))
        with patch("nyaatriggers.ui.instance_tab.canonical_zone_name", return_value=""):
            InstanceTabMixin._on_ws_zone_changed(host, 200, "")
        self.assertIsNone(meter.current)
        self.assertEqual(len(ended), 1)
        meter.set_in_combat(False, False)
        meter.set_in_combat(True, True)
        meter.process(ability("0003", 2000))
        current = meter.current
        self.assertIsNotNone(current)
        InstanceTabMixin._on_ws_zone_changed(host, 200, "New Arena")
        self.assertIs(meter.current, current)
        self.assertEqual(current.zone, "New Arena")
        self.assertEqual(len(ended), 1)

    def test_late_zone_name_keeps_callouts_started_after_the_boundary(self):
        host = Host([1000.0])
        host.prepare_zone()
        host._triggers = [Trigger(ability_id="ABCD", delay_s=10)]
        self.addCleanup(host._clear_seq_runners)
        with patch("nyaatriggers.ui.instance_tab.canonical_zone_name", return_value=""):
            host._on_ws_zone_changed(200, "")
            host.dispatch("ABCD", kind="20")
            runner, = host._seq_runners
            host._on_ws_zone_changed(200, "New Arena")
        self.assertEqual(host._seq_runners, [runner])
        self.assertEqual(host._current_zone, "New Arena")

    def test_corrected_name_for_the_same_zone_preserves_callouts_and_damage(self):
        host = Host([1000.0])
        host.prepare_zone()
        host._triggers = [Trigger(ability_id="ABCD", delay_s=10)]
        self.addCleanup(host._clear_seq_runners)
        host.dispatch("ABCD", kind="20")
        runner, = host._seq_runners
        host._dps_meter.set_in_combat(True, True)
        host._dps_meter.process(ability("0003", 1000))
        current = host._dps_meter.current
        host._on_ws_zone_changed(100, "Localized Arena")
        with self.subTest(state="callouts"):
            self.assertEqual(host._seq_runners, [runner])
        with self.subTest(state="damage"):
            self.assertIs(host._dps_meter.current, current)
        self.assertEqual(host._current_zone, "Localized Arena")

    def test_empty_zone_metadata_does_not_forget_the_previous_identity(self):
        host = Host([1000.0])
        host.prepare_zone()
        host._triggers = [Trigger(ability_id="ABCD", delay_s=10)]
        self.addCleanup(host._clear_seq_runners)
        host.dispatch("ABCD", kind="20")
        host._on_ws_zone_changed(0, "")
        with self.subTest(state="identity"):
            self.assertEqual(host._current_zone_id, 100)
        host._on_ws_zone_changed(200, "Old Arena")
        with self.subTest(state="boundary"):
            self.assertFalse(host._seq_runners)

    def test_unknown_name_after_a_zone_boundary_does_not_block_filtered_callouts(self):
        host = Host([1000.0])
        host.prepare_zone()
        host._triggers = [Trigger(ability_id="ABCD", zone_regex="New Arena", delay_s=10)]
        self.addCleanup(host._clear_seq_runners)
        with patch("nyaatriggers.ui.instance_tab.canonical_zone_name", return_value=""):
            host._on_ws_zone_changed(200, "")
        host.dispatch("ABCD", kind="20")
        self.assertEqual(len(host._seq_runners), 1)

    def test_consecutive_nameless_id_changes_remain_real_boundaries(self):
        host = Host([1000.0])
        host.prepare_zone()
        host._triggers = [Trigger(ability_id="ABCD", delay_s=10)]
        self.addCleanup(host._clear_seq_runners)
        with patch("nyaatriggers.ui.instance_tab.canonical_zone_name", return_value=""):
            for zone_id in (200, 300):
                host.dispatch("ABCD", kind="20")
                self.assertEqual(len(host._seq_runners), 1)
                host._on_ws_zone_changed(zone_id, "")
                self.assertFalse(host._seq_runners)


if __name__ == "__main__":
    unittest.main()
