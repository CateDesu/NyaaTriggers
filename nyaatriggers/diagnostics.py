"""Bounded support records containing only approved diagnostic fields."""

from __future__ import annotations

import builtins
from datetime import datetime, timezone
import errno
from functools import lru_cache
import importlib.metadata
import io
import json
import math
import os
from pathlib import Path
import platform
import re
import stat
import sys
import tempfile
import threading
import time
import uuid
import zipfile
import zlib

from nyaatriggers.paths import bundle_root, data_root, source_root
from nyaatriggers.engine_build_info import engine_commit

_LOG_FILE = Path(os.environ.get("NYAA_DIAGNOSTICS_DIR") or data_root()) / "diagnostics.log"
_ENABLED = os.environ.get("NYAA_REPLAY_TEST") != "1" or bool(os.environ.get("NYAA_DIAGNOSTICS_DIR"))
_MAX_BYTES = 8 << 20
_MAX_RECORD_BYTES = 16384
_GENERATIONS = 3
_SESSION = uuid.uuid4().hex
_START = time.monotonic()
_LOCK = threading.RLock()
_INT_LIMIT = (1 << 53) - 1
_VERSION = re.compile(r"[0-9]{1,4}(?:\.[0-9]{1,4}){0,4}(?:(?:a|b|rc|\.post|\.dev)[0-9]{1,4})?\Z")
_IDENTIFIER = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]{0,159}\Z")
_ERROR_TYPES = frozenset(name for name, value in vars(builtins).items()
                         if isinstance(value, type) and issubclass(value, BaseException)) | {
    "HTTPError", "URLError", "TimeoutExpired", "CalledProcessError", "JSONDecodeError",
    "BadZipFile", "error", "Empty", "Full",
    "java.lang.NullPointerException", "java.lang.IllegalStateException",
    "java.lang.IllegalArgumentException", "java.lang.RuntimeException",
    "java.lang.NoClassDefFoundError", "java.lang.ClassNotFoundException",
    "java.util.NoSuchElementException", "java.lang.InterruptedException",
    "java.lang.AssertionError", "java.lang.OutOfMemoryError",
    "java.lang.StackOverflowError", "java.io.IOException",
    "gg.xp.xivsupport.events.triggers.seq.SequentialTriggerTimeoutException",
}
_DROP_SITES = frozenset("""
cactbot cactbot-inventory cooldown crash-log dispatch dispatch-budget dps-meter
dps-snapshot dps-store engine-chain engine-feed engine-parse engine-seq fflogs
fflogs-api fflogs-token fight-catalog plugin-drop plugin-link plugin-tx
plugin-tx-dps plugin-tx-tick pull-capture pull-observer redos save session-tracking
te-inventory telesto-http telesto-mark telesto-queue telesto-send timeline
timeline-sync tn-inventory trigger-load trigger-sequence triggers tts-aplay
tts-backend tts-backlog tts-error tts-interrupt tts-jp tts-kokoro tts-notify
tts-overflow tts-piper tts-playback tts-shutdown tts-sound umad update
""".split())


def _enum(*values):
    return frozenset(values)


_ENGINE_REASONS = _enum(
    "unavailable", "snapshot_failed", "spawn_failed", "write_failed", "read_failed",
    "dispatch_failed", "invalid_json", "invalid_shape", "oversize", "unknown_type",
    "feed_protocol", "queue_full", "cancel_overflow", "unsupported", "inactive",
    "stale", "disabled", "cancel_pending", "cancel_all_pending",
    "cancel_overflow_pending", "replacement_empty", "empty", "ready", "requested",
    "eof", "exception", "sequence_gap", "sequence_regression")
_ENGINE = {
    **dict.fromkeys(("gen", "previous_gen", "seq", "count", "frames", "chars",
                    "frame_chars", "queue_depth", "queue_bytes", "dropped",
                    "checkpoint", "token", "pending_ids"), "int"),
    **dict.fromkeys(("active", "accepted", "expected", "text", "tts", "tts_only",
                    "history", "recovery", "catchup", "custom", "speech_cancel",
                    "speech_cancel_all", "wait", "java_available", "jar_available",
                    "xvfb", "has_text", "has_tts", "expired", "replaying"), "bool"),
    "duration_ms": "number", "queue_delay_ms": "number", "returncode": "signed",
    "reason": _ENGINE_REASONS,
    "result": _enum("queued", "rejected", "received", "emitted", "filtered",
                    "acknowledged", "ignored"),
    "state": _enum("cancel_speech", "custom_triggers", "set_callout", "reset_callout",
                   "set_automark", "unknown", "recover_begin", "recover_log",
                   "catchup", "recover_end", "recovery_checkpoint", "recovered",
                   "status", "inventory", "combatants_request", "telesto"),
    "channel": _enum("stdout", "stderr", "feed", "writer", "text", "tts"),
    "error_type": "error", "trigger_class": "class", "trigger_field": "identifier",
}
_SCHEMAS = {event: dict(_ENGINE) for event in (
    "engine_start", "engine_started", "engine_ready", "engine_stop", "engine_exit",
    "engine_error", "engine_protocol", "engine_queue", "engine_command",
    "engine_recovery", "engine_cancel", "engine_callout")}
