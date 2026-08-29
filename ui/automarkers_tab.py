"""Automarkers tab. UMAD chain and gaze logic, mark dispatch and the
Telesto retry queues. Mixin for MainWindow, all state rides on self.
"""

import re
import time
import urllib.parse

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QListWidget, QListWidgetItem, QHBoxLayout, QPushButton, QLineEdit, QLabel,
)

from locale_util import _
from telesto_client import (
    TelestoClient, MARKERS as TELESTO_MARKERS, MARKER_TOKENS as TELESTO_MARKER_TOKENS, _actor_int,
)
from umad_chains import (
    BlackHoleChains, RELEVANT_IDS as _UMAD_CHAIN_IDS, role_for_job, StatusPairs,
    parse_compound as _parse_compound, canon_status_key as _canon_status,
    CursedShriekPairs, GAZE_FOLLOWUP_IDS as _UMAD_GAZE_FOLLOWUP_IDS,
)

import app_common as ac
from app_common import (
    DEFAULT_TELESTO_URI, _UMAD_AUTOMARK_PRESET, _UMAD_FIGHT_TAG, _UMAD_FIGHT_TAG_CF, _UMAD_STATUS_LABELS,
)


class AutomarkersTabMixin:
    def _init_automarkers(self) -> None:
        # Native Telesto client, built after _load_settings. Places head sign
        # marks, /mk <marker> <me>, for automark rules and the Test button.
        # Started now so Test and the indicator work as soon as Settings opens.
        self._telesto_client = TelestoClient(
            uri=self._settings.get("telesto_uri", DEFAULT_TELESTO_URI),
            enabled=bool(self._settings.get("telesto_enabled", False)))
        self._telesto_client.status_changed.connect(self._on_telesto_client_status)
        self._telesto_client.start()
        QTimer.singleShot(1200, self._telesto_client.ping)   # initial reachability probe
        # Keeps the party slot map fresh for marking other players, mirrors the
        # engine's 10s TelestoPartyRefresh. Sends only while automarkers is
        # enabled. Zone changes refresh immediately. Party marks need this map,
        # self marks don't.
        self._telesto_party_timer = QTimer(self)
        self._telesto_party_timer.setInterval(10_000)
        self._telesto_party_timer.timeout.connect(self._refresh_telesto_party)
        self._telesto_party_timer.start()
        # User automark rules, dicts of fight, status, marker, scope, enabled.
        # scope defaults to "self". Matched off status 26 lines in _on_log_line.
        # Coerced like _as_dict so a hand edited null can't raise out of init.
        # The string fields get str coerced for the same reason. Every rule
        # consumer .strip calls them and a hand edited int would raise there.
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
        # Party scoped marks skipped while the slot map was cold. Entries hold
        # actor, marker, name, queued_at and statusKey. Retried by the 10s party
        # tick in _retry_automark_pending and dropped once too old to matter.
        self._automark_pending: list = []
        # Clear on loss. A rule placed sign is removed when the debuff that
        # placed it falls off on a 30 line. The Accretion pair's signs vanish
        # the moment they are cleansed. _automark_active maps the target key,
        # "me" or actor id, to the statusKey whose rule placed their current
        # sign, so a loss only ever clears the sign its own rule put up.
        self._automark_clear_on_loss = bool(self._settings.get("automark_clear_on_loss", True))
        self._automark_active: dict = {}
        # Pair tracker for compound rules, "A+B" means both statuses on one
        # player. Tracks compound tokens from the preset and the loaded rules.
        # Fed 26/30 lines regardless of toggles, since state must already be
        # warm if rules are enabled mid fight. Reset per zone and wipe. Entries
        # go stale after STALE_S so missed loss lines can't mis mark later.
        self._automark_pairs = StatusPairs(
            p
            for token in ([h for h, _unused in _UMAD_AUTOMARK_PRESET]
                          + [str(r.get("status") or "") for r in self._automark_rules])
            for p in (_parse_compound(token) or ()))
        # UMAD P3 black hole marker chains. One roaming sign per cleanse queue,
        # see umad_chains.py. Jobs come from 03 AddedCombatant lines, the
        # PartyChanged roster, and a one shot getCombatants backfill so a mid
        # instance restart still resolves roles. Keyed by _actor_int, cleared
        # per zone. The enabled flag is cached as a plain attribute because it
        # gates the per line hot path.
        self._actor_jobs: dict[int, int] = {}        # _actor_int value to ClassJob id
        self._umad_actor_names: dict[int, str] = {}  # _actor_int value to name, chain targets only
        self._umad_chain_enabled = bool(self._settings.get("umad_chain_enabled", False))
        self._umad_chains = BlackHoleChains(
            role_of=lambda aid: role_for_job(self._actor_jobs.get(_actor_int(aid))),
            markers=self._umad_chain_markers_from_settings())
        # Chain mark and clear commands whose target's party slot wasn't known
        # yet. Retried on the party refresh tick and the debounce flush. A
        # dropped hop would desync the visible sign from the engine's holder
        # for good.
        self._umad_chain_pending: list = []
        # Enqueue time per queued action, for the retry age cap. Parallel to
        # the pending list so the action tuple shape the tests drive stays as
        # it is.
        self._umad_chain_pending_since: dict = {}
        # Debounced best effort start for queues the fast path couldn't prove
        # complete, like a missed line or a dead player. Re armed on every
        # chain debuff.
        self._umad_chain_flush_timer = QTimer(self)
        self._umad_chain_flush_timer.setSingleShot(True)
        self._umad_chain_flush_timer.setInterval(1200)
        self._umad_chain_flush_timer.timeout.connect(self._on_umad_chain_flush)
        # UMAD P4 Cursed Shriek gaze pairing. Two Grand Cross waves each deal
        # a gaze pair, and each wave's Inferno or Tsunami followup cast tells
        # fake from real. The fake pair gets the look-at signs, bind or chain,
        # the real pair the look-away ignore signs, one distinct sign each.
        # Separate engine, retry queue and debounced flush. Same Telesto
        # transport as the chains. Opt in like the chains and off by default.
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
        # Job feeds beyond the 03 lines. The PartyChanged roster, decimal ints,
        # and getCombatants snapshots, one shot on enable and zone entry plus
        # whatever the DPS logger's polling produces.
        self._ws.party_jobs.connect(self._on_ws_party_jobs)
        self._ws.combatants.connect(self._on_ws_combatants_jobs)

    def _build_automark_settings(self, layout) -> None:
        """Automarkers section in Settings. Drives the Telesto Dalamud plugin to
        place head-signs, /mk, via direct POST. No Triggevent Engine involved.
        A rule is fight + debuff -> marker, targeting you, `<me>`, or whoever
        got the debuff by party slot `<N>` from Telesto's GetPartyMembers.
        _match_automark_rules fires them off 26 GainsEffect lines. Test and
        Clear force a send to verify the pipeline."""
        testing_note = QLabel(_("These automarkers need testing, please let me know."))
        testing_note.setWordWrap(True)
        testing_note.setStyleSheet("color: #8f8f9a; font-size: 11px;")
        layout.addWidget(testing_note)

        # ── Connection ──
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

        # exercise the pipeline, put a marker on yourself even before Enable is
        # ticked, just to prove Telesto is wired up
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

        # ── Rules, when a debuff is gained place a marker ──
        self._settings_header(layout, _("Rules"))
        self._automark_rules_list = QListWidget()
        self._automark_rules_list.setMaximumHeight(150)
        layout.addWidget(self._automark_rules_list)

        rm_row = QHBoxLayout()
        rm_row.addWidget(QLabel(_("Marker:")))
        # assigns a sign to the SELECTED rule. Rules seed unassigned and inert,
        # pick a sign here to arm one. The unassigned entry disarms it again
        self._automark_assign_combo = QComboBox()
        self._automark_assign_combo.addItem(_("(unassigned)"), "")
        for _label, _tok in TELESTO_MARKERS:
            self._automark_assign_combo.addItem(_(_label), _tok)
        self._automark_assign_combo.setMaximumWidth(140)
        self._automark_assign_combo.setEnabled(False)   # until a rule is selected
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

        # Preset + bulk-clear row.
        preset_row = QHBoxLayout()
        umad_btn = QPushButton(_("Load UMAD preset"))
        umad_btn.clicked.connect(self._on_automark_load_umad_preset)
        preset_row.addWidget(umad_btn)
        clear_marks_btn = QPushButton(_("Clear all party marks"))
        clear_marks_btn.clicked.connect(self._on_automark_clear_all)
        preset_row.addWidget(clear_marks_btn)
        preset_row.addStretch()
        layout.addLayout(preset_row)

        # clear-on-loss. Rule signs track their debuff and vanish with it, the
        # Accretion pair's marks drop the moment they cleanse. Default on.
        self._automark_col_cb = QCheckBox(
            _("Remove the mark when the debuff falls off (auto-cleanse)"))
        self._automark_col_cb.setChecked(self._automark_clear_on_loss)
        self._automark_col_cb.toggled.connect(self._on_automark_col_toggled)
        layout.addWidget(self._automark_col_cb)

        # ── UMAD, P3 black-hole chains then P4 gaze pairs ──
        self._settings_header(layout, _("UMAD"))
        # One roaming sign per cleanse queue.
        chain_row = QHBoxLayout()
        self._umad_chain_cb = QCheckBox(_("UMAD black-hole chains (P3)"))
        self._umad_chain_cb.setChecked(bool(self._settings.get("umad_chain_enabled", False)))
        self._umad_chain_cb.toggled.connect(self._on_umad_chain_toggled)
        chain_row.addWidget(self._umad_chain_cb)
        self._umad_chain_combos = {}
        chain_markers = self._umad_chain_markers_from_settings()   # validated tokens
        for key, label in (("dps", _("DPS:")), ("support", _("Supports:")),
                           ("accretion", _("Accretion:"))):
            chain_row.addWidget(QLabel(label))
            combo = self._make_marker_combo(current=chain_markers[key], width=110)
            combo.currentIndexChanged.connect(self._on_umad_chain_marker_changed)
            self._umad_chain_combos[key] = combo
            chain_row.addWidget(combo)
        chain_row.addStretch()
        layout.addLayout(chain_row)

        # Fake pair looks at, gets the bind signs. Real pair looks away.
        gaze_row = QHBoxLayout()
        self._umad_gaze_cb = QCheckBox(_("UMAD Cursed Shriek gaze pairs (P4)"))
        self._umad_gaze_cb.setChecked(bool(self._settings.get("umad_gaze_enabled", False)))
        self._umad_gaze_cb.toggled.connect(self._on_umad_gaze_toggled)
        gaze_row.addWidget(self._umad_gaze_cb)
        self._umad_gaze_combos = {}
        gaze_markers = self._umad_gaze_markers_from_settings()   # validated tokens
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
        # turns the Telesto client on so automark rules can fire. Test forces
        # sends regardless. The Triggevent Engine is untouched.
        self._apply_automark_state()
        self._telesto_client.ping()

    def _on_automark_col_toggled(self, checked: bool) -> None:
        self._automark_clear_on_loss = bool(checked)
        self._settings["automark_clear_on_loss"] = self._automark_clear_on_loss
        self._save_settings()
        if not checked:
            # No more auto-clears, so the placed-by bookkeeping is dead weight.
            self._automark_active.clear()

    def _on_umad_chain_toggled(self, checked: bool) -> None:
        self._settings["umad_chain_enabled"] = bool(checked)
        self._umad_chain_enabled = bool(checked)
        self._save_settings()
        if checked:
            self._ws.request_combatants_once()   # backfill party jobs right away
        else:
            self._umad_chain_reset(clear_marks=True)   # drop state + clear any signs still up

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
            self._umad_gaze_reset(clear_marks=True)   # drop state + clear any signs still up

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
        """Place the selected marker on you via Telesto, forced so it works before
        automarkers is even on. The mark actually showing up is the confirmation."""
        tok = (self._automark_test_combo.currentData()
               if hasattr(self, "_automark_test_combo") else None) or "attack1"
        self._telesto_client.configure(uri=self._settings.get("telesto_uri", DEFAULT_TELESTO_URI))
        self._telesto_client.mark_self(tok, force=True)

    def _on_automark_clear(self) -> None:
        """Remove any marker currently on you."""
        self._telesto_client.clear_self(force=True)

    def _on_automark_clear_all(self) -> None:
        """Remove head-signs from every party slot, /mk clear <1> through <8>."""
        self._telesto_client.configure(uri=self._settings.get("telesto_uri", DEFAULT_TELESTO_URI))
        self._telesto_client.clear_all(force=True)

    @staticmethod
    def _sync_umad_preset_rules(rules: "list[dict]") -> "tuple[list[dict], int, int]":
        """Reconcile a rule list against _UMAD_AUTOMARK_PRESET. Returns the
        synced rules plus added and removed counts. UMAD-tagged rules still in
        the preset keep their assigned markers, ones no longer in it get dropped,
        missing entries append unassigned. Non-UMAD rules pass through untouched."""
        preset_keys = {_canon_status(h) for h, _label in _UMAD_AUTOMARK_PRESET}
        synced: "list[dict]" = []
        seen_umad: "set[str]" = set()
        removed = 0
        for r in rules:
            if (r.get("fight") or "").strip().casefold() != _UMAD_FIGHT_TAG_CF:
                synced.append(r)
                continue
            # canonical key, since "644+0bbc" and "BBC+644" are spelling variants
            # of the same rule and must survive the sync with their marker
            key = _canon_status((r.get("status") or "").strip())
            if key in preset_keys and key not in seen_umad:
                seen_umad.add(key)
                synced.append(r)
            else:
                removed += 1                 # trimmed from the preset, or a duplicate
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
        """Sync the rule list to the current UMAD preset. Add missing entries
        unassigned, drop outdated ones, keep the rest and their markers as-is."""
        synced, added, removed = self._sync_umad_preset_rules(self._automark_rules)
        if added or removed:
            self._automark_rules = synced
            self._settings["automark_rules"] = self._automark_rules
            self._save_settings()
            self._refresh_automark_rules_list()
            msg = _("UMAD preset synced: {added} added, {removed} outdated removed.").format(
                added=added, removed=removed)
        else:
            msg = _("Your UMAD rules already match the preset.")
        try:
            ac.QMessageBox.information(self, _("UMAD preset"), msg)
        except Exception:  # noqa: BLE001 - never let a dialog failure break the click
            pass

    @staticmethod
    def _norm_hex(s: str) -> str:
        """Normalise a status id for comparison. Upper-case it, drop a 0x prefix
        and any leading zeros so '8D1', '08D1' and '0x8d1' all compare equal."""
        s = s.strip().upper()
        if s.startswith("0X"):
            s = s[2:]
        return s.lstrip("0") or "0"

    def _match_automark_rules(self, fields: list[str]) -> None:
        """A 26 GainsEffect status line came in, fire any automark rule for
        this debuff. Field layout
        26|ts|effectId|effectName|dur|srcId|src|tgtId|tgtName|...

        Scope 'self' marks you via `<me>`, 'party' marks whoever got the debuff
        by party slot, skipped without burning the cooldown while the slot is
        unknown. A compound rule, "A+B", fires only when the target holds both
        statuses inside the staleness window, per _automark_pairs. The cooldown
        keys on the canonical compound token so the pair's two lines fire once."""
        if len(fields) < 9:
            return
        tc = getattr(self, "_telesto_client", None)
        if tc is None:
            return
        tgt_id = fields[7].strip().upper()   # canonical id case, same as the chain/gaze engines
        if not tgt_id.startswith("10"):
            return                                  # players only
        tgt_name = fields[8]
        eff_id_n = self._norm_hex(fields[2])
        eff_name = fields[3]
        fight = (self._current_fight_tag or "").casefold()
        # the black-hole chain sequencer owns its statuses while active. A plain
        # rule on the same debuff would fight the roaming queue sign on the same
        # players, one sign per player and last /mk wins
        if (self._umad_chain_enabled and eff_id_n in _UMAD_CHAIN_IDS
                and (not fight or fight == _UMAD_FIGHT_TAG_CF)):
            return
        # same for the gaze pairing owning Cursed Shriek while active. Its
        # two-sign split would fight a plain rule marking all four with one sign
        if (self._umad_gaze_enabled and eff_id_n in self._umad_gaze.ids
                and (not fight or fight == _UMAD_FIGHT_TAG_CF)):
            return
        # Prefer actor id so a same-named cross-world member can't be mistaken
        # for you. Fall back to name until the 02 line gives our id.
        is_me = self._is_me_actor(tgt_id, tgt_name)
        now = time.monotonic()
        for rule in self._automark_rules:
            if not rule.get("enabled", True):
                continue
            rfight = (rule.get("fight") or "").strip().casefold()
            if rfight and fight and rfight != fight:
                # an unknown fight passes, empty tag from starting or reconnecting
                # mid-instance with no 01 zone line yet. Fight-scoped rules must
                # not sit dead until the user happens to re-zone
                continue
            status = (rule.get("status") or "").strip()
            if not status:
                continue
            pair = _parse_compound(status)
            if pair is not None:
                # compound rule, the pair tracker in _on_log_line already counted
                # this line's status. Cooldown keys on the canonical compound
                # token so the pair's two lines can't double-fire
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
                continue                             # self rule, debuff is on someone else
            marker = (rule.get("marker") or "").strip()
            if not marker:
                continue                             # no sign assigned, rule is inert
            key = (status_key, ("me" if is_me else tgt_id), marker)
            if now - self._automark_cooldowns.get(key, 0.0) < 3.0:
                continue
            if self._mark_player(tgt_id, marker, tgt_name, is_me=is_me):
                self._automark_cooldowns[key] = now
                # remember which rule's status put the sign up so its loss line
                # can clear it again, clear-on-loss. Last writer wins, same as
                # the game's one-sign-per-player
                self._automark_active["me" if is_me else tgt_id] = status_key
            elif len(self._automark_pending) < 16:
                # party slot unknown, cold map. Queue a bounded retry on the
                # 10s party tick instead of dropping the mark for good. The
                # gain line that triggered it won't repeat
                if not any(p[0] == tgt_id and p[1] == marker
                           for p in self._automark_pending):
                    self._automark_pending.append((tgt_id, marker, tgt_name, now, status_key))
        # bound the cooldown map over a long session, per-target keys pile up
        if len(self._automark_cooldowns) > 256:
            self._automark_cooldowns = {
                k: v for k, v in self._automark_cooldowns.items() if now - v < 10.0}

    def _match_automark_unmark(self, fields: list[str]) -> None:
        """A 30 LosesEffect status line came in. When the effect falling off is
        the one whose rule placed the target's current sign, remove the sign.
        This is what drops the Accretion pair's marks the moment they cleanse.
        Only the placing rule's own statuses clear it. Losing anything else
        leaves the sign alone, and a sign another engine put up, chains or gaze,
        never lands in _automark_active at all. Runs on every 30 line, with
        clear-on-loss off too, since the queued retry purge below must still
        fire."""
        if len(fields) < 9:
            return
        tgt_id = fields[7].strip().upper()   # same canonical case the gains store under
        if not tgt_id.startswith("10"):
            return                                  # players only
        key = "me" if self._is_me_actor(tgt_id, fields[8]) else tgt_id
        # A queued retry for this same debuff dies with it on every loss
        # line, placed sign or not. Else the 10s tick can still mark a
        # player whose debuff already fell off, and when another rule has
        # since marked the target that stale retry would overwrite the
        # live sign. getattr, duck-typed test windows may lack the pending
        # list.
        pending = getattr(self, "_automark_pending", None)
        if pending:
            eff_n = self._norm_hex(fields[2])
            pending[:] = [p for p in pending
                          if not (p[0] == tgt_id and eff_n in p[4].split("+"))]
        if not self._automark_clear_on_loss:
            return   # auto-cleanse off, signs stay up, only the purge above runs
        status_key = self._automark_active.get(key)
        if status_key is None:
            return
        if self._norm_hex(fields[2]) not in status_key.split("+"):
            return                                  # some other debuff fell off
        if self._clear_player(tgt_id, fields[8]):
            del self._automark_active[key]

    @staticmethod
    def _make_marker_combo(current: "str | None" = None, width: int = 110) -> QComboBox:
        """One QComboBox over the Telesto marker tokens, the file builds five of
        these. `current` preselects a token. An unknown token keeps index 0."""
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
        """Display name for a chain/gaze actor id, "" when unknown. The None
        check is explicit, an `or -1` shortcut on _actor_int would eat a valid 0."""
        aid = _actor_int(actor_id)
        return "" if aid is None else self._umad_actor_names.get(aid, "")

    def _is_me_actor(self, actor_id: str, name: str = "") -> bool:
        """Is this actor the local player? Prefer the unique actor id from the
        02 line, fall back to a name compare until that line has shown up."""
        if self._me_id:
            a, m = _actor_int(actor_id), _actor_int(self._me_id)
            return a is not None and a == m
        return bool(self._me_name) and bool(name) \
            and name.casefold() == self._me_name.casefold()

    def _mark_player(self, actor_id: str, marker: str, name: str = "",
                     is_me: "bool | None" = None) -> bool:
        """Place a sign on a player. `<me>` for the local player always works,
        others go through party-slot resolution. Returns False when the slot
        isn't known yet or the command never reached the queue, so callers
        can retry instead of losing the mark."""
        tc = self._telesto_client
        if tc is None:
            return False
        if is_me is None:
            is_me = self._is_me_actor(actor_id, name)
        if is_me:
            return tc.mark_self(marker)
        return tc.mark_actor(actor_id, marker)

    def _umad_chain_markers_from_settings(self) -> dict:
        """Per-queue chain markers from settings, validated against the real
        token list. A hand-edited value falls back to the default instead of
        reaching the game as '/mk <garbage>'."""
        markers = {}
        for key, default in (("dps", "attack1"), ("support", "attack2"),
                             ("accretion", "attack3")):
            tok = self._settings.get(f"umad_chain_marker_{key}", default)
            markers[key] = tok if tok in TELESTO_MARKER_TOKENS else default
        return markers

    def _umad_chain_line(self, fields: list[str]) -> None:
        """Route a status line, 26 gain or 30 loss, into the black-hole chain
        engine. Same layout as automark rules
        26/30|ts|effectId|effectName|dur|srcId|src|tgtId|tgtName|...
        Cheap rejections first, this runs for every status line in combat. Gated
        on automarkers being enabled, Telesto is the mark transport, plus the
        chain toggle and the fight being UMAD when the zone is recognised."""
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
            # name fallback for _is_me_actor before the 02 line pins our id.
            # chain targets only, never more than a party's worth
            self._umad_actor_names[aid] = fields[8]
        now = time.monotonic()
        if fields[0] == "26":
            actions = self._umad_chains.on_gain(eff, tgt_id, now)
            # re-arm the debounced flush. Once the debuff burst goes quiet,
            # best-effort-start any queue the fast path couldn't prove
            self._umad_chain_flush_timer.start()
        else:
            actions = self._umad_chains.on_loss(eff, tgt_id, now)
        self._dispatch_umad_chain_actions(actions)

    def _umad_chain_reset(self, clear_marks: bool = False, force: bool = False) -> None:
        """Drop chain state and queued retries on zone change, wipe, toggle off.
        A pending mark from a dead mechanic must not fire later. clear_marks
        also removes the signs already on players. A wipe or toggle-off
        mid-cleanse would else strand whatever sign hadn't walked out. A zone
        change skips that, the party is gone and its slot map is about to churn.
        force sends the clears through a disabled client, the parent automarkers
        toggle goes dark right after this runs."""
        if clear_marks:
            for actor in self._umad_chains.outstanding():
                self._clear_player(actor, self._umad_name_of(actor), force=force)
        self._umad_chains.reset()
        self._umad_chain_pending.clear()
        # getattr, duck-typed test windows may lack the timestamp map
        since = getattr(self, "_umad_chain_pending_since", None)
        if since is not None:
            since.clear()

    def _on_umad_chain_flush(self) -> None:
        if not (self._umad_chain_enabled and self._settings.get("telesto_enabled")):
            return
        self._retry_umad_chain_pending()
        self._dispatch_umad_chain_actions(self._umad_chains.flush(time.monotonic()))

    def _retry_umad_chain_pending(self) -> None:
        """Re-send chain commands skipped because the target's party slot
        wasn't known, stale or empty Telesto party list. Called from the 10s
        party-refresh tick and the debounce flush. Anything that fails again
        just goes back on the list. Entries older than the age cap are
        dropped instead, the mechanic has long resolved, same as the rule
        retries."""
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
        """Party slot for the gaze 1/2 ordering. None until the slot map warms."""
        tc = getattr(self, "_telesto_client", None)
        return tc.slot_of_actor(actor_id) if tc is not None else None

    def _umad_gaze_markers_from_settings(self) -> dict:
        """Per-slot gaze markers from settings, validated against the real token
        list so a hand-edited value can't reach the game as '/mk <garbage>'."""
        markers = {}
        for key, default in (("away1", "ignore1"), ("away2", "ignore2"),
                             ("look1", "bind1"), ("look2", "bind2")):
            tok = self._settings.get(f"umad_gaze_marker_{key}", default)
            markers[key] = tok if tok in TELESTO_MARKER_TOKENS else default
        return markers

    def _umad_gaze_line(self, fields: list[str]) -> None:
        """Route a status line, 26 gain or 30 loss, into the Cursed Shriek gaze
        engine. Same layout as automark rules
        26/30|ts|effectId|effectName|dur|srcId|src|tgtId|tgtName|...
        The gain's duration, field 4, is just set bookkeeping now, the real or
        fake kind comes from the wave's followup cast via _umad_gaze_cast.
        Gated on automarkers enabled, Telesto transport, the gaze toggle, and
        the fight being UMAD when the zone is recognised."""
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
            self._umad_actor_names[aid] = fields[8]   # name fallback for _is_me_actor
        now = time.monotonic()
        if fields[0] == "26":
            try:
                dur = float(fields[4])
            except (TypeError, ValueError, IndexError):
                dur = None
            actions = self._umad_gaze.on_gain(eff, tgt_id, dur, now)
            self._umad_gaze_flush_timer.start()   # re-arm the debounce backstop
        else:
            actions = self._umad_gaze.on_loss(eff, tgt_id, now)
        self._dispatch_umad_gaze_actions(actions)

    def _umad_gaze_reset(self, clear_marks: bool = False, force: bool = False) -> None:
        """Drop gaze state and queued retries on zone change, wipe, toggle off.
        clear_marks also removes signs already placed so an abort mid-gaze
        doesn't strand them. Zone change skips it, the slot map is churning.
        force sends the clears through a disabled client, like the chain reset."""
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
        """Re-send gaze marks skipped because the target's party slot wasn't known
        yet. Called from the 10s party-refresh tick and the debounce flush.
        Entries older than the age cap are dropped, the mechanic has long
        resolved, same as the rule retries."""
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
        """Send an engine's mark/clear actions through the shared transport and
        return the updated pending list. Self goes via <me>, others by party
        slot, no guess if the slot is unknown, the command is queued for retry.
        A queued command is superseded and dropped the moment a newer action for
        the same actor arrives, of either kind. A queued mark must not outlive
        the engine's clear for that player, the mechanic has resolved, and a
        queued clear must not outlive a newer mark. A queued mark also dies when
        its sign moves to another player, so a retry can never regress.
        Shared by the black-hole chains and the Cursed Shriek gaze engine.
        `since`, when passed, records each queued action's first enqueue time
        in a parallel map, so the retry paths can drop aged entries without
        changing the action tuple shape."""
        if not actions:
            return pending
        for action in actions:
            kind, actor = action[0], action[1]
            name = self._umad_name_of(actor)
            # the engine's command owns this head now. A rule-placed sign it
            # overwrites must not be cleared later by that rule's own loss
            # line. getattr because duck-typed test windows lack _automark_active
            active = getattr(self, "_automark_active", None)
            if active:
                active.pop(actor, None)
                if self._is_me_actor(actor, name):
                    active.pop("me", None)
            if kind == "mark":
                marker = action[2]
                pending = [p for p in pending
                           if p[1] != actor and not (p[0] == "mark" and p[2] == marker)]
                sent = self._mark_player(actor, marker, name)
            elif kind == "clear":
                pending = [p for p in pending if p[1] != actor]
                sent = self._clear_player(actor, name)
            else:
                continue
            if not sent and len(pending) < 16:
                pending.append(action)
                if since is not None:
                    # setdefault so a failed retry keeps its first enqueue
                    # time and the age cap measures the mechanic's age
                    since.setdefault(action, time.monotonic())
        if since is not None:
            # drop the stamps of entries no longer queued
            live = set(pending)
            for a in list(since):
                if a not in live:
                    del since[a]
        return pending

    def _dispatch_umad_chain_actions(self, actions) -> None:
        self._umad_chain_pending = self._dispatch_mark_actions(
            actions, self._umad_chain_pending,
            getattr(self, "_umad_chain_pending_since", None))

    def _dispatch_umad_gaze_actions(self, actions) -> None:
        self._umad_gaze_pending = self._dispatch_mark_actions(
            actions, self._umad_gaze_pending,
            getattr(self, "_umad_gaze_pending_since", None))

    def _rearm_umad_chain_flush(self) -> None:
        """Re-arm the debounced chain flush when a job feed lands while the
        engine still has an open queue. The flush fails closed on unknown
        roles, so the backfill that finally names them deserves a rerun.
        QTimer.start re-arms a single-shot, and flushing a cold engine is a
        no-op. getattr, duck-typed test windows may lack the engine or timer."""
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
            # show the debuff's name for known UMAD ids, UMAD-tagged rules only
            # since the same hex could mean something else in another fight
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
            self._settings["automark_rules"] = self._automark_rules
            self._save_settings()
            self._refresh_automark_rules_list()

    def _on_automark_rule_selected(self, row: int) -> None:
        """Sync the Marker combo to the newly selected rule's sign, no save."""
        combo = getattr(self, "_automark_assign_combo", None)
        if combo is None:
            return
        ok = 0 <= row < len(self._automark_rules)
        combo.setEnabled(ok)
        if ok:
            idx = combo.findData((self._automark_rules[row].get("marker") or "").strip())
            combo.setCurrentIndex(idx if idx >= 0 else 0)

    def _on_automark_assign_marker(self) -> None:
        """User picked a sign in the Marker combo, write it to the selected rule.
        `activated` only fires on user action, so syncing the combo never saves."""
        lst = getattr(self, "_automark_rules_list", None)
        if lst is None:
            return
        row = lst.currentRow()
        if not (0 <= row < len(self._automark_rules)):
            return
        self._automark_rules[row]["marker"] = self._automark_assign_combo.currentData() or ""
        self._settings["automark_rules"] = self._automark_rules
        self._save_settings()
        self._refresh_automark_rules_list()
        lst.setCurrentRow(row)   # refresh cleared the selection. Keep the rule active

    def _on_automark_uri_changed(self) -> None:
        uri = (self._automark_uri_edit.text() or "").strip() or DEFAULT_TELESTO_URI
        if uri == self._settings.get("telesto_uri", DEFAULT_TELESTO_URI):
            return   # unchanged text, a bare focus-out must not re-save or re-ping
        parsed = urllib.parse.urlparse(uri)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            # malformed URL, revert to the saved value and explain via tooltip
            # so the client never gets pointed at a bad endpoint
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
        """Ask Telesto for the party list so the actor-id -> slot map stays
        fresh. Only sends while automarkers is enabled. Harmless no-op when
        Telesto is absent. Also the retry tick for chain/rule marks skipped
        while a slot was unknown."""
        tc = getattr(self, "_telesto_client", None)
        if tc is not None and self._settings.get("telesto_enabled"):
            # self-heal. Re-assert the client's enabled flag from the setting so
            # a desync can't silently stop marks, and force the probe so
            # reachability and the party-slot map stay fresh right through a
            # Telesto hiccup. Once the plugin answers again the next tick goes
            # green and marks resume on their own, no relaunch
            tc.set_enabled(True)
            tc.request_party_members(force=True)
            if self._umad_chain_enabled:
                self._retry_umad_chain_pending()
            if self._umad_gaze_enabled:
                self._retry_umad_gaze_pending()
            # getattr rather than self._automark_pending because duck-typed test
            # windows call this unbound with only the attributes they need
            if getattr(self, "_automark_pending", None):
                self._retry_automark_pending()

    def _retry_automark_pending(self) -> None:
        """Re-send party-scoped rule marks skipped while the target's party slot
        was unknown, cold Telesto slot map. Called from the 10s party-refresh
        tick. A failed resend goes back on the list, and anything older than
        the age cap is dropped. The mechanic has long resolved, so a late mark
        would be noise. A retry whose target now carries a different rule's
        live sign is dropped too, re-marking would overwrite that sign."""
        if not self._automark_pending:
            return
        now = time.monotonic()
        keep: list = []
        for actor, marker, name, queued_at, status_key in self._automark_pending:
            if now - queued_at > 30.0:
                continue   # age cap, the mechanic resolved long ago
            player_key = "me" if self._is_me_actor(actor, name) else actor
            live = self._automark_active.get(player_key)
            if live is not None and live != status_key:
                continue   # another rule's sign is up, the stale retry must not overwrite it
            if not self._mark_player(actor, marker, name):
                keep.append((actor, marker, name, queued_at, status_key))
            else:
                # late mark landed, track it for clear-on-loss like a live one
                self._automark_active[player_key] = status_key
        self._automark_pending = keep

    def _apply_automark_state(self) -> None:
        """Sync the native Telesto client to the saved URL and on/off. `enabled`
        gates whether rules auto-fire. Test forces a send regardless. On enable,
        pull the party list right away so party-scoped rules don't wait for the
        10s refresh tick. On disable, bring engine-placed and rule-placed signs
        down first, the same reset-with-clear the chain and gaze toggles run."""
        tc = getattr(self, "_telesto_client", None)
        if tc is None:
            return
        enabled = bool(self._settings.get("telesto_enabled", False))
        if not enabled:
            # Forced, and before configure takes the client down. The plain
            # clears fail closed on a disabled client, which is how turning
            # automarkers off used to strand the signs the engines had placed.
            self._umad_chain_reset(clear_marks=True, force=True)
            self._umad_gaze_reset(clear_marks=True, force=True)
            # Rule marks are the third placement path and stranded the same
            # way: once the client is down a loss line never reaches the
            # unmark, and a plain clear would fail closed anyway. Bring those
            # signs down forced too and drop the bookkeeping. "me" routes to
            # clear_self, _clear_player would read the key as an actor id.
            for key in list(self._automark_active):
                if key == "me":
                    tc.clear_self(force=True)
                else:
                    self._clear_player(key, force=True)
            self._automark_active.clear()
        tc.configure(uri=self._settings.get("telesto_uri", DEFAULT_TELESTO_URI),
                     enabled=enabled)
        if enabled:
            tc.request_party_members()
        else:
            self._automark_pending.clear()   # no retries while automarkers are off
        self._update_automark_status_label()

    def _on_telesto_client_status(self, reachable: bool, message: str, degraded: bool = False) -> None:
        """Telesto reachability from the native client, Test button and user
        marks. degraded means it answers but errors, persistent HTTP non-2xx.
        Not down, but every mark is failing, so show amber instead of green."""
        self._telesto_status = "bad" if not reachable else ("degraded" if degraded else "good")
        self._update_automark_status_label()

    def _on_telesto_status(self, _status: str) -> None:
        # marking is native now so the engine's own Telesto probe is vestigial.
        # It used to clobber the real client status here, the green light
        # flapping to "off" a moment after boot. _on_telesto_client_status is
        # the single source of truth for the indicator. Ignore the engine's view.
        return

    @staticmethod
    def _is_dot(t) -> bool:
        """A DoT or maintained-debuff trigger, one carrying a reapply timer
        that fires pre-expiry. Used to split the General tab into a General
        utility group and a DoT group."""
        return (getattr(t, "expiry_warn_s", 0) or 0) > 0

    def _umad_gaze_cast(self, fields: list[str]) -> None:
        """Route a 20 StartsCast into the gaze engine when it is a wave's
        fire or water followup, Inferno or Tsunami. That cast tells whether
        the wave's gaze set is the fake look-at pair or the real look-away
        one, and its cast line starts ~4s before the shriek gains land.
        20|ts|srcId|src|abilityId|ability|x|y|z|heading
        Cheap rejection first, this runs for every cast line in combat."""
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
