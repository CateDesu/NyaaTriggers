"""MainWindow callbacks for update checks, downloads and installation. Update policy lives
in updater.py.
"""

import json
import sys
import threading
import urllib.request

from nyaatriggers.http_fetch import fetch_bytes

from PyQt6.QtCore import Qt, QUrl
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import QFrame, QProgressBar, QVBoxLayout, QHBoxLayout, QPushButton, QLabel

from nyaatriggers.tts import kokoro_ready, download_kokoro_model, install_kokoro_deps
from nyaatriggers.locale_util import _
from nyaatriggers.triggevent_bridge import update_engine as _te_update_engine
from nyaatriggers.plugin_link import plugin_supports_dps
from nyaatriggers import theme
from nyaatriggers import updater

from nyaatriggers import app_common as ac
from nyaatriggers.app_common import (
    _ITEM_TYPE_ROLE, _REPO_JSON_MAX_BYTES, _REPO_TRIGGERS_BRANCH, _VERSION, _atomic_write_json,
)


class UpdaterUiMixin:
    def _init_update_flow(self) -> None:
        self._pending_release = None
        self._trig_dl_in_flight: dict = {}   # Download button to original label.
        self._manual_check_in_flight = False
        self._install_in_flight = False
        self._update_action = "install"   # "install" | "openpage" | "restart"
        self._update_applied_version = None   # set once an update is applied this session
        # The banner also shows cactbot failures. Only update offers can be snoozed.
        self._update_banner_mode = "update"   # "update" | "cactbot"

    def _update_live_dps(self) -> None:
        """Show the selected past pull until new damage returns the meter to the live feed.
        Between encounters, preserve the last pull.
        """
        title_lbl = getattr(self, "_dps_live_title", None)
        table = getattr(self, "_dps_live_table", None)
        if title_lbl is None or table is None:
            return
        back_btn = getattr(self, "_dps_back_btn", None)
        snap = self._dps_meter.snapshot()
        live_has_damage = snap["isActive"] and any(
            c.get("damage", 0) > 0 for c in snap["Combatant"].values())
        if live_has_damage and not self._dps_live_active:
            self._dps_selected_idx = None
        self._dps_live_active = live_has_damage

        idx = self._dps_selected_idx
        if back_btn is not None:
            back_btn.setVisible(idx is not None)
        if idx is not None and idx < len(self._dps_history):
            entry = self._dps_history[idx]
            s = entry["snapshot"]
            enc = s["Encounter"]
            title_lbl.setText(
                _("Reviewing: {title} ({dur}, {when})").format(
                    title=enc["title"], dur=enc["duration"], when=entry["when"]))
            self._dps_live_encdps.setText(
                _("party DPS {dps}").format(dps=self._fmt_dps_num(enc["encdps"])))
            self._populate_dps_table(table, s)
            return

        if not snap["isActive"] and not snap["Combatant"]:
            title_lbl.setText(_("No active encounter"))
            self._dps_live_encdps.setText("")
            table.setRowCount(0)
            return
        enc = snap["Encounter"]
        title = f"{enc['title']} — {enc['duration']}"
        if not snap["isActive"]:
            title += " " + _("(last pull)")
        title_lbl.setText(title)
        self._dps_live_encdps.setText(
            _("party DPS {dps}").format(dps=self._fmt_dps_num(enc["encdps"])))
        self._populate_dps_table(table, snap)

    def _update_fflogs_visibility(self) -> None:
        """Show the FFLogs line only once credentials and server are set."""
        lbl = getattr(self, "_fflogs_lbl", None)
        if lbl is None:
            return
        visible = self._fflogs_configured()
        lbl.setVisible(visible)
        self._fflogs_btn.setVisible(visible)
        if not visible:
            lbl.setText("")

    def _update_plugin_link_status_label(self, connected: bool, msg: str) -> None:
        lbl = getattr(self, "_plugin_link_status_lbl", None)
        if lbl is None:
            return
        link = getattr(self, "_plugin_link", None)
        if connected:
            version = link.plugin_version() if link is not None else ""
            msg = (_("Connected to plugin {version}").format(version=version)
                   if version else _("Connected"))
        elif msg == "Off":
            msg = _("Off")
        elif msg == "Waiting for the game plugin":
            msg = _("Waiting for the game plugin")
        if (connected and link is not None
                and not plugin_supports_dps(link.plugin_version())):
            # Older protocol 1 plugins can connect without meter support. Show that
            # limitation in the status.
            msg += " " + _("too old for the DPS meter, update the plugin")
        lbl.setText(f"● {msg}")
        lbl.setStyleSheet(
            f"color:{'#a6e3a1' if connected else '#8f8f9a'}; font-weight:bold;")

    def _update_automark_status_label(self) -> None:
        # Show Telesto connectivity even with rules disabled because marker tests still
        # use it.
        lbl = getattr(self, "_automark_status_lbl", None)
        if lbl is None:
            return
        st = self._telesto_status
        if st == "good":
            lbl.setText(_("● Connected to Telesto"))
            lbl.setStyleSheet("color:#a6e3a1; font-weight:bold;")
        elif st == "degraded":
            lbl.setText(_("● Telesto reachable, but calls are failing"))
            lbl.setStyleSheet("color:#f9e2af; font-weight:bold;")
        elif st == "bad":
            lbl.setText(_("● Telesto not reachable"))
            lbl.setStyleSheet("color:#f38ba8; font-weight:bold;")
        else:
            lbl.setText(_("● Telesto: not checked yet"))
            lbl.setStyleSheet("color:#8f8f9a; font-weight:bold;")

    def _update_engine_status_label(self) -> None:
        # Show failed sidecars in red and running ones in green. Hide the indicator when
        # unused or intentionally stopped.
        lbl = getattr(self, "_engine_status_lbl", None)
        if lbl is None:
            return
        names = {"triggevent": _("Triggevent"), "triggernometry": _("Triggernometry")}
        bad = [(src, msg) for src, (st, msg) in self._engine_sidecar_state.items() if st == "bad"]
        if bad:
            src, msg = bad[0]
            full = _("● {name} Engine: {msg}").format(name=names.get(src, src), msg=msg)
            # Keep the full status in the tooltip when the header label is truncated.
            lbl.setText(lbl.fontMetrics().elidedText(
                full, Qt.TextElideMode.ElideRight, 420))
            lbl.setToolTip(full)
            lbl.setStyleSheet("color:#f38ba8; font-weight:bold;")
            lbl.setVisible(True)
            return
        good = [src for src, (st, _msg) in self._engine_sidecar_state.items() if st == "good"]
        if good:
            lbl.setText(_("● {names} Engine").format(
                names=", ".join(names.get(s, s) for s in good)))
            lbl.setStyleSheet("color:#a6e3a1; font-weight:bold;")
            lbl.setVisible(True)
            return
        lbl.setVisible(False)

    def _update_fight_controls(self) -> None:
        """Refresh fight checkboxes from the actual enabled state of their triggers."""
        bar = getattr(self, "_fight_bar", None)
        if bar is None:
            return
        it = self._tree.currentItem()
        item_type  = it.data(0, _ITEM_TYPE_ROLE) if it else None
        selectable = bool(it and (it.flags() & Qt.ItemFlag.ItemIsSelectable))
        if not selectable or item_type in ("folder", "custom_group"):
            bar.setVisible(False)
            return
        fight = it.data(0, Qt.ItemDataRole.UserRole) or ""
        self._fight_cur = fight
        locals_ = self._fight_local_triggers(fight)
        tv_ids  = self._fight_tv_ids(fight)
        if not locals_ and not tv_ids:
            bar.setVisible(False)
            return
        bar.setVisible(True)
        self._fight_bar_lbl.setText(it.text(0).lstrip("▶▼ "))
        tv_dis   = self._engine_disabled.get("triggevent", set())
        local_on = bool(locals_) and all(t.enabled for t in locals_)
        tv_on    = bool(tv_ids)  and all(tid not in tv_dis for tid in tv_ids)
        for c in (self._cb_local, self._cb_tv):
            c.blockSignals(True)
        self._cb_local.setChecked(local_on)
        self._cb_tv.setChecked(tv_on)
        self._cb_local.setEnabled(bool(locals_))
        self._cb_tv.setEnabled(bool(tv_ids))
        for c in (self._cb_local, self._cb_tv):
            c.blockSignals(False)

    def _on_kokoro_download(self) -> None:
        # Allow one voice setup at a time because concurrent installs share the
        # environment and model files.
        if getattr(self, "_kokoro_setup_running", False):
            return
        self._kokoro_setup_running = True
        self._kokoro_dl_btn.setEnabled(False)
        self._kokoro_dl_btn.setText(_("Downloading…"))

        def _setup() -> None:
            status = "no-deps:"
            try:
                # Install dependencies before downloading model files.
                _deps_ok, log = install_kokoro_deps()
                model_ok = download_kokoro_model()
                if kokoro_ready():
                    status = "ready"
                elif not model_ok:
                    status = "no-model"
                else:                   # model present but deps/phonemizer failed
                    status = "no-deps:" + (log or "")[:300]
            except Exception as exc:    # noqa: BLE001
                status = "no-deps:" + repr(exc)[:300]
            self._kokoro_dl_signal.emit(status)

        try:
            threading.Thread(target=_setup, daemon=True).start()
        except Exception:  # noqa: BLE001
            self._kokoro_setup_running = False
            self._kokoro_dl_btn.setEnabled(True)
            self._kokoro_dl_btn.setText(_("Download"))

    def _build_update_banner(self, root: QVBoxLayout) -> None:
        bar = QFrame()
        bar.setObjectName("updateBanner")
        row = QHBoxLayout(bar)
        row.setContentsMargins(10, 6, 8, 6)
        row.setSpacing(8)

        self._upd_msg = QLabel("")
        self._upd_msg.setStyleSheet(f"color: {theme.ACCENT}; font-weight: bold;")
        row.addWidget(self._upd_msg)

        self._upd_progress = QProgressBar()
        self._upd_progress.setMaximumWidth(220)
        self._upd_progress.setVisible(False)
        row.addWidget(self._upd_progress)
        row.addStretch()

        self._upd_install_btn = QPushButton(_("Install"))
        self._upd_install_btn.clicked.connect(self._on_update_install_clicked)
        row.addWidget(self._upd_install_btn)

        self._upd_notes_btn = QPushButton(_("Release notes"))
        self._upd_notes_btn.clicked.connect(self._on_update_notes_clicked)
        row.addWidget(self._upd_notes_btn)

        self._upd_dismiss_btn = QPushButton("✕")
        self._upd_dismiss_btn.setFixedWidth(28)
        self._upd_dismiss_btn.clicked.connect(self._on_update_dismiss_clicked)
        row.addWidget(self._upd_dismiss_btn)

        bar.setVisible(False)
        self._update_banner = bar
        root.addWidget(bar)

    def _check_for_updates(self) -> None:
        """Check for updates manually and report when no banner is needed."""
        if self._manual_check_in_flight:
            return
        self._manual_check_in_flight = True
        self._chk_updates_btn.setEnabled(False)
        self._chk_updates_btn.setText(_("Checking..."))
        self._start_update_check(manual=True)

    def _start_update_check(self, manual: bool) -> None:
        def _work() -> None:
            try:
                try:
                    rel = updater.fetch_latest_release(timeout=8, channel="stable")
                except updater.RateLimited:
                    # Use the last cached release when API limits prevent a fresh
                    # lookup.
                    rel = updater.read_cached_release()
                    if rel is None:
                        self._upd_available_signal.emit(None)
                        self._upd_checkmsg_signal.emit(
                            manual,
                            _("Update check failed - GitHub rate limit reached, try again later")
                            if manual else "")
                        return
                # Snooze rolling tags for source installs whose base version stays
                # unchanged. Manual checks bypass the snooze.
                snoozed = (not manual and not updater.is_frozen()
                           and rel.tag and rel.tag == self._settings.get("update_snoozed"))
                # Check whether git already contains the release commit because the base
                # version alone cannot tell.
                covers = (updater.install_kind() == "git"
                          and updater.git_covers_upstream())
                if (rel.version and not snoozed and not covers
                        and updater.is_update_for_here(rel.version, _VERSION)):
                    self._upd_available_signal.emit(rel)
                    self._upd_checkmsg_signal.emit(manual, "")
                else:
                    self._upd_available_signal.emit(None)
                    self._upd_checkmsg_signal.emit(
                        manual,
                        _("Up to date (v{version})").format(version=_VERSION) if manual else "")
            except Exception:
                self._upd_available_signal.emit(None)
                self._upd_checkmsg_signal.emit(
                    manual,
                    _("Update check failed - no network or GitHub unreachable")
                    if manual else "")
        # Handle thread startup failures as well as worker failures.
        try:
            threading.Thread(target=_work, daemon=True).start()
        except Exception:  # noqa: BLE001
            self._upd_checkmsg_signal.emit(
                manual,
                _("Update check failed - could not start") if manual else "")

    def _on_update_checkmsg(self, manual: bool, msg: str) -> None:
        """Show manual check feedback. Keep the button disabled if another manual check is
        still running.
        """
        if manual:
            self._manual_check_in_flight = False
            self._chk_updates_btn.setEnabled(True)
            self._chk_updates_btn.setText(_("Check for Updates"))
        if msg:
            ac.QMessageBox.information(self, _("Update Check"), msg)

    def _on_update_available(self, rel) -> None:
        if rel is None:
            return
        try:
            self._show_update_banner(rel)
        except Exception:  # noqa: BLE001
            print(f"_on_update_available raised: version={getattr(rel, 'version', None)!r}",
                  file=sys.stderr)
            try:
                ac.log_drop("update", "_on_update_available raised")
            except Exception:  # noqa: BLE001
                pass

    def _show_update_banner(self, rel) -> None:
        # Do not let a check result replace installation progress. The install updates
        # the banner when it finishes.
        if self._install_in_flight:
            return
        self._pending_release = rel
        self._update_banner_mode = "update"
        self._upd_progress.setVisible(False)
        if rel.version and rel.version == self._update_applied_version:
            self._update_action = "restart"
            self._upd_msg.setText(
                _("NyaaTriggers v{version} installed - restart to finish.").format(version=rel.version))
            self._upd_install_btn.setText(_("Restart now"))
            self._upd_notes_btn.setVisible(False)
        elif (updater.install_kind() == "frozen-windows"
              and updater.is_rejected_update(rel.version)):
            self._update_action = "openpage"
            self._upd_msg.setText(
                _("NyaaTriggers v{version} failed to start and was rolled back. "
                  "Download it manually.").format(version=rel.version))
            self._upd_install_btn.setText(_("Download"))
            self._upd_notes_btn.setVisible(True)
        elif updater.can_self_apply():
            self._update_action = "install"
            self._upd_msg.setText(_("NyaaTriggers v{version} is available.").format(version=rel.version))
            self._upd_install_btn.setText(_("Install"))
            self._upd_notes_btn.setVisible(True)
        else:
            self._update_action = "openpage"
            self._upd_msg.setText(_("NyaaTriggers v{version} is available.").format(version=rel.version))
            self._upd_install_btn.setText(_("Download"))
            self._upd_notes_btn.setVisible(True)
        self._upd_install_btn.setVisible(True)
        self._upd_install_btn.setEnabled(True)
        self._upd_dismiss_btn.setVisible(True)
        self._update_banner.setVisible(True)

    def _on_update_dismiss_clicked(self) -> None:
        self._update_banner.setVisible(False)
        # A cactbot warning can share a banner with a pending release. Dismissing it
        # must not snooze the update.
        if self._update_banner_mode == "update":
            self._snooze_offered_update()

    def _snooze_offered_update(self) -> None:
        """Snooze the offered tag for source installs whose version stays at the base.
        Frozen builds use the full release stamp.
        """
        rel = self._pending_release
        if rel is not None and rel.tag and not updater.is_frozen():
            self._settings["update_snoozed"] = rel.tag
            self._save_settings()

    def _on_update_notes_clicked(self) -> None:
        rel = self._pending_release
        url = (rel.html_url if rel else "") or updater.RELEASES_URL
        QDesktopServices.openUrl(QUrl(url))

    def _on_update_install_clicked(self) -> None:
        if self._update_action == "restart":
            self._restart_for_update()
            return
        rel = self._pending_release
        if rel is None:
            return
        if self._update_action == "openpage":
            self._snooze_offered_update()
            QDesktopServices.openUrl(QUrl(rel.html_url or updater.RELEASES_URL))
            return
        if self._install_in_flight:
            return
        kind = updater.install_kind()
        verb = (_("pull the latest code with git") if kind == "git" else
                _("download and install v{version}").format(version=rel.version))
        if ac.QMessageBox.question(
            self, _("Install update"),
            _("This will {action} and then restart NyaaTriggers. Continue?").format(action=verb),
        ) != ac.QMessageBox.StandardButton.Yes:
            return
        self._upd_progress.setRange(0, 0)
        self._upd_progress.setVisible(True)
        for b in (self._upd_install_btn, self._upd_notes_btn, self._upd_dismiss_btn):
            b.setVisible(False)
        self._upd_msg.setText(_("Starting update..."))
        self._install_in_flight = True
        self._start_install(rel, kind)

    def _on_update_progress(self, pct: int, msg: str) -> None:
        self._upd_progress.setVisible(True)
        if pct < 0:
            self._upd_progress.setRange(0, 0)         # indeterminate
        else:
            self._upd_progress.setRange(0, 100)
            self._upd_progress.setValue(pct)
        self._upd_msg.setText(msg)

    def _on_update_done(self, ok: bool, msg: str) -> None:
        try:
            self._handle_update_done(ok, msg)
        except Exception:  # noqa: BLE001
            # Log callback failures and leave the banner available for diagnosis.
            print(f"_on_update_done raised: ok={ok!r} msg={msg!r}", file=sys.stderr)
            try:
                ac.log_drop("update", f"_on_update_done raised (ok={ok!r})")
            except Exception:  # noqa: BLE001
                pass

    def _handle_update_done(self, ok: bool, msg: str) -> None:
        self._install_in_flight = False
        self._upd_progress.setVisible(False)
        if not ok:
            self._upd_msg.setText(_("Update failed."))
            ac.QMessageBox.warning(self, _("Update failed"), msg)
            # Offer a manual download after a failed Windows update.
            if (updater.install_kind() == "frozen-windows"
                    and self._pending_release is not None):
                self._update_action = "openpage"
                self._upd_msg.setText(_("Update failed - download it manually."))
                self._upd_install_btn.setText(_("Download"))
                self._upd_install_btn.setVisible(True)
                self._upd_install_btn.setEnabled(True)
                self._upd_notes_btn.setVisible(True)
                self._upd_dismiss_btn.setVisible(True)
                self._update_banner.setVisible(True)
            else:
                if self._pending_release is not None:
                    self._on_update_available(self._pending_release)
            return
        # Quit after the Windows handoff to release file locks. The staged updater
        # relaunches the program.
        if msg == "__windows_handoff__":
            self._upd_msg.setText(_("Installing update - NyaaTriggers will reopen..."))
            self._quit_for_windows_handoff()
            return
        if self._pending_release is not None:
            self._update_applied_version = self._pending_release.version
            # Snooze the tag for source installs because their base version remains
            # unchanged after pulling.
            self._snooze_offered_update()
        self._upd_msg.setText(_("Update installed - restart to finish."))
        if ac.QMessageBox.question(
            self, _("Restart"),
            _("Update installed. Restart NyaaTriggers now to finish?"),
        ) == ac.QMessageBox.StandardButton.Yes:
            self._restart_for_update()
        else:
            # The files are updated but the running process still uses the old build.
            self._upd_msg.setText(
                _("Update installed - the files on disk are now the new version. "
                  "Restart NyaaTriggers soon to run it."))
            self._update_action = "restart"
            self._upd_install_btn.setText(_("Restart now"))
            self._upd_install_btn.setVisible(True)
            self._upd_install_btn.setEnabled(True)
            self._upd_dismiss_btn.setVisible(True)
            self._upd_notes_btn.setVisible(False)

    def _restart_for_update(self) -> None:
        # Stop child processes before exec because they survive a process replacement.
        # Isolate teardown steps so one failure cannot skip the rest.
        step = self._teardown_step
        # Stop timers before teardown callbacks can run.
        self._stop_background_timers()
        step("clear status timers", lambda: self._clear_status_timers())
        step("clear seq runners", lambda: self._clear_seq_runners())
        # Flush pending settings before replacing the process.
        step("settings save flush", lambda: self._flush_pending_settings_save())
        # Finish the active encounter before restarting.
        step("meter encounter finalize", lambda: self._finalize_live_encounter())
        step("ws disconnect", lambda: self._ws.disconnect_from())
        # This path bypasses closeEvent, so finalize the pull capture here too.
        step("pull capture finalize", lambda: self._pull_capture.close())
        step("cactbot reader stop", lambda: self._stop_cactbot_reader())
        # Wait for sidecar shutdown. exec would otherwise kill the escalation thread
        # before it can stop a stubborn child.
        step("triggevent stop", lambda: self._stop_sidecar("_triggevent", wait=True))
        step("triggernometry stop", lambda: self._stop_sidecar("_triggernometry", wait=True))
        step("telesto stop", lambda: self._stop_sidecar("_telesto_client"))
        step("plugin link stop", lambda: self._stop_sidecar("_plugin_link"))
        try:
            updater.relaunch()
        except Exception as exc:  # noqa: BLE001
            ac.QMessageBox.warning(
                self, _("Restart failed"),
                _("Could not restart automatically: {error}\n\n"
                  "Please close and reopen NyaaTriggers to finish the update.").format(error=exc))

    def _on_auto_update_changed(self, state: int) -> None:
        self._settings["auto_check_updates"] = bool(state)
        self._save_settings()

    def _on_te_auto_update_changed(self, state: int) -> None:
        self._settings["triggevent_auto_update"] = bool(state)
        self._save_settings()

    def _on_te_update_clicked(self) -> None:
        btn = getattr(self, "_te_update_btn", None)
        if btn is not None:
            btn.setEnabled(False)
            btn.setText(_("Updating…"))
        self._maybe_update_triggevent(manual=True)

    def _maybe_update_triggevent(self, manual: bool = False) -> None:
        """Pull and rebuild the engine in the background, reporting through
        _te_update_signal. Manual requests always receive feedback.
        """
        channel = "stable"
        # Serialize engine builds in the shared target directory. Remember manual
        # requests received during a run so they still get feedback.
        if getattr(self, "_te_update_running", False):
            if manual:
                self._te_update_pending_manual = True
            return
        self._te_update_running = True
        def _work():
            try:
                changed, msg = _te_update_engine(channel, manual)
            except Exception as exc:  # noqa: BLE001
                changed, msg = False, _("Triggevent update error: {error!r}").format(error=exc)
            self._te_update_signal.emit(changed, msg, manual)
        try:
            threading.Thread(target=_work, daemon=True).start()
        except Exception:  # noqa: BLE001
            self._te_update_running = False
            btn = getattr(self, "_te_update_btn", None)
            if btn is not None:
                btn.setEnabled(True)
                btn.setText(_("Update Triggevent Engine"))

    def _on_te_update_done(self, changed: bool, msg: str, manual: bool) -> None:
        self._te_update_running = False
        manual = manual or getattr(self, "_te_update_pending_manual", False)
        self._te_update_pending_manual = False
        print(f"[triggevent] {msg}", file=sys.stderr)
        btn = getattr(self, "_te_update_btn", None)
        if btn is not None:
            btn.setEnabled(True)
            btn.setText(_("Update Triggevent Engine"))
        if changed:
            ac.QMessageBox.information(self, _("Triggevent updated"), msg)
        elif manual:
            ac.QMessageBox.information(self, _("Triggevent"), msg)

    def _update_triggers_data(self) -> None:
        self._download_repo_triggers(self._update_trig_btn, _("Update Triggers"))

    def _download_repo_triggers(self, btn, label: str) -> None:
        # Reload repository triggers while preserving local overrides. Track each
        # download button separately so concurrent requests restore the right labels.
        if btn in self._trig_dl_in_flight:
            return
        self._trig_dl_in_flight[btn] = label
        btn.setEnabled(False)
        btn.setText(_("Downloading..."))

        def _fetch() -> None:
            try:
                req = urllib.request.Request(
                    f"https://raw.githubusercontent.com/{updater.REPO}/{_REPO_TRIGGERS_BRANCH}/assets/triggers.json",
                    headers={"User-Agent": "NyaaTriggers"},
                )
                raw = fetch_bytes(req, _REPO_JSON_MAX_BYTES)
                if len(raw) > _REPO_JSON_MAX_BYTES:
                    raise ValueError("triggers.json response too large")
                data = json.loads(raw)
                if not isinstance(data, list):
                    raise ValueError("Unexpected format - not a list")
                # Download to untracked cache names so source checkouts keep their
                # tracked trigger files unchanged.
                _atomic_write_json(ac._REPO_TRIGGERS_FILE, data, indent=2)
                # Refresh retirements too, without failing the trigger update if they
                # are unavailable.
                try:
                    rreq = urllib.request.Request(
                        f"https://raw.githubusercontent.com/{updater.REPO}/{_REPO_TRIGGERS_BRANCH}/assets/retired.json",
                        headers={"User-Agent": "NyaaTriggers"},
                    )
                    rraw = fetch_bytes(rreq, _REPO_JSON_MAX_BYTES)
                    if len(rraw) > _REPO_JSON_MAX_BYTES:
                        raise ValueError("retired.json response too large")
                    rdata = json.loads(rraw)
                    if isinstance(rdata, (dict, list)):
                        _atomic_write_json(ac._REPO_RETIRED_FILE, rdata, indent=2)
                except Exception:  # noqa: BLE001
                    pass
                # Write the stamp last so incomplete downloads are ignored.
                _atomic_write_json(ac._REPO_TRIGGERS_VERSION, _VERSION)
                self._trig_update_signal.emit(btn, f"ok:{len(data)}")
            except Exception as exc:
                self._trig_update_signal.emit(btn, f"err:{exc}")

        try:
            threading.Thread(target=_fetch, daemon=True).start()
        except Exception:  # noqa: BLE001
            self._trig_dl_in_flight.pop(btn, None)
            btn.setEnabled(True)
            btn.setText(label)

    def _on_trig_update_result(self, btn, msg: str) -> None:
        label = self._trig_dl_in_flight.pop(btn, None)
        if label is None:
            return
        btn.setEnabled(True)
        btn.setText(label)
        if msg.startswith("ok:"):
            count = msg[3:]
            self._load_triggers()
            ac.QMessageBox.information(self, _("Bundled Triggers Reloaded"),
                                    _("Loaded {count} bundled triggers from the repo. "
                                      "Your own (custom and edited) triggers are untouched.").format(count=count))
        else:
            ac.QMessageBox.warning(self, _("Download Failed"), msg[4:])
