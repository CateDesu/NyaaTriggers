"""Order owned combatant replies before passing them to native engines."""

from collections import OrderedDict
import base64
import json

_MAX_PARTIAL_ROWS = 4096
_MAX_PARTIAL_BYTES = 8 << 20
_KINDS = {"allCombatants", "specificCombatants"}


def request_tag(kind, sequence, zone=None):
    tag = f"nyaa:{kind}:{sequence}"
    return tag if zone is None else f"{tag}:{zone}"


def _owned_tag(value):
    if not isinstance(value, str):
        return None
    parts = value.split(":")
    if (len(parts) not in (3, 4) or parts[0] != "nyaa" or parts[1] not in _KINDS
            or not parts[2].isascii() or not parts[2].isdigit() or len(parts[2]) > 20):
        return None
    zone = parts[3] if len(parts) == 4 else None
    if zone is not None and (len(zone) > 256 or any(
            not (char.isascii() and (char.isalnum() or char in "-_")) for char in zone)):
        return None
    sequence = int(parts[2])
    return (parts[1], sequence, zone) if sequence > 0 else None


def extract_raw(data):
    """Extract a raw ACT log line, or an empty string for unrelated messages."""
    kind = str(data.get("type", "")).lower()
    if kind == "logline":
        line = data.get("line")
        raw = data.get("rawLine") or data.get("raw_line")
        if raw:
            return str(raw)
        return "|".join(str(field) for field in line) if isinstance(line, list) else ""
    if kind == "broadcast" and str(data.get("msgtype", "")).lower() == "logline":
        return str(data.get("msg", "")).strip()
    return ""


def _actor_id(row):
    if not isinstance(row, dict):
        return None
    try:
        return int(row.get("ID", row.get("id")))
    except (TypeError, ValueError, OverflowError):
        return None


class CombatantResponses:
    def __init__(self):
        self.reset()

    def reset(self, sequence=0):
        self._full_sequence = sequence
        self._floor = sequence
        self._partials = OrderedDict()
        self._bytes = 0
        self.zone_token = None

    def observe_raw(self, raw, sequence=0):
        fields = raw.split("|", 4)
        if (len(fields) <= 3 or fields[0] != "01" or not fields[1].isascii()
                or len(fields[1]) > 64
                or len(fields[2]) > 8):
            return
        try:
            zone = int(fields[2], 16)
        except ValueError:
            return
        if not 0 <= zone <= 0xFFFFFFFF:
            return
        boundary = f"{fields[1]}|{zone}".encode("utf-8")
        self.zone_token = base64.urlsafe_b64encode(boundary).decode("ascii").rstrip("=")
        self._floor = max(self._floor, self._full_sequence, sequence,
                          max((row[0] for row in self._partials.values()), default=0))
        self._partials.clear()
        self._bytes = 0

    def accept(self, data, sequence=0):
        """Return a canonical reply, the unchanged message, or None for a stale reply."""
        self.observe_raw(extract_raw(data), sequence)
        tag = _owned_tag(data.get("rseq"))
        rows = data.get("combatants")
        if tag is None or not isinstance(rows, list):
            return data
        kind, sequence, zone = tag
        if zone is not None and self.zone_token is not None and zone != self.zone_token:
            return None
        if sequence <= max(self._full_sequence, self._floor):
            return None
        if kind == "allCombatants":
            newer = {ident: row for ident, (version, row, _size) in self._partials.items()
                     if version > sequence}
            accepted = [newer.pop(_actor_id(row), row) for row in rows]
            accepted.extend(newer.values())
            self._full_sequence = sequence
            self._discard_through(sequence)
        else:
            accepted = []
            for row in rows:
                ident = _actor_id(row)
                if ident is not None:
                    previous = self._partials.get(ident)
                    if previous is not None and previous[0] >= sequence:
                        continue
                    size = len(json.dumps(row))
                    if previous is not None:
                        self._bytes -= previous[2]
                    self._partials[ident] = (sequence, row, size)
                    self._partials.move_to_end(ident)
                    self._bytes += size
                accepted.append(row)
            if not accepted:
                return None
            while (len(self._partials) > _MAX_PARTIAL_ROWS
                   or self._bytes > _MAX_PARTIAL_BYTES):
                # Forgetting rows also retires replies that could overwrite them.
                self._floor = max(self._floor, next(iter(self._partials.values()))[0])
                self._discard_through(self._floor)
        return {**data, "rseq": kind, "combatants": accepted}

    def _discard_through(self, sequence):
        for ident, (version, _row, size) in list(self._partials.items()):
            if version <= sequence:
                del self._partials[ident]
                self._bytes -= size

    def normalize_line(self, raw):
        try:
            data = json.loads(raw)
        except (ValueError, RecursionError):
            self.observe_raw(raw.strip())
            return raw
        if not isinstance(data, dict):
            return raw
        accepted = self.accept(data)
        if accepted is None:
            return None
        return raw if accepted is data else json.dumps(accepted)
