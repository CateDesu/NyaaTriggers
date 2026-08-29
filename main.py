#!/usr/bin/env python3
"""NyaaTriggers entry point. Bootstrap, first-run setup, and app launch."""
import os
import sys

# Keep the app on one tested windowing path, XWayland, rather than letting Qt
# pick the session default. That would put QtWebEngine, the headless cactbot
# reader, on a native Wayland surface it has never been exercised against.
# Must be set BEFORE any Qt import. setdefault respects a user override. Linux
# only. The xcb plugin does not exist in Windows Qt builds and forcing it
# aborts QApplication.
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

import drop_log

_LOG_FILE = (Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).parent) / "nyaatriggers.log"


def _owner_only(path, flags):
    # New logs are created owner-only, 0600 survives a 022 umask. A plain
    # open in append mode would inherit the umask and leave fight/callout
    # details world-readable on shared hosts. Same idiom as dps_store.
    return os.open(path, flags, 0o600)


def _log_crash(exc_type, exc_value, exc_tb) -> None:
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        # drop_log.log_crash writes under the same lock and size cap as the
        # DROP entries, so a crash write can never race a truncation.
        drop_log.log_crash(
            f"\n{'='*60}\n"
            f"CRASH  {timestamp}\n"
            + ''.join(traceback.format_exception(exc_type, exc_value, exc_tb)))
    except OSError:
        pass
    sys.__excepthook__(exc_type, exc_value, exc_tb)

sys.excepthook = _log_crash


def _thread_crash(args) -> None:
    # Worker threads bypass sys.excepthook. Without this their crashes are
    # invisible in a frozen build, no console.
    if args.exc_type is SystemExit:
        return
    _log_crash(args.exc_type, args.exc_value, args.exc_traceback)

threading.excepthook = _thread_crash


def _maybe_finish_windows_update() -> bool:
    """When relaunched as the staged copy with --apply-update, perform the
    Windows swap, relaunch the installed exe, and return True so the caller
    exits without starting a GUI. No-op on a normal launch. Runs above the Qt
    import block, see below, so it must stay stdlib-only."""
    if "--apply-update" not in sys.argv:
        return False
    # Guard the ENTIRE body, import plus arg parsing plus swap. This mode must
    # never start a GUI or surface a traceback. Worst case the swap is skipped
    # and the installed app stays put.
    dest = None
    try:
        import updater
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
    except Exception:  # noqa: BLE001 - the updater must never crash visibly
        # Log into the install dir when known. _LOG_FILE points inside the
        # staging copy here, which gets swept.
        try:
            log = (Path(dest) / _LOG_FILE.name) if dest else _LOG_FILE
            with open(log, "a", encoding="utf-8", opener=_owner_only) as f:
                f.write(f"\nAPPLY-UPDATE FAILED  "
                        f"{datetime.now():%Y-%m-%d %H:%M:%S}\n")
                f.write(traceback.format_exc())
            # os.open's mode only applies at creation. Tighten a pre-existing log.
            os.chmod(log, 0o600)
        except OSError:
            pass
    return True


# The staged Windows updater runs out of the freshly extracted staging tree, so
# it must do its swap before ANY Qt import. If the new build's Qt is broken,
# this is the one path that can still roll it back. Stdlib alone, never raises.
if "--apply-update" in sys.argv:
    _maybe_finish_windows_update()
    sys.exit(0)

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
from theme import STYLESHEET

_FFXIV_VENV = Path.home() / ".venv" / "ffxiv"
# Bundled data lives in _MEIPASS when frozen, else alongside this file.
# __file__ does not point at the bundled data dir in a frozen onedir app.
_BUNDLE_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
_VOICES_DIR = _BUNDLE_DIR / "voices"
_VOICE_STEM  = "en_US-arctic-medium"
_VOICE_FILE  = _VOICES_DIR / f"{_VOICE_STEM}.onnx"
_VOICE_CONFIG = _VOICES_DIR / f"{_VOICE_STEM}.onnx.json"
_VOICE_BASE  = (
    "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0"
    "/en/en_US/arctic/medium"
)


def _voice_present() -> bool:
    # PiperVoice.load needs BOTH files. Checking only the model would skip
    # setup after a run where the config download failed, leaving TTS
    # silently broken with no repair path. install.py checks both too.
    return _VOICE_FILE.exists() and _VOICE_CONFIG.exists()


