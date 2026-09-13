"""Prog sessions and their saved pull summaries."""

from copy import deepcopy
from pathlib import Path
import time
from uuid import uuid4

from recap_store import RecapStore
from record_store import load_records, record_id, write_record
from prog_phases import DEFINITIONS, PhaseAttempt, definition_for, match_event, new_tracking, read_tracking

COMPLETE_REASONS = {"combat-ended", "wipe"}
LATE_DEATH_SECONDS = 2


def validate_session(data):
    if not isinstance(data.get("name"), str) or not isinstance(data.get("zone"), str):
        raise ValueError("Invalid session name or duty")
    if type(data.get("zone_id")) is not int or data["zone_id"] < 0:
        raise ValueError("Invalid duty ID")
    if data.get("state") not in ("active", "ended", "interrupted"):
        raise ValueError("Invalid session state")
    for key in ("started", "elapsed"):
        if type(data.get(key)) not in (int, float) or not 0 <= data[key] <= 253402214400:
            raise ValueError("Invalid session time")
    if not isinstance(data.get("pulls"), list):
        raise ValueError("Invalid pull list")
    seen = set()
    for pull in data["pulls"]:
        if not isinstance(pull, dict):
            raise ValueError("Invalid pull")
        ident = record_id(pull.get("id"))
        if ident in seen:
            raise ValueError("Duplicate pull ID")
        seen.add(ident)
        for key in ("started", "duration"):
            if type(pull.get(key)) not in (int, float) or not 0 <= pull[key] <= 253402214400:
                raise ValueError("Invalid pull time")
        if type(pull.get("deaths")) is not int or pull["deaths"] < 0:
            raise ValueError("Invalid death count")
        if "recap_count" in pull and (type(pull["recap_count"]) is not int or pull["recap_count"] < 0):
            raise ValueError("Invalid recap count")
        if type(pull.get("complete")) is not bool or type(pull.get("bookmark")) is not bool:
            raise ValueError("Invalid pull flags")
        if not isinstance(pull.get("note"), str) or not isinstance(pull.get("ending"), str):
            raise ValueError("Invalid pull text")
        if pull["complete"] != (pull["ending"] in COMPLETE_REASONS):
            raise ValueError("Pull completeness does not match its ending")


def summary(session):
    pulls = session["pulls"]
    complete = [p for p in pulls if p["complete"]]
    return {"pulls": len(complete), "interrupted": sum(not p["complete"] and p["ending"] != "active" for p in pulls),
            "longest": max((p["duration"] for p in complete), default=0),
            "combat": sum(p["duration"] for p in pulls)}


