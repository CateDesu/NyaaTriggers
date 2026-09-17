#!/usr/bin/env python3
"""Run the GPL-3.0 Triggevent engine from xpdota/event-trigger through the maintained fork.
Send raw IINACT JSON messages to triggevent-core and relay resolved callouts as Qt
signals. The program can run without Java or the jar, so check availability before
starting.
"""

from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from nyaatriggers.paths import bundle_root, source_root

from PyQt6.QtCore import QObject, pyqtSignal

from nyaatriggers import proc_env
from nyaatriggers.drop_log import log_drop, open_private_log, rotate_one_generation
from nyaatriggers.trigger_engine import _safe_sub, compile_user_regex

# Search both bundled resource and executable directories because jars and JRE data may
# be packaged separately.
_BASE = bundle_root()
_JAVA_EXE = "java.exe" if os.name == "nt" else "java"

# Discard oversized stdout lines without buffering them whole.
_MAX_LINE = 1 << 20

# Bound queued bytes as well as message count because feed frames can be large.
_MAX_QUEUE_BYTES = 64 << 20


def _read_lines_bounded(stream):
    """Yield bounded lines and discard oversized input through its newline."""
    while True:
        line = stream.readline(_MAX_LINE + 1)
        if not line:
            return
        # A complete line at the limit includes its newline and remains valid.
        if len(line) > _MAX_LINE and not line.endswith("\n"):
            while True:
                more = stream.readline(_MAX_LINE + 1)
                if not more or more.endswith("\n"):
                    break
            continue
        yield line


class _ByteQueue(queue.Queue):
    """Bound queued strings by bytes and count. Overflow requires engine recovery to
    preserve control and event ordering. Stop sentinels use no byte budget.
    """

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


def _bundle_bases() -> "list[Path]":
    """Search candidate bundle directories in priority order without duplicates."""
    bases: list[Path] = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        bases.append(Path(meipass))
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        bases += [exe_dir, exe_dir / "_internal", exe_dir.parent,
                  exe_dir.parent / "Resources", exe_dir.parent / "Frameworks"]
    bases.append(source_root())
    seen: set = set()
    out: list[Path] = []
    for b in bases:
        try:
            key = b.resolve()
        except OSError:
            key = b
        if key not in seen:
            seen.add(key)
            out.append(b)
    return out


def _bundled_jre_dir() -> "Path | None":
    for base in _bundle_bases():
        if (base / "jre" / "bin" / _JAVA_EXE).exists():
            return base / "jre"
    return None


def _log_dir() -> Path:
    """Keep diagnostic logs in a writable user directory."""
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


# Serialize log rotation and append across GUI and reader threads.
_LOG_LOCK = threading.Lock()


def _log(msg: str) -> None:
    """Write diagnostics to a persistent log and stderr. Logging failures must not stop the
    engine.
    """
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
    """Check for a bundled or system Java runtime."""
    return _find_java() is not None


_CORE_DIR     = _BASE / "triggevent-core"
_ET_DIR       = _CORE_DIR / "event-trigger"
_BUILD_SCRIPT = _CORE_DIR / ("build.bat" if os.name == "nt" else "build.sh")
# Use the maintained fork. Upstream changes enter through merges to its main branch.
_ET_REPO_URL  = "https://github.com/CateDesu/event-trigger.git"
_ET_BRANCH    = "main"
# Record source HEAD only after a successful jar build. Clone state alone cannot prove
# the jar is current.
_JAR_STAMP    = _CORE_DIR / "target" / "triggevent-core.jar.built-from"


def _jar_built_from() -> "str | None":
    """Return the jar build commit, or None when the stamp is unavailable."""
    try:
        return _JAR_STAMP.read_text(encoding="ascii").strip() or None
    except (OSError, ValueError):
        return None


