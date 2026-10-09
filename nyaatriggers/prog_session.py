from copy import deepcopy
from collections import Counter, OrderedDict
from pathlib import Path
import shutil
import time
from uuid import uuid4

from nyaatriggers.recap_store import RecapStore
from nyaatriggers.record_store import load_records, record_id, write_record
from nyaatriggers.prog_phases import DEFINITIONS, PhaseAttempt, definition_for, match_event, new_tracking, read_tracking
from nyaatriggers.prog_progress import hp_samples, phase_progress, player_damage_target, wire_number

COMPLETE_REASONS = {"combat-ended", "wipe"}
LATE_DEATH_SECONDS = 2
LATE_WIPE_SECONDS = 5
CHECKPOINT_SECONDS = 15


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


def phase_summary(session, definitions=DEFINITIONS):
    """Count confirmed reach only for complete attempts with compatible rules."""
    excluded = Counter()
    eligible = []
    keys = set()
    readable = {}
    supported = definition_for(session["zone_id"], definitions)
    for pull in session["pulls"]:
        block = pull.get("phase_tracking")
        if isinstance(block, dict):
            ident, revision = block.get("definition_id"), block.get("definition_revision")
            if isinstance(ident, str) and type(revision) is int:
                keys.add((ident, revision))
        data, definition, error = read_tracking(pull, session["zone_id"], definitions)
        if error:
            excluded["unavailable"] += 1
        elif data is None:
            excluded["not-recorded" if "phase_tracking" not in pull or supported else "not-supported"] += 1
        elif not definition.verified:
            excluded["not-supported"] += 1
        else:
            readable[(definition.ident, definition.revision)] = definition
            if pull["ending"] == "active":
                excluded["active"] += 1
            elif data["coverage"] != "complete" or not pull["complete"]:
                excluded["uncertain" if data["coverage"] == "uncertain" else "interrupted"] += 1
            else:
                rank = max((definition.phases.index(o["phase"]) for o in data["observations"]), default=-1)
                eligible.append(rank)
    definition = next(iter(readable.values()), None)
    if len(keys) > 1:
        status = "tracking-differs"
        excluded["tracking-differs"] += len(eligible)
        eligible = []
        definition = None
    elif definition is not None:
        status = "available"
    elif not session["pulls"] and supported is not None:
        status = "available"
        definition = supported
    elif excluded["unavailable"]:
        status = "unavailable"
    elif excluded["not-recorded"]:
        status = "not-recorded"
    else:
        status = "not-supported"
    furthest = max(eligible, default=-1)
    return {"status": status, "definition": definition, "eligible": len(eligible),
            "counts": tuple(sum(rank >= index for rank in eligible)
                            for index in range(len(definition.phases))) if definition else (),
            "furthest": definition.phases[furthest] if definition and furthest >= 0 else None,
            "excluded": {reason: count for reason, count in excluded.items() if count}}


def _duration_basis(session, definitions):
    bases = set()
    for pull in session["pulls"]:
        if not pull["complete"]:
            continue
        data, definition, error = read_tracking(pull, session["zone_id"], definitions)
        if error or (definition is not None and not definition.verified):
            return None
        bases.add(("phase", definition.ident, definition.revision) if data else ("meter",))
    return next(iter(bases)) if len(bases) == 1 else None


def compare_sessions(selected, compared, definitions=DEFINITIONS):
    left = phase_summary(selected, definitions)
    right = phase_summary(compared, definitions)
    if selected["zone_id"] != compared["zone_id"]:
        status = "different-duty"
    elif left["status"] == right["status"] == "available":
        a, b = left["definition"], right["definition"]
        status = "available" if (a.ident, a.revision) == (b.ident, b.revision) else "tracking-differs"
    else:
        status = next(reason for reason in ("tracking-differs", "unavailable", "not-recorded", "not-supported")
                      if reason in (left["status"], right["status"]))
    phases = []
    if status == "available":
        for index, phase in enumerate(left["definition"].phases):
            a, b = left["counts"][index], right["counts"][index]
            change = 100 * (a / left["eligible"] - b / right["eligible"]) if left["eligible"] and right["eligible"] else None
            phases.append({"phase": phase, "selected": a, "compared": b, "change": change})
    basis = _duration_basis(selected, definitions)
    return {"selected": left, "compared": right, "phase_status": status, "phases": phases,
            "durations_comparable": status != "different-duty" and basis is not None
            and basis == _duration_basis(compared, definitions)}


