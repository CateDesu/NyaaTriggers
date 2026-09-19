"""Connect passive session pages to the shared combat feed."""


class SessionTrackingMixin:
    def _prog_pull_started(self, snapshot):
        events = getattr(self, "_prog_events", None)
        if events is not None:
            events.append(("start", snapshot))
            return
        started = self._prog_sessions.pull_started(snapshot)
        if started or self._prog_sessions.attempt is None:
            self._death_recap.begin_pull()
        self._prog_tab.refresh()

    def _prog_pull_finished(self, snapshot):
        events = getattr(self, "_prog_events", None)
        if events is not None:
            events.append(("finish", snapshot))
            return
        self._prog_sessions.pull_finished(snapshot)
        if self._prog_sessions.attempt is None:
            self._death_recap.end_pull()
        self._prog_tab.refresh()

    def _begin_activity_event(self):
        self._prog_events = []
        # Use one arrival time for the meter and recap duplicate checks.
        self._prog_event_time = self._prog_sessions.clock()
        return self._prog_event_time

    def _finish_activity_event(self, fields=()):
        events = getattr(self, "_prog_events", None)
        if events is None:
            return
        self._prog_events = None
        snapshot = self._dps_meter.full_snapshot() if self._prog_sessions.needs_phase_snapshot(fields) else None
        started, ended = self._prog_sessions.process_event(
            fields, events, self._prog_event_time, snapshot)
        if ended or (any(kind == "finish" for kind, _ in events) and not self._prog_sessions.pending):
            self._death_recap.end_pull()
        if started or (any(kind == "start" for kind, _ in events) and self._prog_sessions.attempt is None):
            self._death_recap.begin_pull()
        if started or ended or (any(kind == "finish" for kind, _ in events) and self._prog_sessions.attempt is None):
            self._prog_tab.refresh()

    def _track_activity_line(self, fields):
        self._finish_activity_event(fields)
        self._death_recap.process(fields, now=self._prog_event_time)
        if fields[0] == "01" and len(fields) > 3:
            self._prog_sessions.end(self._dps_meter.full_snapshot(), "duty-left")
            self._prog_tab.refresh()

    def _track_combat(self, act, game):
        self._begin_activity_event()
        self._combat_known = True
        self._prog_sessions.combat(game)

    def _track_activity_connection(self, connected, message):
        if not connected:
            self._combat_known = False
            self._death_recap.reset()
            self._prog_sessions.feed_lost()
        self._prog_tab.tick()

    def _track_activity_zone(self, zone, zone_id):
        active = self._prog_sessions.current
        if active and ((zone_id and zone_id != active["zone_id"])
                       or (not zone_id and zone and zone != self._current_zone)):
            self._prog_sessions.end(self._dps_meter.full_snapshot(), "duty-left")
            self._prog_tab.refresh()
        changed_id = zone_id and self._current_zone_id and zone_id != self._current_zone_id
        known_ids = bool(zone_id and self._current_zone_id)
        changed_name = zone and self._death_recap.zone and zone != self._death_recap.zone
        first_metadata = getattr(self, "_awaiting_zone_metadata", False)
        if not first_metadata and (changed_id or (not known_ids and changed_name)):
            self._death_recap.reset()
        if first_metadata or zone or changed_id:
            self._death_recap.zone = zone

    def _finish_activity(self):
        self._prog_tab.flush()
        self._prog_sessions.end(self._dps_meter.full_snapshot(), "program-closed")
