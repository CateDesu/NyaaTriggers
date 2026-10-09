from typing import NamedTuple

from nyaatriggers.dps_meter import _unpack_effect
from nyaatriggers.prog_phases import definition_for, read_tracking, seconds


class PhaseProgress(NamedTuple):
    phase: str
    first_reached: float | None
    best_hp_percent: float | None


def wire_number(value, base=10):
    if not isinstance(value, str) or not value or value.strip() != value:
        return None
    try:
        number = int(value, base)
    except ValueError:
        return None
    return number if 0 <= number <= 0xFFFFFFFF else None


def hp_samples(fields):
    if not fields:
        return ()
    kind = fields[0]
    if kind in ("37", "38", "39", "43"):
        indexes = ((2, 4 if kind == "39" else 5),)
    elif kind in ("21", "22"):
        indexes = ((6, 24), (2, 34))
    elif kind == "24" and len(fields) > 4 and fields[4] in ("DoT", "HoT"):
        indexes = ((2, 7),)
    else:
        return ()
    return tuple((wire_number(fields[actor], 16), wire_number(fields[offset]),
                  wire_number(fields[offset + 1]), fields[offset + 1] == "")
                 for actor, offset in indexes if len(fields) > max(actor, offset + 1))


def player_damage_target(fields):
    if not fields or fields[0] not in ("21", "22") or len(fields) < 24:
        return None
    source = wire_number(fields[2], 16)
    if source is None or not 0x10000000 <= source < 0x11000000:
        return None
    reflected = False
    for offset in range(8, 24, 2):
        flags = wire_number(fields[offset], 16)
        if flags is None:
            continue
        if flags & 0xFF == 0x1D:
            reflected = True
        kind, amount, _, _ = _unpack_effect(fields[offset], fields[offset + 1])
        if kind == "damage" and amount > 0 and not reflected:
            return wire_number(fields[6], 16)
    return None


def phase_progress(sessions, zone_id, definitions):
    definition = definition_for(zone_id, definitions)
    if definition is None:
        return ()
    dates = {}
    health = {}
    for session in sessions:
        if session["zone_id"] != zone_id:
            continue
        for pull in session["pulls"]:
            data, saved_definition, error = read_tracking(pull, zone_id, definitions)
            if error or saved_definition is not definition:
                continue
            observed = {o["phase"]: o for o in data["observations"]}
            for observation in data["observations"]:
                phase = observation["phase"]
                reached = observation.get("observed_at")
                if "observed_at" not in observation and definition.continuous_combat and not definition.transitions:
                    reached = pull["started"] + observation["at"]
                if seconds(reached):
                    dates[phase] = min(dates.get(phase, reached), reached)
            saved_hp = data.get("boss_hp", {})
            if not isinstance(saved_hp, dict):
                continue
            for phase, sample in saved_hp.items():
                if phase not in observed or not isinstance(sample, dict):
                    continue
                current, maximum = sample.get("current"), sample.get("maximum")
                npc_id = sample.get("npc_id")
                caster_name = dict(definition.caster_bosses).get(phase)
                actor = sample.get("actor")
                caster_confirmed = (caster_name is not None
                                    and type(actor) is int and 0x40000000 <= actor < 0x50000000
                                    and actor == observed[phase].get("actor")
                                    and sample.get("npc_name_id") == caster_name
                                    and sample.get("target_confirmed") is True)
                if (type(current) is not int or type(maximum) is not int
                        or not 0 <= current <= maximum <= 0xFFFFFFFF or maximum == 0
                        or type(npc_id) is not int or not 0 < npc_id <= 0xFFFFFFFF
                        or not (npc_id in dict(definition.bosses).get(phase, ()) or caster_confirmed)):
                    continue
                percent = 100 * current / maximum
                health[phase] = min(health.get(phase, percent), percent)
    return tuple(PhaseProgress(phase, dates.get(phase), health.get(phase)) for phase in definition.phases)
