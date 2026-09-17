#!/usr/bin/env python3
"""Program startup and first run setup."""
import os
import sys

# Default to XWayland on Linux because the cactbot WebEngine runs on that path. Set
# before importing Qt and allow an explicit user override.
if sys.platform == "linux":
    os.environ.setdefault("QT_QPA_PLATFORM", "xcb")

import glob
import platform
import subprocess
import threading
import time
import traceback
import urllib.request
from datetime import datetime
from pathlib import Path

from nyaatriggers import drop_log
from nyaatriggers.paths import bundle_root, data_root, default_voice_dir

_LOG_FILE = data_root() / "nyaatriggers.log"


def _owner_only(path, flags):
    # Create logs with owner access only, independently of the process umask.
    return os.open(path, flags, 0o600)


def _log_crash(exc_type, exc_value, exc_tb) -> None:
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        # Use the drop log lock and size limit for crash entries too.
        drop_log.log_crash(
            f"\n{'='*60}\n"
            f"CRASH  {timestamp}\n"
            + ''.join(traceback.format_exception(exc_type, exc_value, exc_tb)))
    except OSError:
        pass
    sys.__excepthook__(exc_type, exc_value, exc_tb)

sys.excepthook = _log_crash


def _thread_crash(args) -> None:
    # Record worker exceptions because frozen builds have no console.
    if args.exc_type is SystemExit:
        return
    _log_crash(args.exc_type, args.exc_value, args.exc_traceback)

threading.excepthook = _thread_crash


def _maybe_finish_windows_update() -> bool:
    """Apply a staged Windows update and relaunch the installed executable. Return true to
    exit without starting Qt. This path must use only the standard library.
    """
    if "--apply-update" not in sys.argv:
        return False
    # Contain failures across imports, argument parsing and the swap so update mode
    # cannot open the GUI.
    dest = None
    try:
        from nyaatriggers import updater
        argv = sys.argv

        def _opt(flag: str):
            i = argv.index(flag) if flag in argv else -1
            return argv[i + 1] if 0 <= i < len(argv) - 1 else None

        dest, staging = _opt("--dest"), _opt("--staging")
        exe_name = _opt("--exe-name") or Path(sys.executable).name
        try:
            pid = int(_opt("--pid") or 0)
        except ValueError:
            pid = 0
        if dest and staging:
            updater.finish_windows_update(Path(dest), Path(staging), pid, exe_name)
    except Exception:  # noqa: BLE001
        # Write errors to the install directory because the staging directory will be
        # removed.
        try:
            log = (Path(dest) / _LOG_FILE.name) if dest else _LOG_FILE
            with open(log, "a", encoding="utf-8", opener=_owner_only) as f:
                f.write(f"\nAPPLY-UPDATE FAILED  "
                        f"{datetime.now():%Y-%m-%d %H:%M:%S}\n")
                f.write(traceback.format_exc())
            # Apply owner permissions to existing logs as well as new ones.
            os.chmod(log, 0o600)
        except OSError:
            pass
    return True


# Apply updates before importing Qt so a broken staged Qt build can still be rolled
# back.
if "--apply-update" in sys.argv:
    _maybe_finish_windows_update()
    sys.exit(0)

from nyaatriggers.http_fetch import configure_ssl_trust

configure_ssl_trust()

try:
    from PyQt6.QtCore import QThread, pyqtSignal
    from PyQt6.QtWidgets import (
        QApplication, QDialog, QLabel, QProgressBar, QPushButton, QVBoxLayout,
    )
except ImportError:
    print("PyQt6 is required. Install it with:")
    print("  Linux (pacman): sudo pacman -S python-pyqt6")
    print("  Linux (apt):    sudo apt install python3-pyqt6")
    sys.exit(1)

import install
from nyaatriggers.theme import STYLESHEET

_FFXIV_VENV = Path.home() / ".venv" / "ffxiv"
_BUNDLE_DIR = bundle_root()
_VOICES_DIR = default_voice_dir()
_VOICE_STEM  = "en_US-arctic-medium"
_VOICE_FILE  = _VOICES_DIR / f"{_VOICE_STEM}.onnx"
_VOICE_CONFIG = _VOICES_DIR / f"{_VOICE_STEM}.onnx.json"
_VOICE_BASE  = (
    "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0"
    "/en/en_US/arctic/medium"
)


def _voice_present() -> bool:
    # Piper needs both files. Retry setup if either download is missing.
    return _VOICE_FILE.exists() and _VOICE_CONFIG.exists()


