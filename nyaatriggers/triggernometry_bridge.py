#!/usr/bin/env python3
"""Relay JSON lines to the MIT-licensed paissaheavyindustries/Triggernometry engine.
The host supports C# scripts. POSIX requires Mono."""

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

_MAX_LINE = 1 << 20

_MAX_QUEUE_BYTES = 64 << 20


def _read_lines_bounded(stream):
    """Yield bounded lines and discard oversized input through its newline."""
    while True:
        line = stream.readline(_MAX_LINE + 1)
        if not line:
            return
        if len(line) > _MAX_LINE and not line.endswith("\n"):
            while True:
                more = stream.readline(_MAX_LINE + 1)
                if not more or more.endswith("\n"):
                    break
            continue
        yield line


class _ByteQueue(queue.Queue):
    """Bound queued strings by bytes and count. Stop sentinels still need an item slot."""

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


_LOG_LOCK = threading.Lock()


def _log(msg: str) -> None:
    """Write persistent diagnostics without interrupting the engine on logging failure."""
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


def _pack_stamp(path):
    try:
        info = Path(path).stat()
    except OSError:
        return None
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def has_mono() -> bool:
    return os.name == "nt" or _find_mono() is not None


def has_exe() -> bool:
    return _find_exe() is not None


def is_available() -> bool:
    return has_exe() and has_mono()


