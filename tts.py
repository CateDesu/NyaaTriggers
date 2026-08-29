"""TTS pipeline, non-blocking.

Stage 1 - piper-tts turns text into WAV, in process via ~/.venv/ffxiv or bundled
Stage 2 - audio playback, platform specific
  Linux   uses aplay from alsa-utils
  Windows uses winsound from the stdlib

All synthesis is queued and consumed serially by one daemon thread.
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

# child_env undoes PyInstaller's library-path injection for spawned children,
# so a system espeak resolves the system libespeak-ng, not the bundled copy.
import proc_env
from locale_util import has_japanese   # no Qt in here, safe on the module load path
from drop_log import log_drop

# Optional fast path for volume scaling. The pure Python loops in _scale_pcm are
# the fallback, so numpy must never become a hard dependency.
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

_BASE        = Path(getattr(sys, '_MEIPASS', Path(__file__).parent))
_VOICES      = _BASE / "voices"
_PIPER_MODEL = _VOICES / "en_US-arctic-medium.onnx"

# Runtime-downloaded models, Kokoro, live here. On a frozen build _VOICES sits
# inside _internal/, which every self-update replaces wholesale, so a model saved
# there would vanish on the next update. Keep downloads next to the exe instead,
# which an update leaves alone, the same place the app keeps its user data.
# Source runs have no _internal, so this is just the repo voices/ folder.
_MODEL_DIR = (Path(sys.executable).parent / "voices"
              if getattr(sys, 'frozen', False) else _VOICES)

_piper_voice: object | None = None
_piper_failed = False   # sticky failed load marker, cleared when the model or venv changes
_piper_lock  = threading.Lock()
# Serializes the slow session build only. The GUI setters never take this one,
# so changing the voice or venv mid build no longer stalls the GUI thread
# behind a cold multi second onnxruntime session build.
_piper_build_lock = threading.Lock()
# Bumped by set_model and set_venv_path. A build that started before the bump
# is for stale inputs and its result is discarded instead of published.
_piper_epoch: int = 0
_proc_lock   = threading.Lock()
_current_proc: "subprocess.Popen | None" = None
# The proc interrupt killed on purpose. _run_speak_proc treats that proc's
# nonzero exit as handled, no Piper fallback, unlike a genuine synth failure.
_interrupted_proc: "subprocess.Popen | None" = None

# Bounded so a stalled worker, wedged synth or playback, can't let queued
# callouts grow forever. _enqueue drops the oldest when full.
_queue          = Queue(maxsize=64)
# Serializes _enqueue's drop-oldest check, so two full-queue callers can't
# each evict an item for one free slot.
_enqueue_lock   = threading.Lock()
# In-flight notification chimes. Each holds a thread and an aplay, so a wedged
# device would otherwise pile them up one per alert until the 60 s timeouts.
_notification_slots = threading.BoundedSemaphore(4)
_worker_started = threading.Event()
_worker_lock    = threading.Lock()
_master_volume: float = 1.0
# Bumped by interrupt. Synthesis paths capture it before synthesizing and drop
# the result if it moved, so a callout that was mid-synthesis when the interrupt
# landed, untrackable by _current_proc, is not played late.
_generation: int = 0

# TTS backend, "piper" for offline neural or "system" for the OS default voice.
# Windows defaults to system so it works with no model download. main_window
# overrides this from the saved setting.
_engine: str = "system" if platform.system() == "Windows" else "piper"

# Japanese system voice. Piper is English-only, so Japanese callout text always
# routes through the OS voice. _jp_voice is an explicit voice token, "" lets the
# OS pick one. _jp_auto routes any text with kana or kanji to it. On by default,
# harmless for English text since has_japanese never matches, so localized
# callouts speak right out of the box. main_window overrides both from settings.
_jp_voice: str = ""
_jp_auto: bool = True

# Kana readings for known Japanese callout displays, display_with_kanji -> kana,
# so espeak, which can't read kanji, speaks the kana form. main_window pushes its
# loaded map here once at startup. speak then auto-applies it for every caller,
# not just the ones that pass reading= explicitly.
_READINGS: dict = {}
# The ideograph set has_japanese routes on, 々 plus Ext A, Unified, compat and
# Ext B through E. A block it misses reaches espeak unstripped and is announced
# as "Chinese letter", the failure this strip exists to prevent.
_KANJI = re.compile(r"[\u3005\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\U00020000-\U0002a6df\U0002a700-\U0002ceaf]")

# Kokoro, an in-process neural Japanese voice in ONNX like the English Piper
# voice, no separate server. Needs `pip install kokoro-onnx` plus the two model
# files in voices/. The 0.4.7 wheel's deps are all wheels themselves, colorlog,
# espeakng-loader, numpy, onnxruntime, phonemizer-fork, so no misaki[ja] and no
# pyopenjtalk source build. Reads the kana readings we feed it via the espeak-ng
# phonemizer. main_window enables it.
_jp_neural: bool = False
_jp_neural_voice: str = "jf_alpha"
_kokoro = None
_kokoro_failed = False   # sticky failed load marker, cleared when the model is re-downloaded
# Sticky failed import marker for kokoro_ready, so a broken dependency does
# not re-run the heavy import on the GUI thread on every check. Cleared with
# the load marker when the model is re-downloaded.
_kokoro_import_failed = False
_kokoro_lock = threading.Lock()
# Serializes the slow session build only. set_jp_neural never takes this one,
# so turning the voice off mid build no longer stalls the GUI thread behind a
# cold multi second onnxruntime session build.
_kokoro_build_lock = threading.Lock()
# Bumped by set_jp_neural when it drops the built session. A build that
# started before the bump is discarded instead of published and leaves the
# failed marker alone.
_kokoro_epoch: int = 0


# ══════════════════════════════════════════════════════════════════════════════
# Public API
# ══════════════════════════════════════════════════════════════════════════════

def set_master_volume(v: float) -> None:
    global _master_volume
    # 2.0 is the UI slider's 200% ceiling. Anything higher just clips.
    _master_volume = max(0.0, min(2.0, v))


def set_engine(name: str) -> None:
    """Select the TTS backend, 'piper' or 'system'."""
    global _engine
    _engine = "system" if str(name).lower() == "system" else "piper"


def default_engine() -> str:
    """Default backend per platform. Windows gets its built-in voice, the rest get Piper."""
    return "system" if platform.system() == "Windows" else "piper"


def set_jp_voice(name: str) -> None:
    """Explicit system Japanese voice token, a SAPI voice name on Windows or an
    espeak/spd-say ja tag on Linux. Empty lets the OS choose a Japanese voice.
    Only pass tokens from list_jp_voices. The name is interpolated into the
    Windows PowerShell command."""
    global _jp_voice
    _jp_voice = str(name or "")


# Kokoro model files are downloaded at runtime into the persistent model dir,
# next to the exe on frozen builds, so a self-update does not wipe them.
_KOKORO_MODEL  = _MODEL_DIR / "kokoro-v1.0.onnx"
_KOKORO_VOICES = _MODEL_DIR / "voices-v1.0.bin"


_KOKORO_URLS = {
    _KOKORO_MODEL:  "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.onnx",
    _KOKORO_VOICES: "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin",
}

# SHA256 of the pinned release assets above, so a corrupted or tampered download
# is rejected instead of installed. The URLs point at a fixed release tag, so
# these never legitimately change without the URLs changing too.
_KOKORO_SHA256 = {
    _KOKORO_MODEL:  "7d5df8ecf7d4b1878015a32686053fd0eebe2bc377234608764cc0ef3636a6c5",
    _KOKORO_VOICES: "bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d",
}

# Hard ceiling on one download. The model is ~330 MB, so a stream running past
# 1 GiB has a lying Content-Length or no end at all. The short-read check below
# only fires when the server gave a length, so without this cap an endless
# stream would fill the disk.
_KOKORO_MAX_BYTES = 1 << 30

# Total deadline per file. The socket timeout bounds each read, not the whole
# transfer, so a peer trickling one byte at a time would otherwise hold the
# setup worker, and its stuck button state, forever.
_KOKORO_DL_DEADLINE_S = 30 * 60
# Stall window for the same transfer. The read loop runs on a daemon helper
# while the caller enforces the window and the deadline from outside the
# read, the same guard main.py runs for its downloads.
_KOKORO_DL_STALL_S = 60


def _unblock_reader(resp) -> None:
    """Shut the underlying socket down so a read parked in another thread
    wakes at once. A plain resp.close from this side would block on the
    buffer lock the parked read still holds. Best effort, the reader is a
    daemon thread either way."""
    try:
        resp.fp.raw._sock.shutdown(socket.SHUT_RDWR)
    except Exception:  # noqa: BLE001
        pass


def _venv_python() -> str:
    """The Python that has the app's TTS packages, the piper venv on source
    runs. With no venv configured the app Python is the fallback. A configured
    venv with no interpreter raises instead, falling back there would let
    install_kokoro_deps pip install kokoro-onnx into the app interpreter,
    which requirements.txt deliberately omits."""
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
    """pip-install kokoro-onnx into the app's venv, source runs only. Returns
    ok and the log tail. kokoro-onnx pulls the espeak-ng phonemizer, which reads
    the kana readings we feed it, so no misaki[ja]/pyopenjtalk C build is needed."""
    if getattr(sys, "frozen", False):
        # The frozen build bundles kokoro-onnx and the espeak-ng phonemizer, so
        # there is nothing to install here. Only the voice model still downloads.
        return True, "bundled"
    try:
        # The pin lives here because requirements.txt deliberately omits
        # kokoro, which the app interpreter never imports.
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
    """Download the Kokoro model files, ~330 MB, into the persistent model dir. True
    on success. Writes to a .part then renames, so an interrupted download can't
    leave a half file. An OSError creating the model dir itself, say a protected
    install location, propagates so the caller's status text carries the real
    reason instead of the generic connection hint."""
    tmp = None
    try:
        _MODEL_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        # A protected install dir is no network problem. Keep the real reason
        # on its way to the caller's status text.
        print(f"[tts] kokoro model dir not writable: {exc!r}", file=sys.stderr)
        raise
    try:
        # A hard kill mid download leaks its .part, unique per pid so two app
        # instances can't interleave writes into one file, same idiom as
        # install.py. A dead owner's file goes now. A live pid only proves some
        # process holds that number, Linux recycles pids, so a live probe still
        # falls through to the age check. A genuinely in-flight download
        # touches its .part constantly and reads as fresh.
        for stale in _MODEL_DIR.glob("*.part"):
            try:
                pid = int(stale.name.rsplit(".", 2)[1])
            except (IndexError, ValueError):
                continue   # not one of our .part names, leave it alone
            if platform.system() == "Windows":
                # os.kill has no pure probe there, sig 0 terminates. Age only.
                dead = False
            else:
                try:
                    os.kill(pid, 0)
                    dead = False   # may be a recycled pid, age decides below
                except ProcessLookupError:
                    dead = True
                except OSError:
                    dead = False   # probe inconclusive, fall back to age
            if not dead:
                try:
                    if time.time() - stale.stat().st_mtime < 3600:
                        continue   # fresh enough to belong to a live run
                except OSError:
                    continue   # vanished mid sweep
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
            with urllib.request.urlopen(req, timeout=120) as r, open(tmp, "wb") as f:
                # A junk length from a proxy reads as unknown. The byte cap
                # below still bounds the download.
                try:
                    total = int(r.headers.get("Content-Length", 0) or 0)
                except ValueError:
                    total = 0
                # Read loop on a daemon helper, watchdog here. The per read
                # socket timeout resets on every received byte, so a peer
                # trickling one byte at a time can hold one r.read open
                # forever. The stall window and the total deadline are
                # enforced from outside the read.
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
                deadline = time.monotonic() + _KOKORO_DL_DEADLINE_S
                last_seen = progress[0]
                last_change = time.monotonic()
                while not done.wait(timeout=min(_KOKORO_DL_STALL_S, max(0.0, deadline - time.monotonic()))):
                    now = time.monotonic()
                    if progress[0] == last_seen or now > deadline:
                        # Shut the connection down so the parked reader wakes
                        # instead of leaking. A plain r.close here would
                        # block on the lock the parked read still holds.
                        _unblock_reader(r)
                        # Same label rule as updater and install: the stall
                        # line only fits when the whole stall window really
                        # passed with no byte. A wake near the deadline after
                        # less than a full window of quiet is the deadline.
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
            # A dropped connection is a short read with NO exception. Without
            # this check the truncated file would be renamed into place and,
            # since the exists guard above never re-downloads, Kokoro would
            # fail to load forever.
            if total and got < total:
                raise OSError(f"short read: {got}/{total} bytes for {dest.name}")
            if digest.hexdigest() != _KOKORO_SHA256[dest]:
                raise OSError(f"checksum mismatch for {dest.name}")
            os.replace(tmp, dest)
            tmp = None
        # A fresh model just landed. Clear the sticky load and import failure
        # flags so the next synthesis and readiness check retry instead of
        # staying down until restart.
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
    """Turn the in-process Kokoro neural Japanese voice for JP text on or off."""
    global _jp_neural, _jp_neural_voice, _kokoro, _kokoro_epoch
    _jp_neural = bool(enabled)
    _jp_neural_voice = voice or "jf_alpha"
    if not enabled:
        # Drop the built session too, else its ~330 MB of model stays resident
        # until exit. Rebuilt lazily if the voice is turned back on. The epoch
        # bump keeps an in-flight build from publishing the session this call
        # just dropped.
        with _kokoro_lock:
            _kokoro = None
            _kokoro_epoch += 1


def kokoro_ready() -> bool:
    """True when kokoro-onnx is importable and both model files are present.
    A failed import sticks via _kokoro_import_failed, so broken dependencies
    do not re-run the heavy import on the GUI thread on every check. Cleared
    by download_kokoro_model, which the setup flow runs before each re-check."""
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
    """Lazily build the Kokoro engine once. None if kokoro-onnx isn't installed or
    the model files are missing, caller falls back to the OS voice. Never raises.

    A failed load sticks via _kokoro_failed so a transient or corrupt-model
    failure is not retried on every Japanese callout, since each retry rebuilds
    the ~330 MB ONNX session. A hung load or synthesis sticks the same marker,
    see _kokoro_synth. Cleared by download_kokoro_model once it lands.
    A failed build keeps the model files, a transient failure must not destroy
    a healthy ~330 MB download, and the Download button clears the marker
    without refetching them. An import failure is a missing dependency and
    likewise deletes nothing.

    The slow session build runs under _kokoro_build_lock only, so a GUI thread
    set_jp_neural never waits on it. The built engine is published under
    _kokoro_lock, and only when the epoch captured at build start still
    matches, so a set_jp_neural that dropped the session mid build is not
    undone by the late publish. The piper loader guards the same way."""
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
        # set_venv_path purges sys.modules without taking this lock, so a
        # reconfigure mid import can break the import, the same race the
        # piper loader guards with a second purge. Purge here too and retry
        # a failed import once, so that race cannot stick the failed marker.
        # A genuinely missing dependency fails the retry as well and sticks
        # below, same as before.
        _purge_stale_venv_modules()
        try:
            from kokoro_onnx import Kokoro
        except Exception:  # noqa: BLE001
            _purge_stale_venv_modules()
            try:
                from kokoro_onnx import Kokoro
            except Exception as exc:  # noqa: BLE001
                # A missing dependency. The model files are fine, so keep them.
                with _kokoro_lock:
                    _kokoro_failed = True
                _log_once("kokoro-load", f"[tts] kokoro-onnx import failed: {exc!r}")
                return None
        try:
            # The session build runs under the same ceiling as synthesis. A
            # wedged build would block the single TTS worker, and every later
            # callout, forever. The abandoned thread holds no lock.
            ok, kokoro = _synth_call(
                lambda: Kokoro(str(_KOKORO_MODEL), str(_KOKORO_VOICES)))
        except Exception as exc:  # noqa: BLE001
            with _kokoro_lock:
                # Only fail the session still wanted. A set_jp_neural off mid
                # build already dropped it and deserves a fresh attempt.
                if _kokoro_epoch == epoch:
                    _kokoro_failed = True
                    _kokoro = None
            _log_once("kokoro-load", f"[tts] kokoro voice load failed: {exc!r}")
            # Keep the model files. A transient failure, an AV lock or out of
            # memory mid session build, must not destroy a healthy ~330 MB
            # download. The Download button clears the failed marker without
            # refetching files already on disk, so a file that went bad on
            # disk itself has to be deleted by hand first.
            log_drop("tts-kokoro",
                     "kokoro voice load failed; model files kept, "
                     "use the Download button to clear the failure, or delete "
                     "the model files and Download fetches fresh copies")
            return None
        if not ok:
            # A hung load sticks the failed marker like a hung synthesis does,
            # but the model files stay. A hang is no proof they are corrupt,
            # and running the download flow again clears the marker without
            # fetching ~330 MB again.
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
    """Neural JP synthesis via Kokoro -> WAV bytes, or None to fall back. Japanese
    needs misaki[ja], that is pyopenjtalk, for kanji. A missing phonemizer just
    returns None. `speed` honors the per-trigger TTS speed, matching espeak/SAPI/Piper."""
    global _kokoro, _kokoro_failed
    k = _load_kokoro()
    if k is None:
        return None
    try:
        import numpy as np
        # kokoro's create asserts 0.5 <= speed <= 2.0 while the trigger dialog
        # allows 0.5 to 3.0. Clamp into kokoro's range instead of raising the
        # callout into the espeak fallback.
        ok, out = _synth_call(lambda: k.create(text, voice=_jp_neural_voice,
                                               speed=min(2.0, max(0.5, speed)), lang="ja"))
        if not ok:
            # A hang leaves the ONNX session wedged. Stick the failed marker
            # like the load path does and unpublish the session, else every
            # later callout re-enters the hang, burns another
            # _SYNTH_TIMEOUT_S on the worker and leaks one daemon thread.
            # Only fail the session still published, a mid-hang set_jp_neural
            # already dropped it and deserves a fresh attempt.
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
    """When on, text containing kana or kanji is spoken by the Japanese voice
    regardless of the selected engine. Piper can't speak Japanese."""
    global _jp_auto
    _jp_auto = bool(on)