_SCHEMAS.update({
    "app_start": {"version": "version", "frozen": "bool"},
    "app_stop": {},
    "ws_state": {"state": _enum("connecting", "connected", "disconnected", "error",
                                "reconnect_scheduled", "heartbeat_timeout",
                                "pong_accepted", "pong_rejected"),
                 "delay_ms": "int", "error_code": "signed", "elapsed_ms": "int"},
    "ws_feed": {**dict.fromkeys(("frames", "bytes", "log_lines", "rejected", "zone_id"), "int"),
                "combat": "bool"},
    "gaze_state": {"kind": _enum("vfx", "gain", "loss", "reset"),
                   "vfx": "int", "duration_s": "number", "slot": "int",
                   "sets": "int", "assigned": "int", "marked": "int",
                   "polarity": _enum("away1", "look1"), "tell_age_s": "number"},
    "gaze_action": {"kind": _enum("mark", "clear"), "slot": "int",
                    "result": _enum("queued", "pending")},
    "marker_transport": {"kind": _enum("mark", "clear"), "slot": "int",
                         "result": _enum("accepted", "failed", "cancelled", "unknown_slot")},
    "ui_callout": {"gen": "int", "channel": _enum("display", "speech"),
                   "result": _enum("stale", "disabled", "disconnected", "emitted",
                                   "empty", "duplicate", "queued")},
    "tts_queue": {"gen": "int", "depth": "int", "duration_ms": "number",
                  "result": _enum("muted", "suspended", "queued", "evicted",
                                  "dequeued", "finished", "interrupted", "failed"),
                  "kind": _enum("tts", "wav")},
    "tts_backend": {"backend": _enum("piper", "system", "kokoro", "aplay", "winsound"),
                    "result": _enum("attempt", "unavailable", "superseded", "timed_out",
                                    "interrupted", "failed", "finished", "early_exit", "muted", "stale"),
                    "gen": "int", "returncode": "signed", "duration_ms": "number", "elapsed_ms": "number"},
    "python_exception": {"site": _enum("uncaught", "tts", "thread", "dispatch"),
                         "error_type": "error", "frames": "python_frames"},
    "drop": {"site": _DROP_SITES, "count": "int", "error_type": "error"},
    "sequence_failed": {"gen": "int", "trigger_class": "class", "trigger_field": "identifier",
                        "initial_event": "class", "wait_event": "class",
                        "wait_kind": _enum("event", "time", "burst", "until", "unknown"),
                        "wait_ms": "int", "error_type": "error", "frames": "java_frames",
                        "replaying": "bool"},
    "engine_pipeline": {"gen": "int", "counters": "counters", "replaying": "bool"},
    "engine_runtime": {"gen": "int", "java_version": "version", "heap_max": "int"},
    "engine_event": {"gen": "int", "event_kind": _enum("cast", "buff_add", "buff_remove",
                        "tether", "headmarker", "death", "zone", "pull_start", "pull_end"),
                     **dict.fromkeys(("game_id", "source_actor", "target_actor",
                                      "duration_ms", "stacks", "zone_id"), "int"),
                     "event_ms": "signed", "on_player": "bool", "source_player": "bool",
                     "replaying": "bool"},
    "support_snapshot": {"program_version": "version", "python_version": "version",
                         "qt_version": "version", "pyqt_version": "version",
                         "os": _enum("nt", "posix", "unknown"), "frozen": "bool",
                         "platform": _enum("linux", "win32", "darwin", "unknown"),
                         "arch": _enum("x86_64", "AMD64", "aarch64", "arm64", "x86",
                                       "i386", "i686", "armv7l", "armv8l", "ppc64le", "riscv64", "unknown"),
                         "engine_commit": "commit", "settings": "settings"},
})
_SCHEMAS["engine_error"].update({
    "site": _enum("boot", "feed", "inventory", "callout", "delayed_speech", "command", "automark"),
    "frames": "java_frames",
})
_SCHEMAS["engine_event"].update(source_actor="actor", target_actor="actor")
for _event in ("gaze_action", "marker_transport"):
    _SCHEMAS[_event]["marker"] = _enum(
        *(f"attack{i}" for i in range(1, 9)), "attack", "clear",
        "bind1", "bind2", "bind3", "ignore1", "ignore2",
        "circle", "cross", "triangle", "square")
