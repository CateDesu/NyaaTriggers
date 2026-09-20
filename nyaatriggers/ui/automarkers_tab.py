"""Automarker controls, UMAD routing and Telesto retry queues for MainWindow."""

import time
import urllib.parse

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QListWidget, QListWidgetItem, QHBoxLayout, QPushButton, QLineEdit, QLabel,
)

from nyaatriggers.locale_util import _
from nyaatriggers.telesto_client import (
    TelestoClient, MARKERS as TELESTO_MARKERS, MARKER_TOKENS as TELESTO_MARKER_TOKENS, _actor_int,
)
from nyaatriggers.umad_chains import (
    BlackHoleChains, RELEVANT_IDS as _UMAD_CHAIN_IDS, role_for_job, StatusPairs,
    parse_compound as _parse_compound, canon_status_key as _canon_status,
    CursedShriekPairs, GAZE_FOLLOWUP_IDS as _UMAD_GAZE_FOLLOWUP_IDS,
)

from nyaatriggers import app_common as ac
from nyaatriggers.app_common import (
    DEFAULT_TELESTO_URI, _UMAD_AUTOMARK_PRESET, _UMAD_FIGHT_TAG, _UMAD_FIGHT_TAG_CF, _UMAD_STATUS_LABELS,
)


class AutomarkersTabMixin:
    def _init_automarkers(self) -> None:
        # Start the native Telesto client so marker tests and connection status work
        # immediately.
        self._telesto_client = TelestoClient(
            uri=self._settings.get("telesto_uri", DEFAULT_TELESTO_URI),
            enabled=bool(self._settings.get("telesto_enabled", False)))
        self._telesto_client.status_changed.connect(self._on_telesto_client_status)
        self._telesto_client.start()
        QTimer.singleShot(1200, self._telesto_client.ping)
        # Refresh party slots while automarkers are enabled. Zone changes refresh them
        # immediately.
        self._telesto_party_timer = QTimer(self)
        self._telesto_party_timer.setInterval(10_000)
        self._telesto_party_timer.timeout.connect(self._refresh_telesto_party)
        self._telesto_party_timer.start()
        # Load and coerce saved rule fields before matching or displaying them.
        raw_rules = self._settings.get("automark_rules")
        self._automark_rules = [
            {**r,
             "fight": str(r.get("fight") or ""),
             "status": str(r.get("status") or ""),
             "marker": str(r.get("marker") or ""),
             "scope": str(r.get("scope") or "self")}
            for r in raw_rules if isinstance(r, dict)] \
            if isinstance(raw_rules, list) else []
        self._automark_cooldowns: dict = {}   # statusKey, target, marker to last fired monotonic time
        # Retry marks whose party slots were unknown, retaining their age and status
        # key.
        self._automark_pending: list = []
        # Track which status placed each sign so only its own loss can clear it.
        self._automark_clear_on_loss = bool(self._settings.get("automark_clear_on_loss", True))
        self._automark_active: dict = {}
        # Keep compound status tracking active even with rules disabled so enabling
        # midfight has current state.
        self._automark_pairs = StatusPairs(
            p
            for token in ([h for h, _unused in _UMAD_AUTOMARK_PRESET]
                          + [str(r.get("status") or "") for r in self._automark_rules])
            for p in (_parse_compound(token) or ()))
        # Cache UMAD chain roles from combatant lines, the party roster and live
        # snapshots. Clear them on zone changes.
        self._actor_jobs: dict[int, int] = {}        # _actor_int value to ClassJob id
        self._umad_actor_names: dict[int, str] = {}  # _actor_int value to name, chain targets only
        self._umad_chain_enabled = bool(self._settings.get("umad_chain_enabled", False))
        self._umad_chains = BlackHoleChains(
            role_of=lambda aid: role_for_job(self._actor_jobs.get(_actor_int(aid))),
            markers=self._umad_chain_markers_from_settings())
        # Retry chain actions with unknown party slots to keep visible signs aligned
        # with engine state.
        self._umad_chain_pending: list = []
        # Track enqueue times separately without changing action tuples.
        self._umad_chain_pending_since: dict = {}
        # Debounce incomplete chain queues after each relevant status event.
        self._umad_chain_flush_timer = QTimer(self)
        self._umad_chain_flush_timer.setSingleShot(True)
        self._umad_chain_flush_timer.setInterval(1200)
        self._umad_chain_flush_timer.timeout.connect(self._on_umad_chain_flush)
        # UMAD gaze pairs use separate state and retries with the same Telesto transport
        # as chains.
        self._umad_gaze_enabled = bool(self._settings.get("umad_gaze_enabled", False))
        self._umad_gaze = CursedShriekPairs(
            markers=self._umad_gaze_markers_from_settings(),
            slot_of=self._gaze_slot_of)
        self._umad_gaze_pending: list = []
        self._umad_gaze_pending_since: dict = {}   # enqueue times, see _umad_chain_pending_since
        self._umad_gaze_flush_timer = QTimer(self)
        self._umad_gaze_flush_timer.setSingleShot(True)
        self._umad_gaze_flush_timer.setInterval(1200)
        self._umad_gaze_flush_timer.timeout.connect(self._on_umad_gaze_flush)
        # Backfill jobs through party updates and combatant snapshots.
        self._ws.party_jobs.connect(self._on_ws_party_jobs)
        self._ws.combatants.connect(self._on_ws_combatants_jobs)

    def _build_automark_settings(self, layout) -> None:
        """Build marker controls using the native Telesto client. Rules target self or a
        resolved party slot on status gain. Test and Clear can send while automarkers
        are disabled.
        """
        testing_note = QLabel(_("These automarkers need testing, please let me know."))
        testing_note.setWordWrap(True)
        testing_note.setStyleSheet("color: #8f8f9a; font-size: 11px;")
        layout.addWidget(testing_note)

        self._settings_header(layout, _("Connection"))
        row = QHBoxLayout()
        self._automark_cb = QCheckBox(_("Enable automarkers"))
        self._automark_cb.setChecked(bool(self._settings.get("telesto_enabled", False)))
        self._automark_cb.toggled.connect(self._on_automark_toggled)
        row.addWidget(self._automark_cb)
        self._automark_status_lbl = QLabel(_("● Off"))
        self._automark_status_lbl.setStyleSheet("color:#8f8f9a; font-weight:bold;")
        row.addWidget(self._automark_status_lbl)
        row.addStretch()
        layout.addLayout(row)

        uri_row = QHBoxLayout()
        uri_row.addWidget(QLabel(_("Telesto URL:")))
        telesto_uri = self._settings.get("telesto_uri")
        self._automark_uri_edit = QLineEdit(
            telesto_uri if isinstance(telesto_uri, str) else DEFAULT_TELESTO_URI)
        self._automark_uri_edit.editingFinished.connect(self._on_automark_uri_changed)
        uri_row.addWidget(self._automark_uri_edit)
        layout.addLayout(uri_row)

        test_row = QHBoxLayout()
        test_row.addWidget(QLabel(_("Marker:")))
        self._automark_test_combo = self._make_marker_combo(width=140)
        test_row.addWidget(self._automark_test_combo)
        test_btn = QPushButton(_("Test mark (on me)"))
        test_btn.clicked.connect(self._on_automark_test)
        test_row.addWidget(test_btn)
        clear_btn = QPushButton(_("Clear"))
        clear_btn.setMaximumWidth(80)
        clear_btn.clicked.connect(self._on_automark_clear)
        test_row.addWidget(clear_btn)
        test_row.addStretch()
        layout.addLayout(test_row)

        self._settings_header(layout, _("Rules"))
        self._automark_rules_list = QListWidget()
        self._automark_rules_list.setMaximumHeight(150)
        layout.addWidget(self._automark_rules_list)

        rm_row = QHBoxLayout()
        rm_row.addWidget(QLabel(_("Marker:")))
        # Unassigned rules remain inactive until a marker is chosen.
        self._automark_assign_combo = QComboBox()
        self._automark_assign_combo.addItem(_("(unassigned)"), "")
        for _label, _tok in TELESTO_MARKERS:
            self._automark_assign_combo.addItem(_(_label), _tok)
        self._automark_assign_combo.setMaximumWidth(140)
        self._automark_assign_combo.setEnabled(False)
        self._automark_assign_combo.activated.connect(self._on_automark_assign_marker)
        rm_row.addWidget(self._automark_assign_combo)
        rm_btn = QPushButton(_("Remove"))
        rm_btn.setMaximumWidth(90)
        rm_btn.clicked.connect(self._on_automark_remove_rule)
        rm_row.addWidget(rm_btn)
        rm_row.addStretch()
        layout.addLayout(rm_row)
        self._automark_rules_list.currentRowChanged.connect(
            self._on_automark_rule_selected)

        preset_row = QHBoxLayout()
        umad_btn = QPushButton(_("Load UMAD preset"))
        umad_btn.clicked.connect(self._on_automark_load_umad_preset)
        preset_row.addWidget(umad_btn)
        clear_marks_btn = QPushButton(_("Clear all party marks"))
        clear_marks_btn.clicked.connect(self._on_automark_clear_all)
        preset_row.addWidget(clear_marks_btn)
        preset_row.addStretch()
        layout.addLayout(preset_row)

        self._automark_col_cb = QCheckBox(
            _("Remove the mark when the debuff falls off (auto-cleanse)"))
        self._automark_col_cb.setChecked(self._automark_clear_on_loss)
        self._automark_col_cb.toggled.connect(self._on_automark_col_toggled)
        layout.addWidget(self._automark_col_cb)

        self._settings_header(layout, _("UMAD"))
        chain_row = QHBoxLayout()
        self._umad_chain_cb = QCheckBox(_("UMAD black-hole chains (P3)"))
        self._umad_chain_cb.setChecked(bool(self._settings.get("umad_chain_enabled", False)))
        self._umad_chain_cb.toggled.connect(self._on_umad_chain_toggled)
        chain_row.addWidget(self._umad_chain_cb)
        self._umad_chain_combos = {}
        chain_markers = self._umad_chain_markers_from_settings()
        for key, label in (("dps", _("DPS:")), ("support", _("Supports:")),
                           ("accretion", _("Accretion:"))):
            chain_row.addWidget(QLabel(label))
            combo = self._make_marker_combo(current=chain_markers[key], width=110)
            combo.currentIndexChanged.connect(self._on_umad_chain_marker_changed)
            self._umad_chain_combos[key] = combo
            chain_row.addWidget(combo)
        chain_row.addStretch()
        layout.addLayout(chain_row)

        gaze_row = QHBoxLayout()
        self._umad_gaze_cb = QCheckBox(_("UMAD Cursed Shriek gaze pairs (P4)"))
        self._umad_gaze_cb.setChecked(bool(self._settings.get("umad_gaze_enabled", False)))
        self._umad_gaze_cb.toggled.connect(self._on_umad_gaze_toggled)
        gaze_row.addWidget(self._umad_gaze_cb)
        self._umad_gaze_combos = {}
        gaze_markers = self._umad_gaze_markers_from_settings()
        for key, label in (("away1", _("Look away 1:")), ("away2", _("Look away 2:")),
                           ("look1", _("Look at 1:")), ("look2", _("Look at 2:"))):
            gaze_row.addWidget(QLabel(label))
            combo = self._make_marker_combo(current=gaze_markers[key], width=100)
            combo.currentIndexChanged.connect(self._on_umad_gaze_marker_changed)
            self._umad_gaze_combos[key] = combo
            gaze_row.addWidget(combo)
        gaze_row.addStretch()
        layout.addLayout(gaze_row)

        self._refresh_automark_rules_list()

        self._update_automark_status_label()

    def _on_automark_toggled(self, checked: bool) -> None:
        self._settings["telesto_enabled"] = bool(checked)
        self._save_settings()
        self._apply_automark_state()
        self._telesto_client.ping()

    def _on_automark_col_toggled(self, checked: bool) -> None:
        self._automark_clear_on_loss = bool(checked)
        self._settings["automark_clear_on_loss"] = self._automark_clear_on_loss
        self._save_settings()
        if not checked:
            self._automark_active.clear()

    def _on_umad_chain_toggled(self, checked: bool) -> None:
        self._settings["umad_chain_enabled"] = bool(checked)
        self._umad_chain_enabled = bool(checked)
        self._save_settings()
        if checked:
            self._ws.request_combatants_once()
        else:
            self._umad_chain_reset(clear_marks=True)

    def _on_umad_chain_marker_changed(self, _index: int = 0) -> None:
        defaults = {"dps": "attack1", "support": "attack2",
                    "accretion": "attack3"}
        markers = {}
        for key, combo in self._umad_chain_combos.items():
            markers[key] = combo.currentData() or defaults[key]
            self._settings[f"umad_chain_marker_{key}"] = markers[key]
        self._save_settings()
        self._umad_chains.set_markers(markers)

    def _on_umad_gaze_toggled(self, checked: bool) -> None:
        self._settings["umad_gaze_enabled"] = bool(checked)
        self._umad_gaze_enabled = bool(checked)
        self._save_settings()
        if not checked:
            self._umad_gaze_reset(clear_marks=True)

    def _on_umad_gaze_marker_changed(self, _index: int = 0) -> None:
        defaults = {"away1": "ignore1", "away2": "ignore2",
                    "look1": "bind1", "look2": "bind2"}
        markers = {}
        for key, combo in self._umad_gaze_combos.items():
            markers[key] = combo.currentData() or defaults[key]
            self._settings[f"umad_gaze_marker_{key}"] = markers[key]
        self._save_settings()
        self._umad_gaze.set_markers(markers)

    def _on_automark_test(self) -> None:
        """Force the selected marker onto the local player for testing."""
        tok = (self._automark_test_combo.currentData()
               if hasattr(self, "_automark_test_combo") else None) or "attack1"
        self._telesto_client.configure(uri=self._settings.get("telesto_uri", DEFAULT_TELESTO_URI))
        self._telesto_client.mark_self(tok, force=True)

    def _on_automark_clear(self) -> None:
        self._telesto_client.clear_self(force=True)

    def _on_automark_clear_all(self) -> None:
        """Clear markers from all eight party slots."""
        self._telesto_client.configure(uri=self._settings.get("telesto_uri", DEFAULT_TELESTO_URI))
        self._telesto_client.clear_all(force=True)

    @staticmethod
    def _sync_umad_preset_rules(rules: "list[dict]") -> "tuple[list[dict], int, int]":
        """Sync UMAD rules with the preset, retaining assigned markers for surviving rules.
        New rules start unassigned. Preserve other fights and return added and removed
        counts.
        """
        preset_keys = {_canon_status(h) for h, _label in _UMAD_AUTOMARK_PRESET}
        synced: "list[dict]" = []
        seen_umad: "set[str]" = set()
        removed = 0
        for r in rules:
            if (r.get("fight") or "").strip().casefold() != _UMAD_FIGHT_TAG_CF:
                synced.append(r)
                continue
            # Use canonical compound keys so equivalent spellings retain the assigned
            # marker.
            key = _canon_status((r.get("status") or "").strip())
            if key in preset_keys and key not in seen_umad:
                seen_umad.add(key)
                synced.append(r)
            else:
                removed += 1
        added = 0
        for status_hex, _label in _UMAD_AUTOMARK_PRESET:
            if _canon_status(status_hex) in seen_umad:
                continue
            synced.append({
                "fight": _UMAD_FIGHT_TAG,
                "status": status_hex,
                "marker": "",
                "scope": "party",
                "enabled": True,
            })
            added += 1
        return synced, added, removed

    def _on_automark_load_umad_preset(self) -> None:
        synced, added, removed = self._sync_umad_preset_rules(self._automark_rules)
        if added or removed:
            self._automark_rules = synced
            self._cancel_changed_rule_marks()
            self._settings["automark_rules"] = self._automark_rules
            self._save_settings()
            self._refresh_automark_rules_list()
            msg = _("UMAD preset synced: {added} added, {removed} outdated removed.").format(
                added=added, removed=removed)
        else:
            msg = _("Your UMAD rules already match the preset.")
        try:
            ac.QMessageBox.information(self, _("UMAD preset"), msg)
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    def _norm_hex(s: str) -> str:
        """Normalize status IDs for comparison."""
        s = s.strip().upper()
        if s.startswith("0X"):
            s = s[2:]
        return s.lstrip("0") or "0"

    def _match_automark_rules(self, fields: list[str]) -> None:
        """Match automarker rules on status gain. Compound rules require both statuses and
        share one canonical cooldown key. Retry unknown party slots without consuming
        cooldown.
        """
        if len(fields) < 9:
            return
        tc = getattr(self, "_telesto_client", None)
        if tc is None:
            return
        tgt_id = fields[7].strip().upper()
        if not tgt_id.startswith("10"):
            return                                  # players only
        tgt_name = fields[8]
        eff_id_n = self._norm_hex(fields[2])
        eff_name = fields[3]
        fight = (self._current_fight_tag or "").casefold()
        # Active chain mechanics own their statuses. Plain rules must not overwrite
        # their signs.
        if (self._umad_chain_enabled and eff_id_n in _UMAD_CHAIN_IDS
                and (not fight or fight == _UMAD_FIGHT_TAG_CF)):
            return
        # Active gaze pairing owns Cursed Shriek signs.
        if (self._umad_gaze_enabled and eff_id_n in self._umad_gaze.ids
                and (not fight or fight == _UMAD_FIGHT_TAG_CF)):
            return
        # Prefer actor ID because players on different worlds can share a name.
        is_me = self._is_me_actor(tgt_id, tgt_name)
        now = time.monotonic()
        for rule in self._automark_rules:
            if not rule.get("enabled", True):
                continue
            rfight = (rule.get("fight") or "").strip().casefold()
            if rfight and fight and rfight != fight:
                # Allow fight rules before the current zone is known after connecting.
                continue
            status = (rule.get("status") or "").strip()
            if not status:
                continue
            pair = _parse_compound(status)
            if pair is not None:
                # Use the compound status key for cooldown so its two gains cannot fire
                # twice.
                if eff_id_n not in pair or not self._automark_pairs.holds_all(tgt_id, pair, now):
                    continue
                status_key = "+".join(sorted(pair))
            elif not (self._norm_hex(status) == eff_id_n
                      or status.casefold() == eff_name.casefold()):
                continue
            else:
                status_key = eff_id_n
            self_only = (rule.get("scope") or "self").strip().casefold() in ("self", "me")
            if self_only and not is_me:
                continue
            marker = (rule.get("marker") or "").strip()
            if not marker:
                continue
            key = (status_key, ("me" if is_me else tgt_id), marker)
            if now - self._automark_cooldowns.get(key, 0.0) < 3.0:
                continue
            if self._mark_player(tgt_id, marker, tgt_name, is_me=is_me):
                self._automark_cooldowns[key] = now
                # Record the placing status for clear on loss. The latest mark owns the
                # sign.
                self._automark_active["me" if is_me else tgt_id] = status_key
            elif len(self._automark_pending) < 16:
                # Retry after party refresh because the triggering gain will not repeat.
                if not any(p[0] == tgt_id and p[1] == marker
                           and p[4] == status_key and p[6] == rule
                           for p in self._automark_pending):
                    self._automark_pending.append((tgt_id, marker, tgt_name, now, status_key, rfight,
                                                   rule.copy()))
        # Prune expired cooldown keys during long sessions.
        if len(self._automark_cooldowns) > 256:
            self._automark_cooldowns = {
                k: v for k, v in self._automark_cooldowns.items() if now - v < 10.0}

    def _match_automark_unmark(self, fields: list[str]) -> None:
        """Cancel queued marks when their status is lost. If clearing is enabled, remove a
        sign only when that status still owns it.
        """
        if len(fields) < 9:
            return
        tgt_id = fields[7].strip().upper()
        if not tgt_id.startswith("10"):
            return                                  # players only
        key = "me" if self._is_me_actor(tgt_id, fields[8]) else tgt_id
        # Cancel retries when their status is lost so stale marks cannot overwrite a
        # newer sign.
        pending = getattr(self, "_automark_pending", None)
        if pending:
            eff_n = self._norm_hex(fields[2])
            pending[:] = [p for p in pending
                          if not (p[0] == tgt_id and eff_n in p[4].split("+"))]
        if not self._automark_clear_on_loss:
            return
        status_key = self._automark_active.get(key)
        if status_key is None:
            return
        if self._norm_hex(fields[2]) not in status_key.split("+"):
            return
        if self._clear_player(tgt_id, fields[8]):
            del self._automark_active[key]

    @staticmethod
    def _make_marker_combo(current: "str | None" = None, width: int = 110) -> QComboBox:
        """Build a marker selector, falling back to its first entry for unknown tokens.
        """
        combo = QComboBox()
        for _label, _tok in TELESTO_MARKERS:
            combo.addItem(_(_label), _tok)
        if current:
            idx = combo.findData(current)
            if idx >= 0:
                combo.setCurrentIndex(idx)
        combo.setMaximumWidth(width)
        return combo

    def _umad_name_of(self, actor_id) -> str:
        """Return a cached actor name or an empty string. Actor ID zero remains valid.
        """
        aid = _actor_int(actor_id)
        return "" if aid is None else self._umad_actor_names.get(aid, "")

    def _is_me_actor(self, actor_id: str, name: str = "") -> bool:
        """Identify the local player by actor ID, falling back to name until the ID
        arrives.
        """
        if self._me_id:
            a, m = _actor_int(actor_id), _actor_int(self._me_id)
            return a is not None and a == m
        return bool(self._me_name) and bool(name) \
            and name.casefold() == self._me_name.casefold()

    def _mark_player(self, actor_id: str, marker: str, name: str = "",
                     is_me: "bool | None" = None) -> bool:
        """Mark the local player directly or resolve another player's party slot. Return
        False when resolution or enqueueing fails so callers can retry.
        """
        tc = self._telesto_client
        if tc is None:
            return False
        if is_me is None:
            is_me = self._is_me_actor(actor_id, name)
        if is_me:
            return tc.mark_self(marker)
        return tc.mark_actor(actor_id, marker)

    def _umad_chain_markers_from_settings(self) -> dict:
        """Read valid chain marker tokens from settings, falling back to defaults."""
        markers = {}
        for key, default in (("dps", "attack1"), ("support", "attack2"),
                             ("accretion", "attack3")):
            tok = self._settings.get(f"umad_chain_marker_{key}", default)
            markers[key] = tok if tok in TELESTO_MARKER_TOKENS else default
        return markers

    def _umad_chain_line(self, fields: list[str]) -> None:
        """Route status gains and losses into enabled UMAD chains. Allow an unknown current
        fight.
        """
        if not self._umad_chain_enabled or len(fields) < 9:
            return
        eff = self._norm_hex(fields[2])
        if eff not in _UMAD_CHAIN_IDS:
            return
        if not self._settings.get("telesto_enabled"):
            return
        fight = (self._current_fight_tag or "").casefold()
        if fight and fight != _UMAD_FIGHT_TAG_CF:
            return
        tgt_id = fields[7].strip().upper()
        if not tgt_id.startswith("10"):
            return                                # players only
        aid = _actor_int(tgt_id)
        if aid is not None and fields[8]:
            # Cache chain target names until the player ID is known.
            self._umad_actor_names[aid] = fields[8]
        now = time.monotonic()
        if fields[0] == "26":
            actions = self._umad_chains.on_gain(eff, tgt_id, now)
            self._umad_chain_flush_timer.start()
        else:
            actions = self._umad_chains.on_loss(eff, tgt_id, now)
        self._dispatch_umad_chain_actions(actions)

    def _umad_chain_reset(self, clear_marks: bool = False, force: bool = False) -> None:
        """Reset chains and pending retries. Optionally clear existing signs, forcing sends
        when disabling automarkers. Zone changes skip clears because party slots may
        have changed.
        """
        if clear_marks:
            for actor in self._umad_chains.outstanding():
                self._clear_player(actor, self._umad_name_of(actor), force=force)
        self._umad_chains.reset()
        self._umad_chain_pending.clear()
        since = getattr(self, "_umad_chain_pending_since", None)
        if since is not None:
            since.clear()

    def _on_umad_chain_flush(self) -> None:
        if not (self._umad_chain_enabled and self._settings.get("telesto_enabled")):
            return
        self._retry_umad_chain_pending()
        self._dispatch_umad_chain_actions(self._umad_chains.flush(time.monotonic()))

    def _retry_umad_chain_pending(self) -> None:
        """Retry chain actions after party refresh or debounce, retaining failures until
        their age limit.
        """
        if not self._umad_chain_pending:
            return
        since = getattr(self, "_umad_chain_pending_since", None)
        if since:
            now = time.monotonic()
            self._umad_chain_pending = [
                a for a in self._umad_chain_pending
                if now - since.get(a, now) <= 30.0]
            for a in list(since):
                if a not in self._umad_chain_pending:
                    del since[a]
            if not self._umad_chain_pending:
                return
        pending, self._umad_chain_pending = self._umad_chain_pending, []
        self._dispatch_umad_chain_actions(pending)

    def _gaze_slot_of(self, actor_id):
        """Return the party slot for gaze ordering, or None before resolution."""
        tc = getattr(self, "_telesto_client", None)
        return tc.slot_of_actor(actor_id) if tc is not None else None

    def _umad_gaze_markers_from_settings(self) -> dict:
        """Read valid gaze marker tokens from settings, falling back to defaults."""
        markers = {}
        for key, default in (("away1", "ignore1"), ("away2", "ignore2"),
                             ("look1", "bind1"), ("look2", "bind2")):
            tok = self._settings.get(f"umad_gaze_marker_{key}", default)
            markers[key] = tok if tok in TELESTO_MARKER_TOKENS else default
        return markers

    def _umad_gaze_line(self, fields: list[str]) -> None:
        """Route status gains and losses into enabled UMAD gaze pairing. Followup casts
        determine polarity. Allow an unknown current fight.
        """
        if not self._umad_gaze_enabled or len(fields) < 9:
            return
        eff = self._norm_hex(fields[2])
        if eff not in self._umad_gaze.ids:
            return
        if not self._settings.get("telesto_enabled"):
            return
        fight = (self._current_fight_tag or "").casefold()
        if fight and fight != _UMAD_FIGHT_TAG_CF:
            return
        tgt_id = fields[7].strip().upper()
        if not tgt_id.startswith("10"):
            return                                # players only
        aid = _actor_int(tgt_id)
        if aid is not None and fields[8]:
            self._umad_actor_names[aid] = fields[8]
        now = time.monotonic()
        if fields[0] == "26":
            try:
                dur = float(fields[4])
            except (TypeError, ValueError, IndexError):
                dur = None
            actions = self._umad_gaze.on_gain(eff, tgt_id, dur, now)
            self._umad_gaze_flush_timer.start()
        else:
            actions = self._umad_gaze.on_loss(eff, tgt_id, now)
        self._dispatch_umad_gaze_actions(actions)

    def _umad_gaze_reset(self, clear_marks: bool = False, force: bool = False) -> None:
        """Reset gaze state and retries, optionally clearing signs. Force sends when
        disabling automarkers and skip clears on zone changes.
        """
        if clear_marks:
            for actor in self._umad_gaze.outstanding():
                self._clear_player(actor, self._umad_name_of(actor), force=force)
        self._umad_gaze.reset()
        self._umad_gaze_pending.clear()
        since = getattr(self, "_umad_gaze_pending_since", None)
        if since is not None:
            since.clear()

    def _on_umad_gaze_flush(self) -> None:
        if not (self._umad_gaze_enabled and self._settings.get("telesto_enabled")):
            return
        self._retry_umad_gaze_pending()
        self._dispatch_umad_gaze_actions(self._umad_gaze.flush(time.monotonic()))

    def _retry_umad_gaze_pending(self) -> None:
        """Retry gaze actions after party refresh or debounce until their age limit."""
        if not self._umad_gaze_pending:
            return
        since = getattr(self, "_umad_gaze_pending_since", None)
        if since:
            now = time.monotonic()
            self._umad_gaze_pending = [
                a for a in self._umad_gaze_pending
                if now - since.get(a, now) <= 30.0]
            for a in list(since):
                if a not in self._umad_gaze_pending:
                    del since[a]
            if not self._umad_gaze_pending:
                return
        pending, self._umad_gaze_pending = self._umad_gaze_pending, []
        self._dispatch_umad_gaze_actions(pending)

    def _dispatch_mark_actions(self, actions, pending: list,
                               since: "dict | None" = None) -> list:
        """Dispatch chain and gaze actions and return unresolved actions for retry. New
        actions replace pending ones for the same actor. Moving a sign cancels its
        previous pending mark. Record first enqueue times in since when provided.
        """
        if not actions:
            return pending
        for action in actions:
            kind, actor = action[0], action[1]
            if kind not in ("mark", "clear"):
                continue
            name = self._umad_name_of(actor)
            # An engine mark replaces rule ownership so a later rule loss cannot clear
            # it.
            active = getattr(self, "_automark_active", None)
            if active:
                active.pop(actor, None)
                if self._is_me_actor(actor, name):
                    active.pop("me", None)
            rule_pending = getattr(self, "_automark_pending", None)
            if rule_pending:
                rule_pending[:] = [p for p in rule_pending
                                   if p[0] != actor and not (kind == "mark" and p[1] == action[2])]
            if kind == "mark":
                marker = action[2]
                pending = [p for p in pending
                           if p[1] != actor and not (p[0] == "mark" and p[2] == marker)]
                sent = self._mark_player(actor, marker, name)
            elif kind == "clear":
                pending = [p for p in pending if p[1] != actor]
                sent = self._clear_player(actor, name)
            if not sent and len(pending) < 16:
                pending.append(action)
                if since is not None:
                    # Preserve the first enqueue time across retries.
                    since.setdefault(action, time.monotonic())
        if since is not None:
            live = set(pending)
            for a in list(since):
                if a not in live:
                    del since[a]
        return pending

    def _dispatch_umad_chain_actions(self, actions) -> None:
        fight = (getattr(self, "_current_fight_tag", "") or "").casefold()
        if fight and fight != _UMAD_FIGHT_TAG_CF:
            self._umad_chain_reset()
            return
        self._umad_chain_pending = self._dispatch_mark_actions(
            actions, self._umad_chain_pending,
            getattr(self, "_umad_chain_pending_since", None))

    def _dispatch_umad_gaze_actions(self, actions) -> None:
        fight = (getattr(self, "_current_fight_tag", "") or "").casefold()
        if fight and fight != _UMAD_FIGHT_TAG_CF:
            self._umad_gaze_reset()
            return
        self._umad_gaze_pending = self._dispatch_mark_actions(
            actions, self._umad_gaze_pending,
            getattr(self, "_umad_gaze_pending_since", None))

    def _rearm_umad_chain_flush(self) -> None:
        """Retry the chain debounce when new job data can resolve an open queue."""
        chains = getattr(self, "_umad_chains", None)
        timer = getattr(self, "_umad_chain_flush_timer", None)
        if chains is not None and timer is not None and chains.has_open_queues():
            timer.start()

    def _refresh_automark_rules_list(self) -> None:
        lst = getattr(self, "_automark_rules_list", None)
        if lst is None:
            return
        labels = {tok: lab for lab, tok in TELESTO_MARKERS}
        lst.clear()
        for rule in self._automark_rules:
            fight_raw = (rule.get("fight") or "").strip()
            fight = fight_raw or _("Any fight")
            status = rule.get("status") or "?"
            # Use known debuff names only for UMAD rules because IDs may overlap other
            # fights.
            if fight_raw.casefold() == _UMAD_FIGHT_TAG_CF:
                label = _UMAD_STATUS_LABELS.get(_canon_status(status), "")
                if label:
                    status = f"{label.split(' - ')[0]} ({status})"
            tok = (rule.get("marker") or "").strip()
            marker = _(labels.get(tok, tok)) if tok else _("(unassigned)")
            self_only = (rule.get("scope") or "self").strip().casefold() in ("self", "me")
            who = _("me") if self_only else _("whoever gets it")
            off = "" if rule.get("enabled", True) else _("   (disabled)")
            lst.addItem(QListWidgetItem(
                _("{fight}   ·   {status}   →   {marker} on {who}{off}").format(
                    fight=fight, status=status, marker=marker, who=who, off=off)))

    def _on_automark_remove_rule(self) -> None:
        lst = getattr(self, "_automark_rules_list", None)
        if lst is None:
            return
        row = lst.currentRow()
        if 0 <= row < len(self._automark_rules):
            del self._automark_rules[row]
            self._cancel_changed_rule_marks()
            self._settings["automark_rules"] = self._automark_rules
            self._save_settings()
            self._refresh_automark_rules_list()

    def _on_automark_rule_selected(self, row: int) -> None:
        combo = getattr(self, "_automark_assign_combo", None)
        if combo is None:
            return
        ok = 0 <= row < len(self._automark_rules)
        combo.setEnabled(ok)
        if ok:
            idx = combo.findData((self._automark_rules[row].get("marker") or "").strip())
            combo.setCurrentIndex(idx if idx >= 0 else 0)

    def _on_automark_assign_marker(self) -> None:
        """Save marker changes only on user activation of the selector."""
        lst = getattr(self, "_automark_rules_list", None)
        if lst is None:
            return
        row = lst.currentRow()
        if not (0 <= row < len(self._automark_rules)):
            return
        self._automark_rules[row]["marker"] = self._automark_assign_combo.currentData() or ""
        self._cancel_changed_rule_marks()
        self._settings["automark_rules"] = self._automark_rules
        self._save_settings()
        self._refresh_automark_rules_list()
        lst.setCurrentRow(row)   # Preserve the rule when a refresh clears table selection.

    def _cancel_changed_rule_marks(self) -> None:
        self._automark_pending = [p for p in self._automark_pending
                                  if p[6] in self._automark_rules]

    def _on_automark_uri_changed(self) -> None:
        uri = (self._automark_uri_edit.text() or "").strip() or DEFAULT_TELESTO_URI
        if uri == self._settings.get("telesto_uri", DEFAULT_TELESTO_URI):
            return
        try:
            parsed = urllib.parse.urlparse(uri)
            valid = (parsed.scheme in ("http", "https") and bool(parsed.hostname)
                     and (parsed.port is None or 1 <= parsed.port <= 65535))
        except ValueError:
            valid = False
        if not valid:
            saved = self._settings.get("telesto_uri")
            self._automark_uri_edit.setText(saved if isinstance(saved, str) else DEFAULT_TELESTO_URI)
            self._automark_uri_edit.setToolTip(_("Invalid URL - must be http(s)://host[:port]"))
            return
        self._automark_uri_edit.setToolTip("")
        if uri != self._automark_uri_edit.text():
            self._automark_uri_edit.setText(uri)
        self._settings["telesto_uri"] = uri
        self._save_settings()
        self._apply_automark_state()
        self._telesto_client.ping()

    def _refresh_telesto_party(self) -> None:
        """Refresh enabled Telesto party slots and retry unresolved marks."""
        tc = getattr(self, "_telesto_client", None)
        if tc is not None and self._settings.get("telesto_enabled"):
            # Reapply the saved enabled state and probe so Telesto can recover after a
            # connection failure.
            tc.set_enabled(True)
            tc.request_party_members(force=True)
            if self._umad_chain_enabled:
                self._retry_umad_chain_pending()
            if self._umad_gaze_enabled:
                self._retry_umad_gaze_pending()
            if getattr(self, "_automark_pending", None):
                self._retry_automark_pending()

    def _retry_automark_pending(self) -> None:
        """Retry unresolved rule marks until their age limit. Drop retries that would
        overwrite another rule's current sign.
        """
        if not self._automark_pending:
            return
        now = time.monotonic()
        fight = (self._current_fight_tag or "").casefold()
        keep: list = []
        sent = set()
        for pending in self._automark_pending:
            actor, marker, name, queued_at, status_key, rule_fight, rule = pending
            if (rule not in self._automark_rules or now - queued_at > 30.0
                    or (rule_fight and fight and rule_fight != fight)):
                continue
            if (actor, marker) in sent:
                continue
            player_key = "me" if self._is_me_actor(actor, name) else actor
            live = self._automark_active.get(player_key)
            if live is not None and live != status_key:
                continue   # Do not replace another rule's current sign.
            if not self._mark_player(actor, marker, name):
                keep.append(pending)
            else:
                sent.add((actor, marker))
                self._automark_active[player_key] = status_key
        self._automark_pending = [entry for entry in keep if (entry[0], entry[1]) not in sent]

    def _apply_automark_state(self) -> None:
        """Apply saved Telesto settings. Refresh party slots on enable and clear engine and
        rule signs before disabling.
        """
        tc = getattr(self, "_telesto_client", None)
        if tc is None:
            return
        enabled = bool(self._settings.get("telesto_enabled", False))
        if not enabled:
            # Clear signs before disabling the client. Force sends so cleanup can also
            # recover from a disabled client.
            self._umad_chain_reset(clear_marks=True, force=True)
            self._umad_gaze_reset(clear_marks=True, force=True)
            # Clear rule marks too. The me key uses clear_self instead of actor lookup.
            for key in list(self._automark_active):
                if key == "me":
                    tc.clear_self(force=True)
                else:
                    self._clear_player(key, force=True)
            self._automark_active.clear()
        tc.configure(uri=self._settings.get("telesto_uri", DEFAULT_TELESTO_URI),
                     enabled=enabled)
        if tc.last_status() is None:
            self._telesto_status = "unknown"
        bridge = getattr(self, "_triggernometry", None)
        if bridge is not None:
            changed = bridge.configure_telesto(self._settings.get("telesto_uri"), enabled)
            if changed and getattr(self, "_triggernometry_mode", False):
                self._set_triggernometry_enabled(False)
                self._set_triggernometry_enabled(True)
        if enabled:
            tc.request_party_members()
        else:
            self._automark_pending.clear()
        self._update_automark_status_label()

    def _on_telesto_client_status(self, reachable: bool, message: str, degraded: bool = False) -> None:
        """Show native Telesto reachability. Use amber when the server responds but
        commands fail.
        """
        # Queued signals may describe an endpoint that has since been replaced.
        state = self._telesto_client.last_status()
        if state is None:
            self._telesto_status = "unknown"
        else:
            reachable, degraded = state
            self._telesto_status = "bad" if not reachable else ("degraded" if degraded else "good")
        self._update_automark_status_label()

    def _on_telesto_status(self, _status: str, gen: "int | None" = None) -> None:
        # The native Telesto client owns connection status. Ignore the engine probe and
        # stale generations.
        if ac._stale_gen(getattr(self, "_triggevent", None), gen):
            return
        return

    @staticmethod
    def _is_dot(t) -> bool:
        """Identify triggers with expiry reminders for the DoT section."""
        return (getattr(t, "expiry_warn_s", 0) or 0) > 0

    def _umad_gaze_cast(self, fields: list[str]) -> None:
        """Route UMAD Inferno and Tsunami casts to arm gaze polarity before status gains
        arrive.
        """
        if not self._umad_gaze_enabled or len(fields) < 5:
            return
        eff = self._norm_hex(fields[4])
        if eff not in _UMAD_GAZE_FOLLOWUP_IDS:
            return
        if not self._settings.get("telesto_enabled"):
            return
        fight = (self._current_fight_tag or "").casefold()
        if fight and fight != _UMAD_FIGHT_TAG_CF:
            return
        actions = self._umad_gaze.on_followup(eff, time.monotonic())
        self._dispatch_umad_gaze_actions(actions)
