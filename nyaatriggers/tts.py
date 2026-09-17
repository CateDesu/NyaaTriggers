"""Queue synthesis and playback on one worker. Support Piper, Kokoro and system voices,
with aplay on Linux and winsound on Windows.
"""

import array
import glob
import hashlib
import io
import os
import re
import socket
import urllib.request
import platform
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import wave
from pathlib import Path
from queue import Queue, Empty, Full

from nyaatriggers.paths import bundle_root, default_voice_dir

# Restore system library paths before launching external speech tools.
from nyaatriggers import proc_env
from nyaatriggers.locale_util import has_japanese
from nyaatriggers.drop_log import log_drop
from nyaatriggers.http_fetch import open_response

# Use NumPy when available and retain the Python PCM scaling fallback.
try:
    import numpy as _np
except Exception:
    _np = None

_venv_sps: list[str] = []   # site-packages entries we inserted, swapped around by set_venv_path
_stale_venv_sps: set[str] = set()   # entries a previous venv used, their leftover modules get purged
if not getattr(sys, 'frozen', False):
    _FFXIV_VENV = Path.home() / ".venv" / "ffxiv"
    _sp_paths  = glob.glob(str(_FFXIV_VENV / "lib" / "python*" / "site-packages"))
    _sp_paths += glob.glob(str(_FFXIV_VENV / "Lib" / "site-packages"))
    for _sp in _sp_paths:
        if _sp not in sys.path:
            sys.path.insert(0, _sp)
            _venv_sps.append(_sp)

_BASE        = bundle_root()
_VOICES      = _BASE / "voices"
_PIPER_MODEL = default_voice_dir() / "en_US-arctic-medium.onnx"

# Store downloaded models outside the frozen bundle so updates preserve them.
_MODEL_DIR = (Path(sys.executable).parent / "voices"
              if getattr(sys, 'frozen', False) else _VOICES)

_piper_voice: object | None = None
_piper_failed = False   # sticky failed load marker, cleared when the model or venv changes
_piper_lock  = threading.Lock()
# Lock only slow session construction. GUI setters must not wait on model loading.
_piper_build_lock = threading.Lock()
# Discard builds started before the model or voice environment changed.
_piper_epoch: int = 0
_proc_lock   = threading.Lock()
_current_proc: "subprocess.Popen | None" = None
# Distinguish deliberate interruption from synthesis failure so interrupted text is not
# replayed.
_interrupted_proc: "subprocess.Popen | None" = None

# Drop the oldest queued callout if the worker cannot keep up.
_queue          = Queue(maxsize=64)
# Serialize eviction and insertion so competing callers do not evict twice.
_enqueue_lock   = threading.Lock()
# Bound concurrent notification threads and playback processes.
_notification_slots = threading.BoundedSemaphore(4)
_worker_started = threading.Event()
_worker_lock    = threading.Lock()
_master_volume: float = 1.0
# Invalidate results still synthesizing when interrupt runs.
_generation: int = 0

# Windows defaults to the system voice so startup needs no model download.
_engine: str = "system" if platform.system() == "Windows" else "piper"

# Route Japanese text to a Japanese voice. An empty system voice token selects the OS
# default.
_jp_voice: str = ""
_jp_auto: bool = True

# Map displayed Japanese text to kana readings for speech engines that cannot read
# kanji.
_READINGS: dict = {}
# Match the same ideographs as has_japanese so unsupported kanji can be removed before
# espeak.
_KANJI = re.compile(r"[\u3005\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\U00020000-\U0002a6df\U0002a700-\U0002ceaf]")

# Kokoro provides Japanese synthesis in process using ONNX and the espeak-ng phonemizer.
_jp_neural: bool = False
_jp_neural_voice: str = "jf_alpha"
_kokoro = None
_kokoro_failed = False   # sticky failed load marker, cleared when the model is re-downloaded
# Cache import failures to avoid repeating expensive imports. Model setup clears the
# marker.
_kokoro_import_failed = False
_kokoro_lock = threading.Lock()
# Keep slow session construction separate from GUI settings locks.
_kokoro_build_lock = threading.Lock()
# Discard session builds invalidated by a voice setting change.
_kokoro_epoch: int = 0



def set_master_volume(v: float) -> None:
    global _master_volume
    _master_volume = max(0.0, min(2.0, v))


def set_engine(name: str) -> None:
    global _engine
    _engine = "system" if str(name).lower() == "system" else "piper"


def default_engine() -> str:
    return "system" if platform.system() == "Windows" else "piper"


def set_jp_voice(name: str) -> None:
    """Set an enumerated Japanese system voice token. An empty value selects the OS
    default.
    """
    global _jp_voice
    _jp_voice = str(name or "")


_KOKORO_MODEL  = _MODEL_DIR / "kokoro-v1.0.onnx"
_KOKORO_VOICES = _MODEL_DIR / "voices-v1.0.bin"


_KOKORO_URLS = {
    _KOKORO_MODEL:  "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.onnx",
    _KOKORO_VOICES: "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin",
}

# Existing checksums correspond to the pinned release assets above.
_KOKORO_SHA256 = {
    _KOKORO_MODEL:  "7d5df8ecf7d4b1878015a32686053fd0eebe2bc377234608764cc0ef3636a6c5",
    _KOKORO_VOICES: "bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d",
}

# Bound downloads even when Content-Length is absent or incorrect.
_KOKORO_MAX_BYTES = 1 << 30

# Limit total transfer time independently of each socket read timeout.
_KOKORO_DL_DEADLINE_S = 30 * 60
# Enforce stalled transfer detection outside the blocking read.
_KOKORO_DL_STALL_S = 60


def _unblock_reader(resp) -> None:
    """Shut down the socket to unblock a reader without waiting on its buffer lock."""
    try:
        resp.fp.raw._sock.shutdown(socket.SHUT_RDWR)
    except Exception:  # noqa: BLE001
        pass


