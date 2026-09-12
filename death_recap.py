"""Bounded death history from the observed combat feed."""

from collections import OrderedDict, deque
from copy import deepcopy
import math
import time
from uuid import uuid4

from dps_meter import _actor_int, _unpack_effect

WINDOW_SECONDS = 15
MAX_DEATHS = 80
MAX_ACTORS = 128
MAX_EVENTS = 256
MAX_STATUSES = 64


def player_id(value):
    actor = _actor_int(value)
    return actor if actor is not None and 0x10000000 <= actor < 0x11000000 else None


class DeathRecap:
    def __init__(self, clock=None):
        self.clock = clock or time.monotonic
        self.deaths = deque(maxlen=MAX_DEATHS)
        self.buffers = OrderedDict()
        self.zone = ""
        self.on_death = None
        self.reset_on_pull = False

    def reset(self):
        self.buffers.clear()
        self.reset_on_pull = False

    def begin_pull(self):
        if self.reset_on_pull:
            self.reset()

    def _buffer(self, actor, now):
        if actor not in self.buffers:
            self.buffers[actor] = {"events": deque(maxlen=MAX_EVENTS),
                                   "statuses": OrderedDict(), "death": -math.inf}
        self.buffers.move_to_end(actor)
        while len(self.buffers) > MAX_ACTORS:
            self.buffers.popitem(last=False)
        buf = self.buffers[actor]
        while buf["events"] and now - buf["events"][0]["time"] > WINDOW_SECONDS:
            buf["events"].popleft()
        for key, status in list(buf["statuses"].items()):
            if status["expires"] is not None and status["expires"] <= now:
                del buf["statuses"][key]
        return buf

    @staticmethod
    def _event(buf, now, kind, source, name, amount=None):
        buf["events"].append({"time": now, "kind": kind, "source": source[:200],
                              "name": name[:200], "amount": amount})

    def process(self, fields):
        if not fields:
            return
        now = self.clock()
        kind = fields[0]
        if kind == "01" and len(fields) > 3:
            self.reset()
            self.zone = fields[3]
        elif kind == "33" and len(fields) > 3 and fields[3].upper() == "4000000F":
            self.reset_on_pull = True
        elif kind in ("21", "22") and len(fields) >= 24:
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
                effect, amount, _, _dh = _unpack_effect(fields[index], fields[index + 1])
                on_source = (effect == "heal" and flags & 0x100) or (effect == "damage" and reflected)
                actor = player_id(fields[2] if on_source else fields[6])
                if actor is None:
                    continue
                buf = self._buffer(actor, now)
                if effect in ("damage", "heal"):
                    source = fields[7] if reflected and effect == "damage" else fields[3]
                    self._event(buf, now, "instant-death" if flags & 0xFF == 0x33 else effect,
                                source, fields[5], None if flags & 0xFF == 0x33 else amount)
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
            self._event(self._buffer(actor, now), now,
                        "dot" if fields[4] == "DoT" else "hot",
                        fields[18] if len(fields) > 18 else "", fields[4], amount)
        elif kind in ("26", "30") and len(fields) > 8:
            actor = player_id(fields[7])
            if actor is None:
                return
            buf = self._buffer(actor, now)
            try:
                effect_id = int(fields[2], 16)
            except ValueError:
                return
            if not 0 < effect_id <= 0xFFFFFFFF:
                return
            key = (effect_id, _actor_int(fields[5]))
            if kind == "26":
                try:
                    duration = float(fields[4])
                except ValueError:
                    return
                if not math.isfinite(duration) or duration < 0:
                    return
                buf["statuses"][key] = {"name": fields[3][:200], "source": fields[6][:200],
                                        "expires": now + duration if duration else None}
                buf["statuses"].move_to_end(key)
                while len(buf["statuses"]) > MAX_STATUSES:
                    buf["statuses"].popitem(last=False)
            else:
                buf["statuses"].pop(key, None)
            self._event(buf, now, "gained" if kind == "26" else "lost", fields[6], fields[3])
        elif kind == "25" and len(fields) > 3:
            actor = player_id(fields[2])
            if actor is None:
                return
            buf = self._buffer(actor, now)
            if now - buf["death"] < 1:
                return
            buf["death"] = now
            events = [{**event, "time": round(event["time"] - now, 3)}
                      for event in buf["events"]]
            death = {"id": str(uuid4()), "actor": actor, "name": fields[3][:200],
                     "zone": self.zone[:200], "when": time.time(), "events": events,
                     "statuses": deepcopy(list(buf["statuses"].values()))}
            self.deaths.appendleft(death)
            buf["events"].clear()
            buf["statuses"].clear()
            if self.on_death is not None:
                self.on_death(death)
