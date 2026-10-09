import array
import hashlib
import io
import os
import re
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

from nyaatriggers.paths import bundle_root, default_voice_dir, resolve_voice_venv, voice_site_packages

from nyaatriggers import proc_env
from nyaatriggers.locale_util import has_japanese
from nyaatriggers.drop_log import log_drop
from nyaatriggers.diagnostics import record, record_exception
from nyaatriggers.http_fetch import ReadTimeout, copy_response, open_response

try:
    import numpy as _np
except Exception:
    _np = None

_venv_sps: list[str] = []   # site-packages entries we inserted, swapped around by set_venv_path
_stale_venv_sps: set[str] = set()   # entries a previous venv used, their leftover modules get purged
if not getattr(sys, 'frozen', False):
    _FFXIV_VENV = Path.home() / ".venv" / "ffxiv"
    _sp_paths = voice_site_packages(_FFXIV_VENV)
    for _sp in _sp_paths:
        if _sp not in sys.path:
            sys.path.insert(0, _sp)
            _venv_sps.append(_sp)

_BASE        = bundle_root()
_VOICES      = _BASE / "voices"
_PIPER_MODEL = default_voice_dir() / "en_US-arctic-medium.onnx"

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
# Interrupted synthesis must not trigger fallback playback.
_interrupted_proc: "subprocess.Popen | None" = None

_queue          = Queue(maxsize=64)
# Serialize eviction and insertion so competing callers do not evict twice.
_enqueue_lock   = threading.Lock()
_notification_slots = threading.BoundedSemaphore(4)
_notification_epoch = 0
_notification_procs: set = set()
_worker_started = threading.Event()
_worker_lock    = threading.Lock()
_master_volume: float = 1.0
_speech_suspended = False
_generation: int = 0

_engine: str = "system" if platform.system() == "Windows" else "piper"

_jp_voice: str = ""
_jp_auto: bool = True

_READINGS: dict = {}
# Match has_japanese so espeak never receives unsupported kanji.
_KANJI = re.compile(r"[\u3005\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\U00020000-\U0002a6df\U0002a700-\U0002ceaf]")

_jp_neural: bool = False
_jp_neural_voice: str = "jf_alpha"
_kokoro = None
_kokoro_failed = False   # sticky failed load marker, cleared when the model is re-downloaded
# Retry failed imports only after model setup.
_kokoro_import_failed = False
_kokoro_lock = threading.Lock()
# Keep slow session construction separate from GUI settings locks.
_kokoro_build_lock = threading.Lock()
_kokoro_epoch: int = 0



def set_master_volume(v: float) -> None:
    global _master_volume
    previous = _master_volume
    _master_volume = max(0.0, min(2.0, v))
    if previous > 0 and _master_volume == 0:
        interrupt()
        _stop_notifications()


def set_engine(name: str) -> None:
    global _engine
    _engine = "system" if str(name).lower() == "system" else "piper"


def default_engine() -> str:
    return "system" if platform.system() == "Windows" else "piper"


def set_jp_voice(name: str) -> None:
    """An empty token selects the default Japanese system voice."""
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

_KOKORO_MAX_BYTES = 1 << 30

_KOKORO_DL_DEADLINE_S = 30 * 60
_KOKORO_DL_STALL_S = 60


def _venv_python() -> str:
    """Reject incomplete voice environments to avoid installing into the wrong interpreter."""
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
    """Return success and the installer output tail."""
    if getattr(sys, "frozen", False):
        return True, "bundled"
    try:
        from install import run_setup_command, setup_lock

        # Kokoro uses the voice environment, outside requirements.txt.
        with setup_lock():
            r = run_setup_command(
                [_venv_python(), "-m", "pip", "install", "--no-input", "--upgrade", "kokoro-onnx==0.4.7"],
                timeout=timeout, capture_output=True, env=proc_env.child_env())
        tail = (r.stderr or r.stdout or "").strip().splitlines()[-3:]
        return r.returncode == 0, "\n".join(tail)
    except subprocess.CalledProcessError as exc:
        tail = (exc.stderr or exc.stdout or str(exc)).strip().splitlines()[-3:]
        return False, "\n".join(tail)
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def download_kokoro_model() -> bool:
    """Install model files atomically, propagating directory errors."""
    tmp = None
    try:
        _MODEL_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"[tts] kokoro model dir not writable: {exc!r}", file=sys.stderr)
        raise
    try:
        # Check age even for live PIDs because they can be reused.
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
                try:
                    total = int(r.headers.get("Content-Length", 0) or 0)
                except ValueError:
                    total = 0
                try:
                    got = copy_response(r, f, _KOKORO_MAX_BYTES, stall=_KOKORO_DL_STALL_S,
                                        deadline=deadline, chunk_cb=digest.update)
                except ReadTimeout as exc:
                    if exc.stalled:
                        raise OSError(
                            f"download of {dest.name} stalled, no new bytes "
                            f"for {_KOKORO_DL_STALL_S} seconds") from exc
                    raise OSError(
                        f"download of {dest.name} still running past "
                        f"{_KOKORO_DL_DEADLINE_S // 60} min; giving up") from exc
            # Early EOF may not raise.
            if total and got < total:
                raise OSError(f"short read: {got}/{total} bytes for {dest.name}")
            if digest.hexdigest() != _KOKORO_SHA256[dest]:
                raise OSError(f"checksum mismatch for {dest.name}")
            os.replace(tmp, dest)
            tmp = None
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
        with _kokoro_lock:
            _kokoro = None
            _kokoro_epoch += 1


