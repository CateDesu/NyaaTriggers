"""Connection controls and IINACT log folder handling for MainWindow."""

from pathlib import Path
import json
import os
import re

from PyQt6.QtCore import QTimer, QUrl
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import QApplication

from nyaatriggers.locale_util import _
from nyaatriggers.plugin_link import DEFAULT_PORT, parse_port

from nyaatriggers import app_common as ac


class ConnectionMixin:
    def _on_auto_connect_changed(self, state: int) -> None:
        self._settings["auto_connect"] = bool(state)
        self._save_settings()

    def _on_record_pulls_changed(self, state: int) -> None:
        self._settings["triggevent_record_pulls"] = bool(state)
        self._save_settings()
        self._pull_capture.set_recording(bool(state))

    def _push_plugin_tick(self) -> None:
        """Push the clock only while it runs. Stop paths clear it so the plugin cannot
        continue a dead pull.
        """
        if self._timeline.is_active():
            self._plugin_link.send_tick(self._timeline.current_time())

    def _on_plugin_link_status(self, connected: bool, msg: str) -> None:
        """Update connection status and restore the schedule after reconnect."""
        if (connected, msg) != self._plugin_link.last_status():
            return
        self._update_plugin_link_status_label(connected, msg)
        if connected:
            self._push_timeline_to_plugin()

    def _on_plugin_port_changed(self) -> None:
        """Save and apply changes to the overlay port."""
        edit = getattr(self, "_plugin_port_edit", None)
        if edit is None:
            return
        raw = (edit.text() or "").strip()
        port = parse_port(raw) if raw else DEFAULT_PORT   # an empty field means the default
        if port is None:
            saved = parse_port(self._settings.get("plugin_port"))
            edit.setText(str(saved if saved is not None else DEFAULT_PORT))
            edit.setToolTip(_("Invalid port - must be 1024 to 65535"))
            return
        edit.setToolTip("")
        saved = parse_port(self._settings.get("plugin_port"))
        if port == (saved if saved is not None else DEFAULT_PORT):
            return
        if str(port) != raw:
            edit.setText(str(port))
        self._settings["plugin_port"] = port
        self._save_settings()
        self._plugin_link.set_port(port)

    @staticmethod
    def _find_iinact_log_dir() -> "Path | None":
        """Resolve IINACT's log directory from its configuration, mapping Windows paths
        into Wine on Linux. Fall back to the default Documents location.
        """
        if os.name == "nt":
            cfg = (Path(os.environ.get("APPDATA", "")) / "XIVLauncher"
                   / "pluginConfigs" / "IINACT.json")
        else:
            cfg = Path.home() / ".xlcore" / "pluginConfigs" / "IINACT.json"
        try:
            conf = json.loads(cfg.read_text(encoding="utf-8"))
        except (OSError, ValueError, RecursionError):
            conf = {}
        raw = conf.get("LogFilePath") if isinstance(conf, dict) else None
        if isinstance(raw, str) and raw.strip():
            if os.name == "nt":
                direct = Path(raw.strip())
                if direct.is_dir():
                    return direct
            m = re.match(r"^([a-z]):[\\/](.+)$", raw.strip(), re.IGNORECASE)
            if m and os.name != "nt":
                drive = m.group(1).lower()
                rest = m.group(2).replace("\\", "/").strip("/")
                prefix = Path.home() / ".xlcore" / "wineprefix"
                if rest:
                    mapped = prefix / "dosdevices" / f"{drive}:" / rest
                    if mapped.is_dir():
                        return mapped
                # Match the lowercase Wine user directory while preserving later path
                # components.
                first, _, tail = rest.partition("/")
                rest = first.lower() + ("/" + tail if tail else "")
                # Reject a bare drive root as a log directory.
                if drive == "c" and rest:
                    mapped = prefix / "drive_c" / rest
                    if mapped.is_dir():
                        return mapped
        default = Path.home() / "Documents" / "IINACT"
        if default.is_dir():
            return default
        if os.name != "nt":
            for cand in sorted((Path.home() / ".xlcore" / "wineprefix"
                                / "drive_c" / "users").glob(
                                    "*/Documents/IINACT")):
                if cand.is_dir():
                    return cand
        return None

    def _open_iinact_logs(self) -> None:
        """Open the resolved IINACT log folder."""
        path = self._find_iinact_log_dir()
        if path is None:
            ac.QMessageBox.information(
                self, _("IINACT Logs"),
                _("Could not find an IINACT log folder. Is IINACT installed?"))
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def _on_ws_party_jobs(self, jobs: dict) -> None:
        """Use the party roster as a shared job feed for automarkers and the meter."""
        note_job = getattr(getattr(self, "_dps_meter", None), "note_job", None)
        for k, v in jobs.items():
            if not v:
                continue
            try:
                aid, job = int(k), int(v)
            except (TypeError, ValueError):
                continue
            self._note_actor_job(aid, job)
            if note_job is not None:
                note_job(aid, job)
        self._rearm_umad_chain_flush()

    def _on_ws_primary_player(self, _char_id: int, name: str) -> None:
        """Apply a known primary player from cached or live metadata without overwriting
        the name with an empty value. Pass the ID to the meter too.
        """
        self._me_id = f"{_char_id:08X}" if 0x10000000 <= _char_id < 0x11000000 else ""
        name = name.strip()
        if name and name != self._me_name:
            self._set_me_name(name)
        set_me = getattr(getattr(self, "_dps_meter", None), "set_me", None)
        if set_me is not None:
            set_me(_char_id)

    def _on_ws_combatants_jobs(self, payload: dict) -> None:
        """Use live player combatant snapshots to backfill jobs after a midfight restart.
        """
        if not self._umad_chain_enabled:
            return
        for c in (payload or {}).get("list") or []:
            if not isinstance(c, dict):
                continue
            try:
                cid, job = int(c.get("id") or 0), int(c.get("job") or 0)
            except (TypeError, ValueError):
                continue
            if job and cid >= 0x10000000:
                self._note_actor_job(cid, job)
        self._rearm_umad_chain_flush()

    def _quit_for_windows_handoff(self) -> None:
        # Stop sidecars synchronously before the Windows handoff so they release runtime
        # files. The staged updater handles relaunch. Flush status first and isolate
        # teardown steps so one failure cannot skip cleanup.
        app = QApplication.instance()
        if app is not None:
            app.processEvents()
        step = self._teardown_step
        # Stop timers before allowing the final repaint.
        self._stop_background_timers()
        step("clear status timers", lambda: self._clear_status_timers())
        step("clear seq runners", lambda: self._clear_seq_runners())
        # Flush pending settings because this path bypasses closeEvent.
        step("settings save flush", lambda: self._flush_pending_settings_save())
        # Finish the active encounter before the handoff.
        step("meter encounter finalize", lambda: self._finalize_live_encounter())
        step("ws disconnect", lambda: self._ws.disconnect_from())
        # Finalize the pull capture as closeEvent would.
        step("pull capture finalize", lambda: self._pull_capture.close())
        step("cactbot reader stop", lambda: self._stop_cactbot_reader())
        step("triggevent stop", lambda: self._stop_sidecar("_triggevent", wait=True))
        # Wait for Triggernometry shutdown too so it releases the bundled runtime before
        # the swap.
        step("triggernometry stop", lambda: self._stop_sidecar("_triggernometry", wait=True))
        step("telesto stop", lambda: self._stop_sidecar("_telesto_client"))
        step("plugin link stop", lambda: self._stop_sidecar("_plugin_link"))
        # Allow the final repaint before exiting.
        QTimer.singleShot(300, app.quit if app is not None else (lambda: None))

    def _toggle_connection(self) -> None:
        if not self._connected:
            url = self._url_edit.text().strip()
            self._settings["ws_url"] = url
            self._save_settings()
            self._ws.connect_to(url)
        else:
            self._ws.disconnect_from()
