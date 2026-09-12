"""Named snapshots of trigger choices and editable callout text."""

from copy import deepcopy
from uuid import uuid4

SOURCES = ("cactbot", "triggevent", "triggernometry")


def validate_profile(data):
    if not isinstance(data.get("name"), str) or not data["name"].strip() or len(data["name"]) > 200:
        raise ValueError("Invalid profile name")
    if not isinstance(data.get("local"), dict) or not isinstance(data.get("engines"), dict):
        raise ValueError("Invalid profile choices")
    mappings = [data["local"]]
    if set(data["engines"]) - set(SOURCES):
        raise ValueError("Unknown trigger source")
    mappings.extend(data["engines"].values())
    for mapping in mappings:
        if not isinstance(mapping, dict):
            raise ValueError("Invalid trigger choices")
        for ident, choice in mapping.items():
            if not isinstance(ident, str) or not ident or not isinstance(choice, dict):
                raise ValueError("Invalid trigger choice")
            if type(choice.get("enabled")) is not bool:
                raise ValueError("Invalid trigger toggle")
            if choice.get("text") is not None and not isinstance(choice["text"], str):
                raise ValueError("Invalid callout text")
    for choice in data["local"].values():
        if not isinstance(choice.get("text"), str):
            raise ValueError("Missing local callout text")


def capture_profile(window, name, ident=None):
    data = {"version": 1, "id": ident or str(uuid4()), "name": name.strip(),
            "local": {t.id: {"enabled": t.enabled, "text": t.tts_text} for t in window._triggers},
            "engines": {}}
    for source in SOURCES:
        edits = getattr(window, f"_{source}_callout_edits", {})
        ids = set(window._engine_seen.get(source, set())) | set(window._engine_disabled[source]) | set(edits)
        ids.update(e["id"] for e in window._engine_inventory if e.get("source") == source)
        data["engines"][source] = {ident: {"enabled": ident not in window._engine_disabled[source],
                                            "text": edits.get(ident)} for ident in ids}
    validate_profile(data)
    return deepcopy(data)


def apply_choices(window, profile):
    validate_profile(profile)
    for trigger in window._triggers:
        choice = profile["local"].get(trigger.id)
        if choice is not None:
            if (trigger.enabled, trigger.tts_text) != (choice["enabled"], choice["text"]):
                trigger.enabled = choice["enabled"]
                trigger.tts_text = choice["text"]
                window._local_ids.add(trigger.id)
    for source, choices in profile["engines"].items():
        disabled = window._engine_disabled[source]
        edits = getattr(window, f"_{source}_callout_edits", None)
        for ident, choice in choices.items():
            if choice["enabled"]:
                disabled.discard(ident)
            else:
                disabled.add(ident)
            if edits is not None:
                if choice.get("text") is None:
                    edits.pop(ident, None)
                else:
                    edits[ident] = choice["text"]
        window._settings[f"{source}_disabled_triggers"] = sorted(disabled)
        if edits is not None:
            window._settings[f"{source}_callout_edits"] = edits