def kokoro_ready() -> bool:
    """Cache dependency failures until model setup clears them."""
    global _kokoro_import_failed
    if not (_KOKORO_MODEL.exists() and _KOKORO_VOICES.exists()):
        return False
    if _kokoro_import_failed:
        return False
    epoch = _kokoro_epoch
    try:
        import kokoro_onnx  # noqa: F401
        return True
    except Exception as exc:  # noqa: BLE001
        with _kokoro_lock:
            if _kokoro_epoch == epoch:
                _kokoro_import_failed = True
        _log_once("kokoro-import", f"[tts] kokoro-onnx unavailable: {exc!r}")
        return False


def _load_kokoro(gen: "int | None" = None):
    """Build outside the GUI lock and publish for unchanged settings. Retain model files on failure."""
    global _kokoro, _kokoro_failed
    if gen is None:
        gen = _generation
    if gen != _generation:
        return None
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
                with _kokoro_lock:
                    if _kokoro_epoch == epoch and gen == _generation:
                        _kokoro_failed = True
                _log_once("kokoro-load", f"[tts] kokoro-onnx import failed: {exc!r}")
                return None
        try:
            ok, kokoro = _synth_call(
                lambda: Kokoro(str(_KOKORO_MODEL), str(_KOKORO_VOICES)), gen)
        except _SynthesisBusy:
            record("tts_backend", gen=gen, backend="kokoro", result="busy")
            log_drop("tts-kokoro", "voice build waiting for previous synthesis calls")
            return None
        except Exception as exc:  # noqa: BLE001
            with _kokoro_lock:
                if _kokoro_epoch == epoch and gen == _generation:
                    _kokoro_failed = True
                    _kokoro = None
            _log_once("kokoro-load", f"[tts] kokoro voice load failed: {exc!r}")
            # Setup retries must not redownload healthy models.
            log_drop("tts-kokoro",
                     "kokoro voice load failed; model files kept, "
                     "use the Download button to clear the failure, or delete "
                     "the model files and Download fetches fresh copies")
            return None
        if not ok:
            if gen != _generation:
                record("tts_backend", gen=gen, backend="kokoro", result="interrupted")
                return None
            with _kokoro_lock:
                if _kokoro_epoch == epoch and gen == _generation:
                    _kokoro_failed = True
                    _kokoro = None
            log_drop("tts-kokoro",
                     f"kokoro voice load hung {_SYNTH_TIMEOUT_S}s; engine failed")
            return None
        with _kokoro_lock:
            if _kokoro_epoch == epoch:
                _kokoro = kokoro
            return _kokoro


def _kokoro_synth(text: str, speed: float = 1.0, gen: "int | None" = None) -> "bytes | None":
    """Use kana for Japanese synthesis. Return None for system voice fallback."""
    global _kokoro, _kokoro_failed
    record("tts_backend", gen=gen, backend="kokoro", result="attempt")
    if gen is not None and gen != _generation:
        record("tts_backend", gen=gen, backend="kokoro", result="stale")
        return None
    k = _load_kokoro(gen)
    if k is None or (gen is not None and gen != _generation):
        record("tts_backend", gen=gen, backend="kokoro",
               result="unavailable" if k is None else "superseded")
        return None
    try:
        import numpy as np
        ok, out = _synth_call(lambda: k.create(text, voice=_jp_neural_voice,
                                               speed=min(2.0, max(0.5, speed)), lang="ja"), gen)
        if gen is not None and gen != _generation:
            if not ok:
                with _kokoro_lock:
                    if _kokoro is k:
                        _kokoro = None
            record("tts_backend", gen=gen, backend="kokoro", result="interrupted")
            return None
        if not ok:
            record("tts_backend", gen=gen, backend="kokoro", result="timed_out")
            # Retire only the timed-out session to avoid repeating stalled synthesis.
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
    except _SynthesisBusy:
        record("tts_backend", gen=gen, backend="kokoro", result="busy")
        log_drop("tts-kokoro", "previous synthesis calls still running; callout used system voice")
        return None
    except Exception as exc:  # noqa: BLE001
        _log_once("kokoro-synth", f"[tts] kokoro synthesis failed: {exc!r}")
        return None


