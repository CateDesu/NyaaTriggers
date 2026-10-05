from copy import deepcopy
from pathlib import Path

from nyaatriggers.death_recap import MAX_EVENTS, MAX_STATUSES, WINDOW_SECONDS
from nyaatriggers.record_store import read_record, record_id, write_record

EVENT_KINDS = {"damage", "heal", "dot", "hot", "gained", "lost", "instant-death", "health"}
MAX_PENDING_RECAPS = 256


def optional_number(entry, key, maximum=0xFFFFFFFF, integer=True):
    value = entry.get(key)
    if value is None:
        return
    types = (int,) if integer else (int, float)
    if type(value) not in types or not 0 <= value <= maximum:
        raise ValueError(f"Invalid observation {key}")


def validate_statuses(statuses):
    if not isinstance(statuses, list) or len(statuses) > MAX_STATUSES:
        raise ValueError("Invalid recap statuses")
    for status in statuses:
        if not isinstance(status, dict):
            raise ValueError("Invalid recap status")
        for key in ("name", "source"):
            if not isinstance(status.get(key), str) or len(status[key]) > 200:
                raise ValueError("Invalid status text")
        for key in ("id", "source_id"):
            optional_number(status, key)
        optional_number(status, "stacks", 65535)
        optional_number(status, "remaining", 1e12, integer=False)


def validate_recap(data):
    for key in ("id", "session_id", "pull_id"):
        record_id(data.get(key))
    if type(data.get("actor")) is not int or not 0x10000000 <= data["actor"] < 0x11000000:
        raise ValueError("Invalid recap actor")
    if type(data.get("when")) not in (int, float) or not 0 <= data["when"] <= 253402214400:
        raise ValueError("Invalid recap date")
    for key in ("name", "zone"):
        if not isinstance(data.get(key), str) or len(data[key]) > 200:
            raise ValueError("Invalid recap text")
    for key, limit in (("events", MAX_EVENTS), ("statuses", MAX_STATUSES)):
        if not isinstance(data.get(key), list) or len(data[key]) > limit:
            raise ValueError("Invalid recap observations")
        for entry in data[key]:
            if not isinstance(entry, dict):
                raise ValueError("Invalid recap observation")
            for field in ("name", "source"):
                if not isinstance(entry.get(field), str) or len(entry[field]) > 200:
                    raise ValueError("Invalid observation text")
    for event in data["events"]:
        if not isinstance(event.get("kind"), str) or event["kind"] not in EVENT_KINDS:
            raise ValueError("Invalid recap event")
        if type(event.get("time")) not in (int, float) or not -WINDOW_SECONDS <= event["time"] <= 0:
            raise ValueError("Invalid event time")
        if "amount" not in event:
            raise ValueError("Missing event amount")
        amount = event["amount"]
        if amount is not None and (type(amount) is not int or not 0 <= amount <= 0xFFFFFFFF):
            raise ValueError("Invalid event amount")
        for key in ("action_id", "status_id", "sequence", "hp", "max_hp", "hp_after"):
            optional_number(event, key)
        for key in ("shield", "shield_after"):
            optional_number(event, key, 255)
        optional_number(event, "status_stacks", 65535)
        optional_number(event, "status_duration", 1e12, integer=False)
        optional_number(event, "damage_type", 15)
        for key in ("critical", "direct_hit", "blocked", "parried"):
            if key in event and type(event[key]) is not bool:
                raise ValueError("Invalid combat flag")
        for key in ("statuses", "source_statuses"):
            if key in event:
                validate_statuses(event[key])
    validate_statuses(data["statuses"])


class RecapStore:
    def __init__(self, directory, writer=None):
        self.directory = Path(directory)
        self.writer = writer
        self._saving = {}
        self.unsaved = {}
        self.errors = {}
        self.dropped = 0

    @property
    def save_error(self):
        return "\n".join(list(self.errors.values())[:3])

    def _directory(self, session_id, pull_id):
        return self.directory / record_id(session_id) / record_id(pull_id)

    def record(self, session_id, pull_id, death):
        data = deepcopy(death)
        data.update(version=1, session_id=session_id, pull_id=pull_id)
        data["statuses"] = [{k: v for k, v in status.items() if k not in ("expires", "bank")}
                            for status in data["statuses"]]
        validate_recap(data)
        self.save(data)
        return data

    def save(self, data):
        ident = data["id"]
        token = self._saving[ident] = object()
        try:
            validate_recap(data)
            directory = self._directory(data["session_id"], data["pull_id"])
            if self.writer is not None:
                self.writer.submit(directory, data, write_record,
                                   lambda error: self._saved(data, error, token))
                self.unsaved[ident] = data
                return True
            write_record(directory, data)
        except (OSError, ValueError) as exc:
            return self._saved(data, exc, token)
        return self._saved(data, None, token)

    def _saved(self, data, error, token):
        ident = data["id"]
        if self._saving.get(ident) is not token:
            return False
        del self._saving[ident]
        if error is not None:
            self.unsaved[ident] = data
            self.errors[ident] = str(error)
            while len(self.unsaved) > MAX_PENDING_RECAPS:
                oldest = next((key for key in self.unsaved if key not in self._saving), None)
                if oldest is None:
                    break
                del self.unsaved[oldest]
                self.errors.pop(oldest, None)
                self.dropped += 1
            return False
        self.unsaved.pop(ident, None)
        self.errors.pop(ident, None)
        return True

    def flush_pending(self):
        for data in list(self.unsaved.values()):
            if data["id"] not in self._saving:
                self.save(data)

    def load(self, session_id, pull_id):
        directory = self._directory(session_id, pull_id)
        records, errors = {}, []
        try:
            paths = sorted(directory.iterdir())
        except FileNotFoundError:
            paths = []
        except OSError as exc:
            paths = []
            errors.append(str(exc))
        for path in paths:
            if path.suffix != ".json":
                continue
            try:
                data = read_record(path)
                validate_recap(data)
                if (data["session_id"], data["pull_id"]) != (session_id, pull_id):
                    raise ValueError("Recap belongs to another pull")
                records[data["id"]] = data
            except (OSError, ValueError, TypeError, KeyError, RecursionError, OverflowError) as exc:
                errors.append(f"{path.name}: {exc}")
        for data in self.unsaved.values():
            if (data["session_id"], data["pull_id"]) == (session_id, pull_id):
                records[data["id"]] = deepcopy(data)
        return sorted(records.values(), key=lambda d: (d["when"], d["id"]), reverse=True), errors
