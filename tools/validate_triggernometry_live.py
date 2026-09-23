#!/usr/bin/env python3
"""Check a live IINACT connection with an isolated, silent Triggernometry engine."""

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="ws://127.0.0.1:10501/ws")
    parser.add_argument("--seconds", type=int, default=45)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not 1 <= args.seconds <= 300:
        parser.error("--seconds must be between 1 and 300")
    with tempfile.TemporaryDirectory(prefix="nyaa-tn-live-") as temp:
        os.environ["NYAA_TRIGGERNOMETRY_PACKS"] = temp
        os.environ["XDG_CONFIG_HOME"] = temp
        os.environ["APPDATA"] = temp
        from PyQt6.QtCore import QCoreApplication, QTimer
        from nyaatriggers.triggernometry_editor import PackDocument, validate_native, xml_bytes
        from nyaatriggers.triggernometry_bridge import TriggernometryBridge
        from nyaatriggers.ws_client import WSClient

        app = QCoreApplication([])
        document = PackDocument(Path(temp) / "live-probe.xml")
        for source, name, pattern in (("FFXIVNetwork", "network", r"^\d+\|"), ("Log", "act", r"^\[")):
            trigger = document.add_trigger()
            trigger.attrib.update(Name=name, Source=source, RegularExpression=pattern)
            ET.SubElement(trigger.find("Actions"), "Action", OrderNumber="1", ActionType="UseTTS",
                          UseTTSTextExpression=name + "|${_me.name}|${_me.currenthp}|${_me.x}|${_ffxivzoneid}")
        raw = xml_bytes(document.root)
        validate_native(raw)
        document.path.write_bytes(raw)
        bridge = TriggernometryBridge()
        ws = WSClient()
        report = {"passed": False, "connected": False, "combatant_snapshots": 0,
                  "party_members": 0, "live_lines": 0, "samples": [], "errors": []}
        state = {"me": None, "zone": None, "last_log": 0.0}

        def combatants(payload):
            bridge.feed_combatants(payload)
            report["combatant_snapshots"] += 1
            report["party_members"] = sum(c["party"] == 1 for c in payload["list"])
            state["me"] = next((c for c in payload["list"] if c["id"] == payload["me"]), None)

        def zone(ident, name):
            state["zone"] = ident
            bridge.feed_zone(ident, name)

        def log(line):
            now = time.monotonic()
            if state["me"] is not None and state["zone"] is not None and now - state["last_log"] >= 1:
                bridge.feed_log(line)
                state["last_log"] = now
                report["live_lines"] += 1

        def callout(text, _severity, _generation):
            fields = text.split("|")
            if len(fields) != 5 or state["me"] is None:
                report["errors"].append("Unexpected probe output")
                return
            try:
                valid = fields[1] == state["me"]["name"] and int(fields[4]) == state["zone"]
                valid = valid and int(fields[2]) >= 0 and abs(float(fields[3])) < 100000
            except ValueError:
                valid = False
            report["samples"].append({"source": fields[0], "resolved_live_identity_and_zone": valid,
                                      "hp": fields[2], "x": fields[3]})
            sources = {sample["source"] for sample in report["samples"] if sample["resolved_live_identity_and_zone"]}
            if sources == {"network", "act"} and report["live_lines"] >= 3:
                report["passed"] = True
                app.quit()

        def connection(connected, message):
            report["connected"] = connected
            if not connected:
                report["errors"].append(message)

        def engine(active, message, _generation):
            if not active:
                report["errors"].append(message)

        started = False

        def inventory(_payload, _generation):
            nonlocal started
            if not started:
                started = True
                ws.set_combatant_polling(True)
                ws.connect_to(args.url)

        ws.combatants.connect(combatants)
        ws.zone_changed.connect(zone)
        ws.log_line.connect(log)
        ws.status_changed.connect(connection)
        bridge.callout.connect(callout)
        bridge.status.connect(engine)
        bridge.inventory.connect(inventory)
        timeout = QTimer()
        timeout.setSingleShot(True)
        timeout.timeout.connect(app.quit)
        timeout.start(args.seconds * 1000)
        try:
            bridge.start()
            app.exec()
        finally:
            ws.status_changed.disconnect(connection)
            bridge.status.disconnect(engine)
            ws.disconnect_from()
            bridge.stop(wait=True)
        text = json.dumps(report, indent=2)
        if args.output:
            args.output.write_text(text + "\n", encoding="utf-8")
        print(text)
        return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