def set_jp_auto(on: bool) -> None:
    global _jp_auto
    _jp_auto = bool(on)


def set_readings(readings: dict) -> None:
    """Workers retain snapshots, so never mutate a published reading map."""
    global _READINGS
    _READINGS = {k: v for k, v in (readings or {}).items()
                 if isinstance(k, str) and isinstance(v, str) and v}


def reading_for(text: str) -> "str | None":
    """Look up readings before token substitution."""
    return _READINGS.get(text) if text else None



class _StampedItem(tuple):
    """Carry the enqueue generation while preserving tuple equality."""

    def __new__(cls, item, gen):
        self = super().__new__(cls, item)
        self.gen = gen
        return self


def _enqueue(item) -> None:
    with _enqueue_lock:
        if _master_volume <= 0 or _speech_suspended:
            record("tts_queue", gen=_generation, depth=_queue.qsize(), kind=item[0],
                   result="muted" if _master_volume <= 0 else "suspended")
            return
        # Enqueue stamps invalidate even speech already dequeued for synthesis.
        stamped = _StampedItem(item, _generation)
        try:
            _queue.put_nowait(stamped)
            record("tts_queue", gen=_generation, depth=_queue.qsize(), kind=item[0], result="queued")
        except Full:
            try:
                _queue.get_nowait()
                record("tts_queue", gen=_generation, depth=_queue.qsize(), kind=item[0], result="evicted")
                log_drop("tts-overflow", "TTS queue full; dropped the oldest queued callout")
                _queue.put_nowait(stamped)
                record("tts_queue", gen=_generation, depth=_queue.qsize(), kind=item[0], result="queued")
            except (Empty, Full):
                pass


def speak(text: str, volume: float = 1.0, speed: float = 1.0, reading: "str | None" = None) -> None:
    """Queue speech. Supply kana through reading for backends that need it."""
    _ensure_worker()
    _enqueue(("tts", text, volume, speed, reading))
    depth = _queue.qsize()
    if depth >= 4:
        log_drop("tts-backlog", f"TTS queue {depth} deep; callouts speaking late: {text[:60]!r}")


def play_sound(path: str, volume: float = 1.0) -> None:
    _ensure_worker()
    _enqueue(("wav", path, volume))


def play_notification(path: str, volume: float = 1.0) -> None:
    """Linux notifications overlap speech. Windows shares the single winsound channel."""
    if not path or not os.path.exists(path):
        return
    if platform.system() == "Windows":
        try:
            play_sound(path, volume)
        except RuntimeError as exc:
            log_drop("tts-notify", f"notification worker could not start: {exc!r}")
        return
    with _enqueue_lock:
        if _master_volume <= 0 or _speech_suspended:
            return
        epoch = _notification_epoch
        if not _notification_slots.acquire(blocking=False):
            log_drop("tts-notify", "notification chime dropped; too many plays in flight")
            return
    try:
        threading.Thread(target=_notification_worker, args=(path, volume, epoch),
                         daemon=True).start()
    except RuntimeError as exc:
        _notification_slots.release()
        log_drop("tts-notify", f"notification worker could not start: {exc!r}")
    except Exception:
        _notification_slots.release()
        raise


def interrupt(*, wait: bool = False) -> None:
    """Synchronous winsound playback cannot be interrupted."""
    global _generation
    # Acquire the enqueue lock before the process lock to avoid a lock cycle.
    with _enqueue_lock:
        while True:
            try:
                _queue.get_nowait()
            except Empty:
                break
        with _proc_lock:
            _generation += 1
            record("tts_queue", gen=_generation, depth=_queue.qsize(), result="interrupted")
            proc = _current_proc
            if proc is not None:
                global _interrupted_proc
                _interrupted_proc = proc
                try:
                    proc.terminate()
                except OSError:
                    pass
    if wait and proc is not None:
        try:
            proc.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except OSError:
                pass
            try:
                proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                log_drop("tts-shutdown", "speech process did not stop after kill")
        except OSError:
            pass


