"""Challenge cancellation while connections and outboxes are stalled."""

import http.client
from pathlib import Path
import socketserver
import tempfile
import threading
import unittest
from unittest.mock import patch

from nyaatriggers import plugin_link
from nyaatriggers.telesto_client import TelestoClient
from tests.test_transport_deadlines import HttpPeer, PluginPeer, wait_for
from tests.test_triggernometry_telesto import FakeTelesto


class StalledTlsPeer:
    def __enter__(self):
        self.started = threading.Event()
        self.release = threading.Event()
        owner = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                self.request.recv(4096)
                owner.started.set()
                owner.release.wait(5)

        self.server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": .05}, daemon=True)
        self.thread.start()
        self.uri = f"https://127.0.0.1:{self.server.server_address[1]}/"
        return self

    def __exit__(self, *_args):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)


class CancellationRaces(unittest.TestCase):
    def setUp(self):
        logs = tempfile.TemporaryDirectory()
        self.addCleanup(logs.cleanup)
        patcher = patch("nyaatriggers.drop_log._LOG_FILE", Path(logs.name) / "nyaatriggers.log")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_actor_resolution_cannot_cross_a_party_reset(self):
        for action in ("mark", "clear"):
            with self.subTest(action=action), FakeTelesto() as peer:
                client = TelestoClient(uri=peer.url, enabled=True,
                                       delay_base_ms=0, delay_plus_ms=0)
                client._update_party_slots(b'{"response":[{"order":"1","actor":"10000001"}]}')
                entered, release, cancelled = (threading.Event() for _ in range(3))
                lookup = client.slot_of_actor

                def resolve(actor):
                    slot = lookup(actor)
                    entered.set()
                    release.wait(3)
                    return slot

                def cancel():
                    client.cancel_pending(clear_party=True)
                    cancelled.set()

                mark = lambda: client.mark_actor("10000001", "attack1", force=True)
                clear = lambda: client.clear_actor("10000001", force=True)
                producer = threading.Thread(target=mark if action == "mark" else clear, daemon=True)
                boundary = threading.Thread(target=cancel, daemon=True)
                try:
                    with patch.object(client, "slot_of_actor", side_effect=resolve):
                        producer.start()
                        self.assertTrue(entered.wait(2))
                        boundary.start()
                        cancelled.wait(.5)
                        release.set()
                        producer.join(2)
                        boundary.join(2)
                    self.assertFalse(producer.is_alive())
                    self.assertFalse(boundary.is_alive())
                    self.assertTrue(cancelled.is_set())
                    client.mark_self("attack2")
                    client.start()
                    self.assertTrue(wait_for(lambda: any(
                        item["payload"].get("command") == "/mk attack2 <me>"
                        for item in list(peer.messages.queue))))
                    self.assertEqual([item["payload"]["command"]
                                      for item in list(peer.messages.queue)], ["/mk attack2 <me>"])
                finally:
                    release.set()
                    producer.join(2)
                    if boundary.ident:
                        boundary.join(2)
                    client.stop()

    def test_clear_all_cannot_straddle_a_party_reset(self):
        with FakeTelesto() as peer:
            client = TelestoClient(uri=peer.url, enabled=True,
                                   delay_base_ms=0, delay_plus_ms=0)
            entered, release, cancelled = (threading.Event() for _ in range(3))
            enqueue = client._enqueue

            def queue_command(*args, **kwargs):
                result = enqueue(*args, **kwargs)
                if not entered.is_set():
                    entered.set()
                    release.wait(3)
                return result

            def cancel():
                client.cancel_pending(clear_party=True)
                cancelled.set()

            producer = threading.Thread(target=client.clear_all, kwargs={"force": True}, daemon=True)
            boundary = threading.Thread(target=cancel, daemon=True)
            try:
                with patch.object(client, "_enqueue", side_effect=queue_command):
                    producer.start()
                    self.assertTrue(entered.wait(2))
                    boundary.start()
                    cancelled.wait(.5)
                    release.set()
                    producer.join(2)
                    boundary.join(2)
                self.assertFalse(producer.is_alive())
                self.assertFalse(boundary.is_alive())
                self.assertTrue(cancelled.is_set())
                client.mark_self("attack2")
                client.start()
                self.assertTrue(wait_for(lambda: any(
                    item["payload"].get("command") == "/mk attack2 <me>"
                    for item in list(peer.messages.queue))))
                self.assertEqual([item["payload"]["command"]
                                  for item in list(peer.messages.queue)], ["/mk attack2 <me>"])
            finally:
                release.set()
                producer.join(2)
                if boundary.ident:
                    boundary.join(2)
                client.stop()

    def test_stop_interrupts_a_stalled_tls_handshake(self):
        with StalledTlsPeer() as peer:
            client = TelestoClient(uri=peer.uri, enabled=True, timeout=4)
            try:
                client.start()
                worker = client._thread
                client.ping()
                self.assertTrue(peer.started.wait(2))
                client.stop(join_timeout=.7)
                self.assertFalse(worker.is_alive())
            finally:
                peer.release.set()
                client.stop()

    def test_endpoint_change_interrupts_a_stalled_tls_handshake(self):
        with StalledTlsPeer() as peer, FakeTelesto() as fresh:
            client = TelestoClient(uri=peer.uri, enabled=True, timeout=4,
                                   delay_base_ms=0, delay_plus_ms=0)
            try:
                client.start()
                client.ping()
                self.assertTrue(peer.started.wait(2))
                client.configure(uri=fresh.url)
                client.mark_self("attack2")
                self.assertTrue(wait_for(lambda: not fresh.messages.empty(), timeout=1))
                self.assertEqual(fresh.messages.get_nowait()["payload"]["command"],
                                 "/mk attack2 <me>")
            finally:
                peer.release.set()
                client.stop()

    def test_boundary_cancels_a_mark_still_connecting(self):
        for clear_party in (False, True):
            with self.subTest(clear_party=clear_party), FakeTelesto() as peer:
                client = TelestoClient(uri=peer.url, enabled=True,
                                       delay_base_ms=0, delay_plus_ms=0)
                entered, release = threading.Event(), threading.Event()
                connect = http.client.HTTPConnection.connect

                def delayed_connect(connection):
                    if not entered.is_set():
                        entered.set()
                        release.wait(3)
                    return connect(connection)

                with patch.object(http.client.HTTPConnection, "connect", delayed_connect):
                    client.start()
                    try:
                        client.mark_self("attack1", force=True)
                        self.assertTrue(entered.wait(2))
                        client.cancel_pending(clear_party=clear_party)
                        client.clear_self(force=True)
                        client.mark_self("attack2")
                        release.set()
                        self.assertTrue(wait_for(lambda: any(
                            item["payload"].get("command") == "/mk attack2 <me>"
                            for item in list(peer.messages.queue))))
                        client.stop()
                        commands = [item["payload"]["command"]
                                    for item in list(peer.messages.queue)]
                        self.assertEqual(commands, ["/mk clear <me>", "/mk attack2 <me>"])
                    finally:
                        release.set()
                        client.stop()

    def test_connecting_cleanup_survives_wipes_but_not_party_changes(self):
        for clear_party in (False, True):
            with self.subTest(clear_party=clear_party), FakeTelesto() as peer:
                client = TelestoClient(uri=peer.url, enabled=True,
                                       delay_base_ms=0, delay_plus_ms=0)
                entered, release = threading.Event(), threading.Event()
                connect = http.client.HTTPConnection.connect

                def delayed_connect(connection):
                    if not entered.is_set():
                        entered.set()
                        release.wait(3)
                    return connect(connection)

                with patch.object(http.client.HTTPConnection, "connect", delayed_connect):
                    client.start()
                    try:
                        client.clear_self(force=True)
                        self.assertTrue(entered.wait(2))
                        client.cancel_pending(clear_party=clear_party)
                        client.cancel_pending(clear_party=clear_party)
                        client.mark_self("attack2")
                        release.set()
                        self.assertTrue(wait_for(lambda: any(
                            item["payload"].get("command") == "/mk attack2 <me>"
                            for item in list(peer.messages.queue))))
                        commands = [item["payload"]["command"]
                                    for item in list(peer.messages.queue)]
                        expected = [] if clear_party else ["/mk clear <me>"]
                        self.assertEqual(commands, expected + ["/mk attack2 <me>"])
                    finally:
                        release.set()
                        client.stop()

    def test_boundary_interrupts_stale_response_before_cleanup(self):
        for mode in ("headers", "body", "redirect"):
            with self.subTest(mode=mode):
                peer = HttpPeer(mode)
                client = TelestoClient(uri=peer.uri, enabled=True, timeout=4,
                                       delay_base_ms=0, delay_plus_ms=0)
                try:
                    client.start()
                    client.ping()
                    self.assertTrue(peer.started.wait(2))
                    client.cancel_pending(clear_party=True)
                    client.clear_self(force=True)
                    self.assertTrue(wait_for(lambda: len(peer.requests) >= 2, timeout=1))
                    self.assertEqual(peer.requests[-1]["payload"]["command"], "/mk clear <me>")
                finally:
                    client.stop()
                    peer.close()

    def test_disabling_marks_does_not_report_cancellation_as_connection_failure(self):
        peer = HttpPeer("body")
        client = TelestoClient(uri=peer.uri, enabled=True, timeout=4,
                               delay_base_ms=0, delay_plus_ms=0)
        finished = threading.Event()
        send = client._post

        def post(message):
            try:
                return send(message)
            finally:
                finished.set()

        try:
            client._report_reachable(True, "Connected")
            with patch.object(client, "_post", side_effect=post):
                client.start()
                client.mark_self("attack1")
                self.assertTrue(peer.started.wait(2))
                client.set_enabled(False)
                self.assertTrue(finished.wait(1))
                client.stop()
            self.assertEqual(client.last_status(), (True, False))
        finally:
            client.stop()
            peer.close()

    def test_clear_survives_new_traffic_filling_the_outbox(self):
        for kind in ("alert", "tick"):
            with self.subTest(kind=kind):
                peer = PluginPeer()
                link = plugin_link.PluginLink(port=peer.port)
                entered, release = threading.Event(), threading.Event()
                try:
                    link.start()
                    self.assertTrue(wait_for(link.is_connected))
                    link.send_alert("old")
                    self.assertTrue(wait_for(lambda: any(frame.get("text") == "old"
                                                         for frame in peer.frames)))

                    def drain(_ws, _stopping):
                        if not entered.is_set():
                            entered.set()
                            release.wait(3)
                        return False

                    with patch.object(link, "_drain_inbound", side_effect=drain):
                        link.send_tick(1)
                        self.assertTrue(entered.wait(2))
                        link.send_clear()
                        for index in range(plugin_link.OUTBOX_CAPACITY):
                            if kind == "alert":
                                link.send_alert(f"fresh {index}")
                            else:
                                link.send_tick(index + 2)
                        release.set()
                        self.assertTrue(wait_for(lambda: link._queue.empty()))
                        link.stop()
                    visible = []
                    for frame in peer.frames:
                        if frame["c"] == "clear":
                            visible.clear()
                        elif frame["c"] == "alert":
                            visible.append(frame["text"])
                    self.assertFalse("old" in visible, "The old alert survived the clear")
                    self.assertTrue(any(frame["c"] == "clear" for frame in peer.frames))
                    if kind == "alert":
                        self.assertIn("fresh 0", visible)
                finally:
                    release.set()
                    link.stop()
                    peer.close()

    def test_repeated_clears_keep_the_stronger_dps_reset(self):
        for keep_dps in (False, True):
            with self.subTest(keep_dps=keep_dps):
                link = plugin_link.PluginLink()
                for _ in range(plugin_link.OUTBOX_CAPACITY):
                    link.send_clear(keep_dps=not keep_dps)
                link.send_clear(keep_dps=keep_dps)
                for _ in range(plugin_link.OUTBOX_CAPACITY):
                    link.send_tick(1)
                frames = list(link._queue.queue)
                self.assertTrue(any(frame["c"] == "clear" and not frame["keepDps"]
                                    for frame in frames))

    def test_eviction_cannot_let_a_sender_overtake_a_clear(self):
        link = plugin_link.PluginLink()
        link.send_clear()
        link.send_alert("fresh")
        for _ in range(plugin_link.OUTBOX_CAPACITY - 2):
            link.send_tick(1)
        ready, consumed = threading.Event(), threading.Event()
        first = []
        get = link._queue.get_nowait

        def get_during_eviction():
            frame = get()
            if frame.get("c") == "clear":
                ready.set()
                consumed.wait(2)
            return frame

        def produce():
            try:
                link.send_tick(2)
            finally:
                ready.set()

        def consume():
            if ready.wait(2):
                first.append(link._queue.get(timeout=1))
            consumed.set()

        with patch.object(link._queue, "get_nowait", side_effect=get_during_eviction):
            producer = threading.Thread(target=produce, daemon=True)
            consumer = threading.Thread(target=consume, daemon=True)
            producer.start()
            consumer.start()
            producer.join(3)
            consumer.join(3)
        self.assertFalse(producer.is_alive())
        self.assertFalse(consumer.is_alive())
        self.assertEqual([frame["c"] for frame in first], ["clear"])
        self.assertEqual(link._queue.get_nowait().get("text"), "fresh")


if __name__ == "__main__":
    unittest.main()