def set_readings(readings: dict) -> None:
    """Push the {display_with_kanji -> kana} map so speak can auto-apply a kana
    reading for any caller, not just those passing reading=. Replaces wholesale.

    Copy-on-write contract. This builds a NEW dict and rebinds the module global
    in one step, and must never mutate the bound dict in place. The TTS worker
    reads through a local snapshot of the reference, so the swap needs no lock."""
    global _READINGS
    _READINGS = {k: v for k, v in (readings or {}).items()
                 if isinstance(k, str) and isinstance(v, str) and v}


def reading_for(text: str) -> "str | None":
    """The kana reading for a known Japanese display, or None. Callers that hold
    the unsubstituted template can resolve its reading here, then substitute the
    same tokens into both the text and the returned reading before speak."""
    return _READINGS.get(text) if text else None



def list_jp_voices() -> list[tuple[str, str]]:
    """Installed Japanese system voices as label/token pairs for a picker.

    Windows enumerates SAPI voices, culture ja*. Linux offers espeak/spd-say
    if present. They synthesize JP via a language flag, no per-voice list.
    Best effort. Any failure yields []."""
    try:
        system = platform.system()
        if system == "Windows":
            # Force UTF-8 on the output half, the speak path already forces it
            # on stdin. The locale default would mojibake a non-ASCII voice
            # name, kanji or kana vendor voices, and SelectVoice would then
            # fail on the token. utf-8-sig tolerates a BOM the console host
            # may prepend.
            ps = (
                "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;"
                "Add-Type -AssemblyName System.Speech;"
                "(New-Object System.Speech.Synthesis.SpeechSynthesizer)."
                "GetInstalledVoices()|%{$i=$_.VoiceInfo;"
                "if($i.Culture.Name -like 'ja*'){$i.Name}}"
            )
            out = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                capture_output=True, text=True, encoding="utf-8-sig", timeout=15,
                creationflags=0x08000000, env=proc_env.child_env()).stdout
            return [(n.strip(), n.strip()) for n in out.splitlines() if n.strip()]
        import shutil
        out = []
        if shutil.which("spd-say"):
            out.append(("speech-dispatcher (Japanese)", "spd:ja"))
        if shutil.which("espeak"):   # mirrors the backend check in _system_speak
            out.append(("espeak (Japanese)", "espeak:ja"))
        return out
    except Exception:
        return []