_SCHEMAS["engine_started"]["engine_commit"] = "commit"
_SCHEMAS["engine_recovery"]["history_status"] = _enum(
    "complete", "degraded", "unavailable", "failed", "state_only")
_SCHEMAS["recovery_state"] = {
    **dict.fromkeys(("gen", "pending_frames", "pending_bytes", "checkpoint"), "int"),
    "state": _enum("starting", "stopped", "ready", "connected", "disconnected",
                   "restoring", "fallback", "queue_wait", "catchup", "ending",
                   "live", "restart", "buffer_overflow"),
    "reason": _enum("engine_start", "engine_exit", "requested", "connection", "disconnect",
                    "ready", "history_restore", "recovery_unsupported", "no_local_log",
                    "history_unsupported", "invalid_world_state", "state_only", "unknown",
                    "queue_full", "checkpoint", "acknowledged", "catchup_unsupported",
                    "progress_timeout", "feed_overflow", "buffer_overflow", "reconnect"),
}
_SETTINGS = {
    **dict.fromkeys(("auto_connect", "local_enabled", "cactbot_enabled", "triggevent_auto_update",
                     "triggevent_record_pulls", "triggernometry_enabled", "telesto_enabled",
                     "overlay_enabled", "jp_neural_enabled", "overlay_sound_enabled",
                     "triggevent_mode", "triggernometry_mode", "connected", "muted",
                     "speech_suspended"), "bool"),
    "tts_engine": _enum("piper", "system"), "active_tts_engine": _enum("piper", "system"),
    "master_volume": "volume", "active_volume": "volume",
    **dict.fromkeys(("cactbot_disabled_count", "triggevent_disabled_count", "triggernometry_disabled_count",
                     "triggevent_edit_count", "triggernometry_edit_count", "engine_text_override_count"), "int"),
}
_SETTING_COUNTS = {
    "cactbot_disabled_triggers": "cactbot_disabled_count",
    "triggevent_disabled_triggers": "triggevent_disabled_count",
    "triggernometry_disabled_triggers": "triggernometry_disabled_count",
    "triggevent_callout_edits": "triggevent_edit_count",
    "triggernometry_callout_edits": "triggernometry_edit_count",
    "engine_text_overrides": "engine_text_override_count",
}
_INTERNAL_MODULES = """
__init__ app_common cactbot_reader combatant_responses convert_cactbot
convert_event_trigger convert_triggernometry death_recap diagnostics dps_meter
dps_store drop_log engine_build_info fflogs fight_catalog http_fetch instance_lock locale_util
main_window paths plugin_link proc_env prog_phases prog_session pull_capture
recap_filters recap_log recap_store record_store sequential status_metadata
status_timer telesto_client theme timeline_engine timeline_parser trigger_dialog
trigger_engine trigger_profiles triggernometry_bridge triggernometry_dialog
triggernometry_editor triggernometry_telesto triggevent_bridge triggevent_custom
triggevent_dialog triggevent_recovery tts ui.__init__ ui.ambient_fx
ui.automarkers_tab ui.connection ui.custom_triggevent ui.death_recap_tab ui.dps_tab
ui.engines ui.instance_tab ui.profiles ui.prog_comparison ui.prog_tab ui.recap_browser ui.recap_widgets
ui.session_tracking ui.settings_tab ui.timeline_tab ui.triggernometry_editor
ui.triggers_tab ui.voice_tab umad_chains updater updater_ui voice_config ws_client
""".split()
_INTERNAL_FILES = frozenset({"main.py"} | {
    "nyaatriggers/" + module.replace(".", "/") + ".py" for module in _INTERNAL_MODULES})


