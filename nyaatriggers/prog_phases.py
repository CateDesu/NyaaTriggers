"""Passive phase evidence and validation of saved observations."""

from dataclasses import dataclass
import math


UMAD_ZONE = 1363
UMAD_PHASES = ("p1", "p2", "p3", "p4", "p5")
MAX_SECONDS = 253402214400


@dataclass(frozen=True)
class PhaseRule:
    ident: str
    event: str
    ability: int
    phase: str
    after: str | None = None
    starts_pull: bool = False


@dataclass(frozen=True)
class TransitionRule:
    ident: str
    event: str
    ability: int
    source: str
    target: str
    timeout: float


@dataclass(frozen=True)
class PhaseDefinition:
    ident: str
    revision: int
    zone_id: int
    phases: tuple[str, ...]
    rules: tuple[PhaseRule, ...]
    transitions: tuple[TransitionRule, ...] = ()
    verified: bool = False
    continuous_combat: bool = False

    def __post_init__(self):
        if (not text_id(self.ident) or type(self.revision) is not int or self.revision < 1
                or type(self.zone_id) is not int or self.zone_id < 1
                or not 1 <= len(self.phases) <= 16
                or len(set(self.phases)) != len(self.phases)
                or any(not text_id(p) for p in self.phases)):
            raise ValueError("Invalid phase definition")
        ids = set()
        for rule in (*self.rules, *self.transitions):
            if (not text_id(rule.ident) or rule.ident in ids
                    or rule.event not in ("20", "21", "22")
                    or type(rule.ability) is not int or not 0 < rule.ability <= 0xFFFFFFFF):
                raise ValueError("Invalid phase rule")
            ids.add(rule.ident)
        for rule in self.rules:
            if rule.phase not in self.phases or (rule.after is not None and rule.after not in self.phases):
                raise ValueError("Invalid phase prerequisite")
            if rule.starts_pull and (rule.phase != self.phases[0] or rule.after is not None):
                raise ValueError("Invalid initial pull rule")
        for rule in self.transitions:
            if (rule.source not in self.phases or rule.target not in self.phases
                    or self.phases.index(rule.target) != self.phases.index(rule.source) + 1
                    or not seconds(rule.timeout) or not 0 < rule.timeout <= 600):
                raise ValueError("Invalid phase transition")
        if self.verified and {r.phase for r in self.rules} != set(self.phases):
            raise ValueError("Verified definitions must cover every phase")


def text_id(value):
    return isinstance(value, str) and 0 < len(value) <= 80 and value.isascii() and all(
        c.isalnum() or c in "_.-" for c in value)


def seconds(value):
    return type(value) in (int, float) and 0 <= value <= MAX_SECONDS and math.isfinite(value)


# C24A also occurs in P4. BB40 confirms P5 without counting repeated jumps.
UMAD = PhaseDefinition(
    "umad", 1, UMAD_ZONE, UMAD_PHASES,
    tuple(PhaseRule(f"{phase}-{event}-{ability:x}", event, ability, phase)
          for phase, ability in (("p1", 0xC403), ("p2", 0xC24C), ("p3", 0xC3F7),
                                 ("p4", 0xC2DC), ("p5", 0xBB40))
          for event in ("20", "21", "22")),
    verified=True, continuous_combat=True,
)
DEFINITIONS: tuple[PhaseDefinition, ...] = (UMAD,)


def definition_for(zone_id, definitions=DEFINITIONS):
    return next((d for d in definitions if d.zone_id == zone_id and d.verified), None)


def new_tracking(definition):
    return {"version": 1, "definition_id": definition.ident,
            "definition_revision": definition.revision, "coverage": "recording",
            "reason": "", "transition": None, "observations": []}


def read_tracking(pull, zone_id, definitions=DEFINITIONS):
    """Return readable phase data without changing an unrecognized block."""
    data = pull.get("phase_tracking")
    if data is None:
        return None, None, ""
    try:
        if not isinstance(data, dict) or type(data.get("version")) is not int or data["version"] != 1:
            raise ValueError("Unsupported phase format")
        revision = data.get("definition_revision")
        if type(revision) is not int:
            raise ValueError("Invalid phase revision")
        definition = next((d for d in definitions if d.ident == data.get("definition_id")
                           and d.revision == revision and d.zone_id == zone_id), None)
        if definition is None:
            raise ValueError("Unknown phase definition")
        coverage = data.get("coverage")
        if coverage not in ("recording", "complete", "interrupted", "uncertain"):
            raise ValueError("Invalid phase coverage")
        if (not isinstance(data.get("reason"), str) or len(data["reason"]) > 200
                or (coverage == "complete" and not pull["complete"])
                or (coverage == "recording" and pull["ending"] != "active")):
            raise ValueError("Phase coverage does not match the pull")
        transition = data.get("transition")
        if transition is not None and (not isinstance(transition, str)
                                       or transition not in {r.ident for r in definition.transitions}
                                       or coverage != "recording"):
            raise ValueError("Invalid saved transition")
        observations = data.get("observations")
        if not isinstance(observations, list) or len(observations) > len(definition.phases):
            raise ValueError("Invalid phase observations")
        previous_rank, previous_time = -1, 0
        for observation in observations:
            if not isinstance(observation, dict):
                raise ValueError("Invalid phase observation")
            phase, at = observation.get("phase"), observation.get("at")
            rule = next((r for r in definition.rules if r.ident == observation.get("rule")), None)
            if (phase not in definition.phases or rule is None or rule.phase != phase
                    or not seconds(at) or at > pull["duration"]):
                raise ValueError("Invalid phase confirmation")
            rank = definition.phases.index(phase)
            if rank <= previous_rank or at < previous_time:
                raise ValueError("Phase confirmations are out of order")
            previous_rank, previous_time = rank, at
        return data, definition, ""
    except (ValueError, TypeError, KeyError) as exc:
        return None, None, str(exc)