class _StampedItem(tuple):
    """A queued callout carrying the _generation from its enqueue. The worker
    dispatches on the stamp instead of reading _generation there, so an
    interrupt landing between the queue get and synthesis still vetoes the
    callout. A tuple subclass so queued items keep comparing equal to plain
    tuples."""

    def __new__(cls, item, gen):
        self = super().__new__(cls, item)
        self.gen = gen
        return self


def _enqueue(item) -> None:
    """Enqueue on the bounded _queue, dropping the oldest item when full. Same
    drop-oldest idiom as plugin_link._enqueue. A wedged worker costs a stale
    callout, never a blocked caller. The lock only wraps nonblocking ops."""
    with _enqueue_lock:
        # Stamp now, at enqueue. A generation read at dispatch would either
        # race an interrupt that landed after the dequeue or, read before the
        # blocking get, poison the first callout queued after any interrupt.
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
    """Enqueue text for TTS. Returns right away, playback is non-blocking.
    `reading` is a kana form spoken by voices that can't read kanji like espeak."""
    _ensure_worker()
    _enqueue(("tts", text, volume, speed, reading))
    depth = _queue.qsize()
    if depth >= 4:
        log_drop("tts-backlog", f"TTS queue {depth} deep; callouts speaking late: {text[:60]!r}")


