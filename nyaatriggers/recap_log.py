"""Replay network logs into temporary, indexed death recaps."""

from datetime import datetime
import json
from pathlib import Path
import tempfile
import threading

from nyaatriggers.death_recap import DeathRecap
from nyaatriggers.dps_meter import DpsMeter

LOG_KINDS = {b"01", b"02", b"03", b"04", b"21", b"22", b"24", b"25", b"26", b"30", b"33",
             b"37", b"38", b"39", b"42", b"43", b"260"}
MAX_LINE_BYTES = 64 * 1024
MAX_IMPORTED_DEATHS = 50000


class ImportedRecaps:
    def __init__(self, path):
        self.path = Path(path)
        self.records = []
        self.statuses = {}
        self.skipped = 0
        self.pulls = 0
        self._file = tempfile.TemporaryFile(mode="w+b")

    def add(self, death, stamp, pull):
        if len(self.records) >= MAX_IMPORTED_DEATHS:
            raise ValueError("This log exceeds the limit of 50,000 deaths")
        death["when"] = stamp
        raw = json.dumps(death, ensure_ascii=False, allow_nan=False).encode("utf-8")
        offset = self._file.tell()
        self._file.write(raw)
        self.records.append({key: death[key] for key in ("id", "actor", "name", "zone", "when")} |
                            {"offset": offset, "size": len(raw), "pull": pull})
        for statuses in [death["statuses"]] + [event.get(key, []) for event in death["events"]
                                               for key in ("statuses", "source_statuses")]:
            for status in statuses:
                ident = status.get("id")
                if isinstance(ident, int) and 0 < ident <= 65535:
                    self.statuses[str(ident)] = {"id": ident, "name": status["name"]}
        for event in death["events"]:
            ident = event.get("status_id")
            if isinstance(ident, int) and 0 < ident <= 65535:
                self.statuses.setdefault(str(ident), {"id": ident, "name": event["name"]})

    def read(self, record):
        self._file.seek(record["offset"])
        return json.loads(self._file.read(record["size"]))

    def close(self):
        self._file.close()


def read_log(path, cancel=None, progress=None):
    """Read the initial file extent so an actively growing log still finishes."""
    cancel = cancel or threading.Event()
    result = ImportedRecaps(path)
    recap = DeathRecap()
    stamp = 0.0
    observed = 0.0
    previous = None
    meter = DpsMeter(clock=lambda: observed)

    def started(_snapshot):
        result.pulls += 1
        recap.begin_pull()

    meter.on_pull_start = started
    meter.on_pull_finish = lambda _snapshot: recap.end_pull()
    recap.on_death = lambda death: result.add(death, stamp, result.pulls)
    try:
        with Path(path).open("rb") as source:
            source.seek(0, 2)
            size = source.tell()
            source.seek(0)
            position = 0
            lines = 0
            while position < size:
                if cancel.is_set():
                    result.close()
                    return None
                raw = source.readline(min(MAX_LINE_BYTES + 1, size - position))
                if not raw:
                    break
                position += len(raw)
                lines += 1
                if len(raw) > MAX_LINE_BYTES:
                    while not raw.endswith(b"\n") and position < size:
                        if cancel.is_set():
                            result.close()
                            return None
                        raw = source.readline(min(MAX_LINE_BYTES, size - position))
                        if not raw:
                            break
                        position += len(raw)
                    result.skipped += 1
                    continue
                if position == size and not raw.endswith(b"\n"):
                    result.skipped += 1
                    continue
                if progress and lines % 2048 == 0:
                    progress(min(99, position * 100 // max(1, size)))
                raw = raw.removeprefix(b"\xef\xbb\xbf")
                if raw.split(b"|", 1)[0] not in LOG_KINDS:
                    continue
                try:
                    fields = raw.decode("utf-8-sig").rstrip("\r\n").split("|")
                    if len(fields) < 3:
                        raise ValueError("Missing fields")
                    parsed = datetime.fromisoformat(fields[1])
                    if not 1970 <= parsed.year <= 9998:
                        raise ValueError("Invalid log date")
                    if fields[0] == "260" and (len(fields) < 4 or any(value not in ("0", "1") for value in fields[2:4])):
                        raise ValueError("Invalid combat state")
                    stamp = parsed.timestamp()
                except (UnicodeError, ValueError, OverflowError, OSError):
                    result.skipped += 1
                    continue
                if previous is not None and stamp < previous - 1:
                    meter.feed_lost()
                    recap.reset()
                    previous = None
                observed = max(stamp, previous) if previous is not None else stamp
                previous = observed
                if fields[0] == "260":
                    meter.set_in_combat(fields[2] == "1", fields[3] == "1")
                    if fields[2:4] == ["0", "0"]:
                        meter.finalize()
                else:
                    meter.process(fields, now=observed)
                recap.process(fields, now=observed)
                recap.deaths.clear()
        if cancel.is_set():
            result.close()
            return None
        result.records.sort(key=lambda death: death["when"], reverse=True)
        if progress:
            progress(100)
        return result
    except BaseException:
        result.close()
        raise


class LogImportJob(threading.Thread):
    def __init__(self, path):
        super().__init__(name="recap-log-import", daemon=True)
        self.path = path
        self.cancel = threading.Event()
        self.done = threading.Event()
        self.progress = 0
        self.result = None
        self.error = ""

    def run(self):
        try:
            self.result = read_log(self.path, self.cancel, self._progress)
        except Exception as exc:
            self.error = str(exc)
        finally:
            self.done.set()

    def _progress(self, percent):
        self.progress = percent