def _venv_python() -> str:
    """Use the configured voice interpreter, or the program interpreter when no voice
    environment is set. Reject incomplete configured environments to avoid installing
    into the wrong interpreter.
    """
    venv = globals().get("_FFXIV_VENV")
    if venv:
        for cand in (venv / "bin" / "python", venv / "Scripts" / "python.exe"):
            if cand.exists():
                return str(cand)
        log_drop("tts-kokoro",
                 f"no python interpreter in configured venv {venv}; kokoro deps not installed")
        raise FileNotFoundError(f"no python interpreter in configured venv {venv}")
    return sys.executable


def install_kokoro_deps(timeout: int = 1200) -> tuple[bool, str]:
    """Install Kokoro dependencies into the voice environment for source runs. Return
    success and the command output tail.
    """
    if getattr(sys, "frozen", False):
        return True, "bundled"
    try:
        # Keep this pin here because Kokoro is installed in the voice environment, not
        # from requirements.txt.
        r = subprocess.run(
            [_venv_python(), "-m", "pip", "install", "--no-input", "--upgrade", "kokoro-onnx==0.4.7"],
            capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace",
            env=proc_env.child_env())
        tail = (r.stderr or r.stdout or "").strip().splitlines()[-3:]
        return r.returncode == 0, "\n".join(tail)
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def download_kokoro_model() -> bool:
    """Download models into persistent storage using temporary files and atomic
    replacement. Propagate directory creation errors so the UI can report permission
    failures.
    """
    tmp = None
    try:
        _MODEL_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"[tts] kokoro model dir not writable: {exc!r}", file=sys.stderr)
        raise
    try:
        # Remove abandoned temporary downloads. Check age even for live PIDs because
        # process IDs can be reused.
        for stale in _MODEL_DIR.glob("*.part"):
            try:
                pid = int(stale.name.rsplit(".", 2)[1])
            except (IndexError, ValueError):
                continue
            if platform.system() == "Windows":
                # Use age checks on Windows because this signal probe is not safe there.
                dead = False
            else:
                try:
                    os.kill(pid, 0)
                    dead = False
                except ProcessLookupError:
                    dead = True
                except OSError:
                    dead = False
            if not dead:
                try:
                    if time.time() - stale.stat().st_mtime < 3600:
                        continue
                except OSError:
                    continue
            try:
                stale.unlink()
            except OSError:
                pass
        for dest, url in _KOKORO_URLS.items():
            if dest.exists():
                continue
            tmp = dest.with_name(f"{dest.name}.{os.getpid()}.part")
            req = urllib.request.Request(url, headers={"User-Agent": "NyaaTriggers"})
            digest = hashlib.sha256()
            deadline = time.monotonic() + _KOKORO_DL_DEADLINE_S
            headers_deadline = min(deadline, time.monotonic() + _KOKORO_DL_STALL_S)
            with open_response(req, 120, headers_deadline) as r, open(tmp, "wb") as f:
                # Treat invalid Content-Length as unknown and retain the byte limit.
                try:
                    total = int(r.headers.get("Content-Length", 0) or 0)
                except ValueError:
                    total = 0
                # Read on a helper thread so external stall and total deadlines remain
                # enforceable.
                done = threading.Event()
                progress = [0]
                reader_error = [None]

                def _reader() -> None:
                    try:
                        while True:
                            chunk = r.read(1 << 16)
                            if not chunk:
                                break
                            progress[0] += len(chunk)
                            if progress[0] > _KOKORO_MAX_BYTES:
                                raise OSError(
                                    f"download over {_KOKORO_MAX_BYTES} bytes for {dest.name}")
                            digest.update(chunk)
                            f.write(chunk)
                    except BaseException as exc:
                        reader_error[0] = exc
                    finally:
                        done.set()

                threading.Thread(target=_reader, daemon=True).start()
                last_seen = progress[0]
                last_change = time.monotonic()
                while not done.wait(timeout=min(_KOKORO_DL_STALL_S, max(0.0, deadline - time.monotonic()))):
                    now = time.monotonic()
                    if progress[0] == last_seen or now > deadline:
                        # Unblock the reader through its socket because response.close
                        # may wait on the read lock.
                        _unblock_reader(r)
                        # Report a stall only after the full inactivity window has
                        # elapsed.
                        if now - last_change >= _KOKORO_DL_STALL_S:
                            raise OSError(
                                f"download of {dest.name} stalled, no new bytes "
                                f"for {_KOKORO_DL_STALL_S} seconds")
                        raise OSError(
                            f"download of {dest.name} still running past "
                            f"{_KOKORO_DL_DEADLINE_S // 60} min; giving up")
                    last_seen = progress[0]
                    last_change = now
                if reader_error[0]:
                    raise reader_error[0]
                got = progress[0]
            # Check for short reads before installing the file because early EOF may not
            # raise.
            if total and got < total:
                raise OSError(f"short read: {got}/{total} bytes for {dest.name}")
            if digest.hexdigest() != _KOKORO_SHA256[dest]:
                raise OSError(f"checksum mismatch for {dest.name}")
            os.replace(tmp, dest)
            tmp = None
        # Allow loading to retry after model setup completes.
        global _kokoro_failed, _kokoro_import_failed
        _kokoro_failed = False
        _kokoro_import_failed = False
        return True
    except Exception as exc:
        print(f"[tts] kokoro model download failed: {exc!r}", file=sys.stderr)
        if tmp is not None:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
        return False


def set_jp_neural(enabled: bool, voice: str = "jf_alpha") -> None:
    global _jp_neural, _jp_neural_voice, _kokoro, _kokoro_epoch
    _jp_neural = bool(enabled)
    _jp_neural_voice = voice or "jf_alpha"
    if not enabled:
        # Release the loaded session and invalidate any build still in progress.
        with _kokoro_lock:
            _kokoro = None
            _kokoro_epoch += 1


def kokoro_ready() -> bool:
    """Check dependencies and both model files. Cache import failures until model setup
    clears the marker.
    """
    global _kokoro_import_failed
    if not (_KOKORO_MODEL.exists() and _KOKORO_VOICES.exists()):
        return False
    if _kokoro_import_failed:
        return False
    try:
        import kokoro_onnx  # noqa: F401
        return True
    except Exception as exc:  # noqa: BLE001
        _kokoro_import_failed = True
        _log_once("kokoro-import", f"[tts] kokoro-onnx unavailable: {exc!r}")
        return False