def suspend() -> None:
    """Stop queued and active speech before program teardown."""
    global _speech_suspended
    with _enqueue_lock:
        _speech_suspended = True
    interrupt(wait=True)
    _stop_notifications(wait=True)


def _stop_notifications(*, wait: bool = False) -> None:
    global _notification_epoch
    with _proc_lock:
        _notification_epoch += 1
        processes = tuple(_notification_procs)
        for process in processes:
            try:
                process.kill()
            except OSError:
                pass
    if wait:
        for process in processes:
            try:
                process.wait(timeout=.5)
            except subprocess.TimeoutExpired:
                try:
                    process.kill()
                    process.wait(timeout=1)
                except (OSError, subprocess.TimeoutExpired):
                    log_drop("tts-shutdown", "notification process did not stop after kill")
            except OSError:
                pass


def resume() -> None:
    """Accept fresh speech after startup or a failed restart."""
    global _speech_suspended
    with _enqueue_lock:
        _speech_suspended = False


def set_model(path: Path) -> None:
    global _PIPER_MODEL, _piper_voice, _piper_failed, _piper_epoch
    with _piper_lock:
        _PIPER_MODEL = Path(path)
        _piper_voice = None
        _piper_failed = False
        _piper_epoch += 1


def _purge_stale_venv_modules() -> None:
    """Changing sys.path does not replace modules already imported from an old environment."""
    venv = globals().get("_FFXIV_VENV")
    if not venv:
        return
    sps = voice_site_packages(venv)
    for name, mod in list(sys.modules.items()):
        mod_file = getattr(mod, "__file__", "") or ""
        if not mod_file:
            continue
        if any(mod_file.startswith(sp + os.sep) for sp in sps):
            continue
        if name.split(".")[0] in ("piper", "onnxruntime") \
                or any(mod_file.startswith(sp + os.sep) for sp in _stale_venv_sps.copy()):
            sys.modules.pop(name, None)


def set_venv_path(path: str) -> None:
    global _FFXIV_VENV, _piper_voice, _piper_failed, _piper_epoch
    global _kokoro, _kokoro_failed, _kokoro_import_failed, _kokoro_epoch
    if getattr(sys, 'frozen', False):
        return
    try:
        new_venv = resolve_voice_venv(path)
    except RuntimeError as exc:
        _log_once("invalid-voice-environment", f"[tts] invalid voice environment, using default: {exc}")
        new_venv = resolve_voice_venv("")
    new_sps = voice_site_packages(new_venv)
    _FFXIV_VENV = new_venv
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
    _purge_stale_venv_modules()
    with _piper_lock:
        _piper_voice = None
        _piper_failed = False
        _piper_epoch += 1
    with _kokoro_lock:
        _kokoro = None
        _kokoro_failed = False
        _kokoro_import_failed = False
        _kokoro_epoch += 1



_logged_once: set = set()


def _log_once(key: str, msg: str) -> None:
    if key not in _logged_once:
        _logged_once.add(key)
        print(msg, file=sys.stderr)


_aplay_missing_logged = False


def _aplay_missing() -> None:
    """Report missing aplay once, including in frozen builds."""
    global _aplay_missing_logged
    if not _aplay_missing_logged:
        _aplay_missing_logged = True
        log_drop("tts-aplay", "aplay not found; install alsa-utils, callouts have no audio",
                 throttle_s=0)


# Bound ONNX calls so stalled synthesis cannot block the speech queue.
_SYNTH_TIMEOUT_S = 60
_synthesis_slots = threading.BoundedSemaphore(2)


class _SynthesisBusy(RuntimeError):
    pass