def play_sound(path: str, volume: float = 1.0) -> None:
    """Queue a WAV file for playback. Returns right away."""
    _ensure_worker()
    _enqueue(("wav", path, volume))


def play_notification(path: str, volume: float = 1.0) -> None:
    """Play a short notification WAV.

    On Linux it plays on its own channel, bypassing the serial TTS
    worker and the tracked subprocess on a throwaway daemon thread, so the
    chime overlaps a spoken callout and is not cut off by interrupt.
    Windows has a single process-wide winsound channel, so there the chime
    goes through the TTS worker instead. A detached PlaySound would cut off
    the in-flight callout and vice versa. Missing or empty paths no-op.
    """
    if not path or not os.path.exists(path):
        return
    if platform.system() == "Windows":
        play_sound(path, volume)
        return
    # Each play holds a thread and an aplay until it ends or its 60 s timeout.
    # Bound the pileup a wedged audio device plus a burst of alerts causes.
    if not _notification_slots.acquire(blocking=False):
        log_drop("tts-notify", "notification chime dropped; too many plays in flight")
        return
    try:
        threading.Thread(target=_notification_worker, args=(path, volume),
                         daemon=True).start()
    except Exception:
        _notification_slots.release()
        raise


def play_notification_bytes(wav_bytes: bytes, volume: float = 1.0) -> None:
    """Like play_notification but from in-memory WAV bytes, so no plaintext
    file hits disk on Windows or Linux. Always fires on its own thread, so on
    Windows, with its single process-wide winsound channel, it cuts off any
    in-flight callout rather than overlapping it as on Linux."""
    if not wav_bytes:
        return
    # Same in-flight bound as play_notification. A wedged audio device plus a
    # burst of alerts would otherwise pile up a thread and an aplay per call.
    if not _notification_slots.acquire(blocking=False):
        log_drop("tts-notify", "notification chime dropped; too many plays in flight")
        return
    try:
        threading.Thread(target=_notification_bytes_worker, args=(wav_bytes, volume),
                         daemon=True).start()
    except Exception:
        _notification_slots.release()
        raise


def interrupt() -> None:
    """Clear the queue and stop any currently playing audio.

    Windows WAV and Piper playback goes through winsound.PlaySound, synchronous
    and untracked, so an already-started callout there plays to completion.
    Only queued callouts are dropped.
    """
    global _generation
    # The drain and the bump share _enqueue_lock with _enqueue, so a drop-oldest
    # put can't land after the drain and sneak a pre-interrupt callout past it.
    # This is the only spot nesting the two locks, _enqueue_lock outside
    # _proc_lock, and nothing takes _enqueue_lock while holding _proc_lock, so
    # the order can't cycle.
    with _enqueue_lock:
        while True:
            try:
                _queue.get_nowait()
            except Empty:
                break
        with _proc_lock:
            _generation += 1   # whatever is still mid-synthesis is stale now
            if _current_proc is not None:
                global _interrupted_proc
                _interrupted_proc = _current_proc   # mark its nonzero exit as intentional
                try:
                    _current_proc.terminate()
                except OSError:
                    pass


def set_model(path: Path) -> None:
    """Hot-swap the voice model. Piper reloads on the next synthesis call."""
    global _PIPER_MODEL, _piper_voice, _piper_failed, _piper_epoch
    with _piper_lock:
        _PIPER_MODEL = Path(path)
        _piper_voice = None
        _piper_failed = False
        _piper_epoch += 1   # a build still running is for the old model now


def _purge_stale_venv_modules() -> None:
    """Drop imported modules that did not come from the current venv. A
    sys.path swap alone can't move an already-imported module, it stays bound
    to the old venv through sys.modules. piper pulls numpy and more in with
    it, so matching by package name would always lag the real dependency set.
    Anything imported from a site-packages a past venv used goes, name rule
    or not."""
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
            continue   # bound to the current venv already
        if name.split(".")[0] in ("piper", "onnxruntime") \
                or any(mod_file.startswith(sp + os.sep) for sp in _stale_venv_sps.copy()):
            # The stale set is copied, set_venv_path grows it from the GUI
            # thread while the worker runs this scan. set.copy is one C call,
            # so the snapshot itself cannot race the update.
            del sys.modules[name]


def set_venv_path(path: str) -> None:
    """Reconfigure the piper venv path, source installs only."""
    global _FFXIV_VENV, _piper_voice, _piper_failed, _piper_epoch
    if getattr(sys, 'frozen', False):
        return
    new_venv = Path(path)
    # Rebind the global too. _venv_python is the Kokoro installer's only
    # selector and it reads the global, not sys.path, so without this a
    # same-session install still targets the previous venv.
    _FFXIV_VENV = new_venv
    new_sps  = glob.glob(str(new_venv / "lib" / "python*" / "site-packages"))
    new_sps += glob.glob(str(new_venv / "Lib" / "site-packages"))
    # Drop the entries a previous configure inserted, so repeated reconfigures
    # don't pile up in sys.path and a stale venv can't shadow the new one.
    for old in _venv_sps:
        try:
            sys.path.remove(old)
        except ValueError:
            pass
    _stale_venv_sps.update(_venv_sps)   # their imported modules are purge candidates now
    _venv_sps.clear()
    for sp in new_sps:
        if sp not in sys.path:
            sys.path.insert(0, sp)
            _venv_sps.append(sp)
    # sys.path alone can't switch piper. Already-imported piper and onnxruntime
    # modules stay bound to the old venv through sys.modules, so purge any that
    # did not come from the new venv and let the next synthesis re-import them.
    _purge_stale_venv_modules()
    with _piper_lock:
        _piper_voice = None
        _piper_failed = False
        _piper_epoch += 1   # a build still running is for the old venv now


# ══════════════════════════════════════════════════════════════════════════════
# Internal
# ══════════════════════════════════════════════════════════════════════════════

_logged_once: set = set()


def _log_once(key: str, msg: str) -> None:
    """Log `msg` to stderr the first time `key` is seen. For failures that
    would otherwise repeat on every callout."""
    if key not in _logged_once:
        _logged_once.add(key)
        print(msg, file=sys.stderr)


_aplay_missing_logged = False   # one drop line per process is enough


def _aplay_missing() -> None:
    """aplay is a hard Linux requirement with no presence check, and a frozen
    build has no console for the spawn error to reach. Leave one drop line per
    process instead of an invisible stderr print per callout."""
    global _aplay_missing_logged
    if not _aplay_missing_logged:
        _aplay_missing_logged = True
        log_drop("tts-aplay", "aplay not found; install alsa-utils, callouts have no audio",
                 throttle_s=0)


# Ceiling for one in-process ONNX call, a synthesis or a session build, kokoro
# or piper. Every subprocess path has a 30 or 60 s bound, and without the same
# here a hung ONNX call wedges the single TTS worker, and every later callout,
# forever.
_SYNTH_TIMEOUT_S = 60


def _synth_call(fn):
    """Run one in-process ONNX call, synthesis or session build, under the
    _SYNTH_TIMEOUT_S ceiling on a one-shot daemon thread, so a hang is
    abandoned instead of wedging the worker. Returns ok and the result, False
    and None on timeout. A failure inside fn is re-raised here so callers keep
    their usual handling."""
    box: dict = {}

    def _run() -> None:
        try:
            box["out"] = fn()
        except Exception as exc:   # noqa: BLE001 - re-raised on the caller's thread
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
        gen = getattr(item, "gen", None)   # the stamp _enqueue attached
        try:
            if item[0] == "wav":
                _, path, volume = item
                _play_wav_file(path, volume, gen)
            else:
                _, text, volume, speed, reading = item
                _pipeline(text, volume, speed, reading, gen)
        except Exception as exc:  # noqa: BLE001 - one bad item must not kill the worker
            traceback.print_exc()
            log_drop("tts-error", f"{type(exc).__name__} in the TTS worker: {exc}")


