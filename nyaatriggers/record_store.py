import json
import os
import tempfile
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
from queue import SimpleQueue, Empty
import threading
from uuid import UUID

MAX_BYTES = 8 << 20


class RecordWriter:
    """Write snapshots in order and deliver completion on the polling thread."""

    def __init__(self):
        self._lock = threading.Lock()
        self._pending = deque()
        self._latest = {}
        self._completed = SimpleQueue()
        self._executor = None
        self._future = None
        self._running = False

    def submit(self, directory, data, write, completed):
        key = Path(directory), record_id(data.get("id"))
        token = object()
        job = (deepcopy(data), write, completed, token)
        with self._lock:
            replace = bool(self._pending and self._pending[-1][0] == key)
            if ((key not in self._latest and len(self._latest) >= 256)
                    or (not replace and len(self._pending) >= 256)):
                raise OSError("Session writer queue is full")
            if self._executor is None:
                self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="session-writer")
            if not self._running:
                try:
                    self._future = self._executor.submit(self._run)
                except RuntimeError as exc:
                    self._executor.shutdown(wait=False, cancel_futures=True)
                    self._executor = None
                    self._future = None
                    raise OSError(f"Session writer could not start: {exc}") from exc
                self._running = True
            self._latest[key] = token
            # Only adjacent snapshots can be replaced without reordering records.
            if replace:
                self._pending.pop()
            self._pending.append((key, job))

    def _run(self):
        while True:
            with self._lock:
                if not self._pending:
                    self._running = False
                    return
                key, job = self._pending.popleft()
            data, write, completed, token = job
            error = None
            try:
                write(key[0], data)
            except Exception as exc:
                error = str(exc)
            self._completed.put((key, completed, token, error))

    def poll(self, *, wait=False):
        if wait and self._future is not None:
            self._future.result()
        while True:
            try:
                key, completed, token, error = self._completed.get_nowait()
            except Empty:
                return
            with self._lock:
                if self._latest.get(key) is not token:
                    continue
                del self._latest[key]
            completed(error)

    def close(self):
        self.poll(wait=True)
        if self._executor is not None:
            self._executor.shutdown(wait=True)
            self._executor = None


def record_id(value):
    if not isinstance(value, str) or str(UUID(value)) != value:
        raise ValueError("Invalid record ID")
    return value


def read_record(path):
    with Path(path).open("rb") as stream:
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("Record is too large")
    data = json.loads(raw)
    if not isinstance(data, dict) or type(data.get("version")) is not int or data["version"] != 1:
        raise ValueError("Unsupported record format")
    record_id(data.get("id"))
    if Path(path).stem != data["id"]:
        raise ValueError("Record ID does not match its filename")
    return data


def write_record(directory, data):
    directory = Path(directory)
    destination = directory / (record_id(data.get("id")) + ".json")
    raw = json.dumps(data, ensure_ascii=False, allow_nan=False, indent=2).encode("utf-8")
    if len(raw) > MAX_BYTES:
        raise ValueError("Record is too large")
    directory.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=directory, prefix=".record-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def load_records(directory, validate):
    records, errors = [], []
    try:
        paths = sorted(Path(directory).iterdir())
    except FileNotFoundError:
        return records, errors
    except OSError as exc:
        return records, [f"{directory}: {exc}"]
    for path in paths:
        if path.suffix != ".json":
            continue
        try:
            data = read_record(path)
            validate(data)
            records.append(data)
        except (OSError, ValueError, TypeError, KeyError, RecursionError, OverflowError) as exc:
            errors.append(f"{path.name}: {exc}")
    return records, errors
