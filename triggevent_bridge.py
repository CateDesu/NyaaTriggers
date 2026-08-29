#!/usr/bin/env python3
"""Bridge to the headless Triggevent Engine sidecar, `triggevent-core`.

Runs Triggevent's real engine, xpdota/event-trigger, GPL-3.0, as a JVM
subprocess. Every raw IINACT/OverlayPlugin WS message is teed to the sidecar's
stdin, one JSON per line. It runs all Triggevent triggers, built-in Java plus
user Groovy, and writes resolved callouts to stdout as JSON lines.

Mirrors the CactbotReader interface, callout/tts/status signals and
start/feed/stop, so it drops into the same main_window wiring.

The JVM is optional, so call is_available first. The app runs fine without
Java or the jar. No heavy imports at module load.
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

from PyQt6.QtCore import QObject, pyqtSignal

import proc_env
from drop_log import log_drop
from trigger_engine import _safe_sub, compile_user_regex

# PyInstaller puts the a.datas jar under sys._MEIPASS, but the Tree-added JRE
# often lands next to the executable instead. Missing it makes has_java False
# and hides the whole feature, so _bundle_bases covers every plausible spot.
_BASE = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
_JAVA_EXE = "java.exe" if os.name == "nt" else "java"

# A single sidecar stdout line longer than this is discarded without buffering
# it whole. Mirrors the 1 MiB cap applied to Telesto responses. A buggy or
# hostile sidecar must not be able to OOM the app by emitting one giant line.
_MAX_LINE = 1 << 20

# Byte budget for the sidecar stdin queue, on top of its item count. One raw
# WS message can be up to 4 MiB, so a sidecar stalled on stdin while a
# hostile or buggy peer floods it would otherwise pin tens of GB before the
# count cap engaged.
_MAX_QUEUE_BYTES = 64 << 20


def _read_lines_bounded(stream):
    """Yield lines from a text stream, skipping any single line longer than
    _MAX_LINE chars. An over-long line drains to its newline, never held whole."""
    while True:
        line = stream.readline(_MAX_LINE + 1)
        if not line:
            return
        # At the cap with its newline is still a complete line. Only a chunk
        # that fills the read window without one is genuinely overlong.
        if len(line) > _MAX_LINE and not line.endswith("\n"):
            while True:
                more = stream.readline(_MAX_LINE + 1)
                if not more or more.endswith("\n"):
                    break
            continue
        yield line


class _ByteQueue(queue.Queue):
    """queue.Queue with a byte budget on top of the item count.

    Items are sidecar stdin lines. put_nowait raises Full once the queued
    payload bytes pass the budget, so the drop oldest policy at the call
    sites covers byte pressure unchanged. The _STOP sentinel is not a str
    and always fits the byte budget. The item count cap still applies.
    """

    def __init__(self, maxsize: int, maxbytes: int = _MAX_QUEUE_BYTES) -> None:
        super().__init__(maxsize)
        self._maxbytes = maxbytes
        self._nbytes = 0

    def put_nowait(self, item) -> None:
        n = len(item) if isinstance(item, str) else 0
        if self._nbytes + n > self._maxbytes:
            raise queue.Full
        super().put_nowait(item)

    def _put(self, item) -> None:
        super()._put(item)
        if isinstance(item, str):
            self._nbytes += len(item)

    def _get(self):
        item = super()._get()
        if isinstance(item, str):
            self._nbytes -= len(item)
        return item


def _bundle_bases() -> "list[Path]":
    """Candidate dirs for the bundled jre/ and the sidecar jar, in priority order, deduped."""
    bases: list[Path] = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        bases.append(Path(meipass))
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        bases += [exe_dir, exe_dir / "_internal", exe_dir.parent,
                  exe_dir.parent / "Resources", exe_dir.parent / "Frameworks"]
    bases.append(Path(__file__).resolve().parent)
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
    """Bundled JRE root, the .../jre dir, or None on a source/dev build."""
    for base in _bundle_bases():
        if (base / "jre" / "bin" / _JAVA_EXE).exists():
            return base / "jre"
    return None


def _log_dir() -> Path:
    """Writable per-user dir for the sidecar's diagnostic log. Never the
    possibly read-only install dir."""
    if os.name == "nt":
        # `or`, not a get default. An APPDATA set-but-empty would yield
        # Path built from an empty string, the current directory.
        root = Path(os.environ.get("APPDATA") or Path.home())
    else:
        root = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
    d = root / "nyaatriggers"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _log_path() -> "Path | None":
    """Path to the Triggevent diagnostic log, or None when its dir can't be made."""
    try:
        return _log_dir() / "triggevent.log"
    except OSError:
        return None


