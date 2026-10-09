"""Replay recorded UWU jails through Python, the native engine and loopback Telesto."""

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("NYAA_REPLAY_TEST", "1")
CORE = Path(__file__).resolve().parent
sys.path.insert(0, str(CORE.parent))

from PyQt6.QtCore import QObject
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QCheckBox, QLineEdit, QPushButton, QSpinBox

from nyaatriggers import app_common as ac
from nyaatriggers import triggevent_bridge
from nyaatriggers.telesto_client import TelestoClient
from nyaatriggers.ui.automarkers_tab import AutomarkersTabMixin
from nyaatriggers.ui.engines import EnginesMixin
from nyaatriggers.ui.native_automarkers import NativeAutomarkersPanel
from nyaatriggers.ws_client import WSClient


PARTY = ("10669D22", "106AC23F", "10694638", "10026D97",
         "1072D7EE", "10679943", "1082C4CB", "105E0EF5")
EXPECTED_MARKS = ["/mk attack <2>", "/mk attack <1>", "/mk attack <3>"]


class InventoryHost(QObject, EnginesMixin, AutomarkersTabMixin):
    def __init__(self, bridge, endpoint):
        super().__init__()
        self._triggevent = bridge
        self._triggevent_mode = False
        self._settings = {"telesto_enabled": True, "telesto_uri": endpoint.url}
        self._telesto_client = TelestoClient(uri=endpoint.url, enabled=False)
        self._telesto_client.start()
        self._engine_inventory = []
        self._automark_active = {}
        self._automark_pending = []
        self._automark_owners = {}
        self._native_automarkers_panel = NativeAutomarkersPanel()
        self._native_automarkers_panel.changed.connect(self._on_native_automark_setting_changed)
        bridge.inventory.connect(self._on_triggevent_inventory)
        bridge.automark_inventory.connect(self._receive_native_automark_inventory)
        bridge.ready.connect(self._on_native_automarkers_ready)

    def _save_triggevent_inventory_cache(self):
        pass

    def _save_settings(self):
        pass

    def _record_engine_seen(self, _source):
        pass

    def _refresh_table(self):
        pass

    def _callout_edits_for(self, _source):
        return {}

    def _umad_chain_reset(self, **_kwargs):
        pass

    def _umad_gaze_reset(self, **_kwargs):
        pass

    def _update_automark_status_label(self):
        pass


def wait_for(predicate, message, seconds=30):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        QTest.qWait(20)
    raise AssertionError(message)


def recorded_inputs(source):
    recording = source / "testutils/testutils-samplelogs/src/main/resources/uwu.log"
    if not recording.is_file():
        raise SystemExit("FAIL automarker bridge verification requires the engine UWU recording")
    state, jails = [], []
    with recording.open(encoding="utf-8") as lines:
        for index, raw in enumerate(lines):
            fields = raw.strip().split("|")
            if index < 57 and (fields[0] in ("01", "02", "11")
                              or fields[0] == "03" and fields[2].upper() in PARTY):
                state.append(fields)
            if fields[0] == "21" and fields[4].upper() in ("2B6B", "2B6C"):
                jails.append(fields)
                if len(jails) == 3:
                    break
    if len(state) != 11 or [fields[6].upper() for fields in jails] != [PARTY[0], PARTY[1], PARTY[2]]:
        raise AssertionError("Recorded UWU state or jail actors changed")
    return state, jails


def feed(ws, lines, when):
    for fields in lines:
        current = list(fields)
        current[1] = when.isoformat()
        ws._on_message(json.dumps({"type": "LogLine", "rawLine": "|".join(current)}))