def _system_speak(text: str, volume: float = 1.0, speed: float = 1.0,
                  reading: "str | None" = None, gen: "int | None" = None) -> bool:
    """Speak via the OS's built-in TTS. Returns False if no system backend
    exists on this platform, caller falls back to Piper. Runs as a tracked
    subprocess so interrupt can stop it. `gen` rides through to the spawn so
    an interrupt during backend selection vetoes the stale callout."""
    if not text:
        return True
    system = platform.system()
    # Backends with headroom honor the slider's 200% ceiling, espeak -a runs
    # to 200 and spd-say -i to +100. SAPI caps at 100, clamped at the spawn.
    vol = max(0.0, min(2.0, volume * _master_volume))
    # Route kana/kanji text to the Japanese voice, Piper can't speak it. Selection
    # is best effort. A missing JP voice degrades to the OS default rather than
    # raising, so we never fall through to Piper and mangle Japanese into English.
    jp = _jp_auto and has_japanese(text)
    # espeak on Linux can't read kanji. It announces each as "Chinese letter", so
    # on that path speak the kana `reading` instead. Windows SAPI reads kanji.
    # If no reading is known, strip residual kanji from the espeak input so it reads
    # the surrounding kana/ASCII instead of spamming "Chinese letter". A kanji-only
    # string with no reading has nothing espeak can speak. Report it handled, True,
    # so the caller does NOT fall through to Piper and mangle Japanese into English.
    linux_text = reading if (jp and reading) else text
    is_linux = system != "Windows"
    if is_linux and jp and _KANJI.search(linux_text):
        linux_text = _KANJI.sub("", linux_text).strip()
        if not linux_text:
            log_drop("tts-jp", f"kanji-only text with no kana reading; nothing spoken: {text[:60]!r}")
            return True
    try:
        if system == "Windows":
            # System.Speech via PowerShell, present on every Windows. Text goes
            # on stdin to dodge quoting and escaping.
            rate = max(-10, min(10, int(round((speed - 1.0) * 5))))
            sel = ""
            if jp and _jp_voice:
                # Only enumerated voice names reach here. Still double any quote,
                # the PS escape, since the name is interpolated into the command.
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
        # Linux and others. speech-dispatcher or espeak if installed, else Piper.
        # For JP, both synthesize via a language flag, no per-voice list. The
        # _jp_voice token's backend hint is informational. We use whichever exists.
        # A failed spd-say, speech-dispatcher down, falls through to espeak,
        # whose -v ja is a Japanese voice too. Only when no backend is left does
        # JP report handled. Falling through to the English-only Piper model
        # would garble the kana, silence matches the no-backend path below.
        import shutil
        if shutil.which("spd-say"):
            rate = max(-100, min(100, int((speed - 1.0) * 100)))
            # the -i scale runs -100..100 with 0 as normal, so 100% maps to 0
            # and the 200% ceiling to 100.
            cmd = ["spd-say", "-w", "-r", str(rate),
                   "-i", str(max(-100, min(100, int(round((vol - 1.0) * 100)))))]
            if jp:
                cmd += ["-l", "ja"]
            if _run_speak_proc(cmd, linux_text, stdin_text=False, gen=gen):
                return True
        if shutil.which("espeak"):
            # -a takes amplitude 0..200 with 100 as normal, so the 200%
            # ceiling maps to 200.
            cmd = ["espeak", "-s", str(max(80, int(175 * speed))),
                   "-a", str(max(0, min(200, int(round(vol * 100)))))]
            if jp:
                cmd += ["-v", "ja"]
            return _run_speak_proc(cmd, linux_text, stdin_text=False, gen=gen) or jp
        # No system backend on this box. For JP, returning False would drop the
        # kana to the English-only Piper model, garbled. Report handled instead,
        # silent, the alert sound still fires. English still falls to Piper.
        return jp
    except Exception as exc:
        # Same rule as the no-backend path. A spawn failure here, say Popen
        # raising, must not drop JP to the English-only Piper model either.
        # It still leaves a line, JP going silent on this path would otherwise
        # have no diagnostic anywhere.
        _log_once("system-spawn", f"[tts] system voice spawn failed: {exc!r}")
        return jp


def _run_speak_proc(cmd: list[str], text: str, stdin_text: bool, no_window: bool = False,
                    gen: "int | None" = None) -> bool:
    """Run an external TTS command tracked by _current_proc so interrupt can
    terminate it. Text goes on stdin when stdin_text is set, else as the last arg.
    `gen` is the generation _pipeline captured at dequeue. It is re-checked
    under _proc_lock right before the spawn, so an interrupt that landed while
    the backend was being picked vetoes the stale callout instead of playing
    it to completion."""
    global _current_proc, _interrupted_proc
    kwargs: dict = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
                    "env": proc_env.child_env()}
    if no_window and platform.system() == "Windows":
        kwargs["creationflags"] = 0x08000000          # CREATE_NO_WINDOW
    if stdin_text:
        kwargs["stdin"] = subprocess.PIPE
        # Force UTF-8 on stdin. Default text mode uses the ANSI code page, which
        # can't encode Japanese on a non-JP Windows locale. That would raise and
        # the caller would fall to English-only Piper. The PS side sets InputEncoding=UTF8.
        kwargs["text"] = True
        kwargs["encoding"] = "utf-8"
        kwargs["errors"] = "replace"
    else:
        # `--` ends option parsing so a callout that starts with a flag, say
        # espeak-ng's "--phonout=<file>", is spoken as text instead of being
        # parsed as an option. espeak-ng and spd-say both honour it. stdin is
        # DEVNULL so espeak, which reads stdin by default with no -f/--stdin,
        # never inherits the parent's.
        cmd = cmd + ["--", text]
        kwargs["stdin"] = subprocess.DEVNULL
    # Spawn while holding _proc_lock so an interrupt can never land between
    # the spawn and the registration. It would clear the queue but leave this
    # freshly started process speaking to completion.
    with _proc_lock:
        if gen is not None and gen != _generation:
            # The interrupt landed before the spawn. Report handled so the
            # stale callout is dropped, not replayed through Piper.
            return True
        proc = subprocess.Popen(cmd, **kwargs)
        _current_proc = proc
    # Generous ceiling. Callouts are seconds long, but a wedged backend like a
    # hung PowerShell or aplay would otherwise block the single worker thread forever.
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
                    # A D-state backend ignores the kill. Leave the husk
                    # behind rather than hang the single worker on it.
                    pass
            except OSError:
                # BrokenPipeError included, it is an OSError subclass. A broken
                # pipe means the child stopped reading stdin, not that it
                # exited. Same cleanup as the timeout path, kill and re-wait so
                # the child is reaped and returncode gets set. Otherwise the
                # finally clears _current_proc over a still running child and
                # the None returncode reports failure, so _pipeline falls back
                # to Piper and double-speaks over the live backend.
                proc.kill()
                try:
                    proc.communicate(timeout=5)
                except (subprocess.TimeoutExpired, OSError):
                    # The kill did not take, the husk may still be speaking.
                    # Report handled below, the None returncode would read as
                    # a failure and Piper would double-speak over it.
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
    # An interrupt-driven terminate also exits nonzero, but that is intentional.
    # Report it as handled so _pipeline does NOT replay the cut-off callout through
    # Piper. Otherwise report the real exit status, so a genuine synth failure is
    # unhandled and the caller can fall back. PowerShell's SilentlyContinue exits 0,
    # so an uninterrupted Windows run still returns True.
    if was_interrupted:
        return True
    if timed_out:
        # Killed at the deadline, so report handled. Replaying a half-minute-stale
        # callout through Piper would be worse than the silence already suffered.
        log_drop("tts-backend", f"system TTS wedged; killed after 30s, callout dropped: {text[:60]!r}")
        return True
    if kill_failed:
        log_drop("tts-backend", f"system TTS survived the kill; callout dropped: {text[:60]!r}")
        return True
    return proc.returncode == 0