def _piper_installed() -> bool:
    # Frozen builds bundle piper, so treat it as present. Checking the venv there
    # would trigger a fake "setup" that runs sys.executable. When frozen that's
    # the app exe itself, so it would relaunch the setup dialog in a fork bomb.
    if getattr(sys, "frozen", False):
        return True
    sp_paths  = glob.glob(str(_FFXIV_VENV / "lib" / "python*" / "site-packages"))
    sp_paths += glob.glob(str(_FFXIV_VENV / "Lib" / "site-packages"))
    return any((Path(p) / "piper").is_dir() for p in sp_paths)


def _needs_setup() -> bool:
    return not _voice_present() or not _piper_installed()


# Hard ceiling on one download. The voice model is ~77 MB, so a stream running
# past 1 GiB has a lying Content-Length or no end at all and would otherwise be
# written until the disk fills. Same value install.py uses for the same files.
_MAX_DOWNLOAD_BYTES = 1 << 30


def _download(url: str, dest: Path, timeout: int = 30,
              progress: "list[int] | None" = None) -> None:
    """Download url -> dest with a per-read timeout. Writes to a unique .part
    so an abandoned attempt can never interleave writes with a retry, verifies
    Content-Length, renames on success. A clean early connection close is a
    short read with no exception, so the length check matters. `progress[0]`
    accumulates received bytes so a supervisor can tell a stall from a slow link."""
    req = urllib.request.Request(url, headers={"User-Agent": "NyaaTriggers"})
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(
        dest.suffix + f".{os.getpid()}.{threading.get_ident()}.part")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            # A junk length from a proxy reads as unknown. The byte cap
            # below still bounds the download.
            try:
                total = int(resp.headers.get("Content-Length", 0) or 0)
            except ValueError:
                total = 0
            downloaded = 0
            with part.open("wb") as f:
                block = 65536
                while True:
                    chunk = resp.read(block)
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded += len(chunk)
                    # The per-read timeout can't stop a lying Content-Length
                    # or an endless trickle. Cap the total like install.py and
                    # updater.download do for the same class of file.
                    if downloaded > _MAX_DOWNLOAD_BYTES:
                        raise OSError(
                            f"download over {_MAX_DOWNLOAD_BYTES} bytes: {url}")
                    if progress is not None:
                        progress[0] += len(chunk)
        if total and downloaded < total:
            raise OSError(
                f"Download incomplete: received {downloaded} of {total} bytes")
        part.replace(dest)
    except BaseException:
        try:
            part.unlink()
        except OSError:
            pass
        raise


