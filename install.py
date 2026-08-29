#!/usr/bin/env python3
"""
NyaaTriggers installer. Run once before launching the app.

  python install.py

Downloads the en_US-arctic-medium voice model into voices/ and creates
~/.venv/ffxiv with piper-tts installed. Roughly 77 MB, CC0. Linux, Windows.
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

VOICES_DIR   = Path(__file__).parent / "voices"
VENV_DIR     = Path.home() / ".venv" / "ffxiv"
VOICE_STEM   = "en_US-arctic-medium"
VOICE_FILE   = VOICES_DIR / f"{VOICE_STEM}.onnx"

# Official Piper voice, CC0. Must match the app default in tts.py and main.py.
VOICE_BASE = (
    "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0"
    "/en/en_US/arctic/medium"
)

# Pinned SHA-256 of the v1.0.0 onnx model, 76,766,385 bytes. Mirrors the check
# the release pipeline runs in release.yml. A truncated or MITM'd download is
# rejected before onnxruntime ever parses the file. The .json config has no
# upstream-pinned hash, so only the model is verified.
VOICE_ONNX_SHA256 = (
    "483303e294947a3ec2f910ea96093d876e1640f5772e9d89e511d6c82c667286"
)

# Hard ceiling on one download. The model is ~77 MB, so a stream running past
# 1 GiB has a lying Content-Length or no end at all and would otherwise be
# written until the disk fills.
_MAX_DOWNLOAD_BYTES = 1 << 30
# Watchdog timing for the read loop below. The loop runs on a daemon helper
# while the calling thread enforces a stall window and a total deadline from
# outside the read, the same guard main.py runs for the same files. The per
# read socket timeout resets on every received byte, so it alone can never
# cut off a peer that trickles one byte at a time.
_READ_STALL_S = 60
_DOWNLOAD_DEADLINE_S = 3600


def _unblock_reader(resp) -> None:
    """Shut the underlying socket down so a read parked in another thread
    wakes at once. A plain resp.close from this side would block on the
    buffer lock the parked read still holds. Best effort, the reader is a
    daemon thread either way."""
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
    """True when a pre-existing model matches the pinned hash. The skip checks
    below used to key on existence alone, so a truncated file left by a hard
    kill mid download was kept forever and the hash check never ran on it."""
    try:
        return path.exists() and _sha256(path) == VOICE_ONNX_SHA256
    except OSError:
        return False


def _run(args: list[str], timeout: int) -> None:
    print(f"  $ {' '.join(str(a) for a in args)}")
    # Bounded like the same steps in main.py. A wedged child otherwise hangs
    # the installer forever. TimeoutExpired propagates with a readable message.
    subprocess.run(args, check=True, timeout=timeout)


def download_voice() -> None:
    VOICES_DIR.mkdir(exist_ok=True)
    # A hard kill mid download leaks its .part, unique per pid so nothing
    # sweeps it later. Remove stale ones. The age guard keeps a concurrent
    # second installer's in-flight download untouched.
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

    # piper needs both the .onnx model and its .onnx.json config.
    for ext, label in ((".onnx", "model (~77 MB)"), (".onnx.json", "config")):
        dest = VOICES_DIR / f"{VOICE_STEM}{ext}"
        url  = f"{VOICE_BASE}/{VOICE_STEM}{ext}"
        if dest.exists():
            if ext != ".onnx" or _voice_model_ok(dest):
                # Fetch only what is missing. The early-out above needs both
                # files, so a lone missing .json must not re-download the 77 MB
                # model. A pre-existing model is hashed before it is skipped,
                # a truncated survivor of a hard kill is replaced, not kept.
                print(f"Already present: {dest}")
                continue
            print(f"Existing {dest.name} failed the pinned checksum; re-downloading.")
        print(f"Downloading {VOICE_STEM}{ext} {label} ...")
        print(f"  Source: {url}")
        last_pct[0] = -1
        # Write to a .part and rename on success, so a hard kill mid download
        # never leaves a truncated file at the final path for the existence
        # checks above to keep. Unique per process, two runs at once can't
        # truncate each other's write. Same pattern as updater.download.
        part = dest.with_name(f"{dest.name}.{os.getpid()}.part")
        try:
            with urllib.request.urlopen(url, timeout=30) as resp, open(part, "wb") as f:
                # A junk length from a proxy reads as unknown. The byte cap
                # below still bounds the download.
                try:
                    total = int(resp.headers.get("Content-Length", 0) or 0)
                except ValueError:
                    total = 0
                # Read loop on a daemon helper, watchdog here. The stall
                # window and the total deadline are enforced from outside the
                # read because a trickling peer can hold one resp.read open
                # forever. Same guard main.py runs for the same files.
                done = threading.Event()
                progress = [0]
                reader_error = [None]

                def _reader() -> None:
                    try:
                        while True:
                            chunk = resp.read(1 << 16)
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
                deadline = time.monotonic() + _DOWNLOAD_DEADLINE_S
                last_seen = progress[0]
                last_change = time.monotonic()
                while not done.wait(timeout=min(_READ_STALL_S, max(0.0, deadline - time.monotonic()))):
                    now = time.monotonic()
                    if progress[0] == last_seen or now > deadline:
                        # Shut the connection down so the parked reader wakes
                        # instead of leaking. A plain resp.close here would
                        # block on the lock the parked read still holds.
                        _unblock_reader(resp)
                        # Say which bound cut the read off. The stall line
                        # only fits when the whole stall window really
                        # passed with no byte. Near the deadline the wait is
                        # clamped short, so a wake there after less than a
                        # full window of quiet is the deadline, not a stall.
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
            # A clean early close is a short read with no exception. urlretrieve
            # raised ContentTooShortError for it, so keep the length check.
            if total and got < total:
                raise OSError(
                    f"Download incomplete: received {got} of {total} bytes")
            os.replace(part, dest)
        except BaseException:
            # The partial download never lands at the final path, so the
            # "already present" check next run can't mistake it for complete.
            try:
                part.unlink()
            except OSError:
                pass
            raise
        print()
        # Verify the onnx model against the pinned hash. A bad file is removed so
        # the next run re-downloads instead of feeding garbage to onnxruntime.
        if ext == ".onnx":
            actual = _sha256(dest)
            if actual != VOICE_ONNX_SHA256:
                try:
                    dest.unlink()
                except OSError:
                    pass
                raise SystemExit(
                    f"voice model checksum mismatch for {dest.name}:\n"
                    f"  expected {VOICE_ONNX_SHA256}\n  got      {actual}\n"
                    "Refusing to install a tampered or truncated model.")
        print(f"  Saved to {dest}")


# Cross process setup lock, taken around venv create plus pip install. Two
# app instances in first run setup at once both pass the pip gate below and
# then build the same venv concurrently, which can corrupt it. The kokoro
# install in main_window has the same hazard guarded, but only per process.
_SETUP_LOCK = VENV_DIR.parent / "ffxiv.setup.lock"
# Longest legitimate hold is one venv create at 120 s plus one pip install
# at 600 s. A lock file older than this outlived its holder, a hard kill
# never runs the release, so the next waiter breaks it.
_SETUP_LOCK_S = 900
# A waiter gives up after the same window. The stale check runs first, so a
# dead holder's lock is broken at this mark instead of failing.
_SETUP_WAIT_S = 900


@contextlib.contextmanager
def setup_lock():
    """Hold a cross process lock around venv create plus pip install.

    The file create with O_EXCL is the atomic acquire on POSIX and Windows.
    A holder killed mid setup leaves the file behind, so a lock older than
    the longest legitimate hold is broken as stale. Age is the check, pid
    liveness is not portable, os.kill signal 0 terminates the target on
    Windows. Raises RuntimeError when the wait outlasts one full setup."""
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
    # The pid inside is diagnostic only, staleness is judged by file age.
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
            # A killed venv create leaves the directory behind with no pip
            # inside. Gate on pip itself so a partial venv gets recreated.
            # The gate sits inside the lock so two setups at once cannot
            # both pass it and then build the same venv concurrently.
            if not pip.exists():
                print(f"\nCreating piper venv at {VENV_DIR} ...")
                _run([sys.executable, "-m", "venv", str(VENV_DIR)], timeout=120)
            else:
                print(f"\nPiper venv already exists: {VENV_DIR}")

            print("Installing / upgrading piper-tts ...")
            # Pinned like requirements.txt so source runs are reproducible. Bump when you mean it.
            _run([str(pip), "install", "--upgrade", "piper-tts==1.4.2"], timeout=600)
    except RuntimeError as e:
        # The lock could not be taken. Bow out with the message, no traceback.
        raise SystemExit(str(e))


def main() -> None:
    print("=== NyaaTriggers Setup ===\n")
    download_voice()
    setup_venv()
    print("\nDone. Launch the app with:  python main.py")


if __name__ == "__main__":
    main()