def _load_kokoro():
    """Build Kokoro lazily and return None for fallback when unavailable. Cache failures
    until model setup retries, retaining existing model files. Build outside the GUI
    lock and publish only if the captured settings epoch remains current.
    """
    global _kokoro, _kokoro_failed
    if _kokoro is not None:
        return _kokoro
    with _kokoro_build_lock:
        with _kokoro_lock:
            if _kokoro is not None:
                return _kokoro
            if _kokoro_failed:
                return None
            if not (_KOKORO_MODEL.exists() and _KOKORO_VOICES.exists()):
                return None
            epoch = _kokoro_epoch
        # Retry imports after purging modules from the previous voice environment.
        # Settings may have changed while the first import was running.
        _purge_stale_venv_modules()
        try:
            from kokoro_onnx import Kokoro
        except Exception:  # noqa: BLE001
            _purge_stale_venv_modules()
            try:
                from kokoro_onnx import Kokoro
            except Exception as exc:  # noqa: BLE001
                # Retain model files when a dependency is missing.
                with _kokoro_lock:
                    _kokoro_failed = True
                _log_once("kokoro-load", f"[tts] kokoro-onnx import failed: {exc!r}")
                return None
        try:
            # Bound session construction so it cannot block later callouts indefinitely.
            ok, kokoro = _synth_call(
                lambda: Kokoro(str(_KOKORO_MODEL), str(_KOKORO_VOICES)))
        except Exception as exc:  # noqa: BLE001
            with _kokoro_lock:
                # Mark failure only if the build still matches current settings.
                if _kokoro_epoch == epoch:
                    _kokoro_failed = True
                    _kokoro = None
            _log_once("kokoro-load", f"[tts] kokoro voice load failed: {exc!r}")
            # Keep model files on load failure. Setup can clear the failure marker
            # without downloading healthy files again.
            log_drop("tts-kokoro",
                     "kokoro voice load failed; model files kept, "
                     "use the Download button to clear the failure, or delete "
                     "the model files and Download fetches fresh copies")
            return None
        if not ok:
            # Cache timeout failures while retaining the downloaded models.
            with _kokoro_lock:
                if _kokoro_epoch == epoch:
                    _kokoro_failed = True
                    _kokoro = None
            log_drop("tts-kokoro",
                     f"kokoro voice load hung {_SYNTH_TIMEOUT_S}s; engine failed")
            return None
        with _kokoro_lock:
            if _kokoro_epoch == epoch:
                _kokoro = kokoro
            return _kokoro


def _kokoro_synth(text: str, speed: float = 1.0) -> "bytes | None":
    """Return synthesized Japanese WAV bytes, or None for system voice fallback. Use kana
    readings with the espeak-ng phonemizer.
    """
    global _kokoro, _kokoro_failed
    k = _load_kokoro()
    if k is None:
        return None
    try:
        import numpy as np
        # Clamp speed to Kokoro's supported range before synthesis.
        ok, out = _synth_call(lambda: k.create(text, voice=_jp_neural_voice,
                                               speed=min(2.0, max(0.5, speed)), lang="ja"))
        if not ok:
            # Retire only the session that timed out so later callouts do not repeat its
            # stalled synthesis.
            with _kokoro_lock:
                if _kokoro is k:
                    _kokoro_failed = True
                    _kokoro = None
            log_drop("tts-kokoro",
                     f"kokoro synthesis hung {_SYNTH_TIMEOUT_S}s; engine failed, callout dropped: {text[:60]!r}")
            return None
        samples, sr = out
        pcm = (np.clip(np.asarray(samples, dtype="float32"), -1.0, 1.0) * 32767.0).astype("<i2").tobytes()
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(int(sr))
            wf.writeframes(pcm)
        return buf.getvalue()
    except Exception as exc:  # noqa: BLE001
        _log_once("kokoro-synth", f"[tts] kokoro synthesis failed: {exc!r}")
        return None


def set_jp_auto(on: bool) -> None:
    """Route text containing Japanese characters to a Japanese voice."""
    global _jp_auto
    _jp_auto = bool(on)


def set_readings(readings: dict) -> None:
    """Replace the reading map as a whole. Workers hold a snapshot of the reference, so
    published dictionaries must not be mutated.
    """
    global _READINGS
    _READINGS = {k: v for k, v in (readings or {}).items()
                 if isinstance(k, str) and isinstance(v, str) and v}


def reading_for(text: str) -> "str | None":
    """Look up a template reading before substituting the same tokens into display and
    spoken text.
    """
    return _READINGS.get(text) if text else None



class _StampedItem(tuple):
    """Capture the interrupt generation at enqueue time. Retain tuple equality for existing
    callers.
    """

    def __new__(cls, item, gen):
        self = super().__new__(cls, item)
        self.gen = gen
        return self


def _enqueue(item) -> None:
    """Queue without blocking, evicting the oldest item when full."""
    with _enqueue_lock:
        # Stamp at enqueue so interruptions also invalidate items already dequeued for
        # synthesis.
        stamped = _StampedItem(item, _generation)
        try:
            _queue.put_nowait(stamped)
        except Full:
            try:
                _queue.get_nowait()
                log_drop("tts-overflow", "TTS queue full; dropped the oldest queued callout")
                _queue.put_nowait(stamped)
            except (Empty, Full):
                pass


def speak(text: str, volume: float = 1.0, speed: float = 1.0, reading: "str | None" = None) -> None:
    """Queue speech without blocking. reading supplies kana for backends that need it.
    """
    _ensure_worker()
    _enqueue(("tts", text, volume, speed, reading))
    depth = _queue.qsize()
    if depth >= 4:
        log_drop("tts-backlog", f"TTS queue {depth} deep; callouts speaking late: {text[:60]!r}")


def play_sound(path: str, volume: float = 1.0) -> None:
    _ensure_worker()
    _enqueue(("wav", path, volume))


