# Some suites execute on import and need isolated processes. Run python3 -m tests.
import glob
import os

_KEEP = {"test_regressions.py", "test_updater_windows.py", "test_download_deadline.py"}

collect_ignore = [
    os.path.basename(p)
    for p in glob.glob(os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_*.py"))
    if os.path.basename(p) not in _KEEP
]
