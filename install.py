#!/usr/bin/env python3

import contextlib
import errno
import hashlib
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path
from uuid import uuid4
from nyaatriggers.http_fetch import open_response
from nyaatriggers.paths import voice_venv_version
from nyaatriggers.voice_config import (
    MAX_VOICE_CONFIG_BYTES as _MAX_VOICE_CONFIG_BYTES, validate_voice_config, voice_config_ok,
)

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

_MAX_DOWNLOAD_BYTES = 1 << 30
# Socket timeouts reset on every byte, so enforce total and stall deadlines separately.
_READ_STALL_S = 60
_DOWNLOAD_DEADLINE_S = 3600


def _unblock_reader(resp) -> None:
    """Wake the reader without waiting for its buffer lock."""
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
    try:
        return path.exists() and _sha256(path) == VOICE_ONNX_SHA256
    except OSError:
        return False


def _run(args: list[str], timeout: int) -> None:
    print(f"  $ {' '.join(str(a) for a in args)}")
    run_setup_command(args, timeout)


def _windows_setup_command(args: list[str]) -> None:
    """Keep the installer and all descendants in a job owned by this wrapper."""
    import ctypes

    class Limits(ctypes.Structure):
        _fields_ = [("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
                    ("flags", ctypes.c_uint32), ("minimum_working_set", ctypes.c_size_t),
                    ("maximum_working_set", ctypes.c_size_t), ("active_processes", ctypes.c_uint32),
                    ("affinity", ctypes.c_size_t), ("priority", ctypes.c_uint32),
                    ("scheduling", ctypes.c_uint32)]

    class ExtendedLimits(ctypes.Structure):
        _fields_ = [("basic", Limits), ("io", ctypes.c_uint64 * 6),
                    ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
                    ("peak_process_memory", ctypes.c_size_t), ("peak_job_memory", ctypes.c_size_t)]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
    kernel.CreateJobObjectW.restype = ctypes.c_void_p
    kernel.SetInformationJobObject.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
    kernel.SetInformationJobObject.restype = ctypes.c_int32
    kernel.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    kernel.AssignProcessToJobObject.restype = ctypes.c_int32
    kernel.GetCurrentProcess.argtypes = []
    kernel.GetCurrentProcess.restype = ctypes.c_void_p
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel.CloseHandle.restype = ctypes.c_int32
    job = kernel.CreateJobObjectW(None, None)
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())
    limits = ExtendedLimits()
    limits.basic.flags = 0x2000
    try:
        if not kernel.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            raise ctypes.WinError(ctypes.get_last_error())
        if not kernel.AssignProcessToJobObject(job, kernel.GetCurrentProcess()):
            raise ctypes.WinError(ctypes.get_last_error())
    except BaseException:
        kernel.CloseHandle(job)
        raise
    code = 1
    try:
        code = subprocess.run(args, close_fds=True).returncode
    except BaseException:
        import traceback
        traceback.print_exc()
    finally:
        # Process exit closes the sole job handle and terminates surviving descendants.
        os._exit(code)


def run_setup_command(args: list[str], timeout: int, *, capture_output=False, env=None):
    """Finish the owned process tree before failed setup can restore user files."""
    command = args
    options = {"text": True, "encoding": "utf-8", "errors": "replace"}
    if capture_output:
        options.update(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if env is not None:
        options["env"] = env
    if os.name == "nt":
        base = getattr(sys, "_base_executable", None) or sys.executable
        command = [base, str(Path(__file__).resolve()), "--run-setup-command", *args]
    else:
        options["start_new_session"] = True
    process = subprocess.Popen(command, **options)
    try:
        output, error = process.communicate(timeout=timeout)
    finally:
        try:
            if os.name == "nt":
                if process.poll() is None:
                    process.kill()
            else:
                import signal
                os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=5)
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                stream.close()
    if process.returncode:
        raise subprocess.CalledProcessError(process.returncode, args, output=output, stderr=error)
    return subprocess.CompletedProcess(args, process.returncode, output, error)


