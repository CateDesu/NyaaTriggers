#!/usr/bin/env python3
"""Relay raw IINACT JSON to the GPL-3.0 Triggevent engine through the maintained fork.
Java and the jar are optional until the engine starts."""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from nyaatriggers.paths import bundle_bases as _bundle_bases, bundle_root

from PyQt6.QtCore import QObject, pyqtSignal

from nyaatriggers import proc_env
from nyaatriggers.diagnostics import record, record_engine
from nyaatriggers.drop_log import log_drop, open_private_log, rotate_one_generation
from nyaatriggers.engine_build_info import engine_commit as _launch_build_commit
from nyaatriggers.trigger_engine import apply_replacements

# Jars and JRE data may be packaged in separate directories.
_BASE = bundle_root()
_JAVA_EXE = "java.exe" if os.name == "nt" else "java"

_MAX_LINE = 1 << 20

_MAX_QUEUE_BYTES = 64 << 20
_MAX_SPEECH_CANCEL_IDS = 4096


def _read_lines_bounded(stream):
    """Yield bounded lines and discard oversized input through its newline."""
    while True:
        line = stream.readline(_MAX_LINE + 1)
        if not line:
            return
        if len(line) > _MAX_LINE and not line.endswith("\n"):
            chars = len(line)
            while True:
                more = stream.readline(_MAX_LINE + 1)
                chars += len(more)
                if not more or more.endswith("\n"):
                    break
            record("engine_protocol", reason="oversize", chars=chars)
            continue
        yield line


class _ByteQueue(queue.Queue):
    """Bound queued strings by bytes and count. Overflow requires ordered engine recovery."""

    def __init__(self, maxsize: int, maxbytes: int = _MAX_QUEUE_BYTES) -> None:
        super().__init__(maxsize)
        self._maxbytes = maxbytes
        self._nbytes = 0

    def _put(self, item) -> None:
        n = sys.getsizeof(item) if isinstance(item, str) else 0
        if self._nbytes + n > self._maxbytes:
            raise queue.Full
        super()._put(item)
        self._nbytes += n

    def _get(self):
        item = super()._get()
        if isinstance(item, str):
            self._nbytes -= sys.getsizeof(item)
        return item


def _bundled_jre_dir() -> "Path | None":
    for base in _bundle_bases():
        if (base / "jre" / "bin" / _JAVA_EXE).exists():
            return base / "jre"
    return None


def _log_dir() -> Path:
    if os.name == "nt":
        # An empty APPDATA must fall back to home rather than the current directory.
        root = Path(os.environ.get("APPDATA") or Path.home())
    else:
        root = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
    d = root / "nyaatriggers"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _log_path() -> "Path | None":
    try:
        return _log_dir() / "triggevent.log"
    except OSError:
        return None


_LOG_LOCK = threading.Lock()


def _log(msg: str) -> None:
    """Write persistent diagnostics without interrupting the engine on logging failure."""
    try:
        print(f"[triggevent] {msg}", file=sys.stderr)
    except Exception:  # noqa: BLE001
        pass
    p = _log_path()
    if p is None:
        return
    try:
        import time
        with _LOG_LOCK:
            try:
                if p.exists() and p.stat().st_size > (1 << 20):
                    rotate_one_generation(p)
            except OSError:
                pass
            with open_private_log(p) as fh:
                fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {msg}\n")
    except Exception:  # noqa: BLE001
        pass


_STOP = object()


def _snapshot_jar(jar: Path):
    directory = tempfile.TemporaryDirectory(prefix="nyaa-triggevent-", ignore_cleanup_errors=True)
    runtime_jar = Path(directory.name) / jar.name
    try:
        shutil.copyfile(jar, runtime_jar)
    except BaseException:
        directory.cleanup()
        raise
    return directory, runtime_jar


def _find_java() -> str | None:
    """Prefer bundled Java, then JAVA_HOME, then PATH."""
    jre = _bundled_jre_dir()
    if jre is not None:
        return str(jre / "bin" / _JAVA_EXE)
    jh = os.environ.get("JAVA_HOME")
    if jh:
        cand = Path(jh) / "bin" / _JAVA_EXE
        if cand.exists():
            return str(cand)
    return shutil.which("java")


def _find_jar() -> Path | None:
    env = os.environ.get("NYAA_TRIGGEVENT_JAR")
    if env and Path(env).is_file():
        return Path(env)
    for base in _bundle_bases():
        cand = base / "triggevent-core" / "target" / "triggevent-core.jar"
        if cand.is_file():
            return cand
    return None


def has_java() -> bool:
    return _find_java() is not None


_CORE_DIR     = _BASE / "triggevent-core"
_ET_DIR       = _CORE_DIR / "event-trigger"
_BUILD_SCRIPT = _CORE_DIR / ("build.bat" if os.name == "nt" else "build.sh")
# Use the maintained fork. Upstream changes enter through merges to its main branch.
_ET_REPO_URL  = "https://github.com/CateDesu/event-trigger.git"
_ET_BRANCH    = "main"
_JAR_STAMP    = _CORE_DIR / "target" / "triggevent-core.jar.built-from"


def _jar_built_from() -> "dict | None":
    """Old stamps without wrapper inputs require a rebuild."""
    try:
        value = json.loads(_JAR_STAMP.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError, RecursionError):
        return None


def _wrapper_build_inputs() -> dict:
    """Track source edits and additions without including generated files."""
    def unreadable(error):
        raise error

    paths = [_CORE_DIR / name for name in ("pom.xml", "build.sh", "build.bat")]
    paths.extend(path for path in (_CORE_DIR / "build_engine.py",
                                   _CORE_DIR / "patches" / "same-zone-history.patch") if path.is_file())
    for directory, _dirs, files in os.walk(_CORE_DIR / "src" / "main", onerror=unreadable):
        paths.extend(Path(directory) / name for name in files)
    inputs = {}
    for path in sorted(paths):
        stat = path.stat()
        inputs[path.relative_to(_CORE_DIR).as_posix()] = [stat.st_mtime_ns, stat.st_size]
    return inputs


