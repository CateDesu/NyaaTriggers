"""Engine revisions must travel with the jar distributed to users."""

import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from zipfile import ZIP_DEFLATED, ZipFile

from nyaatriggers import diagnostics, triggevent_bridge


class EngineBuildInfoTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.jar = self.root / "triggevent-core/target/triggevent-core.jar"
        self.jar.parent.mkdir(parents=True)
        self.revision = "a" * 40

    def jar_with_revision(self, revision):
        with ZipFile(self.jar, "w") as archive:
            archive.writestr("META-INF/MANIFEST.MF", "Manifest-Version: 1.0\r\n"
                             + f"Nyaa-Engine-Commit: {revision}\r\n\r\n")

    def test_release_jar_needs_no_adjacent_build_stamp(self):
        self.jar_with_revision(self.revision)
        self.assertEqual(triggevent_bridge._launch_build_commit(self.jar), self.revision)
        with patch.object(diagnostics, "bundle_root", return_value=self.root):
            self.assertEqual(diagnostics._snapshot({})["data"].get("engine_commit"), self.revision)

    def test_embedded_revision_overrides_stale_checkout_stamp(self):
        self.jar_with_revision(self.revision)
        self.jar.with_name(self.jar.name + ".built-from").write_text(json.dumps({"engine": "b" * 40}))
        self.assertEqual(triggevent_bridge._launch_build_commit(self.jar), self.revision)

    def test_folded_manifest_value_is_read(self):
        self.jar_with_revision(self.revision[:20] + "\r\n " + self.revision[20:])
        self.assertEqual(triggevent_bridge._launch_build_commit(self.jar), self.revision)

    def test_private_or_oversized_manifest_value_is_omitted(self):
        for revision in ("PersonalComputer", "/home/PrivateUser", "f" * 41, "f" * 100000):
            with self.subTest(length=len(revision)):
                self.jar_with_revision(revision)
                self.assertIsNone(triggevent_bridge._launch_build_commit(self.jar))

    def test_legacy_source_build_stamp_remains_supported(self):
        with ZipFile(self.jar, "w") as archive:
            archive.writestr("META-INF/MANIFEST.MF", "Manifest-Version: 1.0\r\n\r\n")
        self.jar.with_name(self.jar.name + ".built-from").write_text(json.dumps({"engine": self.revision}))
        self.assertEqual(triggevent_bridge._launch_build_commit(self.jar), self.revision)

    def test_damaged_manifest_does_not_raise_from_diagnostic_metadata_lookup(self):
        with ZipFile(self.jar, "w", compression=ZIP_DEFLATED) as archive:
            archive.writestr("META-INF/MANIFEST.MF", "Manifest-Version: 1.0\r\n"
                             + f"Nyaa-Engine-Commit: {self.revision}\r\n\r\n")
            info = archive.getinfo("META-INF/MANIFEST.MF")
            offset = info.header_offset + 30 + len(info.filename.encode()) + len(info.extra)
        data = bytearray(self.jar.read_bytes())
        data[offset] = 255
        self.jar.write_bytes(data)
        self.assertIsNone(triggevent_bridge._launch_build_commit(self.jar))
        with patch.object(diagnostics, "bundle_root", return_value=self.root):
            self.assertNotIn("engine_commit", diagnostics._snapshot({})["data"])

    def test_launch_records_the_copied_jar_if_an_update_replaces_the_source(self):
        self.jar_with_revision(self.revision)
        bridge = triggevent_bridge.TriggeventBridge()
        snapshot = triggevent_bridge._snapshot_jar
        process = SimpleNamespace(pid=42)

        def update_then_copy(jar):
            self.jar_with_revision("b" * 40)
            return snapshot(jar)

        try:
            with patch.object(triggevent_bridge, "_find_java", return_value="java"), \
                    patch.object(triggevent_bridge, "_find_jar", return_value=self.jar), \
                    patch.object(triggevent_bridge, "_bundled_jre_dir", return_value=None), \
                    patch.object(triggevent_bridge, "_snapshot_jar", side_effect=update_then_copy), \
                    patch.object(triggevent_bridge.shutil, "which", return_value=None), \
                    patch.object(triggevent_bridge.subprocess, "Popen", return_value=process), \
                    patch.object(triggevent_bridge.threading, "Thread"), \
                    patch.object(triggevent_bridge, "_log"), \
                    patch.object(bridge, "_diagnostic") as record:
                bridge.start()
            started = next(call for call in record.call_args_list if call.args[0] == "engine_started")
            self.assertEqual(started.kwargs["engine_commit"], "b" * 40)
            self.assertEqual(triggevent_bridge._launch_build_commit(
                Path(process._nyaa_runtime_dir.name) / self.jar.name), "b" * 40)
        finally:
            if hasattr(process, "_nyaa_runtime_dir"):
                process._nyaa_runtime_dir.cleanup()


if __name__ == "__main__":
    unittest.main()
