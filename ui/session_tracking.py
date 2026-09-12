"""Connect passive session pages to the shared combat feed."""


class SessionTrackingMixin:
    def _prog_pull_started(self, snapshot):
        self._death_recap.begin_pull()
        self._prog_sessions.pull_started(snapshot)
        self._prog_tab.refresh()

    def _prog_pull_finished(self, snapshot):
        self._death_recap.reset_on_pull = True
        self._prog_sessions.pull_finished(snapshot)
        self._prog_tab.refresh()

    def _track_activity_line(self, fields):
        self._death_recap.process(fields)
        if fields[0] == "01" and len(fields) > 3:
            self._prog_sessions.end(self._dps_meter.full_snapshot(), "duty-left")
            self._prog_tab.refresh()

    def _track_combat(self, act, game):
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
                       or (not zone_id and zone and zone != active["zone"])):
            self._prog_sessions.end(self._dps_meter.full_snapshot(), "duty-left")
            self._prog_tab.refresh()
        changed_id = zone_id and self._current_zone_id and zone_id != self._current_zone_id
        if changed_id or (zone and zone != self._death_recap.zone):
            self._death_recap.reset()
            self._death_recap.zone = zone

    def _finish_activity(self):
        self._prog_tab.flush()
        self._prog_sessions.end(self._dps_meter.full_snapshot(), "program-closed")