def download_voice() -> None:
    VOICES_DIR.mkdir(exist_ok=True)
    # Preserve recent partial files that another installer may be writing.
    for stale in VOICES_DIR.glob(f"{VOICE_STEM}.*.part"):
        try:
            if stale.stat().st_mtime < time.time() - 3600:
                stale.unlink()
        except OSError:
            pass

    config_file = VOICES_DIR / f"{VOICE_STEM}.onnx.json"
    if _voice_model_ok(VOICE_FILE) and voice_config_ok(config_file):
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
        limit = (min(_MAX_DOWNLOAD_BYTES, _MAX_VOICE_CONFIG_BYTES)
                 if ext == ".onnx.json" else _MAX_DOWNLOAD_BYTES)
        if dest.exists():
            valid = _voice_model_ok(dest) if ext == ".onnx" else voice_config_ok(dest)
            if valid:
                print(f"Already present: {dest}")
                continue
            if ext == ".onnx":
                print(f"Existing {dest.name} failed the pinned checksum; re-downloading.")
            else:
                print(f"Existing {dest.name} is not a usable voice config; re-downloading.")
        print(f"Downloading {VOICE_STEM}{ext} {label} ...")
        print(f"  Source: {url}")
        last_pct[0] = -1
        part = dest.with_name(f"{dest.name}.{os.getpid()}.part")
        deadline = time.monotonic() + _DOWNLOAD_DEADLINE_S
        try:
            with open_response(url, 30, min(deadline, time.monotonic() + _READ_STALL_S)) as resp, open(part, "wb") as f:
                # Treat invalid lengths as unknown. The byte limit still applies.
                try:
                    total = int(resp.headers.get("Content-Length", 0) or 0)
                except ValueError:
                    total = 0
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
                            if progress[0] > limit:
                                raise OSError(
                                    f"download over {limit} bytes: {url}")
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
                        _unblock_reader(resp)
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
            # Early connection closure may not raise an error.
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
            else:
                validate_voice_config(part)
            os.replace(part, dest)
        except BaseException:
            try:
                part.unlink()
            except OSError:
                pass
            raise
        print()
        print(f"  Saved to {dest}")


_SETUP_LOCK = VENV_DIR.parent / "ffxiv.setup.lock"
_SETUP_LOCK_S = 900
_SETUP_WAIT_S = 900


def _wait_for_setup(deadline):
    if time.monotonic() >= deadline:
        raise RuntimeError(
            "Another NyaaTriggers setup is already running. "
            "Wait for it to finish, then retry.")
    time.sleep(1)


@contextlib.contextmanager
def _setup_guard(deadline):
    """Keep stale marker recovery and setup under one process lock."""
    guard = _SETUP_LOCK.with_name(_SETUP_LOCK.name + ".guard")
    fd = os.open(guard, os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, "r+b", buffering=0) as lock:
        lock.seek(0)
        if os.name == "nt":
            import msvcrt
        else:
            import fcntl
        while True:
            try:
                if os.name == "nt":
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                    raise
            _wait_for_setup(deadline)
        try:
            yield
        finally:
            if os.name == "nt":
                lock.seek(0)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(fd, fcntl.LOCK_UN)


@contextlib.contextmanager
def setup_lock():
    """Serialize setup and retain a killed installer's marker for its surviving children."""
    _SETUP_LOCK.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + _SETUP_WAIT_S
    with _setup_guard(deadline):
        while True:
            try:
                fd = os.open(_SETUP_LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                break
            except FileExistsError:
                pass
            try:
                if _SETUP_LOCK.stat().st_mtime < time.time() - _SETUP_LOCK_S:
                    _SETUP_LOCK.unlink()
                    continue
            except OSError:
                pass
            _wait_for_setup(deadline)
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


def _voice_repair_marker(directory: Path) -> Path:
    return directory.with_name(directory.name + ".repair.json")


def voice_repair_pending(directory: Path) -> bool:
    return _voice_repair_marker(directory).exists()


def _voice_install_marker(directory: Path) -> Path:
    return directory.with_name(directory.name + ".installing")


def voice_setup_pending(directory: Path) -> bool:
    return voice_repair_pending(directory) or _voice_install_marker(directory).exists()


def _mark_voice_install(directory: Path) -> None:
    directory.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=directory.parent, prefix=".voice-install-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(b"pending\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, _voice_install_marker(directory))
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _verified_voice_venv_version(directory: Path) -> tuple[int, int] | None:
    version = voice_venv_version(directory)
    config = directory / "pyvenv.cfg"
    if version is not None or not config.exists():
        return version
    python = directory / ("Scripts/python.exe" if platform.system() == "Windows" else "bin/python")
    try:
        if not config.is_file() or not python.is_file():
            raise ValueError("The environment configuration or interpreter is missing")
        code = ("import json,sys; print(json.dumps([list(sys.version_info[:2]), "
                "sys.prefix, sys.base_prefix]))")
        result = run_setup_command([str(python), "-I", "-c", code],
                                   timeout=10, capture_output=True)
        version, prefix, base = json.loads(result.stdout)
        if (not isinstance(version, list) or len(version) != 2
                or any(type(value) is not int for value in version)
                or not isinstance(prefix, str) or not isinstance(base, str)
                or Path(prefix).resolve() != directory.resolve()
                or Path(prefix).resolve() == Path(base).resolve()):
            raise ValueError("The interpreter does not identify this directory as its environment")
        return tuple(version)
    except Exception as exc:
        raise RuntimeError(f"Cannot verify the voice environment at {directory}: {exc}") from exc


