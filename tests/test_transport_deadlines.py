"""Real local peers for HTTP deadlines and plugin reply checks."""

import json
import os
from pathlib import Path
import shutil
import ssl
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt6.QtCore import QCoreApplication
from websockets.sync.server import serve

from nyaatriggers import plugin_link
from nyaatriggers.telesto_client import TelestoClient

APP = QCoreApplication.instance() or QCoreApplication([])


def wait_for(predicate, timeout=4):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


class HttpPeer:
    def __init__(self, mode, tls=False):
        self.mode = mode
        self.requests = []
        self.started = threading.Event()
        self.release = threading.Event()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                data = json.loads(body) if body else None
                outer.requests.append(data)
                if len(outer.requests) != 1:
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b'{"response":[]}')
                    return
                try:
                    if outer.mode == "headers":
                        self.wfile.write(b"HTTP/1.1 200 OK\r\nX-Audit: ")
                    else:
                        self.send_response(302 if outer.mode == "redirect" else 200)
                        if outer.mode == "redirect":
                            self.send_header("Location", "/redirected")
                        self.send_header("Content-Length", "1000000")
                        self.end_headers()
                    outer.started.set()
                    while not outer.release.wait(0.03):
                        if outer.mode != "silent":
                            self.wfile.write(b" ")
                            self.wfile.flush()
                except OSError:
                    pass

            do_GET = do_POST

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.certificate_dir = None
        if tls:
            self.certificate_dir = tempfile.TemporaryDirectory()
            folder = Path(self.certificate_dir.name)
            self.certificate = folder / "cert.pem"
            key = folder / "key.pem"
            subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                            "-keyout", str(key), "-out", str(self.certificate),
                            "-subj", "/CN=127.0.0.1", "-addext", "subjectAltName=IP:127.0.0.1",
                            "-days", "1"], check=True, capture_output=True, timeout=10)
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(self.certificate, key)
            self.server.socket = context.wrap_socket(self.server.socket, server_side=True)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.uri = f"{'https' if tls else 'http'}://127.0.0.1:{self.server.server_port}/"

    def close(self):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        if self.certificate_dir is not None:
            self.certificate_dir.cleanup()


class HttpDeadlines(unittest.TestCase):
    def make_client(self, peer, timeout=.3):
        client = TelestoClient(uri=peer.uri, enabled=True, timeout=timeout,
                               delay_base_ms=0, delay_plus_ms=0)
        self.addCleanup(client.stop)
        client.start()
        return client

    def test_trickling_body_and_headers_release_queue_without_retrying_command(self):
        for mode in ("body", "headers", "silent", "redirect"):
            with self.subTest(mode=mode):
                peer = HttpPeer(mode)
                try:
                    client = self.make_client(peer)
                    client.mark_self("attack1")
                    self.assertTrue(peer.started.wait(2))
                    client.ping()
                    self.assertTrue(wait_for(lambda: len(peer.requests) == 2, timeout=1.5))
                    self.assertTrue(wait_for(lambda: client._reachable == (True, False)))
                    commands = [r for r in peer.requests if r["type"] == "ExecuteCommand"]
                    self.assertEqual(len(commands), 1)
                    client.stop()
                    self.assertIsNone(client._thread)
                finally:
                    peer.close()

    def test_stop_interrupts_both_body_and_header_reads(self):
        for mode in ("body", "headers", "redirect"):
            with self.subTest(mode=mode):
                peer = HttpPeer(mode)
                try:
                    client = self.make_client(peer, timeout=5)
                    client.ping()
                    self.assertTrue(peer.started.wait(2))
                    worker = client._thread
                    start = time.monotonic()
                    client.stop(join_timeout=.7)
                    self.assertFalse(worker.is_alive())
                    self.assertLess(time.monotonic() - start, .7)
                    self.assertFalse(any(t.name == "TelestoDeadline" for t in threading.enumerate()))
                finally:
                    peer.close()

    @unittest.skipUnless(shutil.which("openssl"), "TLS fixture needs openssl")
    def test_https_deadlines_and_verified_recovery(self):
        for mode in ("body", "headers"):
            with self.subTest(mode=mode):
                peer = HttpPeer(mode, tls=True)
                try:
                    context = ssl.create_default_context(cafile=peer.certificate)
                    with patch.object(ssl, "_create_default_https_context", return_value=context):
                        client = self.make_client(peer)
                        client.ping()
                        self.assertTrue(peer.started.wait(2))
                        client.ping()
                        self.assertTrue(wait_for(lambda: len(peer.requests) == 2, timeout=1.5))
                        self.assertTrue(wait_for(lambda: client._reachable == (True, False)))
                        client.stop()
                finally:
                    peer.close()

    def test_restart_does_not_inherit_the_old_request_cancellation(self):
        peer = HttpPeer("body")
        try:
            client = self.make_client(peer, timeout=5)
            client.ping()
            self.assertTrue(peer.started.wait(2))
            old_worker = client._thread
            client.stop(join_timeout=.7)
            self.assertFalse(old_worker.is_alive())
            client.start()
            client.ping()
            self.assertTrue(wait_for(lambda: len(peer.requests) == 2))
            self.assertTrue(wait_for(lambda: client._reachable == (True, False)))
            client.stop()
            self.assertFalse(any(t.name == "TelestoDeadline" for t in threading.enumerate()))
        finally:
            peer.close()