def _load_piper():
    """Load once and return the Piper voice, or None after a failed load. The
    failure sticks until set_model or set_venv_path changes the inputs, so a
    broken install isn't re-attempted on every callout. Returning the instance
    rather than having callers re-read the module global keeps synthesis safe
    when a GUI-thread set_model nulls _piper_voice mid-pipeline.

    The slow session build runs under _piper_build_lock only, so a GUI-thread
    setter never waits on it. It publishes under _piper_lock, and only if the
    inputs it built from are still current."""
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
        # set_venv_path purges sys.modules without taking this lock, so a
        # reconfigure during a running build can leave old venv modules
        # behind, inserted as that build's imports finish. Purge again here
        # so the imports below can't bind to those survivors.
        _purge_stale_venv_modules()
        try:
            import json
            import onnxruntime
            from piper.config import PiperConfig
            from piper.voice import PiperVoice
            # PiperVoice.load builds the onnxruntime session with default
            # SessionOptions, whose intra-op pool SPINS, busy-waits, for the
            # process lifetime. That pegs one core per worker thread even
            # while no synthesis runs and starves the WS feed handler so
            # callouts drop in and out. Build the session ourselves with the
            # spin disabled, then hand it to PiperVoice the same way load
            # does. PiperVoice's espeak/download defaults match load's.
            sess_options = onnxruntime.SessionOptions()
            sess_options.add_session_config_entry(
                "session.intra_op.allow_spinning", "0")
            with open(f"{model}.json", "r", encoding="utf-8") as cfg:
                config = PiperConfig.from_dict(json.load(cfg))
            # The session build runs under the same ceiling as synthesis. A
            # wedged build would block the single TTS worker, and every later
            # callout, forever. The abandoned thread holds no lock.
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
                # Only fail the inputs that actually failed. A setter mid
                # build already cleared the marker for the new inputs. The
                # print is gated the same way, a superseded build's failure
                # says nothing about the current inputs.
                if _piper_epoch == epoch:
                    _piper_failed = True
                    print(f"[tts] piper voice load failed: {exc!r}", file=sys.stderr)
            return None
        with _piper_lock:
            if _piper_epoch == epoch:
                _piper_voice = voice
            return _piper_voice


def _scale_pcm(frames: bytes, sampwidth: int, volume: float) -> "bytes | None":
    """Scale raw PCM frames by `volume`. Handles 8-bit unsigned and 16-bit
    signed PCM. None for unsupported widths like 24/32-bit or float. Uses numpy
    when available, else the pure Python loops."""
    if sampwidth == 2:                              # 16-bit signed
        if len(frames) % 2:                         # corrupt WAV with a stray
            frames = frames[:-1]                    # trailing byte, drop it
        if _np is not None:
            s = _np.frombuffer(frames, dtype="<i2").astype(_np.float64) * volume
            return _np.clip(s, -32768, 32767).astype("<i2").tobytes()
        samples = array.array('h', frames)
        # WAV PCM is little-endian. An array of 'h' uses native byte order, so swap on
        # a big-endian host or the audio is byte-corrupted. The numpy path uses <i2.
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
    """Scale a PCM WAV in place. Handles 8-bit unsigned and 16-bit signed PCM.
    Returns False for unsupported formats, 24/32-bit or float, and for WAVs too
    corrupt or truncated to parse, leaving the file untouched so the caller
    falls back to native-volume playback."""
    try:
        with wave.open(wav_path, 'rb') as r:
            params = r.getparams()
            frames = r.readframes(params.nframes)
    except (wave.Error, EOFError):
        # Corrupt, empty or IEEE-float WAV. Same fallback as an unsupported
        # format, the caller plays it at its native level.
        return False
    scaled = _scale_pcm(frames, params.sampwidth, volume)
    if scaled is None:
        return False
    with wave.open(wav_path, 'wb') as w:
        w.setparams(params)
        w.writeframes(scaled)
    return True


def _riff_wav_seconds(source) -> float:
    """Duration from the RIFF chunks directly, for wavs the wave module
    refuses, IEEE float and extensible formats. Those still play, so their
    runtime has to bound the playback kill and feed the blackhole check the
    same as PCM. 0.0 when the chunks do not parse."""
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
            # Chunks are word aligned, an odd size is followed by a pad byte.
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
                    # The chunk header may claim more than a truncated file
                    # really holds, clamp to the bytes actually there.
                    data_size = min(chunk_size, max(0, size - src.tell()))
                    src.seek(skip, 1)
                else:
                    src.seek(skip, 1)
            channels = int.from_bytes(fmt[2:4], "little")
            rate = int.from_bytes(fmt[4:8], "little")
            block = int.from_bytes(fmt[12:14], "little")
            bits = int.from_bytes(fmt[14:16], "little")
            if not block and channels and bits:
                # Some writers leave block align zero, derive it.
                block = channels * bits // 8
            if not rate or not block:
                return 0.0
            return data_size / float(rate * block)
    except Exception:  # noqa: BLE001 - same contract as _wav_seconds
        return 0.0


def _wav_seconds(source) -> float:
    """Duration of a wav in seconds, 0.0 when unreadable. Takes a path or
    in-memory wav bytes."""
    try:
        src = io.BytesIO(source) if isinstance(source, bytes) else source
        with wave.open(src, "rb") as w:
            return w.getnframes() / float(w.getframerate() or 1)
    except Exception:  # noqa: BLE001 - a missing or corrupt wav reads as 0
        # The wave module only reads PCM. Float and extensible wavs are real
        # playable files, so parse their RIFF chunks before giving up.
        return _riff_wav_seconds(source)


def _play_winsound(source, flags: int) -> None:
    """winsound.PlaySound under a bounded wait. PlaySound is synchronous with
    no timeout of its own, so a wedged waveOut driver would otherwise pin its
    caller forever, the single TTS worker or a chime slot. Runs on a one-shot
    daemon thread and is abandoned on a hang, the same idiom as _synth_call.
    A failure inside PlaySound is re-raised on the caller's thread."""
    import winsound
    box: dict = {}

    def _run() -> None:
        try:
            winsound.PlaySound(source, flags)
        except Exception as exc:   # noqa: BLE001 - re-raised on the caller's thread
            box["err"] = exc

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    # Same bound as the aplay paths. A long user sound is legitimate, so the
    # wav's own runtime sets the cap instead of a flat 60 s.
    t.join(max(60.0, _wav_seconds(source) * 1.5 + 5))
    if t.is_alive():
        log_drop("tts-playback",
                 "winsound hung on the audio device; playback abandoned, callout had no audio")
        return
    if "err" in box:
        raise box["err"]


