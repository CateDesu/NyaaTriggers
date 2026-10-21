from dataclasses import dataclass, replace
from functools import lru_cache
import json
import math
import re
import uuid
import xml.etree.ElementTree as ET

from nyaatriggers import triggevent_custom as custom
from nyaatriggers.convert_triggernometry import _fields_pipe, _simple_field_limit, _split_top, expand_id_expr
from nyaatriggers.locale_util import _
from nyaatriggers.paths import bundle_root
from nyaatriggers.trigger_engine import Trigger, _safe_search, compile_user_regex
from nyaatriggers.triggernometry_editor import normalized_id, validate_pack, xml_bytes


@dataclass(frozen=True)
class PortableTrigger:
    name: str
    log_type: str
    ability_id: str
    tts_text: str
    fight: str = ""
    enabled: bool = True
    zone_regex: str = ""
    zone_id: int = 0
    cooldown_s: float = 0.0
    cooldown_scope: str = "trigger"
    delay_s: float = 0.0
    status_scope: str = "any"
    # Reviewed changes adopt the destination's reset and zone readiness behavior.
    origin: str = ""


class ConversionAdjustmentRequired(ValueError):
    def __init__(self, description, adjusted):
        super().__init__(description)
        self.description = description
        self.adjusted = adjusted


_EVENTS = {"20": "cast", "26": "status_gain", "30": "status_loss"}
_LOG_TYPES = {value: key for key, value in _EVENTS.items()}
_ABILITY_TYPES = {"20", "21", "22", "23"}
_STATUS_TYPES = {"26", "30"}
_TOKENS = re.compile(r"\{([^{}]*)\}")


def _ids(text):
    if not isinstance(text, str) or not text or len(text) > 512:
        raise ValueError(_("Conversion requires hexadecimal event IDs"))
    values = text.split("|")
    if not all(re.fullmatch(r"[0-9A-Fa-f]{1,8}", value.strip()) for value in values):
        raise ValueError(_("Conversion requires hexadecimal event IDs"))
    return "|".join(dict.fromkeys(value.strip().upper() for value in values))


def _types(text):
    kinds = tuple(dict.fromkeys(text.split("|")))
    if not kinds or not (set(kinds) <= _ABILITY_TYPES or set(kinds) <= _STATUS_TYPES):
        raise ValueError(_("This combination of log types cannot be converted"))
    return kinds


