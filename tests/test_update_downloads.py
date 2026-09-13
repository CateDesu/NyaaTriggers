"""Update archives belong to a private directory for each install attempt."""

from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

from nyaatriggers import updater
from nyaatriggers.ui import voice_tab


class UpdateDownloadTests(unittest.TestCase):
    def test_each_attempt_uses_a_private_directory_and_cleans_it(self):
        for kind, asset, apply_name in (
                ("frozen-linux", updater.LINUX_ASSET, "apply_frozen_linux"),
                ("frozen-windows", updater.WINDOWS_ASSET, "apply_frozen_windows")):
            with self.subTest(kind=kind), ExitStack() as stack:
                root = Path(stack.enter_context(tempfile.TemporaryDirectory()))
                shared = root / asset
                shared.write_bytes(b"someone else's archive")
                stack.enter_context(patch.object(voice_tab.tempfile, "gettempdir", return_value=str(root)))
                stack.enter_context(patch.object(updater, "asset_for_platform", return_value="https://example.test/archive"))
                stack.enter_context(patch.object(voice_tab, "_sweep_stale_update_parts"))
                thread = stack.enter_context(patch.object(voice_tab.threading, "Thread"))
                thread.side_effect = lambda *, target, **kw: SimpleNamespace(start=target)
                paths = []
                fail = None

                def download(url, dest, progress_cb):
                    self.assertNotEqual(dest.parent, root)
                    self.assertEqual(dest.name, asset)
                    if voice_tab.os.name == "posix":
                        self.assertEqual(dest.parent.stat().st_mode & 0o777, 0o700)
                    paths.append(dest)
                    dest.write_bytes(b"new archive")
                    if fail == "download":
                        raise OSError("download interrupted")

                def verify(rel, name, dest):
                    self.assertEqual(dest.read_bytes(), b"new archive")
                    return fail != "verify", "verification result"

                stack.enter_context(patch.object(updater, "download", side_effect=download))
                stack.enter_context(patch.object(updater, "verify_release_asset", side_effect=verify))
                apply = stack.enter_context(patch.object(updater, apply_name))
                window = SimpleNamespace(_upd_progress_signal=Mock(), _upd_done_signal=Mock())
                rel = updater.Release("v9.9.9", "9.9.9", "")
                for fail in (None, "download", "verify", "apply"):
                    with self.subTest(failure=fail):
                        apply.reset_mock()
                        apply.return_value = (fail != "apply", "__windows_handoff__" if kind == "frozen-windows" else "installed")
                        voice_tab.VoiceTabMixin._start_install(window, rel, kind)
                        self.assertFalse(paths[-1].parent.exists())
                        self.assertEqual(shared.read_bytes(), b"someone else's archive")
                        self.assertEqual(window._upd_done_signal.emit.call_args.args[0], fail is None)
                        self.assertEqual(apply.call_count, 0 if fail in ("download", "verify") else 1)
                self.assertEqual(len({path.parent for path in paths}), 4)


if __name__ == "__main__":
    unittest.main()
