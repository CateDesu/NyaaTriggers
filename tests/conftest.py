# Run all suites with python3 -m tests. Restrict pytest collection because other suites
# execute checks on import and need separate processes.
import glob
import os

_KEEP = {"test_regressions.py", "test_updater_windows.py", "test_download_deadline.py"}

collect_ignore = [
    os.path.basename(p)
    for p in glob.glob(os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_*.py"))
    if os.path.basename(p) not in _KEEP
]
