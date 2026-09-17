"""Live meter, encounter recording and FFLogs controls for MainWindow."""

from datetime import datetime
from pathlib import Path
import math
import re
import threading
import time

from PyQt6.QtCore import Qt, QUrl
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import QHBoxLayout, QPushButton, QTableWidgetItem, QLineEdit, QLabel

from nyaatriggers.locale_util import _
from nyaatriggers.dps_meter import DpsMeter
from nyaatriggers import dps_store
from nyaatriggers.fflogs import FflogsClient

from nyaatriggers import app_common as ac


class DpsTabMixin:
    def _init_dps(self) -> None:
        # Feed the meter independently of trigger mode.
        self._dps_meter = DpsMeter()
        # Use the same supported timeout value in the meter and selector.
        try:
            self._dps_idle_timeout = int(self._settings.get("dps_idle_timeout", 120))
        except (TypeError, ValueError):
            self._dps_idle_timeout = 120
        if self._dps_idle_timeout not in (15, 30, 60, 120, 180, 240, 300, 600):
            self._dps_idle_timeout = 120
        self._dps_meter.set_idle_timeout(self._dps_idle_timeout)
        self._dps_meter.on_encounter_end = self._on_meter_encounter_end
        self._fflogs_last_title = ""           # last finalized encounter, for the FFLogs refresh button
        # Track the last finalization so a wipe can restore the final overlay frame.
        self._dps_last_end = 0.0
        self._dps_history: list[dict] = []
        self._dps_selected_idx: "int | None" = None
        self._dps_live_active: bool = False
        self._dps_overlay_live = False
        self._dps_write_threads: list[threading.Thread] = []

    def _dps_dir(self) -> Path:
        return ac._DATA_DIR / "dps_logs"

    def _on_dps_record_toggled(self, on: bool) -> None:
        self._settings["dps_enabled"] = on
        self._save_settings()

    def _on_dps_idle_changed(self, _idx: int) -> None:
        secs = self._dps_idle_combo.currentData()
        self._settings["dps_idle_timeout"] = secs
        self._save_settings()
        self._dps_meter.set_idle_timeout(secs)

    def _open_dps_folder(self) -> None:
        self._dps_dir().mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._dps_dir())))

    def _dps_tick(self) -> None:
        self._update_live_dps()
        prog_tab = getattr(self, "_prog_tab", None)
        if prog_tab is not None:
            prog_tab.tick()
        if self._plugin_link.is_connected():
            snap = self._dps_meter.snapshot()
            if snap["isActive"]:
                enc = snap["Encounter"]
                self._plugin_link.send_dps(
                    {"t": enc["title"], "d": enc["duration"],
                     "dps": round(enc["encdps"], 1)},
                    self._dps_meter.overlay_rows(), show=True)
                self._dps_overlay_live = True
            elif self._dps_overlay_live:
                self._plugin_link.send_dps(None, [], show=False)
                self._dps_overlay_live = False

    @staticmethod
    def _fmt_dps_num(v, decimals: int = 0) -> str:
        try:
            return f"{float(str(v).replace(',', '')):,.{decimals}f}"
        except (ValueError, TypeError):
            return str(v or "")

    @staticmethod
    def _fmt_dps_pct(v) -> str:
        try:
            return f"{float(str(v).replace(',', '')):.1f}"
        except (ValueError, TypeError):
            return ""

    @staticmethod
    def _fmt_maxhit(value) -> str:
        """Format numeric skill placeholders for display."""
        s = str(value or "")
        if "-" not in s:
            return s
        name, _, amount = s.rpartition("-")
        try:
            return f"{name} {int(amount):,}"
        except ValueError:
            return s

    def _fill_dps_row(self, table, r: int, name: str, job: str,
                      cells: "list[str]", right_from: int = 2) -> None:
        table.setItem(r, 0, QTableWidgetItem(name))
        table.setItem(r, 1, QTableWidgetItem(job))
        for off, val in enumerate(cells):
            cell = QTableWidgetItem(val)
            if off + 2 >= right_from:
                cell.setTextAlignment(
                    Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            table.setItem(r, off + 2, cell)

    def _populate_dps_table(self, table, snap) -> None:
        """Display a live or recorded meter snapshot."""
        rows = sorted(snap["Combatant"].values(),
                      key=lambda c: c.get("encdps", 0.0), reverse=True)
        table.setRowCount(len(rows))
        for r, c in enumerate(rows):
            self._fill_dps_row(table, r, str(c.get("name", "")),
                               str(c.get("Job", "")),
                               [self._fmt_dps_num(c.get("encdps", 0.0)),
                                self._fmt_dps_pct(c.get("damage%", 0.0)),
                                self._fmt_dps_num(c.get("enchps", 0.0)),
                                self._fmt_dps_pct(c.get("crithit%", 0.0)),
                                self._fmt_dps_pct(c.get("DirectHitPct", 0.0)),
                                self._fmt_maxhit(c.get("maxhit", "")),
                                str(c.get("deaths", 0))])

    def _refresh_dps_history_list(self) -> None:
        lst = getattr(self, "_dps_history_list", None)
        if lst is None:
            return
        lst.blockSignals(True)
        lst.clear()
        for entry in self._dps_history:
            enc = (entry["snapshot"].get("Encounter") or {})
            lst.addItem(_("{title} ({dur})  party {dps}  {when}").format(
                title=enc.get("title", ""),
                dur=enc.get("duration", ""),
                dps=self._fmt_dps_num(enc.get("encdps", 0.0)),
                when=entry["when"]))
        lst.blockSignals(False)

    def _on_dps_history_click(self, item) -> None:
        row = self._dps_history_list.row(item)
        if 0 <= row < len(self._dps_history):
            self._dps_selected_idx = row
            self._update_live_dps()

    def _on_dps_back_to_live(self) -> None:
        self._dps_selected_idx = None
        self._update_live_dps()

    def _on_meter_encounter_end(self, snapshot: dict) -> None:
        """Finish encounter display and recording, preserving final values in the table and
        starting any FFLogs lookup.
        """
        self._plugin_link.send_dps(None, [], show=False)
        self._dps_overlay_live = False
        self._dps_last_end = time.monotonic()
        enc = snapshot.get("Encounter") or {}
        title = enc.get("title") or ""
        if title:
            self._fflogs_last_title = title
        if self._settings.get("dps_enabled", False):
            self._write_dps_snapshot(snapshot)
        self._maybe_fetch_fflogs(title)
        # Clear deduplication state between pulls.
        self._clear_callout_dedup()
        # Reset cooldowns because status effect IDs remain the same across pulls.
        for t in self._triggers:
            t._last_fired.clear()
        # Keep bounded session history with the newest pull first.
        self._dps_history.insert(0, {"snapshot": snapshot,
                                     "when": datetime.now().strftime("%H:%M:%S")})
        del self._dps_history[80:]
        self._dps_selected_idx = None
        self._refresh_dps_history_list()

    def _write_dps_snapshot(self, snapshot: dict) -> None:
        """Append a finalized encounter to the rolling DPS log. dps_store manages rotation
        and retention.
        """
        enc = snapshot.get("Encounter") or {}
        title = (enc.get("title") or "").strip() or "Unknown"
        try:
            # Use the recorded start time because this writer runs at encounter end.
            wall = enc.get("wall_start")
            if isinstance(wall, (int, float)) and math.isfinite(wall):
                stamp = datetime.fromtimestamp(wall).strftime("%Y-%m-%d_%H-%M-%S")
            else:
                stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            combatants = []
            for c in (snapshot.get("Combatant") or {}).values():
                if not isinstance(c, dict):
                    continue
                combatants.append({
                    "name": c.get("name", ""),
                    "job": c.get("Job", ""),
                    "dps": round(float(c.get("dps", 0.0)), 1),
                    "hps": round(float(c.get("enchps", 0.0)), 1),
                    "damage_pct": round(float(c.get("damage%", 0.0)), 1),
                    "damage": int(c.get("damage", 0)),
                    "encdps": round(float(c.get("encdps", 0.0)), 1),
                    "crit_pct": round(float(c.get("crithit%", 0.0)), 1),
                    "dh_pct": round(float(c.get("DirectHitPct", 0.0)), 1),
                    "cdh_pct": round(float(c.get("CritDirectHitPct", 0.0)), 1),
                    "maxhit": c.get("maxhit", ""),
                    "deaths": int(c.get("deaths", 0)),
                })
            combatants.sort(key=lambda x: x["encdps"], reverse=True)
            data = {
                "title": title,
                "zone": enc.get("CurrentZoneName", ""),
                "duration": enc.get("duration", ""),
                "encdps": round(float(enc.get("encdps", 0.0)), 1),
                "started": stamp,
                "updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "combatants": combatants,
            }
        except (OSError, ValueError, TypeError) as exc:
            ac.log_drop("dps-snapshot", f"write failed: {exc!r}")
            return
        # Write logs outside the GUI thread and track workers so quitting can wait for
        # pending saves.
        log_dir = str(self._dps_dir())

        def work() -> None:
            try:
                dps_store.write_pull(log_dir, data)
            except (OSError, ValueError, TypeError) as exc:
                ac.log_drop("dps-snapshot", f"write failed: {exc!r}")

        self._dps_write_threads = [t for t in self._dps_write_threads if t.is_alive()]
        worker = threading.Thread(target=work, daemon=True)
        worker.start()
        self._dps_write_threads.append(worker)

    def _fflogs_configured(self) -> bool:
        return bool(self._settings.get("fflogs_client_id")
                    and self._settings.get("fflogs_client_secret")
                    and self._settings.get("fflogs_server"))

    def _maybe_fetch_fflogs(self, title: str) -> None:
        """Fetch configured FFLogs comparisons in the background and deliver through
        _fflogs_signal.
        """
        lbl = getattr(self, "_fflogs_lbl", None)
        if not title or lbl is None or not self._fflogs_configured():
            return
        name = self._settings.get("fflogs_name")
        if not isinstance(name, str):
            name = ""
        char = (name or self._me_name or "").strip()
        if not char:
            lbl.setText(_("FFLogs: no data"))
            return
        cid = self._settings.get("fflogs_client_id")
        secret = self._settings.get("fflogs_client_secret")
        server = self._settings.get("fflogs_server")
        region = self._settings.get("fflogs_region", "NA")
        lbl.setText(_("FFLogs: fetching…"))
        # Reuse the client for each credential pair to preserve its token cache.
        creds = (cid, secret)
        if getattr(self, "_fflogs_client_creds", None) != creds:
            self._fflogs_client = FflogsClient(cid, secret)
            self._fflogs_client_creds = creds
        client = self._fflogs_client

        def work() -> None:
            self._fflogs_signal.emit(
                client.fetch_best(char, server, region, title))

        threading.Thread(target=work, daemon=True).start()

    def _on_fflogs_refresh(self) -> None:
        title = self._fflogs_last_title
        if not title:
            title = (self._dps_meter.snapshot().get("Encounter") or {}).get("title", "")
        self._maybe_fetch_fflogs(title)

    def _on_fflogs_result(self, res) -> None:
        lbl = getattr(self, "_fflogs_lbl", None)
        if lbl is None:
            return
        if not isinstance(res, dict):
            lbl.setText(_("FFLogs: no data"))
            return
        amount = res.get("amount")
        percent = res.get("percent")
        if not isinstance(amount, (int, float)) or not amount:
            lbl.setText(_("FFLogs: no data"))
            return
        best = f"{amount / 1000:.1f}k" if amount >= 1000 else f"{amount:,.0f}"
        text = _("FFLogs best: {best} rDPS").format(best=best)
        if isinstance(percent, (int, float)) and percent:
            text += f" ({percent:.0f}%)"
        lbl.setText(text)

    def _build_fflogs_settings(self, layout) -> None:
        self._settings_header(layout, _("FFLogs"))
        row = QHBoxLayout()
        iinact_btn = QPushButton(_("IINACT Logs"))
        iinact_btn.setMaximumWidth(130)
        iinact_btn.clicked.connect(self._open_iinact_logs)
        row.addWidget(iinact_btn)
        iinact_note = QLabel(
            _("Folder with the raw log files an FFLogs uploader takes"))
        iinact_note.setWordWrap(True)
        iinact_note.setStyleSheet("color:#8f8f9a;")
        row.addWidget(iinact_note)
        row.addStretch(1)
        layout.addLayout(row)

        # Show comparisons once credentials and server are set. Reuse the connection
        # character name by default.
        cmp_note = QLabel(
            _("Show your FFLogs best next to the meter after a fight. Needs a "
              "personal API client from your fflogs.com profile page."))
        cmp_note.setWordWrap(True)
        cmp_note.setStyleSheet("color:#8f8f9a;")
        layout.addWidget(cmp_note)

        id_row = QHBoxLayout()
        id_row.addWidget(QLabel(_("Client ID:")))
        cid = self._settings.get("fflogs_client_id")
        self._fflogs_id_edit = QLineEdit(cid if isinstance(cid, str) else "")
        self._fflogs_id_edit.editingFinished.connect(self._on_fflogs_credentials_changed)
        id_row.addWidget(self._fflogs_id_edit, stretch=1)
        id_row.addWidget(QLabel(_("Client secret:")))
        secret = self._settings.get("fflogs_client_secret")
        self._fflogs_secret_edit = QLineEdit(secret if isinstance(secret, str) else "")
        self._fflogs_secret_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self._fflogs_secret_edit.editingFinished.connect(self._on_fflogs_credentials_changed)
        id_row.addWidget(self._fflogs_secret_edit, stretch=1)
        layout.addLayout(id_row)

        srv_row = QHBoxLayout()
        srv_row.addWidget(QLabel(_("Server:")))
        server = self._settings.get("fflogs_server")
        self._fflogs_server_edit = QLineEdit(server if isinstance(server, str) else "")
        self._fflogs_server_edit.setPlaceholderText(_("lowercase slug, e.g. tonberry"))
        self._fflogs_server_edit.setMaximumWidth(200)
        self._fflogs_server_edit.editingFinished.connect(self._on_fflogs_credentials_changed)
        srv_row.addWidget(self._fflogs_server_edit)
        srv_row.addWidget(QLabel(_("Region:")))
        region = self._settings.get("fflogs_region")
        self._fflogs_region_edit = QLineEdit(region if isinstance(region, str) else "")
        self._fflogs_region_edit.setPlaceholderText("NA")
        self._fflogs_region_edit.setMaximumWidth(60)
        self._fflogs_region_edit.editingFinished.connect(self._on_fflogs_credentials_changed)
        srv_row.addWidget(self._fflogs_region_edit)
        srv_row.addStretch(1)
        layout.addLayout(srv_row)

    def _on_fflogs_credentials_changed(self) -> None:
        """Save FFLogs fields and update comparison visibility."""
        region_raw = self._fflogs_region_edit.text().strip()
        region = region_raw.upper()
        if region != region_raw:
            self._fflogs_region_edit.setText(region)
        vals = {"fflogs_client_id": self._fflogs_id_edit.text().strip(),
                "fflogs_client_secret": self._fflogs_secret_edit.text().strip(),
                "fflogs_server": self._fflogs_server_edit.text().strip(),
                "fflogs_region": region}
        changed = False
        for key, val in vals.items():
            if val:
                if self._settings.get(key) != val:
                    self._settings[key] = val
                    changed = True
            elif key in self._settings:
                del self._settings[key]
                changed = True
        if not changed:
            return
        self._save_settings()
        self._update_fflogs_visibility()

    def _finalize_live_encounter(self) -> None:
        """Finalize the active encounter before quitting so recording can save it."""
        finish_activity = getattr(self, "_finish_activity", None)
        if finish_activity is not None:
            finish_activity()
        if self._dps_meter.current is not None:
            self._dps_meter.finalize("program-closed")
        # Wait for writers under one deadline so quitting preserves the final pull
        # without waiting indefinitely.
        deadline = time.monotonic() + 5.0
        for worker in self._dps_write_threads:
            worker.join(timeout=max(0.0, deadline - time.monotonic()))
        self._dps_write_threads = [t for t in self._dps_write_threads if t.is_alive()]