def _synth_call(fn, gen: "int | None" = None):
    """Bound abandoned synthesis while letting a fresh pull continue."""
    slots = _synthesis_slots if gen is not None else None
    if gen is not None and gen != _generation:
        return False, None
    if slots is not None and not slots.acquire(blocking=False):
        raise _SynthesisBusy("previous synthesis calls are still running")
    box: dict = {}

    def _run() -> None:
        try:
            box["out"] = fn()
        except Exception as exc:   # noqa: BLE001
            box["err"] = exc
        finally:
            if slots is not None:
                slots.release()

    t = threading.Thread(target=_run, daemon=True)
    try:
        t.start()
    except Exception:
        if slots is not None:
            slots.release()
        raise
    deadline = time.monotonic() + _SYNTH_TIMEOUT_S
    while t.is_alive():
        if gen is not None and gen != _generation:
            return False, None
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        t.join(min(0.05, remaining) if gen is not None else remaining)
    if t.is_alive():
        return False, None
    if gen is not None and gen != _generation:
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
        started = time.monotonic()
        try:
            record("tts_queue", gen=gen, depth=_queue.qsize(), kind=item[0], result="dequeued")
            if item[0] == "wav":
                _, path, volume = item
                _play_wav_file(path, volume, gen)
            else:
                _, text, volume, speed, reading = item
                _pipeline(text, volume, speed, reading, gen)
            record("tts_queue", gen=gen, depth=_queue.qsize(), kind=item[0],
                   result="finished", duration_ms=(time.monotonic() - started) * 1000)
        except Exception as exc:  # noqa: BLE001
            record_exception("python_exception", exc, site="tts")
            record("tts_queue", gen=gen, depth=_queue.qsize(), result="failed")
            traceback.print_exc()
            log_drop("tts-error", f"{type(exc).__name__} in the TTS worker: {exc}")


def _system_speak(text: str, volume: float = 1.0, speed: float = 1.0,
                  reading: "str | None" = None, gen: "int | None" = None) -> bool:
    """Japanese must never fall back to an English Piper model."""
    if not text:
        return True
    system = platform.system()
    # Allow volume up to 200 percent where supported. Clamp SAPI at its own limit.
    vol = max(0.0, min(2.0, volume * _master_volume))
    jp = _jp_auto and has_japanese(text)
    # Prefer kana for espeak and treat empty Japanese output as handled.
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
        for backend in ("espeak-ng", "espeak"):
            if not shutil.which(backend):
                continue
            cmd = [backend, "-s", str(max(80, int(175 * speed))),
                   "-a", str(max(0, min(200, int(round(vol * 100)))))]
            if jp:
                cmd += ["-v", "ja"]
            if _run_speak_proc(cmd, linux_text, stdin_text=False, gen=gen):
                return True
        return jp
    except Exception as exc:
        _log_once("system-spawn", f"[tts] system voice spawn failed: {exc!r}")
        return jp


def _run_speak_proc(cmd: list[str], text: str, stdin_text: bool, no_window: bool = False,
                    gen: "int | None" = None) -> bool:
    """Track the child for interruption and reject stale generations before spawning."""
    global _current_proc, _interrupted_proc
    record("tts_backend", gen=gen, backend="system", result="attempt")
    started = time.monotonic()
    kwargs: dict = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
                    "env": proc_env.child_env()}
    if no_window and platform.system() == "Windows":
        kwargs["creationflags"] = 0x08000000
    if stdin_text:
        kwargs["stdin"] = subprocess.PIPE
        # Use UTF-8 even on non-Japanese Windows locales.
        kwargs["text"] = True
        kwargs["encoding"] = "utf-8"
        kwargs["errors"] = "replace"
    else:
        # Disable inherited stdin for text arguments and end option parsing before speech text.
        cmd = cmd + ["--", text]
        kwargs["stdin"] = subprocess.DEVNULL
    with _proc_lock:
        if gen is not None and gen != _generation:
            return True
        if _current_proc is not None and _current_proc.poll() is None:
            log_drop("tts-backend", "previous speech process has not exited; callout dropped")
            return True
        try:
            proc = subprocess.Popen(cmd, **kwargs)
        except OSError as exc:
            record("tts_backend", gen=gen, backend="system", result="unavailable")
            _log_once(f"system-spawn:{cmd[0]}",
                      f"[tts] system voice spawn failed for {cmd[0]!r}: {exc!r}")
            return False
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
                    pass
            except OSError:
                # A broken pipe does not prove exit. Reap before releasing ownership or allowing fallback.
                proc.kill()
                try:
                    proc.communicate(timeout=5)
                except (subprocess.TimeoutExpired, OSError):
                    # Avoid fallback while a child may still be speaking.
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
        try:
            if proc.returncode is None:
                proc.kill()
            proc.wait(timeout=5)
        finally:
            with _proc_lock:
                was_interrupted = _interrupted_proc is proc
                if was_interrupted:
                    _interrupted_proc = None
                if proc.returncode is not None:
                    _current_proc = None
            for pipe in (proc.stdin, proc.stderr):
                if pipe is not None:
                    pipe.close()
    # Intentional termination counts as handled. Real backend failures can use fallback.
    if was_interrupted:
        record("tts_backend", gen=gen, backend="system", result="interrupted")
        return True
    if timed_out:
        record("tts_backend", gen=gen, backend="system", result="timed_out")
        log_drop("tts-backend", f"system TTS wedged; killed after 30s, callout dropped: {text[:60]!r}")
        return True
    if kill_failed:
        record("tts_backend", gen=gen, backend="system", result="failed")
        log_drop("tts-backend", f"system TTS did not finish cleanly; callout dropped: {text[:60]!r}")
        return True
    if proc.returncode != 0:
        log_drop("tts-backend", f"system TTS failed with exit status {proc.returncode}")
    record("tts_backend", gen=gen, backend="system",
           result="finished" if proc.returncode == 0 else "failed", returncode=proc.returncode,
           elapsed_ms=(time.monotonic() - started) * 1000)
    return proc.returncode == 0


