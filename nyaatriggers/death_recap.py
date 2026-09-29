"""Bounded death history from the observed combat feed."""

from collections import OrderedDict, deque
from copy import deepcopy
import math
import struct
import time
from uuid import uuid4

from nyaatriggers.dps_meter import DEATH_DUPLICATE_SECONDS, _actor_int, _unpack_effect
from nyaatriggers.status_metadata import is_permanent

WINDOW_SECONDS = 60
MAX_DEATHS = 80
MAX_ACTORS = 128
MAX_EVENTS = 256
MAX_STATUSES = 64
SOURCE_MITIGATION = frozenset((1203, 1195, 1193, 860, 1715, 2115, 3642))


def wire_int(value, base=10, maximum=0xFFFFFFFF):
    try:
        number = int(value, base)
    except (ValueError, TypeError):
        return None
    return number if 0 <= number <= maximum else None


def status_snapshot(statuses, now):
    return [{k: v for k, v in status.items() if k not in ("expires", "bank")} |
            {"remaining": round(max(0, status["expires"] - now), 3)
             if status["expires"] is not None else None}
            for status in statuses.values()]


def player_id(value):
    actor = _actor_int(value)
    return actor if actor is not None and 0x10000000 <= actor < 0x11000000 else None


class DeathRecap:
    def __init__(self, clock=None):
        self.clock = clock or time.monotonic
        self.deaths = deque(maxlen=MAX_DEATHS)
        self.buffers = OrderedDict()
        self.sources = OrderedDict()
        self.source_preparation = OrderedDict()
        # Late deaths still need the old history while the next pull is prepared.
        self.preparation = OrderedDict()
        self.zone = ""
        self.on_death = None
        self.reset_on_pull = False

    def reset(self):
        self.buffers.clear()
        self.preparation.clear()
        self.sources.clear()
        self.source_preparation.clear()
        self.reset_on_pull = False

    def end_pull(self):
        self.reset_on_pull = True

    def begin_pull(self):
        if self.reset_on_pull:
            self.buffers = self.preparation
            self.preparation = OrderedDict()
            self.sources = self.source_preparation
            self.source_preparation = OrderedDict()
            self.reset_on_pull = False

    def _buffer(self, actor, now, preparing=False):
        buffers = self.preparation if preparing else self.buffers
        if player_id(actor) is None:
            buffers = self.source_preparation if preparing else self.sources
        if actor not in buffers:
            buffers[actor] = {"events": deque(maxlen=MAX_EVENTS),
                              "statuses": OrderedDict(), "death": -math.inf,
                              "hp": None, "max_hp": None, "shield": None, "trusted_hp": False}
        buffers.move_to_end(actor)
        while len(buffers) > MAX_ACTORS:
            buffers.popitem(last=False)
        buf = buffers[actor]
        while buf["events"] and now - buf["events"][0]["time"] > WINDOW_SECONDS:
            buf["events"].popleft()
        for key, status in list(buf["statuses"].items()):
            if status["expires"] is not None and status["expires"] <= now:
                del buf["statuses"][key]
        return buf

    def _observation_buffers(self, actor, now, prepare):
        yield self._buffer(actor, now)
        if prepare and self.reset_on_pull:
            yield self._buffer(actor, now, preparing=True)

    def _event(self, actor, now, kind, source, name, amount=None, **extra):
        for buf in self._observation_buffers(actor, now, kind in ("heal", "hot", "gained", "lost")):
            buf["events"].append({"time": now, "kind": kind, "source": source[:200],
                                  "name": name[:200], "amount": amount,
                                  "hp": buf["hp"], "max_hp": buf["max_hp"],
                                  "shield": buf["shield"],
                                  "statuses": status_snapshot(buf["statuses"], now), **extra})

    def _source_snapshot(self, actor, now):
        buffers = self.buffers if player_id(actor) is not None else self.sources
        if actor not in buffers:
            return []
        statuses = self._buffer(actor, now)["statuses"]
        return [s for s in status_snapshot(statuses, now) if s["id"] in SOURCE_MITIGATION]

    def _health(self, actor, now, fields, offset, shield_index=None, sequence=None, observed=False):
        if actor is None or len(fields) <= offset + 1:
            return
        hp, maximum = (wire_int(fields[offset]), wire_int(fields[offset + 1]))
        shield = wire_int(fields[shield_index], maximum=255) if shield_index is not None and len(fields) > shield_index else None
        for buf in self._observation_buffers(actor, now, True):
            before = (buf["hp"], buf["max_hp"], buf["shield"])
            if hp is not None and (observed or not buf["trusted_hp"]):
                buf["hp"] = hp
            if maximum and (observed or not buf["trusted_hp"]):
                buf["max_hp"] = maximum
            if observed and hp is not None:
                buf["trusted_hp"] = True
            if shield is not None:
                buf["shield"] = shield
            after = (buf["hp"], buf["max_hp"], buf["shield"])
            matched = False
            if sequence and hp is not None:
                for event in reversed(buf["events"]):
                    if now - event["time"] > 10:
                        break
                    if event.get("sequence") == sequence:
                        event.update(hp_after=buf["hp"], shield_after=buf["shield"])
                        matched = True
            elif observed and hp is not None and fields[1] and fields[0] in ("38", "39", "43"):
                for event in reversed(buf["events"]):
                    if now - event["time"] > 1:
                        break
                    if event.get("_tick_stamp") == fields[1] and "hp_after" not in event:
                        event.update(hp_after=buf["hp"], shield_after=buf["shield"])
                        matched = True
                        break
            if observed and before != after and not matched:
                buf["events"].append({"time": now, "kind": "health", "source": "", "name": "",
                                      "amount": None, "hp": buf["hp"], "max_hp": buf["max_hp"],
                                      "shield": buf["shield"],
                                      "statuses": status_snapshot(buf["statuses"], now)})

    def _status_list(self, actor, now, fields):
        # These packet formats replace the actor's full status list.
        start = {"38": 18, "43": 15, "42": 4}[fields[0]]
        if len(fields) < start + 1:
            return
        for buf in self._observation_buffers(actor, now, True):
            current = OrderedDict()
            for i in range(start, min(len(fields) - 2, start + MAX_STATUSES * 3), 3):
                packed, duration_bits, source_id = (wire_int(v, 16) for v in fields[i:i + 3])
                if packed is None or duration_bits is None or source_id is None:
                    continue
                effect_id = packed & 0xFFFF
                if effect_id == 0 or (player_id(actor) is None and effect_id not in SOURCE_MITIGATION):
                    continue
                duration = abs(struct.unpack("!f", struct.pack("!I", duration_bits))[0])
                if not math.isfinite(duration) or duration > 1e12:
                    continue
                if is_permanent(effect_id):
                    duration = None
                source_id = _actor_int(source_id)
                key = (effect_id, source_id)
                old = buf["statuses"].get(key, {})
                current[key] = {"id": effect_id, "name": old.get("name", f"Status {effect_id:X}"),
                                "source": old.get("source", ""), "source_id": source_id,
                                "stacks": packed >> 16,
                                "expires": now + duration if duration else None}
            buf["statuses"].clear()
            buf["statuses"].update(current)
            while len(buf["statuses"]) > MAX_STATUSES:
                buf["statuses"].popitem(last=False)

    def is_duplicate_death(self, actor, now):
        buf = self.buffers.get(actor)
        return buf is not None and now - buf["death"] < DEATH_DUPLICATE_SECONDS

    def process(self, fields, *, now=None):
        if not fields:
            return
        now = self.clock() if now is None else now
        kind = fields[0]
        if kind == "01" and len(fields) > 3:
            self.reset()
            self.zone = fields[3]
        elif kind == "33" and len(fields) > 3 and fields[3].upper() == "4000000F":
            self.end_pull()
        elif kind == "04" and len(fields) > 2:
            actor = _actor_int(fields[2])
            self.sources.pop(actor, None)
            self.source_preparation.pop(actor, None)
        elif kind in ("37", "38", "39", "42", "43") and len(fields) > 3:
            actor = _actor_int(fields[2])
            if actor is None or actor > 0xFFFFFFFF:
                return
            if kind in ("38", "42", "43"):
                self._status_list(actor, now, fields)
            if kind != "42" and player_id(actor) is not None:
                self._health(actor, now, fields, 4 if kind == "39" else 5,
                             9 if kind in ("37", "38", "43") else None,
                             sequence=wire_int(fields[4], 16) if kind == "37" else None,
                             observed=True)
        elif kind in ("21", "22") and len(fields) >= 24:
            self._health(player_id(fields[6]), now, fields, 24)
            self._health(player_id(fields[2]), now, fields, 34)
            reflected = False
            for index in range(8, 24, 2):
                try:
                    flags = int(fields[index], 16)
                except ValueError:
                    continue
                if not 0 <= flags <= 0xFFFFFFFF:
                    continue
                if flags & 0xFF == 0x1D:
                    reflected = True
                    continue
                effect, amount, critical, direct = _unpack_effect(fields[index], fields[index + 1])
                if flags & 0xFF == 1:
                    effect, amount = "damage", 0
                on_source = (effect == "heal" and flags & 0x100) or (effect == "damage" and reflected)
                actor = player_id(fields[2] if on_source else fields[6])
                if actor is None:
                    continue
                if effect in ("damage", "heal"):
                    source = fields[7] if reflected and effect == "damage" else fields[3]
                    detail = {}
                    if effect == "damage":
                        source_actor = _actor_int(fields[6] if reflected else fields[2])
                        detail = {"source_statuses": self._source_snapshot(source_actor, now),
                                  "direct_hit": direct, "damage_type": (flags >> 16) & 0xF,
                                  "blocked": flags & 0xFF == 5, "parried": flags & 0xFF == 6}
                    self._event(actor, now, "instant-death" if flags & 0xFF == 0x33 else effect,
                                source, fields[5], None if flags & 0xFF == 0x33 else amount,
                                action_id=wire_int(fields[4], 16), critical=critical,
                                sequence=wire_int(fields[44], 16) if len(fields) > 44 else None, **detail)
        elif kind == "24" and len(fields) >= 7:
            actor = player_id(fields[2])
            if actor is None or fields[4] not in ("DoT", "HoT"):
                return
            try:
                amount = int(fields[6], 16)
            except ValueError:
                return
            if not 0 <= amount <= 0xFFFFFFFF:
                return
            self._health(actor, now, fields, 7)
            self._event(actor, now,
                        "dot" if fields[4] == "DoT" else "hot",
                        fields[18] if len(fields) > 18 else "", fields[4], amount,
                        status_id=wire_int(fields[5], 16), _tick_stamp=fields[1])
        elif kind in ("26", "30") and len(fields) > 8:
            actor = _actor_int(fields[7])
            if actor is None or actor > 0xFFFFFFFF:
                return
            try:
                effect_id = int(fields[2], 16)
            except ValueError:
                return
            if not 0 < effect_id <= 65535 or (player_id(actor) is None and effect_id not in SOURCE_MITIGATION):
                return
            key = (effect_id, _actor_int(fields[5]))
            if kind == "26":
                try:
                    duration = float(fields[4])
                except ValueError:
                    return
                if not math.isfinite(duration) or not 0 <= duration <= 1e12:
                    return
                if is_permanent(effect_id):
                    duration = None
            for buf in self._observation_buffers(actor, now, True):
                if kind == "26":
                    buf["statuses"][key] = {"name": fields[3][:200], "source": fields[6][:200],
                                            "id": effect_id, "source_id": key[1],
                                            "stacks": wire_int(fields[9], 16, 65535) if len(fields) > 9 else 0,
                                            "expires": now + duration if duration else None}
                    buf["statuses"].move_to_end(key)
                    while len(buf["statuses"]) > MAX_STATUSES:
                        buf["statuses"].popitem(last=False)
                else:
                    buf["statuses"].pop(key, None)
            if player_id(actor) is not None:
                self._event(actor, now, "gained" if kind == "26" else "lost", fields[6], fields[3],
                            status_id=effect_id,
                            status_stacks=wire_int(fields[9], 16, 65535) if len(fields) > 9 else 0,
                            status_duration=duration if kind == "26" else None)
        elif kind == "25" and len(fields) > 3:
            self.sources.pop(_actor_int(fields[2]), None)
            self.source_preparation.pop(_actor_int(fields[2]), None)
            actor = player_id(fields[2])
            if actor is None:
                return
            buf = self._buffer(actor, now)
            if self.is_duplicate_death(actor, now):
                return
            buf["death"] = now
            events = [{**{k: v for k, v in event.items() if k != "_tick_stamp"},
                       "time": round(event["time"] - now, 3)}
                      for event in buf["events"]]
            death = {"id": str(uuid4()), "actor": actor, "name": fields[3][:200],
                     "zone": self.zone[:200], "when": time.time(), "events": events,
                     "statuses": deepcopy(list(buf["statuses"].values()))}
            self.deaths.appendleft(death)
            buf["events"].clear()
            buf["statuses"].clear()
            buf.update(hp=None, max_hp=None, shield=None, trusted_hp=False)
            self.preparation.pop(actor, None)
            if self.on_death is not None:
                self.on_death(death)
