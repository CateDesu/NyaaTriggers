import os
import http.client
import json
import queue
import subprocess
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt6.QtCore import QObject, Qt, QTimer
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QMessageBox

APP = QApplication.instance() or QApplication(["delivery-tests"])

from nyaatriggers.triggevent_bridge import TriggeventBridge
from nyaatriggers.ui.engines import EnginesMixin
from nyaatriggers.ui.voice_tab import VoiceTabMixin
from nyaatriggers import plugin_link
from nyaatriggers.telesto_client import TelestoClient
from tests.test_transport_deadlines import PluginPeer, wait_for
from tests.test_triggernometry_telesto import FakeTelesto, envelope
from nyaatriggers.triggernometry_telesto import TriggernometryTelesto
from nyaatriggers.cactbot_reader import CactbotReader
from nyaatriggers import app_common as ac
from nyaatriggers import tts
from nyaatriggers.trigger_engine import Trigger
from tests import test_session_ui as session_ui


_ACTUAL_WARNING = QMessageBox.warning


class PersonalStatusTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        session_ui.SessionUiTests.setUpClass()

    def setUp(self):
        session_ui.SessionUiTests.setUp(self)
        self.window._on_status_changed(True, "Connected")
        self.window._on_ws_zone_changed(1, "Duty")
        self.window._on_ws_primary_player(0x10000001, "Same Name")
        self.window._local_enabled = True

    def status(self, scope, actor, kind="26"):
        fields = [kind, "ts", "ABC", "Status", "30", "40000001", "Boss",
                  "10000001", "Same Name", "01"]
        fields[7 if scope == "self" else 5] = actor
        fields[8 if scope == "self" else 6] = "Same Name"
        self.window._on_log_line("|".join(fields))

    def test_same_name_actor_cannot_fire_or_consume_personal_cooldown(self):
        for scope in ("self", "by_me"):
            for kind in ("26", "30"):
                with self.subTest(scope=scope, kind=kind):
                    trigger = Trigger(log_type=kind, ability_id="ABC", status_scope=scope,
                                      cooldown_s=10, tts_text="Personal warning")
                    self.window._triggers = [trigger]
                    with patch.object(self.window, "_fire") as fire:
                        self.status(scope, "10000002", kind)
                        self.assertFalse(fire.called)
                        self.assertFalse(trigger._last_fired)
                        self.status(scope, "10000001", kind)
                        self.assertEqual(fire.call_count, 1)
                        self.status(scope, "10000001", kind)
                        self.assertEqual(fire.call_count, 1)

    def test_same_name_actor_cannot_arm_or_cancel_personal_expiry(self):
        for scope in ("self", "by_me"):
            with self.subTest(scope=scope):
                self.window._clear_status_timers()
                trigger = Trigger(log_type="26|30", ability_id="ABC", status_scope=scope,
                                  expiry_warn_s=5, tts_text="Refresh")
                self.window._triggers = [trigger]
                self.status(scope, "10000002")
                self.assertFalse(self.window._status_timers)
                self.status(scope, "10000001")
                runner, = self.window._status_timers
                self.status(scope, "10000002", "30")
                self.assertEqual(self.window._status_timers, [runner])
                self.status(scope, "10000001", "30")
                self.assertFalse(self.window._status_timers)

    def test_identity_is_authoritative_and_unknown_identity_keeps_name_fallback(self):
        fields = ["26", "ts", "ABC", "Status", "30", "40000001", "Boss",
                  "100000AB", "Current Name", "01"]
        for name in ("", "Saved Name"):
            with self.subTest(name=name):
                trigger = Trigger(log_type="26", cooldown_s=0)
                self.assertIsNotNone(trigger.matches(fields, me=name, me_id="100000ab"))
        for unknown in ("", "0", "E0000000", "invalid"):
            with self.subTest(unknown=unknown):
                trigger = Trigger(log_type="26", cooldown_s=0)
                self.assertIsNotNone(trigger.matches(fields, me="current name", me_id=unknown))
                self.assertIsNone(trigger.matches(fields, me="Other Name", me_id=unknown))
        for actor in ("", "invalid", "100000AC"):
            with self.subTest(actor=actor):
                fields[7] = actor
                trigger = Trigger(log_type="26", cooldown_s=0)
                self.assertIsNone(trigger.matches(fields, me="Current Name", me_id="100000AB"))
                trigger.status_scope = "any"
                self.assertIsNotNone(trigger.matches(fields, me="Current Name", me_id="100000AB"))

    def test_reconnect_and_raw_identity_replace_the_personal_target(self):
        trigger = Trigger(log_type="26", ability_id="ABC", cooldown_s=0,
                          expiry_warn_s=5, tts_text="Refresh")
        self.window._triggers = [trigger]
        self.status("self", "10000001")
        self.assertEqual(len(self.window._status_timers), 1)
        self.window._on_status_changed(False, "Disconnected")
        self.assertFalse(self.window._status_timers)
        self.window._on_status_changed(True, "Connected")
        self.window._on_ws_zone_changed(1, "Duty")
        self.window._on_log_line("02|ts|10000002|Same Name")
        self.status("self", "10000001")
        self.assertFalse(self.window._status_timers)
        self.status("self", "10000002")
        self.assertEqual(len(self.window._status_timers), 1)


class CactbotChecklistTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        session_ui.SessionUiTests.setUpClass()

    def setUp(self):
        session_ui.SessionUiTests.setUp(self)
        self.reader = CactbotReader(self.window)
        self.window._cactbot_reader = self.reader
        self.window._on_cactbot_triggers_enumerated(json.dumps([
            {"id": "Checklist probe", "name": "Checklist probe", "zone": "Duty"}]))

    def test_checklist_changes_reach_reader_inside_actual_save_failure_dialog(self):
        item = self.window._cactbot_trig_list.item(0)
        self.window._cactbot_trig_list.setCurrentItem(item)
        settings = ac._SETTINGS_FILE
        settings.unlink(missing_ok=True)
        settings.mkdir()
        try:
            for expected in ({"Checklist probe"}, set()):
                with self.subTest(disabled=bool(expected)):
                    self.window._save_warned = False
                    observed = []
                    timer = QTimer()

                    def inspect_warning():
                        dialog = QApplication.activeModalWidget()
                        if isinstance(dialog, QMessageBox):
                            observed.append(set(self.reader._disabled))
                            timer.stop()
                            dialog.accept()

                    timer.timeout.connect(inspect_warning)
                    with patch.object(ac.QMessageBox, "warning", _ACTUAL_WARNING):
                        timer.start(10)
                        QTest.keyClick(self.window._cactbot_trig_list, Qt.Key.Key_Space)
                    timer.stop()
                    self.assertEqual(observed, [expected])
                    self.assertEqual(self.window._settings["cactbot_disabled_triggers"], sorted(expected))
        finally:
            settings.rmdir()

    def test_checklist_saves_choices_before_reader_startup(self):
        self.window._cactbot_reader = None
        item = self.window._cactbot_trig_list.item(0)
        item.setCheckState(Qt.CheckState.Unchecked)
        stored = json.loads(ac._SETTINGS_FILE.read_text())
        self.assertEqual(stored["cactbot_disabled_triggers"], ["Checklist probe"])


class LocalTimelineToggleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        session_ui.SessionUiTests.setUpClass()

    def setUp(self):
        session_ui.SessionUiTests.setUp(self)

    def test_local_timeline_already_deferred_cannot_speak_inside_save_failure(self):
        window = self.window
        window._local_enabled = True
        window._global_local_on_flag = True
        window._triggers_enabled = True
        window._timeline_from_cactbot = False
        settings = ac._SETTINGS_FILE
        settings.unlink(missing_ok=True)
        settings.mkdir()
        timer = QTimer()
        observed = []
        try:
            with patch("nyaatriggers.main_window.speak") as speech:
                window._on_timeline_tts("Deferred timeline warning")
                self.assertTrue(window._pending_guests)

                def inspect_warning():
                    dialog = QApplication.activeModalWidget()
                    if isinstance(dialog, QMessageBox):
                        observed.append(speech.call_count)
                        timer.stop()
                        dialog.accept()

                timer.timeout.connect(inspect_warning)
                with patch.object(ac.QMessageBox, "warning", _ACTUAL_WARNING):
                    timer.start(350)
                    window._toggle_global_local()
                self.assertEqual(observed, [0])
                self.assertFalse(window._pending_guests)
                window._toggle_global_local()
                window._on_timeline_tts("Fresh timeline warning")
                QTest.qWait(300)
                speech.assert_called_once()
                self.assertEqual(speech.call_args.args[0], "Fresh timeline warning")
        finally:
            timer.stop()
            settings.rmdir()


class WholeModeSpeechTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        session_ui.SessionUiTests.setUpClass()

    def setUp(self):
        session_ui.SessionUiTests.setUp(self)

    def test_mode_switch_stops_active_and_queued_speech_but_noop_preserves_them(self):
        from nyaatriggers.ui import triggers_tab
        for previous, enabled in ((True, False), (False, True), (True, True)):
            with self.subTest(previous=previous, enabled=enabled):
                self.window._triggers_enabled = previous
                proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
                pending = queue.Queue()
                try:
                    with patch.object(tts, "_current_proc", proc), \
                            patch.object(tts, "_interrupted_proc", None), \
                            patch.object(tts, "_queue", pending), \
                            patch.object(tts, "_master_volume", 1.0), \
                            patch.object(tts, "_speech_suspended", False), \
                            patch.object(triggers_tab.TriggeventBridge, "is_available", return_value=False), \
                            patch.object(triggers_tab.TriggernometryBridge, "is_available", return_value=False):
                        tts._enqueue(("tts", "Previous mode speech", 1.0, 1.0, None))
                        self.window._set_triggers_enabled(enabled)
                        if previous == enabled:
                            self.assertIsNone(proc.poll())
                            self.assertEqual(pending.get_nowait()[1], "Previous mode speech")
                        else:
                            self.assertTrue(pending.empty())
                            proc.wait(timeout=2)
                            tts._enqueue(("tts", "Fresh mode speech", 1.0, 1.0, None))
                            self.assertEqual(pending.get_nowait()[1], "Fresh mode speech")
                finally:
                    if proc.poll() is None:
                        proc.kill()
                    proc.wait(timeout=2)

    def test_feed_loss_clears_local_queue_and_preserves_independent_cactbot_speech(self):
        for cactbot in (False, True):
            with self.subTest(cactbot=cactbot):
                self.window._cactbot_mode = cactbot
                pending = queue.Queue()
                with patch.object(tts, "_queue", pending), \
                        patch.object(tts, "_master_volume", 1.0), \
                        patch.object(tts, "_speech_suspended", False):
                    tts._enqueue(("tts", "Accepted speech", 1.0, 1.0, None))
                    self.window._on_status_changed(False, "Disconnected")
                    self.assertEqual(pending.empty(), not cactbot)
                    if cactbot:
                        self.assertEqual(pending.get_nowait()[1], "Accepted speech")

    def test_encounter_boundaries_retire_accepted_local_speech(self):
        for boundary in ("raw zone", "changed zone", "wipe"):
            with self.subTest(boundary=boundary):
                self.window._cactbot_mode = False
                self.window._current_zone = "Arena"
                self.window._current_zone_id = 1
                self.window._awaiting_zone_metadata = False
                pending = queue.Queue()
                proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
                try:
                    with patch.object(tts, "_queue", pending), \
                            patch.object(tts, "_current_proc", proc), \
                            patch.object(tts, "_interrupted_proc", None), \
                            patch.object(tts, "_master_volume", 1.0), \
                            patch.object(tts, "_speech_suspended", False):
                        tts._enqueue(("tts", "Previous pull speech", 1.0, 1.0, None))
                        if boundary == "raw zone":
                            self.window._on_log_line("01|ts|1|Arena|")
                        elif boundary == "changed zone":
                            self.window._on_ws_zone_changed(2, "New arena")
                        else:
                            self.window._on_log_line("33|ts|80000000|4000000F|0|")
                        self.assertTrue(pending.empty())
                        proc.wait(timeout=2)
                        tts._enqueue(("tts", "Fresh pull speech", 1.0, 1.0, None))
                        self.assertEqual(pending.get_nowait()[1], "Fresh pull speech")
                finally:
                    if proc.poll() is None:
                        proc.kill()
                    proc.wait(timeout=2)

    def test_metadata_and_combat_end_preserve_accepted_speech(self):
        self.window._cactbot_mode = False
        self.window._current_zone = "Arena"
        self.window._current_zone_id = 1
        self.window._awaiting_zone_metadata = False
        pending = queue.Queue()
        with patch.object(tts, "_queue", pending), \
                patch.object(tts, "_master_volume", 1.0), \
                patch.object(tts, "_speech_suspended", False):
            tts._enqueue(("tts", "Current pull speech", 1.0, 1.0, None))
            self.window._on_ws_zone_changed(1, "Corrected arena name")
            self.window._on_in_combat(False, False)
            self.assertEqual(pending.get_nowait()[1], "Current pull speech")

    def test_main_feed_boundaries_preserve_independent_cactbot_speech(self):
        self.window._cactbot_mode = True
        pending = queue.Queue()
        with patch.object(tts, "_queue", pending), \
                patch.object(tts, "_master_volume", 1.0), \
                patch.object(tts, "_speech_suspended", False):
            tts._enqueue(("tts", "Independent reader speech", 1.0, 1.0, None))
            self.window._on_log_line("01|ts|1|Arena|")
            self.window._on_log_line("33|ts|80000000|4000000F|0|")
            self.assertEqual(pending.get_nowait()[1], "Independent reader speech")