# _log is called from the GUI, reader and stderr-pump threads. Serialize the
# size check, unlink, append sequence so a concurrent truncate can't drop
# another thread's write.
_LOG_LOCK = threading.Lock()


def _log(msg: str) -> None:
    """Append a timestamped line to the Triggevent log file and stderr.
    The frozen Windows build has no console, so this file is the only view of
    engine boot and launch errors. Never raises."""
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
            try:                               # bound the growth
                if p.exists() and p.stat().st_size > (1 << 20):
                    p.unlink()
            except OSError:
                pass
            with open(p, "a", encoding="utf-8", errors="replace") as fh:
                fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {msg}\n")
    except Exception:  # noqa: BLE001
        pass


# Sentinel pushed onto the write queue when the writer thread should stop.
_STOP = object()


def _find_java() -> str | None:
    """Absolute `java` path. Bundled JRE first, then JAVA_HOME, then PATH."""
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
    """Path to the triggevent-core jar, or None. Searches the same bases as the JRE does."""
    env = os.environ.get("NYAA_TRIGGEVENT_JAR")
    if env and Path(env).is_file():
        return Path(env)
    for base in _bundle_bases():
        cand = base / "triggevent-core" / "target" / "triggevent-core.jar"
        if cand.is_file():
            return cand
    return None


def has_java() -> bool:
    """True if a Java runtime is on PATH, or JAVA_HOME points at one."""
    return _find_java() is not None


_CORE_DIR     = _BASE / "triggevent-core"
_ET_DIR       = _CORE_DIR / "event-trigger"
_BUILD_SCRIPT = _CORE_DIR / ("build.bat" if os.name == "nt" else "build.sh")
# Records the event-trigger HEAD the jar was built from. update_engine trusts
# it over a bare behind count, which a failed build leaves pointing at code
# the jar does not contain. Written only after a successful build.
_JAR_STAMP    = _CORE_DIR / "target" / "triggevent-core.jar.built-from"


def _jar_built_from() -> "str | None":
    """The event-trigger HEAD the built jar was stamped with. None when the
    stamp is missing or unreadable, which callers treat as needs rebuild."""
    try:
        return _JAR_STAMP.read_text(encoding="ascii").strip() or None
    except OSError:
        return None


def _kill_build_tree(proc) -> None:
    """Kill a timed-out engine build and everything it spawned. A lone
    proc.kill orphans Maven and the java compilers on the shared
    event-trigger tree, which the next update click would git checkout and
    rebuild underneath."""
    try:
        if os.name == "posix":
            # Own process group, so the kill reaches the whole tree.
            import signal
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        else:
            # build.bat runs through cmd.exe and TerminateProcess hits only
            # that wrapper. taskkill /T takes the tree below it.
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True)
    except (OSError, ProcessLookupError):
        try:               # process group is gone, hit the proc directly
            proc.kill()
        except OSError:
            pass


