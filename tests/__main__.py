"""Run each suite in its own process to keep Qt and module patches isolated."""

import argparse
import os
from pathlib import Path
import subprocess
import sys


def main():
    directory = Path(__file__).resolve().parent
    available = sorted(path.stem for path in directory.glob("test_*.py"))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("suites", nargs="*", metavar="test_name",
                        help="Suite names without .py, or omit to run all suites")
    args = parser.parse_args()
    unknown = sorted(set(args.suites) - set(available))
    if unknown:
        parser.error("Unknown suites: " + ", ".join(unknown))
    selected = args.suites or available
    if not selected:
        parser.error("No test suites found")

    environment = os.environ.copy()
    environment.setdefault("QT_QPA_PLATFORM", "offscreen")
    github = environment.get("GITHUB_ACTIONS") == "true"
    failed = []
    for name in selected:
        print(f"::group::{name}" if github else f"Running {name}", flush=True)
        result = subprocess.run([sys.executable, "-m", f"tests.{name}"],
                                cwd=directory.parent, env=environment)
        if result.returncode:
            failed.append(name)
            print(f"::error::{name} failed" if github else f"FAILED: {name}", flush=True)
        if github:
            print("::endgroup::", flush=True)

    print(f"{len(selected) - len(failed)}/{len(selected)} suites completed without failures", flush=True)
    if failed:
        print("Failed suites: " + ", ".join(failed), flush=True)
    return int(bool(failed))


if __name__ == "__main__":
    raise SystemExit(main())