def _load_piper(gen: "int | None" = None):
    """Build outside the GUI lock. Return the instance so callers retain it across settings changes."""
    global _piper_voice, _piper_failed
    if gen is None:
        gen = _generation
    if gen != _generation:
        return None
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
            # PiperVoice.load enables ONNX thread spinning, wasting CPU while idle.
            sess_options = onnxruntime.SessionOptions()
            sess_options.add_session_config_entry(
                "session.intra_op.allow_spinning", "0")
            with open(f"{model}.json", "r", encoding="utf-8") as cfg:
                config = PiperConfig.from_dict(json.load(cfg))
            ok, session = _synth_call(lambda: onnxruntime.InferenceSession(
                str(model),
                sess_options=sess_options,
                providers=["CPUExecutionProvider"],
            ), gen)
            if not ok:
                if gen != _generation:
                    record("tts_backend", gen=gen, backend="piper", result="interrupted")
                    return None
                raise TimeoutError(
                    f"piper session build hung past {_SYNTH_TIMEOUT_S}s")
            voice = PiperVoice(config=config, session=session)
        except _SynthesisBusy:
            record("tts_backend", gen=gen, backend="piper", result="busy")
            log_drop("tts-piper", "voice build waiting for previous synthesis calls")
            return None
        except Exception as exc:  # noqa: BLE001
            with _piper_lock:
                if _piper_epoch == epoch and gen == _generation:
                    _piper_failed = True
                    print(f"[tts] piper voice load failed: {exc!r}", file=sys.stderr)
            return None
        with _piper_lock:
            if _piper_epoch == epoch:
                _piper_voice = voice
            return _piper_voice


def _scale_pcm(frames: bytes, sampwidth: int, volume: float) -> "bytes | None":
    """Support unsigned 8-bit and signed 16-bit PCM. Return None for other formats."""
    if sampwidth == 2:
        if len(frames) % 2:
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
    if sampwidth == 1:
        if _np is not None:
            s = (_np.frombuffer(frames, dtype=_np.uint8).astype(_np.float64) - 128.0) * volume
            return _np.clip(_np.rint(s) + 128.0, 0, 255).astype(_np.uint8).tobytes()
        samples = array.array('B', frames)
        for i in range(len(samples)):
            samples[i] = max(0, min(255, int(round((samples[i] - 128) * volume)) + 128))
        return samples.tobytes()
    return None


def _apply_volume(wav_path: str, volume: float) -> bool:
    """Return False for unsupported or damaged WAV data so callers retain native volume."""
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
    """Handle float and extensible WAV formats unsupported by wave. Return zero on failure."""
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
                    data_size = min(chunk_size, max(0, size - src.tell()))
                    src.seek(skip, 1)
                else:
                    src.seek(skip, 1)
            channels = int.from_bytes(fmt[2:4], "little")
            rate = int.from_bytes(fmt[4:8], "little")
            block = int.from_bytes(fmt[12:14], "little")
            bits = int.from_bytes(fmt[14:16], "little")
            if not block and channels and bits:
                block = channels * bits // 8
            if not rate or not block:
                return 0.0
            return data_size / float(rate * block)
    except Exception:  # noqa: BLE001
        return 0.0


def _wav_seconds(source) -> float:
    try:
        src = io.BytesIO(source) if isinstance(source, bytes) else source
        with wave.open(src, "rb") as w:
            return w.getnframes() / float(w.getframerate() or 1)
    except Exception:  # noqa: BLE001
        # Try RIFF parsing because wave does not support every playable format.
        return _riff_wav_seconds(source)


