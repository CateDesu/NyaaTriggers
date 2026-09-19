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