def _play_wav(wav_path: str, gen: "int | None" = None) -> None:
    """Play a WAV through the tracked aplay subprocess. `gen` is the generation
    the caller captured when the callout was picked up. It is re-checked under
    _proc_lock right before the spawn, closing the window where an interrupt
    sees _current_proc is None and a stale callout plays past it."""
    global _current_proc, _interrupted_proc
    system = platform.system()
    if system == "Windows":
        import winsound
        _play_winsound(wav_path, winsound.SND_FILENAME | winsound.SND_NODEFAULT)
        return
    # aplay supports `--` so a wav path that starts with `-` is handled. stderr
    # is captured, not DEVNULL, so a playback failure, typically ALSA failing to
    # open the device and exiting nonzero almost instantly, leaves a drop line
    # instead of silent no-audio. aplay -q is quiet, so the pipe can't fill.
    player = ["aplay", "-q", "--", wav_path]
    with _proc_lock:
        if gen is not None and gen != _generation:
            # Interrupted between pickup and spawn. Drop the stale callout.
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
    # A long user sound is legitimate within the 32 MiB cap, so bound the kill
    # by the wav's own runtime instead of cutting real playback at 60 s.
    duration = _wav_seconds(wav_path)
    play_timeout = max(60.0, duration * 1.5 + 5)
    try:
        _, err = proc.communicate(timeout=play_timeout)
    except subprocess.TimeoutExpired:   # a wedged player must not block the worker
        timed_out = True
        proc.kill()
        try:
            err = proc.communicate(timeout=5)[1]
        except subprocess.TimeoutExpired:
            # Same D-state husk as _run_speak_proc. Abandon it instead of
            # hanging the worker.
            pass
    finally:
        with _proc_lock:
            was_interrupted = _interrupted_proc is proc
            if was_interrupted:   # don't leave a stale marker behind
                _interrupted_proc = None
            _current_proc = None
    # An interrupt-driven terminate also exits nonzero, but that is a
    # higher-priority callout cutting this one off, not a lost callout. Skip the
    # drop log so the intentional cut-off is not mistaken for playback failure.
    if was_interrupted:
        return
    if timed_out:
        # A hung device, a wireless dongle stall or a wedged PipeWire node,
        # blocks in write instead of erroring. The kill leaves no error text
        # and the callout never played. This is the failure mode the exit
        # status check below cannot see.
        log_drop("tts-playback",
                 f"aplay hung {play_timeout:.0f}s on the audio device; killed, callout had no audio")
        return
    if proc.returncode != 0:
        lines = (err or b"").decode(errors="replace").strip().splitlines()
        detail = lines[-1][:160] if lines else f"exit {proc.returncode}"
        log_drop("tts-playback",
                 f"aplay exit {proc.returncode}; callout had no audio: {detail}")
        return
    # Exit 0 well before the wav's own runtime means the device discarded the
    # audio, the null device, a suspended sink, a dead wireless link. aplay
    # blocks for the sound's duration on real playback, so a clean but fast
    # exit is the only app-side signal of a blackholed callout.
    elapsed = time.monotonic() - started
    if duration and elapsed < duration * 0.5:
        log_drop("tts-playback",
                 f"aplay exited 0 after {elapsed:.2f}s for a {duration:.2f}s wav; "
                 "the device likely discarded the audio")


# Cap on a user-configured sound_file. The volume-scaling path copies it to a
# tmp WAV first, and an unbounded copy of a huge or still-growing file would
# fill up /tmp and hang the single TTS worker.
_MAX_SOUND_BYTES = 32 << 20


def _copy_sound_to_tmp(path: str) -> "str | None":
    """Copy a user-picked sound file to a temp WAV, capped at _MAX_SOUND_BYTES.
    None, with a drop line, when the path is not a regular file, is empty,
    starts over the cap, or grows past it mid copy. The path comes from a file
    dialog, so a FIFO or device node must never reach the copy: it would hang
    the caller."""
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
    # Bounded chunked copy, not shutil.copy2. The pre-checks above race a
    # growing file, so the cap is enforced again mid-copy.
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
        # A vanished source or a full tmpfs must not orphan the mkstemp file.
        log_drop("tts-sound", f"sound copy failed; not played: {path!r}: {exc}")
        ok = False
    if not ok:
        Path(tmp_path).unlink(missing_ok=True)
        return None
    return tmp_path


def _play_wav_file(path: str, volume: float = 1.0, gen: "int | None" = None) -> None:
    tmp_path = None
    # The worker passes the stamp from enqueue. A direct caller has none, so
    # capture it here. Plumbed into _play_wav, which re-checks it under
    # _proc_lock right before the spawn, so an interrupt landing during the
    # prechecks or the copy vetoes the stale sound instead of playing it late.
    if gen is None:
        gen = _generation
    try:
        effective_volume = volume * _master_volume
        if effective_volume <= 0.0:                 # muted, nothing to play
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
            # sound_file is a raw path from the trigger config. A FIFO here
            # would park the single TTS worker in aplay until its 60 s kill.
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
    """Daemon-thread body of play_notification. Linux only, since
    Windows chimes serialize through the TTS worker. Bakes volume into a
    temp WAV if needed, plays blocking, then cleans up."""
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
            # Same guard as the TTS worker path in _play_wav_file. A FIFO here
            # would park this daemon thread and its chime slot in aplay until
            # the 60 s kill.
            if not os.path.isfile(path):
                log_drop("tts-notify", f"not a regular file; not played: {path!r}")
                return
            if os.path.getsize(path) == 0:
                log_drop("tts-notify", f"empty sound file; not played: {path!r}")
                return
            # Same cap as the TTS worker path in _play_wav_file, a huge sound
            # must not be handed to aplay unbounded.
            if os.path.getsize(path) > _MAX_SOUND_BYTES:
                log_drop("tts-notify", f"sound file over {_MAX_SOUND_BYTES >> 20} MiB; not played: {path!r}")
                return
        _play_wav_detached(play_path)
    except Exception as exc:  # noqa: BLE001 - a chime must never crash the app
        print(f"[tts] notification failed: {exc!r}", file=sys.stderr)
    finally:
        _notification_slots.release()
        if tmp_path:
            try:
                Path(tmp_path).unlink(missing_ok=True)
            except OSError:
                pass


def _apply_volume_bytes(wav_bytes: bytes, volume: float) -> bytes:
    """In-memory _apply_volume. Returns new WAV bytes, or the input unchanged
    for unsupported 24/32-bit or float formats and for WAVs too corrupt or
    truncated to parse, which the caller then plays at its native level."""
    try:
        with wave.open(io.BytesIO(wav_bytes), 'rb') as r:
            params = r.getparams()
            frames = r.readframes(params.nframes)
    except (wave.Error, EOFError):
        # Corrupt, empty or float WAV. Same fallback as an unsupported format,
        # the caller plays the input at its native level.
        return wav_bytes
    scaled = _scale_pcm(frames, params.sampwidth, volume)
    if scaled is None:
        return wav_bytes
    out = io.BytesIO()
    with wave.open(out, 'wb') as w:
        w.setparams(params)
        w.writeframes(scaled)
    return out.getvalue()