def play_notification(path: str, volume: float = 1.0) -> None:
    """Play notifications alongside speech on Linux. Windows file notifications use the
    shared TTS queue because winsound has one channel. Ignore missing paths.
    """
    if not path or not os.path.exists(path):
        return
    if platform.system() == "Windows":
        play_sound(path, volume)
        return
    # Limit notifications when playback is slow or blocked.
    if not _notification_slots.acquire(blocking=False):
        log_drop("tts-notify", "notification chime dropped; too many plays in flight")
        return
    try:
        threading.Thread(target=_notification_worker, args=(path, volume),
                         daemon=True).start()
    except Exception:
        _notification_slots.release()
        raise


def interrupt() -> None:
    """Clear queued callouts and stop tracked playback. Already running synchronous
    winsound audio finishes normally.
    """
    global _generation
    # Hold the enqueue lock while clearing and incrementing the generation. Always
    # acquire it before the process lock to avoid a lock cycle.
    with _enqueue_lock:
        while True:
            try:
                _queue.get_nowait()
            except Empty:
                break
        with _proc_lock:
            _generation += 1
            if _current_proc is not None:
                global _interrupted_proc
                _interrupted_proc = _current_proc
                try:
                    _current_proc.terminate()
                except OSError:
                    pass


def set_model(path: Path) -> None:
    """Reload the selected Piper model on the next synthesis call."""
    global _PIPER_MODEL, _piper_voice, _piper_failed, _piper_epoch
    with _piper_lock:
        _PIPER_MODEL = Path(path)
        _piper_voice = None
        _piper_failed = False
        _piper_epoch += 1


def _purge_stale_venv_modules() -> None:
    """Remove modules imported from previous voice environments. Updating sys.path alone
    cannot replace already imported modules.
    """
    venv = globals().get("_FFXIV_VENV")
    if not venv:
        return
    sps = glob.glob(str(venv / "lib" / "python*" / "site-packages"))
    sps += glob.glob(str(venv / "Lib" / "site-packages"))
    for name, mod in list(sys.modules.items()):
        mod_file = getattr(mod, "__file__", "") or ""
        if not mod_file:
            continue
        if any(mod_file.startswith(sp + os.sep) for sp in sps):
            continue
        if name.split(".")[0] in ("piper", "onnxruntime") \
                or any(mod_file.startswith(sp + os.sep) for sp in _stale_venv_sps.copy()):
            # Read a snapshot because the GUI can add stale environment paths
            # concurrently.
            del sys.modules[name]


def set_venv_path(path: str) -> None:
    global _FFXIV_VENV, _piper_voice, _piper_failed, _piper_epoch
    if getattr(sys, 'frozen', False):
        return
    new_venv = Path(path.strip()).expanduser() if path.strip() else Path.home() / ".venv" / "ffxiv"
    # Update the interpreter selector used by the Kokoro installer too.
    _FFXIV_VENV = new_venv
    new_sps  = glob.glob(str(new_venv / "lib" / "python*" / "site-packages"))
    new_sps += glob.glob(str(new_venv / "Lib" / "site-packages"))
    # Remove previous path entries so they cannot shadow the new environment.
    for old in _venv_sps:
        try:
            sys.path.remove(old)
        except ValueError:
            pass
    _stale_venv_sps.update(_venv_sps)
    _venv_sps.clear()
    for sp in new_sps:
        if sp not in sys.path:
            sys.path.insert(0, sp)
            _venv_sps.append(sp)
    # Purge imports still bound to the old environment before loading from the new one.
    _purge_stale_venv_modules()
    with _piper_lock:
        _piper_voice = None
        _piper_failed = False
        _piper_epoch += 1



_logged_once: set = set()


def _log_once(key: str, msg: str) -> None:
    """Log the first occurrence of each failure key."""
    if key not in _logged_once:
        _logged_once.add(key)
        print(msg, file=sys.stderr)


_aplay_missing_logged = False


def _aplay_missing() -> None:
    """Record missing aplay once in the drop log so frozen builds report the failure."""
    global _aplay_missing_logged
    if not _aplay_missing_logged:
        _aplay_missing_logged = True
        log_drop("tts-aplay", "aplay not found; install alsa-utils, callouts have no audio",
                 throttle_s=0)


# Bound in-process model operations so a stalled ONNX call cannot block the speech
# queue.
_SYNTH_TIMEOUT_S = 60


def _synth_call(fn):
    """Run an ONNX operation on a daemon thread with a deadline. Return false and None on
    timeout, and propagate exceptions from completed calls.
    """
    box: dict = {}

    def _run() -> None:
        try:
            box["out"] = fn()
        except Exception as exc:   # noqa: BLE001
            box["err"] = exc

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(_SYNTH_TIMEOUT_S)
    if t.is_alive():
        return False, None
    if "err" in box:
        raise box["err"]
    return True, box.get("out")


def _ensure_worker() -> None:
    if _worker_started.is_set():
        return
    with _worker_lock:
        if _worker_started.is_set():
            return
        t = threading.Thread(target=_worker_loop, daemon=True, name="tts-worker")
        t.start()
        _worker_started.set()


def _worker_loop() -> None:
    while True:
        item = _queue.get()
        gen = getattr(item, "gen", None)
        try:
            if item[0] == "wav":
                _, path, volume = item
                _play_wav_file(path, volume, gen)
            else:
                _, text, volume, speed, reading = item
                _pipeline(text, volume, speed, reading, gen)
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            log_drop("tts-error", f"{type(exc).__name__} in the TTS worker: {exc}")