def _restore_voice_environment(directory: Path) -> None:
    marker = _voice_repair_marker(directory)
    with marker.open(encoding="utf-8") as stream:
        record = json.loads(stream.read(16384))
    name = record.get("backup") if isinstance(record, dict) else None
    version = record.get("version") if isinstance(record, dict) else None
    pattern = re.escape(directory.name) + r"\.backup-[0-9a-f]{32}"
    if (not isinstance(name, str) or not re.fullmatch(pattern, name)
            or not isinstance(version, list) or len(version) != 2
            or any(type(value) is not int for value in version)):
        raise RuntimeError("Invalid voice environment recovery record")
    backup = directory.parent / name
    if directory.is_symlink() or backup.is_symlink():
        raise RuntimeError("Voice environment recovery paths must not be links")
    if backup.exists():
        if not backup.is_dir():
            raise RuntimeError("Voice environment backup is not a directory")
        if _verified_voice_venv_version(backup) != tuple(version):
            raise RuntimeError("Voice environment backup does not match its recovery record")
        if directory.exists():
            shutil.rmtree(directory)
        os.replace(backup, directory)
    elif not directory.is_dir() or _verified_voice_venv_version(directory) != tuple(version):
        raise RuntimeError("The original voice environment backup is missing")
    marker.unlink()


def _start_voice_repair(directory: Path, backup: Path, version=None) -> None:
    marker = _voice_repair_marker(directory)
    if version is None:
        version = _verified_voice_venv_version(directory)
    fd, temporary = tempfile.mkstemp(dir=directory.parent, prefix=".voice-repair-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump({"backup": backup.name, "version": version}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, marker)
        os.replace(directory, backup)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def prepare_voice_venv(directory: Path, run=None) -> None:
    """Repair interpreter upgrades under setup_lock while preserving the old environment."""
    directory = Path(directory)
    run = run or _run
    incomplete = _voice_install_marker(directory).exists()
    if voice_repair_pending(directory):
        _restore_voice_environment(directory)
    recorded_version = voice_venv_version(directory)
    version = _verified_voice_venv_version(directory)
    repair = version is not None and (recorded_version is None or version != sys.version_info[:2])
    creator = (getattr(sys, "_base_executable", None) or sys.executable) if repair else sys.executable
    kokoro = repair and (any(directory.glob("lib/python*/site-packages/kokoro_onnx"))
                         or (directory / "Lib" / "site-packages" / "kokoro_onnx").is_dir())
    if repair and directory.is_symlink():
        raise RuntimeError("The voice environment is a link. Choose a compatible environment before setup.")
    backup = directory.with_name(directory.name + ".backup-" + uuid4().hex) if repair else None
    _mark_voice_install(directory)
    try:
        if backup is not None:
            _start_voice_repair(directory, backup, version)
        scripts = directory / ("Scripts" if platform.system() == "Windows" else "bin")
        pip = scripts / ("pip.exe" if platform.system() == "Windows" else "pip")
        if not pip.exists():
            run([creator, "-m", "venv", str(directory)], timeout=120)
        packages = ["piper-tts==1.4.2"]
        if kokoro:
            packages.append("kokoro-onnx==0.4.7")
        retry = ["--force-reinstall"] if incomplete else []
        run([str(pip), "install", "--upgrade", "--no-input", *retry, *packages], timeout=600)
        python = scripts / ("python.exe" if platform.system() == "Windows" else "python")
        imports = "import piper.voice, onnxruntime" + (", kokoro_onnx" if kokoro else "")
        run([str(python), "-c", imports], timeout=30)
        if backup is not None:
            _voice_repair_marker(directory).unlink()
        _voice_install_marker(directory).unlink()
    except BaseException as exc:
        if backup is not None and voice_repair_pending(directory):
            try:
                _restore_voice_environment(directory)
            except Exception as recovery:
                original = backup if backup.exists() else directory
                raise RuntimeError(
                    f"Voice setup failed and recovery could not finish: {recovery}. "
                    f"The original environment is preserved at {original}.") from exc
        raise


def setup_venv() -> None:
    try:
        with setup_lock():
            prepare_voice_venv(VENV_DIR)
    except RuntimeError as e:
        raise SystemExit(str(e))


def main() -> None:
    print("=== NyaaTriggers Setup ===\n")
    download_voice()
    setup_venv()
    print("\nDone. Start the program with: python main.py")


if __name__ == "__main__":
    if sys.argv[1:2] == ["--run-setup-command"]:
        _windows_setup_command(sys.argv[2:])
    else:
        main()