def _kill_build_tree(proc) -> None:
    """Stop the whole build tree so timed-out Maven and compiler children cannot keep running."""
    try:
        if os.name == "posix":
            import signal
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        else:
            # Killing cmd.exe alone leaves build children running.
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True)
    except (OSError, ProcessLookupError):
        try:
            proc.kill()
        except OSError:
            pass


def update_engine(channel: str = "stable", manual: bool = False) -> "tuple[bool, str]":
    """Rebuild source with a local toolchain, or install the stable jar on manual request.
Run in the background and return whether it changed."""
    if getattr(sys, "frozen", False):
        if not manual:
            return (False, "The engine is bundled with the program")
        return _download_engine("stable")
    # Source builds require a toolchain and, on POSIX, bash.
    tools = ("git", "java", "mvn") if os.name == "nt" else ("git", "java", "mvn", "bash")
    if (_CORE_DIR / "build_engine.py").is_file():
        tools += ("python" if os.name == "nt" else "python3",)
    missing = next((t for t in tools if not shutil.which(t)), "")
    buildable = (_ET_DIR / ".git").is_dir() and _BUILD_SCRIPT.is_file()
    if missing or not buildable:
        if not manual:
            why = (f"'{missing}' is not on PATH" if missing
                   else "no event-trigger clone to build from")
            return (False, f"Triggevent auto-update skipped: {why}. Click "
                           "'Update Triggevent Engine' to download the prebuilt engine.")
        return _download_engine("stable")

    def _git(*args):
        return subprocess.run(["git", "-C", str(_ET_DIR), *args],
                              capture_output=True, text=True, timeout=120)

    try:
        d = _git("status", "--porcelain")
        if d.returncode != 0:
            return (False, f"Triggevent could not check local changes: {d.stderr.strip()[:200]}")
        if d.stdout.strip():
            return (False, "Triggevent pull skipped: local event-trigger clone has uncommitted changes")
        # Point older installs at the maintained fork, adding origin if absent.
        u = _git("remote", "get-url", "origin")
        if u.returncode != 0:
            a = _git("remote", "add", "origin", _ET_REPO_URL)
            if a.returncode != 0:
                return (False, f"Triggevent could not add the origin remote: {a.stderr.strip()[:200]}")
        elif u.stdout.strip() != _ET_REPO_URL:
            s = _git("remote", "set-url", "origin", _ET_REPO_URL)
            if s.returncode != 0:
                return (False, f"Triggevent could not repoint the origin remote: {s.stderr.strip()[:200]}")
        f = _git("fetch", "origin", _ET_BRANCH)
        if f.returncode != 0:
            return (False, f"Triggevent fetch failed: {f.stderr.strip()[:200]}")
        a = _git("merge-base", "--is-ancestor", "HEAD", f"origin/{_ET_BRANCH}")
        if a.returncode == 1:
            return (False, "Triggevent pull skipped: local event-trigger clone has unpublished commits")
        if a.returncode != 0:
            return (False, f"Triggevent could not check local history: {a.stderr.strip()[:200]}")
        r = _git("rev-list", "--count", f"HEAD..origin/{_ET_BRANCH}")
        if r.returncode != 0:
            return (False, f"Triggevent rev-list failed: {r.stderr.strip()[:200]}")
        h = _git("rev-parse", f"origin/{_ET_BRANCH}")
        if h.returncode != 0:
            return (False, f"Triggevent rev-parse failed: {h.stderr.strip()[:200]}")
        head = h.stdout.strip()
        behind = r.stdout.strip()
        build_state = {"engine": head, "inputs": _wrapper_build_inputs()}
        if not behind.isdigit() or int(behind) == 0:
            if (_jar_built_from() == build_state
                    and (_CORE_DIR / "target" / "triggevent-core.jar").is_file()):
                return (False, "Triggevent Engine already up to date")
        if _git("merge", "--ff-only", f"origin/{_ET_BRANCH}").returncode != 0:
            return (False, "Triggevent pull skipped: local event-trigger clone has diverged or has uncommitted changes")
        # Require a clean checkout so the build stamp identifies the compiled source.
        d = _git("status", "--porcelain")
        if d.returncode != 0:
            return (False, f"Triggevent could not check local changes: {d.stderr.strip()[:200]}")
        if d.stdout.strip():
            return (False, "Triggevent pull skipped: local event-trigger clone has uncommitted changes")
        cmd = [str(_BUILD_SCRIPT)] if os.name == "nt" else ["bash", str(_BUILD_SCRIPT)]
        # Build the newly merged commit instead of restoring the old pin.
        env = {**os.environ, "EVENT_TRIGGER_REF": f"origin/{_ET_BRANCH}"}
        popen_kwargs = dict(stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, env=env)
        if os.name == "posix":
            popen_kwargs["start_new_session"] = True
        _JAR_STAMP.unlink(missing_ok=True)
        proc = subprocess.Popen(cmd, **popen_kwargs)
        try:
            _, b_err = proc.communicate(timeout=1800)
        except subprocess.TimeoutExpired:
            _kill_build_tree(proc)
            proc.wait()
            return (False, f"Triggevent update timed out running {cmd[0]}")
    except subprocess.TimeoutExpired as e:
        prog = e.cmd[0] if isinstance(e.cmd, (list, tuple)) and e.cmd else str(e.cmd)
        return (False, f"Triggevent update timed out running {prog}")
    except OSError as e:
        return (False, f"Triggevent could not prepare the build: {e}")
    if proc.returncode != 0:
        return (False, f"Triggevent rebuild failed:\n{b_err.strip()[-400:]}")
    # Keep the prebuild inputs so edits during compilation remain pending.
    try:
        _JAR_STAMP.write_text(json.dumps(build_state, sort_keys=True) + "\n", encoding="utf-8")
    except OSError:
        pass
    if behind.isdigit() and int(behind) > 0:
        return (True, f"Triggevent Engine updated ({behind} new commit(s)) and rebuilt. "
                      f"Restart NyaaTriggers to load the new triggers.")
    return (True, "Triggevent Engine rebuilt from the current source. "
                  "Restart NyaaTriggers to load it.")


def _unlink(p) -> None:
    try:
        p.unlink()
    except OSError:
        pass


