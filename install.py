#!/usr/bin/env python3
"""Install the CC0 en_US-arctic-medium voice and piper-tts environment before launching
NyaaTriggers. Run with python install.py.
"""

import contextlib
import hashlib
import os
import platform
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path
from nyaatriggers.http_fetch import open_response

VOICES_DIR   = Path(__file__).parent / "voices"
VENV_DIR     = Path.home() / ".venv" / "ffxiv"
VOICE_STEM   = "en_US-arctic-medium"
VOICE_FILE   = VOICES_DIR / f"{VOICE_STEM}.onnx"

# Official CC0 Piper voice, matching the defaults in tts.py and main.py.
VOICE_BASE = (
    "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0"
    "/en/en_US/arctic/medium"
)

# Existing checksum for the pinned model release, also used by the release workflow.
VOICE_ONNX_SHA256 = (
    "483303e294947a3ec2f910ea96093d876e1640f5772e9d89e511d6c82c667286"
)

# Limit download size even when Content-Length is missing or incorrect.
_MAX_DOWNLOAD_BYTES = 1 << 30
# Enforce stall and total deadlines outside the read because socket timeouts reset on
# each received byte.
_READ_STALL_S = 60
_DOWNLOAD_DEADLINE_S = 3600


def _unblock_reader(resp) -> None:
    """Try to shut down the socket without waiting for the reader to release its buffer
    lock.
    """
    try:
        resp.fp.raw._sock.shutdown(socket.SHUT_RDWR)
    except Exception:  # noqa: BLE001
        pass


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _voice_model_ok(path: Path) -> bool:
    """Check an existing model against the pinned checksum before reusing it."""
    try:
        return path.exists() and _sha256(path) == VOICE_ONNX_SHA256
    except OSError:
        return False


def _run(args: list[str], timeout: int) -> None:
    print(f"  $ {' '.join(str(a) for a in args)}")
    # Bound child processes so a stalled setup cannot wait indefinitely.
    subprocess.run(args, check=True, timeout=timeout)