def _system_speak(text: str, volume: float = 1.0, speed: float = 1.0,
                  reading: "str | None" = None, gen: "int | None" = None) -> bool:
    """Use a tracked system speech process. Fall back to Piper only for non-Japanese text
    when no backend succeeds. Check the interrupt generation before spawning.
    """
    if not text:
        return True
    system = platform.system()
    # Allow volume up to 200 percent where supported. Clamp SAPI at its own limit.
    vol = max(0.0, min(2.0, volume * _master_volume))
    # Japanese text must not fall through to an English Piper model.
    jp = _jp_auto and has_japanese(text)
    # Prefer kana readings for espeak. Remove unsupported kanji when no reading exists,
    # and treat empty Japanese output as handled to prevent English fallback.
    linux_text = reading if (jp and reading) else text
    is_linux = system != "Windows"
    if is_linux and jp and _KANJI.search(linux_text):
        linux_text = _KANJI.sub("", linux_text).strip()
        if not linux_text:
            log_drop("tts-jp", f"kanji-only text with no kana reading; nothing spoken: {text[:60]!r}")
            return True
    try:
        if system == "Windows":
            # Pass speech text through stdin to avoid PowerShell quoting issues.
            rate = max(-10, min(10, int(round((speed - 1.0) * 5))))
            sel = ""
            if jp and _jp_voice:
                # Escape quotes even in enumerated voice names.
                sel = f"$s.SelectVoice('{_jp_voice.replace(chr(39), chr(39) * 2)}');"
            elif jp:
                sel = ("$s.SelectVoiceByHints("
                       "[System.Speech.Synthesis.VoiceGender]::NotSet,"
                       "[System.Speech.Synthesis.VoiceAge]::NotSet,0,"
                       "(New-Object System.Globalization.CultureInfo('ja-JP')));")
            ps = (
                "$ErrorActionPreference='SilentlyContinue';"
                "[Console]::InputEncoding=[System.Text.Encoding]::UTF8;"
                "Add-Type -AssemblyName System.Speech;"
                "$s=New-Object System.Speech.Synthesis.SpeechSynthesizer;"
                f"{sel}"
                f"$s.Volume={int(min(1.0, vol) * 100)};$s.Rate={rate};"
                "$s.Speak([Console]::In.ReadToEnd());"
            )
            return _run_speak_proc(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                text, stdin_text=True, no_window=True, gen=gen) or jp
        # Try available system backends in order. If all fail for Japanese, avoid
        # passing it to an English Piper model.
        import shutil
        if shutil.which("spd-say"):
            rate = max(-100, min(100, int((speed - 1.0) * 100)))
            # Map normal volume to zero and doubled volume to 100 for speech-dispatcher.
            cmd = ["spd-say", "-w", "-r", str(rate),
                   "-i", str(max(-100, min(100, int(round((vol - 1.0) * 100)))))]
            if jp:
                cmd += ["-l", "ja"]
            if _run_speak_proc(cmd, linux_text, stdin_text=False, gen=gen):
                return True
        if shutil.which("espeak"):
            cmd = ["espeak", "-s", str(max(80, int(175 * speed))),
                   "-a", str(max(0, min(200, int(round(vol * 100)))))]
            if jp:
                cmd += ["-v", "ja"]
            return _run_speak_proc(cmd, linux_text, stdin_text=False, gen=gen) or jp
        # Suppress English fallback for Japanese when no system backend is available.
        return jp
    except Exception as exc:
        # Report spawn failures while preserving the Japanese fallback rule.
        _log_once("system-spawn", f"[tts] system voice spawn failed: {exc!r}")
        return jp


def _run_speak_proc(cmd: list[str], text: str, stdin_text: bool, no_window: bool = False,
                    gen: "int | None" = None) -> bool:
    """Track the speech process for interruption. Send text on stdin or as the final
    argument, and reject stale generations before spawn.
    """
    global _current_proc, _interrupted_proc
    kwargs: dict = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
                    "env": proc_env.child_env()}
    if no_window and platform.system() == "Windows":
        kwargs["creationflags"] = 0x08000000          # CREATE_NO_WINDOW
    if stdin_text:
        kwargs["stdin"] = subprocess.PIPE
        # Use UTF-8 on both sides of stdin so Japanese works on non-Japanese Windows
        # locales.
        kwargs["text"] = True
        kwargs["encoding"] = "utf-8"
        kwargs["errors"] = "replace"
    else:
        # End option parsing before callout text. Disable inherited stdin when using a
        # text argument.
        cmd = cmd + ["--", text]
        kwargs["stdin"] = subprocess.DEVNULL
    # Make process creation and registration atomic with interrupt.
    with _proc_lock:
        if gen is not None and gen != _generation:
            # Treat interrupted text as handled so it cannot be replayed through Piper.
            return True
        proc = subprocess.Popen(cmd, **kwargs)
        _current_proc = proc
    timed_out = False
    kill_failed = False
    try:
        if stdin_text:
            try:
                proc.communicate(text, timeout=30)
            except subprocess.TimeoutExpired:
                timed_out = True
                proc.kill()
                try:
                    proc.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    # Stop waiting if the backend remains blocked after kill.
                    pass
            except OSError:
                # A broken pipe does not prove the child exited. Kill and reap it before
                # releasing ownership or allowing fallback.
                proc.kill()
                try:
                    proc.communicate(timeout=5)
                except (subprocess.TimeoutExpired, OSError):
                    # Avoid fallback if the process may still be speaking after a failed
                    # kill.
                    kill_failed = True
        else:
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                timed_out = True
                proc.kill()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
    finally:
        with _proc_lock:
            was_interrupted = _interrupted_proc is proc
            if was_interrupted:
                _interrupted_proc = None
            _current_proc = None
    # Intentional termination counts as handled. Real backend failures can use fallback.
    if was_interrupted:
        return True
    if timed_out:
        # Drop timed out callouts instead of replaying stale text.
        log_drop("tts-backend", f"system TTS wedged; killed after 30s, callout dropped: {text[:60]!r}")
        return True
    if kill_failed:
        log_drop("tts-backend", f"system TTS survived the kill; callout dropped: {text[:60]!r}")
        return True
    if proc.returncode != 0:
        log_drop("tts-backend", f"system TTS failed with exit status {proc.returncode}")
    return proc.returncode == 0


