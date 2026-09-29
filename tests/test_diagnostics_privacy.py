"""Support exports must exclude private data even when local records are malformed."""

from contextlib import ExitStack
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from zipfile import ZIP_DEFLATED, ZipFile

from nyaatriggers import diagnostics as diag


class DiagnosticsPrivacyTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.directory = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.log = self.directory / "diagnostics.log"
        self.stack.enter_context(patch.object(diag, "_LOG_FILE", self.log))
        self.stack.enter_context(patch.object(diag, "_ENABLED", True))
        self.canaries = ["PrivacyComputer", "private_login", "Player Privacy",
                         "chat-canary-secret", "access-token-canary", "private@example.invalid"]

    def export(self, settings=None):
        destination = self.directory / "support.jsonl"
        count = diag.export_diagnostics(destination, settings)
        text = destination.read_text()
        entries = [json.loads(line) for line in text.splitlines()]
        self.assertEqual(count, len(entries))
        for secret in self.canaries:
            self.assertNotIn(secret, text)
        return entries

    def test_unknown_fields_cannot_capture_names_chat_addresses_or_credentials(self):
        diag.record("engine_queue", gen=3, frames=42, queue_bytes=512, accepted=True,
                    hostname=self.canaries[0], username=self.canaries[1],
                    actor=self.canaries[2], chat=self.canaries[3],
                    token=self.canaries[4], email=self.canaries[5],
                    url="wss://private_login:access-token-canary@PrivacyComputer/ws?key=access-token-canary",
                    path=r"C:\Users\private_login\Documents\private.txt",
                    raw_line="00|2026-09-28T00:00:00|0038|Player Privacy|chat-canary-secret",
                    settings={"fflogs_client_secret": self.canaries[4]})
        entries = self.export()
        self.assertEqual(entries[-1]["data"], {
            "gen": 3, "frames": 42, "queue_bytes": 512, "accepted": True})
        self.assertEqual(entries[-1]["schema"], 1)
        self.assertRegex(entries[-1]["session"], r"^[0-9a-f]{32}$")
        self.assertRegex(entries[-1]["time"], r"Z$")
        self.assertGreaterEqual(entries[-1]["elapsed_ms"], 0)

    def test_unknown_events_and_mistyped_values_are_dropped(self):
        diag.record("Player Privacy", username=self.canaries[1])
        self.assertFalse(self.log.exists())
        diag.record("engine_queue", gen=True, frames="Player Privacy", chars=-1,
                    accepted="true", queue_delay_ms=float("nan"), count=2 ** 100,
                    reason="chat-canary-secret", token={"secret": self.canaries[4]})
        self.assertEqual(self.export()[-1]["data"], {})

    def test_snapshot_contains_only_fixed_booleans_and_valid_versions(self):
        settings = {"auto_connect": True, "local_enabled": False,
                    "cactbot_enabled": "Player Privacy", "triggevent_record_pulls": 1,
                    "hostname": self.canaries[0], "char_name": self.canaries[2],
                    "fflogs_client_secret": self.canaries[4],
                    "ws_url": "ws://private_login:access-token-canary@PrivacyComputer/"}
        with patch.object(diag.importlib.metadata, "version", return_value="private_login"):
            snapshot = self.export(settings)[0]["data"]
        self.assertEqual(snapshot["settings"], {"auto_connect": True, "local_enabled": False})
        self.assertIn("python_version", snapshot)
        self.assertNotIn("pyqt_version", snapshot)
        self.assertNotIn("qt_version", snapshot)

    def test_voice_runtime_flags_and_edit_counts_exclude_their_contents(self):
        settings = {"tts_engine": "system", "master_volume": 1.25,
                    "jp_neural_enabled": True, "overlay_sound_enabled": False,
                    "connected": True, "triggevent_mode": True, "muted": False,
                    "triggevent_disabled_triggers": ["Player Privacy", "private_login"],
                    "triggevent_callout_edits": {"private_login": "chat-canary-secret"},
                    "engine_text_overrides": {"Player Privacy": "access-token-canary"},
                    "triggernometry_disabled_triggers": "private_login"}
        safe = self.export(settings)[0]["data"]["settings"]
        self.assertEqual(safe["tts_engine"], "system")
        self.assertEqual(safe["master_volume"], 1.25)
        self.assertTrue(safe["connected"])
        self.assertFalse(safe["muted"])
        self.assertEqual(safe["triggevent_disabled_count"], 2)
        self.assertEqual(safe["triggevent_edit_count"], 1)
        self.assertEqual(safe["engine_text_override_count"], 1)
        self.assertNotIn("triggernometry_disabled_count", safe)
        for volume in (-1, 3, True, float("inf"), "private_login"):
            self.assertNotIn("master_volume", self.export({"master_volume": volume})[0]["data"]["settings"])

    def test_export_does_not_read_legacy_logs_raw_captures_or_settings(self):
        for filename in ("nyaatriggers.log", "nyaatriggers.log.1", "triggevent.log",
                         "nyaatriggers.crash.log", "nyaatriggers-update.log",
                         "nyaatriggers_settings.json", "pull.jsonl"):
            (self.directory / filename).write_text(" ".join(self.canaries))
        diag.record("engine_ready", gen=2, recovery=True)
        original = diag._owner_only
        seen = []

        def track(path, flags):
            path = Path(path)
            if path.parent == self.directory:
                seen.append(path.name)
            return original(path, flags)

        with patch.object(diag, "_owner_only", track):
            entries = self.export()
        self.assertEqual(len(entries), 2)
        self.assertEqual(set(seen), {"diagnostics.log", "diagnostics.log.1", "diagnostics.log.2"})

    def test_export_revalidates_records_and_rejects_forged_headers(self):
        valid = diag._entry("ws_state", {"state": "connected", "hostname": self.canaries[0]})
        valid["private"] = self.canaries[4]
        malformed = []
        for key, value in (("schema", True), ("event", self.canaries[2]),
                           ("time", self.canaries[0]), ("session", self.canaries[1]),
                           ("elapsed_ms", -1), ("data", self.canaries[3])):
            malformed.append({**valid, key: value})
        self.log.write_text("not JSON " + self.canaries[4] + "\n" + "\n".join(
            json.dumps(value) for value in [valid, *malformed]) + "\n")
        entries = self.export()
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[-1]["data"], {"state": "connected"})
        self.assertNotIn("private", entries[-1])

    def test_deep_json_and_oversized_records_do_not_hide_later_valid_records(self):
        valid = diag._encode(diag._entry("engine_exit", {"gen": 7, "returncode": -9}))
        self.log.write_bytes(b"[" * 1200 + b"]" * 1200 + b"\n"
                             + b"x" * (diag._MAX_RECORD_BYTES + 100) + b"\n" + valid)
        self.assertEqual(self.export()[-1]["data"], {"gen": 7, "returncode": -9})

    def test_export_reads_only_a_bounded_prefix_of_each_generation(self):
        valid = diag._encode(diag._entry("engine_exit", {"gen": 7}))
        self.log.write_bytes(b"x" * 5000 + b"\n" + valid)
        with patch.object(diag, "_MAX_BYTES", 100), patch.object(diag, "_MAX_RECORD_BYTES", 512):
            self.assertEqual(len(self.export()), 1)

    def test_rotations_and_export_have_private_permissions(self):
        self.log.write_text("")
        self.log.chmod(0o666)
        with patch.object(diag, "_MAX_BYTES", 550):
            for generation in range(12):
                diag.record("engine_started", gen=generation)
            paths = diag._log_paths()
            self.assertTrue(all(path.exists() for path in paths))
            self.assertTrue(all(path.stat().st_size <= 550 for path in paths))
        entries = self.export()
        generations = [entry["data"]["gen"] for entry in entries[1:]]
        self.assertEqual(generations, sorted(generations))
        self.assertEqual(generations[-1], 11)
        self.assertLess(len(generations), 12)
        self.assertFalse(self.log.with_name("diagnostics.log.3").exists())
        if os.name == "posix":
            for path in [*paths, self.directory / "support.jsonl"]:
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_unreadable_generation_is_skipped_without_exporting_its_exception(self):
        diag.record("engine_started", gen=3)
        original = diag._owner_only

        def fail(path, flags):
            if Path(path).name == "diagnostics.log.1":
                raise PermissionError("/home/private_login/PrivacyComputer")
            return original(path, flags)

        with patch.object(diag, "_owner_only", fail):
            self.assertEqual(self.export()[-1]["data"], {"gen": 3})

    def test_output_failure_preserves_the_previous_export_and_removes_temporary_file(self):
        destination = self.directory / "support.jsonl"
        destination.write_text("previous export")
        with patch.object(diag.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                diag.export_diagnostics(destination)
        self.assertEqual(destination.read_text(), "previous export")
        self.assertEqual(list(self.directory.glob(".nyaa-diagnostics-*")), [])

    def test_export_cannot_replace_the_active_log_through_dot_segments(self):
        diag.record("engine_started", gen=3)
        previous = self.log.read_bytes()
        (self.directory / "subdir").mkdir()
        with self.assertRaises(ValueError):
            diag.export_diagnostics(self.directory / "subdir" / ".." / "diagnostics.log")
        self.assertEqual(self.log.read_bytes(), previous)

    def test_logging_failure_and_replay_mode_do_not_interrupt_program_work(self):
        self.log.mkdir()
        diag.record("engine_started", gen=1)
        self.log.rmdir()
        with patch.object(diag, "_ENABLED", False):
            diag.record("engine_started", gen=1)
        self.assertFalse(self.log.exists())

    @unittest.skipUnless(hasattr(os, "mkfifo"), "Named pipes are unavailable")
    def test_nonregular_log_files_do_not_block_writes_or_exports(self):
        os.mkfifo(self.log)
        for action in ("record", "export"):
            with self.subTest(action=action):
                command = (
                    "import sys; from pathlib import Path; "
                    "from nyaatriggers import diagnostics as d; "
                    "d._LOG_FILE=Path(sys.argv[1]); d._ENABLED=True; "
                    "d.record('engine_started', gen=1) if sys.argv[2]=='record' "
                    "else d.export_diagnostics(Path(sys.argv[3]))")
                result = subprocess.run(
                    [sys.executable, "-c", command, str(self.log), action,
                     str(self.directory / "support.jsonl")],
                    capture_output=True, text=True, timeout=2)
                self.assertEqual(result.returncode, 0, result.stderr)
        destination = self.directory / "support.jsonl"
        if destination.exists():
            self.assertEqual(len(destination.read_text().splitlines()), 1)

    @unittest.skipUnless(os.name == "posix", "Symlink creation requires platform support")
    def test_log_symlinks_do_not_modify_unrelated_files(self):
        unrelated = self.directory / "unrelated.txt"
        unrelated.write_text("private unrelated contents")
        unrelated.chmod(0o644)
        self.log.symlink_to(unrelated)
        diag.record("engine_started", gen=1)
        self.assertEqual(unrelated.read_text(), "private unrelated contents")
        self.assertEqual(stat.S_IMODE(unrelated.stat().st_mode), 0o644)
        self.log.unlink()
        self.log.with_name("diagnostics.log.1").symlink_to(unrelated)
        diag.record("engine_started", gen=2)
        self.assertEqual(stat.S_IMODE(unrelated.stat().st_mode), 0o644)
        self.assertEqual(self.export()[-1]["data"], {"gen": 2})

    @unittest.skipUnless(os.name == "posix", "Symlink creation requires platform support")
    def test_broken_archive_symlink_does_not_prevent_export(self):
        archive = self.log.with_name("diagnostics.log.1")
        archive.symlink_to(archive.name)
        diag.record("engine_started", gen=2)
        self.assertEqual(self.export()[-1]["data"], {"gen": 2})

    def test_export_validation_does_not_block_engine_diagnostic_writes(self):
        diag.record("engine_started", gen=1)
        validating, release, written = threading.Event(), threading.Event(), threading.Event()
        original = diag._validated_entry
        errors = []

        def validate(value):
            validating.set()
            release.wait(5)
            return original(value)

        def export():
            try:
                diag.export_diagnostics(self.directory / "support.jsonl")
            except Exception as exc:
                errors.append(exc)

        def write():
            diag.record("engine_started", gen=2)
            written.set()

        with patch.object(diag, "_validated_entry", validate):
            exporter = threading.Thread(target=export)
            writer = threading.Thread(target=write)
            exporter.start()
            try:
                self.assertTrue(validating.wait(3))
                writer.start()
                self.assertTrue(written.wait(3), "Engine writes must continue during export validation")
            finally:
                release.set()
                exporter.join(5)
                if writer.ident is not None:
                    writer.join(5)
        self.assertFalse(exporter.is_alive())
        self.assertFalse(errors)
        self.assertIn('"gen":2', self.log.read_text())

    def test_exception_records_use_internal_frames_without_messages_or_locals(self):
        filename = "/home/private_login/NyaaTriggers/nyaatriggers/tts.py"
        try:
            exec(compile("raise ValueError('chat-canary-secret access-token-canary')", filename, "exec"))
        except ValueError as error:
            diag.record_exception("python_exception", error, site="tts")
        data = self.export()[-1]["data"]
        self.assertEqual(data, {"site": "tts", "error_type": "ValueError",
                                "frames": [{"file": "nyaatriggers/tts.py", "line": 1}]})

    def test_external_python_frames_and_unknown_exception_types_are_omitted(self):
        error = type("PrivacyComputer", (Exception,), {})(self.canaries[4])
        diag.record("python_exception", site="uncaught", error_type=type(error).__name__,
                    frames=[{"file": "/home/private_login/custom.py", "line": 23},
                            {"file": "nyaatriggers/../../private_login.py", "line": 2},
                            {"file": "nyaatriggers/tts.py", "line": 3, "source": self.canaries[3]}])
        data = self.export()[-1]["data"]
        self.assertEqual(data["error_type"], "Exception")
        self.assertEqual(data["frames"], [{"file": "nyaatriggers/tts.py", "line": 3}])

    def test_drop_records_capture_only_known_site_count_and_error_type(self):
        diag.record_drop("Player Privacy", "ValueError access-token-canary")
        self.assertFalse(self.log.exists())
        diag.record_drop("dispatch", "ValueError chat-canary-secret on Player Privacy")
        self.assertEqual(self.export()[-1]["data"], {
            "site": "dispatch", "count": 1, "error_type": "ValueError"})

    def test_java_records_require_bundled_classes_and_declared_members(self):
        cls = "gg.xp.xivsupport.triggers.ultimate.DMU"
        with patch.object(diag, "_java_classes", return_value=frozenset([cls])), \
                patch.object(diag, "_java_members", return_value=(frozenset(["ttSq"]), frozenset(["handle"]))):
            safe = diag.record_engine({"t": "diagnostic", "event": "sequence_failed",
                "trigger_class": cls, "trigger_field": "ttSq", "wait_kind": "burst", "wait_ms": 100,
                "error_type": "java.util.NoSuchElementException", "message": self.canaries[3],
                "frames": [{"class": cls, "method": "handle", "line": 577},
                           {"class": cls, "method": "private_login", "line": 1},
                           {"class": "gg.xp.custom.PrivacyComputer", "method": "run", "line": 1}]}, gen=3)
            self.assertEqual(safe["trigger_field"], "ttSq")
            self.assertEqual(safe["frames"], [{"class": cls, "method": "handle", "line": 577}])
            diag.record_engine({"event": "engine_callout", "trigger_class": cls,
                                "trigger_field": "private_login", "seq": 42}, gen=3)
            entries = self.export()
        self.assertNotIn("trigger_field", entries[-1]["data"])
        self.assertIsNone(diag.record_engine({"event": "PrivateComputer", "message": self.canaries[4]}, gen=3))

    def test_damaged_engine_class_keeps_diagnostics_available(self):
        jar = self.directory / "triggevent-core/target/triggevent-core.jar"
        jar.parent.mkdir(parents=True)
        name = "gg/xp/Test.class"
        with ZipFile(jar, "w", compression=ZIP_DEFLATED) as archive:
            archive.writestr(name, b"\xca\xfe\xba\xbe" * 100)
        with ZipFile(jar) as archive:
            member = archive.getinfo(name)
        data = bytearray(jar.read_bytes())
        start = member.header_offset + 30 + len(name.encode()) + len(member.extra)
        data[start:start + member.compress_size] = b"\xff" * member.compress_size
        jar.write_bytes(data)
        diag._java_classes.cache_clear()
        diag._java_members.cache_clear()
        self.addCleanup(diag._java_classes.cache_clear)
        self.addCleanup(diag._java_members.cache_clear)
        with patch.object(diag, "bundle_root", return_value=self.directory), \
                patch.object(diag, "source_root", return_value=self.directory):
            safe = diag.record_engine({"event": "sequence_failed", "trigger_class": "gg.xp.Test",
                                       "trigger_field": "private_login", "wait_ms": 100,
                                       "frames": [{"class": "gg.xp.Test", "method": "test", "line": 1}]}, gen=3)
            self.assertEqual(safe["wait_ms"], 100)
            self.assertNotIn("trigger_field", safe)
            self.assertEqual(safe["frames"], [])
            self.assertEqual(self.export()[-1]["event"], "sequence_failed")

    def test_mechanic_records_keep_session_tokens_but_reject_raw_actor_ids(self):
        diag.record_engine({"event": "engine_event", "event_kind": "buff_add", "game_id": 0x130C,
                            "source_actor": 4, "target_actor": 0x10031A09, "on_player": True,
                            "event_ms": -2300, "duration_ms": 7000, "stacks": 0,
                            "source_name": self.canaries[2]}, gen=2)
        data = self.export()[-1]["data"]
        self.assertEqual(data["source_actor"], 4)
        self.assertNotIn("target_actor", data)
        self.assertEqual(data["event_ms"], -2300)
        self.assertEqual(data["game_id"], 0x130C)

    def test_counter_dictionary_and_frames_are_bounded(self):
        diag.record_engine({"event": "engine_pipeline", "counters": {
            "raw": 20, "buffs": 8, "callouts": True, "username": self.canaries[1]}}, gen=2)
        self.assertEqual(self.export()[-1]["data"], {"gen": 2, "counters": {"raw": 20, "buffs": 8}})
        diag.record("python_exception", frames=[{"file": "nyaatriggers/tts.py", "line": 1}] * 100)
        self.assertEqual(len(self.export()[-1]["data"]["frames"]), 32)

    def test_settings_action_exports_diagnostics_without_copying_combat_capture(self):
        from nyaatriggers.ui.settings_tab import SettingsTabMixin
        from nyaatriggers import app_common as ac
        from PyQt6.QtWidgets import QDialog

        destination = self.directory / "support.jsonl"
        dialog = Mock()
        dialog.exec.return_value = QDialog.DialogCode.Accepted
        dialog.selectedFiles.return_value = [str(destination)]
        host = Mock()
        host._settings = {"auto_connect": True, "char_name": self.canaries[2]}
        host._raw_capture = ["00|private_login|Player Privacy|chat-canary-secret"]
        diag.record("ws_state", state="connected")
        with patch.object(ac, "QFileDialog", return_value=dialog), \
                patch.object(ac.QMessageBox, "information") as success, \
                patch.object(ac.QMessageBox, "critical") as failure:
            SettingsTabMixin._save_diagnostics(host)
        success.assert_called_once()
        failure.assert_not_called()
        for secret in self.canaries:
            self.assertNotIn(secret, destination.read_text())
        self.assertIn('"state":"connected"', destination.read_text())


if __name__ == "__main__":
    unittest.main()
