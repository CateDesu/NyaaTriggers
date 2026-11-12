from contextlib import ExitStack
from datetime import datetime
import json
import os
from pathlib import Path
import signal
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from PyQt6.QtCore import QObject, QTimer
from PyQt6.QtNetwork import QAbstractSocket, QHostAddress
from PyQt6.QtTest import QTest
from PyQt6.QtWebSockets import QWebSocketServer
from PyQt6.QtWidgets import QApplication
from nyaatriggers.triggevent_bridge import TriggeventBridge
from nyaatriggers.ws_client import WSClient
from nyaatriggers.ui.engines import EnginesMixin

app = QApplication.instance() or QApplication([])


class Host(QObject):
    _ensure_triggevent_bridge = EnginesMixin._ensure_triggevent_bridge

    def __init__(self, ws):
        super().__init__()
        self._ws = ws
        self._connected = False
        self._triggevent = None
        self._triggevent_disabled = set()
        self._find_iinact_log_dir = lambda: None
        for name in (
            '_on_triggevent_callout', '_on_triggevent_tts', '_on_triggevent_inventory',
            '_receive_native_automark_inventory', '_on_telesto_status',
            '_on_engine_sidecar_status', '_on_engine_chain_failure',
            '_on_custom_triggevent_ready', '_on_native_automarkers_ready',
            '_on_custom_triggevent_status', '_on_triggevent_combatants_request',
            '_apply_engine_overrides',
        ):
            setattr(self, name, Mock())
        ws.status_changed.connect(self.connection)

    def connection(self, connected, message):
        self._connected = connected


def wait(predicate, seconds=15):
    end = time.monotonic() + seconds
    while not predicate() and time.monotonic() < end:
        QTest.qWait(10)
    assert predicate(), 'bounded condition failed'

def check(connection_first):
    with ExitStack() as stack:
        home = stack.enter_context(tempfile.TemporaryDirectory(prefix='nyaa-round2-engines-'))
        stack.enter_context(patch.dict(os.environ, {'JAVA_TOOL_OPTIONS': f'-Duser.home={home}', 'NYAA_AUTOMARK': '0'}))
        stack.enter_context(patch('nyaatriggers.diagnostics._LOG_FILE', Path(home) / 'diagnostics.log'))
        stack.enter_context(patch('nyaatriggers.triggevent_bridge._log'))
        stack.enter_context(patch('nyaatriggers.triggevent_recovery._log'))
        ws = WSClient()
        host = Host(ws)
        if not connection_first:
            host._ensure_triggevent_bridge()
        def stop_bridge():
            bridge = host._triggevent
            bridge.stop(wait=True)
            for worker in (bridge._reader, bridge._writer, bridge._errpump):
                if worker is not None:
                    worker.join(timeout=8)
                    assert not worker.is_alive()
        stack.callback(stop_bridge)
        server = QWebSocketServer('late recovery', QWebSocketServer.SslMode.NonSecureMode)
        assert server.listen(QHostAddress('127.0.0.1'), 0)
        stack.callback(server.close)
        peers, connections, speech = [], [], []
        ws.status_changed.connect(lambda connected, message: connections.append(connected))
        def accept():
            peer = server.nextPendingConnection()
            peers.append(peer)
        server.newConnection.connect(accept)
        stack.callback(ws.disconnect_from)
        ws.connect_to(f'ws://127.0.0.1:{server.serverPort()}/ws')
        wait(lambda: connections == [True] and len(peers) == 1)
        assert ws._ws.state() == QAbstractSocket.SocketState.ConnectedState
        bridge = host._ensure_triggevent_bridge()
        bridge.tts.connect(lambda text, generation: speech.append((text, generation)))
        recovery = host._triggevent_recovery
        stack.callback(recovery._progress_timer.stop)
        stack.callback(recovery._retry_timer.stop)
        def send(payload):
            peers[0].sendTextMessage(json.dumps(payload))
        send({'type': 'ChangeZone', 'zoneID': 0x553, 'zoneName': 'Raid'})
        send({'type': 'ChangePrimaryPlayer', 'charID': 0x10000001})
        send({'type': 'LogLine', 'rawLine': '00|' + datetime.now().astimezone().isoformat() + '|0038|Player|Anchor|0'})
        bridge.start()
        wait(lambda: recovery._live)
        generation = bridge.generation()
        os.killpg(bridge._proc.pid, signal.SIGKILL)
        wait(lambda: not bridge.is_active() and not recovery._ready)
        marker = {'type': 'LogLine', 'rawLine': '00|' + datetime.now().astimezone().isoformat() + '|0038|Player|AFTER_EXIT_DISTINCTIVE|0'}
        send(marker)
        wait(lambda: any('AFTER_EXIT_DISTINCTIVE' in raw for raw in recovery._pending))
        observed = []
        observer = QTimer()
        observer.setInterval(10)
        observer.timeout.connect(lambda: observed.append((bridge.is_active(), bridge.generation(), recovery._retry_timer.isActive())))
        observer.start()
        stack.callback(observer.stop)
        wait(lambda: bridge.is_active() and recovery._live and bridge.generation() == generation + 1)
        assert len(peers) == 1 and connections == [True], connections
        assert any(row[2] for row in observed), observed
        line = '20|' + datetime.now().astimezone().isoformat() + '|40000001|Boss|C622|Light of Judgment|10000001|Player|5|100|100|0|0|0'
        send({'type': 'LogLine', 'rawLine': line})
        send({'type': 'LogLine', 'rawLine': line})
        wait(lambda: len(speech) == 2)
        assert speech[0][0] == speech[1][0] and all(row[1] == generation + 1 for row in speech), speech
        for peer in peers:
            peer.abort()

@unittest.skipUnless(os.name == "posix" and TriggeventBridge.is_available(),
                     "Requires POSIX and the real native engine")
class StartupRecoveryTests(unittest.TestCase):
    def test_connection_before_manager(self):
        check(True)

    def test_manager_before_connection(self):
        check(False)


if __name__ == '__main__':
    unittest.main()
