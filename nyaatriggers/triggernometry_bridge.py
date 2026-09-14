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
import time
from pathlib import Path

from nyaatriggers.paths import source_root

from PyQt6.QtCore import QObject, pyqtSignal

from nyaatriggers import proc_env
from nyaatriggers.drop_log import log_drop, open_private_log, rotate_one_generation
from nyaatriggers.trigger_engine import _safe_sub, compile_user_regex
from nyaatriggers.triggernometry_telesto import TriggernometryTelesto
from nyaatriggers.telesto_client import DEFAULT_URI as DEFAULT_TELESTO_URI

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
    string memory passes the budget, so the drop oldest policy at the call
    sites covers byte pressure unchanged. The _STOP sentinel is not a str
    and always fits the byte budget. The item count cap still applies.
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
            try:                               # bound growth, keep one old generation
                if p.exists() and p.stat().st_size > (1 << 20):
                    rotate_one_generation(p)
            except OSError:
                pass
            with open_private_log(p) as fh:
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

    # The trailing int on the UI bound signals is the emitter's sidecar
    # generation. It rides the payload so the slot can re-check it against
    # the bridge's live generation, Qt queued delivery can land a pre
    # restart signal at the slot after the restart.
    callout   = pyqtSignal(str, str, int)   # on-screen text, severity in {info, alert, alarm}, generation
    tts       = pyqtSignal(str, int)        # spoken text, generation
    sound     = pyqtSignal(str, int, int)   # sound file path, volume 0-100, generation
    status    = pyqtSignal(bool, str, int)  # active, message, generation
    inventory = pyqtSignal(str)        # one-shot JSON [{id,name,fight,text}] of editable UseTTS callouts

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._proc: "subprocess.Popen | None" = None
        self._reader: "threading.Thread | None" = None
        self._errpump: "threading.Thread | None" = None
        self._writer: "threading.Thread | None" = None
        self._telesto = None
        self._retired_telesto = []
        self._telesto_uri = DEFAULT_TELESTO_URI
        self._telesto_commands = False
        self._wq: queue.Queue = _ByteQueue(maxsize=20000)
        self._active = False
        # The live sidecar generation, bumped by every start and stop. The
        # reader thread carries its own in its args like proc and wq, and
        # every UI bound emit is stamped with it. _active alone cannot gate
        # dispatch, a restart flips it back on while a previous generation's
        # reader is still draining its buffered output.
        self._gen = 0
        self._replacements: list = []          # user find->replace callout overrides
        self._disabled: frozenset = frozenset()  # callout ids the user switched OFF
        # Makes the stop and reader-exit check-and-clear of _proc and _active atomic.
        self._state_lock = threading.Lock()

    @staticmethod
    def is_available() -> bool:
        return is_available()

    def is_active(self) -> bool:
        return self._active

    def generation(self) -> int:
        """The live sidecar generation. Every UI bound emit carries the
        emitter's generation and the slots compare it against this."""
        return self._gen

    def _gen_live(self, gen: "int | None") -> bool:
        """True when gen names the live generation and the engine is on.
        A previous generation's reader fails this once stop or a restart
        swapped the state out from under it."""
        return gen is not None and gen == self._gen and self._active

    def set_replacements(self, rules: list) -> None:
        """Set callout find->replace overrides, applied before speaking/showing."""
        self._replacements = list(rules or [])

    def configure_telesto(self, uri=DEFAULT_TELESTO_URI, commands_enabled=False) -> bool:
        uri = uri if isinstance(uri, str) and uri else DEFAULT_TELESTO_URI
        changed = uri != self._telesto_uri
        self._telesto_uri = uri
        self._telesto_commands = bool(commands_enabled)
        if self._telesto:
            self._telesto.configure(self._telesto_commands)
        return changed

    def _feed_endpoint(self, body: str, gen: int) -> None:
        with self._state_lock:
            if self._gen_live(gen):
                self._enqueue({"t": "endpoint", "body": body})

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
            self.status.emit(False, "Mono runtime or triggernometry-core.exe not found", self._gen)
            return

        # PyInstaller data can lose exec bits. Restore them.
        if os.name == "posix":
            _make_bundled_mono_executable()

        try:
            cmd = self._launch_cmd(exe)
        except OSError as e:
            _log(f"launch failed: {e!r}")
            self.status.emit(False, f"Failed to launch sidecar: {e}", self._gen)
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

        gen = self._gen + 1
        relay = TriggernometryTelesto(
            lambda body: self._feed_endpoint(body, gen), _log,
            self._telesto_uri, self._telesto_commands)
        try:
            relay.start()
        except OSError as exc:
            _log(f"Telesto callback listener failed: {exc}")
            self.status.emit(False, f"Telesto callback listener failed: {exc}", self._gen)
            return
        popen_kwargs["env"]["NYAA_TRIGGERNOMETRY_TELESTO_RELAY"] = relay.url
        popen_kwargs["env"]["NYAA_TRIGGERNOMETRY_CALLBACK_URI"] = relay.callback_url

        try:
            proc = subprocess.Popen(cmd, **popen_kwargs)
        except OSError as e:
            relay.close()
            _log(f"launch failed: {e!r}")
            self.status.emit(False, f"Failed to launch sidecar: {e}", self._gen)
            self._proc = None
            return
        _log(f"sidecar spawned pid={proc.pid}")

        wq: queue.Queue = _ByteQueue(maxsize=20000)
        with self._state_lock:
            self._proc = proc
            self._telesto = relay
            self._wq = wq
            self._active = True
            self._gen += 1
            gen = self._gen
        self._reader = threading.Thread(target=self._read_loop, args=(proc, wq, gen), daemon=True, name="tn-reader")
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
        self.status.emit(True, "Starting Triggernometry engine...", gen)

    def stop(self, wait: bool = False) -> None:
        if not self._active and self._proc is None:
            if wait:
                self._close_telesto(None, wait=True)
            return
        with self._state_lock:
            self._active = False
            # A stopped engine has no live generation. Bumping here retires
            # every token the old threads could still stamp, so their queued
            # emits fail the slot check too, not just the dispatch gate.
            self._gen += 1
            gen = self._gen
            proc, self._proc = self._proc, None
            relay, self._telesto = self._telesto, None
            wq = self._wq   # capture this generation's queue under the lock
        self._close_telesto(relay, wait=wait)
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
        self.status.emit(False, "Off", gen)

    def _close_telesto(self, relay, wait=False):
        with self._state_lock:
            self._retired_telesto = [old for old in self._retired_telesto if not old.is_finished()]
            if relay is not None:
                self._retired_telesto.append(relay)
            retired = list(self._retired_telesto)
        if relay is not None:
            relay.close()
        if wait:
            for old in retired:
                old.close(wait=True)

    @staticmethod
    def _signal_group(proc: subprocess.Popen, graceful: bool) -> None:
        """SIGTERM, graceful, or SIGKILL the sidecar's whole process group,
        falling back to the direct child if the group is already gone."""
        try:
            if os.name == "posix":
                import signal
                # start_new_session makes the child PID its group ID.
                # The group can survive after the wrapper has been reaped.
                os.killpg(proc.pid, signal.SIGTERM if graceful else signal.SIGKILL)
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
        # The reader and stop can both reach cleanup for the same process.
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
                    # Waiting for xvfb-run alone says nothing about its children.
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

    def _read_loop(self, proc: subprocess.Popen, wq: queue.Queue, gen: int) -> None:
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
                    log_drop("engine-parse", f"unparsed trig sidecar line {line[:160]!r}", 0)
                    continue
                try:
                    self._dispatch(msg, gen)
                except Exception as exc:
                    _log(f"dispatch error: {exc!r}")
        except Exception as exc:
            _log(f"reader error: {exc!r}")
        finally:
            # Retire only this generation, including when stdout fails.
            with self._state_lock:
                was_current = proc is self._proc and self._active
                relay = None
                if was_current:
                    self._active = False
                    self._proc = None
                    relay, self._telesto = self._telesto, None
            try:
                if relay:
                    self._close_telesto(relay)
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

    def _dispatch(self, msg: dict, gen: "int | None" = None) -> None:
        kind = msg.get("t")
        # Stale generation gate. stop flips _active and retires the
        # generation while the reader thread still has queued lines, and a
        # restart swaps _proc and flips _active back on while the previous
        # reader is still draining its buffered stdout. Output from a dead
        # generation must not fire into the live session, and a late boot
        # status would flip the indicator back on for a dying engine. The gen
        # token rides each emit below so the UI slot drops it too, queued
        # delivery can outlive the generation. Inventory frames stay useful
        # either way.
        if kind in ("callout", "sound", "status") and not self._gen_live(gen):
            return
        if kind == "callout":
            tts = self._apply_replacements((msg.get("tts") or "").strip())
            text = self._apply_replacements((msg.get("text") or msg.get("tts") or "").strip())
            sev = msg.get("severity", "info")
            if sev not in ("info", "alert", "alarm"):
                sev = "info"
            if text:
                self.callout.emit(text, sev, gen)
            if tts:
                self.tts.emit(tts, gen)
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
                self.sound.emit(f, max(0, min(100, vol)), gen)
        elif kind == "status":
            self.status.emit(bool(msg.get("active", self._active)), str(msg.get("msg", "")), gen)
        elif kind == "inventory":
            triggers = msg.get("triggers")
            if isinstance(triggers, list):
                self.inventory.emit(json.dumps(triggers))
        # unknown kinds ignored
