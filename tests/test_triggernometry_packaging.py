import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from nyaatriggers import triggernometry_bridge as bridge
from nyaatriggers.triggernometry_editor import PackDocument


CORE = Path(__file__).resolve().parents[1] / "triggernometry-core"


@unittest.skipUnless(os.name == "posix" and shutil.which("mono") and shutil.which("mcs"),
                     "Requires Mono and mcs")
class PackagingTests(unittest.TestCase):
    def test_full_build_packages_a_working_validator(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for file in ("build-all.sh", "package.sh", "build-validator.sh"):
                shutil.copy2(CORE / file, root / file)
            for directory in ("host", "shims", "bin"):
                shutil.copytree(CORE / directory, root / directory)
            (root / "bin" / "triggernometry-validate.exe").unlink()
            engine = root / ".engine"
            source = engine / "Source"
            (engine / ".git").mkdir(parents=True)
            (source / "packages").mkdir(parents=True)
            engine_bin = source / "Triggernometry" / "bin" / "Release"
            shutil.copytree(CORE / "bin", engine_bin)
            (source / "Triggernometry" / "TriggernometryPlugin.csproj").write_text("<Project />")
            commands = root / "commands"
            commands.mkdir()
            for name in ("git", "xbuild", "curl"):
                command = commands / name
                command.write_text("#!/bin/sh\nexit 0\n")
                command.chmod(0o755)
            result = subprocess.run(["bash", str(root / "build-all.sh")],
                                    env={**os.environ, "PATH": str(commands) + os.pathsep + os.environ["PATH"],
                                         "ENGINE_DIR": str(engine)},
                                    capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            document = PackDocument(root / "saved.xml")
            document.add_trigger().set("RegularExpression", "^TEST$")
            with patch.object(bridge, "_find_exe", return_value=root / "bin" / "triggernometry-core.exe"):
                document.save()
            self.assertTrue(document.path.is_file())
            self.assertTrue((root / "bin" / "triggernometry-core.exe.config").is_file())


if __name__ == "__main__":
    unittest.main()