def _kill_build_tree(proc) -> None:
    """Stop the entire build tree so a timed out wrapper cannot leave Maven or compilers
    using the checkout.
    """
    try:
        if os.name == "posix":
            import signal
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        else:
            # Terminate the Windows build tree because killing cmd.exe alone leaves
            # children running.
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True)
    except (OSError, ProcessLookupError):
        try:
            proc.kill()
        except OSError:
            pass


def update_engine(channel: str = "stable", manual: bool = False) -> "tuple[bool, str]":
    """Update source checkouts and rebuild when the toolchain is available. Otherwise
    install the published jar on manual request. Run on a background thread and return
    whether it changed. Engine downloads use the stable channel.
    """
    if getattr(sys, "frozen", False):
        if not manual:
            return (False, "The engine is bundled with the program")
        return _download_engine("stable")
    # Use the prebuilt jar when source or build tools are missing. POSIX source builds
    # also require bash.
    tools = ("git", "java", "mvn") if os.name == "nt" else ("git", "java", "mvn", "bash")
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
        r = _git("rev-list", "--count", f"HEAD..origin/{_ET_BRANCH}")
        if r.returncode != 0:
            return (False, f"Triggevent rev-list failed: {r.stderr.strip()[:200]}")
        h = _git("rev-parse", f"origin/{_ET_BRANCH}")
        if h.returncode != 0:
            return (False, f"Triggevent rev-parse failed: {h.stderr.strip()[:200]}")
        head = h.stdout.strip()
        behind = r.stdout.strip()
        if not behind.isdigit() or int(behind) == 0:
            # Use the successful build stamp because an updated checkout may still have
            # an older jar after a failed build.
            if _jar_built_from() == head:
                return (False, "Triggevent Engine already up to date")
        # Remove legacy patch edits before fast-forwarding. Committed divergence still
        # fails the merge.
        _git("checkout", "--", ".")
        if _git("merge", "--ff-only", f"origin/{_ET_BRANCH}").returncode != 0:
            return (False, "Triggevent pull skipped: local event-trigger clone has diverged or has uncommitted changes")
        # Require a clean checkout so the build stamp identifies the compiled source.
        d = _git("status", "--porcelain")
        if d.returncode == 0 and d.stdout.strip():
            return (False, "Triggevent pull skipped: local event-trigger clone has uncommitted changes")
        cmd = [str(_BUILD_SCRIPT)] if os.name == "nt" else ["bash", str(_BUILD_SCRIPT)]
        # Build the newly merged commit instead of restoring the old pin.
        env = {**os.environ, "EVENT_TRIGGER_REF": f"origin/{_ET_BRANCH}"}
        # Use Popen so timeout cleanup can terminate the full build tree on either
        # platform.
        popen_kwargs = dict(stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, env=env)
        if os.name == "posix":
            popen_kwargs["start_new_session"] = True
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
    if proc.returncode != 0:
        return (False, f"Triggevent rebuild failed:\n{b_err.strip()[-400:]}")
    # Stamp only after the jar build succeeds.
    try:
        _JAR_STAMP.write_text(head + "\n", encoding="ascii")
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
    """Download and atomically install the published jar, including on source installs
    without a local build.
    """
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
    # Remove old temporary jar downloads left by interrupted processes.
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
    # Reject downloads that are not valid ZIP archives.
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
    # Apply the existing release asset verification before installing the jar.
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
    # A full frozen update replaces this jar with its bundled engine.
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

    # Stamp UI signals with their generation so queued output can be rejected after
    # restart.
    callout     = pyqtSignal(str, str, int)   # on-screen text, severity in {info, alert, alarm}, generation
    tts         = pyqtSignal(str, int)        # spoken text, generation
    status      = pyqtSignal(bool, str, int)  # active, message, generation
    phrase_seen = pyqtSignal(str)        # a callout phrase observed, for the override UI
    inventory   = pyqtSignal(str)        # one-shot JSON [{id,name,fight,group,text}] of all engine callouts
    telesto     = pyqtSignal(str, int)   # Telesto automark connection status, "good"|"bad"|"unknown", generation
    ready       = pyqtSignal(int)        # sidecar is reading stdin, generation
    chain_failure = pyqtSignal(str, int)  # an engine chain died, the "Error in sequential trigger" line, generation
    combatants_request = pyqtSignal(object, int)
    recovery_progress = pyqtSignal(object, int)
    feed_overflow = pyqtSignal(int)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._proc: subprocess.Popen | None = None
        self._recovery_gen = -1
        self._history_gen = -1
        self._catchup_gen = -1
        self._overflow_gen = -1
        self._reader: threading.Thread | None = None
        self._errpump: threading.Thread | None = None
        self._writer: threading.Thread | None = None
        self._wq: queue.Queue = _ByteQueue(maxsize=10000)
        self._active = False
        # Increment on start and stop. An active flag alone cannot identify output from
        # a replaced reader.
        self._gen = 0
        # Replace the rules list atomically so the reader can use a stable snapshot.
        self._replacements: list = []
        # Replace disabled IDs atomically for reader access.
        self._disabled: frozenset = frozenset()
        self._seen: dict = {}            # ordered set of observed callout phrases
        # Lock phrase insertion and eviction because the GUI reads snapshots
        # concurrently.
        self._seen_lock = threading.Lock()
        # Makes stop and reader-exit check-and-clear of the _proc/_active pair atomic.
        self._state_lock = threading.Lock()

    @staticmethod
    def is_available() -> bool:
        return is_available()

    def is_active(self) -> bool:
        return self._active

    def generation(self) -> int:
        """Current generation used by UI slots to reject stale signals."""
        return self._gen

    def _gen_live(self, gen: "int | None") -> bool:
        """Accept only the active generation."""
        return gen is not None and gen == self._gen and self._active

    def set_replacements(self, rules: list) -> None:
        """Replace callout rules atomically. An empty replacement result suppresses the
        callout.
        """
        self._replacements = list(rules or [])

    def set_disabled(self, ids) -> None:
        """Replace disabled IDs for the next callout without restarting."""
        self._disabled = frozenset(ids or ())

    def set_callout(self, cid: str, tts: str | None = None,
                    text: str | None = None, enable: bool | None = None) -> None:
        """Update engine text and enabled state while preserving template tokens. The
        caller replays edits after restart.
        """
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

    def set_automark(self, enable: bool, uri: str | None = None) -> None:
        """Configure native engine automarking through Telesto. It starts disabled, and
        callers replay settings after restart.
        """
        cmd: dict = {"nyaa_cmd": "set_automark", "enable": bool(enable)}
        if uri:
            cmd["uri"] = str(uri)
        self._send_command(cmd)

    def _send_command(self, cmd: dict) -> None:
        """Queue a control command and recover if the engine cannot keep up."""
        if not self._active:
            return
        try:
            line = json.dumps(cmd)
        except (TypeError, ValueError):
            return
        try:
            self._wq.put_nowait(line)
        except queue.Full:
            self._queue_overflow()

    def _queue_overflow(self) -> None:
        if self._active and self._overflow_gen != self._gen:
            self._overflow_gen = self._gen
            log_drop("engine-feed", "sidecar stdin queue full; restarting pull recovery")
            self.feed_overflow.emit(self._gen)

    def seen_phrases(self) -> list:
        with self._seen_lock:
            return list(self._seen.keys())

    def _apply_replacements(self, s: str) -> str:
        rules = self._replacements
        if not rules or not s:
            return s.strip()
        out = s
        for r in rules:
            if not r.get("enabled", True):
                continue
            # Normalize edited replacement rules before dispatch.
            find = r.get("find") or ""
            if not isinstance(find, str):
                find = str(find)
            if not find:
                continue
            repl = r.get("replace", "") or ""
            if not isinstance(repl, str):
                repl = str(repl)
            pat = find if r.get("regex") else re.escape(find)
            rx = compile_user_regex(pat, re.IGNORECASE)
            if rx is None:
                continue
            # Bound regex substitution and preserve the callout if the replacement is
            # invalid.
            out = _safe_sub(rx, repl, out)
        return out.strip()

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
        """Start if not already running. Check availability first."""
        if self._active:
            return
        _log(f"start() requested (os={os.name})")
        java = _find_java()
        jar = _find_jar()
        if java is None or jar is None:
            _log(f"cannot start: java={java!r} jar={jar!r}")
            self.status.emit(False, "Java runtime or triggevent-core.jar not found", self._gen)
            return

        # Restore executable permissions on bundled Java tools.
        if os.name == "posix" and _bundled_jre_dir() is not None:
            _make_bundled_jre_executable()

        # Use Xvfb for Swing initialization when available. Cap the heap at 512 MiB
        # because 256 MiB caused GC pauses and sequential trigger timeouts during long
        # encounters.
        cmd = [java, "-Xmx512m", "-jar", str(jar)]
        xvfb = shutil.which("xvfb-run")
        if xvfb:
            # Use 24-bit visuals for reliable Swing initialization.
            cmd = [xvfb, "-a", "-s", "-screen 0 1024x768x24"] + cmd

        # Give the wrapper and JVM their own process group for shutdown.
        popen_kwargs = dict(
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            bufsize=1, text=True, encoding="utf-8", errors="replace",
            cwd=str(jar.parent.parent),
        )
        if os.name == "posix":
            popen_kwargs["start_new_session"] = True
            # Let stdin EOF stop the host after parent death. A parent-death signal
            # would stop the Xvfb wrapper before it cleans up its child.
        if os.name == "nt":
            # Hide the Java console in Windows GUI builds.
            popen_kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
        # Restore system library paths for the JVM and shell children.
        popen_kwargs["env"] = proc_env.child_env()

        try:
            proc = subprocess.Popen(cmd, **popen_kwargs)
        except OSError as e:
            _log(f"launch failed: {e!r}")
            self.status.emit(False, f"Failed to launch sidecar: {e}", self._gen)
            self._proc = None
            return
        _log(f"sidecar spawned pid={proc.pid}")

        wq = _ByteQueue(maxsize=10000)
        with self._state_lock:
            self._proc = proc
            self._wq = wq
            self._active = True
            self._gen += 1
            gen = self._gen
        # Bind each worker to its process, queue and generation. Keep the callout
        # sequence watermark with that generation because the jar restarts numbering at
        # one.
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
            return
        with self._state_lock:
            self._active = False
            # Invalidate queued output from the stopped generation.
            self._gen += 1
            gen = self._gen
            proc, self._proc = self._proc, None
            wq = self._wq
        # Clear observed phrases so the next generation can report them again.
        with self._seen_lock:
            self._seen.clear()
        # Free a queue slot for the writer stop sentinel.
        try:
            wq.put_nowait(_STOP)
        except queue.Full:
            try:
                wq.get_nowait()
                wq.put_nowait(_STOP)
            except (queue.Empty, queue.Full):
                pass
        if proc is not None:
            # Signal the process group before returning because the parent may exit
            # before a background reaper runs.
            self._signal_group(proc, graceful=True)
            if wait:
                # Wait for Java to release bundle files before a Windows update replaces
                # them.
                self._reap(proc)
            else:
                # Complete ordinary shutdown cleanup off the GUI thread.
                threading.Thread(target=self._reap, args=(proc,), daemon=True,
                                 name="triggevent-reap").start()
        self.status.emit(False, "Off", gen)

    @staticmethod
    def _signal_group(proc: subprocess.Popen, graceful: bool) -> None:
        """Signal the process group on POSIX or the process on Windows. Fall back to the
        direct child when the group is unavailable.
        """
        try:
            if os.name == "posix":
                import signal
                # The process group may survive its wrapper.
                os.killpg(proc.pid,
                          signal.SIGTERM if graceful else signal.SIGKILL)
            else:
                proc.terminate() if graceful else proc.kill()
        except (OSError, ProcessLookupError):
            try:
                proc.terminate() if graceful else proc.kill()
            except OSError:
                pass

    @classmethod
    def _reap(cls, proc: subprocess.Popen) -> None:
        """Allow the sidecar to exit, then kill any surviving group members."""
        # Serialize cleanup shared by the reader and explicit stop.
        lock = proc.__dict__.setdefault("_nyaa_reap_lock", threading.Lock())
        with lock:
            if getattr(proc, "_nyaa_reaped", False):
                return
            try:
                deadline = time.monotonic() + 4
                try:
                    proc.wait(timeout=4)
                except subprocess.TimeoutExpired:
                    pass
                else:
                    if os.name != "posix":
                        return
                    # Check child processes as well as the Xvfb wrapper.
                    while time.monotonic() < deadline:
                        try:
                            os.killpg(proc.pid, 0)
                        except ProcessLookupError:
                            return
                        except OSError:
                            break
                        time.sleep(0.05)
                cls._signal_group(proc, graceful=False)
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass
            finally:
                proc._nyaa_reaped = True


    def feed(self, raw_msg: str) -> None:
        """Queue raw feed messages without blocking the GUI. Overflow requests engine
        recovery.
        """
        if not self._active or not raw_msg:
            return
        try:
            data = json.loads(raw_msg)
        except (ValueError, TypeError, RecursionError):
            log_drop("engine-feed", "discarded invalid feed JSON")
            return
        if not isinstance(data, dict) or "nyaa_cmd" in data:
            log_drop("engine-feed", "discarded feed frame outside the event protocol")
            return
        # Keep each JSON message on one protocol line.
        line = raw_msg.replace("\r", " ").replace("\n", " ")
        try:
            self._wq.put_nowait(line)
        except queue.Full:
            self._queue_overflow()

    def recover(self, frames, timestamp: str, *, history=None, state=(), checkpoint=None) -> bool:
        """Queue a silent replay as one bounded item before accepting live events."""
        if not self._active or not self.supports_recovery():
            return False
        command = {"nyaa_cmd": "recover_begin", "time": timestamp}
        if history is not None:
            if not self.supports_local_history():
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
        try:
            self._wq.put_nowait("\n".join(lines))
        except queue.Full:
            return False
        return True

    def supports_recovery(self) -> bool:
        return self._active and self._recovery_gen == self._gen

    def supports_local_history(self) -> bool:
        return self.supports_recovery() and self._history_gen == self._gen

    def supports_catchup(self) -> bool:
        return self.supports_recovery() and self._catchup_gen == self._gen

    def _write_loop(self, proc: subprocess.Popen, wq: queue.Queue) -> None:
        if proc.stdin is None:
            return
        while True:
            item = wq.get()
            if item is _STOP:
                break
            try:
                proc.stdin.write(item + "\n")
                proc.stdin.flush()
            except (BrokenPipeError, OSError, ValueError):
                break
        try:
            if proc.stdin is not None:
                proc.stdin.close()
        except OSError:
            pass

    def _read_loop(self, proc: subprocess.Popen, wq: queue.Queue, seq_state: dict, gen: int) -> None:
        try:
            if proc.stdout is None:
                return
            for line in _read_lines_bounded(proc.stdout):
                line = line.strip()
                if not line:
                    continue
                if not line.startswith("{"):
                    _log(f"[sidecar] {line}")
                    continue
                try:
                    msg = json.loads(line)
                except (ValueError, RecursionError):
                    log_drop("engine-parse", f"unparsed sidecar line {line[:160]!r}", 0)
                    continue
                try:
                    self._dispatch(msg, seq_state, gen)
                except Exception as exc:
                    _log(f"dispatch error: {exc!r}")
        except Exception as exc:
            _log(f"reader error: {exc!r}")
        finally:
            # Retire only this generation, including when stdout fails.
            with self._state_lock:
                was_current = proc is self._proc and self._active
                if was_current:
                    self._active = False
                    self._proc = None
            try:
                if was_current:
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
                self._reap(proc)

    def _err_loop(self, proc: subprocess.Popen, gen: int) -> None:
        if proc.stderr is None:
            return
        for line in _read_lines_bounded(proc.stderr):
            line = line.rstrip()
            if not line:
                continue
            _log(f"[sidecar stderr] {line}")
            # Report sequential trigger failures from stderr only for the current
            # generation.
            if "Error in sequential trigger" in line:
                log_drop("engine-chain", line, 0)
                if self._gen_live(gen):
                    self.chain_failure.emit(line, gen)
            # Replay world state once the live sidecar is reading stdin.
            if "reading WS messages on stdin" in line:
                with self._state_lock:
                    if not self._gen_live(gen):
                        continue
                    if "recovery=1" in line:
                        self._recovery_gen = gen
                    if "history=1" in line:
                        self._history_gen = gen
                    if "catchup=1" in line:
                        self._catchup_gen = gen
                self.ready.emit(gen)

    def _dispatch(self, msg: dict, seq_state: "dict | None" = None,
                  gen: "int | None" = None) -> None:
        kind = msg.get("t")
        if kind == "callout":
            # Check sequence gaps before filtering callouts. Keep the watermark local to
            # this reader generation.
            if seq_state is None:
                seq_state = {}
            seq = msg.get("seq")
            if isinstance(seq, int):
                last = seq_state.get("last")
                seq_state["last"] = seq
                if last is not None and seq > last + 1:
                    log_drop("engine-seq",
                             f"callout seq gap {last} -> {seq}, "
                             f"{seq - last - 1} lost between engine and program", 0)
            # Reject old generation output before dispatch. UI slots also recheck queued
            # signals.
            if not self._gen_live(gen):
                return
            cid = msg.get("id")
            if cid and cid in self._disabled:
                return
            text = (msg.get("text") or "").strip()
            tts = (msg.get("tts") or "").strip()
            sev = msg.get("severity", "info")
            if sev not in ("info", "alert", "alarm"):
                sev = "info"
            # Record original phrases before applying overrides. Empty results mean
            # suppression.
            for phrase in (tts, text):
                self._record_seen(phrase)
            had_engine_text = bool(text)
            text = self._apply_replacements(text)
            tts = self._apply_replacements(tts)
            if not text and tts and not had_engine_text:
                # Use TTS as display text only when no visual text was supplied.
                # Preserve intentional suppression by replacement rules.
                text = tts
            if text:
                self.callout.emit(text, sev, gen)
            if tts:
                self.tts.emit(tts, gen)
        elif kind == "status":
            # Ignore status from stopped or replaced generations.
            if not self._gen_live(gen):
                return
            active = bool(msg.get("active", self._active))
            self.status.emit(active, str(msg.get("message", "")), gen)
        elif kind in ("recovery_checkpoint", "recovered"):
            if self._gen_live(gen):
                self.recovery_progress.emit(msg, gen)
        elif kind == "combatants_request":
            ids = msg.get("ids")
            if self._gen_live(gen) and isinstance(ids, list) and len(ids) <= 1000:
                if all(type(actor) is int and 0 < actor <= 0xFFFFFFFF for actor in ids):
                    self.combatants_request.emit(ids, gen)
        elif kind == "inventory":
            triggers = msg.get("triggers")
            if isinstance(triggers, list):
                self.inventory.emit(json.dumps(triggers))
        elif kind == "telesto":
            # Ignore stale Telesto status. Keep inventory ungated because it remains
            # useful after restart.
            if not self._gen_live(gen):
                return
            st = str(msg.get("status", "unknown")).lower()
            if st not in ("good", "bad", "unknown"):
                st = "unknown"
            self.telesto.emit(st, gen)