class RelayCommandCancellationTests(unittest.TestCase):
    def test_disabling_automarkers_cancels_a_relay_command_still_connecting(self):
        for reenable in (False, True):
            with self.subTest(reenable=reenable), FakeTelesto() as peer:
                relay = TriggernometryTelesto(lambda _body: None, lambda _msg: None,
                                             peer.url, commands_enabled=True)
                entered, release = threading.Event(), threading.Event()
                connect = http.client.HTTPConnection.connect
                results = []

                def delayed_connect(connection):
                    entered.set()
                    release.wait(2)
                    return connect(connection)

                command = envelope("ExecuteCommand", command="/mk attack1 <1>")
                worker = threading.Thread(target=lambda: results.append(relay.forward(command)))
                try:
                    with patch.object(http.client.HTTPConnection, "connect", delayed_connect):
                        worker.start()
                        self.assertTrue(entered.wait(1))
                        relay.configure(False)
                        if reenable:
                            relay.configure(True)
                        release.set()
                        worker.join(timeout=3)
                    self.assertFalse(worker.is_alive())
                    self.assertTrue(peer.messages.empty())
                    self.assertEqual(results[0][0], 502)
                    relay.configure(True)
                    self.assertEqual(relay.forward(envelope("ExecuteCommand",
                                                           command="/mk attack2 <1>"))[0], 200)
                    self.assertEqual(peer.next("ExecuteCommand")["payload"]["command"],
                                     "/mk attack2 <1>")
                finally:
                    release.set()
                    worker.join(timeout=3)
                    relay.close(wait=True)

    def test_drawing_only_request_survives_disabling_automarkers(self):
        with FakeTelesto() as peer:
            relay = TriggernometryTelesto(lambda _body: None, lambda _msg: None,
                                         peer.url, commands_enabled=True)
            relay.start()
            entered, release = threading.Event(), threading.Event()
            connect = http.client.HTTPConnection.connect
            results = []

            def delayed_connect(connection):
                entered.set()
                release.wait(2)
                return connect(connection)

            drawing = envelope("EnableDoodle", name="safe", type="circle", radius="5")
            worker = threading.Thread(target=lambda: results.append(relay.forward(drawing)))
            try:
                with patch.object(http.client.HTTPConnection, "connect", delayed_connect):
                    worker.start()
                    self.assertTrue(entered.wait(1))
                    relay.configure(False)
                    release.set()
                    worker.join(timeout=3)
                self.assertFalse(worker.is_alive())
                self.assertEqual(results[0][0], 200)
                self.assertEqual(peer.next("EnableDoodle")["payload"]["radius"], "5")
                self.assertTrue(relay._drawings)
            finally:
                release.set()
                worker.join(timeout=3)
                relay.close(wait=True)
            self.assertFalse(peer.drawings)

    def test_queued_bundle_cannot_revive_commands_after_reenable(self):
        for initially_enabled in (True, False):
            with self.subTest(initially_enabled=initially_enabled), FakeTelesto() as peer:
                relay = TriggernometryTelesto(lambda _body: None, lambda _msg: None,
                                             peer.url, commands_enabled=initially_enabled)
                relay.start()
                entered = threading.Event()
                sending = threading.Lock()

                class ObservedLock:
                    def __enter__(self):
                        entered.set()
                        sending.acquire()

                    def __exit__(self, *args):
                        sending.release()

                relay._sending = ObservedLock()
                sending.acquire()
                bundle = {"type": "Bundle", "payload": [
                    {"type": "Bundle", "payload": [envelope("Macro", command="/mk attack1 <1>")]},
                    envelope("EnableDoodle", name="safe", type="circle", radius="5")]}
                results = []
                worker = threading.Thread(target=lambda: results.append(relay.forward(bundle)))
                try:
                    worker.start()
                    self.assertTrue(entered.wait(1))
                    relay.configure(False)
                    relay.configure(True)
                    sending.release()
                    worker.join(timeout=3)
                    self.assertFalse(worker.is_alive())
                    self.assertEqual(results[0][0], 200)
                    messages = []
                    while not peer.messages.empty():
                        messages.append(peer.messages.get_nowait())
                    self.assertEqual([message["type"] for message in messages], ["EnableDoodle"])
                    self.assertEqual(relay.forward(envelope("Macro", command="/mk attack2 <1>"))[0], 200)
                    self.assertEqual(peer.next("Macro")["payload"]["command"], "/mk attack2 <1>")
                finally:
                    if sending.locked():
                        sending.release()
                    worker.join(timeout=3)
                    relay.close(wait=True)