@lru_cache(maxsize=1)
def _java_classes():
    classes = set()
    for base in (bundle_root(), source_root()):
        jar = base / "triggevent-core" / "target" / "triggevent-core.jar"
        try:
            with zipfile.ZipFile(jar) as archive:
                classes.update(name[:-6].replace("/", ".") for name in archive.namelist()
                               if name.startswith("gg/xp/") and name.endswith(".class")
                               and len(name) <= 256)
            break
        except (OSError, ValueError, zipfile.BadZipFile):
            continue
    return frozenset(classes)


@lru_cache(maxsize=256)
def _java_members(class_name):
    """Read declared code identifiers without loading or executing Java classes."""
    for base in (bundle_root(), source_root()):
        try:
            with zipfile.ZipFile(base / "triggevent-core" / "target" / "triggevent-core.jar") as archive:
                member = archive.getinfo(class_name.replace(".", "/") + ".class")
                if member.file_size > 1 << 20:
                    return frozenset(), frozenset()
                data = archive.read(member)
            offset = 8

            def number(size):
                nonlocal offset
                result = int.from_bytes(data[offset:offset + size], "big")
                offset += size
                if offset > len(data):
                    raise ValueError("Invalid class file")
                return result

            constants = {}
            count = number(2)
            index = 1
            while index < count:
                tag = number(1)
                if tag == 1:
                    length = number(2)
                    constants[index] = data[offset:offset + length].decode("utf-8", errors="replace")
                    offset += length
                elif tag in {5, 6}:
                    number(8)
                    index += 1
                else:
                    number({3: 4, 4: 4, 7: 2, 8: 2, 9: 4, 10: 4, 11: 4, 12: 4,
                            15: 3, 16: 2, 17: 4, 18: 4, 19: 2, 20: 2}[tag])
                index += 1
            number(6)
            number(2 * number(2))
            groups = []
            for _ in range(2):
                names = set()
                for _ in range(number(2)):
                    number(2)
                    names.add(constants[number(2)])
                    number(2)
                    for _ in range(number(2)):
                        number(2)
                        number(number(4))
                groups.append(frozenset(names))
            return tuple(groups)
        except (OSError, ValueError, KeyError, IndexError, RuntimeError, zipfile.BadZipFile, zlib.error):
            continue
    return frozenset(), frozenset()


def _clean_value(rule, value):
    if isinstance(rule, frozenset):
        return value if type(value) is str and value in rule else None
    if rule == "bool":
        return value if type(value) is bool else None
    if rule == "actor":
        return value if type(value) is int and 0 <= value <= 1000000 else None
    if rule in {"int", "signed"}:
        lower = -_INT_LIMIT if rule == "signed" else 0
        return value if type(value) is int and lower <= value <= _INT_LIMIT else None
    if rule == "number":
        return value if type(value) in (int, float) and 0 <= value <= _INT_LIMIT and math.isfinite(value) else None
    if rule == "volume":
        return value if type(value) in (int, float) and 0 <= value <= 2 and math.isfinite(value) else None
    if rule in {"version", "commit", "identifier", "class", "error"}:
        if type(value) is not str or len(value) > 256:
            return None
        if rule == "version":
            return value if _VERSION.fullmatch(value) else None
        if rule == "commit":
            return value if re.fullmatch(r"[0-9a-f]{40}", value) else None
        if rule == "identifier":
            return value if _IDENTIFIER.fullmatch(value) else None
        if rule == "class":
            return value if value in _java_classes() else None
        return value if value in _ERROR_TYPES or value in _java_classes() else "Exception"
    if rule == "settings":
        if type(value) is not dict:
            return None
        cleaned = _clean_fields(_SETTINGS, value)
        for key, output in _SETTING_COUNTS.items():
            if type(value.get(key)) in (list, dict):
                cleaned[output] = len(value[key])
        return cleaned
    if rule == "counters":
        return _clean_fields({key: "int" for key in ("raw", "casts", "abilities", "buffs", "callouts", "tts", "failures")}, value)
    if rule in {"python_frames", "java_frames"}:
        if type(value) is not list:
            return None
        cleaned = []
        for frame in value[:32]:
            if type(frame) is not dict:
                continue
            line = _clean_value("signed", frame.get("line"))
            if line is None or not -2 <= line <= 10000000:
                continue
            if rule == "python_frames":
                filename = frame.get("file")
                if type(filename) is str and filename in _INTERNAL_FILES:
                    cleaned.append({"file": filename, "line": line})
            else:
                cls = _clean_value("class", frame.get("class"))
                method = _clean_value("identifier", frame.get("method"))
                if cls is not None and method is not None and method in _java_members(cls)[1]:
                    cleaned.append({"class": cls, "method": method, "line": line})
        return cleaned
    return None