def _load_piper():
    """Cache the Piper voice or its load failure until settings change. Build outside the
    GUI lock and publish only if inputs still match. Return the instance so callers keep
    it if a setter clears the global.
    """
    global _piper_voice, _piper_failed
    v = _piper_voice
    if v is not None:
        return v
    with _piper_build_lock:
        with _piper_lock:
            if _piper_voice is not None:
                return _piper_voice
            if _piper_failed:
                return None
            model = _PIPER_MODEL
            epoch = _piper_epoch
        # Purge stale modules again because imports may finish after a settings change.
        _purge_stale_venv_modules()
        try:
            import json
            import onnxruntime
            from piper.config import PiperConfig
            from piper.voice import PiperVoice
            # Disable ONNX thread spinning so an idle model does not consume CPU needed
            # by the combat feed. Construct the session directly because PiperVoice.load
            # uses default options.
            sess_options = onnxruntime.SessionOptions()
            sess_options.add_session_config_entry(
                "session.intra_op.allow_spinning", "0")
            with open(f"{model}.json", "r", encoding="utf-8") as cfg:
                config = PiperConfig.from_dict(json.load(cfg))
            # Apply the synthesis deadline to session construction too.
            ok, session = _synth_call(lambda: onnxruntime.InferenceSession(
                str(model),
                sess_options=sess_options,
                providers=["CPUExecutionProvider"],
            ))
            if not ok:
                raise TimeoutError(
                    f"piper session build hung past {_SYNTH_TIMEOUT_S}s")
            voice = PiperVoice(config=config, session=session)
        except Exception as exc:  # noqa: BLE001
            with _piper_lock:
                # Record failure only for the settings that started this build.
                if _piper_epoch == epoch:
                    _piper_failed = True
                    print(f"[tts] piper voice load failed: {exc!r}", file=sys.stderr)
            return None
        with _piper_lock:
            if _piper_epoch == epoch:
                _piper_voice = voice
            return _piper_voice


def _scale_pcm(frames: bytes, sampwidth: int, volume: float) -> "bytes | None":
    """Scale unsigned 8-bit or signed 16-bit PCM, using NumPy when available. Return None
    for unsupported formats.
    """
    if sampwidth == 2:                              # 16-bit signed
        if len(frames) % 2:                         # Discard an incomplete sample.
            frames = frames[:-1]
        if _np is not None:
            s = _np.frombuffer(frames, dtype="<i2").astype(_np.float64) * volume
            return _np.clip(s, -32768, 32767).astype("<i2").tobytes()
        samples = array.array('h', frames)
        # PCM is little-endian, while array uses native byte order.
        if sys.byteorder == 'big':
            samples.byteswap()
        for i in range(len(samples)):
            samples[i] = max(-32768, min(32767, int(samples[i] * volume)))
        if sys.byteorder == 'big':
            samples.byteswap()
        return samples.tobytes()
    if sampwidth == 1:                              # 8-bit unsigned, centred at 128
        if _np is not None:
            s = (_np.frombuffer(frames, dtype=_np.uint8).astype(_np.float64) - 128.0) * volume
            return _np.clip(_np.rint(s) + 128.0, 0, 255).astype(_np.uint8).tobytes()
        samples = array.array('B', frames)
        for i in range(len(samples)):
            samples[i] = max(0, min(255, int(round((samples[i] - 128) * volume)) + 128))
        return samples.tobytes()
    return None                                     # 24-bit / 32-bit / float


def _apply_volume(wav_path: str, volume: float) -> bool:
    """Scale supported PCM WAV files in place. Return false for unsupported or damaged
    input so callers can retain native volume.
    """
    try:
        with wave.open(wav_path, 'rb') as r:
            params = r.getparams()
            frames = r.readframes(params.nframes)
    except (wave.Error, EOFError):
        return False
    scaled = _scale_pcm(frames, params.sampwidth, volume)
    if scaled is None:
        return False
    with wave.open(wav_path, 'wb') as w:
        w.setparams(params)
        w.writeframes(scaled)
    return True


def _riff_wav_seconds(source) -> float:
    """Read duration from RIFF chunks for float and extensible WAV formats. Return zero for
    invalid data.
    """
    try:
        src = io.BytesIO(source) if isinstance(source, bytes) else open(source, "rb")
        with src:
            size = src.seek(0, 2)
            src.seek(0)
            head = src.read(12)
            if len(head) < 12 or head[:4] != b"RIFF" or head[8:12] != b"WAVE":
                return 0.0
            fmt = None
            data_size = 0
            # RIFF chunks with odd sizes have a padding byte.
            while fmt is None or not data_size:
                hdr = src.read(8)
                if len(hdr) < 8:
                    return 0.0
                chunk_size = int.from_bytes(hdr[4:8], "little")
                skip = chunk_size + (chunk_size & 1)
                if hdr[:4] == b"fmt " and fmt is None:
                    if chunk_size < 16:
                        return 0.0
                    fmt = src.read(16)
                    src.seek(skip - 16, 1)
                elif hdr[:4] == b"data":
                    # Limit declared chunk size to the bytes actually present.
                    data_size = min(chunk_size, max(0, size - src.tell()))
                    src.seek(skip, 1)
                else:
                    src.seek(skip, 1)
            channels = int.from_bytes(fmt[2:4], "little")
            rate = int.from_bytes(fmt[4:8], "little")
            block = int.from_bytes(fmt[12:14], "little")
            bits = int.from_bytes(fmt[14:16], "little")
            if not block and channels and bits:
                # Derive block alignment when the header omits it.
                block = channels * bits // 8
            if not rate or not block:
                return 0.0
            return data_size / float(rate * block)
    except Exception:  # noqa: BLE001
        return 0.0


def _wav_seconds(source) -> float:
    """Return WAV duration from a path or bytes, or zero when unreadable."""
    try:
        src = io.BytesIO(source) if isinstance(source, bytes) else source
        with wave.open(src, "rb") as w:
            return w.getnframes() / float(w.getframerate() or 1)
    except Exception:  # noqa: BLE001
        # Try RIFF parsing because wave does not support every playable format.
        return _riff_wav_seconds(source)


def _play_winsound(source, flags: int, gen: "int | None" = None) -> None:
    """Run synchronous winsound playback with a bounded wait so a stalled driver cannot
    hold the worker. Propagate errors from completed playback.
    """
    import winsound
    box: dict = {}

    def _run() -> None:
        try:
            # Check the generation when playback starts, then release the lock so
            # interrupt stays responsive.
            with _proc_lock:
                if gen is not None and gen != _generation:
                    return
            winsound.PlaySound(source, flags)
        except Exception as exc:   # noqa: BLE001
            box["err"] = exc

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    # Allow long sounds to finish by deriving the deadline from WAV duration.
    t.join(max(60.0, _wav_seconds(source) * 1.5 + 5))
    if t.is_alive():
        log_drop("tts-playback",
                 "winsound hung on the audio device; playback abandoned, callout had no audio")
        return
    if "err" in box:
        raise box["err"]


