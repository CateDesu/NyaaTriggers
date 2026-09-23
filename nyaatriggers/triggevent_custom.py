"""Saved definitions for the Triggevent callout builder."""

import json
import re
import uuid

from nyaatriggers.paths import bundle_root, data_root
from nyaatriggers.locale_util import _


CUSTOM_FILE = data_root() / "triggevent.custom.json"
EVENTS = ("cast", "ability", "status_gain", "status_loss", "headmarker", "tether")
TARGETS = ("any", "me", "start_target", "start_source")
CONDITIONS = ("always", "start_id", "event_id", "my_status", "no_my_status")
TOKENS = {"source", "target", "start.source", "start.target", "id", "start.id", "player"}
MAX_TRIGGERS = 200
MAX_STEPS = 32
MAX_BYTES = 4 << 20


def fight_choices():
    """Match bundled fight patterns to the bundled zone names."""
    assets = bundle_root() / "assets"
    try:
        triggers = json.loads((assets / "triggers.json").read_text(encoding="utf-8"))
        zones = json.loads((assets / "zone_names.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return []
    if not isinstance(triggers, list) or not isinstance(zones, dict):
        return []
    patterns = {(row["fight"], row["zone_regex"]) for row in triggers
                if isinstance(row, dict) and isinstance(row.get("fight"), str) and row["fight"]
                and isinstance(row.get("zone_regex"), str) and row["zone_regex"]}
    choices = {}
    for fight, pattern in sorted(patterns):
        try:
            matcher = re.compile(pattern, re.IGNORECASE)
        except re.error:
            continue
        for zone_id, name in zones.items():
            if (isinstance(name, str) and str(zone_id).isascii() and str(zone_id).isdecimal()
                    and 0 < int(zone_id) <= 0xFFFFFFFF and matcher.search(name)):
                choices[fight, int(zone_id)] = {
                    "fight": fight, "zone_id": int(zone_id), "zone_name": name,
                    "label": f"{fight} · {name} · {zone_id}",
                }
    return sorted(choices.values(), key=lambda row: (row["fight"].casefold(), row["zone_id"]))


def new_trigger(fight="", zone_id=0):
    return {
        "id": "nyaa:" + str(uuid.uuid4()), "name": "", "fight": fight,
        "zone_id": zone_id, "timeout_s": 120.0,
        "start": {"event": "status_gain", "ids": "", "target": "me"},
        "steps": [{"kind": "callout", "condition": "always", "ids": "",
                   "text": "", "otherwise": ""}],
    }


def hex_ids(text):
    if not isinstance(text, str) or len(text) > 512:
        raise ValueError(_("Enter hexadecimal IDs separated by |"))
    parts = text.split("|")
    if not all(re.fullmatch(r"(?:0[xX])?[0-9a-fA-F]{1,8}", p.strip()) for p in parts):
        raise ValueError(_("Enter hexadecimal IDs separated by |"))
    return {int(p.strip(), 16) for p in parts}


def _number(value, minimum, maximum):
    if type(value) not in (int, float) or not minimum <= value <= maximum:
        raise ValueError(_("Enter a number from {minimum} to {maximum}").format(minimum=minimum, maximum=maximum))


def _text(value, limit=200):
    if not isinstance(value, str) or len(value.encode("utf-16-le")) > limit * 2:
        raise ValueError(_("Text must be at most {limit} characters").format(limit=limit))


def _matcher(data, start=False):
    if not isinstance(data, dict) or data.get("event") not in EVENTS:
        raise ValueError(_("Choose a supported event"))
    hex_ids(data.get("ids"))
    if data.get("target") not in (TARGETS[:2] if start else TARGETS):
        raise ValueError(_("Choose a target"))


def _callout(text):
    _text(text, 2000)
    for token in re.findall(r"\{([^{}]*)\}", text):
        if token not in TOKENS:
            raise ValueError(_("Unknown callout placeholder: {token}").format(token="{" + token + "}"))
    if "{" in re.sub(r"\{[^{}]*\}", "", text) or "}" in re.sub(r"\{[^{}]*\}", "", text):
        raise ValueError(_("Close each callout placeholder with }"))


def validate_trigger(data):
    if not isinstance(data, dict):
        raise ValueError(_("Invalid Triggevent callout"))
    ident = data.get("id", "")
    if not isinstance(ident, str) or not re.fullmatch(r"nyaa:[0-9a-f-]{36}", ident):
        raise ValueError(_("Invalid callout ID"))
    for key in ("name", "fight"):
        _text(data.get(key))
    if not data["name"].strip():
        raise ValueError(_("Enter a callout name"))
    if type(data.get("zone_id")) is not int or not 0 <= data["zone_id"] <= 0xFFFFFFFF:
        raise ValueError(_("Enter a decimal zone ID or 0 for any zone"))
    _number(data.get("timeout_s"), 1, 600)
    _matcher(data.get("start"), start=True)
    steps = data.get("steps")
    if not isinstance(steps, list) or not 1 <= len(steps) <= MAX_STEPS:
        raise ValueError(_("Add between 1 and {maximum} steps").format(maximum=MAX_STEPS))
    has_callout = False
    for step in steps:
        if not isinstance(step, dict):
            raise ValueError(_("Invalid callout step"))
        kind = step.get("kind")
        if kind == "wait":
            _matcher(step)
        elif kind == "delay":
            _number(step.get("seconds"), 0.1, 600)
        elif kind == "callout":
            has_callout = True
            condition = step.get("condition")
            if condition not in CONDITIONS:
                raise ValueError(_("Choose a callout condition"))
            if condition == "always":
                _text(step.get("ids", ""), 512)
            else:
                hex_ids(step.get("ids"))
            _callout(step.get("text"))
            _callout(step.get("otherwise"))
            if not step["text"].strip() and (condition == "always" or not step["otherwise"].strip()):
                raise ValueError(_("Enter spoken text for the callout"))
        else:
            raise ValueError(_("Choose a supported step"))
    if not has_callout:
        raise ValueError(_("Add a callout step"))
    delays_ms = sum(int(s["seconds"] * 1000) for s in steps if s["kind"] == "delay")
    if delays_ms >= int(data["timeout_s"] * 1000):
        raise ValueError(_("The total timeout must exceed the delays"))


def validate_document(data):
    if (not isinstance(data, dict) or type(data.get("version")) is not int
            or data["version"] != 1):
        raise ValueError(_("Unsupported Triggevent callout file"))
    rows = data.get("triggers")
    if not isinstance(rows, list) or len(rows) > MAX_TRIGGERS:
        raise ValueError(_("Too many Triggevent callouts"))
    seen = set()
    for row in rows:
        validate_trigger(row)
        if row["id"] in seen:
            raise ValueError(_("Duplicate Triggevent callout ID"))
        seen.add(row["id"])
    return rows


def load_triggers(path=None):
    path = path or CUSTOM_FILE
    try:
        with path.open("rb") as stream:
            raw = stream.read(MAX_BYTES + 1)
    except FileNotFoundError:
        return []
    if len(raw) > MAX_BYTES:
        raise ValueError(_("Triggevent callout file is too large"))
    return validate_document(json.loads(raw))


def save_triggers(rows, path=None):
    from nyaatriggers.app_common import _atomic_write_bytes
    path = path or CUSTOM_FILE
    load_triggers(path)
    data = {"version": 1, "triggers": rows}
    validate_document(data)
    payload = json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8")
    if len(payload) > MAX_BYTES:
        raise ValueError(_("Triggevent callout file is too large"))
    _atomic_write_bytes(path, payload)