def _same_file(a, b) -> bool:
    try:
        if a.stat().st_size != b.stat().st_size:
            return False
        import hashlib
        def _h(p):
            h = hashlib.sha256()
            with open(p, "rb") as f:
                for chunk in iter(lambda: f.read(65536), b""):
                    h.update(chunk)
            return h.digest()
        return _h(a) == _h(b)
    except OSError:
        return False


def _download_engine(channel: str) -> "tuple[bool, str]":
    """Install the published jar atomically, including without a local source build."""
    jar = _find_jar()
    if jar is None:
        jar = _BASE / "triggevent-core" / "target" / "triggevent-core.jar"
        try:
            jar.parent.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            return (False, f"Couldn't create {jar.parent}: {e}")
    try:
        from nyaatriggers import updater  # Delay import to avoid a module cycle.
        rel = updater.fetch_latest_release(channel=channel)
    except Exception as e:  # noqa: BLE001
        return (False, f"Couldn't check for an engine update: {e}")
    url = rel.assets.get("triggevent-core.jar")
    if not url:
        return (False, "No separate engine download is available. Update NyaaTriggers.")
    tmp = jar.parent / "triggevent-core.jar.new"
    for stale in jar.parent.glob("triggevent-core.jar.new.*.part"):
        try:
            if stale.stat().st_mtime < time.time() - 3600:
                stale.unlink()
        except OSError:
            pass
    try:
        updater.download(url, tmp)
    except Exception as e:  # noqa: BLE001
        _unlink(tmp)
        return (False, f"Engine download failed: {e}")
    try:
        with open(tmp, "rb") as f:
            head = f.read(2)
        if tmp.stat().st_size < 100_000 or head != b"PK":
            _unlink(tmp)
            return (False, "Downloaded engine looks corrupt - kept your current one")
    except OSError as e:
        _unlink(tmp)
        return (False, f"Couldn't verify the download: {e}")
    if _same_file(tmp, jar):
        _unlink(tmp)
        return (False, "Triggevent Engine is already up to date")
    ok, why = updater.verify_release_asset(rel, "triggevent-core.jar", tmp)
    if not ok:
        _unlink(tmp)
        return (False, f"Engine update rejected ({why}) - kept your current one")
    try:
        os.replace(str(tmp), str(jar))
    except OSError as e:
        _unlink(tmp)
        return (False, f"Couldn't install the new engine: {e}")
    # Remove the source build stamp because it does not identify this downloaded jar.
    _unlink(_JAR_STAMP)
    frozen_note = (" The next NyaaTriggers update ships and restores its own "
                   "bundled engine.") if getattr(sys, "frozen", False) else ""
    if not has_java():
        # Report a missing Java runtime even when jar installation succeeded.
        return (True, "Triggevent Engine installed, but no Java runtime was found. "
                      "Install one (Arch: sudo pacman -S jre-openjdk), then restart "
                      "NyaaTriggers." + frozen_note)
    return (True, "Triggevent Engine updated. Restart NyaaTriggers to load it." + frozen_note)


def has_jar() -> bool:
    return _find_jar() is not None


def is_available() -> bool:
    return has_java() and has_jar()


def _make_bundled_jre_executable() -> None:
    """Restore executable permissions on bundled Java tools and jspawnhelper."""
    jre = _bundled_jre_dir()
    if jre is None:
        return
    targets = list((jre / "bin").glob("*"))
    targets.append(jre / "lib" / "jspawnhelper")
    for p in targets:
        try:
            if p.is_file():
                os.chmod(p, os.stat(p).st_mode | 0o111)
        except OSError:
            pass


