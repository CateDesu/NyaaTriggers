"""Install a release archive, update it and require a successful program restart."""

import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from nyaatriggers import updater


def wait_for_boot(folder, process=None, previous_pid=None, timeout=60):
    deadline = time.monotonic() + timeout
    marker = folder / updater._BOOT_OK_MARKER
    while time.monotonic() < deadline:
        if process is not None and process.poll() is not None:
            raise RuntimeError(f"Program exited before startup with status {process.returncode}")
        try:
            pid = int(marker.read_text(encoding="utf-8").strip())
            if pid > 0 and pid != previous_pid and (process is None or pid == process.pid):
                return pid
        except (OSError, ValueError):
            pass
        time.sleep(.1)
    raise TimeoutError("The main window did not confirm startup")


def stop_program(pid, process=None):
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True, timeout=15)
    else:
        try:
            os.killpg(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    if process is not None:
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def check_archive(archive, version, previous_archive=None):
    with tempfile.TemporaryDirectory(prefix="nyaa packaged update ") as directory:
        root = Path(directory)
        extracted = root / "installed"
        extracted.mkdir()
        windows = os.name == "nt"
        extract = updater._safe_extract_zip if windows else updater._safe_extract_tar
        extract(previous_archive or archive, extracted)
        folder = updater._archive_app_root(extracted)
        executable = folder / ("NyaaTriggers.exe" if windows else "NyaaTriggers.sh")
        settings = {
            "auto_check_updates": False, "auto_connect": False,
            "cactbot_enabled": False, "triggevent_auto_update": False,
            "tts_engine": "none",
        }
        (folder / "nyaatriggers_settings.json").write_text(json.dumps(settings), encoding="utf-8")
        saved = folder / "update-check-user-data.txt"
        saved.write_bytes(b"saved user data\n")
        stale = folder / "_internal" / "update-check-stale.txt"
        stale.write_text("old runtime", encoding="utf-8")
        log = root / "startup.log"
        process = None
        restarted_pid = None
        handoff = []
        try:
            with log.open("w", encoding="utf-8") as output:
                environment = {**os.environ, "QT_QPA_PLATFORM": "offscreen"}

                def launch():
                    return subprocess.Popen(
                        [str(executable)], cwd=folder, env=environment,
                        stdout=output, stderr=output, start_new_session=not windows)

                process = launch()
                previous_pid = wait_for_boot(folder, process)
                print("Installed program reached the main window", flush=True)
                if windows:
                    # The running program normally calls this itself.
                    popen = subprocess.Popen

                    def start_handoff(*args, **kwargs):
                        child = popen(*args, **kwargs)
                        handoff.append(child)
                        return child

                    with patch.object(updater.os, "getpid", return_value=previous_pid), \
                            patch.object(updater.subprocess, "Popen", side_effect=start_handoff):
                        ok, detail = updater.apply_frozen_windows(
                            archive, folder, "NyaaTriggers.exe", version)
                    if not ok or detail != "__windows_handoff__":
                        raise RuntimeError(detail)
                    stop_program(previous_pid, process)
                    process = None
                    restarted_pid = wait_for_boot(folder, previous_pid=previous_pid)
                    update_log = folder / updater._UPDATE_LOG_NAME
                    deadline = time.monotonic() + 15
                    while time.monotonic() < deadline:
                        text = update_log.read_text(encoding="utf-8") if update_log.exists() else ""
                        if "update applied, keeping the new build" in text:
                            break
                        time.sleep(.1)
                    else:
                        raise RuntimeError("The Windows updater did not confirm the new build")
                    for child in handoff:
                        if child.wait(timeout=15):
                            raise RuntimeError("The staged updater failed after restarting the program")
                else:
                    ok, detail = updater.apply_frozen_linux(archive, folder, "NyaaTriggers")
                    if not ok:
                        raise RuntimeError(detail)
                    if process.poll() is not None:
                        raise RuntimeError("The running program exited during installation")
                    stop_program(previous_pid, process)
                    process = None
                    (folder / updater._BOOT_OK_MARKER).unlink(missing_ok=True)
                    process = launch()
                    restarted_pid = wait_for_boot(folder, process)
                if stale.exists():
                    raise RuntimeError("The update left the previous runtime in place")
                stamp = (folder / "_internal" / "nyaatriggers.version").read_text(encoding="utf-8")
                if stamp != version:
                    raise RuntimeError(f"Expected installed version {version}, got {stamp!r}")
                if saved.read_bytes() != b"saved user data\n":
                    raise RuntimeError("The update changed saved user data")
                if (folder / updater._REJECTED_NAME).exists():
                    raise RuntimeError("The updated build was rejected")
                print(f"PASS packaged update and restart for {version}", flush=True)
        except BaseException:
            for path in (log, folder / "nyaatriggers.log", folder / updater._UPDATE_LOG_NAME):
                if path.is_file():
                    print(f"{path.name}:\n{path.read_text(encoding='utf-8', errors='replace')[-20000:]}",
                          file=sys.stderr)
            raise
        finally:
            if process is not None:
                stop_program(process.pid, process)
            elif restarted_pid is not None:
                stop_program(restarted_pid)
            for child in handoff:
                if child.poll() is None:
                    stop_program(child.pid, child)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("--version", required=True)
    parser.add_argument("--previous-archive", type=Path)
    args = parser.parse_args()
    check_archive(args.archive.resolve(), args.version,
                  args.previous_archive.resolve() if args.previous_archive else None)
