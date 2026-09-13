"""Exercise local HTTP routing and feed recovery against real peers."""

import base64
import hashlib
import http.server
import json
import os
import socket
import struct
import threading
import time
import unittest
from unittest.mock import patch
import urllib.request

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication

from nyaatriggers.telesto_client import TelestoClient, _is_loopback_uri
from nyaatriggers import ws_client

_app = QApplication.instance() or QApplication([])


def wait_for(predicate, timeout=4):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        QTest.qWait(10)
    return bool(predicate())


class FeedPeer:
    def __init__(self, reply=True):
        self.reply = reply
        self.messages = []
        self.pings = []
        self.connections = 0
        self.errors = []
        self.stop = threading.Event()
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen()
        self.sock.settimeout(.1)
        self.url = f"ws://127.0.0.1:{self.sock.getsockname()[1]}/ws"
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def run(self):
        while not self.stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            self.connections += 1
            try:
                with conn:
                    conn.settimeout(.1)
                    self.handle(conn)
            except (ConnectionError, OSError):
                pass
            except Exception as exc:
                self.errors.append(repr(exc))

    def read(self, conn, size):
        data = bytearray()
        while len(data) < size and not self.stop.is_set():
            try:
                part = conn.recv(size - len(data))
            except socket.timeout:
                continue
            if not part:
                raise ConnectionError("peer closed")
            data.extend(part)
        if len(data) < size:
            raise ConnectionError("test stopped")
        return bytes(data)

    def handle(self, conn):
        header = bytearray()
        while not header.endswith(b"\r\n\r\n"):
            header.extend(self.read(conn, 1))
        key = next(line.split(b":", 1)[1].strip() for line in header.split(b"\r\n")
                   if line.lower().startswith(b"sec-websocket-key:"))
        accept = base64.b64encode(hashlib.sha1(
            key + b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11").digest())
        conn.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                     b"Connection: Upgrade\r\nSec-WebSocket-Accept: " + accept + b"\r\n\r\n")
        while not self.stop.is_set():
            head = self.read(conn, 2)
            opcode, length = head[0] & 15, head[1] & 127
            if length == 126:
                length = struct.unpack("!H", self.read(conn, 2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self.read(conn, 8))[0]
            mask = self.read(conn, 4) if head[1] & 128 else b"\0" * 4
            payload = bytes(v ^ mask[i % 4] for i, v in enumerate(self.read(conn, length)))
            if opcode == 8:
                conn.sendall(bytes((0x88, len(payload))) + payload)
                return
            if opcode == 9:
                self.pings.append(payload)
                if self.reply:
                    conn.sendall(bytes((0x8a, len(payload))) + payload)
            elif opcode == 1:
                self.messages.append(json.loads(payload))

    def close(self):
        self.stop.set()
        self.sock.close()
        self.thread.join(timeout=2)


class ConnectionRecoveryTests(unittest.TestCase):
    def test_loopback_posts_bypass_proxy_and_remote_posts_keep_it(self):
        seen = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                seen.append((self.server.role, self.path, json.loads(body)))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"response": []}')

            def log_message(self, *_args):
                pass

        servers = []
        try:
            for role in ("target", "proxy"):
                server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
                server.role = role
                threading.Thread(target=server.serve_forever, daemon=True).start()
                servers.append(server)
            target, proxy = servers
            env = {"http_proxy": f"http://127.0.0.1:{proxy.server_port}"}
            with patch.dict(os.environ, env, clear=True), patch.object(urllib.request, "_opener", None):
                client = TelestoClient(timeout=1)
                for host, expected in (("localhost", "target"), ("127.0.0.1", "target"),
                                       ("telesto.invalid", "proxy"), ("127.0.0.1", "target")):
                    client.configure(uri=f"http://{host}:{target.server_port}/")
                    client._post({"id": 1, "type": "GetPartyMembers", "payload": {}})
                    self.assertEqual(seen[-1][0], expected)
                    self.assertTrue(client._reachable)
                urllib.request.urlopen(f"http://telesto.invalid:{target.server_port}/",
                                       data=b'{}', timeout=1).close()
                self.assertEqual(seen[-1][0], "proxy")
        finally:
            for server in servers:
                server.shutdown()
                server.server_close()

    def test_loopback_host_classification_does_not_match_suffixes(self):
        for host in ("localhost", "LOCALHOST.", "127.0.0.2", "[::1]", "[::ffff:127.0.0.1]"):
            self.assertTrue(_is_loopback_uri(f"http://{host}:45678/"), host)
        for host in ("localhost.example.com", "127.0.0.1.example.com", "192.168.1.5"):
            self.assertFalse(_is_loopback_uri(f"http://{host}:45678/"), host)

    def make_feed(self, reply):
        peer = FeedPeer(reply)
        self.addCleanup(peer.close)
        client = ws_client.WSClient()
        self.addCleanup(client.disconnect_from)
        status = []
        client.status_changed.connect(lambda *args: status.append(args))
        client.connect_to(peer.url)
        self.assertTrue(wait_for(lambda: status and status[-1][0]))
        return peer, client, status

    def test_quiet_healthy_feed_answers_pings_without_reconnect(self):
        with patch.object(ws_client, "_PING_INTERVAL_MS", 80), \
             patch.object(ws_client, "_PONG_TIMEOUT_MS", 400):
            peer, client, status = self.make_feed(True)
            self.assertTrue(wait_for(lambda: len(peer.pings) >= 3))
            self.assertEqual(peer.connections, 1)
            self.assertEqual(status, [(True, "Connected")])
            self.assertFalse(client._reconnect_timer.isActive())
            self.assertEqual(peer.errors, [])
            self.assertNotEqual(peer.pings[0], peer.pings[1])

    def test_unanswered_ping_reconnects_even_with_polling(self):
        for polling in (False, True):
            with self.subTest(polling=polling), \
                 patch.object(ws_client, "_PING_INTERVAL_MS", 80), \
                 patch.object(ws_client, "_PONG_TIMEOUT_MS", 250):
                peer, client, status = self.make_feed(False)
                client.set_combatant_polling(polling)
                client._state_cache["changezone"] = "stale state"
                self.assertTrue(wait_for(lambda: bool(peer.pings)))
                client._on_pong(1, b"wrong pong")
                self.assertTrue(wait_for(lambda: any(not ok for ok, _ in status)))
                self.assertIn((False, "Connection timed out"), status)
                self.assertTrue(client._reconnect_timer.isActive())
                self.assertFalse(client._heartbeat_timer.isActive())
                self.assertFalse(client._poll_timer.isActive())
                self.assertEqual(client._state_cache, {})
                peer.reply = True
                client._reconnect_timer.start(1)
                self.assertTrue(wait_for(lambda: peer.connections == 2 and status[-1][0]))
                self.assertTrue(wait_for(lambda: len(peer.pings) >= 2))
                QTest.qWait(300)
                self.assertEqual(peer.connections, 2)
                self.assertTrue(status[-1][0])
                self.assertEqual(peer.errors, [])
                client.disconnect_from()
                peer.close()

    def test_user_disconnect_cancels_pending_probe(self):
        with patch.object(ws_client, "_PING_INTERVAL_MS", 80), \
             patch.object(ws_client, "_PONG_TIMEOUT_MS", 200):
            peer, client, status = self.make_feed(False)
            self.assertTrue(wait_for(lambda: bool(peer.pings)))
            old_ping = peer.pings[-1]
            client.disconnect_from()
            client._on_pong(1, old_ping)
            QTest.qWait(350)
            self.assertFalse(client._heartbeat_timer.isActive())
            self.assertFalse(client._reconnect_timer.isActive())
            self.assertEqual(peer.connections, 1)
            self.assertNotIn((False, "Connection timed out"), status)

    def test_retarget_discards_the_previous_connections_pong(self):
        with patch.object(ws_client, "_PING_INTERVAL_MS", 80), \
             patch.object(ws_client, "_PONG_TIMEOUT_MS", 300):
            old_peer, client, status = self.make_feed(False)
            self.assertTrue(wait_for(lambda: bool(old_peer.pings)))
            old_ping = old_peer.pings[-1]
            new_peer = FeedPeer(True)
            self.addCleanup(new_peer.close)
            client.connect_to(new_peer.url)
            client._on_pong(1, old_ping)
            self.assertTrue(wait_for(lambda: len(new_peer.pings) >= 2))
            client._on_pong(1, old_ping)
            QTest.qWait(350)
            self.assertTrue(status[-1][0])
            self.assertEqual(old_peer.connections, 1)
            self.assertEqual(new_peer.connections, 1)
            self.assertNotIn((False, "Connection timed out"), status)
            self.assertEqual(new_peer.errors, [])


if __name__ == "__main__":
    unittest.main()
