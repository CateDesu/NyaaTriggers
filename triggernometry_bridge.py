#!/usr/bin/env python3
"""Bridge to the headless Triggernometry engine sidecar, `triggernometry-core`.

Runs the real Triggernometry engine from paissaheavyindustries/Triggernometry,
MIT licensed, as a Mono/.NET subprocess. Each FFXIV log line from the IINACT
feed is teed to the sidecar's stdin, one JSON per line. It runs every trigger,
runtime-compiled C# ExecuteScript included, and writes callouts to stdout as
JSON lines. Combatant snapshots are teed too, so the engine's BridgeFFXIV
resolves ${_me}/position/HP/party with no real FFXIV_ACT_Plugin.

Mirrors the TriggeventBridge interface, callout/tts/status signals plus
start/feed_log/feed_combatants/stop, so the main_window wiring stays the same.

Mono is optional, so check is_available first.

Wire protocol, NyaaTriggers -> sidecar stdin, one JSON object per line.
    {"t":"log","line":"21|..."}                         raw pipe-delimited log line
    {"t":"zone","id":<n>,"name":"<zone>"}               zone change, also derived from 01| lines
    {"t":"combatants","me":<id>,"list":[{...}, ...]}    combatant snapshot
Wire protocol, sidecar -> NyaaTriggers stdout.
    {"t":"callout","tts":"..."} | {"t":"sound","file":..,"volume":..} | {"t":"status","active":bool,"msg":".."}
"""

from __future__ import annotations

import json
import os
import queue
import re
import shlex
import shutil
import subprocess
import sys
import threading
from pathlib import Path

from PyQt6.QtCore import QObject, pyqtSignal

import proc_env
from drop_log import log_drop
from trigger_engine import _safe_sub, compile_user_regex

_STOP = object()

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
    _MAX_LINE chars, drained to its newline without holding it whole."""
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
    """Candidate dirs for the bundled sidecar and Mono, priority order, deduped.
    Mirrors triggevent_bridge._bundle_bases."""
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


def _find_exe() -> "Path | None":
    """Path to triggernometry-core.exe, or None. The exe must sit next to the
    engine DLLs, TriggernometryPlugin.dll plus deps. That dir becomes cwd."""
    env = os.environ.get("NYAA_TRIGGERNOMETRY_EXE")
    if env and Path(env).is_file():
        return Path(env)
    for base in _bundle_bases():
        for cand in (base / "triggernometry-core" / "bin" / "triggernometry-core.exe",
                     base / "triggernometry-core" / "triggernometry-core.exe"):
            if cand.is_file():
                return cand
    return None


def _find_mono() -> "str | None":
    """Mono launcher path. Bundled first, then PATH. None on Windows, where
    native .NET Framework runs the exe directly."""
    if os.name == "nt":
        return None
    for base in _bundle_bases():
        cand = base / "mono" / "bin" / "mono"
        if cand.is_file():
            return str(cand)
    return shutil.which("mono")


def _make_bundled_mono_executable() -> None:
    """Restore exec bits on a bundled Mono. PyInstaller ships it as data, which
    can drop them from bin/. POSIX only, no-op for a system mono."""
    for base in _bundle_bases():
        mono_dir = base / "mono"
        if (mono_dir / "bin" / "mono").is_file():
            for p in (mono_dir / "bin").glob("*"):
                try:
                    if p.is_file():
                        os.chmod(p, os.stat(p).st_mode | 0o111)
                except OSError:
                    pass
            return


def _rundata_dir() -> Path:
    """Writable per-user dir for the sidecar's config, Triggernometry.config.xml,
    and sound cache. Never the possibly read-only install dir."""
    if os.name == "nt":
        # `or`, not a get default. An APPDATA set-but-empty would yield
        # an empty Path, which means the current directory.
        root = Path(os.environ.get("APPDATA") or Path.home())
    else:
        root = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
    d = root / "nyaatriggers" / "triggernometry-core"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _packs_dir() -> Path:
    """Dir holding the user's TriggernometryExport .xml packs, created if absent."""
    env = os.environ.get("NYAA_TRIGGERNOMETRY_PACKS")
    d = Path(env) if env else (_rundata_dir().parent / "triggernometry-packs")
    d.mkdir(parents=True, exist_ok=True)
    return d


def _log_path() -> "Path | None":
    """Path to the Triggernometry diagnostic log, or None if its dir can't be made."""
    try:
        return _rundata_dir() / "triggernometry.log"
    except OSError:
        return None


# _log is called from the GUI, reader and stderr-pump threads. Serialize the
# size-check/unlink/append so a concurrent truncate can't drop another
# thread's write.
_LOG_LOCK = threading.Lock()


