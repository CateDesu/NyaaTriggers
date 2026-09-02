"""Current Instance tab. Live log view, status timers and the zone
detect UI. The log line dispatch spine stays in the shell and calls into
here. Mixin for MainWindow, all state rides on self.
"""

import math
import re
import time

from PyQt6.QtCore import Qt, QTimer, pyqtSlot
from PyQt6.QtGui import QBrush, QColor, QTextCharFormat, QTextCursor
from PyQt6.QtWidgets import QMenu

from trigger_engine import Trigger, compile_user_regex, _safe_search
from tts import speak
from locale_util import _
from sequential import SequentialRunner
from status_timer import StatusTimerRunner
from telesto_client import _actor_int
from dps_meter import DpsMeter, METER_LOG_TYPES

import app_common as ac
from app_common import (
    _ABILITY_TYPES, _AbilityData, _C_EN, _C_ZONE, _DISPATCH_BUDGET_S, _DOT_GREEN, _DOT_GREY, _DOT_RED, _hex_id, _prefill_name_tts, canonical_zone_name,
)


class InstanceTabMixin:
    def _zone_dot(self, t: "Trigger") -> tuple[str, str]:
        if not self._current_zone:
            return "", _DOT_GREY
        # Whitespace-only counts as unlocked, matching the sort grouping.
        if not t.zone_regex.strip():
            return "●", _DOT_GREEN
        rx = compile_user_regex(t.zone_regex, re.IGNORECASE)
        if rx is None:
            return "●", _DOT_GREY
        return "●", (_DOT_GREEN if self._zone_matches(rx) else _DOT_RED)

    def _refresh_zone_column(self) -> None:
        trigger_map = {t.id: t for t in self._triggers}
        prev = self._table.blockSignals(True)
        for row in range(self._table.rowCount()):
            en_item = self._table.item(row, _C_EN)
            if en_item is None:
                continue
            t = trigger_map.get(en_item.data(Qt.ItemDataRole.UserRole))
            if t is None:
                continue
            dot, color = self._zone_dot(t)
            zone_item = self._table.item(row, _C_ZONE)
            if zone_item:
                zone_item.setText(dot)
                zone_item.setForeground(QBrush(QColor(color)))
        self._table.blockSignals(prev)

    def _clear_player(self, actor_id: str, name: str = "", force: bool = False) -> bool:
        """Remove the sign from a player. Routes like _mark_player and skips
        when the party slot is unknown. force bypasses the client's enabled
        gate, for the clears that must land while automarkers is going off."""
        tc = self._telesto_client
        if tc is None:
            return False
        if self._is_me_actor(actor_id, name):
            return tc.clear_self(force=force)
        return tc.clear_actor(actor_id, force=force)

    def _note_actor_job(self, aid: int, job: int) -> None:
        """Single bounded ingestion point for the actor->job map. Every feed,
        03 lines, party roster, combatants snapshots, shares the same cap so
        no one path can grow the dict past it."""
        if len(self._actor_jobs) > 1024:
            self._actor_jobs.clear()
        self._actor_jobs[aid] = job

    def _on_ws_zone_changed(self, zone_id: int, zone_name: str) -> None:
        """ChangeZone from the WS feed, replayed from cache on subscribe, live
        on zone change. Seeds the Current Instance state when connecting
        mid-instance. _apply_zone dedupes the same-zone repeat every live zone
        change produces, this event plus the 01 log line, and owns the id on
        that path so its same-zone redetect guard can fire. With no name there
        is no zone to apply, but the id alone is all the zone-id cactbot index
        needs, so that resolution runs here instead of waiting for the 30 s
        redetect tick. The id is also retained so a restarted
        Triggernometry sidecar can be fed the zone it missed. Its feed_zone
        connection only fires while the bridge exists."""
        if zone_name:
            self._apply_zone(zone_name, zone_id)
        else:
            self._current_zone_id = zone_id
            # Unchanged is a strict no-op inside, so a nameless replay of
            # the zone already loaded costs nothing. getattr, duck-typed
            # test windows carry only the id.
            if zone_id and getattr(self, "_cactbot_mode", False):
                self._redetect_zone_fight()

    @pyqtSlot(bool, str)
    def _on_status_changed(self, connected: bool, msg: str) -> None:
        self._connected = connected   # drives _toggle_connection, not the button text, which localizes
        if connected:
            self._status_lbl.setText(f"● {msg}")
            self._status_lbl.setStyleSheet("color:#a6e3a1; font-weight:bold;")
            self._conn_btn.setText(_("Disconnect"))
            self._zone_lbl.setText(self._zone_banner_text())
            # The timeline engine survived the feed drop, reset keeps the
            # entries, but the plugin was cleared. Re-push the schedule so
            # a feed hiccup doesn't leave the overlay empty until the next
            # zone.
            self._push_timeline_to_plugin()
            if getattr(self, "_umad_chain_enabled", False):
                # Job backfill for a session started or restarted
                # mid-instance. No 01 zone line or 03 burst will come, but
                # live memory knows the jobs.
                self._ws.request_combatants_once()
        else:
            self._status_lbl.setText(f"● {msg}")
            self._status_lbl.setStyleSheet("color:#f38ba8; font-weight:bold;")
            self._conn_btn.setText(_("Connect"))
            self._zone_lbl.setText(self._zone_banner_text())
            # Feed loss stops the fight clock, timeline.feed_status_changed.
            # The plugin must not keep drawing that dead pull.
            self._plugin_link.send_clear()

    def _set_zone_aliases(self, zone: str, zone_id: int) -> None:
        """Resolve the names this zone matches against. What the feed
        reported, plus its canonical English name when the id is known.
        Shipped patterns are English, so on a localized client only the
        second one can match."""
        canon = canonical_zone_name(zone_id) if zone_id else ""
        self._match_zone = canon or zone
        self._zone_aliases = (zone, canon) if (canon and canon != zone) else (zone,)

    def _zone_banner_text(self) -> str:
        """Current Instance caption. Shows the client's own wording, plus
        the English name when it differs, that is the one patterns match
        against. Connected with no zone yet is its own state. The feed
        only announces a zone on entry, so a mid-instance connect knows
        nothing until the next zone change, and callouts run unfiltered
        until then."""
        if self._current_zone:
            label = f"◉  {self._current_zone}"
            if self._match_zone and self._match_zone != self._current_zone:
                label += f"   ·   {self._match_zone}"
            return label
        if self._connected:
            return _("◉  Instance unknown (connected mid-duty) - callouts are not zone-filtered")
        return _("◉  No instance")

    def _zone_matches(self, rx) -> bool:
        """True if a compiled zone pattern matches the current zone under any of
        its names. Matching both keeps a client-language name that already worked
        working, and adds the English name every shipped pattern is written in."""
        return any(_safe_search(rx, z) for z in self._zone_aliases if z)

    def _apply_zone(self, zone: str, zone_id: int = 0) -> None:
        """Track a zone change. State teardown, fight tag, Current Instance
        label, timeline, and the per-zone Telesto party map. Fed from both
        the 01 log line and the ChangeZone WS event. A live zone change
        delivers both, so a same-zone repeat is a no-op, otherwise the
        ability log would banner the zone twice and the timeline would
        reload needlessly."""
        if zone_id:
            prev_zone_id = self._current_zone_id
            self._current_zone_id = zone_id
        else:
            prev_zone_id = 0
        if zone == self._current_zone:
            # Same zone, but this event may be the one that carries the
            # id, the 01 line and the ChangeZone event arrive in either
            # order. Resolve the English name now if we are still matching
            # on the reported one. A changed id always re-resolves, the old
            # id's canonical name must not keep steering the zone regexes.
            # Re-resolution rebuilds the alias tuple wholesale, so a
            # corrected id cannot pile up extra names.
            if zone_id and (self._current_zone_id != prev_zone_id
                            or len(self._zone_aliases) < 2):
                self._set_zone_aliases(zone, zone_id)
            if zone_id and self._current_zone_id != prev_zone_id:
                # A late or corrected id changes what the zone-id cactbot
                # index maps here. A strict no-op when nothing moved.
                self._redetect_zone_fight()
            return
        self._current_zone = zone
        if not zone_id:
            # The previous zone's id must not leak into this one's lookups.
            self._current_zone_id = 0
        self._set_zone_aliases(zone, zone_id)
        # Zone change. The plugin drops the old schedule and any live
        # alerts. The new zone's schedule arrives from
        # _load_timeline_for_zone below.
        self._plugin_link.send_clear()
        # Drop pending reapply warnings from the previous zone so one can't
        # speak after a zone change. The effect is long gone.
        self._clear_status_timers()
        self._clear_seq_runners()         # same hazard for in-flight sequences
        self._actor_jobs.clear()          # new zone repopulates via 03 lines
        self._umad_actor_names.clear()
        self._umad_chain_reset()
        self._umad_gaze_reset()           # zone change, drop gaze state, party is gone
        self._automark_pairs.reset()      # held pair statuses die with the zone
        self._automark_pending.clear()    # queued rule marks die with it too
        self._automark_active.clear()     # and the placed-by bookkeeping
        if self._umad_chain_enabled:
            # Backfill jobs from live memory in case this session missed
            # the 03 burst, app started mid-instance. Reply merges via the
            # combatants signal.
            self._ws.request_combatants_once()
        self._current_fight_tag, _unused = self._fight_tag_for_zone(self._match_zone)
        self._zone_lbl.setText(self._zone_banner_text())
        self._append_zone_to_ability_log(self._current_zone)
        self._refresh_zone_column()
        self._load_timeline_for_zone(self._match_zone)
        self._refresh_telesto_party()   # party may have changed. Refresh slot map
        if self._mute_until_zone:
            self._mute_btn.setChecked(False)   # clears the until-next-zone mute

    @pyqtSlot(bool, bool)
    def _on_in_combat(self, act: bool, game: bool) -> None:
        was = self._in_game_combat
        self._in_game_combat = game
        # The meter's encounter boundaries follow the combat flags, either
        # edge begins or ends one, see DpsMeter.set_in_combat.
        try:
            self._dps_meter.set_in_combat(act, game)
        except Exception as exc:  # noqa: BLE001 - never break combat tracking
            ac.log_drop("dps-meter", f"in-combat {exc!r}")
        # Feed the timeline the synthetic 260 line, idx 2 = ACT, 3 = game,
        # so InCombat syncs match and a game-combat flip starts the clock.
        # The only start a non-casting target, striking dummy, can produce.
        # Cactbot mode feeds too, its schedule rides the same engine.
        if (getattr(self, "_cactbot_mode", False)
                or (getattr(self, "_local_enabled", True)
                    and getattr(self, "_global_local_on_flag", True))):
            # A pull starting with an empty schedule means the zone's
            # timeline never loaded, mid-instance restart, or a zone
            # replay that arrived before the zone could resolve. Re-arm
            # from the current zone now, or the whole pull goes without
            # timeline callouts. No-op whenever entries are already
            # loaded.
            if game and not was and not self._timeline.upcoming():
                self._load_timeline_for_zone(self._match_zone)
            self._timeline.process_line(
                ["260", "", "1" if act else "0", "1" if game else "0"])
        # Opt-in leave-combat reset, the sample fight. End the run, clear
        # the plugin, and re-arm the schedule so the next engage starts at
        # 0. Real timelines never set the marker because intermissions
        # must not reset.
        if was and not game and self._timeline_reset_on_combat_end:
            self._timeline.reset()
            self._plugin_link.send_clear()
            self._push_timeline_to_plugin()

    @pyqtSlot(str)
    def _on_log_line(self, raw: str) -> None:
        fields = raw.split("|")
        # Capture the full feed verbatim for the Save-log export, before any
        # display filtering. Independent of the triggers master switch.
        self._raw_capture.append(raw)
        try:
            self._dispatch_log_line(fields, raw)
        except Exception as exc:  # noqa: BLE001 - one bad line must not abort its own dispatch
            ac.log_drop("dispatch", f"{exc!r} on {raw[:140]!r}")

    def _dispatch_log_line(self, fields: list[str], raw: str) -> None:
        log_type = fields[0]

        # DPS meter tap. Additive, and a parse bug must never break
        # triggers.
        if log_type in METER_LOG_TYPES:
            try:
                self._dps_meter.process(fields, raw)
            except Exception as exc:  # noqa: BLE001 - same guard as dispatch itself
                ac.log_drop("dps-meter", f"{exc!r} on {raw[:140]!r}")

        # 03 = AddedCombatant, "03|ts|id|name|job|level|...", job is hex.
        # Remember each player's ClassJob so the UMAD chain engine can
        # split DPS from supports. Players only, the '10'-prefixed ids.
        # Keyed by _actor_int so a 26-line target id with different
        # padding or case still resolves. Cleared on zone change. The size
        # cap guards pathological zones, a city streams 03 lines for every
        # passer-by.
        if log_type == "03" and len(fields) > 4 and fields[2][:2] == "10":
            try:
                job = int(fields[4], 16)
            except ValueError:
                job = 0
            aid = _actor_int(fields[2]) if job else None
            if aid is not None:
                self._note_actor_job(aid, job)

        # 02 = ChangePrimaryPlayer, "02|ts|id|name". Tracks the local
        # player so status, 26 and 30, triggers know whose debuffs to call
        # out.
        if log_type == "02" and len(fields) > 3:
            self._me_id = fields[2].strip()      # local player actor id, for the automark self-match
            name = fields[3].strip()
            if name and name != self._me_name:
                self._set_me_name(name)

        if fields[0] == "01" and len(fields) > 3:
            self._apply_zone(fields[3], _hex_id(fields[2]))

        # Tear down pending reapply warnings regardless of the local-enabled
        # gate. An early LosesEffect, 30, for the same effect, source and
        # target cancels the warning, boss death, dispel, overwrite, and a
        # wipe, ActorControl 4000000F, clears all of them. Arming, by
        # contrast, stays gated below.
        if fields[0] == "30":
            self._cancel_status_timers_for_loss(fields)
        elif (fields[0] == "33" and len(fields) > 3
              and fields[3].upper() == "4000000F"):
            # ActorControl, 33. The wipe command is field 3, `command`,
            # not field 4, `data0`. Reading 4 never matched the constant.
            self._clear_status_timers()
            # Wipe and fight end. The plugin drops the schedule and live
            # alerts instead of interpolating a clock that just stopped.
            self._plugin_link.send_clear()
            # The clear takes the meter's end state down with it, so the
            # overlay's hold-last never survived a wipe. When this wipe just
            # closed a pull, re-assert the end after the clear and the held
            # final numbers stay up the way they do after a kill.
            if time.monotonic() - self._dps_last_end < 10.0:
                self._plugin_link.send_dps(None, [], show=False)
            # Re-push the schedule at once. The engine reset keeps its
            # entries, and the re-arm guard in _on_in_combat skips while the
            # schedule is non-empty, so without this the overlay stays blank
            # until the next zone.
            self._push_timeline_to_plugin()
            self._clear_seq_runners()                  # wipe, drop in-flight sequences too
            self._umad_chain_reset(clear_marks=True)   # wipe, void queues and clear stranded signs
            self._umad_gaze_reset(clear_marks=True)    # wipe, void gaze pairs and clear signs
            self._automark_pairs.reset()      # held pair statuses are void too
            self._automark_pending.clear()    # so are queued rule marks
            self._automark_active.clear()     # and the placed-by bookkeeping

        # Keep the compound-rule pair tracker current on every gain and
        # loss of a tracked status, player targets only, independent of the
        # enable toggles. State must be warm when rules switch on, and it
        # must update before the automark match below so the line's own
        # status counts toward "holds both".
        if fields[0] in ("26", "30") and len(fields) > 8 and fields[7].startswith("10"):
            _eff_n = self._norm_hex(fields[2])
            if _eff_n in self._automark_pairs.tracked:
                if fields[0] == "26":
                    self._automark_pairs.on_gain(_eff_n, fields[7], time.monotonic())
                else:
                    self._automark_pairs.on_loss(_eff_n, fields[7])

        # Automark rules run off the Telesto pipeline, independent of the callout
        # toggle. Gated only on automarkers being enabled. 26 lines place signs,
        # 30 lines run the unmark, which clears the sign only when clear-on-loss
        # is on but always purges the fallen debuff's queued retry.
        if (self._automark_rules and fields[0] in ("26", "30")
                and self._settings.get("telesto_enabled")):
            if fields[0] == "26":
                self._match_automark_rules(fields)
            else:
                self._match_automark_unmark(fields)

        # UMAD automarker engines fed off status and cast lines. The P3
        # black-hole chains, Accretion and Crust sequencing, off 26/30, and
        # the P4 Cursed Shriek gaze pairing off 26/30 plus the 20 followup
        # casts that carry each wave's real or fake tell. Each self-gates on
        # its own toggle. Isolated like the dps-meter tap above. A raise
        # here, a role_of callback, a future state-machine regression, must
        # not skip the local-trigger loop and every other trigger that would
        # have fired on this same line.
        if fields[0] in ("26", "30"):
            try:
                self._umad_chain_line(fields)
                self._umad_gaze_line(fields)
            except Exception as exc:  # noqa: BLE001 - same guard as dispatch itself
                ac.log_drop("umad", f"{exc!r} on {raw[:140]!r}")
        elif fields[0] == "20":
            try:
                self._umad_gaze_cast(fields)
            except Exception as exc:  # noqa: BLE001
                ac.log_drop("umad", f"{exc!r} on {raw[:140]!r}")

        # Local, Cactbot, and Triggevent are independent sources and may
        # all fire at once. The Local master toggle and the global kill
        # switch gate only the local engine. Cactbot's schedule lives in
        # the same timeline engine and needs the feed while local triggers
        # are muted, or its bars never advance.
        if (getattr(self, "_cactbot_mode", False)
                or (self._local_enabled
                    and getattr(self, "_global_local_on_flag", True))):
            self._timeline.process_line(fields)
        if self._local_enabled:
            # try_advance invokes _on_seq_complete on the final step, which
            # already removes the runner. Removing again here would
            # ValueError.
            for runner in list(self._seq_runners):
                runner.try_advance(fields)

            deadline = time.monotonic() + _DISPATCH_BUDGET_S
            for t in self._triggers:
                if time.monotonic() > deadline:
                    ac.log_drop("dispatch-budget",
                             f"trigger loop exceeded {_DISPATCH_BUDGET_S:g}s; "
                             f"remaining triggers skipped on {raw[:140]!r}")
                    break
                # The zone lock only applies once we know which zone we are in.
                # Connecting mid-instance gives us neither the 01 line nor a
                # ChangeZone replay, and silently skipping every zone-locked
                # trigger then is indistinguishable from the app being broken.
                # The ability id still has to match, so fail open instead.
                if t.zone_regex and self._zone_aliases:
                    # Cached compile. This runs per trigger per log line,
                    # and 900+ distinct patterns thrash re's tiny internal
                    # cache.
                    rx = compile_user_regex(t.zone_regex, re.IGNORECASE)
                    if rx is None or not self._zone_matches(rx):
                        continue

                m = t.matches(fields, me=self._me_name)
                if m is None:
                    continue

                if t.sequence:
                    for runner in list(self._seq_runners):
                        if runner.trigger is t:
                            self._drop_seq_runner(runner)
                    runner = SequentialRunner(
                        t, m,
                        on_complete=self._on_seq_complete,
                        on_expire=self._on_seq_expire,
                        parent=self,
                    )
                    self._seq_runners.append(runner)
                elif t.expiry_warn_s > 0 and fields[0] == "26":
                    # Don't speak on the gain. Schedule a reapply warning
                    # timed off this effect's own duration. Re-arms on
                    # refresh. The concrete line type is tested, not
                    # t.log_type, matching the cooldown skip in
                    # Trigger.matches, so a piped "26|30" trigger still arms
                    # on the gain line instead of speaking on every refresh.
                    self._arm_status_timer(t, m, fields)
                elif t.expiry_warn_s > 0:
                    # Any other matched line, say the 30 loss of a piped
                    # "26|30" trigger, is swallowed. The loss already
                    # cancelled the armed warning above, speaking now would
                    # call out an effect that is gone.
                    pass
                else:
                    self._fire(t, m)

        self._append_ability_line(fields)

    @staticmethod
    def _status_keys(fields: list[str]) -> tuple[str, str, str]:
        """The effect id, source id, target id triple, upper-cased, from a
        26 or 30 line.
        Layout, type|ts|effectId|effect|duration|srcId|src|tgtId|tgt|count."""
        eff = fields[2].upper() if len(fields) > 2 else ""
        src = fields[5].upper() if len(fields) > 5 else ""
        tgt = fields[7].upper() if len(fields) > 7 else ""
        return eff, src, tgt

    def _drop_status_timer(self, runner: StatusTimerRunner) -> None:
        """Stop a runner, drop our reference, deleteLater its QObject.
        Same lifetime contract as _drop_seq_runner."""
        runner.cancel()
        if runner in self._status_timers:
            self._status_timers.remove(runner)
        runner.deleteLater()

    def _arm_status_timer(self, t: Trigger, captured: dict, fields: list[str]) -> None:
        """Schedule or re-arm a reapply warning for a just-gained effect."""
        try:
            duration = float(fields[4]) if len(fields) > 4 else 0.0
        except ValueError:
            return
        eff, src, tgt = self._status_keys(fields)
        key = (t.id, eff, src, tgt)
        # A refresh of the same effect re-arms. Cancel the prior timer first, so
        # a refresh that no longer warrants a warning still tears down the stale
        # one instead of leaving it armed.
        for r in list(self._status_timers):
            if r.key == key:
                self._drop_status_timer(r)
        # Permanent or unknown duration, or already inside the warning
        # window at apply time. Both would arm a 0-delay timer that speaks
        # on the gain. isfinite also drops a nan or inf duration from a
        # malformed feed line, which slips a plain `<= 0` guard and then
        # blows up the delay-to-int conversion in the timer.
        if not math.isfinite(duration) or duration <= 0 or duration <= t.expiry_warn_s:
            return
        # Clamp so a multi-day duration can't overflow QTimer's int32 interval.
        delay_ms = min((duration - t.expiry_warn_s) * 1000.0, float(2**31 - 1))
        runner = StatusTimerRunner(t, captured, eff, src, tgt, delay_ms,
                                   on_complete=self._on_status_timer, parent=self)
        self._status_timers.append(runner)

    def _on_status_timer(self, runner: StatusTimerRunner, captured: dict) -> None:
        t = runner.trigger
        self._drop_status_timer(runner)
        # Suppress if the world moved on during the countdown. Local
        # triggers off, trigger disabled, edited, the list holds a
        # different object, or deleted. Identity, the `is` check, not
        # equality, so an edit that produced a value-equal object still
        # counts as gone.
        if (not self._local_enabled or not t.enabled
                or not any(x is t for x in self._triggers)):
            return
        # Collapse an AoE burst into one callout. The gain path skips the
        # trigger cooldown so timers re-arm on refresh, so gate it here per
        # effect id. Shares _last_fired with matches. No write race since
        # matches skips its cooldown for exactly these 26 triggers.
        if t.cooldown_s > 0:
            now = time.monotonic()
            if now - t._last_fired.get(runner.effect_id, 0.0) < t.cooldown_s:
                return
            t._last_fired[runner.effect_id] = now
        self._fire(t, captured)

    def _cancel_status_timers_for_loss(self, fields: list[str]) -> None:
        eff, src, tgt = self._status_keys(fields)
        for r in list(self._status_timers):
            if r.matches_loss(eff, src, tgt):
                self._drop_status_timer(r)

    def _clear_status_timers(self) -> None:
        for r in list(self._status_timers):
            self._drop_status_timer(r)

    def _ability_line_visible(self, entry: dict) -> bool:
        if entry.get("is_zone"):
            return True
        log_type  = entry["log_type"]
        is_player = entry["is_player"]
        if is_player and not self._cb_players.isChecked():
            return False
        if not is_player and not self._cb_enemies.isChecked():
            return False
        if log_type == "20" and not self._cb_casts.isChecked():
            return False
        if log_type in ("21", "22") and not self._cb_abilities.isChecked():
            return False
        if log_type == "23" and not self._cb_cancels.isChecked():
            return False
        if log_type == "26" and not self._cb_statuses.isChecked():
            return False
        return True

    def _refilter_ability_log(self) -> None:
        ftext = self._ability_filter_edit.text().strip().lower()
        self._ability_log.clear()
        for entry in self._ability_buffer:
            if entry.get("is_zone"):
                self._write_zone_line(entry["line"])
            elif self._ability_line_visible(entry):
                if not ftext or ftext in entry["line"].lower():
                    self._write_ability_line(
                        entry["line"], entry["color"],
                        entry["log_type"], entry["ability_name"],
                        entry.get("ability_id", ""),
                        entry.get("source", ""), entry.get("target", ""),
                    )

    def _fight_tag_for_zone(self, zone: str) -> tuple[str, str]:
        """Return the fight_tag, zone_regex pair for zone, or an empty tag
        plus escaped_zone if unrecognised."""
        if not zone:
            return "", ""
        seen: dict[str, str] = {}
        for t in self._triggers:
            if t.fight and t.zone_regex and t.fight not in seen:
                seen[t.fight] = t.zone_regex
        for fight, zrx in seen.items():
            # Same guards as the per-log-line zone gate. Reject over-long
            # and catastrophic shapes at compile, wall-clock-timeout the
            # match. This runs on the 30 s redetect tick, so a raw
            # re.search over user zone_regex values could freeze the GUI
            # thread.
            rx = compile_user_regex(zrx, re.IGNORECASE)
            if rx is None:
                continue
            if _safe_search(rx, zone):
                return fight, zrx
        return "", re.escape(zone)

    def _poll_zone_and_triggers(self) -> None:
        """30 s housekeeping tick. Hot-reload trigger files that changed
        on disk, then re-resolve the current fight in case the trigger
        set, or the zone's resolution, moved under a running session."""
        self._maybe_reload_triggers()
        self._redetect_zone_fight()

    def _redetect_zone_fight(self) -> None:
        """Reload the timeline when the current zone's resolved fight no longer
        matches the loaded one, e.g. a hot-reloaded zone_regex fix, a zone id
        arriving after the name, or a content reload changing resolution.
        Unchanged is a strict no-op."""
        # The cached fight tag tracks the LOCAL name-regex resolution, it
        # drives fight-folder prefill and UMAD-specific rules, never the
        # cactbot index tag, which only names the timeline cache file.
        self._current_fight_tag = (self._fight_tag_for_zone(self._match_zone)[0]
                                   if self._match_zone else "")
        fight = self._timeline_fight_tag(self._match_zone)
        if fight == self._timeline_fight:
            return
        self._load_timeline_for_zone(self._match_zone)

    def _append_zone_to_ability_log(self, zone_name: str) -> None:
        line = f"── {zone_name} ──" if zone_name else "── (No zone) ──"
        entry: dict = {"is_zone": True, "line": line}
        self._ability_buffer.append(entry)
        self._write_zone_line(line)

    def _write_zone_line(self, line: str) -> None:
        cursor = self._ability_log.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        if cursor.position() > 0:
            cursor.insertBlock()
        fmt = QTextCharFormat()
        fmt.setForeground(QBrush(QColor("#cba6f7")))
        cursor.setCharFormat(fmt)
        cursor.insertText(line)
        self._ability_log.setTextCursor(cursor)
        self._ability_log.ensureCursorVisible()

    def _append_ability_line(self, fields: list[str]) -> None:
        if not fields or (fields[0] not in _ABILITY_TYPES and fields[0] != "26"):
            return

        # GainsEffect. Shows a debuff or DoT going up, with the status id,
        # not the cast id, so a reapply trigger can be made straight from
        # the line. The status id is field 2, the applier is the source,
        # unlike ability lines.
        if fields[0] == "26":
            if len(fields) < 9:
                return
            ts        = fields[1][11:19] if len(fields[1]) >= 19 else "??:??:??"
            status_id = fields[2]
            status    = fields[3]
            src       = fields[6]
            tgt       = fields[8]
            is_player = fields[5].startswith("10")
            try:
                dur = f"  ({float(fields[4]):.1f}s)"
            except (ValueError, IndexError):
                dur = ""
            line = f"{ts}  {src}  applies  {status}  to {tgt}{dur}  [{status_id}]"
            color = "#a8d8a8" if is_player else "#f4a261"
            entry = {"log_type": "26", "is_player": is_player,
                     "line": line, "color": color, "ability_name": status,
                     "ability_id": status_id, "source": src, "target": tgt}
            self._ability_buffer.append(entry)
            if self._ability_line_visible(entry):
                ftext = self._ability_filter_edit.text().strip().lower()
                if not ftext or ftext in line.lower():
                    self._write_ability_line(line, color, "26", status, status_id, src, tgt)
            return

        if len(fields) < 6:
            return

        log_type   = fields[0]
        is_player  = fields[2].startswith("10")
        ts         = fields[1][11:19] if len(fields[1]) >= 19 else "??:??:??"
        src        = fields[3]
        ability_id = fields[4] if len(fields) > 4 else ""
        ability    = fields[5]

        if log_type == "20":
            raw_ct = fields[8] if len(fields) > 8 else ""
            try:
                ct = f" ({float(raw_ct):.1f}s)"
            except (ValueError, IndexError):
                ct = ""
            line = f"{ts}  {src}  begins casting  {ability}{ct}  [{ability_id}]"
        elif log_type in ("21", "22"):
            target = fields[7] if len(fields) > 7 else ""
            aoe    = "  (AOE)" if log_type == "22" else ""
            line   = f"{ts}  {src}  uses  {ability}  on {target}{aoe}  [{ability_id}]" if target and target != src \
                     else f"{ts}  {src}  uses  {ability}{aoe}  [{ability_id}]"
        else:
            line = f"{ts}  {src}  cancels  {ability}  [{ability_id}]"

        # Only 21/22 carry a meaningful target. Gate capture to those so prefill
        # doesn't bake "on ..." onto casts/cancels.
        target_name = fields[7].strip() if (log_type in ("21", "22") and len(fields) > 7) else ""
        color = "#a8d8a8" if is_player else "#f4a261"
        entry = {"log_type": log_type, "is_player": is_player,
                 "line": line, "color": color, "ability_name": ability, "ability_id": ability_id,
                 "source": src, "target": target_name}

        self._ability_buffer.append(entry)

        if self._ability_line_visible(entry):
            ftext = self._ability_filter_edit.text().strip().lower()
            if not ftext or ftext in line.lower():
                self._write_ability_line(line, color, log_type, ability, ability_id, src, target_name)

    def _on_ability_context_menu(self, pos) -> None:
        cursor = self._ability_log.cursorForPosition(pos)
        block  = cursor.block()
        data   = block.userData()
        if not isinstance(data, _AbilityData):
            return

        sel = self._ability_log.textCursor()
        sel.setPosition(block.position())
        sel.movePosition(QTextCursor.MoveOperation.EndOfBlock, QTextCursor.MoveMode.KeepAnchor)
        self._ability_log.setTextCursor(sel)

        menu   = QMenu(self._ability_log)
        action = menu.addAction(_("Create Trigger from this"))
        chosen = menu.exec(self._ability_log.viewport().mapToGlobal(pos))

        if chosen is action:
            fight, zone_rx = self._fight_tag_for_zone(self._match_zone)
            name, tts = _prefill_name_tts(data.ability_name, data.source, data.target,
                                          me=self._me_name)
            pre = Trigger(
                log_type=data.log_type,
                ability_id=data.ability_id,
                name=name,
                tts_text=tts,
                fight=fight,
                zone_regex=zone_rx,
            )
            self._create_trigger_from_prefill(pre)
