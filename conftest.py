# The repo's test files are standalone scripts. CI runs each one directly,
# python3 test_*.py in .github/workflows/release.yml, and a failure exits
# nonzero at import time. Only the files in _KEEP carry pytest test
# functions. Collecting the rest under pytest reruns every suite during
# collection and turns any failing check into an INTERNALERROR, so ignore
# them here. Run them directly like CI does.
import glob
import os

_KEEP = {"test_regressions.py", "test_updater_windows.py", "test_download_deadline.py"}

collect_ignore = [
    os.path.basename(p)
    for p in glob.glob(os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_*.py"))
    if os.path.basename(p) not in _KEEP
]

# The vendored engine checkout and its maven output are a full java tree.
# Walking it finds no tests and bare pytest segfaults on the deep recursion
# under python 3.14, so keep collection out of it.
collect_ignore += ["triggevent-core"]