def _log(msg: str) -> None:
    """Append a timestamped line to the Triggernometry log file and stderr.
    The frozen Windows build has no console, so this file is the only view of
    engine boot/launch errors. Never raises."""
    try:
        print(f"[triggernometry] {msg}", file=sys.stderr)
    except Exception:  # noqa: BLE001
        pass
    p = _log_path()
    if p is None:
        return
    try:
        import time
        with _LOG_LOCK:
            try:                               # bound growth
                if p.exists() and p.stat().st_size > (1 << 20):
                    p.unlink()
            except OSError:
                pass
            with open(p, "a", encoding="utf-8", errors="replace") as fh:
                fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {msg}\n")
    except Exception:  # noqa: BLE001
        pass


def _find_packs() -> "list[str]":
    """All TriggernometryExport .xml packs to load, sorted for stable ordering."""
    try:
        # Lowercased suffix so an imported Pack.XML still loads on Linux.
        return sorted(str(p) for p in _packs_dir().glob("*")
                      if p.is_file() and p.suffix.lower() == ".xml")
    except OSError:
        return []


def packs_dir() -> Path:
    """The managed dir the Import button stages packs into and the sidecar loads from."""
    return _packs_dir()


def has_packs() -> bool:
    """True if at least one Triggernometry pack has been imported."""
    return bool(_find_packs())


def has_mono() -> bool:
    """True if a Mono runtime is available. Always True on Windows, not needed there."""
    return os.name == "nt" or _find_mono() is not None


def has_exe() -> bool:
    return _find_exe() is not None


def is_available() -> bool:
    """True if the sidecar exe AND its runtime, Mono on POSIX, are both present."""
    return has_exe() and has_mono()