def _piper_installed() -> bool:
    # Frozen builds bundle Piper. Running their executable as Python would reopen setup
    # recursively.
    if getattr(sys, "frozen", False):
        return True
    sp_paths  = glob.glob(str(_FFXIV_VENV / "lib" / "python*" / "site-packages"))
    sp_paths += glob.glob(str(_FFXIV_VENV / "Lib" / "site-packages"))
    return any((Path(p) / "piper").is_dir() for p in sp_paths)


def _needs_setup() -> bool:
    return not _voice_present() or not _piper_installed()


# Bound downloads independently of Content-Length. Keep this limit consistent with
# install.py.
_MAX_DOWNLOAD_BYTES = 1 << 30


def _download(url: str, dest: Path, timeout: int = 30,
              progress: "list[int] | None" = None) -> None:
    """Download to a unique temporary file, verify Content-Length and rename on success.
    Enforce read timeouts and update progress with received bytes.
    """
    from nyaatriggers import updater

    last = 0

    def advanced(received: int, total: int) -> None:
        nonlocal last
        if progress is not None:
            progress[0] += received - last
        last = received

    updater.download(url, dest, progress_cb=advanced, timeout=timeout,
                     max_bytes=_MAX_DOWNLOAD_BYTES)


class _SetupWorker(QThread):
    progress = pyqtSignal(int, str)   # percent, -1 means indeterminate, plus status message
    # Keep QThread.finished available for thread cleanup.
    done = pyqtSignal(bool, str)      # success, error message

    def _fill_to(self, target: int, msg: str) -> None:
        for v in range(self._cur, target + 1, 2):
            self.progress.emit(v, msg)
            time.sleep(0.008)
        self.progress.emit(target, msg)
        self._cur = target

    def run(self) -> None:
        self._cur = 0
        try:
            needs_download = not _voice_present()
            needs_piper   = not _piper_installed()

            dl_event = threading.Event()
            dl_error: list[Exception | None] = [None]
            dl_progress: list[int] = [0]

            def _do_download() -> None:
                try:
                    _VOICES_DIR.mkdir(exist_ok=True)
                    # Remove old partial downloads from interrupted setup attempts. Keep
                    # recent files that another instance may still be writing.
                    for stale in _VOICES_DIR.glob(f"{_VOICE_STEM}.onnx*.part"):
                        try:
                            if stale.stat().st_mtime < time.time() - 3600:
                                stale.unlink()
                        except OSError:
                            pass
                    if not _VOICE_FILE.exists():
                        _download(f"{_VOICE_BASE}/{_VOICE_STEM}.onnx",
                                  _VOICE_FILE, progress=dl_progress)
                    if not _VOICE_CONFIG.exists():
                        _download(f"{_VOICE_BASE}/{_VOICE_STEM}.onnx.json",
                                  _VOICE_CONFIG, progress=dl_progress)
                except Exception as exc:
                    dl_error[0] = exc
                finally:
                    dl_event.set()

            if needs_download:
                threading.Thread(target=_do_download, daemon=True).start()
            else:
                dl_event.set()

            frozen = getattr(sys, "frozen", False)
            t_litter = 0.167 if frozen else 0.333   # ~5s frozen, ~10s source
            t_couch  = 0.200 if frozen else 0.371   # ~7s frozen, ~13s source

            for v in range(0, 30):
                self.progress.emit(v, "Staging the litterbox...")
                time.sleep(t_litter)
            self._cur = 29

            for v in range(30, 65):
                self.progress.emit(v, "Cat-proofing the couch...")
                time.sleep(t_couch)
            self._cur = 64

            # Wait for download cleanup before allowing Retry to start another attempt.
            dl_event.wait()
            if dl_error[0]:
                raise dl_error[0]

            # Only source installs may run this executable as Python.
            if needs_piper and not frozen:
                self.progress.emit(-1, "Installing piper-tts - this can take a few minutes...")
                pip = _FFXIV_VENV / (
                    "Scripts" if platform.system() == "Windows" else "bin"
                ) / ("pip.exe" if platform.system() == "Windows" else "pip")
                # Serialize environment setup across processes to prevent concurrent
                # venv creation.
                with install.setup_lock():
                    # Check pip because interrupted environment creation may leave an
                    # incomplete directory.
                    if not pip.exists():
                        # Decode subprocess output as UTF-8 so non-ASCII paths remain
                        # readable under a C locale.
                        subprocess.run(
                            [sys.executable, "-m", "venv", str(_FFXIV_VENV)],
                            check=True, capture_output=True, text=True, timeout=120,
                            encoding="utf-8", errors="replace",
                        )
                    subprocess.run(
                        [str(pip), "install", "--upgrade", "--no-input", "piper-tts==1.4.2"],
                        check=True, capture_output=True, text=True, timeout=600,
                        encoding="utf-8", errors="replace",
                    )

            for v in range(65, 91):
                self.progress.emit(v, "Making sure no cats are stuck in the pipes...")
                time.sleep(0.073)
            self._cur = 90

            self._fill_to(100, "Setup complete.")
            self.done.emit(True, "")
        except subprocess.CalledProcessError as e:
            # Include command output because the exception alone contains only its exit
            # status.
            detail = ((e.stderr or "") + (e.stdout or "")).strip()
            self.done.emit(False, str(e) + (f"\n{detail[:500]}" if detail else ""))
        except Exception as e:
            self.done.emit(False, str(e))


