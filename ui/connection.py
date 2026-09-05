"""Plugin connection handling. WebSocket connect and reconnect, plugin
link status and the IINACT log dir. Mixin for MainWindow, all state rides
on self.
"""

from pathlib import Path
import json
import os
import re

from PyQt6.QtCore import QTimer, QUrl
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import QApplication

from locale_util import _
from plugin_link import DEFAULT_PORT, parse_port

import app_common as ac


class ConnectionMixin:
    def _on_auto_connect_changed(self, state: int) -> None:
        self._settings["auto_connect"] = bool(state)
        self._save_settings()

    def _on_record_pulls_changed(self, state: int) -> None:
        self._settings["triggevent_record_pulls"] = bool(state)
        self._save_settings()
        self._pull_capture.set_recording(bool(state))

    def _push_plugin_tick(self) -> None:
        """Fight-clock push. Sent only while the timeline clock runs. Every
        path that stops the clock, zone change, wipe, feed loss, local off,
        sends a clear instead, so the plugin never interpolates a dead pull."""
        if self._timeline.is_active():
            self._plugin_link.send_tick(self._timeline.current_time())

    def _on_plugin_link_status(self, connected: bool, msg: str) -> None:
        """Plugin link status. Drives the Settings indicator and, on reconnect,
        re-pushes the schedule. The plugin drops all state when the app goes
        away, so without this a link hiccup mid-pull stays blank until the
        next zone change."""
        self._update_plugin_link_status_label(connected, msg)
        if connected:
            self._push_timeline_to_plugin()

    def _on_plugin_port_changed(self) -> None:
        """Port field under In-Game Overlay. Saved and re-dialed on the spot,
        mirroring the Apply button on the plugin's own port field."""
        edit = getattr(self, "_plugin_port_edit", None)
        if edit is None:
            return
        raw = (edit.text() or "").strip()
        port = parse_port(raw) if raw else DEFAULT_PORT   # an empty field means the default
        if port is None:
            # Not a usable port. Revert to the saved value and explain via
            # tooltip, the same shape as the Telesto URL field.
            saved = parse_port(self._settings.get("plugin_port"))
            edit.setText(str(saved if saved is not None else DEFAULT_PORT))
            edit.setToolTip(_("Invalid port - must be 1024 to 65535"))
            return
        edit.setToolTip("")
        saved = parse_port(self._settings.get("plugin_port"))
        if port == (saved if saved is not None else DEFAULT_PORT):
            return   # unchanged text, a bare focus-out must not re-dial
        if str(port) != raw:
            edit.setText(str(port))
        self._settings["plugin_port"] = port
        self._save_settings()
        self._plugin_link.set_port(port)

    @staticmethod
    def _find_iinact_log_dir() -> "Path | None":
        """Locate the folder IINACT writes its Network log day files into.
        Reads LogFilePath from the IINACT plugin config and resolves it.
        On Linux the config sits under .xlcore and the C drive path maps
        into the wine prefix. On Windows the config sits where XIVLauncher
        puts it and the path resolves directly. Falls back to the default
        Documents\\IINACT spot, for a config that has not been written yet."""
        if os.name == "nt":
            cfg = (Path(os.environ.get("APPDATA", "")) / "XIVLauncher"
                   / "pluginConfigs" / "IINACT.json")
        else:
            cfg = Path.home() / ".xlcore" / "pluginConfigs" / "IINACT.json"
        try:
            conf = json.loads(cfg.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            conf = {}
        raw = conf.get("LogFilePath") if isinstance(conf, dict) else None
        if isinstance(raw, str) and raw.strip():
            if os.name == "nt":
                direct = Path(raw.strip())
                if direct.is_dir():
                    return direct
            m = re.match(r"^c:\\?(.+)$", raw.strip(), re.IGNORECASE)
            if m:
                rest = m.group(1).replace("\\", "/").strip("/")
                # The wine prefix only ever has lowercase users, so lower
                # the first leg to match it. Later legs keep their case.
                first, _, tail = rest.partition("/")
                rest = first.lower() + ("/" + tail if tail else "")
                # A bare C drive root must not map onto all of drive_c.
                if rest:
                    mapped = (Path.home() / ".xlcore" / "wineprefix" / "drive_c"
                              / rest)
                    if mapped.is_dir():
                        return mapped
        if os.name == "nt":
            default = Path.home() / "Documents" / "IINACT"
            if default.is_dir():
                return default
        else:
            for cand in sorted((Path.home() / ".xlcore" / "wineprefix"
                                / "drive_c" / "users").glob(
                                    "*/Documents/IINACT")):
                if cand.is_dir():
                    return cand
        return None

    def _open_iinact_logs(self) -> None:
        """Open IINACT's raw network log folder in the desktop file manager.
        The location comes from the IINACT plugin config, mapped into the
        wine prefix on Linux."""
        path = self._find_iinact_log_dir()
        if path is None:
            ac.QMessageBox.information(
                self, _("IINACT Logs"),
                _("Could not find an IINACT log folder. Is IINACT installed?"))
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def _on_ws_party_jobs(self, jobs: dict) -> None:
        """PartyChanged roster jobs, a dict of actor int to job int in decimal.
        The most reliable feed, update unconditionally. Cheap, tiny dict.
        The dps meter shares it, its own 03 burst may never have arrived."""
        # getattr, duck-typed test windows may lack the meter or the method.
        note_job = getattr(getattr(self, "_dps_meter", None), "note_job", None)
        for k, v in jobs.items():
            if not v:
                continue
            try:
                aid, job = int(k), int(v)
            except (TypeError, ValueError):
                continue   # one malformed entry must not kill the rest of the roster
            self._note_actor_job(aid, job)
            if note_job is not None:
                note_job(aid, job)
        self._rearm_umad_chain_flush()

    def _on_ws_primary_player(self, _char_id: int, name: str) -> None:
        """ChangePrimaryPlayer from the WS feed, replayed from cache on
        subscribe, live on login or character switch. An empty name, server
        started before the game knew the player, must not clobber a saved
        one, so only a real, different name is applied. The dps meter takes
        the id too, its 02 line replay may never come."""
        name = name.strip()
        if name and name != self._me_name:
            self._set_me_name(name)
        set_me = getattr(getattr(self, "_dps_meter", None), "set_me", None)
        if set_me is not None:
            set_me(_char_id)

    def _on_ws_combatants_jobs(self, payload: dict) -> None:
        """getCombatants snapshots double as a job feed. Entries carry decimal
        Job ints and reflect live memory, so a mid-instance app restart, no 03
        burst, no party change, still resolves roles. Players only."""
        if not self._umad_chain_enabled:
            return
        for c in (payload or {}).get("list") or []:
            if not isinstance(c, dict):
                continue
            try:
                cid, job = int(c.get("id") or 0), int(c.get("job") or 0)
            except (TypeError, ValueError):
                continue   # a malformed sidecar entry must not kill the slot
            if job and cid >= 0x10000000:
                self._note_actor_job(cid, job)
        self._rearm_umad_chain_flush()

    def _quit_for_windows_handoff(self) -> None:
        # The freshly-staged exe is waiting for this process to exit so
        # Windows releases the locks on our exe and _internal. It then
        # swaps the new files in and relaunches us, so we must not relaunch
        # here. Stop the Triggevent sidecar synchronously. Its JVM runtime
        # and jar live inside _internal and must be fully dead before we
        # quit, or the swap fights it. Flush the "reopening" message first,
        # since the sidecar kill can block. Each step runs isolated through
        # _teardown_step, so one failed stop cannot skip the rest and still
        # prints to stderr, the handoff is the worst place to lose a
        # diagnostic.
        app = QApplication.instance()
        if app is not None:
            app.processEvents()
        step = self._teardown_step
        # Stop the timers before the 300 ms event-loop window below, so
        # none fires mid-teardown. Same set closeEvent stops.
        self._stop_background_timers()
        step("clear status timers", lambda: self._clear_status_timers())   # no reapply warning may fire mid-teardown
        step("clear seq runners", lambda: self._clear_seq_runners())
        # This path bypasses closeEvent, so the debounced settings save has
        # to be flushed here or the last slider drag is lost.
        step("settings save flush", lambda: self._flush_pending_settings_save())
        # Finalize an in-progress meter encounter like closeEvent does,
        # or a handoff mid-fight loses the active pull.
        step("meter encounter finalize", lambda: self._finalize_live_encounter())
        step("ws disconnect", lambda: self._ws.disconnect_from())
        step("cactbot reader stop", lambda: self._stop_cactbot_reader())
        step("triggevent stop", lambda: self._stop_sidecar("_triggevent", wait=True))
        # The Triggernometry sidecar is also a child process launched
        # out of _internal on a frozen build. It survives app.quit, and
        # a live child holding _internal files makes the staged swap
        # fail and roll back. wait=True, like the Triggevent stop
        # above, so the child is confirmed dead before we quit. The
        # off-thread reaper would be killed by app.quit 300 ms later,
        # before it could escalate.
        step("triggernometry stop", lambda: self._stop_sidecar("_triggernometry", wait=True))
        step("telesto stop", lambda: self._stop_sidecar("_telesto_client"))
        step("plugin link stop", lambda: self._stop_sidecar("_plugin_link"))
        # Short delay so the final repaint settles. The staged copy waits on our PID.
        QTimer.singleShot(300, app.quit if app is not None else (lambda: None))

    def _toggle_connection(self) -> None:
        if not self._connected:
            url = self._url_edit.text().strip()
            self._settings["ws_url"] = url
            self._save_settings()
            self._ws.connect_to(url)
        else:
            self._ws.disconnect_from()