class TriggernometryBridge(QObject):
    """Manages the triggernometry-core subprocess and relays callouts as Qt signals."""

    callout   = pyqtSignal(str, str)   # on-screen text, severity in {info, alert, alarm}
    tts       = pyqtSignal(str)        # spoken text
    sound     = pyqtSignal(str, int)   # sound file path, volume 0-100
    status    = pyqtSignal(bool, str)  # active, message
    inventory = pyqtSignal(str)        # one-shot JSON [{id,name,fight,text}] of editable UseTTS callouts

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._proc: "subprocess.Popen | None" = None
        self._reader: "threading.Thread | None" = None
        self._errpump: "threading.Thread | None" = None
        self._writer: "threading.Thread | None" = None
        self._wq: queue.Queue = _ByteQueue(maxsize=20000)
        self._active = False
        self._replacements: list = []          # user find->replace callout overrides
        self._disabled: frozenset = frozenset()  # callout ids the user switched OFF
        # Makes the stop and reader-exit check-and-clear of _proc and _active atomic.
        self._state_lock = threading.Lock()

    @staticmethod
    def is_available() -> bool:
        return is_available()

    def is_active(self) -> bool:
        return self._active

    def set_replacements(self, rules: list) -> None:
        """Set callout find->replace overrides, applied before speaking/showing."""
        self._replacements = list(rules or [])

    def set_disabled(self, ids) -> None:
        """Set callout ids to suppress. The sidecar blanks those triggers' spoken
        text in place. Takes effect on the next firing, no restart."""
        # Settings are hand-editable and sorted() on a mixed-type set raises
        # TypeError in the GUI slots, so coerce every id to str at intake.
        self._disabled = frozenset(str(x) for x in (ids or ()))
        self._send_command({"t": "set_disabled", "ids": sorted(self._disabled)})

    def set_callout(self, cid: str, tts: "str | None" = None,
                    text: "str | None" = None, enable: "bool | None" = None) -> None:
        """Edit one callout's spoken text live. Rewrites the trigger's UseTTS
        template, so ${...} still substitutes. Signature mirrors TriggeventBridge.
        Only spoken text applies here, and None reverts to the default.
        The host has no enable concept, so a bare enable call maps to
        set_disabled, False disables the id and True re-enables it."""
        if not cid:
            return
        if tts is None and text is None and enable is not None:
            # Sent on as a set_callout with text null, the host would read
            # this as revert to default and wipe the user's custom spoken
            # text. set_disabled is the real enable switch here.
            ids = set(self._disabled)
            if enable:
                ids.discard(str(cid))
            else:
                ids.add(str(cid))
            self.set_disabled(ids)
            return
        val = tts if tts is not None else text
        self._send_command({"t": "set_callout", "id": cid, "text": val})

    def reset_callout(self, cid: str) -> None:
        """Revert one callout to its engine-default text."""
        if cid:
            self._send_command({"t": "set_callout", "id": cid, "text": None})

    def _send_command(self, cmd: dict) -> None:
        """Queue a control command for the sidecar's stdin. Dropped if down."""
        self._enqueue(cmd)

    # ------------------------------------------------------------------
    @staticmethod
    def _launch_cmd(exe: Path) -> "list[str]":
        """Build the process command. POSIX runs xvfb-run -a mono exe, the engine
        builds a real WinForms UI at boot, so it needs a display, not headless.
        Windows runs the exe natively."""
        cfg = str(_rundata_dir())
        argv = [str(exe), cfg, "--serve", "--"] + _find_packs()
        if os.name == "nt":
            return argv
        mono = _find_mono()
        cmd = [mono] + argv if mono else argv
        xvfb = shutil.which("xvfb-run")
        if xvfb:
            cmd = [xvfb, "-a", "-s", "-screen 0 1024x768x24"] + cmd
        return cmd

    def start(self) -> None:
        """Spawn the sidecar. Idempotent. Guard with is_available first."""
        if self._active:
            return
        _log(f"start() requested (os={os.name})")
        exe = _find_exe()
        if exe is None or not has_mono():
            _log(f"cannot start: exe={exe!r} mono={_find_mono()!r} has_mono={has_mono()} "
                 f"packs={_find_packs()!r}")
            self.status.emit(False, "Mono runtime or triggernometry-core.exe not found")
            return

        # PyInstaller data can lose exec bits. Restore them.
        if os.name == "posix":
            _make_bundled_mono_executable()

        try:
            cmd = self._launch_cmd(exe)
        except OSError as e:
            _log(f"launch failed: {e!r}")
            self.status.emit(False, f"Failed to launch sidecar: {e}")
            self._proc = None
            return
        _log("launch: " + shlex.join(str(c) for c in cmd) + f"  (cwd={exe.parent})")
        popen_kwargs = dict(
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            bufsize=1, text=True, encoding="utf-8", errors="replace",
            cwd=str(exe.parent),  # the engine DLLs live next to the exe
        )
        if os.name == "posix":
            popen_kwargs["start_new_session"] = True   # own process group for stop
            # No pdeathsig preexec. The sidecar already exits on stdin EOF when
            # this process dies hard, RunServer's reader finally, host/Program.cs,
            # and a signal would reach only the xvfb-run wrapper, killing it
            # before its own Xvfb cleanup runs and leaking Xvfb.
        if os.name == "nt":
            popen_kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
        # Give Mono and any /bin/sh it spawns the system libraries, not the frozen
        # app's bundled ones, else a system shell dies on a libreadline symbol.
        popen_kwargs["env"] = proc_env.child_env()

        try:
            proc = subprocess.Popen(cmd, **popen_kwargs)
        except OSError as e:
            _log(f"launch failed: {e!r}")
            self.status.emit(False, f"Failed to launch sidecar: {e}")
            self._proc = None
            return
        _log(f"sidecar spawned pid={proc.pid}")

        wq: queue.Queue = _ByteQueue(maxsize=20000)
        with self._state_lock:
            self._proc = proc
            self._wq = wq
            self._active = True
        self._reader = threading.Thread(target=self._read_loop, args=(proc, wq), daemon=True, name="tn-reader")
        self._errpump = threading.Thread(target=self._err_loop, args=(proc,), daemon=True, name="tn-stderr")
        self._writer = threading.Thread(target=self._write_loop, args=(proc, wq), daemon=True, name="tn-writer")
        self._reader.start()
        self._errpump.start()
        self._writer.start()
        # Replay the cached disabled set into this generation. set_disabled
        # calls made while the sidecar was down, including the one at bridge
        # creation before the first start, were dropped by _enqueue, and the
        # callout messages carry no id, so unlike TriggeventBridge, _dispatch
        # cannot re-check locally. The sidecar's blanking is the only gate.
        self._send_command({"t": "set_disabled", "ids": sorted(self._disabled)})
        self.status.emit(True, "Starting Triggernometry engine...")

    def stop(self, wait: bool = False) -> None:
        if not self._active and self._proc is None:
            return
        with self._state_lock:
            self._active = False
            proc, self._proc = self._proc, None
            wq = self._wq   # capture this generation's queue under the lock
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
            # Term the group SYNCHRONOUSLY first. closeEvent / re-exec exits this
            # process right after, killing an off-thread reaper before it can
            # signal and orphaning the sidecar. The kernel delivers this
            # regardless of our own exit.
            self._signal_group(proc, graceful=True)
            if wait:
                self._reap(proc)
            else:
                threading.Thread(target=self._reap, args=(proc,), daemon=True, name="tn-reap").start()
        self.status.emit(False, "Off")

    @staticmethod
    def _signal_group(proc: subprocess.Popen, graceful: bool) -> None:
        """SIGTERM, graceful, or SIGKILL the sidecar's whole process group,
        falling back to the direct child if the group is already gone."""
        try:
            if os.name == "posix":
                import signal
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM if graceful else signal.SIGKILL)
            else:
                proc.terminate() if graceful else proc.kill()
        except (OSError, ProcessLookupError):
            try:
                proc.terminate() if graceful else proc.kill()
            except OSError:
                pass

    @classmethod
    def _reap(cls, proc: subprocess.Popen) -> None:
        """Wait for the already-SIGTERM'd sidecar to exit. SIGKILL its group if
        it doesn't. The initial term is sent synchronously by stop."""
        try:
            proc.wait(timeout=4)
        except subprocess.TimeoutExpired:
            cls._signal_group(proc, graceful=False)
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass

    # ------------------------------------------------------------------
    def _enqueue(self, obj: dict) -> None:
        """Queue one JSON message, non-blocking, onto the sidecar's stdin."""
        if not self._active:
            return
        try:
            # dumps escapes control chars in strings, always one line.
            line = json.dumps(obj, ensure_ascii=False)
        except (TypeError, ValueError):
            return
        try:
            self._wq.put_nowait(line)
        except queue.Full:
            log_drop("engine-feed", "trig sidecar stdin queue full; dropped oldest feed message")
            try:                       # drop one old, retry once
                self._wq.get_nowait()
                self._wq.put_nowait(line)
            except (queue.Empty, queue.Full):
                pass

    def feed_log(self, line: str) -> None:
        """Tee one pipe-delimited FFXIV log line, ws_client.log_line. Runs on
        the GUI thread and must never block."""
        if not self._active or not line:
            return
        # The sidecar derives zone from 01| lines itself, just forward raw.
        self._enqueue({"t": "log", "line": line})

    def feed_combatants(self, payload: dict) -> None:
        """Tee a combatant snapshot for ${_me}/position/HP/party scripts. Payload
        looks like {"me": <playerId>, "list": [{id,name,job,hp,maxhp,x,y,z,h,party,...}, ...]}."""
        if not self._active or not isinstance(payload, dict):
            return
        self._enqueue({"t": "combatants", "me": payload.get("me", 0), "list": payload.get("list", [])})

    def feed_zone(self, zone_id: int, zone_name: str) -> None:
        """Explicitly push a zone change, also derived from 01| log lines."""
        if not self._active:
            return
        self._enqueue({"t": "zone", "id": int(zone_id or 0), "name": zone_name or ""})

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

    def _read_loop(self, proc: subprocess.Popen, wq: queue.Queue) -> None:
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
            except (json.JSONDecodeError, ValueError):
                log_drop("engine-parse", f"unparsed trig sidecar line {line[:160]!r}", 0)
                continue
            try:
                self._dispatch(msg)
            except Exception as exc:  # noqa: BLE001 - a bad message must never kill the reader
                _log(f"dispatch error: {exc!r}")
        # Check-and-clear under the lock, or a concurrent stop+start between
        # our check and our writes would get its NEW generation's state clobbered.
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
            if line:
                _log(f"[sidecar stderr] {line}")

    def _apply_replacements(self, s: str) -> str:
        rules = self._replacements
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
                continue
            # Bounded engine with a match timeout. A catastrophic user pattern
            # must not wedge the reader thread, and a bad backreference leaves
            # the text unchanged rather than dropping the callout.
            out = _safe_sub(rx, repl, out)
        return out.strip()

    def _dispatch(self, msg: dict) -> None:
        kind = msg.get("t")
        # Teardown race. stop flips _active while the reader thread still has
        # queued lines. Output already in flight must not fire after the engine
        # was switched off, and a late boot status would flip the indicator back
        # on for a dying engine. Inventory frames stay useful either way.
        if kind in ("callout", "sound", "status") and not self._active:
            return
        if kind == "callout":
            tts = self._apply_replacements((msg.get("tts") or "").strip())
            text = self._apply_replacements((msg.get("text") or msg.get("tts") or "").strip())
            sev = msg.get("severity", "info")
            if sev not in ("info", "alert", "alarm"):
                sev = "info"
            if text:
                self.callout.emit(text, sev)
            if tts:
                self.tts.emit(tts)
        elif kind == "sound":
            f = msg.get("file") or ""
            if f:
                # The engine only plays absolute paths. Pack sounds arrive
                # relative, anchor them at the directory the packs load from.
                if not os.path.isabs(f):
                    f = str(_packs_dir() / f)
                try:
                    vol = int(msg.get("volume", 100))
                except (TypeError, ValueError):
                    vol = 100
                self.sound.emit(f, max(0, min(100, vol)))
        elif kind == "status":
            self.status.emit(bool(msg.get("active", self._active)), str(msg.get("msg", "")))
        elif kind == "inventory":
            triggers = msg.get("triggers")
            if isinstance(triggers, list):
                self.inventory.emit(json.dumps(triggers))
        # unknown kinds ignored
