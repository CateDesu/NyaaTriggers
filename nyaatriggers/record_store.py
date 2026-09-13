"""Local JSON records for sessions and trigger profiles."""

import json
import os
import tempfile
from pathlib import Path
from uuid import UUID

MAX_BYTES = 8 << 20


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
    for path in sorted(Path(directory).glob("*.json")):
        try:
            data = read_record(path)
            validate(data)
            records.append(data)
        except (OSError, ValueError, TypeError, KeyError, RecursionError, OverflowError) as exc:
            errors.append(f"{path.name}: {exc}")
    return records, errors