class DeliveryHost(QObject, EnginesMixin, VoiceTabMixin):
    def __init__(self):
        super().__init__()
        self._triggevent = TriggeventBridge(self)
        self._triggevent._active = True
        self._triggevent._gen = 7
        self._triggevent_mode = True
        self._connected = True
        self.alerts = []
        self.speech = []
        self._triggevent.callout.connect(self._on_triggevent_callout)
        self._triggevent.tts.connect(self._on_triggevent_tts)

    def _localize_text(self, text):
        return text

    def _emit_alert(self, text, severity):
        self.alerts.append((text, severity))

    def _triggevent_speak(self, text):
        self.speech.append(text)

    def queue_callout(self, text, generation=7):
        worker = threading.Thread(target=lambda: self._triggevent._dispatch(
            {"t": "callout", "text": text, "tts": text, "severity": "alert"},
            gen=generation))
        worker.start()
        worker.join(timeout=2)
        self.assert_thread_stopped(worker)

    @staticmethod
    def assert_thread_stopped(worker):
        if worker.is_alive():
            raise AssertionError("Callout delivery worker did not stop")


class CalloutDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.host = DeliveryHost()

    def test_feed_loss_drops_callouts_already_queued_from_the_reader(self):
        self.host.queue_callout("Old pull")
        self.assertEqual(self.host.alerts, [])
        self.assertEqual(self.host.speech, [])
        self.host._connected = False
        APP.processEvents()
        self.assertEqual(self.host.alerts, [])
        self.assertEqual(self.host.speech, [])
        self.host._connected = True
        self.host.queue_callout("Live pull")
        APP.processEvents()
        self.assertEqual(self.host.alerts, [("Live pull", "alert")])
        self.assertEqual(self.host.speech, ["Live pull"])

    def test_reconnect_still_rejects_the_previous_engine_generation(self):
        self.host.queue_callout("Old engine")
        self.host._triggevent._gen = 8
        APP.processEvents()
        self.assertEqual(self.host.alerts, [])
        self.assertEqual(self.host.speech, [])
        self.host.queue_callout("Current engine", generation=8)
        APP.processEvents()
        self.assertEqual(self.host.alerts, [("Current engine", "alert")])
        self.assertEqual(self.host.speech, ["Current engine"])

    def test_disabling_callouts_drops_both_queued_outputs(self):
        self.host.queue_callout("Disabled")
        self.host._triggevent_mode = False
        APP.processEvents()
        self.assertEqual(self.host.alerts, [])
        self.assertEqual(self.host.speech, [])