def match_event(fields):
    if not fields or fields[0] not in ("20", "21", "22"):
        return None
    if len(fields) < 6:
        raise ValueError("Incomplete phase event")
    try:
        actor = int(fields[2], 16)
        ability = int(fields[4], 16)
    except (ValueError, TypeError):
        raise ValueError("Invalid numeric phase event") from None
    if not 0x40000000 <= actor < 0x50000000 or not 0 < ability <= 0xFFFFFFFF:
        return None
    return fields[0], ability


class PhaseAttempt:
    """One logical attempt with bounded transition and segment state."""

    def __init__(self, pull, definition, encounter, now):
        self.pull = pull
        self.definition = definition
        self.origin = encounter.get("monotonic_start", now)
        self.data = pull["phase_tracking"]
        self.segments = {}
        self.segment = encounter["pull_id"]
        self.candidate = None
        self.transition = None
        self.deadline = None
        self.waiting = False
        self.closed = False
        self.update(encounter)

    def contains(self, ident):
        return ident in self.segments

    def update(self, encounter, final=False):
        ident = encounter["pull_id"]
        offset = max(0, encounter.get("monotonic_start", self.origin) - self.origin)
        last = encounter.get("last_activity", encounter.get("monotonic_start", self.origin))
        observed = max(0, last - self.origin)
        self.segments[ident] = (offset + encounter["DURATION"], observed, encounter["deaths"])
        marker_time = max((o["at"] for o in self.data["observations"]), default=0)
        index = 1 if final else 0
        self.pull["duration"] = max(marker_time, *(s[index] for s in self.segments.values()))
        self.pull["deaths"] = max(sum(s[2] for s in self.segments.values()), self.pull["recap_count"])

    def offer_segment(self, encounter):
        if self.contains(encounter["pull_id"]):
            return True
        if not self.waiting or self.candidate is not None:
            return False
        self.candidate = encounter["pull_id"]
        self.update(encounter)
        return True

    def observe(self, fields, now):
        event = match_event(fields)
        if event is None or self.closed:
            return False
        observations = self.data["observations"]
        furthest = observations[-1]["phase"] if observations else None
        rank = self.definition.phases.index(furthest) if furthest else -1
        for rule in self.definition.rules:
            if (event != (rule.event, rule.ability)
                    or (rule.after is not None and furthest != rule.after)):
                continue
            if self.waiting and self.candidate is not None and rule.starts_pull:
                raise ValueError("A fresh pull appeared during a transition")
            if self.definition.phases.index(rule.phase) <= rank:
                continue
            if self.transition is not None:
                if rule.phase != self.transition.target:
                    raise ValueError("Unexpected phase during transition")
                if self.waiting and self.candidate is None:
                    raise ValueError("Continuation has no combat encounter")
                if self.candidate is not None:
                    self.segment = self.candidate
                self.candidate = None
                self.transition = None
                self.data["transition"] = None
                self.deadline = None
                self.waiting = False
            at = max(0, now - self.origin)
            self.pull["duration"] = max(self.pull["duration"], at)
            observations.append({"phase": rule.phase, "at": at, "rule": rule.ident})
            return True
        for rule in self.definition.transitions:
            if event == (rule.event, rule.ability) and furthest == rule.source:
                if self.transition is not None:
                    return False
                self.transition = rule
                self.deadline = now + rule.timeout
                self.data["transition"] = rule.ident
                return True
        return False

    def close(self, reason):
        self.closed = True
        self.data["coverage"] = ("complete" if reason in ("wipe", "combat-ended")
                                 else "uncertain" if reason == "boundary-uncertain" else "interrupted")
        self.data["reason"] = "" if self.data["coverage"] == "complete" else reason
        self.data["transition"] = None
        self.transition = None
        self.deadline = None
        self.waiting = False
