"""Update banner, channel logic and the staged download and apply flow.
UI callbacks live here, policy lives in updater.py. Mixin for MainWindow,
all state rides on self.
"""

import json
import math
import os
import re
import sys
import threading
import time
import urllib.request

from PyQt6.QtCore import Qt, QUrl
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import QFrame, QProgressBar, QVBoxLayout, QHBoxLayout, QPushButton, QLabel

from tts import kokoro_ready, download_kokoro_model, install_kokoro_deps
from locale_util import _
from triggevent_bridge import update_engine as _te_update_engine
from plugin_link import plugin_supports_dps
import theme
import updater

import app_common as ac
from app_common import (
    _ITEM_TYPE_ROLE, _REPO_JSON_MAX_BYTES, _REPO_TRIGGERS_BRANCH, _VERSION, _atomic_write_json,
)


class UpdaterUiMixin:
    def _init_update_flow(self) -> None:
        self._pending_release = None
        self._trig_dl_in_flight: dict = {}   # repo trigger download, button to its label
        self._manual_check_in_flight = False   # a manual update check is running
        self._install_in_flight = False        # an update install is running
        self._update_action = "install"   # "install" | "openpage" | "restart"
        self._update_applied_version = None   # set once an update is applied this session
        # The banner widget is shared by update offers and the cactbot
        # failure warning. Dismiss only snoozes in update mode.
        self._update_banner_mode = "update"   # "update" | "cactbot"

    def _update_live_dps(self) -> None:
        """Repaint the meter. Shows the live main feed by default. When a past
        pull is selected it shows that pull instead. A new pull's first strike
        reclaims the main feed. Between pulls the live feed is the preserved
        last pull."""
        title_lbl = getattr(self, "_dps_live_title", None)
        table = getattr(self, "_dps_live_table", None)
        if title_lbl is None or table is None:
            return
        back_btn = getattr(self, "_dps_back_btn", None)
        snap = self._dps_meter.snapshot()
        live_has_damage = snap["isActive"] and any(
            c.get("damage", 0) > 0 for c in snap["Combatant"].values())
        # A new pull landing its first damage grabs the main feed back from
        # whatever past pull was being reviewed.
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
        if (connected and link is not None
                and not plugin_supports_dps(link.plugin_version())):
            # The wire stayed at protocol 1 when the meter arrived, so a
            # plugin too old for it connects cleanly and just never draws it.
            # Say so on the label, otherwise Connected reads as all working.
            msg += " " + _("too old for the DPS meter, update the plugin")
        lbl.setText(f"● {msg}")
        lbl.setStyleSheet(
            f"color:{'#a6e3a1' if connected else '#8f8f9a'}; font-weight:bold;")

    def _update_automark_status_label(self) -> None:
        # the indicator reflects the Telesto connection, meaningful for Test
        # regardless of the rules toggle. "Enable automarkers" gates auto-fire.
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
        # mirrors _update_automark_status_label. Red when a sidecar died or
        # failed to launch, green while one runs, hidden when none ever
        # reported, feature unused, or all were switched off on purpose
        lbl = getattr(self, "_engine_status_lbl", None)
        if lbl is None:
            return
        names = {"triggevent": _("Triggevent"), "triggernometry": _("Triggernometry")}
        bad = [(src, msg) for src, (st, msg) in self._engine_sidecar_state.items() if st == "bad"]
        if bad:
            src, msg = bad[0]
            full = _("● {name} Engine: {msg}").format(name=names.get(src, src), msg=msg)
            # The header lost its old full-width run to the sidebar, so cap the
            # label and keep the whole message on the tooltip.
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
        """Show or refresh the per-fight Local and Triggevent checkboxes.
        Each box derives from the fight's actual enabled-state, checked
        means fully on, so global toggles, per-row changes, and direct
        clicks all reflect here."""
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
        # One setup at a time. Switching between two JP voices mid-download
        # used to spawn a second pip install into the same venv, corrupting
        # it, and a second ~330 MB model download over the first one's
        # .part files.
        if getattr(self, "_kokoro_setup_running", False):
            return
        self._kokoro_setup_running = True
        self._kokoro_dl_btn.setEnabled(False)
        self._kokoro_dl_btn.setText(_("Downloading…"))

        def _setup() -> None:
            status = "no-deps:"   # default so the signal always fires
            try:
                # The app does the install itself, no manual pip. Deps
                # first, pip installing the kokoro-onnx wheel and its
                # dependencies, then the model files.
                _deps_ok, log = install_kokoro_deps()
                model_ok = download_kokoro_model()
                if kokoro_ready():
                    status = "ready"
                elif not model_ok:
                    status = "no-model"
                else:                   # model present but deps/phonemizer failed
                    status = "no-deps:" + (log or "")[:300]
            except Exception as exc:    # noqa: BLE001 - never leave the button stuck
                status = "no-deps:" + repr(exc)[:300]
            self._kokoro_dl_signal.emit(status)

        try:
            threading.Thread(target=_setup, daemon=True).start()
        except Exception:  # noqa: BLE001 - a failed start must not strand the button
            self._kokoro_setup_running = False
            self._kokoro_dl_btn.setEnabled(True)
            self._kokoro_dl_btn.setText(_("Download"))

    def _build_update_banner(self, root: QVBoxLayout) -> None:
        """A slim notification strip shown above the tabs when an update is
        available. Hidden by default."""
        bar = QFrame()
        bar.setObjectName("updateBanner")
        # Styling comes from the QSS #updateBanner rule in theme.py.
        row = QHBoxLayout(bar)
        row.setContentsMargins(10, 6, 8, 6)
        row.setSpacing(8)

        self._upd_msg = QLabel("")
        self._upd_msg.setStyleSheet(f"color: {theme.GOLD_LT}; font-weight: bold;")
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
        """Manual 'Check for Updates' button. Same check, with up-to-date
        and error feedback shown in a dialog."""
        if self._manual_check_in_flight:
            return   # one manual run at a time
        self._manual_check_in_flight = True
        self._chk_updates_btn.setEnabled(False)
        self._chk_updates_btn.setText(_("Checking..."))
        self._start_update_check(manual=True)

    def _start_update_check(self, manual: bool) -> None:
        def _work() -> None:
            try:
                rel = updater.fetch_latest_release(timeout=8, channel="stable")
            except updater.RateLimited:
                # 60 anonymous requests per hour per IP, and a shared NAT or
                # VPN can exhaust that before this launch ever asks. Fall back
                # to the last good answer so a pending update still surfaces.
                rel = updater.read_cached_release()
                if rel is None:
                    self._upd_available_signal.emit(None)
                    self._upd_checkmsg_signal.emit(
                        manual,
                        _("Update check failed - GitHub rate limit reached, try again later")
                        if manual else "")
                    return
            except Exception:
                self._upd_available_signal.emit(None)
                self._upd_checkmsg_signal.emit(
                    manual,
                    _("Update check failed - no network or GitHub unreachable")
                    if manual else "")
                return
            # Git and source installs get re-offered every rolling tag,
            # one per push to main. Their _VERSION stays at the base
            # even after a git pull, so the tag still compares newer.
            # Dismiss, Download, or a successful Install snoozes the
            # tag. A manual check always bypasses the snooze because
            # the user explicitly asked.
            snoozed = (not manual and not updater.is_frozen()
                       and rel.tag and rel.tag == self._settings.get("update_snoozed"))
            # A git checkout that already contains the upstream tip built or
            # pulled the commit this release was cut from. The tag math can
            # never see that, base _VERSION never moves, so ask git directly
            # and stay quiet instead of offering a maintainer their own push.
            covers = (updater.install_kind() == "git"
                      and updater.git_covers_upstream())
            if (rel.version and not snoozed and not covers
                    and updater.is_update_for_here(rel.version, _VERSION)):
                self._upd_available_signal.emit(rel)
                self._upd_checkmsg_signal.emit(manual, "")   # banner handles it
            else:
                self._upd_available_signal.emit(None)
                self._upd_checkmsg_signal.emit(
                    manual,
                    _("Up to date (v{version})").format(version=_VERSION) if manual else "")
        # The worker body is fully guarded. Guard the thread start too, so nothing
        # in the update-check entry point can propagate out and crash the app.
        try:
            threading.Thread(target=_work, daemon=True).start()
        except Exception:  # noqa: BLE001
            self._upd_checkmsg_signal.emit(
                manual,
                _("Update check failed - could not start") if manual else "")

    def _on_update_checkmsg(self, manual: bool, msg: str) -> None:
        """Manual-check feedback only. Empty when a banner was shown
        instead. An auto check finishing while a manual one is still in
        flight must not re-enable the button, or a second click would stack
        two result dialogs."""
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
        except Exception:  # noqa: BLE001 - a banner failure must not crash the app
            # Same diagnostics as _on_update_done. A failure here otherwise
            # means an available update never surfaces, with zero trace.
            print(f"_on_update_available raised: version={getattr(rel, 'version', None)!r}",
                  file=sys.stderr)
            try:
                ac.log_drop("update", "_on_update_available raised")
            except Exception:  # noqa: BLE001
                pass

    def _show_update_banner(self, rel) -> None:
        # A check result landing mid-install must not reset the banner. It
        # would hide the live progress bar and re-enable an Install button
        # that only no-ops, and dismissing it would snooze the offered tag.
        # The running install repaints the banner when it finishes.
        if self._install_in_flight:
            return
        self._pending_release = rel
        self._update_banner_mode = "update"
        self._upd_progress.setVisible(False)
        # If this exact version was already applied this session, don't re-offer
        # a download. Just prompt to restart.
        if rel.version and rel.version == self._update_applied_version:
            self._update_action = "restart"
            self._upd_msg.setText(
                _("NyaaTriggers v{version} installed - restart to finish.").format(version=rel.version))
            self._upd_install_btn.setText(_("Restart now"))
            self._upd_notes_btn.setVisible(False)
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
        # Only an actual update offer snoozes. Dismissing the cactbot
        # failure warning must not pin the offered tag, the banner widget
        # is shared and _pending_release survives underneath it.
        if self._update_banner_mode == "update":
            self._snooze_offered_update()

    def _snooze_offered_update(self) -> None:
        """Record the offered tag so this install stops being re-offered
        it. Only for git and source installs, not frozen. Their _VERSION
        never carries the rolling stamp, so the tag still compares newer
        even after a git pull already installed it. Frozen builds stamp
        the full version on update, compare equal afterwards, and never
        re-offer."""
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
            # They got the releases page. Don't re-offer this tag on source
            # installs. No-op for frozen installs, the post-failure
            # fallback.
            self._snooze_offered_update()
            QDesktopServices.openUrl(QUrl(rel.html_url or updater.RELEASES_URL))
            return
        if self._install_in_flight:
            return   # one install at a time
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
        except Exception:  # noqa: BLE001 - reporting a result must not crash the app
            # Surface to stderr and the drop log so a stuck "Installing..."
            # banner is diagnosable instead of silent. The banner stays
            # visible because we cannot safely tear it down from an unknown
            # failure point.
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
            # A failed Windows update, locked files, AV, permissions, won't
            # fare better on an immediate retry. Steer to the manual
            # download.
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
                # Restore the notify state so the user can retry or dismiss.
                if self._pending_release is not None:
                    self._on_update_available(self._pending_release)
            return
        # Windows can't swap its own running files. apply_frozen_windows
        # staged the new build and launched it to finish the swap once we
        # exit. Quit now so the OS releases our locks. The staged copy
        # reopens us.
        if msg == "__windows_handoff__":
            self._upd_msg.setText(_("Installing update - NyaaTriggers will reopen..."))
            self._quit_for_windows_handoff()
            return
        if self._pending_release is not None:
            self._update_applied_version = self._pending_release.version
            # A git checkout's _VERSION stays at the base after the pull,
            # so the tag still compares newer after the restart. Snooze it
            # now. No-op for frozen builds, which got the full version
            # stamped.
            self._snooze_offered_update()
        self._upd_msg.setText(_("Update installed - restart to finish."))
        if ac.QMessageBox.question(
            self, _("Restart"),
            _("Update installed. Restart NyaaTriggers now to finish?"),
        ) == ac.QMessageBox.StandardButton.Yes:
            self._restart_for_update()
        else:
            # The files on disk are already the new version. What is
            # running is the old build, and the gap shows in lazily loaded
            # Qt plugins and data files, so say so instead of leaving the
            # banner reading like nothing happened yet.
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
        # Tear down network and sidecars so nothing is left dangling,
        # then re-exec. os.execv keeps our PID but does NOT kill child processes,
        # so a sidecar left running here becomes an orphan the new instance can't
        # see. The same JVM/Mono leak closeEvent already avoids. Each step runs
        # isolated through _teardown_step, so one failed stop cannot skip the
        # rest and prints to stderr instead of vanishing right before the
        # re-exec.
        step = self._teardown_step
        # No timer may fire into the teardown either, same set closeEvent stops.
        self._stop_background_timers()
        step("clear status timers", lambda: self._clear_status_timers())   # no reapply warning may fire mid-teardown
        step("clear seq runners", lambda: self._clear_seq_runners())
        # A debounced settings save still pending dies with the process.
        # Flush it so the last slider position survives the re-exec.
        step("settings save flush", lambda: self._flush_pending_settings_save())
        # Finalize an in-progress meter encounter like closeEvent does,
        # or a restart mid-fight loses the active pull.
        step("meter encounter finalize", lambda: self._finalize_live_encounter())
        step("ws disconnect", lambda: self._ws.disconnect_from())
        step("cactbot reader stop", lambda: self._stop_cactbot_reader())
        # wait=True like the Windows handoff. Without it the stop only
        # spawns the SIGKILL-escalation thread and execv kills that thread
        # mid-countdown, so a stubborn JVM or Mono child outlives us into
        # the new instance.
        step("triggevent stop", lambda: self._stop_sidecar("_triggevent", wait=True))
        step("triggernometry stop", lambda: self._stop_sidecar("_triggernometry", wait=True))
        step("telesto stop", lambda: self._stop_sidecar("_telesto_client"))
        step("plugin link stop", lambda: self._stop_sidecar("_plugin_link"))
        try:
            updater.relaunch()
        except Exception as exc:  # noqa: BLE001 - execv failed, tell the user
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
        """Manual 'Update Triggevent Engine' button. Pull and rebuild on
        demand."""
        btn = getattr(self, "_te_update_btn", None)
        if btn is not None:
            btn.setEnabled(False)
            btn.setText(_("Updating…"))
        self._maybe_update_triggevent(manual=True)

    def _maybe_update_triggevent(self, manual: bool = False) -> None:
        """Background work. Pull and rebuild the Triggevent Engine if its
        source is behind upstream master. Off the GUI thread, it can take
        minutes. The outcome is marshalled back via _te_update_signal.
        `manual` means always report the result."""
        channel = "stable"
        # One run at a time, like _kokoro_setup_running. The startup auto
        # update, the install offer and the manual button could otherwise
        # overlap two Maven builds in the same target dir. A manual click
        # arriving mid-run is remembered, the in-flight run reports for it
        # when it completes.
        if getattr(self, "_te_update_running", False):
            if manual:
                self._te_update_pending_manual = True
            return
        self._te_update_running = True
        def _work():
            try:
                changed, msg = _te_update_engine(channel, manual)
            except Exception as exc:  # noqa: BLE001 - must never crash startup
                changed, msg = False, _("Triggevent update error: {error!r}").format(error=exc)
            self._te_update_signal.emit(changed, msg, manual)
        try:
            threading.Thread(target=_work, daemon=True).start()
        except Exception:  # noqa: BLE001 - a failed start must not strand the button
            self._te_update_running = False
            btn = getattr(self, "_te_update_btn", None)
            if btn is not None:
                btn.setEnabled(True)
                btn.setText(_("Update Triggevent Engine"))

    def _on_te_update_done(self, changed: bool, msg: str, manual: bool) -> None:
        self._te_update_running = False
        # A manual click that arrived while this run was in flight still
        # gets the result dialog, its own call was swallowed by the guard.
        manual = manual or getattr(self, "_te_update_pending_manual", False)
        self._te_update_pending_manual = False
        print(f"[triggevent] {msg}", file=sys.stderr)
        btn = getattr(self, "_te_update_btn", None)
        if btn is not None:
            btn.setEnabled(True)
            btn.setText(_("Update Triggevent Engine"))
        if changed:
            ac.QMessageBox.information(self, _("Triggevent updated"), msg)
        elif manual:                                  # manual click always gets feedback
            ac.QMessageBox.information(self, _("Triggevent"), msg)

    def _update_triggers_data(self) -> None:
        self._download_repo_triggers(self._update_trig_btn, _("Update Triggers"))

    def _download_repo_triggers(self, btn, label: str) -> None:
        # Update and Restore both fetch the bundled triggers.json from
        # GitHub and reload it. _load_triggers re-applies local overrides,
        # so custom and edited triggers are never removed. In-flight state
        # is per button. The two buttons used to share one handle, so a
        # second click during a fetch stranded the first button on
        # "Downloading..." for the session.
        if btn in self._trig_dl_in_flight:
            return
        self._trig_dl_in_flight[btn] = label
        btn.setEnabled(False)
        btn.setText(_("Downloading..."))

        def _fetch() -> None:
            try:
                req = urllib.request.Request(
                    f"https://raw.githubusercontent.com/{updater.REPO}/{_REPO_TRIGGERS_BRANCH}/triggers.json",
                    headers={"User-Agent": "NyaaTriggers"},
                )
                with urllib.request.urlopen(req, timeout=15) as resp:
                    raw = resp.read(_REPO_JSON_MAX_BYTES + 1)
                if len(raw) > _REPO_JSON_MAX_BYTES:
                    raise ValueError("triggers.json response too large")
                data = json.loads(raw)
                if not isinstance(data, list):
                    raise ValueError("Unexpected format - not a list")
                # Distinct untracked names, see _REPO_TRIGGERS_FILE. On a
                # source checkout _DATA_DIR is the git tree, where
                # triggers.json is tracked. Writing it clobbered the
                # checkout and blocked pulls.
                _atomic_write_json(ac._REPO_TRIGGERS_FILE, data, indent=2)
                # Retirements ride along, so a trigger withdrawn upstream
                # is purged here without waiting for an app update. Best
                # effort. A master without the file yet must not fail the
                # trigger update.
                try:
                    rreq = urllib.request.Request(
                        f"https://raw.githubusercontent.com/{updater.REPO}/{_REPO_TRIGGERS_BRANCH}/retired.json",
                        headers={"User-Agent": "NyaaTriggers"},
                    )
                    with urllib.request.urlopen(rreq, timeout=15) as rresp:
                        rraw = rresp.read(_REPO_JSON_MAX_BYTES + 1)
                    if len(rraw) > _REPO_JSON_MAX_BYTES:
                        raise ValueError("retired.json response too large")
                    rdata = json.loads(rraw)
                    if isinstance(rdata, (dict, list)):
                        _atomic_write_json(ac._REPO_RETIRED_FILE, rdata, indent=2)
                except Exception:  # noqa: BLE001
                    pass
                # Stamp last. A crash mid-download leaves no stamp, or a
                # stale one, and the loader then ignores the override.
                _atomic_write_json(ac._REPO_TRIGGERS_VERSION, _VERSION)
                self._trig_update_signal.emit(btn, f"ok:{len(data)}")
            except Exception as exc:
                self._trig_update_signal.emit(btn, f"err:{exc}")

        try:
            threading.Thread(target=_fetch, daemon=True).start()
        except Exception:  # noqa: BLE001 - a failed start must not strand the button
            self._trig_dl_in_flight.pop(btn, None)
            btn.setEnabled(True)
            btn.setText(label)

    def _on_trig_update_result(self, btn, msg: str) -> None:
        label = self._trig_dl_in_flight.pop(btn, None)
        if label is None:
            return   # stale result for an already-completed fetch
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
