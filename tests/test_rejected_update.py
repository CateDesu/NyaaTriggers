"""A rolled back Windows release must not offer the same self update again."""

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from contextlib import ExitStack
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from PyQt6.QtWidgets import QApplication, QVBoxLayout, QWidget

from nyaatriggers import updater, updater_ui


class UpdateWindow(QWidget, updater_ui.UpdaterUiMixin):
    def __init__(self):
        super().__init__()
        self._init_update_flow()
        self._settings = {}
        self._build_update_banner(QVBoxLayout(self))
        self._upd_available_signal = SimpleNamespace(emit=self._on_update_available)
        self._upd_checkmsg_signal = Mock()
        self._start_install = Mock()


class RejectedUpdateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.install = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(patch.object(updater, "install_dir", return_value=self.install))
        self.stack.enter_context(patch.object(updater, "install_kind", return_value="frozen-windows"))
        self.stack.enter_context(patch.object(updater, "is_frozen", return_value=True))
        self.stack.enter_context(patch.object(updater_ui, "_", side_effect=lambda text: text))
        self.stack.enter_context(patch.object(updater_ui, "_VERSION", "9.9.8"))
        self.window = UpdateWindow()
        self.addCleanup(self.window.close)
        self.release = updater.Release("v9.9.9", "9.9.9", "https://example.test/release")
        staging = self.install / "staging" / "_internal"
        staging.mkdir(parents=True)
        (staging / "nyaatriggers.version").write_text("9.9.9", encoding="utf-8")
        updater._mark_rejected(self.install, staging.parent)

    def test_only_the_exact_rejected_version_matches(self):
        for version in ("9.9.9", "v9.9.9", "V9.9.9"):
            with self.subTest(version=version):
                self.assertTrue(updater.is_rejected_update(version))
        for version in ("", "9.9.8", "9.9.10", "9.9.9.1", "9.9.9-rc1"):
            with self.subTest(version=version):
                self.assertFalse(updater.is_rejected_update(version))

    def test_missing_or_invalid_marker_keeps_updates_available(self):
        marker = self.install / updater._REJECTED_NAME
        for content in (b"", b"\xff", b"9.9.9", b"junk rejected 9.9.9",
                        b"2026-09-13 12:00:00  rejected unknown\n", b"x" * 1024):
            with self.subTest(content=content[:50]):
                marker.write_bytes(content)
                self.assertFalse(updater.is_rejected_update("9.9.9"))
        marker.unlink()
        self.assertFalse(updater.is_rejected_update("9.9.9"))
        marker.mkdir()
        self.assertFalse(updater.is_rejected_update("9.9.9"))

    def test_live_and_cached_checks_explain_rollback_and_open_download_page(self):
        for manual in (False, True):
            for cached in (False, True):
                with self.subTest(manual=manual, cached=cached), ExitStack() as stack:
                    stack.enter_context(patch.object(
                        updater, "fetch_latest_release", return_value=self.release,
                        side_effect=updater.RateLimited if cached else None))
                    stack.enter_context(patch.object(updater, "read_cached_release",
                                                    return_value=self.release))
                    thread = stack.enter_context(patch.object(updater_ui.threading, "Thread"))
                    thread.side_effect = lambda *, target, **kw: SimpleNamespace(start=target)
                    open_url = stack.enter_context(patch.object(updater_ui.QDesktopServices, "openUrl"))
                    self.window._start_update_check(manual)
                    self.assertEqual(self.window._update_action, "openpage")
                    self.assertIn("9.9.9", self.window._upd_msg.text())
                    self.assertIn("rolled back", self.window._upd_msg.text())
                    self.assertEqual(self.window._upd_install_btn.text(), "Download")
                    self.assertFalse(self.window._update_banner.isHidden())
                    self.window._upd_install_btn.click()
                    open_url.assert_called_once()
                    self.assertEqual(open_url.call_args.args[0].toString(), self.release.html_url)
                    self.window._start_install.assert_not_called()
                    self.window._upd_checkmsg_signal.emit.assert_called_with(manual, "")

    def test_new_release_restores_install_action(self):
        self.window._show_update_banner(self.release)
        self.window._show_update_banner(updater.Release("v9.9.10", "9.9.10", ""))
        self.assertEqual(self.window._update_action, "install")
        self.assertEqual(self.window._upd_install_btn.text(), "Install")

    def test_other_install_kinds_ignore_the_windows_marker(self):
        for kind, action in (("frozen-linux", "install"), ("git", "install"), ("source", "openpage")):
            with self.subTest(kind=kind), patch.object(updater, "install_kind", return_value=kind):
                self.window._show_update_banner(self.release)
                self.assertEqual(self.window._update_action, action)
                self.assertNotIn("rolled back", self.window._upd_msg.text())

    def test_applied_release_still_offers_restart(self):
        self.window._update_applied_version = self.release.version
        self.window._show_update_banner(self.release)
        self.assertEqual(self.window._update_action, "restart")


if __name__ == "__main__":
    unittest.main()