def update_engine(channel: str = "stable", manual: bool = False) -> "tuple[bool, str]":
    """Update the Triggevent Engine on demand.

    Source builds fast-forward the event-trigger clone and rebuild the jar, needs
    git/java/mvn. Frozen builds download the prebuilt jar from the latest release
    and swap it in, but only on a MANUAL request, since the app's own auto-update
    already ships the engine, so background runs stay a no-op. Returns changed and
    a message. Blocking, so call it from a background thread.

    `channel` is accepted for caller compatibility only. The engine stream is
    pinned to stable.
    """
    if getattr(sys, "frozen", False):
        if not manual:
            return (False, "frozen build: the engine ships with the app")
        return _download_engine("stable")
    # Source build. Rebuilding from the event-trigger clone needs that clone and
    # the whole toolchain. A plain `git clone` of NyaaTriggers has neither, and
    # no jar either, so the engine simply never starts and no Triggevent
    # callouts exist. Fall back to the same prebuilt jar the packaged build
    # ships, verified against the release .sha256, one click away.
    # The POSIX build runs through bash, so probe for it too on POSIX.
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
        f = _git("fetch", "origin", "master")
        if f.returncode != 0:
            return (False, f"Triggevent fetch failed: {f.stderr.strip()[:200]}")
        r = _git("rev-list", "--count", "HEAD..origin/master")
        if r.returncode != 0:
            return (False, f"Triggevent rev-list failed: {r.stderr.strip()[:200]}")
        h = _git("rev-parse", "origin/master")
        if h.returncode != 0:
            return (False, f"Triggevent rev-parse failed: {h.stderr.strip()[:200]}")
        head = h.stdout.strip()
        behind = r.stdout.strip()
        if not behind.isdigit() or int(behind) == 0:
            # behind==0 says the clone caught up, it says nothing about the
            # jar. A failed or timed-out build leaves HEAD at origin/master
            # with the old jar in place, so behind alone would report the
            # engine current forever. Trust only the stamp a successful build
            # writes. A missing or unreadable stamp means rebuild.
            if _jar_built_from() == head:
                return (False, "Triggevent Engine already up to date")
        # The vendored patches leave the tree dirty after every build, and a
        # fast forward refuses to run over modified files, so the merge would
        # abort on the patched sources forever. Discard them, the build
        # reapplies the patches right after.
        c = _git("checkout", "--", ".")
        if c.returncode != 0:
            return (False, f"Triggevent checkout failed: {c.stderr.strip()[:200]}")
        if _git("merge", "--ff-only", "origin/master").returncode != 0:
            return (False, "Triggevent pull skipped: local event-trigger clone has diverged")
        cmd = [str(_BUILD_SCRIPT)] if os.name == "nt" else ["bash", str(_BUILD_SCRIPT)]
        # Build what was just merged, not the stale pin. Otherwise the build
        # script checks the pin back out, rebuilds the old source every time
        # and still reports an update. If upstream drifted where a patch
        # applies, the build now fails loudly instead of shipping old code.
        env = {**os.environ, "EVENT_TRIGGER_REF": "origin/master"}
        # A Maven build legitimately takes minutes. Half an hour means it hung.
        # Popen by hand, not subprocess.run. run's timeout kills only the
        # direct bash or build.bat child and orphans Maven and its java
        # compilers on the shared event-trigger tree, which the next update
        # click would git checkout and rebuild underneath. _kill_build_tree
        # takes the whole tree, a process group on POSIX, taskkill /T on
        # Windows, same as _signal_group.
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
    # Stamp only now that the build succeeded. Stamping earlier would vouch
    # for code a failed or killed build never put into the jar.
    try:
        _JAR_STAMP.write_text(head + "\n", encoding="ascii")
    except OSError:
        pass
    if behind.isdigit() and int(behind) > 0:
        return (True, f"Triggevent Engine updated ({behind} new commit(s)) and rebuilt. "
                      f"Restart NyaaTriggers to load the new triggers.")
    # behind was 0, this run only rebuilt a jar that did not match HEAD.
    return (True, "Triggevent Engine rebuilt from the current source. "
                  "Restart NyaaTriggers to load it.")


def _unlink(p) -> None:
    try:
        p.unlink()
    except OSError:
        pass