class TriggeventBridge(QObject):

    # Generation stamps let UI slots reject stale output after restarts.
    callout     = pyqtSignal(str, str, int)   # on-screen text, severity in {info, alert, alarm}, generation
    tts         = pyqtSignal(str, int)        # spoken text, generation
    status      = pyqtSignal(bool, str, int)
    phrase_seen = pyqtSignal(str)        # a callout phrase observed, for the override UI
    inventory   = pyqtSignal(str, int)
    automark_inventory = pyqtSignal(str, int)
    telesto     = pyqtSignal(str, int)   # Telesto automark connection status, "good"|"bad"|"unknown", generation
    ready       = pyqtSignal(int)
    chain_failure = pyqtSignal(str, int)
    combatants_request = pyqtSignal(object, int)
    recovery_progress = pyqtSignal(object, int)
    feed_overflow = pyqtSignal(int)
    custom_status = pyqtSignal(bool, str, int)
    _callout_ready = pyqtSignal(object, int)
    _speech_cancel_ready = pyqtSignal(object, int)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._proc: subprocess.Popen | None = None
        self._recovery_gen = -1
        self._history_gen = -1
        self._catchup_gen = -1
        self._custom_gen = -1
        self._speech_cancel_gen = -1
        self._speech_cancel_all_gen = -1
        self._structured_diagnostics_gen = -1
        self._diagnostic_ready_gen = -1
        self._legacy_failure_gen = -1
        self._legacy_failures = []
        self._legacy_failure_count = 0
        self._speech_cancel_seq = 0
        self._speech_cancel_pending: dict[str, int] = {}
        self._speech_cancel_all_pending: int | None = None
        self._speech_cancel_overflow = -1
        self._overflow_gen = -1
        self._reader: threading.Thread | None = None
        self._errpump: threading.Thread | None = None
        self._writer: threading.Thread | None = None
        self._wq: queue.Queue = _ByteQueue(maxsize=10000)
        self._active = False
        # An active flag alone cannot distinguish replaced readers.
        self._gen = 0
        self._replacements: list = []
        self._disabled: frozenset = frozenset()
        self._seen: dict = {}            # ordered set of observed callout phrases
        self._seen_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._diagnostic_lock = threading.Lock()
        self._diagnostic_rates: dict = {}
        self._feed_frames = 0
        self._feed_chars = 0
        self._feed_report_at = time.monotonic()
        # Preserve reader order through GUI delivery.
        self._callout_ready.connect(self._deliver_callout)
        self._speech_cancel_ready.connect(self._acknowledge_speech_cancel)

    def _diagnostic(self, event: str, gen=None, *, throttle_s=0.0, **fields) -> None:
        if throttle_s:
            key = (event, self._gen if gen is None else gen,
                   fields.get("reason"), fields.get("channel"), fields.get("state"))
            now = time.monotonic()
            with self._diagnostic_lock:
                previous, count = self._diagnostic_rates.get(key, (float("-inf"), 0))
                count += 1
                if now - previous < throttle_s:
                    self._diagnostic_rates[key] = (previous, count)
                    return
                self._diagnostic_rates[key] = (now, 0)
                if len(self._diagnostic_rates) > 128:
                    self._diagnostic_rates.pop(next(iter(self._diagnostic_rates)))
            fields["count"] = count
        record(event, gen=self._gen if gen is None else gen, **fields)

    def _queue_diagnostic(self, *, reason=None, accepted=True, frames=0, chars=0, gen=None, wq=None, channel="feed") -> None:
        q = self._wq if wq is None else wq
        fields = dict(accepted=accepted, frames=frames, chars=chars, channel=channel,
                      queue_depth=q.qsize(), queue_bytes=getattr(q, "_nbytes", 0))
        if reason is not None:
            fields["reason"] = reason
        self._diagnostic("engine_queue", gen, **fields)

    @staticmethod
    def is_available() -> bool:
        return is_available()

    def is_active(self) -> bool:
        return self._active

    def generation(self) -> int:
        return self._gen

    def _gen_live(self, gen: "int | None") -> bool:
        return gen is not None and gen == self._gen and self._active

    def set_replacements(self, rules: list) -> None:
        """Replace rules atomically. An empty result suppresses the callout."""
        self._replacements = list(rules or [])

    def set_disabled(self, ids) -> None:
        disabled = frozenset(ids or ())
        with self._state_lock:
            changed = disabled ^ self._disabled
            self._disabled = disabled
        self.cancel_speech(changed)

    def cancel_speech(self, ids) -> None:
        """Cancel pending output for changed callouts without changing their defaults."""
        changed = frozenset(ids or ())
        with self._state_lock:
            if (not changed or not self._gen_live(self._speech_cancel_gen)
                    or self._speech_cancel_overflow == self._gen):
                return
            overflow = len(self._speech_cancel_pending.keys() | changed) > _MAX_SPEECH_CANCEL_IDS
            if overflow:
                self._speech_cancel_pending.clear()
                self._speech_cancel_overflow = self._gen
            else:
                self._speech_cancel_seq += 1
                token = self._speech_cancel_seq
                self._speech_cancel_pending.update((cid, token) for cid in changed)
        if overflow:
            self._diagnostic("engine_cancel", reason="cancel_overflow", count=len(changed),
                             pending_ids=0, result="rejected")
            self._queue_overflow("speech cancellation queue full; restarting pull recovery")
        else:
            self._diagnostic("engine_cancel", token=token, count=len(changed),
                             pending_ids=len(self._speech_cancel_pending), result="queued")
            self._send_command({"nyaa_cmd": "cancel_speech", "ids": sorted(changed),
                                "token": token})

    def cancel_all_speech(self) -> None:
        """Retire speech across a mode change without resetting the engine."""
        with self._state_lock:
            if not self._gen_live(self._speech_cancel_all_gen):
                return
            self._speech_cancel_seq += 1
            token = self._speech_cancel_seq
            self._speech_cancel_all_pending = token
        self._diagnostic("engine_cancel", token=token, count=0, speech_cancel_all=True, result="queued")
        self._send_command({"nyaa_cmd": "cancel_speech", "all": True, "token": token})

    def supports_custom_triggers(self) -> bool:
        return self._active and self._custom_gen == self._gen

    def set_custom_triggers(self, triggers: list) -> None:
        if self.supports_custom_triggers():
            self._send_command({"nyaa_cmd": "custom_triggers", "triggers": triggers})

    def set_callout(self, cid: str, tts: str | None = None,
                    text: str | None = None, enable: bool | None = None) -> None:
        """Preserve template tokens. The caller replays edits after restart."""
        if not cid:
            return
        cmd: dict = {"nyaa_cmd": "set_callout", "id": cid}
        if tts is not None:
            cmd["tts"] = tts
        if text is not None:
            cmd["text"] = text
        if enable is not None:
            cmd["enable"] = bool(enable)
        self._send_command(cmd)

    def reset_callout(self, cid: str) -> None:
        if cid:
            self._send_command({"nyaa_cmd": "reset_callout", "id": cid})

    def set_automark(self, enable: bool | None, uri: str | None = None, *,
                    native_umad: bool | None = None, settings: dict | None = None) -> None:
        """Native automarking starts disabled. Replay its settings after restart."""
        cmd: dict = {"nyaa_cmd": "set_automark"}
        if enable is not None:
            cmd["enable"] = bool(enable)
        if uri:
            cmd["uri"] = str(uri)
        if native_umad is not None:
            cmd["native_umad"] = bool(native_umad)
        if settings is not None:
            cmd["settings"] = settings
        self._send_command(cmd)

    def _send_command(self, cmd: dict) -> None:
        """Queue a control command and recover if the engine cannot keep up."""
        if not self._active:
            self._diagnostic("engine_command", reason="inactive", result="rejected", throttle_s=5)
            return
        kind = cmd.get("nyaa_cmd")
        kind = kind if kind in ("cancel_speech", "custom_triggers", "set_callout", "reset_callout", "set_automark") else "unknown"
        try:
            line = json.dumps(cmd)
        except (TypeError, ValueError) as exc:
            self._diagnostic("engine_command", state=kind, reason="invalid_shape", result="rejected",
                             error_type=type(exc).__name__)
            return
        try:
            self._wq.put_nowait(line)
            self._diagnostic("engine_command", state=kind, result="queued", chars=len(line),
                             queue_depth=self._wq.qsize())
        except queue.Full:
            self._diagnostic("engine_command", state=kind, reason="queue_full", result="rejected")
            self._queue_overflow()

    def _queue_overflow(self, reason: str = "sidecar stdin queue full; restarting pull recovery") -> None:
        if self._active and self._overflow_gen != self._gen:
            self._overflow_gen = self._gen
            self._queue_diagnostic(reason="queue_full", accepted=False)
            log_drop("engine-feed", reason)
            self.feed_overflow.emit(self._gen)

    def seen_phrases(self) -> list:
        with self._seen_lock:
            return list(self._seen.keys())


    def _record_seen(self, phrase: str) -> None:
        if not phrase:
            return
        with self._seen_lock:
            if phrase in self._seen:
                return
            self._seen[phrase] = None
            if len(self._seen) > 300:
                self._seen.pop(next(iter(self._seen)))
        self.phrase_seen.emit(phrase)

    def start(self) -> None:
        if self._active:
            return
        self._diagnostic("engine_start", active=False)
        _log(f"start() requested (os={os.name})")
        java = _find_java()
        jar = _find_jar()
        if java is None or jar is None:
            self._diagnostic("engine_error", reason="unavailable", java_available=java is not None,
                             jar_available=jar is not None)
            _log(f"cannot start: java={java!r} jar={jar!r}")
            self.status.emit(False, "Java runtime or triggevent-core.jar not found", self._gen)
            return

        if os.name == "posix" and _bundled_jre_dir() is not None:
            _make_bundled_jre_executable()

        # Keep later class loads independent of rebuilds and engine updates.
        engine_commit = _launch_build_commit(jar)
        try:
            runtime_dir, runtime_jar = _snapshot_jar(jar)
        except OSError as exc:
            self._diagnostic("engine_error", reason="snapshot_failed", error_type=type(exc).__name__)
            _log(f"cannot prepare engine: {exc!r}")
            self.status.emit(False, f"Could not prepare Triggevent engine: {exc}", self._gen)
            return
        engine_commit = _launch_build_commit(runtime_jar) or engine_commit

        # Use a 512 MiB heap. At 256 MiB, GC pauses caused sequential trigger timeouts.
        cmd = [java, "-Xmx512m", "-jar", str(runtime_jar)]
        xvfb = shutil.which("xvfb-run")
        if xvfb:
            # Use 24-bit visuals for reliable Swing initialization.
            cmd = [xvfb, "-a", "-s", "-screen 0 1024x768x24"] + cmd

        popen_kwargs = dict(
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            bufsize=1, text=True, encoding="utf-8", errors="replace",
            cwd=str(jar.parent.parent),
        )
        if os.name == "posix":
            popen_kwargs["start_new_session"] = True
            # Use stdin EOF on parent death so Xvfb can clean up its child.
        if os.name == "nt":
            popen_kwargs["creationflags"] = 0x08000000
        popen_kwargs["env"] = proc_env.child_env()

        try:
            proc = subprocess.Popen(cmd, **popen_kwargs)
        except OSError as e:
            self._diagnostic("engine_error", reason="spawn_failed", error_type=type(e).__name__)
            runtime_dir.cleanup()
            _log(f"launch failed: {e!r}")
            self.status.emit(False, f"Failed to launch sidecar: {e}", self._gen)
            self._proc = None
            return
        proc._nyaa_runtime_dir = runtime_dir
        _log(f"sidecar spawned pid={proc.pid}")

        wq = _ByteQueue(maxsize=10000)
        with self._state_lock:
            self._proc = proc
            self._wq = wq
            self._active = True
            self._gen += 1
            self._speech_cancel_pending.clear()
            self._speech_cancel_all_pending = None
            gen = self._gen
        self._feed_frames = 0
        self._feed_chars = 0
        self._feed_report_at = time.monotonic()
        proc._nyaa_generation = gen
        proc._nyaa_started_at = time.monotonic()
        self._diagnostic("engine_started", gen, active=True, xvfb=bool(xvfb), engine_commit=engine_commit)
        # Keep the sequence watermark per generation because each jar starts numbering at one.
        seq_state: dict = {"last": None}
        self._reader = threading.Thread(target=self._read_loop, args=(proc, wq, seq_state, gen),
                                        daemon=True, name="triggevent-reader")
        self._errpump = threading.Thread(target=self._err_loop, args=(proc, gen),
                                         daemon=True, name="triggevent-stderr")
        self._writer = threading.Thread(target=self._write_loop, args=(proc, wq),
                                        daemon=True, name="triggevent-writer")
        self._reader.start()
        self._errpump.start()
        self._writer.start()
        self.status.emit(True, "Starting Triggevent Engine...", gen)

    def stop(self, wait: bool = False) -> None:
        if not self._active and self._proc is None:
            with self._state_lock:
                self._gen += 1
                self._speech_cancel_pending.clear()
                self._speech_cancel_all_pending = None
                gen = self._gen
            self.status.emit(False, "Off", gen)
            return
        with self._state_lock:
            previous_gen = self._gen
            self._active = False
            self._gen += 1
            self._speech_cancel_pending.clear()
            self._speech_cancel_all_pending = None
            gen = self._gen
            proc, self._proc = self._proc, None
            wq = self._wq
        self._diagnostic("engine_stop", gen, previous_gen=previous_gen, wait=wait, reason="requested")
        self._queue_diagnostic(gen=previous_gen, wq=wq, frames=self._feed_frames, chars=self._feed_chars)
        with self._seen_lock:
            self._seen.clear()
        try:
            wq.put_nowait(_STOP)
        except queue.Full:
            try:
                wq.get_nowait()
                wq.put_nowait(_STOP)
            except (queue.Empty, queue.Full):
                pass
        if proc is not None:
            # Signal the group now because the parent may exit before the background reaper.
            self._signal_group(proc, graceful=True)
            if wait:
                # Wait for Java to release bundle files before Windows updates replace them.
                self._reap(proc)
            else:
                threading.Thread(target=self._reap, args=(proc,), daemon=True,
                                 name="triggevent-reap").start()
        self.status.emit(False, "Off", gen)

    _signal_group = staticmethod(proc_env.signal_group)

    _reap = staticmethod(proc_env.reap)


    def feed(self, raw_msg: str) -> bool:
        """Return false when ordered input could not enter the current engine queue."""
        if not self._active:
            return False
        if not raw_msg:
            return True
        try:
            data = json.loads(raw_msg)
        except (ValueError, TypeError, RecursionError):
            self._diagnostic("engine_protocol", channel="feed", reason="invalid_json", throttle_s=5)
            log_drop("engine-feed", "discarded invalid feed JSON")
            return True
        if not isinstance(data, dict) or "nyaa_cmd" in data:
            self._diagnostic("engine_protocol", channel="feed", reason="feed_protocol", throttle_s=5)
            log_drop("engine-feed", "discarded feed frame outside the event protocol")
            return True
        # Keep each JSON message on one protocol line.
        line = raw_msg.replace("\r", " ").replace("\n", " ")
        try:
            with self._state_lock:
                if not self._active:
                    return False
                self._wq.put_nowait(line)
            self._feed_frames += 1
            self._feed_chars += len(line)
            now = time.monotonic()
            if now - self._feed_report_at >= 5:
                self._queue_diagnostic(frames=self._feed_frames, chars=self._feed_chars)
                self._feed_frames = 0
                self._feed_chars = 0
                self._feed_report_at = now
        except queue.Full:
            self._queue_overflow()
            return False
        return True

    def recover(self, frames, timestamp: str, *, history=None, state=(), checkpoint=None) -> bool:
        """Queue a silent replay as one bounded item before accepting live events."""
        if not self._active or not self.supports_recovery():
            self._diagnostic("engine_recovery", state="recover_begin", result="rejected", reason="unsupported")
            return False
        command = {"nyaa_cmd": "recover_begin", "time": timestamp}
        if history is not None:
            if not self.supports_local_history():
                self._diagnostic("engine_recovery", state="recover_log", result="rejected", reason="unsupported")
                return False
            snapshots = []
            for raw in state:
                try:
                    data = json.loads(raw)
                except (ValueError, TypeError, RecursionError):
                    continue
                if isinstance(data, dict) and "nyaa_cmd" not in data:
                    snapshots.append(data)
            command.update(nyaa_cmd="recover_log", history=history, state=snapshots)
        return self._recovery_batch(frames, checkpoint, command=command)

    def catch_up(self, frames, checkpoint: int, *, finish=False) -> bool:
        if not self.supports_catchup():
            self._diagnostic("engine_recovery", state="catchup", result="rejected", reason="unsupported")
            return False
        return self._recovery_batch(frames, checkpoint, finish=finish)

    def _recovery_batch(self, frames, checkpoint, *, command=None, finish=False) -> bool:
        lines = [json.dumps(command)] if command else []
        for raw in frames:
            try:
                data = json.loads(raw)
            except (ValueError, TypeError, RecursionError):
                continue
            if isinstance(data, dict) and "nyaa_cmd" not in data:
                lines.append(raw.replace("\r", " ").replace("\n", " "))
        lines.append(json.dumps({"nyaa_cmd": "recover_end" if finish or checkpoint is None else "recover_checkpoint",
                                 "checkpoint": checkpoint}))
        state = command.get("nyaa_cmd") if command else ("recover_end" if finish else "catchup")
        state = state if state in ("recover_begin", "recover_log", "recover_end", "catchup") else "catchup"
        fields = dict(state=state, frames=len(lines) - 1 - bool(command),
                      checkpoint=checkpoint, history=bool(command and "history" in command))
        try:
            self._wq.put_nowait("\n".join(lines))
        except queue.Full:
            self._diagnostic("engine_recovery", result="rejected", reason="queue_full", **fields)
            return False
        self._diagnostic("engine_recovery", result="queued", queue_depth=self._wq.qsize(), **fields)
        return True

    def supports_recovery(self) -> bool:
        return self._active and self._recovery_gen == self._gen

    def supports_local_history(self) -> bool:
        return self.supports_recovery() and self._history_gen == self._gen

    def supports_catchup(self) -> bool:
        return self.supports_recovery() and self._catchup_gen == self._gen

    def _write_loop(self, proc: subprocess.Popen, wq: queue.Queue) -> None:
        gen = getattr(proc, "_nyaa_generation", self._gen)
        if proc.stdin is None:
            self._diagnostic("engine_error", gen, channel="writer", reason="unavailable")
            return
        frames = chars = 0
        reported_at = time.monotonic()
        while True:
            item = wq.get()
            if item is _STOP:
                break
            try:
                proc.stdin.write(item + "\n")
                proc.stdin.flush()
                frames += item.count("\n") + 1
                chars += len(item)
                now = time.monotonic()
                if now - reported_at >= 5:
                    self._queue_diagnostic(gen=gen, wq=wq, channel="writer", frames=frames, chars=chars)
                    frames = chars = 0
                    reported_at = now
            except (BrokenPipeError, OSError, ValueError) as exc:
                self._diagnostic("engine_error", gen, channel="writer", reason="write_failed",
                                 error_type=type(exc).__name__, queue_depth=wq.qsize())
                break
        self._queue_diagnostic(gen=gen, wq=wq, channel="writer", frames=frames, chars=chars)
        try:
            if proc.stdin is not None:
                proc.stdin.close()
        except OSError:
            pass

    def _read_loop(self, proc: subprocess.Popen, wq: queue.Queue, seq_state: dict, gen: int) -> None:
        reason = "eof"
        try:
            if proc.stdout is None:
                self._diagnostic("engine_error", gen, channel="stdout", reason="unavailable")
                return
            for line in _read_lines_bounded(proc.stdout):
                line = line.strip()
                if not line:
                    continue
                if not line.startswith("{"):
                    _log(f"[sidecar] {line}")
                    self._handle_diagnostic(line, gen)
                    continue
                try:
                    msg = json.loads(line)
                except (ValueError, RecursionError):
                    self._diagnostic("engine_protocol", gen, channel="stdout", reason="invalid_json", throttle_s=5)
                    log_drop("engine-parse", f"unparsed sidecar line {line[:160]!r}", 0)
                    continue
                try:
                    self._dispatch(msg, seq_state, gen)
                except Exception as exc:
                    self._diagnostic("engine_error", gen, channel="stdout", reason="dispatch_failed",
                                     error_type=type(exc).__name__, throttle_s=5)
                    _log(f"dispatch error: {exc!r}")
        except Exception as exc:
            reason = "exception"
            self._diagnostic("engine_error", gen, channel="stdout", reason="read_failed", error_type=type(exc).__name__)
            _log(f"reader error: {exc!r}")
        finally:
            with self._state_lock:
                if self._gen_live(gen):
                    self._diagnostic_ready_gen = gen
            self._flush_legacy_failures(gen)
            # Retire only this generation, including when stdout fails.
            with self._state_lock:
                was_current = proc is self._proc and self._active
                if was_current:
                    self._active = False
                    self._proc = None
            try:
                self._diagnostic("engine_exit", gen, reason=reason, expected=not was_current,
                                 returncode=proc.poll(),
                                 duration_ms=max(0, time.monotonic() - getattr(proc, "_nyaa_started_at", time.monotonic())) * 1000)
                if was_current:
                    self._queue_diagnostic(gen=gen, wq=wq, frames=self._feed_frames, chars=self._feed_chars)
                    _log(f"sidecar exited (returncode={proc.poll()})")
                    self.status.emit(False, "Sidecar exited", gen)
            finally:
                # Release the writer even if reporting the exit fails.
                try:
                    wq.put_nowait(_STOP)
                except queue.Full:
                    try:
                        wq.get_nowait()
                        wq.put_nowait(_STOP)
                    except (queue.Empty, queue.Full):
                        pass
                try:
                    self._reap(proc)
                finally:
                    if proc.stdout is not None:
                        try:
                            proc.stdout.close()
                        except OSError:
                            pass

    def _err_loop(self, proc: subprocess.Popen, gen: int) -> None:
        if proc.stderr is None:
            return
        try:
            for line in _read_lines_bounded(proc.stderr):
                line = line.rstrip()
                if not line:
                    continue
                if " ERROR " in line or "Exception" in line:
                    self._diagnostic("engine_error", gen, channel="stderr", reason="exception", throttle_s=5)
                _log(f"[sidecar stderr] {line}")
                self._handle_diagnostic(line, gen)
        except Exception as exc:
            self._diagnostic("engine_error", gen, channel="stderr", reason="read_failed", error_type=type(exc).__name__)
            raise
        finally:
            try:
                proc.stderr.close()
            except OSError:
                pass

    def _handle_diagnostic(self, line: str, gen: int) -> None:
        # Some Xvfb wrappers merge stderr into stdout.
        if "Error in sequential trigger" in line:
            log_drop("engine-chain", line, 0)
            if self._structured_diagnostics_gen != gen:
                self._diagnostic("engine_error", gen, channel="stderr", reason="exception")
            emit = False
            with self._state_lock:
                if self._gen_live(gen) and self._structured_diagnostics_gen != gen:
                    if self._diagnostic_ready_gen == gen:
                        emit = True
                    else:
                        if self._legacy_failure_gen != gen:
                            self._legacy_failure_gen = gen
                            self._legacy_failures = []
                            self._legacy_failure_count = 0
                        self._legacy_failures = (self._legacy_failures + [line[:4096]])[-50:]
                        self._legacy_failure_count += 1
            if emit:
                self.chain_failure.emit(line, gen)
        if "reading WS messages on stdin" in line:
            with self._state_lock:
                if not self._gen_live(gen):
                    return
                if "recovery=1" in line:
                    self._recovery_gen = gen
                if "history=1" in line:
                    self._history_gen = gen
                if "catchup=1" in line:
                    self._catchup_gen = gen
                if "custom=1" in line:
                    self._custom_gen = gen
                if "speech_cancel=1" in line:
                    self._speech_cancel_gen = gen
                if "speech_cancel_all=1" in line:
                    self._speech_cancel_all_gen = gen
                if "diagnostics=1" in line:
                    self._structured_diagnostics_gen = gen
                self._diagnostic_ready_gen = gen
            self._flush_legacy_failures(gen)
            self._diagnostic("engine_ready", gen, recovery=self._recovery_gen == gen,
                             history=self._history_gen == gen, catchup=self._catchup_gen == gen,
                             custom=self._custom_gen == gen, speech_cancel=self._speech_cancel_gen == gen,
                             speech_cancel_all=self._speech_cancel_all_gen == gen)
            self.ready.emit(gen)

    def _flush_legacy_failures(self, gen: int) -> None:
        with self._state_lock:
            if self._legacy_failure_gen != gen:
                return
            count, lines = self._legacy_failure_count, self._legacy_failures
            self._legacy_failure_count, self._legacy_failures = 0, []
            if self._structured_diagnostics_gen == gen:
                return
        for index in range(count):
            if not self._gen_live(gen):
                return
            offset = index - (count - len(lines))
            line = lines[offset] if offset >= 0 else "Error in sequential trigger before readiness"
            self.chain_failure.emit(line, gen)


    def _delivery_block(self, cid, gen: int, tts_only: bool):
        with self._state_lock:
            if not self._gen_live(gen):
                return "stale"
            if cid in self._disabled:
                return "disabled"
            if cid in self._speech_cancel_pending:
                return "cancel_pending"
            if self._speech_cancel_all_pending is not None:
                return "cancel_all_pending"
            if tts_only and self._speech_cancel_overflow == gen:
                return "cancel_overflow_pending"
        return None

    def _deliver_callout(self, payload, gen: int) -> None:
        cid, text, tts, severity, tts_only = payload[:5]
        seq = payload[5] if len(payload) > 5 else None
        queued_at = payload[6] if len(payload) > 6 else time.monotonic()
        queue_delay_ms = max(0, time.monotonic() - queued_at) * 1000
        for channel, content in (("text", text if not tts_only else ""), ("tts", tts)):
            if not content:
                continue
            reason = self._delivery_block(cid, gen, tts_only)
            if reason is None:
                if channel == "text":
                    self.callout.emit(text, severity, gen)
                else:
                    self.tts.emit(tts, gen)
            self._diagnostic("engine_callout", gen, seq=seq, channel=channel,
                             queue_delay_ms=queue_delay_ms, tts_only=tts_only,
                             result="emitted" if reason is None else "filtered", reason=reason)

    def _acknowledge_speech_cancel(self, token, gen: int) -> None:
        with self._state_lock:
            accepted = self._gen_live(gen) and type(token) is int
            if accepted:
                if token == self._speech_cancel_all_pending:
                    self._speech_cancel_all_pending = None
                self._speech_cancel_pending = {
                    cid: pending for cid, pending in self._speech_cancel_pending.items()
                    if pending != token}
            pending_ids = len(self._speech_cancel_pending)
            all_pending = self._speech_cancel_all_pending is not None
        self._diagnostic("engine_cancel", gen, token=token, pending_ids=pending_ids,
                         speech_cancel_all=all_pending, result="acknowledged" if accepted else "ignored")

    def _dispatch(self, msg: dict, seq_state: "dict | None" = None,
                  gen: "int | None" = None) -> None:
        kind = msg.get("t")
        if kind == "callout":
            # Check sequence gaps before filtering callouts, within this reader generation.
            if seq_state is None:
                seq_state = {}
            seq = msg.get("seq")
            self._diagnostic("engine_callout", gen, seq=seq, result="received",
                             has_text=bool(msg.get("text")), has_tts=bool(msg.get("tts")),
                             tts_only=msg.get("tts_only") is True)
            if isinstance(seq, int):
                last = seq_state.get("last")
                seq_state["last"] = seq
                if last is not None and seq > last + 1:
                    self._diagnostic("engine_protocol", gen, seq=seq, reason="sequence_gap", dropped=seq - last - 1)
                    log_drop("engine-seq",
                             f"callout seq gap {last} -> {seq}, "
                             f"{seq - last - 1} lost between engine and program", 0)
                elif last is not None and seq <= last:
                    self._diagnostic("engine_protocol", gen, seq=seq, reason="sequence_regression")
            # Reject old generations here and again in queued UI slots.
            if not self._gen_live(gen):
                self._diagnostic("engine_callout", gen, seq=seq, result="filtered", reason="stale")
                return
            cid = msg.get("id")
            if cid and cid in self._disabled:
                self._diagnostic("engine_callout", gen, seq=seq, result="filtered", reason="disabled")
                return
            text = (msg.get("text") or "").strip()
            tts = (msg.get("tts") or "").strip()
            tts_only = msg.get("tts_only") is True
            sev = msg.get("severity", "info")
            if sev not in ("info", "alert", "alarm"):
                sev = "info"
            # Record original phrases before overrides. Empty replacements suppress callouts.
            for phrase in (tts, text):
                self._record_seen(phrase)
            had_engine_text = bool(text)
            had_engine_tts = bool(tts)
            text = apply_replacements(text, self._replacements)
            tts = apply_replacements(tts, self._replacements)
            if not text and tts and not had_engine_text and not tts_only:
                # Preserve intentional suppression by replacement rules.
                text = tts
            if not tts and not (text and not tts_only):
                self._diagnostic("engine_callout", gen, seq=seq, result="filtered",
                                 reason="replacement_empty" if had_engine_tts or (had_engine_text and not tts_only) else "empty")
            elif had_engine_text and not text and not tts_only:
                self._diagnostic("engine_callout", gen, seq=seq, channel="text", result="filtered", reason="replacement_empty")
            elif had_engine_tts and not tts:
                self._diagnostic("engine_callout", gen, seq=seq, channel="tts", result="filtered", reason="replacement_empty")
            self._callout_ready.emit((cid, text, tts, sev, tts_only, seq, time.monotonic()), gen)
        elif kind == "diagnostic":
            clean = record_engine(msg, gen=gen)
            if clean is None:
                self._diagnostic("engine_protocol", gen, reason="unknown_type", throttle_s=5)
            elif self._gen_live(gen):
                with self._state_lock:
                    self._structured_diagnostics_gen = gen
                self._flush_legacy_failures(gen)
                if clean.get("event") == "sequence_failed":
                    trigger = clean.get("trigger_class", "SequentialTrigger").rsplit(".", 1)[-1]
                    field = clean.get("trigger_field")
                    if field:
                        trigger += "." + field
                    waiting = clean.get("wait_event", clean.get("wait_kind", "unknown"))
                    error = clean.get("error_type", "Exception")
                    self.chain_failure.emit(
                        f"Error in sequential trigger '{trigger}' while waiting for '{waiting}': {error}", gen)
        elif kind == "speech_canceled":
            if self._gen_live(gen):
                self._speech_cancel_ready.emit(msg.get("token"), gen)
        elif kind == "status":
            if not self._gen_live(gen):
                return
            active = bool(msg.get("active", self._active))
            self._diagnostic("engine_protocol", gen, state="status", result="received", active=active)
            self.status.emit(active, str(msg.get("message", "")), gen)
        elif kind == "custom_triggers":
            self._diagnostic("engine_protocol", gen, state="custom_triggers", result="received",
                             accepted=msg.get("ok") is True)
            if self._gen_live(gen):
                self.custom_status.emit(msg.get("ok") is True, str(msg.get("message", "")), gen)
        elif kind in ("recovery_checkpoint", "recovered"):
            self._diagnostic("engine_recovery", gen, state=kind, result="received",
                             checkpoint=msg.get("checkpoint"), dropped=msg.get("skipped"),
                             history_status=msg.get("status"), accepted=self._gen_live(gen))
            if self._gen_live(gen):
                self.recovery_progress.emit(msg, gen)
        elif kind == "combatants_request":
            ids = msg.get("ids")
            if self._gen_live(gen) and isinstance(ids, list) and len(ids) <= 1000:
                if all(type(actor) is int and 0 < actor <= 0xFFFFFFFF for actor in ids):
                    self._diagnostic("engine_protocol", gen, state="combatants_request", result="received",
                                     count=len(ids))
                    self.combatants_request.emit(ids, gen)
        elif kind == "inventory":
            triggers = msg.get("triggers")
            if self._gen_live(gen) and isinstance(triggers, list):
                self._diagnostic("engine_protocol", gen, state="inventory", result="received", count=len(triggers))
                self.inventory.emit(json.dumps(triggers), gen)
        elif kind == "automark_inventory":
            if (self._gen_live(gen) and msg.get("version") == 1
                    and all(isinstance(msg.get(key), list)
                            for key in ("mechanics", "settings", "jobs", "markers"))):
                self._diagnostic("engine_protocol", gen, state="automark_inventory", result="received",
                                 count=len(msg["mechanics"]))
                self.automark_inventory.emit(json.dumps(msg), gen)
        elif kind == "telesto":
            if not self._gen_live(gen):
                return
            st = str(msg.get("status", "unknown")).lower()
            if st not in ("good", "bad", "unknown"):
                st = "unknown"
            self._diagnostic("engine_protocol", gen, state="telesto", result="received", active=st == "good")
            self.telesto.emit(st, gen)
        else:
            self._diagnostic("engine_protocol", gen, reason="unknown_type", throttle_s=5)