def _clean_fields(schema, fields):
    if type(fields) is not dict:
        return None
    result = {}
    for name, rule in schema.items():
        if name in fields:
            value = _clean_value(rule, fields[name])
            if value is not None:
                result[name] = value
    # A field name without its known built-in class could identify a custom trigger.
    if "trigger_class" not in result or result.get("trigger_field") not in _java_members(result["trigger_class"])[0]:
        result.pop("trigger_field", None)
    return result


def _entry(event, fields):
    return {"schema": 1, "time": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "session": _SESSION, "elapsed_ms": max(0, int((time.monotonic() - _START) * 1000)),
            "event": event, "data": fields}


def _encode(entry):
    return (json.dumps(entry, ensure_ascii=True, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")


def _owner_only(path, flags):
    try:
        mode = os.lstat(path).st_mode
    except FileNotFoundError:
        mode = stat.S_IFREG
    if not stat.S_ISREG(mode):
        raise OSError(errno.EINVAL, "Diagnostic log is not a regular file")
    fd = os.open(path, flags | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(errno.EINVAL, "Diagnostic log is not a regular file")
    except BaseException:
        os.close(fd)
        raise
    return fd


def _private_permissions(stream, path):
    if stat.S_IMODE(os.fstat(stream.fileno()).st_mode) != 0o600:
        if hasattr(os, "fchmod"):
            os.fchmod(stream.fileno(), 0o600)
        else:
            os.chmod(path, 0o600)


def record(event: str, **fields) -> None:
    """Ignore unknown fields and keep failures from interrupting the program."""
    if not _ENABLED or type(event) is not str or event not in _SCHEMAS:
        return
    try:
        data = _encode(_entry(event, _clean_fields(_SCHEMAS[event], fields)))
        if len(data) > _MAX_RECORD_BYTES:
            return
        with _LOCK:
            try:
                current = _LOG_FILE.lstat()
            except FileNotFoundError:
                current = None
            if current is not None and not stat.S_ISREG(current.st_mode):
                return
            if current is not None and current.st_size + len(data) > _MAX_BYTES:
                for generation in range(_GENERATIONS - 1, 0, -1):
                    previous = _LOG_FILE if generation == 1 else _LOG_FILE.with_name(f"{_LOG_FILE.name}.{generation - 1}")
                    if previous.exists() and stat.S_ISREG(previous.lstat().st_mode):
                        previous.replace(_LOG_FILE.with_name(f"{_LOG_FILE.name}.{generation}"))
            with open(_LOG_FILE, "ab", opener=_owner_only) as stream:
                _private_permissions(stream, _LOG_FILE)
                stream.write(data)
            for path in _log_paths():
                if path.exists() and stat.S_ISREG(path.lstat().st_mode) and stat.S_IMODE(path.lstat().st_mode) != 0o600:
                    with open(path, "rb", opener=_owner_only) as stream:
                        _private_permissions(stream, path)
    except (OSError, ValueError, TypeError, OverflowError, RecursionError):
        pass


def record_engine(payload, gen: int):
    """Accept only structured engine fields and return the safe event for the UI."""
    if type(payload) is not dict:
        return None
    event = payload.get("event")
    if type(event) is not str or event not in _SCHEMAS or not (
            event.startswith("engine_") or event == "sequence_failed"):
        return None
    fields = dict(payload)
    fields["gen"] = gen
    safe = _clean_fields(_SCHEMAS[event], fields)
    record(event, **safe)
    return {"event": event, **safe}


def record_drop(site, detail=None):
    if type(site) is not str or site not in _DROP_SITES:
        return
    error = None
    if type(detail) is str:
        error = next((name for name in sorted(_ERROR_TYPES) if re.search(r"(?<!\w)" + re.escape(name) + r"(?!\w)", detail[:4096])), None)
    record("drop", site=site, count=1, error_type=error)


def record_exception(event, exc, *, site="uncaught"):
    """Keep only registered source frames, without messages, locals or source text."""
    frames = []
    tb = exc.__traceback__ if isinstance(exc, BaseException) else None
    inspected = 0
    while tb is not None and len(frames) < 32 and inspected < 128:
        inspected += 1
        filename = tb.tb_frame.f_code.co_filename.replace("\\", "/")
        relative = filename if filename in _INTERNAL_FILES else None
        if "/nyaatriggers/" in filename:
            relative = "nyaatriggers/" + filename.rsplit("/nyaatriggers/", 1)[1]
        if filename == str(source_root() / "main.py").replace("\\", "/"):
            relative = "main.py"
        if relative in _INTERNAL_FILES:
            frames.append({"file": relative, "line": tb.tb_lineno})
        tb = tb.tb_next
    record(event, site=site, error_type=type(exc).__name__, frames=frames)


exception = record_exception


def _log_paths():
    return [_LOG_FILE.with_name(f"{_LOG_FILE.name}.{n}") for n in range(_GENERATIONS - 1, 0, -1)] + [_LOG_FILE]


def _validated_entry(value):
    if type(value) is not dict or type(value.get("schema")) is not int or value["schema"] != 1:
        return None
    event = value.get("event")
    if type(event) is not str or event not in _SCHEMAS:
        return None
    stamp, session = value.get("time"), value.get("session")
    elapsed = _clean_value("int", value.get("elapsed_ms"))
    if (type(stamp) is not str or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z", stamp)
            or type(session) is not str or not re.fullmatch(r"[0-9a-f]{32}", session) or elapsed is None):
        return None
    try:
        datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    data = _clean_fields(_SCHEMAS[event], value.get("data"))
    if data is None:
        return None
    return {"schema": 1, "time": stamp, "session": session, "elapsed_ms": elapsed,
            "event": event, "data": data}


def _snapshot(settings):
    settings = dict(settings) if type(settings) is dict else {}
    tts = sys.modules.get("nyaatriggers.tts")
    if tts is not None:
        settings.update(active_tts_engine=getattr(tts, "_engine", None),
                        active_volume=getattr(tts, "_master_volume", None),
                        speech_suspended=getattr(tts, "_speech_suspended", None))
    fields = {"python_version": ".".join(map(str, sys.version_info[:3])),
              "os": os.name if os.name in {"nt", "posix"} else "unknown",
              "platform": sys.platform if sys.platform in {"linux", "win32", "darwin"} else "unknown",
              "arch": platform.machine(),
              "frozen": bool(getattr(sys, "frozen", False)), "settings": settings}
    app = sys.modules.get("nyaatriggers.app_common")
    fields["program_version"] = (_clean_value("version", getattr(app, "_DISPLAY_VERSION", None))
                                 or getattr(app, "_VERSION", None))
    for distribution, field in (("PyQt6", "pyqt_version"), ("PyQt6-Qt6", "qt_version")):
        try:
            fields[field] = importlib.metadata.version(distribution)
        except (importlib.metadata.PackageNotFoundError, OSError, ValueError):
            pass
    fields["engine_commit"] = engine_commit(
        bundle_root() / "triggevent-core" / "target" / "triggevent-core.jar")
    return _entry("support_snapshot", _clean_fields(_SCHEMAS["support_snapshot"], fields))


def _log_snapshots():
    snapshots = []
    with _LOCK:
        for path in _log_paths():
            try:
                with open(path, "rb", opener=_owner_only) as stream:
                    snapshots.append(stream.read(_MAX_BYTES + _MAX_RECORD_BYTES))
            except OSError:
                continue
    return snapshots


def export_diagnostics(destination: Path, settings: dict | None = None) -> int:
    """Atomically export validated support records. Raw local logs are never read."""
    destination = Path(destination)
    try:
        resolved_destination = destination.resolve()
    except RuntimeError as exc:
        raise ValueError("Choose a writable export file") from exc
    for path in _log_paths():
        try:
            reserved = path.resolve()
        except (OSError, RuntimeError):
            reserved = Path(os.path.abspath(path))
        if resolved_destination == reserved:
            raise ValueError("Choose an export file outside the diagnostic log files")
    fd, temporary = tempfile.mkstemp(prefix=".nyaa-diagnostics-", suffix=".tmp", dir=destination.parent)
    count = 0
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(_encode(_snapshot(settings)))
            count += 1
            for snapshot in _log_snapshots():
                source = io.BytesIO(snapshot)
                while line := source.readline(_MAX_RECORD_BYTES + 1):
                    if len(line) > _MAX_RECORD_BYTES or not line.endswith(b"\n"):
                        while line and not line.endswith(b"\n"):
                            line = source.readline(_MAX_RECORD_BYTES + 1)
                        continue
                    try:
                        entry = _validated_entry(json.loads(line))
                    except (ValueError, UnicodeError, RecursionError):
                        continue
                    if entry is not None:
                        output.write(_encode(entry))
                        count += 1
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, destination)
        return count
    finally:
        Path(temporary).unlink(missing_ok=True)
