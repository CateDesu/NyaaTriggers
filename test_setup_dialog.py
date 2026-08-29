"""Regression test for the first-run setup dialog lifecycle.

Closing the dialog while the setup worker runs used to let main() unwind and
destroy a live QThread, which is a Qt fatal: the process aborted with a core
dump instead of exiting. The dialog must refuse to close until the worker's
done signal lands, then close normally.

Run directly:  python test_setup_dialog.py   (exit 0 = all pass)
"""
import os
import sys
import threading
import contextlib
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PyQt6.QtCore import QThread, pyqtSignal
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QDialog

import main

FAILS = []


def check(name, cond):
    print(("PASS  " if cond else "FAIL  ") + name)
    if not cond:
        FAILS.append(name)


class _FakeWorker(QThread):
    """Stands in for _SetupWorker: blocks until told, then emits done."""
    progress = pyqtSignal(int, str)
    done = pyqtSignal(bool, str)

    def __init__(self):
        super().__init__()
        self.release = threading.Event()
        self.result = (False, "boom")

    def run(self):
        self.release.wait(10)
        self.done.emit(*self.result)


_last_worker = None


def _make_worker():
    global _last_worker
    _last_worker = _FakeWorker()
    return _last_worker


# ── the first run venv create and pip install decode output as utf-8 ──────
# Under a C locale codec a non-ASCII path raises UnicodeDecodeError with
# plain text=True, and the dialog would show codec noise instead of pip's
# error. Same environment class the tts.py pip call was hardened for. The
# sleeps and the lock are stubbed so the worker body runs synchronously.
_setup_app = QApplication.instance() or QApplication(sys.argv)
_pip_calls = []


class _FakeProcResult:
    def __init__(self, args):
        self.args = args
        self.returncode = 0
        self.stdout = ""
        self.stderr = ""


@contextlib.contextmanager
def _no_lock():
    yield


_saved = (main._voice_present, main._piper_installed, main._FFXIV_VENV,
          main.subprocess.run, main.time.sleep, main.install.setup_lock)
main._voice_present = lambda: True
main._piper_installed = lambda: False
main._FFXIV_VENV = Path(tempfile.mkdtemp())
main.subprocess.run = lambda *a, **k: _pip_calls.append(k) or _FakeProcResult(a)
main.time.sleep = lambda *_a, **_k: None
main.install.setup_lock = _no_lock
_worker0 = main._SetupWorker()
_done0 = []
_worker0.done.connect(lambda ok, msg: _done0.append((ok, msg)))
try:
    _worker0.run()
finally:
    (main._voice_present, main._piper_installed, main._FFXIV_VENV,
     main.subprocess.run, main.time.sleep, main.install.setup_lock) = _saved

check("first run setup ran the venv create and the pip install",
      len(_pip_calls) == 2)
check("setup subprocess calls decode utf-8 with replacement",
      all(k.get("encoding") == "utf-8" and k.get("errors") == "replace"
          for k in _pip_calls))
check("first run setup reports success", _done0 == [(True, "")])

main._SetupWorker = _make_worker
_app = QApplication.instance() or QApplication(sys.argv)

# Failure path: reject and the window X are ignored while the worker runs.
dlg = main._SetupDialog()
dlg.show()
QTest.qWait(20)
check("worker running on open", dlg._running)
dlg.reject()
check("reject ignored while worker runs",
      dlg.result() == 0 and dlg._running)
dlg.close()
check("close event ignored while worker runs", dlg.isVisible() and dlg._running)

_last_worker.result = (False, "boom")
_last_worker.release.set()
for _ in range(100):
    QTest.qWait(10)
    if not dlg._running:
        break
check("running cleared after done", not dlg._running)
check("retry shown after failure", dlg._retry_btn.isVisible())
dlg.reject()
check("reject closes after done",
      dlg.result() == QDialog.DialogCode.Rejected)

# Success path: done(True) accepts the dialog on its own.
main._SetupWorker = _make_worker
dlg2 = main._SetupDialog()
dlg2.show()
QTest.qWait(20)
_last_worker.result = (True, "")
_last_worker.release.set()
for _ in range(100):
    QTest.qWait(10)
    if dlg2.result() == QDialog.DialogCode.Accepted:
        break
check("success accepts the dialog", dlg2.result() == QDialog.DialogCode.Accepted)
check("thread reaped before accept", not dlg2._worker.isRunning())

if FAILS:
    print(f"\n{len(FAILS)} failed: {', '.join(FAILS)}")
    sys.exit(1)
print("\nall passed")