class TriggernometryBridge(QObject):

    # Generation stamps let UI slots reject stale output after restarts.
    callout   = pyqtSignal(str, str, int)   # on-screen text, severity in {info, alert, alarm}, generation
    tts       = pyqtSignal(str, int)        # spoken text, generation
    sound     = pyqtSignal(str, int, int)   # sound file path, volume 0-100, generation
    status    = pyqtSignal(bool, str, int)  # active, message, generation
    inventory = pyqtSignal(str, int)   # editable speech and engine generation
    feed_overflow = pyqtSignal(int)

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
        self._pack_stamps = {}
        # An active flag alone cannot distinguish replaced readers.
        self._gen = 0
        self._overflow_gen = -1
        self._replacements: list = []
        self._disabled: frozenset = frozenset()
        self._state_lock = threading.Lock()

    @staticmethod
    def is_available() -> bool:
        return is_available()

    def is_active(self) -> bool:
        return self._active

    def pack_is_current(self, path: Path) -> bool:
        """Check that the pack has not changed since this engine started."""
        stamp = _pack_stamp(path)
        return (self._active and stamp is not None
                and self._pack_stamps.get(Path(path).absolute()) == stamp)

    def generation(self) -> int:
        return self._gen

    def _gen_live(self, gen: "int | None") -> bool:
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
        self._disabled = frozenset(str(x) for x in (ids or ()))
        self._send_command({"t": "set_disabled", "ids": sorted(self._disabled)})

    def set_callout(self, cid: str, tts: "str | None" = None,
                    text: "str | None" = None, enable: "bool | None" = None) -> None:
        """Preserve template tokens. None restores defaults. Use set_disabled for enable changes."""
        if not cid:
            return
        if tts is None and text is None and enable is not None:
            # Enable-only edits must not send null text and erase a custom template.
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
    def _launch_cmd(exe: Path, packs: list[str]) -> "list[str]":
        """POSIX needs Xvfb for WinForms controls and Mono. Windows runs the host directly."""
        cfg = str(_rundata_dir())
        argv = [str(exe), cfg, "--serve", "--"] + packs
        if os.name == "nt":
            return argv
        mono = _find_mono()
        cmd = [mono] + argv if mono else argv
        xvfb = shutil.which("xvfb-run")
        if xvfb:
            cmd = [xvfb, "-a", "-s", "-screen 0 1024x768x24"] + cmd
        return cmd

    def start(self) -> None:
        if self._active:
            return
        _log(f"start() requested (os={os.name})")
        exe = _find_exe()
        if exe is None or not has_mono():
            _log(f"cannot start: exe={exe!r} mono={_find_mono()!r} has_mono={has_mono()} "
                 f"packs={_find_packs()!r}")
            self.status.emit(False, "Mono runtime or triggernometry-core.exe not found", self._gen)
            return

        if os.name == "posix":
            _make_bundled_mono_executable()

        try:
            packs = _find_packs()
            pack_stamps = {Path(path).absolute(): _pack_stamp(path) for path in packs}
            cmd = self._launch_cmd(exe, packs)
        except OSError as e:
            _log(f"launch failed: {e!r}")
            self.status.emit(False, f"Failed to launch sidecar: {e}", self._gen)
            self._proc = None
            return
        _log("launch: " + shlex.join(str(c) for c in cmd) + f"  (cwd={exe.parent})")
        popen_kwargs = dict(
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            bufsize=1, text=True, encoding="utf-8", errors="replace",
            cwd=str(exe.parent),
        )
        if os.name == "posix":
            popen_kwargs["start_new_session"] = True
            # Use stdin EOF on parent death so Xvfb can clean up its child.
        if os.name == "nt":
            popen_kwargs["creationflags"] = 0x08000000
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
            self._pack_stamps = pack_stamps
            self._gen += 1
            gen = self._gen
        self._reader = threading.Thread(target=self._read_loop, args=(proc, wq, gen), daemon=True, name="tn-reader")
        self._errpump = threading.Thread(target=self._err_loop, args=(proc,), daemon=True, name="tn-stderr")
        self._writer = threading.Thread(target=self._write_loop, args=(proc, wq), daemon=True, name="tn-writer")
        self._reader.start()
        self._errpump.start()
        self._writer.start()
        # Replay disabled IDs because offline commands were discarded and callouts carry no IDs.
        self._send_command({"t": "set_disabled", "ids": sorted(self._disabled)})
        self.status.emit(True, "Starting Triggernometry engine...", gen)

    def stop(self, wait: bool = False) -> None:
        if not self._active and self._proc is None:
            if wait:
                self._close_telesto(None, wait=True)
            return
        with self._state_lock:
            self._active = False
            self._gen += 1
            gen = self._gen
            proc, self._proc = self._proc, None
            relay, self._telesto = self._telesto, None
            wq = self._wq
        self._close_telesto(relay, wait=wait)
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
        if not self._active:
            return
        try:
            line = json.dumps(obj, ensure_ascii=False)
        except (TypeError, ValueError):
            return
        try:
            self._wq.put_nowait(line)
        except queue.Full:
            generation = self._gen
            if self._active and self._overflow_gen != generation:
                self._overflow_gen = generation
                log_drop("engine-feed", "Triggernometry feed queue full; restarting engine")
                self.feed_overflow.emit(generation)

    def feed_log(self, line: str) -> None:
        if not self._active or not line:
            return
        # The host also derives zone changes from raw zone lines.
        self._enqueue({"t": "log", "line": line})

    def feed_combatants(self, payload: dict) -> None:
        if not self._active or not isinstance(payload, dict):
            return
        self._enqueue({"t": "combatants", "me": payload.get("me", 0), "list": payload.get("list", [])})

    def feed_combat(self, act: bool, game: bool) -> None:
        self._enqueue({"t": "combat", "active": bool(act)})

    def feed_player(self, player_id: int, _name: str = "") -> None:
        try:
            player_id = int(player_id or 0)
        except (TypeError, ValueError, OverflowError):
            player_id = 0
        self._enqueue({"t": "player", "id": player_id if 0 <= player_id <= 0x7FFFFFFF else 0})

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
                try:
                    self._reap(proc)
                finally:
                    if proc.stdout is not None:
                        try:
                            proc.stdout.close()
                        except OSError:
                            pass

    def _err_loop(self, proc: subprocess.Popen) -> None:
        if proc.stderr is None:
            return
        try:
            for line in _read_lines_bounded(proc.stderr):
                line = line.rstrip()
                if line:
                    _log(f"[sidecar stderr] {line}")
        finally:
            try:
                proc.stderr.close()
            except OSError:
                pass

    def _apply_replacements(self, s: str) -> str:
        rules = self._replacements
        if not rules or not s:
            return s.strip()
        out = s
        for r in rules:
            if not r.get("enabled", True):
                continue
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
            # Preserve callouts when bounded regex substitution fails.
            out = _safe_sub(rx, repl, out)
        return out.strip()

    def _dispatch(self, msg: dict, gen: "int | None" = None) -> None:
        kind = msg.get("t")
        # Reject old generations here and again in queued UI slots.
        if kind in ("callout", "sound", "status", "inventory") and not self._gen_live(gen):
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
                self.inventory.emit(json.dumps(triggers), gen)