def _play_winsound(source, flags: int, gen: "int | None" = None) -> None:
    """Bound driver stalls and propagate completed playback errors."""
    import winsound
    box: dict = {}
    started = time.monotonic()
    record("tts_backend", gen=gen, backend="winsound", result="attempt")

    def _run() -> None:
        try:
            # Recheck the generation when playback starts, then release the lock for interrupt.
            with _proc_lock:
                if gen is not None and gen != _generation:
                    box["stale"] = True
                    return
            winsound.PlaySound(source, flags)
        except Exception as exc:   # noqa: BLE001
            box["err"] = exc

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(max(60.0, _wav_seconds(source) * 1.5 + 5))
    if t.is_alive():
        record("tts_backend", gen=gen, backend="winsound", result="timed_out")
        log_drop("tts-playback",
                 "winsound hung on the audio device; playback abandoned, callout had no audio")
        return
    if "err" in box:
        record("tts_backend", gen=gen, backend="winsound", result="failed")
        raise box["err"]
    record("tts_backend", gen=gen, backend="winsound",
           result="stale" if box.get("stale") else "finished",
           elapsed_ms=(time.monotonic() - started) * 1000)


def _play_wav(wav_path: str, gen: "int | None" = None) -> None:
    """Check the interrupt generation under the process lock before spawning."""
    global _current_proc, _interrupted_proc
    system = platform.system()
    if system == "Windows":
        import winsound
        _play_winsound(wav_path, winsound.SND_FILENAME | winsound.SND_NODEFAULT, gen)
        return
    player = ["aplay", "-q", "--", wav_path]
    record("tts_backend", gen=gen, backend="aplay", result="attempt")
    with _proc_lock:
        if gen is not None and gen != _generation:
            record("tts_backend", gen=gen, backend="aplay", result="stale")
            log_drop("tts-interrupt",
                     "callout cut off by a newer interrupt before playback started")
            return
        if _current_proc is not None and _current_proc.poll() is None:
            log_drop("tts-playback", "previous speech process has not exited; callout dropped")
            return
        try:
            proc = subprocess.Popen(player, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.PIPE,
                                    env=proc_env.child_env())
        except FileNotFoundError:
            record("tts_backend", gen=gen, backend="aplay", result="unavailable")
            _aplay_missing()
            return
        _current_proc = proc
    err = b""
    timed_out = False
    started = time.monotonic()
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
            pass
    finally:
        try:
            if proc.returncode is None:
                proc.kill()
            proc.wait(timeout=5)
        finally:
            with _proc_lock:
                was_interrupted = _interrupted_proc is proc
                if was_interrupted:
                    _interrupted_proc = None
                if proc.returncode is not None:
                    _current_proc = None
            if proc.stderr is not None:
                proc.stderr.close()
    # Intentional interruption is not a playback failure.
    if was_interrupted:
        record("tts_backend", gen=gen, backend="aplay", result="interrupted")
        return
    if timed_out:
        record("tts_backend", gen=gen, backend="aplay", result="timed_out")
        log_drop("tts-playback",
                 f"aplay hung {play_timeout:.0f}s on the audio device; killed, callout had no audio")
        return
    if proc.returncode != 0:
        record("tts_backend", gen=gen, backend="aplay", result="failed", returncode=proc.returncode)
        lines = (err or b"").decode(errors="replace").strip().splitlines()
        detail = lines[-1][:160] if lines else f"exit {proc.returncode}"
        log_drop("tts-playback",
                 f"aplay exit {proc.returncode}; callout had no audio: {detail}")
        return
    # An early successful exit may mean discarded audio.
    elapsed = time.monotonic() - started
    record("tts_backend", gen=gen, backend="aplay",
           result="early_exit" if duration and elapsed < duration * 0.5 else "finished",
           elapsed_ms=elapsed * 1000, duration_ms=duration * 1000)
    if duration and elapsed < duration * 0.5:
        log_drop("tts-playback",
                 f"aplay exited 0 after {elapsed:.2f}s for a {duration:.2f}s wav; "
                 "the device likely discarded the audio")


_MAX_SOUND_BYTES = 32 << 20


def _copy_sound_to_tmp(path: str) -> "str | None":
    """Bound copies of growing files. Log failures and return None."""
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
    # The source may grow after the size check.
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
    # Direct calls need generation checks after file validation and copying too.
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


def _notification_worker(path: str, volume: float, epoch: "int | None" = None) -> None:
    tmp_path = None
    if epoch is None:
        epoch = _notification_epoch
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
            if not os.path.isfile(path):
                log_drop("tts-notify", f"not a regular file; not played: {path!r}")
                return
            if os.path.getsize(path) == 0:
                log_drop("tts-notify", f"empty sound file; not played: {path!r}")
                return
            if os.path.getsize(path) > _MAX_SOUND_BYTES:
                log_drop("tts-notify", f"sound file over {_MAX_SOUND_BYTES >> 20} MiB; not played: {path!r}")
                return
        _play_wav_detached(play_path, epoch)
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