def _play_wav_bytes_detached(wav_bytes: bytes) -> None:
    """Play WAV bytes without touching disk where possible. Windows uses
    winsound SND_MEMORY. Linux pipes to aplay stdin."""
    system = platform.system()
    if system == "Windows":
        import winsound
        # Bounded like the aplay below, a wedge must not pin this daemon
        # thread and its chime slot forever.
        _play_winsound(wav_bytes, winsound.SND_MEMORY | winsound.SND_NODEFAULT)
    else:
        try:
            subprocess.run(["aplay", "-q", "-"], input=wav_bytes,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=max(60.0, _wav_seconds(wav_bytes) * 1.5 + 5),
                           env=proc_env.child_env())
        except FileNotFoundError:
            _aplay_missing()
        except subprocess.TimeoutExpired:
            # run kills the child itself on timeout. A wedged audio device
            # must not pin this daemon thread and its process forever.
            print("[tts] notification playback timed out; killed aplay",
                  file=sys.stderr)


def _notification_bytes_worker(wav_bytes: bytes, volume: float) -> None:
    """Daemon-thread body of play_notification_bytes. Bakes volume in memory
    if needed, then plays from memory. Releases the caller's chime slot."""
    try:
        effective = max(0.0, min(2.0, volume * _master_volume))
        if effective <= 0.0:
            return
        data = wav_bytes
        if abs(effective - 1.0) > 0.01:
            data = _apply_volume_bytes(wav_bytes, effective)
        _play_wav_bytes_detached(data)
    except Exception as exc:  # noqa: BLE001 - a chime must never crash the app
        print(f"[tts] notification (memory) failed: {exc!r}", file=sys.stderr)
    finally:
        _notification_slots.release()


def _play_wav_detached(wav_path: str) -> None:
    """Blocking WAV playback outside the tracked _current_proc, so a
    notification is not cut off by interrupt and, on Linux, where a local
    subprocess gives it its own channel, overlaps a spoken callout. Never
    reached on Windows. Its single process-wide winsound channel would cut
    the callout off, so chimes there serialize through the TTS worker
    instead. See play_notification."""
    try:
        # Same long-sound bound as the worker path, a long chime is
        # legitimate and must not be killed mid-playback at 60 s.
        subprocess.run(["aplay", "-q", "--", wav_path],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=max(60.0, _wav_seconds(wav_path) * 1.5 + 5),
                       env=proc_env.child_env())
    except FileNotFoundError:
        _aplay_missing()
    except subprocess.TimeoutExpired:
        # Same wedged-device guard as in _play_wav_bytes_detached.
        print("[tts] notification playback timed out; killed aplay",
              file=sys.stderr)


def _play_wav_bytes(wav: "bytes | None", volume: float = 1.0,
                    gen: "int | None" = None) -> bool:
    """Play WAV bytes through the tracked path so interrupt works. True if it
    played, False if there was nothing to play, so callers can fall back.
    `gen` rides through to _play_wav's pre-spawn interrupt check."""
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
    except Exception:  # noqa: BLE001 - a chime must never crash the app
        return False
    finally:
        if wav_path:
            try:
                Path(wav_path).unlink(missing_ok=True)
            except OSError:
                pass


def _kokoro_speak(text: str, volume: float = 1.0, speed: float = 1.0,
                  gen: "int | None" = None) -> bool:
    """Synthesize `text` with the in-process Kokoro neural JP voice and play it.
    Returns False to fall back. Callers pass the kana reading, which comes out
    correctly even when the pyopenjtalk kanji phonemizer isn't installed."""
    wav = _kokoro_synth(text, speed)
    if gen is not None and gen != _generation:
        # interrupt landed while synthesizing. Report handled so the stale
        # callout is dropped instead of falling through to the OS voice.
        return True
    return _play_wav_bytes(wav, volume, gen)


def _pipeline(text: str, volume: float = 1.0, speed: float = 1.0,
              reading: "str | None" = None, gen: "int | None" = None) -> None:
    global _piper_voice, _piper_failed
    # Muted, master or per-trigger zero. Skip synthesis entirely. The
    # quietest backend settings, e.g. spd-say -i -100, are still audible,
    # not silent.
    if volume * _master_volume <= 0.0:
        return
    if gen is None:   # direct caller with no enqueue stamp
        gen = _generation
    # Resolve a known kana reading from the shared map first, so every path
    # below, neural and espeak alike, gets kana, not just callers that pass
    # reading= explicitly.
    # Snapshot the reference once. set_readings rebinds, copy-on-write, never
    # mutates, so this read is safe against a concurrent GUI-thread swap.
    if reading is None:
        readings = _READINGS
        if readings:
            reading = readings.get(text)
    # In-app neural Japanese voice, Kokoro, for Japanese text. Feed it the kana
    # `reading` when we have one. Kana synthesizes correctly whether or not the
    # pyopenjtalk phonemizer built, whereas kanji needs it and otherwise comes out
    # as "Chinese letter". Falls back to the OS voice, espeak, on the same reading.
    if has_japanese(text) and _jp_neural and _kokoro_speak(reading or text, volume, speed, gen):
        return
    # System backend first, falls back to Piper if none exists on this platform.
    # Japanese text always tries the system voice even under Piper, which is
    # English-only, and never falls to it. A missing or failed backend leaves
    # the callout silent instead of garbled.
    # `reading` kana is spoken by voices that can't read kanji like espeak.
    if (_engine == "system" or (_jp_auto and has_japanese(text))) \
            and _system_speak(text, volume, speed, reading, gen):
        return

    wav_path = None
    try:
        voice = _load_piper()
        if voice is None:
            with _piper_lock:
                failed = _piper_failed
            if failed:                  # known failed load, already logged once
                log_drop("tts-piper", f"piper voice unavailable (sticky load failure); not spoken: {text[:60]!r}")
            else:
                # The build finished but a mid-build set_model or set_venv_path
                # discarded it. Not a failure, the next callout rebuilds from
                # the new inputs.
                log_drop("tts-piper", f"piper voice build superseded by a settings change; not spoken: {text[:60]!r}")
            return

        fd, wav_path = tempfile.mkstemp(suffix=".wav")
        os.close(fd)

        # Speed goes through SynthesisConfig. The length_scale kwarg is gone
        # from synthesize_wav in piper 1.4+.
        syn_config = None
        if abs(speed - 1.0) > 0.01:
            from piper.config import SynthesisConfig
            syn_config = SynthesisConfig(length_scale=1.0 / max(0.1, speed))

        # Synthesize into memory and write the temp file only after the call
        # returns. On a hang the abandoned thread stays inside wave.open, and
        # on Windows that open handle would make the finally's unlink fail,
        # stranding one temp wav per hang for the machine lifetime.
        def _synth() -> bytes:
            buf = io.BytesIO()
            with wave.open(buf, "wb") as wf:
                voice.synthesize_wav(text, wf, syn_config=syn_config)
            return buf.getvalue()

        ok, wav = _synth_call(_synth)
        if not ok:
            # Same wedged-session rule as kokoro. Stick the failed marker like
            # the load path does and unpublish the voice, else every later
            # callout re-enters the hang and burns another _SYNTH_TIMEOUT_S on
            # the worker. Only fail the voice still published, a mid-hang
            # set_model already cleared the marker for the new inputs.
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

        if gen != _generation:          # interrupted mid-synthesis
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
