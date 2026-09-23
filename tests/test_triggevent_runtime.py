"""Rebuilding a jar must not break classes loaded later in a pull."""

from contextlib import ExitStack
from pathlib import Path
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import zipfile

from nyaatriggers import triggevent_bridge as bridge_module


class TriggeventRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.jar = self.root / "target" / "engine.jar"
        self.jar.parent.mkdir()
        self.jar.write_bytes(b"complete engine")
        for name, value in (("_find_java", "java"), ("_find_jar", self.jar), ("_bundled_jre_dir", None)):
            self.stack.enter_context(patch.object(bridge_module, name, return_value=value))
        self.stack.enter_context(patch.object(bridge_module.shutil, "which", return_value=None))
        self.stack.enter_context(patch.object(bridge_module, "_log"))
        self.stack.enter_context(patch.object(bridge_module.threading, "Thread"))
        self.bridge = bridge_module.TriggeventBridge()

    def test_running_process_keeps_its_jar_until_reaped(self):
        proc = SimpleNamespace(pid=12345)
        proc.wait = Mock()
        with patch.object(bridge_module.subprocess, "Popen", return_value=proc) as spawn:
            self.bridge.start()
        command = spawn.call_args.args[0]
        runtime = Path(command[command.index("-jar") + 1])
        self.assertNotEqual(runtime, self.jar)
        self.assertEqual(spawn.call_args.kwargs["cwd"], str(self.root))
        self.jar.write_bytes(b"intermediate build")
        self.assertEqual(runtime.read_bytes(), b"complete engine")
        proc.wait.side_effect = lambda **kwargs: self.assertTrue(runtime.exists())
        with patch.object(self.bridge, "_signal_group"), \
                patch.object(bridge_module.os, "killpg", side_effect=ProcessLookupError):
            self.bridge.stop(wait=True)
        self.assertFalse(runtime.parent.exists())
        self.assertEqual(self.jar.read_bytes(), b"intermediate build")

    def test_failed_launch_removes_the_temporary_copy(self):
        with patch.object(bridge_module.subprocess, "Popen", side_effect=OSError("launch failed")) as spawn:
            self.bridge.start()
        command = spawn.call_args.args[0]
        runtime = Path(command[command.index("-jar") + 1])
        self.assertFalse(runtime.parent.exists())
        self.assertFalse(self.bridge.is_active())
        self.assertIsNone(self.bridge._proc)

    def test_copy_failure_does_not_launch_or_leave_a_partial_jar(self):
        directories = []
        create = tempfile.TemporaryDirectory

        def directory(**kwargs):
            result = create(**kwargs)
            directories.append(Path(result.name))
            return result

        def fail_copy(source, destination):
            Path(destination).write_bytes(b"partial")
            raise OSError("disk full")

        with patch.object(bridge_module.tempfile, "TemporaryDirectory", side_effect=directory), \
                patch.object(bridge_module.shutil, "copyfile", side_effect=fail_copy), \
                patch.object(bridge_module.subprocess, "Popen") as spawn:
            self.bridge.start()
        spawn.assert_not_called()
        self.assertFalse(self.bridge.is_active())
        self.assertTrue(directories)
        self.assertTrue(all(not path.exists() for path in directories))

    @unittest.skipUnless(shutil.which("java") and shutil.which("javac"), "Requires a JDK")
    def test_java_loads_a_later_callout_after_the_source_jar_is_overwritten(self):
        source = self.root / "Probe.java"
        source.write_text('''
import java.io.BufferedReader;
import java.io.InputStreamReader;
public class Probe {
    public static void main(String[] args) throws Exception {
        System.out.println("READY");
        new BufferedReader(new InputStreamReader(System.in)).readLine();
        System.out.println(Class.forName("LaterCallout").getMethod("text").invoke(null));
    }
}
class LaterCallout {
    public static String text() { return "Motion and Stack"; }
}
''')
        subprocess.run(["javac", str(source)], check=True, capture_output=True, timeout=30)
        with zipfile.ZipFile(self.jar, "w") as jar:
            for path in self.root.glob("*.class"):
                jar.write(path, path.name)
        directory, runtime = bridge_module._snapshot_jar(self.jar)
        self.addCleanup(directory.cleanup)
        proc = subprocess.Popen(["java", "-cp", str(runtime), "Probe"], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual(proc.stdout.readline().strip(), "READY")
            self.jar.write_bytes(b"intermediate build")
            stdout, stderr = proc.communicate("continue\n", timeout=10)
            self.assertEqual(proc.returncode, 0, stderr)
            self.assertEqual(stdout.strip(), "Motion and Stack")
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.communicate()


if __name__ == "__main__":
    unittest.main()