class OverflowHost(QObject, EnginesMixin):
    def __init__(self):
        super().__init__()
        self._triggernometry = None
        self._triggernometry_mode = True
        self._connected = True
        self._triggernometry_disabled = {"disabled-trigger"}
        self._ws = MagicMock()
        self.restarts = []
        self._ensure_triggernometry_bridge()
        self._triggernometry._gen = 4
        self._triggernometry._active = True
        self._triggernometry._wq = queue.Queue(maxsize=1)
        self._triggernometry.feed_log("queued")

    def _apply_engine_overrides(self, source):
        pass

    def _on_triggernometry_tts(self, *args):
        pass

    def _on_triggernometry_sound(self, *args):
        pass

    def _on_engine_sidecar_status(self, *args):
        pass

    def _set_triggernometry_enabled(self, enabled):
        self.restarts.append((enabled, threading.get_ident(),
                              self._triggernometry.is_active()))


class TriggernometryOverflowDeliveryTests(unittest.TestCase):
    def test_relay_overflow_restarts_after_releasing_the_state_lock(self):
        host = OverflowHost()
        bridge = host._triggernometry
        worker = threading.Thread(target=bridge._feed_endpoint,
                                  args=("overflow", bridge.generation()), daemon=True)
        worker.start()
        worker.join(timeout=2)
        self.assertFalse(worker.is_alive())
        self.assertTrue(bridge.is_active())
        self.assertEqual(host.restarts, [])
        APP.processEvents()
        self.assertEqual(host.restarts, [(True, threading.get_ident(), False)])
        self.assertEqual(bridge._disabled, {"disabled-trigger"})

    def test_pending_overflow_cannot_restart_an_engine_after_disconnect_or_disable(self):
        for field in ("_connected", "_triggernometry_mode"):
            with self.subTest(field=field):
                host = OverflowHost()
                host._triggernometry.feed_log("overflow")
                setattr(host, field, False)
                APP.processEvents()
                self.assertEqual(host.restarts, [])
                self.assertFalse(host._triggernometry.is_active())

    def test_old_overflow_cannot_stop_a_replacement_generation(self):
        host = OverflowHost()
        host._triggernometry.feed_log("overflow")
        host._triggernometry._gen += 1
        APP.processEvents()
        self.assertEqual(host.restarts, [])
        self.assertTrue(host._triggernometry.is_active())