def _play_wav(wav_path: str, gen: "int | None" = None) -> None:
    """Play through a tracked aplay process. Check the interrupt generation under the
    process lock before spawning.
    """
    global _current_proc, _interrupted_proc
    system = platform.system()
    if system == "Windows":
        import winsound
        _play_winsound(wav_path, winsound.SND_FILENAME | winsound.SND_NODEFAULT, gen)
        return
    # End option parsing before the path and capture playback errors for the drop log.
    player = ["aplay", "-q", "--", wav_path]
    with _proc_lock:
        if gen is not None and gen != _generation:
            log_drop("tts-interrupt",
                     "callout cut off by a newer interrupt before playback started")
            return
        try:
            proc = subprocess.Popen(player, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.PIPE,
                                    env=proc_env.child_env())
        except FileNotFoundError:
            _aplay_missing()
            return
        _current_proc = proc
    err = b""
    timed_out = False
    started = time.monotonic()
    # Derive the deadline from sound duration instead of cutting long files off at sixty
    # seconds.
    duration = _wav_seconds(wav_path)
    play_timeout = max(60.0, duration * 1.5 + 5)
    try:
        _, err = proc.communicate(timeout=play_timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        proc.kill()
        try:
            err = proc.communicate(timeout=5)[1]
        except subprocess.TimeoutExpired:
            # Stop waiting if the player remains blocked after kill.
            pass
    finally:
        with _proc_lock:
            was_interrupted = _interrupted_proc is proc
            if was_interrupted:
                _interrupted_proc = None
            _current_proc = None
    # Intentional interruption is not a playback failure.
    if was_interrupted:
        return
    if timed_out:
        # Log timeouts because a stalled audio device may produce no error output.
        log_drop("tts-playback",
                 f"aplay hung {play_timeout:.0f}s on the audio device; killed, callout had no audio")
        return
    if proc.returncode != 0:
        lines = (err or b"").decode(errors="replace").strip().splitlines()
        detail = lines[-1][:160] if lines else f"exit {proc.returncode}"
        log_drop("tts-playback",
                 f"aplay exit {proc.returncode}; callout had no audio: {detail}")
        return
    # An unexpectedly early successful exit may indicate discarded audio rather than
    # completed playback.
    elapsed = time.monotonic() - started
    if duration and elapsed < duration * 0.5:
        log_drop("tts-playback",
                 f"aplay exited 0 after {elapsed:.2f}s for a {duration:.2f}s wav; "
                 "the device likely discarded the audio")


# Limit sound copies so large or growing files cannot exhaust temporary storage.
_MAX_SOUND_BYTES = 32 << 20


def _copy_sound_to_tmp(path: str) -> "str | None":
    """Copy a regular sound file to a temporary WAV while enforcing the size cap. Log and
    return None for invalid, oversized or failed copies.
    """
    if not os.path.isfile(path):
        log_drop("tts-sound", f"not a regular file; not played: {path!r}")
        return None
    if os.path.getsize(path) == 0:
        log_drop("tts-sound", f"empty sound file; not played: {path!r}")
        return None
    if os.path.getsize(path) > _MAX_SOUND_BYTES:
        log_drop("tts-sound", f"sound file over {_MAX_SOUND_BYTES >> 20} MiB; not played: {path!r}")
        return None
    fd, tmp_path = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    # Recheck the byte limit while copying because the source may grow after its size
    # check.
    total = 0
    ok = True
    try:
        with open(path, "rb") as fin, open(tmp_path, "wb") as fout:
            while True:
                chunk = fin.read(1 << 20)
                if not chunk:
                    break
                total += len(chunk)
                if total > _MAX_SOUND_BYTES:
                    log_drop("tts-sound", f"sound file grew past {_MAX_SOUND_BYTES >> 20} MiB; not played: {path!r}")
                    ok = False
                    break
                fout.write(chunk)
    except OSError as exc:
        log_drop("tts-sound", f"sound copy failed; not played: {path!r}: {exc}")
        ok = False
    if not ok:
        Path(tmp_path).unlink(missing_ok=True)
        return None
    return tmp_path


def _play_wav_file(path: str, volume: float = 1.0, gen: "int | None" = None) -> None:
    tmp_path = None
    # Capture a generation for direct callers too. Playback rechecks it after file
    # validation and copying.
    if gen is None:
        gen = _generation
    try:
        effective_volume = volume * _master_volume
        if effective_volume <= 0.0:
            return
        if abs(effective_volume - 1.0) > 0.01:
            tmp_path = _copy_sound_to_tmp(path)
            if tmp_path is None:
                return
            if not _apply_volume(tmp_path, effective_volume):
                print(f"[tts] cannot adjust the volume of "
                      f"{os.path.basename(path)} (needs an 8- or 16-bit PCM WAV); "
                      f"playing at its native level", file=sys.stderr)
            _play_wav(tmp_path, gen)
        else:
            # Reject nonregular files before aplay can block on them.
            if not os.path.isfile(path):
                log_drop("tts-sound", f"not a regular file; not played: {path!r}")
                return
            if os.path.getsize(path) > _MAX_SOUND_BYTES:
                log_drop("tts-sound", f"sound file over {_MAX_SOUND_BYTES >> 20} MiB; not played: {path!r}")
                return
            _play_wav(path, gen)
    finally:
        if tmp_path:
            try:
                Path(tmp_path).unlink(missing_ok=True)
            except OSError:
                pass


def _notification_worker(path: str, volume: float) -> None:
    """Play Linux file notifications with optional temporary volume scaling and cleanup.
    """
    tmp_path = None
    try:
        effective = max(0.0, min(2.0, volume * _master_volume))
        if effective <= 0.0:
            return
        play_path = path
        if abs(effective - 1.0) > 0.01:
            tmp_path = _copy_sound_to_tmp(path)
            if tmp_path is None:
                return
            if not _apply_volume(tmp_path, effective):
                print(f"[tts] notification: cannot adjust the volume of "
                      f"{os.path.basename(path)} (needs an 8- or 16-bit PCM WAV); "
                      f"playing at its native level", file=sys.stderr)
            play_path = tmp_path
        else:
            # Reject nonregular files before detached playback too.
            if not os.path.isfile(path):
                log_drop("tts-notify", f"not a regular file; not played: {path!r}")
                return
            if os.path.getsize(path) == 0:
                log_drop("tts-notify", f"empty sound file; not played: {path!r}")
                return
            if os.path.getsize(path) > _MAX_SOUND_BYTES:
                log_drop("tts-notify", f"sound file over {_MAX_SOUND_BYTES >> 20} MiB; not played: {path!r}")
                return
        _play_wav_detached(play_path)
    except Exception as exc:  # noqa: BLE001
        print(f"[tts] notification failed: {exc!r}", file=sys.stderr)
    finally:
        _notification_slots.release()
        if tmp_path:
            try:
                Path(tmp_path).unlink(missing_ok=True)
            except OSError:
                pass


def _log_notification_result(result) -> None:
    if result.returncode:
        detail = (result.stderr or b"").decode("utf-8", errors="replace").strip()[:240]
        log_drop("tts-notify", f"aplay exited {result.returncode}: {detail}")


def _play_wav_detached(wav_path: str) -> None:
    """Play Linux notifications outside the tracked speech process so they overlap speech
    and survive interrupt. Windows file notifications use the TTS queue instead.
    """
    try:
        # Set the playback deadline from the sound duration.
        result = subprocess.run(["aplay", "-q", "--", wav_path],
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                timeout=max(60.0, _wav_seconds(wav_path) * 1.5 + 5),
                                env=proc_env.child_env())
        _log_notification_result(result)
    except FileNotFoundError:
        _aplay_missing()
    except subprocess.TimeoutExpired:
        print("[tts] notification playback timed out; killed aplay",
              file=sys.stderr)


def _play_wav_bytes(wav: "bytes | None", volume: float = 1.0,
                    gen: "int | None" = None) -> bool:
    """Play bytes through the tracked path. Return false for fallback when no audio is
    available.
    """
    if not wav:
        return False
    wav_path = None
    try:
        fd, wav_path = tempfile.mkstemp(suffix=".wav")
        os.close(fd)
        Path(wav_path).write_bytes(wav)
        eff = volume * _master_volume
        if abs(eff - 1.0) > 0.01:
            _apply_volume(wav_path, eff)
        _play_wav(wav_path, gen)
        return True
    except Exception:  # noqa: BLE001
        return False
    finally:
        if wav_path:
            try:
                Path(wav_path).unlink(missing_ok=True)
            except OSError:
                pass


def _kokoro_speak(text: str, volume: float = 1.0, speed: float = 1.0,
                  gen: "int | None" = None) -> bool:
    """Synthesize Japanese with Kokoro using a kana reading when available. Return false
    for system voice fallback.
    """
    wav = _kokoro_synth(text, speed)
    if gen is not None and gen != _generation:
        # Treat interrupted synthesis as handled so it cannot trigger fallback.
        return True
    return _play_wav_bytes(wav, volume, gen)


def _pipeline(text: str, volume: float = 1.0, speed: float = 1.0,
              reading: "str | None" = None, gen: "int | None" = None) -> None:
    global _piper_voice, _piper_failed
    # Skip muted synthesis because minimum backend volume may still be audible.
    if volume * _master_volume <= 0.0:
        return
    if gen is None:
        gen = _generation
    # Snapshot the reading map once. Setters replace the dictionary without mutating the
    # published instance.
    if reading is None:
        readings = _READINGS
        if readings:
            reading = readings.get(text)
    # Try Kokoro with the kana reading before system voice fallback.
    if has_japanese(text) and _jp_neural and _kokoro_speak(reading or text, volume, speed, gen):
        return
    # Try the system voice when selected or required for Japanese. Japanese must never
    # fall through to English Piper.
    if (_engine == "system" or (_jp_auto and has_japanese(text))) \
            and _system_speak(text, volume, speed, reading, gen):
        return

    wav_path = None
    try:
        voice = _load_piper()
        if voice is None:
            with _piper_lock:
                failed = _piper_failed
            if failed:
                log_drop("tts-piper", f"piper voice unavailable (sticky load failure); not spoken: {text[:60]!r}")
            else:
                # Settings invalidated this build. The next callout uses the new inputs.
                log_drop("tts-piper", f"piper voice build superseded by a settings change; not spoken: {text[:60]!r}")
            return

        fd, wav_path = tempfile.mkstemp(suffix=".wav")
        os.close(fd)

        # Piper 1.4 uses SynthesisConfig for speed.
        syn_config = None
        if abs(speed - 1.0) > 0.01:
            from piper.config import SynthesisConfig
            syn_config = SynthesisConfig(length_scale=1.0 / max(0.1, speed))

        # Synthesize in memory before creating a temporary file so a stalled thread
        # cannot retain an undeletable Windows file handle.
        def _synth() -> bytes:
            buf = io.BytesIO()
            with wave.open(buf, "wb") as wf:
                voice.synthesize_wav(text, wf, syn_config=syn_config)
            return buf.getvalue()

        ok, wav = _synth_call(_synth)
        if not ok:
            # Retire only the voice that timed out so future callouts do not repeat a
            # stalled session.
            with _piper_lock:
                if _piper_voice is voice:
                    _piper_failed = True
                    _piper_voice = None
            log_drop("tts-piper",
                     f"piper synthesis hung {_SYNTH_TIMEOUT_S}s; engine failed, callout dropped: {text[:60]!r}")
            return

        Path(wav_path).write_bytes(wav)

        effective_volume = volume * _master_volume
        if abs(effective_volume - 1.0) > 0.01:
            _apply_volume(wav_path, effective_volume)

        if gen != _generation:
            log_drop("tts-interrupt",
                     f"callout cut off by a newer interrupt: {text[:60]!r}")
            return
        _play_wav(wav_path, gen)

    finally:
        if wav_path:
            try:
                Path(wav_path).unlink(missing_ok=True)
            except OSError:
                pass