class ProgSessions:
    def __init__(self, directory, clock=None, wall=None, definitions=None, writer=None):
        self.directory = directory
        self.writer = writer
        self._saving = {}
        self.recaps = RecapStore(Path(directory) / "recaps", writer)
        self.clock = clock or time.monotonic
        self.wall = wall or time.time
        self.definitions = DEFINITIONS if definitions is None else tuple(definitions)
        self.sessions, self.errors = load_records(directory, validate_session)
        for session in self.sessions:
            for pull in list(session["pulls"]):
                if pull["ending"] != "empty" or pull.get("recap_count") != 0:
                    continue
                recaps, errors = self.recaps.load(session["id"], pull["id"])
                if recaps:
                    pull["recap_count"] = len(recaps)
                    pull["deaths"] = max(pull["deaths"], len(recaps))
                elif not errors:
                    session["pulls"].remove(pull)
                self.errors.extend(f"{session['id']}/{pull['id']}: {error}" for error in errors)
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
        self._empty_pull = None
        self._checkpoint_at = None
        self.definition = None
        self.attempt = None
        self.last_attempt = None
        self._awaiting_snapshot = None
        self._wipe_candidate = None
        self._phase_progress = {}
        self._boss_actors = OrderedDict()
        self._boss_ids = {ident for definition in self.definitions
                          for _, ids in definition.bosses for ident in ids}
        self._boss_names = {ident for definition in self.definitions
                            for _, ident in definition.caster_bosses}
        self._attacked_bosses = set()

    def invalidate_phase_progress(self, zone_id=None):
        if zone_id is None:
            self._phase_progress.clear()
        else:
            self._phase_progress.pop(zone_id, None)

    def phase_progress(self, zone_id):
        if zone_id not in self._phase_progress:
            self._phase_progress[zone_id] = phase_progress(self.sessions, zone_id, self.definitions)
        return self._phase_progress[zone_id]

    def reset_bosses(self):
        self._boss_actors.clear()
        self._attacked_bosses.clear()

    def observe_hp(self, fields):
        if not fields:
            return False
        if fields[0] == "01":
            self.reset_bosses()
        elif fields[0] in ("03", "04") and len(fields) > 2:
            actor = wire_number(fields[2], 16)
            self._boss_actors.pop(actor, None)
            self._attacked_bosses.discard(actor)
            if fields[0] == "03" and len(fields) > 12:
                npc_id = wire_number(fields[10])
                name_id = wire_number(fields[9])
                owner = wire_number(fields[6], 16)
                maximum = wire_number(fields[12])
                if (actor is not None and 0x40000000 <= actor < 0x50000000
                        and npc_id and (npc_id in self._boss_ids or name_id in self._boss_names)
                        and owner == 0):
                    self._boss_actors[actor] = (npc_id, maximum, name_id)
                    while len(self._boss_actors) > 64:
                        discarded, _ = self._boss_actors.popitem(last=False)
                        self._attacked_bosses.discard(discarded)
        attempt = self.attempt
        if (attempt is None or attempt.closed or attempt.waiting
                or not attempt.data["observations"]):
            return False
        phase = attempt.data["observations"][-1]["phase"]
        bosses = dict(attempt.definition.bosses).get(phase, ())
        caster_name = dict(attempt.definition.caster_bosses).get(phase)
        caster = attempt.data["observations"][-1].get("actor")
        if caster_name is not None and player_damage_target(fields) == caster:
            self._attacked_bosses.add(caster)
        changed = False
        for actor, current, maximum, missing_maximum in hp_samples(fields):
            metadata = self._boss_actors.get(actor)
            caster_confirmed = (metadata is not None and caster_name is not None
                                and metadata[2] == caster_name and actor == caster
                                and actor in self._attacked_bosses)
            if metadata is None or not (metadata[0] in bosses or caster_confirmed):
                continue
            if missing_maximum:
                maximum = metadata[1]
            if (current is None or maximum is None or maximum == 0
                    or current > maximum):
                continue
            self._boss_actors[actor] = (metadata[0], maximum, metadata[2])
            samples = attempt.data.setdefault("boss_hp", {})
            previous = samples.get(phase)
            if previous is not None and current * previous["maximum"] >= previous["current"] * maximum:
                continue
            samples[phase] = {"current": current, "maximum": maximum,
                              "npc_id": metadata[0], "observed_at": self.wall()}
            if caster_confirmed:
                samples[phase].update(actor=actor, npc_name_id=metadata[2], target_confirmed=True)
            changed = True
        if changed:
            self.invalidate_phase_progress(self.current["zone_id"])
        return changed

    def start(self, name, zone_id, zone, in_combat):
        if self.current is not None:
            raise ValueError("A session is already active")
        if not zone_id or not zone:
            raise ValueError("Wait for the current duty to be detected")
        session = {"version": 1, "id": str(uuid4()), "name": name.strip()[:200] or zone,
                   "zone_id": zone_id, "zone": zone, "started": self.wall(),
                   "elapsed": 0.0, "state": "active", "pulls": []}
        validate_session(session)
        if self.writer is None:
            write_record(self.directory, session)
        else:
            self.save(session)
        self.sessions.insert(0, session)
        self.current = session
        self.started_at = self.clock()
        self._checkpoint_at = self.started_at
        self.ready = not in_combat
        self.pending = None
        self._recap_pull = None
        self._empty_pull = None
        self.definition = definition_for(zone_id, self.definitions)
        if self.definition is not None and not self.definition.continuous_combat:
            self.ready = False
        self.attempt = None
        self.last_attempt = None
        self._awaiting_snapshot = None
        self._wipe_candidate = None
        return session

    def elapsed(self, session):
        return max(0, self.clock() - self.started_at) if session is self.current else session["elapsed"]

    def save(self, session):
        token = self._saving[session["id"]] = object()
        record = session
        if session is self.current:
            session["elapsed"] = self.elapsed(session)
            self._checkpoint_at = self.clock()
            if self._empty_pull is not None:
                # Keep the pull reachable if its late recap saves before the summary.
                record = {**session, "pulls": [*session["pulls"], self._empty_pull]}
        try:
            validate_session(record)
            if self.writer is not None:
                self.writer.submit(self.directory, record, write_record,
                                   lambda error: self._saved(session, error, token))
                self.unsaved[session["id"]] = session
                return True
            write_record(self.directory, record)
        except (OSError, ValueError) as exc:
            return self._saved(session, exc, token)
        return self._saved(session, None, token)

    def _saved(self, session, error, token):
        if self._saving.get(session["id"]) is not token:
            return False
        del self._saving[session["id"]]
        if error is not None:
            self.unsaved[session["id"]] = session
            self.save_errors[session["id"]] = str(error)
            self.save_error = "\n".join(self.save_errors.values())
            return False
        self.unsaved.pop(session["id"], None)
        self.save_errors.pop(session["id"], None)
        self.save_error = "\n".join(self.save_errors.values())
        return True

    def checkpoint(self):
        if self.current is not None and self.clock() - self._checkpoint_at >= CHECKPOINT_SECONDS:
            self.save(self.current)

    def set_archived(self, session, archived):
        if type(archived) is not bool or not any(record is session for record in self.sessions):
            raise ValueError("Invalid session archive choice")
        if session is self.current or session["state"] == "active":
            raise ValueError("End the session before archiving it")
        previous = session.get("archived")
        had_flag = "archived" in session
        session["archived"] = archived
        saved = self.save(session)
        # Archive visibility depends on a confirmed save.
        self.poll_saves(wait=True)
        if saved and session["id"] not in self.save_errors:
            return True
        if had_flag:
            session["archived"] = previous
        else:
            session.pop("archived")
        return False

    def poll_saves(self, *, wait=False):
        if self.writer is not None:
            self.writer.poll(wait=wait)

    def delete(self, session):
        if not any(record is session for record in self.sessions):
            raise ValueError("Invalid session selection")
        if session is self.current or session["state"] == "active":
            raise ValueError("End the session before deleting it")
        ident = record_id(session["id"])
        self.poll_saves(wait=True)
        try:
            shutil.rmtree(self.recaps.directory / ident)
        except FileNotFoundError:
            pass
        (Path(self.directory) / (ident + ".json")).unlink(missing_ok=True)
        self.sessions = [record for record in self.sessions if record is not session]
        self.invalidate_phase_progress(session["zone_id"])
        self.unsaved.pop(ident, None)
        self.save_errors.pop(ident, None)
        self._saving.pop(ident, None)
        self.save_error = "\n".join(self.save_errors.values())
        for recap_id, data in list(self.recaps.unsaved.items()):
            if data["session_id"] == ident:
                self.recaps.unsaved.pop(recap_id)
                self.recaps.errors.pop(recap_id, None)
                self.recaps._saving.pop(recap_id, None)
        self.errors = [error for error in self.errors if not error.startswith(ident + "/")]

    def can_delete_pull(self, session, pull):
        if not any(record is session for record in self.sessions) or not isinstance(pull, dict):
            return False
        if not any(record is pull for record in session["pulls"]) or pull.get("ending") == "active":
            return False
        try:
            record_id(session["id"])
            record_id(pull["id"])
        except (KeyError, ValueError):
            return False
        if session is self.current:
            if self.pending == pull["id"] or (self.attempt is not None and self.attempt.pull is pull):
                return False
            if (self._awaiting_snapshot is not None
                    and self._awaiting_snapshot["Encounter"]["pull_id"] == pull["id"]):
                return False
        return True

    def delete_pull(self, session, pull):
        if not self.can_delete_pull(session, pull):
            raise ValueError("Select a finished saved pull before deleting it")
        session_id, pull_id = record_id(session["id"]), record_id(pull["id"])
        self.poll_saves(wait=True)
        remaining = [record for record in session["pulls"] if record is not pull]
        record = deepcopy({**session, "pulls": remaining})
        if session is self.current:
            record["elapsed"] = self.elapsed(session)
            if self._empty_pull is not None and self._empty_pull is not pull:
                record["pulls"].append(deepcopy(self._empty_pull))
        validate_session(record)
        write_record(self.directory, record)
        session["pulls"] = remaining
        session["elapsed"] = record["elapsed"]
        self.unsaved.pop(session_id, None)
        self.save_errors.pop(session_id, None)
        self._saving.pop(session_id, None)
        self.save_error = "\n".join(self.save_errors.values())
        if session is self.current:
            self._checkpoint_at = self.clock()
            if self._recap_pull == pull_id:
                self._recap_pull = None
                self._recap_until = None
                self._recap_ids.clear()
            if self._empty_pull is pull:
                self._empty_pull = None
            if self.last_attempt is not None and self.last_attempt.pull is pull:
                self.last_attempt = None
            if self._wipe_candidate is not None and self._wipe_candidate[0] is pull:
                self._wipe_candidate = None
        for recap_id, data in list(self.recaps.unsaved.items()):
            if (data["session_id"], data["pull_id"]) == (session_id, pull_id):
                self.recaps.unsaved.pop(recap_id)
                self.recaps.errors.pop(recap_id, None)
                self.recaps._saving.pop(recap_id, None)
        prefix = f"{session_id}/{pull_id}:"
        self.errors = [error for error in self.errors if not error.startswith(prefix)]
        self.invalidate_phase_progress(session["zone_id"])
        try:
            shutil.rmtree(self.recaps._directory(session_id, pull_id))
        except FileNotFoundError:
            pass
        except OSError as exc:
            return str(exc) or type(exc).__name__
        return None

    def flush_pending(self, *, wait=False):
        self.poll_saves(wait=wait)
        for session in list(self.unsaved.values()):
            if session["id"] not in self._saving:
                self.save(session)
        self.recaps.flush_pending()
        self.poll_saves(wait=wait)

    def close(self):
        self.flush_pending(wait=True)
        if self.writer is not None:
            self.writer.close()

    def combat(self, in_game):
        if not in_game and (self.definition is None or self.definition.continuous_combat):
            self.ready = True

    def pull_started(self, snapshot):
        self._wipe_candidate = None
        self._empty_pull = None
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
        self._attacked_bosses.clear()
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
        late_deaths = reason in COMPLETE_REASONS or (
            reason == "empty" and encounter.get("boundary_reason") in COMPLETE_REASONS)
        pull.update(duration=encounter["DURATION"],
                    deaths=max(encounter["deaths"], pull.get("recap_count", 0)),
                    ending=reason, complete=reason in COMPLETE_REASONS)
        if reason == "empty" and not pull.get("recap_count"):
            self.current["pulls"].remove(pull)
            # A late death can restore this attempt without listing empty pulls.
            self._empty_pull = pull if late_deaths else None
        if self.pending == ident:
            self.pending = None
            if reason == "combat-ended":
                self._wipe_candidate = (pull, self.clock())
            if late_deaths:
                self._recap_until = self.clock() + LATE_DEATH_SECONDS
            else:
                self._recap_pull = None
        if reason == "feed-lost":
            self.ready = False
        self.save(self.current)

    def feed_lost(self):
        self.reset_bosses()
        self._interrupt_attempt("feed-lost")
        self.ready = False
        self._recap_pull = None
        self._empty_pull = None
        self._awaiting_snapshot = None
        self._wipe_candidate = None

    def record_death(self, death):
        if self.current is None or self._recap_pull is None:
            return None
        if self._recap_until is not None and self.clock() > self._recap_until:
            self._empty_pull = None
            return None
        if death["id"] in self._recap_ids:
            return None
        pull = next((p for p in self.current["pulls"] if p["id"] == self._recap_pull), None)
        if pull is None and self._empty_pull is not None and self._empty_pull["id"] == self._recap_pull:
            pull = self._empty_pull
        if pull is None:
            return None
        data = self.recaps.record(self.current["id"], pull["id"], death)
        if pull is self._empty_pull:
            self.current["pulls"].append(pull)
            self._empty_pull = None
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
        if reason == "duty-left":
            self.reset_bosses()
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
        self._empty_pull = None
        self.attempt = None
        self.last_attempt = None
        self._awaiting_snapshot = None
        self._wipe_candidate = None

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
            self.observe_hp(fields)
            for kind, notification in notifications:
                if kind == "finish":
                    if wipe and self.attempt is not None:
                        notification = deepcopy(notification)
                        notification["Encounter"]["end_reason"] = "wipe"
                    self.pull_finished(notification)
        if wipe:
            if self.attempt is not None:
                self._close_attempt(self.attempt, "wipe")
            elif self._wipe_candidate is not None:
                pull, ended_at = self._wipe_candidate
                if now - ended_at <= LATE_WIPE_SECONDS and pull["ending"] == "combat-ended":
                    pull["ending"] = "wipe"
                    self.save(self.current)
                self._wipe_candidate = None
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
            if attempt.observe(fields, self.clock() if now is None else now, self.wall()):
                self.invalidate_phase_progress(self.current["zone_id"])
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
                    and self._wipe_candidate is not None and self._wipe_candidate[0] is attempt.pull
                    and self.clock() - self._wipe_candidate[1] <= LATE_WIPE_SECONDS):
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
        if (reason == "empty" and encounter.get("boundary_reason") == "combat-ended"
                and attempt.definition.continuous_combat and attempt.data["observations"]):
            reason = "combat-ended"
        self._close_attempt(attempt, reason, encounter.get("boundary_reason") in COMPLETE_REASONS)

    def _close_attempt(self, attempt, reason, late_deaths=False):
        if attempt.closed:
            return
        marker_time = max((o["at"] for o in attempt.data["observations"]), default=0)
        attempt.pull["duration"] = max(marker_time, *(s[1] for s in attempt.segments.values()))
        attempt.close(reason)
        attempt.pull.update(ending=reason, complete=reason in COMPLETE_REASONS)
        self.pending = None
        self.attempt = None
        self.last_attempt = attempt
        late_deaths = reason in COMPLETE_REASONS or (reason == "empty" and late_deaths)
        self.ready = reason == "wipe" or (attempt.definition.continuous_combat and late_deaths)
        if reason == "combat-ended":
            self._wipe_candidate = (attempt.pull, self.clock())
        if late_deaths:
            self._recap_until = self.clock() + LATE_DEATH_SECONDS
        else:
            self._recap_pull = None
        if reason == "empty" and not attempt.pull["recap_count"] and not attempt.data["observations"]:
            self.current["pulls"].remove(attempt.pull)
            self._empty_pull = attempt.pull if late_deaths else None
            self.last_attempt = None
        self.save(self.current)

    def _interrupt_attempt(self, reason):
        if self.attempt is not None:
            self._close_attempt(self.attempt, reason)