class AlertRetryDeliveryTests(unittest.TestCase):
    def check_retry(self, fill_queue):
        peer = PluginPeer()
        self.addCleanup(peer.close)
        link = plugin_link.PluginLink(port=peer.port)
        link._queue = queue.Queue(maxsize=3)
        self.addCleanup(link.stop)
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        send = link._send
        failed = False

        def fail_first(ws, message):
            nonlocal failed
            if message.get("text") == "First mechanic" and not failed:
                failed = True
                entered.set()
                release.wait(3)
                raise OSError("connection lost during send")
            send(ws, message)

        with patch.object(link, "_send", side_effect=fail_first):
            link.start()
            self.assertTrue(wait_for(link.is_connected))
            link.send_alert("First mechanic")
            self.assertTrue(entered.wait(2))
            if fill_queue:
                link.send_tick(1)
                link.send_tick(2)
            link.send_alert("Second mechanic")
            release.set()
            self.assertTrue(wait_for(lambda: any(frame.get("text") == "Second mechanic"
                                                for frame in peer.frames)))
            self.assertEqual([frame["text"] for frame in peer.frames
                              if frame.get("c") == "alert"],
                             ["First mechanic", "Second mechanic"])

    def test_send_failure_preserves_callout_order_across_reconnect(self):
        self.check_retry(False)

    def test_retry_replaces_queued_ticks_instead_of_dropping_the_callout(self):
        self.check_retry(True)


class ActorMarkerDeliveryTests(unittest.TestCase):
    def deliver_after_party_refresh(self, roster, enqueue):
        entered, release = threading.Event(), threading.Event()
        messages = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                message = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                messages.append(message)
                if message["type"] == "GetPartyMembers":
                    entered.set()
                    release.wait(3)
                    response = json.dumps({"response": roster}).encode()
                else:
                    response = b"{}"
                self.send_response(200)
                self.send_header("Content-Length", str(len(response)))
                self.end_headers()
                self.wfile.write(response)

        peer = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        peer.daemon_threads = True
        server = threading.Thread(target=peer.serve_forever,
                                  kwargs={"poll_interval": .02}, daemon=True)
        server.start()
        client = TelestoClient(uri=f"http://127.0.0.1:{peer.server_port}/", enabled=True,
                               delay_base_ms=0, delay_plus_ms=0)
        client._update_party_slots(b'{"response":[{"actor":"10000001","order":"1"},'
                                   b'{"actor":"10000002","order":"2"}]}')
        client.start()
        try:
            client.ping()
            self.assertTrue(entered.wait(2))
            enqueue(client)
            client.mark_self("circle")
            release.set()
            self.assertTrue(wait_for(lambda: any(message.get("payload", {}).get("command")
                                                == "/mk circle <me>" for message in messages)))
            return [message["payload"]["command"] for message in messages
                    if message["type"] == "ExecuteCommand"]
        finally:
            release.set()
            client.stop()
            peer.shutdown()
            peer.server_close()
            server.join(timeout=2)

    def test_queued_actor_marks_and_clears_follow_the_refreshed_party_order(self):
        def enqueue(client):
            self.assertTrue(client.mark_actor("10000001", "attack1"))
            self.assertTrue(client.clear_actor("10000002"))
            self.assertTrue(client.mark_slot("square", 1))

        sent = self.deliver_after_party_refresh(
            [{"actor": "10000002", "order": "1"}, {"actor": "10000001", "order": "2"}],
            enqueue)
        self.assertEqual(sent, ["/mk attack1 <2>", "/mk clear <1>",
                                "/mk square <1>", "/mk circle <me>"])

    def test_actor_commands_cannot_target_a_replacement_party_member(self):
        def enqueue(client):
            self.assertTrue(client.mark_actor("10000001", "attack1"))
            self.assertTrue(client.clear_actor("10000001", force=True))

        sent = self.deliver_after_party_refresh(
            [{"actor": "10000003", "order": "1"}, {"actor": "10000002", "order": "2"}],
            enqueue)
        self.assertEqual(sent, ["/mk circle <me>"])


if __name__ == "__main__":
    unittest.main()