def feed_state(ws, lines, when):
    zone = next(fields for fields in lines if fields[0] == "01")
    player = next(fields for fields in lines if fields[0] == "02")
    ws._on_message(json.dumps({"type": "ChangeZone", "zoneID": int(zone[2], 16),
                               "zoneName": zone[3]}))
    ws._on_message(json.dumps({"type": "ChangePrimaryPlayer", "charID": int(player[2], 16),
                               "charName": player[3]}))
    feed(ws, lines, when)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine-source", type=Path, default=CORE / "event-trigger")
    args = parser.parse_args()
    if not triggevent_bridge.TriggeventBridge.is_available():
        raise SystemExit("FAIL automarker bridge verification requires the built native engine and Java")
    state, jails = recorded_inputs(args.engine_source)
    app = QApplication.instance() or QApplication([])
    from tests.test_automarker_pipeline import Endpoint

    bridge = triggevent_bridge.TriggeventBridge()
    ws = WSClient()
    ws.engine_message.connect(bridge.feed)
    diagnostics, events, progress = [], [], []
    bridge.recovery_progress.connect(lambda message, _gen: progress.append(message))
    record_engine = triggevent_bridge.record_engine

    def capture(message, **kwargs):
        result = record_engine(message, **kwargs)
        if result is not None:
            events.append(result)
        return result

    with tempfile.TemporaryDirectory(prefix="nyaa-automark-bridge-") as temp, Endpoint(PARTY) as endpoint:
        host = InventoryHost(bridge, endpoint)
        with patch.dict(os.environ, {"JAVA_TOOL_OPTIONS": f"-Duser.home={temp}",
                                     "NYAA_AUTOMARK": "0", "NYAA_TELESTO_URI": endpoint.url}), \
                patch.object(triggevent_bridge, "_log", side_effect=diagnostics.append), \
                patch.object(triggevent_bridge, "record_engine", side_effect=capture), \
                patch.object(ac, "_TRIGGEVENT_AUTOMARK_INVENTORY_CACHE", Path(temp) / "native.json"):
            try:
                for run in range(2):
                    events.clear()
                    host._engine_inventory = []
                    host._load_cached_native_automark_inventory()
                    panel = host._native_automarkers_panel
                    if len(panel._editors) != 46 or not panel.isEnabled():
                        raise AssertionError("Native controls were not editable before engine startup")
                    if run == 0:
                        panel.findChild(QCheckBox, "top.delta.enabled.value").click()
                        delay = panel.findChild(QLineEdit, "uwu.clear_delay_ms.value")
                        delay.setText("12000")
                        delay.editingFinished.emit()
                    offline_settings = deepcopy(host._settings.get("triggevent_automark_settings", {}))
                    if (bridge.is_active() or offline_settings.get("top.delta.enabled") is not True
                            or offline_settings.get("uwu.clear_delay_ms") != (12000 if run == 0 else 2000)):
                        raise AssertionError("Offline native edits did not save without starting the engine")
                    bridge.start()
                    wait_for(lambda: host._engine_inventory and any(
                        item["event"] == "engine_automark_config"
                        and item.get("enabled") is True and item.get("available") is True
                        for item in events), "Inventory did not restore enabled native automarkers")
                    wait_for(lambda: getattr(host, "_native_automark_inventory", None),
                             "Native automarker controls did not arrive")
                    inventory = host._native_automark_inventory
                    if not host._native_automarkers_panel.isEnabled() or host._native_automarkers_panel._engine_message:
                        raise AssertionError("Live native controls were not enabled")
                    if len(inventory["mechanics"]) != 13 or sum(
                            setting["type"] == "marker_map" for setting in inventory["settings"]) != 8:
                        raise AssertionError("Native encounter or marker map inventory is incomplete")
                    wait_for(lambda: "error" not in host._native_automark_inventory
                             and any(setting["id"] == "top.delta.enabled" and setting["value"] is True
                                     for setting in host._native_automark_inventory["settings"])
                             and any(setting["id"] == "uwu.clear_delay_ms"
                                     and setting["value"] == (12000 if run == 0 else 2000)
                                     for setting in host._native_automark_inventory["settings"]),
                             "Live native inventory did not retain the offline edits")
                    if (not panel.findChild(QCheckBox, "top.delta.enabled.value").isChecked()
                            or panel.findChild(QLineEdit, "uwu.clear_delay_ms.value").text()
                            != str(12000 if run == 0 else 2000)
                            or host._settings.get("triggevent_automark_settings", {}) != offline_settings):
                        raise AssertionError("Live controls or saved preferences replaced the offline edits")
                    if run == 1:
                        wait_for(lambda: "error" not in host._native_automark_inventory
                                 and any(setting["id"] == "top.ps.priority_override" and setting["value"] is True
                                         for setting in host._native_automark_inventory["settings"])
                                 and any(setting["id"] == "top.ps.priority" and setting["effective_order"] == override_order
                                         for setting in host._native_automark_inventory["settings"]),
                                 "Restart did not restore the retained TOP priority override")
                    now = datetime.now(timezone.utc)
                    feed_state(ws, state, now)
                    checkpoint = 70 + run
                    if not bridge.catch_up([], checkpoint):
                        raise AssertionError("Native state fence was rejected")
                    wait_for(lambda: any(item.get("checkpoint") == checkpoint for item in progress),
                             "Recorded party state was not processed")
                    party_updates = sum("New Telesto Party List" in line for line in diagnostics)
                    host._settings["telesto_enabled"] = False
                    host._apply_automark_state()
                    host._settings["telesto_enabled"] = True
                    host._apply_automark_state()
                    wait_for(lambda: sum("New Telesto Party List" in line for line in diagnostics) > party_updates,
                             "Native authoritative party order was not applied")
                    feed(ws, jails, datetime.now(timezone.utc))
                    expected_count = 3 * (run + 1)
                    wait_for(lambda: len(endpoint.commands) >= expected_count,
                             "Recorded jails did not reach Telesto")
                    current_marks = EXPECTED_MARKS if run == 0 else ["/mk attack <3>", "/mk attack <1>", "/mk attack <2>"]
                    expected_marks = EXPECTED_MARKS if run == 0 else EXPECTED_MARKS + current_marks
                    if endpoint.commands != expected_marks:
                        raise AssertionError(f"Incorrect jail actors or command order: {endpoint.commands}")
                    if run == 0:
                        events.clear()
                        host._settings["telesto_enabled"] = False
                        host._apply_automark_state()
                        wait_for(lambda: any(item["event"] == "engine_automark_config"
                                             and item.get("enabled") is False for item in events),
                                 "Disabled native marking was not acknowledged")
                        feed(ws, [["33", "", "0", "4000000F"]], datetime.now(timezone.utc))
                        feed(ws, jails, datetime.now(timezone.utc))
                        checkpoint = 80
                        if not bridge.catch_up([], checkpoint):
                            raise AssertionError("Native feed fence was rejected")
                        wait_for(lambda: any(item.get("checkpoint") == checkpoint for item in progress),
                                 "Disabled jail inputs were not processed")
                        if endpoint.commands != EXPECTED_MARKS:
                            raise AssertionError("Disabled markers reached Telesto")
                        override_order = next(setting["value"] for setting in inventory["settings"]
                                              if setting["id"] == "top.priority")
                        host._native_automarkers_panel.findChild(QCheckBox, "top.ps.priority_override.value").click()
                        wait_for(lambda: any(setting["id"] == "top.ps.priority_override" and setting["value"] is True
                                             for setting in host._native_automark_inventory["settings"]),
                                 "TOP priority override did not enable")
                        host._on_native_automark_setting_changed("top.priority", list(reversed(override_order)))
                        wait_for(lambda: any(setting["id"] == "top.priority" and setting["value"] == list(reversed(override_order))
                                             for setting in host._native_automark_inventory["settings"]),
                                 "Shared TOP priority did not change")
                        retained = next(setting["effective_order"] for setting in host._native_automark_inventory["settings"]
                                        if setting["id"] == "top.ps.priority")
                        if retained != override_order:
                            raise AssertionError("Shared priority edit changed the enabled override")
                        bridge.stop(wait=True)
                        host._settings["telesto_enabled"] = True
                        priority = next(setting["value"] for setting in inventory["settings"]
                                        if setting["id"] == "uwu.priority")
                        host._settings["triggevent_automark_settings"].update({
                            "uwu.priority": list(reversed(priority)), "uwu.clear_delay_ms": 2000,
                            "future.unknown": True})
                        host._native_automark_inventory = None
                    else:
                        host._apply_native_automark_state()
                expected = expected_marks + [f"/mk clear <{slot}>" for slot in range(1, 9)]
                wait_for(lambda: len(endpoint.commands) >= len(expected),
                         "Delayed native jail clears did not reach Telesto", seconds=16)
                if endpoint.commands != expected:
                    raise AssertionError(f"Incorrect clear order or stale actions: {endpoint.commands}")
                host._settings["triggevent_automark_settings"]["uwu.enabled"] = False
                host._apply_native_automark_state()
                feed(ws, [["33", "", "0", "4000000F"]], datetime.now(timezone.utc))
                feed(ws, jails, datetime.now(timezone.utc))
                if not bridge.catch_up([], 99):
                    raise AssertionError("Disabled mechanic fence was rejected")
                wait_for(lambda: any(item.get("checkpoint") == 99 for item in progress),
                         "Disabled native mechanic did not process")
                if endpoint.commands != expected:
                    raise AssertionError("Disabled native mechanic emitted markers or obsolete clears")
                events.clear()
                host._settings["triggevent_automark_settings"]["uwu.enabled"] = True
                host._apply_native_automark_state()
                wait_for(lambda: any(setting["id"] == "uwu.enabled" and setting["value"] is True
                                     for setting in host._native_automark_inventory["settings"]),
                         "Native UWU did not re-enable before invalid URI test")
                panel = host._native_automarkers_panel
                panel.findChild(QCheckBox, "top.panto.enabled.value").click()
                wait_for(lambda: "error" not in host._native_automark_inventory and any(
                    setting["id"] == "top.panto.enabled" and setting["value"] is True
                    for setting in host._native_automark_inventory["settings"]),
                    "Unsupported saved preference blocked a supported native edit")
                if host._settings["triggevent_automark_settings"].get("future.unknown") is not True:
                    raise AssertionError("Supported edit erased an unsupported saved preference")
                host._settings["triggevent_automark_settings"]["top.sigma.delay_seconds"] = 51
                host._apply_native_automark_state()
                wait_for(lambda: "error" in host._native_automark_inventory,
                         "Invalid saved delay was not rejected")
                if panel.findChild(QSpinBox, "top.sigma.delay_seconds.value").value() != 0:
                    raise AssertionError("Invalid saved delay did not display its native fallback")
                panel.findChild(QPushButton, "top.sigma.delay_seconds.reset").click()
                wait_for(lambda: "error" not in host._native_automark_inventory
                         and host._settings["triggevent_automark_settings"]["top.sigma.delay_seconds"] == 0,
                         "Explicit reset did not repair the rejected saved delay")
                panel.findChild(QCheckBox, "top.looper.enabled.value").click()
                wait_for(lambda: any(setting["id"] == "top.looper.enabled" and setting["value"] is True
                                     for setting in host._native_automark_inventory["settings"]),
                         "Repaired delay still blocked an unrelated native setting")
                events.clear()
                host._settings.update(telesto_enabled=False, telesto_uri="invalid URI",
                                      triggevent_automark_settings={})
                host._apply_native_automark_state()
                wait_for(lambda: any(item["event"] == "engine_automark_config"
                                     and item.get("enabled") is False for item in events),
                         "Invalid URI blocked the URI-free master disable")
                feed(ws, [["33", "", "0", "4000000F"]], datetime.now(timezone.utc))
                feed(ws, jails, datetime.now(timezone.utc))
                if not bridge.catch_up([], 101):
                    raise AssertionError("Invalid URI master disable fence was rejected")
                wait_for(lambda: any(item.get("checkpoint") == 101 for item in progress),
                         "Invalid URI disabled inputs did not process")
                if endpoint.commands != expected:
                    raise AssertionError("Invalid URI prevented master disable and emitted markers")
                events.clear()
                host._settings.update(telesto_enabled=True, umad_gaze_enabled=True)
                host._apply_native_automark_state()
                wait_for(lambda: any(item["event"] == "engine_automark_config"
                                     and item.get("native_umad") is False for item in events),
                         "Invalid URI blocked URI-free local UMAD ownership")
                if endpoint.commands != expected:
                    raise AssertionError("Local ownership transition emitted unexpected actions")
                print("PASS real WSClient, bridge inventory replay, native UWU actor selection and ordered Telesto marks")
                print("PASS native controls edited before engine startup retain saved choices through live inventory and restart")
                print("PASS disabled negative case, engine restart settings replay and delayed native clearing")
                print("PASS complete native controls, saved priority and delay replay, identical settings and mechanic disable")
                print("PASS enabled TOP priority override survives shared priority changes and engine restart")
                print("PASS unsupported saved preferences survive restart and supported native edits")
                print("PASS explicit reset repairs an invalid saved delay and native edits resume")
                print("PASS invalid URI with empty preferences cannot block master disable or local UMAD ownership")
            except BaseException:
                print("\n".join(diagnostics[-50:]))
                print(json.dumps(events[-20:], indent=2))
                raise
            finally:
                bridge.stop(wait=True)
                host._telesto_client.stop()
                host._native_automarkers_panel.close()
                host._native_automarkers_panel.deleteLater()
                app.processEvents()


if __name__ == "__main__":
    main()