def _same_file(a, b) -> bool:
    """True if two files have identical contents. Skips a no-op engine swap."""
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
    """Download the prebuilt triggevent-core.jar published on the latest release
    for `channel` and swap it in atomically. No git or Maven needed.

    With no jar yet, a fresh source clone, it installs one where the bridge
    looks for it, so this doubles as the first-time engine install."""
    jar = _find_jar()
    if jar is None:
        jar = _BASE / "triggevent-core" / "target" / "triggevent-core.jar"
        try:
            jar.parent.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            return (False, f"Couldn't create {jar.parent}: {e}")
    try:
        import updater  # lazy import, avoids a module-load import cycle
        rel = updater.fetch_latest_release(channel=channel)
    except Exception as e:  # noqa: BLE001 - network / parse
        return (False, f"Couldn't check for an engine update: {e}")
    url = rel.assets.get("triggevent-core.jar")
    if not url:
        return (False, "The latest release has no downloadable engine yet - update the app instead")
    tmp = jar.parent / "triggevent-core.jar.new"
    # updater.download writes a per-process .part beside tmp and a hard kill
    # mid download leaks it. No sweep covers this dir, drop the aged ones
    # here, same idiom as install.py.
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
    # A jar is a zip. Reject anything that isn't one, a truncated file or an HTML error page.
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
    # The jar is executed by the JVM with the user's full privileges on next
    # launch, so hold it to the same bar as the app archives. The release must
    # publish a triggevent-core.jar.sha256 sidecar and it must match. Fails
    # closed on a missing or unreadable sidecar or a mismatch, exactly like the
    # self-updater's updater.verify_release_asset.
    ok, why = updater.verify_release_asset(rel, "triggevent-core.jar", tmp)
    if not ok:
        _unlink(tmp)
        return (False, f"Engine update rejected ({why}) - kept your current one")
    try:
        os.replace(str(tmp), str(jar))     # atomic on the same filesystem
    except OSError as e:
        _unlink(tmp)
        return (False, f"Couldn't install the new engine: {e}")
    # The swapped jar is not the one the stamp vouches for. Drop the stamp or
    # a later source build update would trust it for a jar it never recorded.
    _unlink(_JAR_STAMP)
    # A frozen build drops the jar into _internal, and the next app
    # self-update swaps that tree wholesale, restoring the bundled engine.
    # Say so now or that update looks like it silently reverted this one.
    frozen_note = (" The next NyaaTriggers update ships and restores its own "
                   "bundled engine.") if getattr(sys, "frozen", False) else ""
    if not has_java():
        # The jar is in place but nothing can run it, and the engine is only
        # probed at launch, so say both things now instead of letting the user
        # restart into the same silence.
        return (True, "Triggevent Engine installed, but no Java runtime was found. "
                      "Install one (Arch: sudo pacman -S jre-openjdk), then restart "
                      "NyaaTriggers." + frozen_note)
    return (True, "Triggevent Engine updated. Restart NyaaTriggers to load it." + frozen_note)


def has_jar() -> bool:
    """True if the built or bundled triggevent-core jar is present."""
    return _find_jar() is not None


def is_available() -> bool:
    """True when a Java runtime AND the built sidecar jar are both present."""
    return has_java() and has_jar()