def _play_wav_detached(wav_path: str, epoch: "int | None" = None) -> None:
    if epoch is None:
        epoch = _notification_epoch
    command = ["aplay", "-q", "--", wav_path]
    try:
        with _proc_lock:
            if epoch != _notification_epoch or _master_volume <= 0 or _speech_suspended:
                return
            process = subprocess.Popen(command, stdout=subprocess.DEVNULL,
                                       stderr=subprocess.PIPE, env=proc_env.child_env())
            _notification_procs.add(process)
    except FileNotFoundError:
        _aplay_missing()
        return
    timed_out = False
    try:
        _, error = process.communicate(timeout=max(60.0, _wav_seconds(wav_path) * 1.5 + 5))
        if epoch == _notification_epoch:
            _log_notification_result(subprocess.CompletedProcess(
                command, process.returncode, stderr=error))
    except subprocess.TimeoutExpired:
        timed_out = True
    finally:
        try:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
        finally:
            with _proc_lock:
                if process.poll() is not None:
                    _notification_procs.discard(process)
            if process.stderr is not None:
                process.stderr.close()
    if timed_out:
        print("[tts] notification playback timed out; killed aplay",
              file=sys.stderr)


def _play_wav_bytes(wav: "bytes | None", volume: float = 1.0,
                    gen: "int | None" = None) -> bool:
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
    wav = _kokoro_synth(text, speed, gen)
    if gen is not None and gen != _generation:
        return True
    return _play_wav_bytes(wav, volume, gen)


def _pipeline(text: str, volume: float = 1.0, speed: float = 1.0,
              reading: "str | None" = None, gen: "int | None" = None) -> None:
    global _piper_voice, _piper_failed
    # Skip muted synthesis because minimum backend volume may still be audible.
    if volume * _master_volume <= 0.0:
        record("tts_backend", gen=gen, backend=_engine, result="muted")
        return
    if gen is None:
        gen = _generation
    if gen != _generation:
        record("tts_backend", gen=gen, backend=_engine, result="stale")
        return
    if reading is None:
        readings = _READINGS
        if readings:
            reading = readings.get(text)
    if has_japanese(text) and _jp_neural and _kokoro_speak(reading or text, volume, speed, gen):
        return
    # Japanese text must never fall back to English Piper.
    if (_engine == "system" or (_jp_auto and has_japanese(text))) \
            and _system_speak(text, volume, speed, reading, gen):
        return

    wav_path = None
    try:
        record("tts_backend", gen=gen, backend="piper", result="attempt")
        voice = _load_piper(gen)
        if gen != _generation:
            record("tts_backend", gen=gen, backend="piper", result="superseded")
            return
        if voice is None:
            with _piper_lock:
                failed = _piper_failed
            record("tts_backend", gen=gen, backend="piper",
                   result="unavailable" if failed else "superseded")
            if failed:
                log_drop("tts-piper", f"piper voice unavailable (sticky load failure); not spoken: {text[:60]!r}")
            else:
                log_drop("tts-piper", f"piper voice build superseded by a settings change; not spoken: {text[:60]!r}")
            return

        fd, wav_path = tempfile.mkstemp(suffix=".wav")
        os.close(fd)

        syn_config = None
        if abs(speed - 1.0) > 0.01:
            from piper.config import SynthesisConfig
            syn_config = SynthesisConfig(length_scale=1.0 / max(0.1, speed))

        # A stalled synthesis thread must not retain a Windows file handle.
        def _synth() -> bytes:
            buf = io.BytesIO()
            with wave.open(buf, "wb") as wf:
                voice.synthesize_wav(text, wf, syn_config=syn_config)
            return buf.getvalue()

        try:
            ok, wav = _synth_call(_synth, gen)
        except _SynthesisBusy:
            record("tts_backend", gen=gen, backend="piper", result="busy")
            log_drop("tts-piper", "previous synthesis calls still running; callout dropped")
            return
        if gen != _generation:
            if not ok:
                with _piper_lock:
                    if _piper_voice is voice:
                        _piper_voice = None
            record("tts_backend", gen=gen, backend="piper", result="interrupted")
            return
        if not ok:
            record("tts_backend", gen=gen, backend="piper", result="timed_out")
            # Retire only the timed-out voice to avoid repeating stalled synthesis.
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
