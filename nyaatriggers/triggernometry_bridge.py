#!/usr/bin/env python3
"""Run the MIT-licensed paissaheavyindustries/Triggernometry engine through
triggernometry-core. Send log lines, zone changes and combatant snapshots as JSON lines
on stdin. Read callout, sound and status messages from stdout and relay them as Qt
signals. Mono is required on POSIX. The host supports runtime C# scripts and resolves
combatant data without FFXIV_ACT_Plugin.
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
    """Bound queued strings by retained bytes and item count. The stop sentinel uses no
    byte budget but still needs an item slot.
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


def _find_exe() -> "Path | None":
    """Find the host executable beside its engine dependencies. Use that directory as the
    working directory.
    """
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
    """Prefer bundled Mono, then PATH. Windows runs the executable directly."""
    if os.name == "nt":
        return None
    for base in _bundle_bases():
        cand = base / "mono" / "bin" / "mono"
        if cand.is_file():
            return str(cand)
    return shutil.which("mono")


def _make_bundled_mono_executable() -> None:
    """Restore executable permissions lost when bundled Mono is packaged as data."""
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
    """Use a writable user directory for host configuration and sound cache."""
    if os.name == "nt":
        # An empty APPDATA must fall back to home rather than the current directory.
        root = Path(os.environ.get("APPDATA") or Path.home())
    else:
        root = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
    d = root / "nyaatriggers" / "triggernometry-core"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _packs_dir() -> Path:
    env = os.environ.get("NYAA_TRIGGERNOMETRY_PACKS")
    d = Path(env) if env else (_rundata_dir().parent / "triggernometry-packs")
    d.mkdir(parents=True, exist_ok=True)
    return d


def _log_path() -> "Path | None":
    try:
        return _rundata_dir() / "triggernometry.log"
    except OSError:
        return None


# Serialize log rotation and append across GUI and reader threads.
_LOG_LOCK = threading.Lock()


def _log(msg: str) -> None:
    """Write diagnostics to a persistent log and stderr. Logging failures must not stop the
    engine.
    """
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
            try:
                if p.exists() and p.stat().st_size > (1 << 20):
                    rotate_one_generation(p)
            except OSError:
                pass
            with open_private_log(p) as fh:
                fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {msg}\n")
    except Exception:  # noqa: BLE001
        pass


def _find_packs() -> "list[str]":
    try:
        # Accept XML suffixes in either case on Linux.
        return sorted(str(p) for p in _packs_dir().glob("*")
                      if p.is_file() and p.suffix.lower() == ".xml")
    except OSError:
        return []


def packs_dir() -> Path:
    return _packs_dir()


def has_packs() -> bool:
    return bool(_find_packs())


def has_mono() -> bool:
    """Windows needs no Mono runtime."""
    return os.name == "nt" or _find_mono() is not None


def has_exe() -> bool:
    return _find_exe() is not None


def is_available() -> bool:
    """Require both the host and its platform runtime."""
    return has_exe() and has_mono()


class TriggernometryBridge(QObject):

    # Stamp UI signals with the sidecar generation so queued output can be rejected
    # after restart.
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
        # Increment on start and stop. An active flag alone cannot distinguish output
        # from a replaced reader.
        self._gen = 0
        self._replacements: list = []
        self._disabled: frozenset = frozenset()
        # Makes the stop and reader-exit check-and-clear of _proc and _active atomic.
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
        """Update disabled callout IDs for the next firing without restarting."""
        # Normalize IDs before sorting settings values.
        self._disabled = frozenset(str(x) for x in (ids or ()))
        self._send_command({"t": "set_disabled", "ids": sorted(self._disabled)})

    def set_callout(self, cid: str, tts: "str | None" = None,
                    text: "str | None" = None, enable: "bool | None" = None) -> None:
        """Update the spoken template while preserving token substitution. None restores
        defaults. Route enable changes through set_disabled.
        """
        if not cid:
            return
        if tts is None and text is None and enable is not None:
            # An enable-only edit must not send null text, which would erase a custom
            # template.
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
        if cid:
            self._send_command({"t": "set_callout", "id": cid, "text": None})

    def _send_command(self, cmd: dict) -> None:
        self._enqueue(cmd)

    @staticmethod
    def _launch_cmd(exe: Path) -> "list[str]":
        """Use Xvfb and Mono on POSIX because the engine constructs WinForms controls.
        Windows runs the executable directly.
        """
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
        """Start if not already running. Check availability first."""
        if self._active:
            return
        _log(f"start() requested (os={os.name})")
        exe = _find_exe()
        if exe is None or not has_mono():
            _log(f"cannot start: exe={exe!r} mono={_find_mono()!r} has_mono={has_mono()} "
                 f"packs={_find_packs()!r}")
            self.status.emit(False, "Mono runtime or triggernometry-core.exe not found", self._gen)
            return

        # Restore executable permissions on bundled files.
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
            # Let stdin EOF stop the host after parent death. A parent-death signal
            # would stop the Xvfb wrapper before it cleans up its child.
        if os.name == "nt":
            popen_kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
        # Restore system library paths for Mono and its shell children.
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
        # Replay disabled IDs after startup. Offline commands were discarded and emitted
        # callouts have no IDs for local filtering.
        self._send_command({"t": "set_disabled", "ids": sorted(self._disabled)})
        self.status.emit(True, "Starting Triggernometry engine...", gen)

    def stop(self, wait: bool = False) -> None:
        if not self._active and self._proc is None:
            if wait:
                self._close_telesto(None, wait=True)
            return
        with self._state_lock:
            self._active = False
            # Invalidate queued output from the stopped generation.
            self._gen += 1
            gen = self._gen
            proc, self._proc = self._proc, None
            relay, self._telesto = self._telesto, None
            wq = self._wq
        self._close_telesto(relay, wait=wait)
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
        """Signal the process group, falling back to the direct child if unavailable."""
        try:
            if os.name == "posix":
                import signal
                # The process group may survive its wrapper.
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


    def _enqueue(self, obj: dict) -> None:
        """Queue a JSON message without blocking the caller."""
        if not self._active:
            return
        try:
            line = json.dumps(obj, ensure_ascii=False)
        except (TypeError, ValueError):
            return
        try:
            self._wq.put_nowait(line)
        except queue.Full:
            log_drop("engine-feed", "trig sidecar stdin queue full; dropped oldest feed message")
            try:
                self._wq.get_nowait()
                self._wq.put_nowait(line)
            except (queue.Empty, queue.Full):
                pass

    def feed_log(self, line: str) -> None:
        """Queue a raw log line from the GUI thread without blocking."""
        if not self._active or not line:
            return
        # The host also derives zone changes from raw zone lines.
        self._enqueue({"t": "log", "line": line})

    def feed_combatants(self, payload: dict) -> None:
        """Forward actor snapshots for identity, position, HP and party script variables.
        """
        if not self._active or not isinstance(payload, dict):
            return
        self._enqueue({"t": "combatants", "me": payload.get("me", 0), "list": payload.get("list", [])})

    def feed_zone(self, zone_id: int, zone_name: str) -> None:
        if not self._active:
            return
        self._enqueue({"t": "zone", "id": int(zone_id or 0), "name": zone_name or ""})

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
                # Release the writer even if exit reporting raises.
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

    def _dispatch(self, msg: dict, gen: "int | None" = None) -> None:
        kind = msg.get("t")
        # Reject output from old generations before dispatch and again in queued UI
        # slots. Inventory remains useful across restarts.
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
                # Resolve relative pack sounds against the pack directory.
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