def _make_bundled_jre_executable() -> None:
    """Restore exec bits on the bundled JRE. PyInstaller ships it as data, which
    can drop them from bin/ and lib/jspawnhelper, the JVM's spawn helper. POSIX only."""
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
    """Owns the triggevent-core subprocess and relays its callouts as Qt signals."""

    callout     = pyqtSignal(str, str)   # on-screen text, severity in {info, alert, alarm}
    tts         = pyqtSignal(str)        # spoken text
    status      = pyqtSignal(bool, str)  # active, message
    phrase_seen = pyqtSignal(str)        # a callout phrase observed, for the override UI
    inventory   = pyqtSignal(str)        # one-shot JSON [{id,name,fight,group,text}] of all engine callouts
    telesto     = pyqtSignal(str)        # Telesto automark connection status, "good"|"bad"|"unknown"
    ready       = pyqtSignal()           # sidecar is up and reading stdin, time to replay world state

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._proc: subprocess.Popen | None = None
        self._reader: threading.Thread | None = None
        self._errpump: threading.Thread | None = None
        self._writer: threading.Thread | None = None
        self._wq: queue.Queue = _ByteQueue(maxsize=10000)
        self._active = False
        # Find->replace rules {find, replace, regex, enabled}, applied before a
        # callout is spoken or shown. Atomic list swap so the reader thread can
        # snapshot it lock-free.
        self._replacements: list = []
        # Callout ids the user switched off, dropped in the reader thread.
        # Atomic frozenset swap, same reason.
        self._disabled: frozenset = frozenset()
        self._seen: dict = {}            # ordered set of observed callout phrases
        # _seen is written by the reader thread and snapshotted by the GUI
        # thread. A compound update, insert plus evict, needs a real lock.
        self._seen_lock = threading.Lock()
        # Makes stop and reader-exit check-and-clear of the _proc/_active pair atomic.
        self._state_lock = threading.Lock()

    @staticmethod
    def is_available() -> bool:
        return is_available()

    def is_active(self) -> bool:
        return self._active

    # ------------------------------------------------------------------
    def set_replacements(self, rules: list) -> None:
        """Set find->replace overrides, {find, replace, regex, enabled} dicts.
        An empty result suppresses the callout. Thread-safe via atomic list swap."""
        self._replacements = list(rules or [])

    def set_disabled(self, ids) -> None:
        """Set callout ids to suppress. Takes effect on the next callout, no
        sidecar restart needed. Thread-safe via atomic swap."""
        self._disabled = frozenset(ids or ())

    def set_callout(self, cid: str, tts: str | None = None,
                    text: str | None = None, enable: bool | None = None) -> None:
        """Edit a trigger's live TTS/visual/enable in the engine. Tokens still
        substitute. No-op if the sidecar is down. The caller re-sends on restart."""
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
        """Revert a Triggevent trigger's output to the engine defaults."""
        if cid:
            self._send_command({"nyaa_cmd": "reset_callout", "id": cid})

    def set_automark(self, enable: bool, uri: str | None = None) -> None:
        """Enable or disable Telesto automarking. `uri` points at the user's plugin.
        The engine resolves party slots via Telesto's GetPartyMembers and POSTs "/mk"
        itself. Boots with automarks off. No-op if down, caller re-sends on restart."""
        cmd: dict = {"nyaa_cmd": "set_automark", "enable": bool(enable)}
        if uri:
            cmd["uri"] = str(uri)
        self._send_command(cmd)

    def _send_command(self, cmd: dict) -> None:
        """Queue a control command, one JSON line, for the sidecar's stdin. On a
        full queue the oldest queued line is dropped and logged and the command
        retried, so a set_callout / set_automark is never silently lost the way a
        bare pass would lose it."""
        if not self._active:
            return
        try:
            line = json.dumps(cmd)  # dumps escapes control chars, always one line
        except (TypeError, ValueError):
            return
        try:
            self._wq.put_nowait(line)
        except queue.Full:
            log_drop("engine-cmd", "sidecar stdin queue full; dropped oldest control command")
            try:                       # drop one old line, retry once
                self._wq.get_nowait()
                self._wq.put_nowait(line)
            except (queue.Empty, queue.Full):
                pass

    def seen_phrases(self) -> list:
        """Callout phrases observed so far this session, for the override picker."""
        with self._seen_lock:
            return list(self._seen.keys())

    def _apply_replacements(self, s: str) -> str:
        rules = self._replacements          # atomic snapshot of the list
        if not rules or not s:
            return s.strip()   # same whitespace handling as the rules path
        out = s
        for r in rules:
            if not r.get("enabled", True):
                continue
            # A hand edited settings entry can hold values that are not
            # strings. Coerce so one bad rule cannot raise in the dispatch
            # and mute every callout while it is installed.
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
                continue                    # skip a busted regex rule
            # Bounded engine with a match timeout. A catastrophic user pattern
            # must not wedge the reader thread, and a bad backreference leaves
            # the text unchanged rather than dropping the callout.
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

    # ------------------------------------------------------------------
    def start(self) -> None:
        """Spawn the sidecar. Idempotent. Guard with is_available first."""
        if self._active:
            return
        _log(f"start() requested (os={os.name})")
        java = _find_java()
        jar = _find_jar()
        if java is None or jar is None:
            _log(f"cannot start: java={java!r} jar={jar!r}")
            self.status.emit(False, "Java runtime or triggevent-core.jar not found")
            return

        # PyInstaller data can lose exec bits. Restore them so the JVM can start.
        if os.name == "posix" and _bundled_jre_dir() is not None:
            _make_bundled_jre_executable()

        # The engine builds Swing overlays at boot, so it needs a display, not
        # headless. Prefer a throwaway Xvfb so those overlays stay invisible.
        # Without xvfb-run, fall back to the session display, overlays may show.
        # Cap the heap too. Default ergonomics scale max heap to a quarter of
        # system RAM and G1 commits it eagerly, so a small log parser sat at
        # hundreds of MB for nothing. 256m proved too tight on long DMU
        # sessions: GC pressure stalls the event pump, the sequential
        # triggers time out mid chain, and callouts silently drop. 512m
        # keeps the cap well under default ergonomics while leaving the
        # chain headroom.
        cmd = [java, "-Xmx512m", "-jar", str(jar)]
        xvfb = shutil.which("xvfb-run")
        if xvfb:
            # 24-bit depth, AWT/Swing graphics init is unreliable on 8-bit visuals.
            cmd = [xvfb, "-a", "-s", "-screen 0 1024x768x24"] + cmd

        # Own process group so stop can signal xvfb-run AND its JVM child.
        # terminate on the xvfb-run parent alone doesn't reliably reach the JVM.
        popen_kwargs = dict(
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            bufsize=1, text=True, encoding="utf-8", errors="replace",
            cwd=str(jar.parent.parent),  # the triggevent-core/ dir
        )
        if os.name == "posix":
            popen_kwargs["start_new_session"] = True
            # No pdeathsig preexec. The sidecar already exits on stdin EOF when
            # this process dies hard, the finally in TriggeventCore.java's stdin
            # pump, and a signal would reach only the xvfb-run wrapper, killing
            # it before its own Xvfb cleanup runs and leaking Xvfb.
        if os.name == "nt":
            # java.exe is a console binary. Without this it pops a black console
            # window from the GUI app.
            popen_kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
        # Give xvfb-run's /bin/sh and the JVM the system libraries, not the frozen
        # app's bundled ones, else /bin/sh dies on a libreadline symbol.
        popen_kwargs["env"] = proc_env.child_env()

        try:
            proc = subprocess.Popen(cmd, **popen_kwargs)
        except OSError as e:
            _log(f"launch failed: {e!r}")
            self.status.emit(False, f"Failed to launch sidecar: {e}")
            self._proc = None
            return
        _log(f"sidecar spawned pid={proc.pid}")

        wq = _ByteQueue(maxsize=10000)
        with self._state_lock:
            self._proc = proc
            self._wq = wq
            self._active = True
        # Bind workers to this generation's proc and queue via args so a quick
        # stop->start can't leave a stale thread on the new queue/proc. The
        # seq gap high-water mark rides along for the same reason: a shared
        # self._last_callout_seq survived a spontaneous sidecar exit plus the
        # reconcile restart, since stop() early-returns once the reader-exit
        # path cleared the state, and a late write from the old generation's
        # reader could land after any reset. The jar numbers callouts from 1
        # each generation, so the mark must die with its generation too or
        # real gaps below the old mark go unreported.
        seq_state: dict = {"last": None}
        self._reader = threading.Thread(target=self._read_loop, args=(proc, wq, seq_state),
                                        daemon=True, name="triggevent-reader")
        self._errpump = threading.Thread(target=self._err_loop, args=(proc,),
                                         daemon=True, name="triggevent-stderr")
        self._writer = threading.Thread(target=self._write_loop, args=(proc, wq),
                                        daemon=True, name="triggevent-writer")
        self._reader.start()
        self._errpump.start()
        self._writer.start()
        self.status.emit(True, "Starting Triggevent Engine...")

    def stop(self, wait: bool = False) -> None:
        if not self._active and self._proc is None:
            return
        with self._state_lock:
            self._active = False
            proc, self._proc = self._proc, None
            wq = self._wq   # capture this generation's queue under the lock
        # Forget observed phrases with the session. A phrase seen by a previous
        # sidecar generation must re-emit phrase_seen on the next one.
        with self._seen_lock:
            self._seen.clear()
        # A full queue must not swallow the sentinel or the writer can stay
        # parked in wq.get. Drop one old line and retry, same as the readers.
        try:
            wq.put_nowait(_STOP)
        except queue.Full:
            try:
                wq.get_nowait()
                wq.put_nowait(_STOP)
            except (queue.Empty, queue.Full):
                pass
        if proc is not None:
            # Term the whole group SYNCHRONOUSLY first. closeEvent and the update
            # re-exec exit this process moments after stop returns, which would
            # kill an off-thread reaper before it ever signalled, orphaning the
            # JVM and its xvfb-run. The kernel delivers this regardless of our own
            # exit, so the sidecar dies even if the escalation never gets to run.
            self._signal_group(proc, graceful=True)
            if wait:
                # Block until the JVM is gone. The Windows self-update needs the
                # sidecar's locks on the JRE/jar in _internal released before it
                # swaps that folder.
                self._reap(proc)
            else:
                # Escalate off-thread. JVM teardown can take a moment and stop
                # runs on the GUI thread via closeEvent, which must never block.
                threading.Thread(target=self._reap, args=(proc,), daemon=True,
                                 name="triggevent-reap").start()
        self.status.emit(False, "Off")

    @staticmethod
    def _signal_group(proc: subprocess.Popen, graceful: bool) -> None:
        """SIGTERM when graceful, else SIGKILL the sidecar's whole process group,
        xvfb-run AND its JVM child. Falls back to the direct child if the
        group is already gone. On Windows, terminate or kill the process."""
        try:
            if os.name == "posix":
                import signal
                os.killpg(os.getpgid(proc.pid),
                          signal.SIGTERM if graceful else signal.SIGKILL)
            else:
                proc.terminate() if graceful else proc.kill()
        except (OSError, ProcessLookupError):
            try:                       # process group is gone, hit the proc directly
                proc.terminate() if graceful else proc.kill()
            except OSError:
                pass

    @classmethod
    def _reap(cls, proc: subprocess.Popen) -> None:
        """Wait for the already-SIGTERM'd sidecar to exit. SIGKILL its group if
        it doesn't. The initial term is sent synchronously by stop so it lands
        even when closeEvent / re-exec tears this process down right after."""
        try:
            proc.wait(timeout=4)
        except subprocess.TimeoutExpired:
            cls._signal_group(proc, graceful=False)
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass

    # ------------------------------------------------------------------
    def feed(self, raw_msg: str) -> None:
        """Tee one raw IINACT WS message to the sidecar.

        Connected to WSClient.raw_message. Runs on the GUI thread, so it never
        blocks. The writer thread drains the queue. On overflow the oldest
        messages get dropped."""
        if not self._active or not raw_msg:
            return
        # Protocol is one JSON object per line.
        line = raw_msg.replace("\r", " ").replace("\n", " ")
        try:
            self._wq.put_nowait(line)
        except queue.Full:
            log_drop("engine-feed", "sidecar stdin queue full; dropped oldest log line")
            try:                       # drop one old line, retry once
                self._wq.get_nowait()
                self._wq.put_nowait(line)
            except (queue.Empty, queue.Full):
                pass

    # ------------------------------------------------------------------
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

    def _read_loop(self, proc: subprocess.Popen, wq: queue.Queue, seq_state: dict) -> None:
        if proc.stdout is None:
            return
        for line in _read_lines_bounded(proc.stdout):
            line = line.strip()
            if not line:
                continue
            if not line.startswith("{"):
                # a non-JSON diagnostic line from the sidecar.
                _log(f"[sidecar] {line}")
                continue
            try:
                msg = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                # Was a silent continue: a corrupted callout line used to vanish
                # with no trace, which is exactly the loss we are hunting.
                log_drop("engine-parse", f"unparsed sidecar line {line[:160]!r}", 0)
                continue
            try:
                self._dispatch(msg, seq_state)
            except Exception as exc:  # noqa: BLE001 - a bad message must never kill the reader
                _log(f"dispatch error: {exc!r}")
        # stdout closed means the process ended. Only touch shared state if we're
        # still the current generation, a restart may have swapped procs. Check
        # and clear under the lock, or a concurrent stop+start between our check
        # and our writes would get its NEW generation's _proc/_active clobbered.
        with self._state_lock:
            was_current = proc is self._proc and self._active
            if was_current:
                self._active = False
                self._proc = None
        if was_current:
            _log(f"sidecar exited (returncode={proc.poll()})")
            self.status.emit(False, "Sidecar exited")
        # Always release this generation's writer thread and reap the child.
        # A spontaneous sidecar exit otherwise leaks the writer, blocked in
        # wq.get forever, pinning the Popen and its stdin pipe FD, and leaves
        # a zombie. A later stop early-returns once _active/_proc are cleared,
        # so nothing else can ever clean it up.
        try:
            wq.put_nowait(_STOP)
        except queue.Full:
            try:
                wq.get_nowait()
                wq.put_nowait(_STOP)
            except (queue.Empty, queue.Full):
                pass
        self._reap(proc)

    def _err_loop(self, proc: subprocess.Popen) -> None:
        if proc.stderr is None:
            return
        for line in _read_lines_bounded(proc.stderr):
            line = line.rstrip()
            if not line:
                continue
            _log(f"[sidecar stderr] {line}")
            # Printed once the sidecar starts reading stdin. That's when a replayed
            # zone/party actually lands, so tell the app to send the world state.
            if "reading WS messages on stdin" in line:
                self.ready.emit()

    def _dispatch(self, msg: dict, seq_state: "dict | None" = None) -> None:
        kind = msg.get("t")
        if kind == "callout":
            # Gap-check the engine's callout sequence before any gate, so a
            # callout lost between sidecar stdout and this dispatch shows up as
            # a seq jump instead of a silently missing callout. The high-water
            # mark lives in this generation's seq_state, bound via args like
            # proc and wq, so a late write from a previous generation's reader
            # cannot poison the new one's mark.
            if seq_state is None:
                seq_state = {}
            seq = msg.get("seq")
            if isinstance(seq, int):
                last = seq_state.get("last")
                seq_state["last"] = seq
                if last is not None and seq > last + 1:
                    log_drop("engine-seq",
                             f"callout seq gap {last} -> {seq}, "
                             f"{seq - last - 1} lost between engine and app", 0)
            # Drop callouts whose dispatch lands after stop flipped _active.
            # Stdout already buffered in the kernel pipe when stop ran would
            # otherwise fire callout/phrase_seen into a torn-down UI. Triggernometry
            # gates the same way in triggernometry_bridge._dispatch.
            if not self._active:
                return
            cid = msg.get("id")
            if cid and cid in self._disabled:
                return
            text = (msg.get("text") or "").strip()
            tts = (msg.get("tts") or "").strip()
            sev = msg.get("severity", "info")
            if sev not in ("info", "alert", "alarm"):
                sev = "info"
            # Record originals for the override UI, then apply the user's
            # overrides. An empty result means suppressed.
            for phrase in (tts, text):
                self._record_seen(phrase)
            had_engine_text = bool(text)
            text = self._apply_replacements(text)
            tts = self._apply_replacements(tts)
            if not text and tts and not had_engine_text:
                # TTS-only callout, most engine triggers ship no on-screen
                # text. Show the spoken text on the overlay too, or the plugin
                # can never display engine callouts. When the engine did send
                # text and a rule emptied it, that means suppressed. Do not
                # resurrect the spoken line onto the overlay.
                text = tts
            if text:
                self.callout.emit(text, sev)
            if tts:
                self.tts.emit(tts)
        elif kind == "status":
            # A status frame that lands after stop flipped _active is stale,
            # a late boot ready would flip the indicator back on for a dying
            # engine. stop already said Off.
            if not self._active:
                return
            active = bool(msg.get("active", self._active))
            self.status.emit(active, str(msg.get("message", "")))
        elif kind == "inventory":
            triggers = msg.get("triggers")
            if isinstance(triggers, list):
                self.inventory.emit(json.dumps(triggers))
        elif kind == "telesto":
            # A late frame after stop flipped _active is stale, it would flip
            # the Telesto indicator for a dying engine. Same gate as status
            # above. Inventory stays ungated on purpose, the harvest is
            # useful either way, matching triggernometry_bridge.
            if not self._active:
                return
            st = str(msg.get("status", "unknown")).lower()
            if st not in ("good", "bad", "unknown"):
                st = "unknown"
            self.telesto.emit(st)
        # unknown kinds get ignored