class ProgSessions:
    def __init__(self, directory, clock=None, wall=None, definitions=None):
        self.directory = directory
        self.recaps = RecapStore(Path(directory) / "recaps")
        self.clock = clock or time.monotonic
        self.wall = wall or time.time
        self.definitions = DEFINITIONS if definitions is None else tuple(definitions)
        self.sessions, self.errors = load_records(directory, validate_session)
        for session in self.sessions:
            if session["state"] == "active":
                session["state"] = "interrupted"
                for pull in session["pulls"]:
                    if pull["ending"] == "active":
                        data, _, error = read_tracking(pull, session["zone_id"], self.definitions)
                        if data is not None and not error:
                            data.update(coverage="interrupted", reason="program-closed", transition=None)
                        pull["ending"] = "program-closed"
                        pull["complete"] = False
        self.sessions.sort(key=lambda s: s["started"], reverse=True)
        self.current = None
        self.started_at = None
        self.ready = False
        self.pending = None
        self.save_error = ""
        self.unsaved = {}
        self.save_errors = {}
        self._recap_pull = None
        self._recap_until = None
        self._recap_ids = set()
        self.definition = None
        self.attempt = None
        self.last_attempt = None
        self._awaiting_snapshot = None

    def start(self, name, zone_id, zone, in_combat):
        if self.current is not None:
            raise ValueError("A session is already active")
        if not zone_id or not zone:
            raise ValueError("Wait for the current duty to be detected")
        session = {"version": 1, "id": str(uuid4()), "name": name.strip()[:200] or zone,
                   "zone_id": zone_id, "zone": zone, "started": self.wall(),
                   "elapsed": 0.0, "state": "active", "pulls": []}
        validate_session(session)
        write_record(self.directory, session)
        self.sessions.insert(0, session)
        self.current = session
        self.started_at = self.clock()
        self.ready = not in_combat
        self.pending = None
        self._recap_pull = None
        self.definition = definition_for(zone_id, self.definitions)
        if self.definition is not None:
            self.ready = False
        self.attempt = None
        self.last_attempt = None
        self._awaiting_snapshot = None
        return session

    def elapsed(self, session):
        return max(0, self.clock() - self.started_at) if session is self.current else session["elapsed"]

    def save(self, session):
        if session is self.current:
            session["elapsed"] = self.elapsed(session)
        try:
            validate_session(session)
            write_record(self.directory, session)
        except (OSError, ValueError) as exc:
            self.unsaved[session["id"]] = session
            self.save_errors[session["id"]] = str(exc)
            self.save_error = "\n".join(self.save_errors.values())
            return False
        self.unsaved.pop(session["id"], None)
        self.save_errors.pop(session["id"], None)
        self.save_error = "\n".join(self.save_errors.values())
        return True

    def flush_pending(self):
        for session in list(self.unsaved.values()):
            self.save(session)
        self.recaps.flush_pending()

    def combat(self, in_game):
        if not in_game and self.definition is None:
            self.ready = True

    def pull_started(self, snapshot):
        if self.current is None or not self.ready:
            self._recap_pull = None
            if self.current is not None and self.definition is not None:
                self._awaiting_snapshot = snapshot
            return False
        encounter = snapshot["Encounter"]
        if self.pending is not None:
            if self.attempt is not None and not self.attempt.offer_segment(encounter):
                self._interrupt_attempt("boundary-uncertain")
                self.ready = False
                return False
            self._recap_pull = self.pending
            return False
        pull = {"id": encounter["pull_id"], "started": encounter["wall_start"],
                "duration": 0, "ending": "active", "complete": False,
                "deaths": 0, "bookmark": False, "note": "", "recap_count": 0,
                "phase_tracking": new_tracking(self.definition) if self.definition else None}
        self.current["pulls"].append(pull)
        self.pending = pull["id"]
        self._recap_pull = pull["id"]
        self._recap_until = None
        self._recap_ids.clear()
        self.last_attempt = None
        self._awaiting_snapshot = None
        self.attempt = PhaseAttempt(pull, self.definition, encounter, self.clock()) if self.definition else None
        self.save(self.current)
        return True

    def pull_finished(self, snapshot):
        if self.current is None:
            return
        encounter = snapshot["Encounter"]
        ident = encounter["pull_id"]
        if self._awaiting_snapshot and self._awaiting_snapshot["Encounter"]["pull_id"] == ident:
            self._awaiting_snapshot = None
        attempt = next((a for a in (self.attempt, self.last_attempt) if a and a.contains(ident)), None)
        if attempt is not None:
            self._finish_phase_segment(attempt, encounter)
            return
        pull = next((p for p in self.current["pulls"] if p["id"] == ident), None)
        if pull is None or pull.get("phase_tracking") is not None:
            return
        reason = encounter["end_reason"]
        if reason == "empty" and not pull.get("recap_count"):
            self.current["pulls"].remove(pull)
        else:
            pull.update(duration=encounter["DURATION"],
                        deaths=max(encounter["deaths"], pull.get("recap_count", 0)),
                        ending=reason, complete=reason in COMPLETE_REASONS)
        if self.pending == ident:
            self.pending = None
            if reason in COMPLETE_REASONS:
                self._recap_until = self.clock() + LATE_DEATH_SECONDS
            else:
                self._recap_pull = None
        if reason == "feed-lost":
            self.ready = False
        self.save(self.current)

    def feed_lost(self):
        self._interrupt_attempt("feed-lost")
        self.ready = False
        self._recap_pull = None
        self._awaiting_snapshot = None

    def record_death(self, death):
        if self.current is None or self._recap_pull is None:
            return None
        if self._recap_until is not None and self.clock() > self._recap_until:
            return None
        if death["id"] in self._recap_ids:
            return None
        pull = next((p for p in self.current["pulls"] if p["id"] == self._recap_pull), None)
        if pull is None:
            return None
        data = self.recaps.record(self.current["id"], pull["id"], death)
        self._recap_ids.add(death["id"])
        pull["recap_count"] += 1
        pull["deaths"] = max(pull["deaths"], pull["recap_count"])
        self.save(self.current)
        return data

    def update_active(self, snapshot):
        if self.current is None or self.pending is None or snapshot is None:
            return
        encounter = snapshot["Encounter"]
        if self.attempt is not None:
            if encounter["pull_id"] in (self.attempt.segment, self.attempt.candidate):
                self.attempt.update(encounter)
            return
        if encounter["pull_id"] != self.pending:
            return
        for pull in self.current["pulls"]:
            if pull["id"] == self.pending:
                pull.update(duration=encounter["DURATION"],
                            deaths=max(encounter["deaths"], pull.get("recap_count", 0)))
                return

    def end(self, snapshot=None, reason="session-ended"):
        if self.current is None:
            return
        if self.pending and snapshot:
            copy = deepcopy(snapshot)
            copy["Encounter"]["end_reason"] = reason
            self.pull_finished(copy)
        if self.pending:
            self._interrupt_attempt(reason)
            for pull in self.current["pulls"]:
                if pull["id"] == self.pending:
                    pull.update(ending=reason, complete=False)
        self.current["elapsed"] = self.elapsed(self.current)
        self.current["state"] = "ended"
        self.current["ended"] = self.wall()
        self.save(self.current)
        self.current = None
        self.started_at = None
        self.pending = None
        self.ready = False
        self._recap_pull = None
        self.attempt = None
        self.last_attempt = None
        self._awaiting_snapshot = None

    def process_event(self, fields, notifications, now=None, snapshot=None):
        """Resolve meter notifications and evidence before the page updates."""
        now = self.clock() if now is None else now
        self.check_phase_timeout(now)
        before = self.pending
        started = False
        wipe = len(fields) > 3 and fields[0] == "33" and fields[3].upper() == "4000000F"
        # Combat flag changes can finish and begin in the same message.
        if not fields:
            for kind, snapshot in notifications:
                if kind == "start":
                    started = self.pull_started(snapshot) or started
                else:
                    self.pull_finished(snapshot)
        else:
            for kind, notification in notifications:
                if kind == "start":
                    started = self.pull_started(notification) or started
            self.update_active(snapshot)
            if self._awaiting_snapshot and snapshot is not None:
                if self._awaiting_snapshot["Encounter"]["pull_id"] == snapshot["Encounter"]["pull_id"]:
                    self._awaiting_snapshot = snapshot
            started = self.observe_phase(fields, now) or started
            for kind, notification in notifications:
                if kind == "finish":
                    if wipe and self.attempt is not None:
                        notification = deepcopy(notification)
                        notification["Encounter"]["end_reason"] = "wipe"
                    self.pull_finished(notification)
        if wipe:
            if self.attempt is not None:
                self._close_attempt(self.attempt, "wipe")
            if self.definition is not None:
                self.ready = True
        return started, before is not None and self.pending != before

    def needs_phase_snapshot(self, fields):
        if self.definition is None:
            return False
        try:
            event = match_event(fields)
        except ValueError:
            return False
        return any(event == (r.event, r.ability)
                   for r in (*self.definition.rules, *self.definition.transitions))

    def observe_phase(self, fields, now=None):
        started = False
        if self.attempt is None and self.definition is not None and self._awaiting_snapshot is not None:
            try:
                event = match_event(fields)
            except ValueError:
                event = None
            if any(r.starts_pull and event == (r.event, r.ability) for r in self.definition.rules):
                snapshot = self._awaiting_snapshot
                self.ready = True
                started = self.pull_started(snapshot)
        attempt = self.attempt
        if attempt is None:
            return False
        try:
            if attempt.observe(fields, self.clock() if now is None else now):
                self.save(self.current)
        except Exception:
            self._interrupt_attempt("boundary-uncertain")
            self.ready = False
        return started

    def check_phase_timeout(self, now=None):
        now = self.clock() if now is None else now
        if self.attempt and self.attempt.deadline is not None and now >= self.attempt.deadline:
            self._interrupt_attempt("boundary-uncertain")
            self.ready = False

    def _finish_phase_segment(self, attempt, encounter):
        reason = encounter["end_reason"]
        if reason == "empty" and encounter.get("boundary_reason") in (
                "wipe", "feed-lost", "duty-left", "program-closed", "session-ended"):
            reason = encounter["boundary_reason"]
        if attempt.closed:
            if (reason == "wipe" and attempt.pull["ending"] == "combat-ended"
                    and self._recap_until is not None and self.clock() <= self._recap_until):
                attempt.pull["ending"] = "wipe"
                self.save(self.current)
            return
        ident = encounter["pull_id"]
        if ident != attempt.segment and ident != attempt.candidate:
            return
        attempt.update(encounter, final=True)
        if reason in ("combat-ended", "empty") and attempt.transition is not None:
            if ident == attempt.candidate:
                self._interrupt_attempt("boundary-uncertain")
                self.ready = False
            else:
                attempt.waiting = True
                self.save(self.current)
            return
        self._close_attempt(attempt, reason)

    def _close_attempt(self, attempt, reason):
        if attempt.closed:
            return
        marker_time = max((o["at"] for o in attempt.data["observations"]), default=0)
        attempt.pull["duration"] = max(marker_time, *(s[1] for s in attempt.segments.values()))
        attempt.close(reason)
        attempt.pull.update(ending=reason, complete=reason in COMPLETE_REASONS)
        self.pending = None
        self.attempt = None
        self.last_attempt = attempt
        self.ready = reason == "wipe"
        if reason in COMPLETE_REASONS:
            self._recap_until = self.clock() + LATE_DEATH_SECONDS
        else:
            self._recap_pull = None
        if reason == "empty" and not attempt.pull["recap_count"] and not attempt.data["observations"]:
            self.current["pulls"].remove(attempt.pull)
            self.last_attempt = None
        self.save(self.current)

    def _interrupt_attempt(self, reason):
        if self.attempt is not None:
            self._close_attempt(self.attempt, reason)
