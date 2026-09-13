# Run the full suite with python3 -m tests. Many scripts run checks during
# import and need their own process. Keep pytest collection limited to the
# existing opt-in suites so collection does not run the other scripts.
import glob
import os

_KEEP = {"test_regressions.py", "test_updater_windows.py", "test_download_deadline.py"}

collect_ignore = [
    os.path.basename(p)
    for p in glob.glob(os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_*.py"))
    if os.path.basename(p) not in _KEEP
]