class _SetupWorker(QThread):
    progress = pyqtSignal(int, str)   # percent, -1 means indeterminate, plus status message
    # Named `done`, not `finished`. Redeclaring `finished` shadows QThread's
    # builtin finished signal and silently breaks deleteLater/cleanup hooks.
    done = pyqtSignal(bool, str)      # success, error message

    def _fill_to(self, target: int, msg: str) -> None:
        """Quickly animate the bar from its current position up to target."""
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
                    # Sweep any .part left by a previously abandoned attempt,
                    # a timed-out setup whose daemon thread was killed by
                    # sys.exit before its own cleanup ran. The unique per-run
                    # names never collide with this in-flight download. The
                    # age guard leaves a concurrent second instance's download
                    # alone, same as install.py.
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

            # 0→29%, litterbox
            for v in range(0, 30):
                self.progress.emit(v, "Staging the litterbox...")
                time.sleep(t_litter)
            self._cur = 29

            # 30→64%, couch, then hang until download finishes
            for v in range(30, 65):
                self.progress.emit(v, "Cat-proofing the couch...")
                time.sleep(t_couch)
            self._cur = 64

            # Wait on progress, not a flat ceiling. A slow link is fine as long
            # as bytes keep flowing. The old flat 300 s ceiling failed
            # legitimate downloads below ~2.2 Mbps. Only a genuine stall, no
            # new bytes for 60 s, fails. The per-read socket timeout inside
            # _download catches lower-level hangs.
            # A total deadline still applies on top of the stall check. Kokoro
            # allows 30 minutes for the same class of download. 60 here is
            # headroom for the slow connection case above.
            dl_deadline = time.monotonic() + 3600
            # Seed from current progress so the first 60 s window can already
            # detect a stall instead of always passing on the initial -1.
            last_seen = dl_progress[0]
            while not dl_event.wait(timeout=60):
                if dl_progress[0] == last_seen:
                    raise RuntimeError("Download timed out.")
                if time.monotonic() > dl_deadline:
                    raise RuntimeError("Download timed out after 60 minutes.")
                last_seen = dl_progress[0]
            if dl_error[0]:
                raise dl_error[0]

            # Source installs only. Never spawn sys.executable in a frozen build.
            # It is the app exe, not python, and would fork-bomb the setup dialog.
            if needs_piper and not frozen:
                self.progress.emit(-1, "Installing piper-tts - this can take a few minutes...")
                pip = _FFXIV_VENV / (
                    "Scripts" if platform.system() == "Windows" else "bin"
                ) / ("pip.exe" if platform.system() == "Windows" else "pip")
                # Two app instances in first run setup at once both pass the
                # pip gate and then build the same venv concurrently, which
                # can corrupt it. Serialize on the cross process lock from
                # install.py. A waiter that cannot take it raises and the
                # dialog shows the message with a Retry button.
                with install.setup_lock():
                    # A killed venv create leaves the directory behind with no pip
                    # inside. Gate on pip itself so a partial venv gets recreated.
                    if not pip.exists():
                        # utf-8 like the pip call in tts.py. Under a C locale
                        # codec a non-ASCII path raises UnicodeDecodeError and
                        # the dialog shows codec noise instead of pip's error.
                        subprocess.run(
                            [sys.executable, "-m", "venv", str(_FFXIV_VENV)],
                            check=True, capture_output=True, text=True, timeout=120,
                            encoding="utf-8", errors="replace",
                        )
                    # Pinned like requirements.txt for reproducible source runs. Bump on purpose.
                    subprocess.run(
                        [str(pip), "install", "--upgrade", "--no-input", "piper-tts==1.4.2"],
                        check=True, capture_output=True, text=True, timeout=600,
                        encoding="utf-8", errors="replace",
                    )

            # 65→90%, pipe cat ~3s
            for v in range(65, 91):
                self.progress.emit(v, "Making sure no cats are stuck in the pipes...")
                time.sleep(0.073)
            self._cur = 90

            # 90→100%, quick sweep
            self._fill_to(100, "Setup complete.")
            self.done.emit(True, "")
        except subprocess.CalledProcessError as e:
            # str of the exception is only the command + exit code. The useful part is stderr.
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

        # True while the worker thread runs. Closing the dialog in that window
        # destroys a live QThread, which is a Qt fatal: the process aborts with
        # a core dump instead of exiting. reject and closeEvent honor this.
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
            self._bar.setRange(0, 0)   # indeterminate / pulsing
        else:
            if self._bar.maximum() == 0:
                self._bar.setRange(0, 100)
            self._bar.setValue(pct)
        self._label.setText(msg)

    def _on_finished(self, ok: bool, err: str) -> None:
        # done fires at the tail of run, so this wait is bounded. The thread
        # must be fully finished before the dialog can be destroyed.
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
    # --apply-update is handled at module scope, above the Qt import block.

    app = QApplication(sys.argv)
    app.setApplicationName("NyaaTriggers")
    # Bundled display font for the sidebar, brand block plus nav pills. Best-effort.
    # A missing file just falls back to the system UI font.
    from PyQt6.QtGui import QFontDatabase
    _bundle = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
    _font = _bundle / "fonts" / "KosugiMaru-Regular.ttf"
    if _font.is_file():
        QFontDatabase.addApplicationFont(str(_font))
    app.setStyleSheet(STYLESHEET)

    if _needs_setup():
        dlg = _SetupDialog()
        if dlg.exec() != QDialog.DialogCode.Accepted:
            sys.exit(0)

    # Lazy import so tts.py's venv injection runs after setup completes.
    from main_window import MainWindow
    window = MainWindow()
    # Signal a good boot only after setup and the main window both succeed.
    import updater
    updater.mark_boot_ok()
    # Sweep update leftovers only now. During window construction the Windows
    # boot verify rollback may still need the *.nyaa-old backups, so a sweep
    # inside MainWindow could leave a dead build with nothing to restore.
    try:
        updater.cleanup_old_backups()
    except Exception as exc:  # noqa: BLE001
        drop_log.log_drop("backup-sweep", f"cleanup failed: {exc!r}")
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
