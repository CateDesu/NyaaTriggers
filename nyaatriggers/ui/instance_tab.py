"""Live log display, zone handling and status timers for MainWindow."""

import math
import re
import time

from PyQt6.QtCore import Qt, pyqtSlot
from PyQt6.QtGui import QBrush, QColor, QTextCharFormat, QTextCursor
from PyQt6.QtWidgets import QMenu

from nyaatriggers.trigger_engine import Trigger, compile_user_regex, _safe_search
from nyaatriggers.locale_util import _
from nyaatriggers.sequential import SequentialRunner
from nyaatriggers.status_timer import StatusTimerRunner
from nyaatriggers.telesto_client import _actor_int
from nyaatriggers.dps_meter import METER_LOG_TYPES

from nyaatriggers import app_common as ac
from nyaatriggers.app_common import (
    _ABILITY_TYPES, _AbilityData, _C_EN, _C_ZONE, _DISPATCH_BUDGET_S, _DOT_GREEN, _DOT_GREY, _DOT_RED, _hex_id, _prefill_name_tts, canonical_zone_name,
)


# These Dancing Mad basic attacks have no name in the game data.
_ABILITY_NAME_FALLBACKS = {0xC250: "Attack", 0xC252: "Attack"}


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
        """Clear a player's sign using the same routing as marking. force permits cleanup
        while automarkers are disabled.
        """
        tc = self._telesto_client
        if tc is None:
            return False
        if self._is_me_actor(actor_id, name):
            return tc.clear_self(force=force)
        return tc.clear_actor(actor_id, force=force)

    def _note_actor_job(self, aid: int, job: int) -> None:
        """Update the shared actor job map within its size limit."""
        if len(self._actor_jobs) > 1024:
            self._actor_jobs.clear()
        self._actor_jobs[aid] = job

    def _on_ws_zone_changed(self, zone_id: int, zone_name: str) -> None:
        """Apply cached or live zone metadata. Resolve ID only updates immediately and
        retain the ID for sidecar restarts. _apply_zone handles duplicate name events.
        """
        if zone_name:
            self._apply_zone(zone_name, zone_id)
        else:
            track_zone = getattr(self, "_track_activity_zone", None)
            if track_zone is not None:
                track_zone("", zone_id)
            self._current_zone_id = zone_id
            if zone_id and getattr(self, "_cactbot_mode", False):
                self._redetect_zone_fight()

    @pyqtSlot(bool, str)
    def _on_status_changed(self, connected: bool, msg: str) -> None:
        self._connected = connected   # Connection state is independent of translated button text.
        if connected:
            self._status_lbl.setText(f"● {msg}")
            self._status_lbl.setStyleSheet("color:#a6e3a1; font-weight:bold;")
            self._conn_btn.setText(_("Disconnect"))
            self._zone_lbl.setText(self._zone_banner_text())
            # Restore the plugin schedule after reconnect because feed loss clears its
            # display.
            self._push_timeline_to_plugin()
            if getattr(self, "_umad_chain_enabled", False):
                # Backfill jobs from live memory when connecting midfight.
                self._ws.request_combatants_once()
        else:
            self._status_lbl.setText(f"● {msg}")
            self._status_lbl.setStyleSheet("color:#f38ba8; font-weight:bold;")
            self._conn_btn.setText(_("Connect"))
            self._zone_lbl.setText(self._zone_banner_text())
            # Cancel warnings whose loss or wipe events can no longer arrive.
            self._clear_status_timers()
            self._clear_seq_runners()
            meter = getattr(self, "_dps_meter", None)
            if meter is not None:
                # Close the pull and reset the combat edge so reconnect can start a new
                # encounter.
                meter.feed_lost()
            # Finalize before clearing so its last frame cannot restore disconnected DPS.
            self._plugin_link.send_clear()

    def _set_zone_aliases(self, zone: str, zone_id: int) -> None:
        """Return the reported zone name and its canonical English alias when available.
        """
        canon = canonical_zone_name(zone_id) if zone_id else ""
        self._match_zone = canon or zone
        self._zone_aliases = (zone, canon) if (canon and canon != zone) else (zone,)

    def _zone_banner_text(self) -> str:
        """Show the client zone name and a differing English alias. Distinguish a connected
        feed whose zone is still unknown.
        """
        if self._current_zone:
            label = f"◉  {self._current_zone}"
            if self._match_zone and self._match_zone != self._current_zone:
                label += f"   ·   {self._match_zone}"
            return label
        if self._connected:
            return _("◉  Instance unknown (connected mid-duty) - callouts are not zone-filtered")
        return _("◉  No instance")

    def _zone_matches(self, rx) -> bool:
        """Match a compiled zone pattern against every current name."""
        return any(_safe_search(rx, z) for z in self._zone_aliases if z)

    def _apply_zone(self, zone: str, zone_id: int = 0, *, raw_zone: bool = False) -> None:
        """Apply a zone change and reset its state. Duplicate reports from log and metadata
        events do not repeat teardown.
        """
        track_zone = getattr(self, "_track_activity_zone", None)
        if track_zone is not None:
            track_zone(zone, zone_id)
        if zone_id:
            prev_zone_id = self._current_zone_id
            self._current_zone_id = zone_id
        else:
            prev_zone_id = 0
        meter = getattr(self, "_dps_meter", None)
        if meter is not None:
            meter.set_zone_metadata(zone)
        if raw_zone:
            # Raw zone boundaries clear DPS even when the zone name repeats.
            self._plugin_link.send_clear()
        if zone == self._current_zone:
            # The zone ID and name can arrive in either order. Rebuild aliases when a
            # late or corrected ID changes the canonical name.
            if zone_id and (self._current_zone_id != prev_zone_id
                            or len(self._zone_aliases) < 2):
                self._set_zone_aliases(zone, zone_id)
            if zone_id and self._current_zone_id != prev_zone_id:
                # A changed ID can also select a different cactbot timeline.
                self._redetect_zone_fight()
            if raw_zone:
                self._push_timeline_to_plugin()
            return
        self._current_zone = zone
        if not zone_id:
            self._current_zone_id = 0
        self._set_zone_aliases(zone, zone_id)
        if not raw_zone:
            self._plugin_link.send_clear()
        self._clear_status_timers()
        self._clear_seq_runners()
        for trigger in getattr(self, "_triggers", ()):
            trigger._last_fired.clear()
        self._actor_jobs.clear()
        self._umad_actor_names.clear()
        self._umad_chain_reset()
        self._umad_gaze_reset()
        self._automark_pairs.reset()
        self._automark_pending.clear()
        self._automark_active.clear()
        if self._umad_chain_enabled:
            # Backfill jobs if the session missed initial combatant lines.
            self._ws.request_combatants_once()
        self._current_fight_tag, _unused = self._fight_tag_for_zone(self._match_zone)
        self._zone_lbl.setText(self._zone_banner_text())
        self._append_zone_to_ability_log(self._current_zone)
        self._refresh_zone_column()
        self._load_timeline_for_zone(self._match_zone)
        self._refresh_telesto_party()
        if self._mute_until_zone:
            self._mute_btn.setChecked(False)

    @pyqtSlot(bool, bool)
    def _on_in_combat(self, act: bool, game: bool) -> None:
        was = self._in_game_combat
        self._in_game_combat = game
        track_combat = getattr(self, "_track_combat", None)
        if track_combat is not None:
            track_combat(act, game)
        try:
            self._dps_meter.set_in_combat(act, game)
        except Exception as exc:  # noqa: BLE001
            ac.log_drop("dps-meter", f"in-combat {exc!r}")
        finish_activity = getattr(self, "_finish_activity_event", None)
        if finish_activity is not None:
            try:
                finish_activity()
            except Exception as exc:
                ac.log_drop("session-tracking", f"{exc!r}")
        # Feed synthetic combat state to start timelines even for targets that never
        # cast. Cactbot schedules use the same engine.
        if (getattr(self, "_cactbot_mode", False)
                or (getattr(self, "_local_enabled", True)
                    and getattr(self, "_global_local_on_flag", True))):
            # Try loading an empty schedule at combat start in case zone resolution was
            # delayed.
            if game and not was and not self._timeline.upcoming():
                self._load_timeline_for_zone(self._match_zone)
            self._timeline.process_line(
                ["260", "", "1" if act else "0", "1" if game else "0"])
        # Sample timelines can opt into reset on combat end. Real fight intermissions
        # must preserve the clock.
        if was and not game and self._timeline_reset_on_combat_end:
            self._timeline.reset()
            self._plugin_link.send_clear(keep_dps=True)
            self._push_timeline_to_plugin()

    @pyqtSlot(str)
    def _on_log_line(self, raw: str) -> None:
        fields = raw.split("|")
        # Capture the full feed before display filtering, regardless of the trigger
        # switch.
        self._raw_capture.append(raw)
        try:
            self._dispatch_log_line(fields, raw)
        except Exception as exc:  # noqa: BLE001
            ac.log_drop("dispatch", f"{exc!r} on {raw[:140]!r}")

    def _dispatch_log_line(self, fields: list[str], raw: str) -> None:
        log_type = fields[0]
        begin_activity = getattr(self, "_begin_activity_event", None)
        event_time = begin_activity() if begin_activity is not None else None

        # Keep meter parsing failures from interrupting triggers.
        if log_type in METER_LOG_TYPES:
            try:
                if event_time is None:
                    self._dps_meter.process(fields, raw)
                else:
                    self._dps_meter.process(fields, raw, now=event_time)
            except Exception as exc:  # noqa: BLE001
                ac.log_drop("dps-meter", f"{exc!r} on {raw[:140]!r}")

        track_activity = getattr(self, "_track_activity_line", None)
        if track_activity is not None:
            try:
                track_activity(fields)
            except Exception as exc:
                ac.log_drop("session-tracking", f"{exc!r}")

        # Cache player jobs from AddedCombatant lines. Normalize actor IDs for status
        # matching and bound the cache in busy zones.
        if log_type == "03" and len(fields) > 4 and fields[2][:2] == "10":
            try:
                job = int(fields[4], 16)
            except ValueError:
                job = 0
            aid = _actor_int(fields[2]) if job else None
            if aid is not None:
                self._note_actor_job(aid, job)

        if log_type == "02" and len(fields) > 3:
            self._me_id = fields[2].strip()
            name = fields[3].strip()
            if name and name != self._me_name:
                self._set_me_name(name)

        if fields[0] == "01" and len(fields) > 3:
            self._apply_zone(fields[3], _hex_id(fields[2]), raw_zone=True)

        # Cancel pending warnings on loss or wipe regardless of the local trigger
        # switch. Arming remains gated below.
        if fields[0] == "30":
            self._cancel_status_timers_for_loss(fields)
        elif (fields[0] == "33" and len(fields) > 3
              and fields[3].upper() == "4000000F"):
            # ActorControl stores the wipe command at field 3, before data0.
            self._clear_status_timers()
            # Empty pulls do not reach the meter's encounter end callback.
            for trigger in self._triggers:
                trigger._last_fired.clear()
            self._plugin_link.send_clear(keep_dps=True)
            # Restore the retained schedule immediately so the next pull has bars.
            self._push_timeline_to_plugin()
            self._clear_seq_runners()
            self._umad_chain_reset(clear_marks=True)
            self._umad_gaze_reset(clear_marks=True)
            self._automark_pairs.reset()
            self._automark_pending.clear()
            self._automark_active.clear()

        # Update compound status state before matching and regardless of toggles so
        # enabling midfight works.
        if fields[0] in ("26", "30") and len(fields) > 8 and fields[7].startswith("10"):
            _eff_n = self._norm_hex(fields[2])
            if _eff_n in self._automark_pairs.tracked:
                if fields[0] == "26":
                    self._automark_pairs.on_gain(_eff_n, fields[7], time.monotonic())
                else:
                    self._automark_pairs.on_loss(_eff_n, fields[7])

        # Route marker gains and losses independently of callout mode. Losses always
        # cancel pending retries.
        if (self._automark_rules and fields[0] in ("26", "30")
                and self._settings.get("telesto_enabled")):
            if fields[0] == "26":
                self._match_automark_rules(fields)
            else:
                self._match_automark_unmark(fields)

        # Isolate each automarker engine so its failure cannot skip local triggers on
        # the same line.
        if fields[0] in ("26", "30"):
            try:
                self._umad_chain_line(fields)
                self._umad_gaze_line(fields)
            except Exception as exc:  # noqa: BLE001
                ac.log_drop("umad", f"{exc!r} on {raw[:140]!r}")
        elif fields[0] == "20":
            try:
                self._umad_gaze_cast(fields)
            except Exception as exc:  # noqa: BLE001
                ac.log_drop("umad", f"{exc!r} on {raw[:140]!r}")

        # Cactbot timelines still need log input while local callouts are disabled.
        if (getattr(self, "_cactbot_mode", False)
                or (self._local_enabled
                    and getattr(self, "_global_local_on_flag", True))):
            self._timeline.process_line(fields)
        if self._local_enabled:
            # Completion already removes the sequence runner.
            for runner in list(self._seq_runners):
                runner.try_advance(fields)

            deadline = time.monotonic() + _DISPATCH_BUDGET_S
            for t in self._triggers:
                if time.monotonic() > deadline:
                    ac.log_drop("dispatch-budget",
                             f"trigger loop exceeded {_DISPATCH_BUDGET_S:g}s; "
                             f"remaining triggers skipped on {raw[:140]!r}")
                    break
                # Allow zone filters before the zone is known. The ability matcher still
                # applies.
                if t.zone_regex and self._zone_aliases:
                    # Use the shared compile cache for repeated checks across many
                    # patterns.
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
                    # Arm expiry warnings on concrete gain lines and refresh their
                    # timers. Pipe alternatives use the incoming type.
                    self._arm_status_timer(t, m, fields)
                elif t.expiry_warn_s > 0:
                    # Loss lines already cancelled the warning and must not speak it.
                    pass
                else:
                    self._fire(t, m)

        self._append_ability_line(fields)

    @staticmethod
    def _status_keys(fields: list[str]) -> tuple[str, str, str]:
        """Extract normalized effect, source and target IDs from a status line."""
        eff = fields[2].upper() if len(fields) > 2 else ""
        src = fields[5].upper() if len(fields) > 5 else ""
        tgt = fields[7].upper() if len(fields) > 7 else ""
        return eff, src, tgt

    def _drop_status_timer(self, runner: StatusTimerRunner) -> None:
        """Stop and delete a status runner."""
        runner.cancel()
        if runner in self._status_timers:
            self._status_timers.remove(runner)
        runner.deleteLater()

    def _arm_status_timer(self, t: Trigger, captured: dict, fields: list[str]) -> None:
        try:
            duration = float(fields[4]) if len(fields) > 4 else 0.0
        except ValueError:
            return
        eff, src, tgt = self._status_keys(fields)
        key = (t.id, eff, src, tgt)
        # Cancel the old timer before validating a refresh, even if no new warning is
        # needed.
        for r in list(self._status_timers):
            if r.key == key:
                self._drop_status_timer(r)
        # Reject unknown, nonfinite or already expiring durations instead of speaking
        # immediately.
        if not math.isfinite(duration) or duration <= 0 or duration <= t.expiry_warn_s:
            return
        # Keep the delay within QTimer's integer range.
        delay_ms = min((duration - t.expiry_warn_s) * 1000.0, float(2**31 - 1))
        runner = StatusTimerRunner(t, captured, eff, src, tgt, delay_ms,
                                   on_complete=self._on_status_timer, parent=self)
        self._status_timers.append(runner)

    def _on_status_timer(self, runner: StatusTimerRunner, captured: dict) -> None:
        t = runner.trigger
        self._drop_status_timer(runner)
        # Check mode and object identity before firing so disabled, replaced or deleted
        # triggers cannot speak.
        if (not self._local_enabled or not t.enabled
                or not any(x is t for x in self._triggers)):
            return
        # Apply cooldown when the warning fires. Gains bypass it so refreshes can rearm
        # timers.
        if t.cooldown_s > 0:
            now = time.monotonic()
            cooldown_key = t.cooldown_key(runner.effect_id)
            last_fired = t._last_fired.get(cooldown_key)
            if last_fired is not None and now - last_fired < t.cooldown_s:
                return
            t._last_fired[cooldown_key] = now
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
        """Resolve a fight tag and zone pattern, falling back to an escaped zone name.
        """
        if not zone:
            return "", ""
        seen: dict[str, str] = {}
        for t in self._triggers:
            if t.fight and t.zone_regex and t.fight not in seen:
                seen[t.fight] = t.zone_regex
        for fight, zrx in seen.items():
            # Use the same bounded regex matching as log dispatch to avoid blocking the
            # interface.
            rx = compile_user_regex(zrx, re.IGNORECASE)
            if rx is None:
                continue
            if _safe_search(rx, zone):
                return fight, zrx
        return "", re.escape(zone)

    def _poll_zone_and_triggers(self) -> None:
        """Reload changed trigger files and recheck current fight resolution."""
        self._maybe_reload_triggers()
        self._redetect_zone_fight()

    def _redetect_zone_fight(self) -> None:
        """Reload the timeline only when the resolved fight changes."""
        # Keep the local fight tag separate from the cactbot timeline cache tag.
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

        # Status gains expose the effect ID for creating expiry reminders.
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
        if not ability.strip() or ac._UNKNOWN_NAME_RE.fullmatch(ability):
            ability = _ABILITY_NAME_FALLBACKS.get(_hex_id(ability_id), ability)

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

        # Only ability lines 21 and 22 carry a target suitable for prefill.
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