class PluginPeer:
    def __init__(self, reply=True):
        self.reply = reply
        self.connections = 0
        self.frames = []
        self.sockets = []
        self.server = serve(self.handle, "127.0.0.1", 0)
        self.port = self.server.socket.getsockname()[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def handle(self, ws):
        self.connections += 1
        self.sockets.append(ws)
        ws.send(json.dumps({"ev": "hello", "protocol": 1, "plugin": "0.2.0"}))
        try:
            for raw in ws:
                msg = json.loads(raw)
                self.frames.append(msg)
                if msg.get("c") == "ping" and self.reply:
                    ws.send(json.dumps({"ev": "pong"}))
        except Exception:
            pass

    def pings(self):
        return sum(frame.get("c") == "ping" for frame in self.frames)

    def close(self):
        for ws in self.sockets:
            ws.close()
        self.server.shutdown()
        self.thread.join(timeout=2)


class PluginDeadlines(unittest.TestCase):
    def setUp(self):
        self.timeout_patch = patch.object(plugin_link, "PONG_TIMEOUT_S", .3)
        self.timeout_patch.start()
        self.addCleanup(self.timeout_patch.stop)

    def make_link(self, peer):
        self.addCleanup(peer.close)
        link = plugin_link.PluginLink(port=peer.port, idle_ping_s=.5)
        self.addCleanup(link.stop)
        link.start()
        self.assertTrue(wait_for(link.is_connected))
        return link

    def test_healthy_peer_stays_connected_through_several_probes(self):
        peer = PluginPeer()
        link = self.make_link(peer)
        self.assertTrue(wait_for(lambda: peer.pings() >= 3))
        self.assertEqual(peer.connections, 1)
        self.assertTrue(link.is_connected())

    def test_unanswered_probe_reconnects_and_recovers(self):
        peer = PluginPeer(reply=False)
        link = self.make_link(peer)
        self.assertTrue(wait_for(lambda: peer.connections >= 2))
        peer.reply = True
        before = peer.pings()
        self.assertTrue(wait_for(lambda: peer.pings() >= before + 3))
        connections = peer.connections
        self.assertTrue(wait_for(lambda: peer.pings() >= before + 5))
        self.assertEqual(peer.connections, connections)
        self.assertTrue(link.is_connected())

    def test_busy_outbox_does_not_hide_missing_replies(self):
        peer = PluginPeer(reply=False)
        link = self.make_link(peer)
        stop = threading.Event()

        def produce():
            while not stop.wait(.005):
                link.send_tick(10)

        producer = threading.Thread(target=produce, daemon=True)
        producer.start()
        try:
            self.assertTrue(wait_for(lambda: peer.connections >= 2))
            self.assertGreaterEqual(peer.pings(), 1)
        finally:
            stop.set()
            producer.join(timeout=2)

    def test_retarget_and_stop_retire_the_pending_probe(self):
        first = PluginPeer(reply=False)
        link = self.make_link(first)
        second = PluginPeer()
        self.addCleanup(second.close)
        self.assertTrue(wait_for(lambda: first.pings() == 1))
        link.set_port(second.port)
        self.assertTrue(wait_for(lambda: second.pings() >= 3))
        self.assertEqual(second.connections, 1)
        self.assertTrue(link.is_connected())
        worker = link._thread
        link.stop()
        self.assertFalse(worker.is_alive())
        self.assertFalse(link.is_connected())

    def test_ping_send_failure_does_not_repeat_an_already_sent_alert(self):
        peer = PluginPeer()
        link = self.make_link(peer)
        send = link._send
        failed = []

        def send_with_ping_failure(ws, msg):
            if msg["c"] == "ping" and not failed:
                failed.append(True)
                raise OSError("probe send failed")
            send(ws, msg)
            if msg["c"] == "alert":
                time.sleep(.6)

        link._send = send_with_ping_failure
        link.send_alert("Once")
        self.assertTrue(wait_for(lambda: peer.connections >= 2 and peer.pings() >= 2))
        self.assertEqual([m["text"] for m in peer.frames if m.get("c") == "alert"], ["Once"])


if __name__ == "__main__":
    unittest.main()