def _number(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValueError(_("Trigger timing must be a finite nonnegative number"))
    return value


def _validate(portable):
    _ids(portable.ability_id)
    _types(portable.log_type)
    for value in (portable.cooldown_s, portable.delay_s):
        _number(value)
    if not isinstance(portable.tts_text, str) or not portable.tts_text.strip():
        raise ValueError(_("Conversion requires spoken text"))
    if not isinstance(portable.name, str) or not portable.name.strip():
        raise ValueError(_("Conversion requires a trigger name"))
    if portable.cooldown_scope not in ("trigger", "source"):
        raise ValueError(_("Unsupported cooldown scope"))
    if portable.status_scope not in ("any", "self", "by_me"):
        raise ValueError(_("Unsupported status scope"))
    if type(portable.zone_id) is not int or not 0 <= portable.zone_id <= 0xFFFFFFFF:
        raise ValueError(_("Invalid zone ID"))


@lru_cache(maxsize=1)
def _zones():
    data = json.loads((bundle_root() / "assets" / "zone_names.json").read_text(encoding="utf-8"))
    return {int(ident): name for ident, name in data.items()}


def _zone_id(portable):
    if not portable.zone_regex:
        return portable.zone_id
    matcher = compile_user_regex(portable.zone_regex, re.IGNORECASE)
    if matcher is None:
        raise ValueError(_("The zone pattern cannot be converted"))
    matches = [ident for ident, name in _zones().items() if _safe_search(matcher, name)]
    if len(matches) != 1 or (portable.zone_id and portable.zone_id != matches[0]):
        raise ValueError(_("Conversion requires one known zone or no zone restriction"))
    return matches[0]


def _zone_regex(portable):
    if portable.zone_regex:
        return portable.zone_regex
    if not portable.zone_id:
        return ""
    name = _zones().get(portable.zone_id)
    if name is None:
        raise ValueError(_("The destination needs a known zone name for this zone ID"))
    return "^" + re.escape(name) + "$"


def _plain_tokens(text):
    tokens = set(_TOKENS.findall(text))
    if tokens - {"source", "target"}:
        raise ValueError(_("Only source and target text placeholders can be converted"))
    return tokens


def _review_semantics(portable, destination, zone_restricted):
    changes = []
    adjusted = portable
    if portable.cooldown_s:
        if destination == "local" and portable.origin == "triggernometry":
            changes.append(_("Local cooldowns reset after combat, zone and connection changes. Keep the cooldown with those resets?"))
        elif destination == "triggevent":
            changes.append(_("Triggevent has no additional cooldown for this callout. Convert without its cooldown?"))
            adjusted = replace(adjusted, cooldown_s=0.0)
        elif destination == "triggernometry":
            if portable.cooldown_scope == "source":
                changes.append(_("Triggernometry applies this cooldown to the whole trigger instead of each source and keeps it across fight resets. Convert with that cooldown?"))
                adjusted = replace(adjusted, cooldown_scope="trigger")
            elif portable.origin == "local":
                changes.append(_("Triggernometry cooldowns continue across fight resets and zone changes. Keep the cooldown with that behavior?"))
    if zone_restricted:
        if destination == "local" and portable.origin in ("triggernometry", "triggevent"):
            changes.append(_("Local callouts can run before zone metadata is known. Convert with that behavior?"))
        elif destination != "local" and portable.origin == "local":
            changes.append(_("The destination will wait for zone metadata before running this callout. Convert with that behavior?"))
    if changes:
        raise ConversionAdjustmentRequired("\n\n".join(changes), replace(adjusted, origin=destination))


def from_local(trigger):
    if (trigger.sequence or trigger.sound_file or trigger.interrupt or trigger.speed != 1
            or any((trigger.duration_min, trigger.duration_max, trigger.count_min,
                    trigger.count_max, trigger.expiry_warn_s))):
        raise ValueError(_("Sequences, sounds, speech options and status conditions cannot be converted"))
    portable = PortableTrigger(
        trigger.name, "|".join(part.strip() for part in trigger.log_type.split("|")),
        _ids(trigger.ability_id), trigger.tts_text, trigger.fight, trigger.enabled,
        trigger.zone_regex, cooldown_s=trigger.cooldown_s,
        cooldown_scope=trigger.cooldown_scope, delay_s=trigger.delay_s,
        status_scope=trigger.status_scope, origin="local")
    _validate(portable)
    _plain_tokens(portable.tts_text)
    return portable


def to_local(portable):
    _validate(portable)
    _plain_tokens(portable.tts_text)
    if portable.delay_s and portable.origin == "triggernometry":
        raise ValueError(_("Triggernometry delayed actions have different fight reset behavior"))
    trigger = Trigger(
        name=portable.name, log_type=portable.log_type, ability_id=_ids(portable.ability_id),
        tts_text=portable.tts_text, fight=portable.fight, enabled=portable.enabled,
        zone_regex=_zone_regex(portable), cooldown_s=portable.cooldown_s,
        cooldown_scope=portable.cooldown_scope, delay_s=portable.delay_s,
        status_scope=portable.status_scope)
    _review_semantics(portable, "local", bool(trigger.zone_regex))
    return trigger


def from_triggevent(definition):
    custom.validate_trigger(definition)
    start, steps = definition["start"], definition["steps"]
    if start["event"] not in _LOG_TYPES:
        raise ValueError(_("This Triggevent event has different matching behavior in the destination"))
    if (len(steps) != 1 or steps[0]["kind"] != "callout"
            or steps[0]["condition"] != "always" or steps[0].get("otherwise", "")):
        raise ValueError(_("Only one unconditional Triggevent callout can be converted"))
    kind = _LOG_TYPES[start["event"]]
    if start["target"] == "me" and kind not in _STATUS_TYPES:
        raise ValueError(_("Player-only cast matching cannot be converted"))
    text = steps[0]["text"].replace("{start.source}", "{source}").replace("{start.target}", "{target}")
    _plain_tokens(text)
    return PortableTrigger(
        definition["name"], kind, "|".join(f"{ident:02X}" for ident in sorted(custom.hex_ids(start["ids"]))),
        text, definition["fight"], zone_id=definition["zone_id"],
        status_scope="self" if start["target"] == "me" else "any", origin="triggevent")


def to_triggevent(portable):
    _validate(portable)
    if portable.log_type not in _EVENTS:
        raise ValueError(_("Triggevent ability events group targets differently from raw log triggers"))
    if portable.status_scope == "by_me" and portable.log_type in _STATUS_TYPES:
        raise ValueError(_("Triggevent cannot express this status source restriction"))
    if portable.delay_s:
        raise ValueError(_("Delayed callouts have different pending and fight reset behavior"))
    _plain_tokens(portable.tts_text)
    if any(value != f"{int(value, 16):02X}" for value in _ids(portable.ability_id).split("|")):
        raise ValueError(_("Triggevent numeric matching cannot preserve padded event IDs"))
    zone_id = _zone_id(portable)
    definition = custom.new_trigger(portable.fight, zone_id)
    definition["name"] = portable.name
    definition["start"] = {
        "event": _EVENTS[portable.log_type], "ids": _ids(portable.ability_id),
        "target": "me" if portable.log_type in _STATUS_TYPES and portable.status_scope == "self" else "any"}
    definition["steps"][0]["text"] = portable.tts_text
    custom.validate_trigger(definition)
    _review_semantics(portable, "triggevent", bool(zone_id))
    return definition


def _truth(element, name, default=True):
    value = element.get(name)
    if value is None:
        return default
    if value.strip().lower() not in ("true", "false"):
        raise ValueError(_("Unsupported {option} value").format(option=name))
    return value.strip().lower() == "true"


def _attributes(element, allowed, defaults=None):
    defaults = defaults or {}
    for name, value in element.attrib.items():
        if name in allowed:
            continue
        if name in defaults and value.strip().lower() == str(defaults[name]).lower():
            continue
        raise ValueError(_("Triggernometry option {option} cannot be converted").format(option=name))


def _network_pattern(portable):
    kinds = _types(portable.log_type)
    header = kinds[0] if len(kinds) == 1 else "(?:" + "|".join(kinds) + ")"
    status = kinds[0] in _STATUS_TYPES
    source, target, ident = (6, 8, 2) if status else (3, 7, 4)
    fields = [r"[^|]*"] * target
    ids = _ids(portable.ability_id)
    fields[ident - 1] = ids if "|" not in ids else "(?:" + ids + ")"
    fields[source - 1] = r"(?<source>[^|]*)"
    fields[target - 1] = r"(?<target>[^|]*)"
    if "23" in kinds:
        if "{target}" in portable.tts_text:
            raise ValueError(_("Cancel ability logs do not have a target field to convert"))
        fields = fields[:ident]
    return "(?i)^" + header + r"\|" + r"\|".join(fields) + r"\|"


def _network_matcher(pattern):
    ignore_case = pattern.startswith("(?i)")
    pattern = pattern.removeprefix("(?i)")
    match = re.fullmatch(r"\^(\d+|\(\?:\d+(?:\|\d+)+\))\\\|(.*)", pattern)
    if match is None:
        raise ValueError(_("Only finite ID patterns on network log fields can be converted"))
    header = match[1]
    kinds = _types(header[3:-1] if header.startswith("(?:") else header)
    fields = _fields_pipe(match[2])
    if len(fields) > _simple_field_limit("|".join(kinds), "|"):
        raise ValueError(_("Additional log field restrictions cannot be converted"))
    status = kinds[0] in _STATUS_TYPES
    source, target, ident = (5, 7, 1) if status else (2, None if "23" in kinds else 6, 3)
    if len(fields) <= ident + 1:
        raise ValueError(_("The ID pattern needs a complete field boundary"))
    expression = fields[ident]
    if len(_split_top(expression, "|")) != 1 or any(char.isspace() for char in expression):
        raise ValueError(_("The event ID pattern cannot be converted"))
    for group in re.finditer(r"\(\?(P?<([^>]+)>|'([^']+)')", expression):
        name = group[2] or group[3]
        if group[1].startswith("P") or re.fullmatch(r"\w+", name) is None or name in ("source", "target"):
            raise ValueError(_("The event ID pattern cannot be converted"))
    if not ignore_case and expression != expression.upper():
        raise ValueError(_("Case-sensitive lowercase event IDs cannot be converted"))
    literal = expression[3:-1] if expression.startswith("(?:") and expression.endswith(")") else expression
    try:
        ids = _ids(literal)
    except ValueError:
        expanded = expand_id_expr(expression, exact=True)
        if not expanded:
            raise ValueError(_("The event ID pattern cannot be converted")) from None
        ids = _ids("|".join(expanded))
    captures = set()
    for index, field in enumerate(fields):
        if index == ident or index == len(fields) - 1 and field in ("", ".*", ".*$"):
            continue
        capture = re.fullmatch(r"\(\?<([a-z]+)>\[\^\|\]\*\)", field)
        if capture and (capture[1], index) in (("source", source), ("target", target)):
            captures.add(capture[1])
        elif field != r"[^|]*":
            raise ValueError(_("Additional log field restrictions cannot be converted"))
    return "|".join(kinds), ids, captures


def from_triggernometry(root, trigger_id, *, fight="", text_overrides=None):
    root = getattr(root, "root", root)
    ident, separator, action_index = trigger_id.partition("#")
    ident = normalized_id(ident)
    if separator and action_index != "0":
        raise ValueError(_("Only a trigger with one speech action can be converted"))
    parents = {child: parent for parent in root.iter() for child in parent}
    matches = [trigger for trigger in root.iter("Trigger") if normalized_id(trigger.get("Id")) == normalized_id(ident)]
    if not normalized_id(ident) or len(matches) != 1:
        raise ValueError(_("The Triggernometry trigger is missing or ambiguous"))
    trigger = matches[0]
    _attributes(trigger, {"Id", "NyaaSourceId", "Name", "Enabled", "Source", "Sequential",
                          "RegularExpression", "RefirePeriodExpression", "PeriodRefire",
                          "Description", "DescriptionOverride", "DebugLevel"},
                {"PrevActions": "Keep", "PrevActionsRefire": "Allow", "Scheduling": "FromFire", "IsReadme": "False"})
    if trigger.get("Source", "Log") != "FFXIVNetwork":
        raise ValueError(_("Only network log Triggernometry callouts can be converted"))
    if len(trigger.findall("Actions")) != 1 or any(child.tag != "Actions" for child in trigger):
        raise ValueError(_("Triggernometry conditions and other actions cannot be converted"))
    actions = trigger.findall("Actions/Action")
    if (len(actions) != 1 or len(trigger.find("Actions")) != 1
            or actions[0].get("ActionType") != "UseTTS"):
        raise ValueError(_("Only a single Triggernometry speech action can be converted"))
    action = actions[0]
    if len(action):
        raise ValueError(_("Conditional Triggernometry speech cannot be converted"))
    _attributes(action, {"OrderNumber", "ActionType", "Enabled", "UseTTSTextExpression",
                         "ExecutionDelayExpression", "Description", "DescriptionOverride", "Asynchronous"},
                {"UseTTSVolumeExpression": "100", "UseTTSRateExpression": "0", "UseTTSExclusive": "False"})
    try:
        delay = float(action.get("ExecutionDelayExpression") or "0") / 1000
        cooldown = float(trigger.get("RefirePeriodExpression") or "0") / 1000
    except ValueError as exc:
        raise ValueError(_("Triggernometry timing expressions cannot be converted")) from exc
    if delay:
        raise ValueError(_("Triggernometry delayed actions have different fight reset behavior"))
    if trigger.get("PeriodRefire", "Allow") == "Allow":
        cooldown = 0.0
    elif trigger.get("PeriodRefire") != "Deny":
        raise ValueError(_("Unsupported Triggernometry refire behavior"))
    log_type, ids, captures = _network_matcher(trigger.get("RegularExpression", ""))
    text = (text_overrides or {}).get(ident + "#0", action.get("UseTTSTextExpression", ""))
    if re.search(r"(?<!\$)\{(?:source|target)\}", text):
        raise ValueError(_("Literal source and target placeholder text cannot be preserved"))
    for token in re.findall(r"\$\{([^{}]*)\}", text):
        if token not in captures:
            raise ValueError(_("Triggernometry variables and expressions cannot be converted"))
    text = re.sub(r"\$\{(source|target)\}", r"{\1}", text)
    if "${" in text:
        raise ValueError(_("Triggernometry variables and expressions cannot be converted"))
    _plain_tokens(text)
    enabled = _truth(trigger, "Enabled", False) and _truth(action, "Enabled")
    zone_id = 0
    node = parents.get(trigger)
    while node is not None:
        if node.tag in ("Folder", "ExportedFolder"):
            enabled = enabled and _truth(node, "Enabled")
            for flag in ("ZoneFilterEnabled", "EventFilterEnabled", "FFXIVJobFilterEnabled"):
                if _truth(node, flag, False):
                    raise ValueError(_("Triggernometry folder restrictions cannot be converted"))
            if _truth(node, "FFXIVZoneFilterEnabled", False):
                match = re.fullmatch(r"\^(\d+)\$", node.get("FfxivZoneFilterRegularExpression", ""))
                if match is None or not int(match[1]) or zone_id and zone_id != int(match[1]):
                    raise ValueError(_("Conversion requires one numeric Triggernometry zone"))
                zone_id = int(match[1])
        node = parents.get(node)
    portable = PortableTrigger(trigger.get("Name", ""), log_type, ids, text, fight, enabled,
                               zone_id=zone_id, cooldown_s=cooldown, origin="triggernometry")
    _validate(portable)
    return portable


def to_triggernometry(portable):
    _validate(portable)
    _plain_tokens(portable.tts_text)
    if set(_types(portable.log_type)) <= _STATUS_TYPES and portable.status_scope != "any":
        raise ValueError(_("Player-only status restrictions cannot be converted to a static Triggernometry pattern"))
    if portable.delay_s:
        raise ValueError(_("Triggernometry delayed actions have different fight reset behavior"))
    if "${" in portable.tts_text:
        raise ValueError(_("Literal Triggernometry expression syntax cannot be converted safely"))
    zone_id = _zone_id(portable)
    root = ET.Element("TriggernometryExport", Version="1")
    folder = ET.SubElement(root, "ExportedFolder", Id=str(uuid.uuid4()), Name=portable.fight or "Unsorted", Enabled="true")
    if zone_id:
        folder.set("FFXIVZoneFilterEnabled", "true")
        folder.set("FfxivZoneFilterRegularExpression", "^" + str(zone_id) + "$")
    triggers = ET.SubElement(folder, "Triggers")
    trigger = ET.SubElement(triggers, "Trigger", Id=str(uuid.uuid4()), Name=portable.name,
                            Enabled=str(bool(portable.enabled)).lower(), Source="FFXIVNetwork",
                            RegularExpression=_network_pattern(portable))
    if portable.cooldown_s:
        trigger.set("PeriodRefire", "Deny")
        trigger.set("RefirePeriodExpression", str(portable.cooldown_s * 1000))
    actions = ET.SubElement(trigger, "Actions")
    text = portable.tts_text.replace("{source}", "${source}").replace("{target}", "${target}")
    ET.SubElement(actions, "Action", OrderNumber="1", ActionType="UseTTS", UseTTSTextExpression=text)
    validate_pack(root)
    xml_bytes(root)
    _review_semantics(portable, "triggernometry", bool(zone_id))
    return root