class _SetupDialog(QDialog):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("NyaaTriggers - First Run Setup")
        self.setFixedWidth(440)
        self.setModal(True)

        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(20, 20, 20, 20)

        self._label = QLabel(
            "Setting up NyaaTriggers for the first time.\n"
            "This only happens once and takes about a minute."
        )
        self._label.setWordWrap(True)
        layout.addWidget(self._label)

        self._bar = QProgressBar()
        self._bar.setRange(0, 100)
        layout.addWidget(self._bar)

        self._retry_btn = QPushButton("Retry")
        self._retry_btn.setVisible(False)
        self._retry_btn.clicked.connect(self._on_retry)
        layout.addWidget(self._retry_btn)

        self._close_btn = QPushButton("Close")
        self._close_btn.setVisible(False)
        self._close_btn.clicked.connect(self.reject)
        layout.addWidget(self._close_btn)

        # Block dialog closure while its QThread is running to avoid a Qt abort.
        self._running = False
        self._start_worker()

    def _start_worker(self) -> None:
        self._worker = _SetupWorker()
        self._worker.progress.connect(self._on_progress)
        self._worker.done.connect(self._on_finished)
        self._running = True
        self._worker.start()

    def reject(self) -> None:
        if self._running:
            return
        super().reject()

    def closeEvent(self, event) -> None:
        if self._running:
            event.ignore()
            return
        super().closeEvent(event)

    def _on_retry(self) -> None:
        self._retry_btn.setVisible(False)
        self._close_btn.setVisible(False)
        self._bar.setVisible(True)
        self._bar.setRange(0, 100)
        self._bar.setValue(0)
        self._label.setText("Retrying setup...")
        self._start_worker()

    def _on_progress(self, pct: int, msg: str) -> None:
        if pct == -1:
            self._bar.setRange(0, 0)
        else:
            if self._bar.maximum() == 0:
                self._bar.setRange(0, 100)
            self._bar.setValue(pct)
        self._label.setText(msg)

    def _on_finished(self, ok: bool, err: str) -> None:
        # Wait for run to return before allowing the dialog to be destroyed.
        self._worker.wait()
        self._running = False
        if ok:
            self.accept()
        else:
            self._label.setText(
                f"Setup failed:\n{err}\n\n"
                "You can run  python install.py  manually and then relaunch."
            )
            self._bar.setVisible(False)
            self._retry_btn.setVisible(True)
            self._close_btn.setVisible(True)


def main() -> None:

    app = QApplication(sys.argv)
    app.setApplicationName("NyaaTriggers")
    # Load the bundled font, falling back to the system font if unavailable.
    from PyQt6.QtGui import QFontDatabase
    _bundle = bundle_root()
    _font = _bundle / "fonts" / "KosugiMaru-Regular.ttf"
    if _font.is_file():
        QFontDatabase.addApplicationFont(str(_font))
    app.setStyleSheet(STYLESHEET)

    if _needs_setup():
        dlg = _SetupDialog()
        if dlg.exec() != QDialog.DialogCode.Accepted:
            sys.exit(0)

    # Delay the TTS import until setup has installed its dependencies.
    from nyaatriggers.main_window import MainWindow
    window = MainWindow()
    # Signal a good boot only after setup and the main window both succeed.
    from nyaatriggers import updater
    updater.mark_boot_ok()
    # Keep Windows rollback backups until main window construction and boot verification
    # succeed.
    try:
        updater.cleanup_old_backups()
    except Exception as exc:  # noqa: BLE001
        drop_log.log_drop("backup-sweep", f"cleanup failed: {exc!r}")
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