def download_voice() -> None:
    VOICES_DIR.mkdir(exist_ok=True)
    # Remove abandoned partial downloads. The age limit preserves other running
    # installers' files.
    for stale in VOICES_DIR.glob(f"{VOICE_STEM}.*.part"):
        try:
            if stale.stat().st_mtime < time.time() - 3600:
                stale.unlink()
        except OSError:
            pass

    config_file = VOICES_DIR / f"{VOICE_STEM}.onnx.json"
    if _voice_model_ok(VOICE_FILE) and config_file.exists():
        size_mb = VOICE_FILE.stat().st_size / 1_048_576
        print(f"Voice model already present ({size_mb:.0f} MB): {VOICE_FILE}")
        return

    last_pct = [-1]

    def _progress(count: int, block_size: int, total_size: int) -> None:
        if total_size <= 0:
            return
        pct = min(count * block_size * 100 // total_size, 100)
        if pct != last_pct[0]:
            print(f"\r  {pct}% ", end="", flush=True)
            last_pct[0] = pct

    for ext, label in ((".onnx", "model (~77 MB)"), (".onnx.json", "config")):
        dest = VOICES_DIR / f"{VOICE_STEM}{ext}"
        url  = f"{VOICE_BASE}/{VOICE_STEM}{ext}"
        if dest.exists():
            if ext != ".onnx" or _voice_model_ok(dest):
                # Fetch only missing files. Verify an existing model before reusing it.
                print(f"Already present: {dest}")
                continue
            print(f"Existing {dest.name} failed the pinned checksum; re-downloading.")
        print(f"Downloading {VOICE_STEM}{ext} {label} ...")
        print(f"  Source: {url}")
        last_pct[0] = -1
        # Write to a temporary file unique to this process and rename after completion.
        part = dest.with_name(f"{dest.name}.{os.getpid()}.part")
        deadline = time.monotonic() + _DOWNLOAD_DEADLINE_S
        try:
            with open_response(url, 30, min(deadline, time.monotonic() + _READ_STALL_S)) as resp, open(part, "wb") as f:
                # Treat invalid lengths as unknown. The byte limit still applies.
                try:
                    total = int(resp.headers.get("Content-Length", 0) or 0)
                except ValueError:
                    total = 0
                # Read in a helper so the caller can enforce both deadlines.
                done = threading.Event()
                progress = [0]
                reader_error = [None]

                def _reader() -> None:
                    try:
                        read_chunk = getattr(resp, "read1", resp.read)
                        while True:
                            chunk = read_chunk(1 << 16)
                            if not chunk:
                                break
                            progress[0] += len(chunk)
                            if progress[0] > _MAX_DOWNLOAD_BYTES:
                                raise OSError(
                                    f"download over {_MAX_DOWNLOAD_BYTES} bytes: {url}")
                            f.write(chunk)
                            _progress(progress[0], 1, total)
                    except BaseException as exc:
                        reader_error[0] = exc
                    finally:
                        done.set()

                threading.Thread(target=_reader, daemon=True).start()
                last_seen = progress[0]
                last_change = time.monotonic()
                while not done.wait(timeout=min(_READ_STALL_S, max(0.0, deadline - time.monotonic()))):
                    now = time.monotonic()
                    if progress[0] == last_seen or now > deadline:
                        # Shut down the socket to wake the reader without waiting for
                        # its read lock.
                        _unblock_reader(resp)
                        # Report a stall only after the full quiet window has elapsed.
                        if now - last_change >= _READ_STALL_S:
                            raise OSError(
                                f"download stalled, no new bytes for {_READ_STALL_S} seconds: {url}")
                        raise OSError(
                            f"download timed out after 60 minutes: {url}")
                    last_seen = progress[0]
                    last_change = now
                if reader_error[0]:
                    raise reader_error[0]
                got = progress[0]
            # A connection can close early without raising, so check the expected
            # length.
            if total and got < total:
                raise OSError(
                    f"Download incomplete: received {got} of {total} bytes")
            if ext == ".onnx":
                actual = _sha256(part)
                if actual != VOICE_ONNX_SHA256:
                    raise SystemExit(
                        f"voice model checksum mismatch for {dest.name}:\n"
                        f"  expected {VOICE_ONNX_SHA256}\n  got      {actual}\n"
                        "Refusing to install a tampered or truncated model.")
            os.replace(part, dest)
        except BaseException:
            try:
                part.unlink()
            except OSError:
                pass
            raise
        print()
        print(f"  Saved to {dest}")


# Serialize environment creation and pip installation across program instances.
_SETUP_LOCK = VENV_DIR.parent / "ffxiv.setup.lock"
# Allow for the maximum environment creation and pip install time before treating a lock
# as stale.
_SETUP_LOCK_S = 900
_SETUP_WAIT_S = 900


@contextlib.contextmanager
def setup_lock():
    """Lock environment setup through exclusive file creation. Remove stale locks by age
    because PID liveness checks are not portable. Raise RuntimeError if waiting exceeds
    one full setup window.
    """
    _SETUP_LOCK.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + _SETUP_WAIT_S
    while True:
        try:
            fd = os.open(_SETUP_LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            pass
        try:
            if _SETUP_LOCK.stat().st_mtime < time.time() - _SETUP_LOCK_S:
                _SETUP_LOCK.unlink()
                continue
        except OSError:
            pass
        if time.monotonic() > deadline:
            raise RuntimeError(
                "Another NyaaTriggers setup is already running. "
                "Wait for it to finish, then retry.")
        time.sleep(1)
    # The PID is diagnostic. Lock age determines staleness.
    try:
        os.write(fd, str(os.getpid()).encode())
    except OSError:
        pass
    os.close(fd)
    try:
        yield
    finally:
        try:
            _SETUP_LOCK.unlink()
        except OSError:
            pass


def setup_venv() -> None:
    pip = VENV_DIR / ("Scripts" if platform.system() == "Windows" else "bin") / (
        "pip.exe" if platform.system() == "Windows" else "pip")
    try:
        with setup_lock():
            # Check for pip under the lock because interrupted environment creation can
            # leave an incomplete directory.
            if not pip.exists():
                print(f"\nCreating piper venv at {VENV_DIR} ...")
                _run([sys.executable, "-m", "venv", str(VENV_DIR)], timeout=120)
            else:
                print(f"\nPiper venv already exists: {VENV_DIR}")

            print("Installing / upgrading piper-tts ...")
            # Keep the dependency pin in sync with requirements.txt.
            _run([str(pip), "install", "--upgrade", "piper-tts==1.4.2"], timeout=600)
    except RuntimeError as e:
        raise SystemExit(str(e))


def main() -> None:
    print("=== NyaaTriggers Setup ===\n")
    download_voice()
    setup_venv()
    print("\nDone. Start the program with: python main.py")


if __name__ == "__main__":
    main()
