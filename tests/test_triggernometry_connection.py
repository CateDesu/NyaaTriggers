"""Replay combat and delayed speech across real feed connections."""

from contextlib import ExitStack
import json
import os
from pathlib import Path
import shutil
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
import xml.etree.ElementTree as ET

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt6.QtCore import QObject
from PyQt6.QtNetwork import QHostAddress
from PyQt6.QtTest import QTest
from PyQt6.QtWebSockets import QWebSocketServer
from PyQt6.QtWidgets import QApplication

from nyaatriggers import app_common as ac
from nyaatriggers import triggernometry_bridge as bridge
from nyaatriggers.dps_meter import DpsMeter
from nyaatriggers.main_window import MainWindow
from nyaatriggers.triggernometry_editor import PackDocument
from nyaatriggers.ui.connection import ConnectionMixin
from nyaatriggers.ui.engines import EnginesMixin
from nyaatriggers.ui.instance_tab import InstanceTabMixin
from nyaatriggers.ui.voice_tab import VoiceTabMixin
from nyaatriggers.ws_client import WSClient

APP = QApplication.instance() or QApplication([])


class Host(QObject, EnginesMixin, InstanceTabMixin, VoiceTabMixin, ConnectionMixin):
    _dedup_speak_gate = MainWindow._dedup_speak_gate

    def __init__(self):
        super().__init__()
        self._ws = WSClient(self)
        self._ws.status_changed.connect(self._on_status_changed)
        self._triggernometry = None
        self._triggernometry_mode = False
        self._triggernometry_disabled = set()
        self._triggernometry_last_spoken = {}
        self._settings = {}
        self._current_zone = ""
        self._current_zone_id = 0
        self._connected = False
        self._umad_chain_enabled = False
        for name in ("_status_lbl", "_conn_btn", "_zone_lbl", "_plugin_link",
                     "_clear_status_timers", "_clear_seq_runners", "_clear_callout_dedup",
                     "_push_timeline_to_plugin", "_apply_engine_overrides",
                     "_on_engine_sidecar_status", "_umad_chain_reset", "_umad_gaze_reset",
                     "_automark_pairs"):
            setattr(self, name, Mock())
        self._zone_banner_text = lambda: ""
        self.inventory = []
        self._on_triggernometry_inventory = lambda *args: self.inventory.append(args)
        self.alerts = []
        self._emit_alert = lambda text, severity: self.alerts.append(text)
        self._localize_text = lambda text: text
        self._reading_for = lambda text: text
        self._callout_edits_for = lambda _source: {}
        self._triggers = []
        self._dps_meter = DpsMeter()
        self._actor_jobs = {}
        self._umad_actor_names = {}
        self._automark_pending = {}
        self._automark_active = {}


@unittest.skipUnless(os.name == "posix" and shutil.which("mono") and shutil.which("xvfb-run"),
                     "Requires Mono and Xvfb")
class ConnectionTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        packs, runtime = root / "packs", root / "runtime"
        packs.mkdir()
        runtime.mkdir()
        document = PackDocument(packs / "delayed.xml")
        trigger = document.add_trigger()
        trigger.set("RegularExpression", r"TEST (?<label>\w+)")
        actions = trigger.find("Actions")
        ET.SubElement(actions, "Action", OrderNumber="1", ActionType="UseTTS", Asynchronous="False",
                      UseTTSTextExpression="armed ${label} ${_incombat}")
        ET.SubElement(actions, "Action", OrderNumber="2", ActionType="Placeholder", Asynchronous="False",
                      ExecutionDelayExpression="1200")
        ET.SubElement(actions, "Action", OrderNumber="3", ActionType="UseTTS", Asynchronous="False",
                      UseTTSTextExpression="late ${label}")
        document.save()
        self.stack.enter_context(patch.object(bridge, "_rundata_dir", return_value=runtime))
        self.stack.enter_context(patch.object(bridge, "_packs_dir", return_value=packs))
        self.stack.enter_context(patch.object(bridge, "_make_bundled_mono_executable"))
        self.stack.enter_context(patch.object(bridge, "_log"))
        self.host = Host()
        self.speech = []
        self.stack.enter_context(patch("nyaatriggers.ui.engines.speak", side_effect=
                                      lambda text, **kwargs: self.speech.append(text)))
        self.server = QWebSocketServer("test", QWebSocketServer.SslMode.NonSecureMode)
        self.assertTrue(self.server.listen(QHostAddress(QHostAddress.SpecialAddress.LocalHost), 0))
        self.peers = []
        self.server.newConnection.connect(lambda: self.peers.append(self.server.nextPendingConnection()))
        self.addCleanup(self.shutdown)

    def shutdown(self):
        self.host._ws.disconnect_from()
        if self.host._triggernometry:
            self.host._triggernometry.stop(wait=True)
        for peer in self.peers:
            peer.close()
        self.server.close()
        QTest.qWait(50)

    def wait(self, predicate):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not predicate():
            QTest.qWait(10)
        self.assertTrue(predicate(), self.speech)

    def connect(self):
        previous = len(self.peers)
        self.host._ws.connect_to(f"ws://127.0.0.1:{self.server.serverPort()}/ws")
        self.wait(lambda: self.host._connected and len(self.peers) > previous)

    def send(self, data):
        self.peers[-1].sendTextMessage(json.dumps(data))

    def probe(self, label, combat=0):
        raw = f"00|2026-09-25T12:00:00.0000000-05:00|0038|Tester|TEST {label}|checksum"
        self.send({"type": "LogLine", "rawLine": raw})
        self.wait(lambda: f"armed {label} {combat}" in self.speech)

    def enable(self):
        previous = len(self.host.inventory)
        self.host._set_triggernometry_enabled(True)
        self.wait(lambda: len(self.host.inventory) > previous)

    def check_external_pack_reimport(self, use_copy):
        self.connect()
        self.enable()
        self.host._triggers_enabled = True
        self.host._local_ids = set()
        managed = bridge.packs_dir() / "delayed.xml"
        source = managed.parent.parent / "copy.xml" if use_copy else managed
        original = managed.read_bytes()
        if use_copy:
            source.write_bytes(original)
        self.stack.enter_context(patch.object(ac.QFileDialog, "getOpenFileName",
                                             return_value=(str(source), "")))
        information = self.stack.enter_context(patch.object(ac.QMessageBox, "information"))
        critical = self.stack.enter_context(patch.object(ac.QMessageBox, "critical"))
        warning = self.stack.enter_context(patch.object(ac.QMessageBox, "warning"))
        question = self.stack.enter_context(patch.object(ac.QMessageBox, "question"))
        generation = self.host._triggernometry.generation()
        self.probe("before")
        self.host._import_triggernometry()
        self.assertEqual(self.host._triggernometry.generation(), generation)
        self.wait(lambda: "late before" in self.speech)

        modified = original.replace(b"armed ${label}", b"fresh ${label}")
        stamp = managed.stat()
        managed.write_bytes(modified)
        os.utime(managed, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        if use_copy:
            source.write_bytes(modified)
        self.host._import_triggernometry()
        self.assertGreater(self.host._triggernometry.generation(), generation)
        self.wait(lambda: len(self.host.inventory) == 2)
        self.send({"type": "LogLine", "rawLine":
                   "00|2026-09-26T12:00:00-05:00|0038|Tester|TEST after|checksum"})
        self.wait(lambda: "fresh after 0" in self.speech)
        self.assertNotIn("armed after 0", self.speech)

        generation = self.host._triggernometry.generation()
        self.host._import_triggernometry()
        self.assertEqual(self.host._triggernometry.generation(), generation)
        self.wait(lambda: "late after" in self.speech)
        self.assertIn("is now running", information.call_args.args[-1])
        critical.assert_not_called()
        warning.assert_not_called()
        question.assert_not_called()

    def test_reimport_reloads_a_pack_edited_in_place(self):
        self.check_external_pack_reimport(use_copy=False)

    def test_reimport_reloads_an_external_copy_matching_the_edited_managed_pack(self):
        self.check_external_pack_reimport(use_copy=True)

    def test_disconnect_cancels_pending_speech_and_reconnect_starts_fresh(self):
        self.connect()
        self.enable()
        self.probe("old")
        generation = self.host._triggernometry.generation()
        self.host._toggle_connection()
        self.wait(lambda: not self.host._connected)
        self.assertFalse(self.host._triggernometry.is_active())
        self.assertGreater(self.host._triggernometry.generation(), generation)
        QTest.qWait(1400)
        self.assertNotIn("late old", self.speech)
        self.assertNotIn("late old", self.host.alerts)
        self.connect()
        self.wait(lambda: len(self.host.inventory) == 2)
        self.probe("fresh")
        self.wait(lambda: "late fresh" in self.speech)
        self.assertNotIn("late old", self.speech)
        self.host._set_triggernometry_enabled(False)
        self.host._toggle_connection()
        self.wait(lambda: not self.host._connected)
        self.connect()
        self.assertFalse(self.host._triggernometry.is_active())

    def test_combat_state_is_replayed_when_enabled_midfight(self):
        self.connect()
        self.send({"type": "InCombat", "inACTCombat": True, "inGameCombat": False})
        self.wait(lambda: bool(self.host._ws.state_snapshot()))
        self.enable()
        self.probe("midfight", combat=1)
        self.send({"type": "InCombat", "inACTCombat": False, "inGameCombat": True})
        self.probe("ended", combat=0)


if __name__ == "__main__":
    unittest.main()
