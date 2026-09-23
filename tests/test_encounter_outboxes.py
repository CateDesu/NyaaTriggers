"""Exercise encounter boundaries while transport workers have pending work."""

import json
from types import SimpleNamespace
import threading
import unittest
from unittest.mock import patch

from nyaatriggers import plugin_link
from nyaatriggers.telesto_client import TelestoClient
from nyaatriggers.ui.connection import ConnectionMixin
from tests.test_overlay_retention import OverlayHost
from tests.test_transport_deadlines import PluginPeer, wait_for


class AlertBoundaries(unittest.TestCase):
    def make_link(self):
        peer = PluginPeer()
        self.addCleanup(peer.close)
        link = plugin_link.PluginLink(port=peer.port)
        self.addCleanup(link.stop)
        return peer, link

    def assert_fresh_only(self, peer):
        self.assertTrue(wait_for(lambda: any(frame.get("text") == "fresh"
                                            for frame in peer.frames)))
        self.assertEqual([frame["text"] for frame in peer.frames
                          if frame.get("c") == "alert"], ["fresh"])

    def test_reconnect_discards_alerts_from_before_clear(self):
        peer, link = self.make_link()
        link.send_alert("old")
        link.send_clear(keep_dps=True)
        link.send_alert("fresh")
        link.start()
        self.assert_fresh_only(peer)

    def test_reconnect_without_local_timelines_preserves_engine_alerts(self):
        peer, link = self.make_link()
        host = OverlayHost([1000.0])
        host._plugin_link = link
        host._update_plugin_link_status_label = lambda *_args: None
        entered, release = threading.Event(), threading.Event()

        def drain(_ws, _stopping):
            entered.set()
            release.wait(3)
            return False

        with patch.object(link, "_drain_inbound", side_effect=drain):
            link.start()
            self.assertTrue(wait_for(link.is_connected))
            link.send_alert("engine callout")
            try:
                self.assertTrue(entered.wait(2))
                ConnectionMixin._on_plugin_link_status(host, *link.last_status())
            finally:
                release.set()
            self.assertTrue(wait_for(lambda: any(frame["c"] in ("timeline", "clear")
                                                for frame in peer.frames)))
            visible = []
            for frame in peer.frames:
                if frame["c"] == "alert":
                    visible.append(frame["text"])
                elif frame["c"] == "clear":
                    visible.clear()
            self.assertEqual(visible, ["engine callout"])

    def test_clear_cancels_an_alert_already_dequeued(self):
        peer, link = self.make_link()
        entered, release = threading.Event(), threading.Event()

        def drain(_ws, _stopping):
            entered.set()
            release.wait(3)
            return False

        with patch.object(link, "_drain_inbound", side_effect=drain):
            link.start()
            self.assertTrue(wait_for(link.is_connected))
            link.send_alert("old")
            try:
                self.assertTrue(entered.wait(2))
                link.send_clear(keep_dps=True)
                link.send_alert("fresh")
            finally:
                release.set()
            self.assert_fresh_only(peer)

    def test_failed_alert_is_not_retried_after_clear(self):
        peer, link = self.make_link()
        entered, release = threading.Event(), threading.Event()
        send = link._send
        failed = False

        def fail_old(ws, message):
            nonlocal failed
            if message.get("text") == "old" and not failed:
                failed = True
                entered.set()
                release.wait(3)
                raise OSError("send interrupted")
            send(ws, message)

        with patch.object(link, "_send", side_effect=fail_old):
            link.start()
            self.assertTrue(wait_for(link.is_connected))
            link.send_alert("old")
            try:
                self.assertTrue(entered.wait(2))
                link.send_clear()
                link.send_alert("fresh")
            finally:
                release.set()
            self.assert_fresh_only(peer)
            self.assertFalse(wait_for(lambda: any(frame.get("text") == "old"
                                                  for frame in peer.frames), timeout=.2))

    def test_full_alert_queue_accepts_clear_and_preserves_final_dps(self):
        link = plugin_link.PluginLink()
        for _ in range(plugin_link.OUTBOX_CAPACITY):
            link.send_alert("old")
        link.send_clear(keep_dps=True)
        link.send_dps({}, [], show=False)
        link.send_alert("fresh")
        frames = list(link._queue.queue)
        self.assertEqual([frame["c"] for frame in frames], ["clear", "dps", "alert"])
        self.assertEqual(json.loads(json.dumps(frames[-1]))["text"], "fresh")

    def test_clear_preserves_the_final_dps_already_queued(self):
        link = plugin_link.PluginLink()
        link.send_dps({}, [], show=False)
        link.send_alert("old")
        link.send_clear(keep_dps=True)
        frames = list(link._queue.queue)
        self.assertEqual([frame["c"] for frame in frames], ["dps", "clear"])
        self.assertIs(frames[0]["show"], False)
        self.assertIs(frames[1]["keepDps"], True)


class MarkerBoundaries(unittest.TestCase):
    def test_repeated_wipes_preserve_clears_until_the_party_changes(self):
        for clear_party in (False, True):
            with self.subTest(clear_party=clear_party):
                client = TelestoClient(enabled=True, delay_base_ms=0, delay_plus_ms=0)
                client._update_party_slots(b'{"response":[{"order":"1","actor":"10000001"}]}')
                entered, release = threading.Event(), threading.Event()
                sent = []

                def delay(_stopping):
                    entered.set()
                    release.wait(3)

                with patch.object(client, "_sleep_command_delay", side_effect=delay), \
                        patch.object(client, "_read_response", side_effect=lambda req, _timeout:
                                     (sent.append(json.loads(req.data)) or (200, b'{}'))):
                    client.start()
                    try:
                        client.clear_actor("10000001", force=True)
                        self.assertTrue(entered.wait(2))
                        client.clear_self(force=True)
                        client.mark_self("attack1", force=True)
                        client.cancel_pending(clear_party=clear_party)
                        client.cancel_pending(clear_party=clear_party)
                        client.mark_self("attack3")
                        release.set()
                        self.assertTrue(wait_for(lambda: any(msg["payload"]["command"]
                                                             == "/mk attack3 <me>" for msg in sent)))
                        expected = [] if clear_party else ["/mk clear <1>", "/mk clear <me>"]
                        self.assertEqual([msg["payload"]["command"] for msg in sent],
                                         expected + ["/mk attack3 <me>"])
                        self.assertEqual(client.slot_of_actor("10000001"), None if clear_party else 1)
                    finally:
                        release.set()
                        client.stop()

    def test_old_party_response_cannot_restore_cleared_actor_slots(self):
        client = TelestoClient(enabled=True, delay_base_ms=0, delay_plus_ms=0)
        entered, release = threading.Event(), threading.Event()
        next_entered, next_release = threading.Event(), threading.Event()
        responses = iter((b'{"response":[{"order":"1","actor":"10000001"}]}',
                          b'{"response":[{"order":"1","actor":"10000002"}]}'))

        def response(_request, _timeout):
            if entered.is_set():
                next_entered.set()
                next_release.wait(3)
            else:
                entered.set()
                release.wait(3)
            return 200, next(responses)

        with patch.object(client, "_read_response", side_effect=response):
            client.start()
            try:
                client.ping()
                self.assertTrue(entered.wait(2))
                client.cancel_pending(clear_party=True)
                client.ping()
                release.set()
                self.assertTrue(next_entered.wait(2))
                self.assertIsNone(client.slot_of_actor("10000001"))
                next_release.set()
                self.assertTrue(wait_for(lambda: client.slot_of_actor("10000002") == 1))
                self.assertIsNone(client.slot_of_actor("10000001"))
            finally:
                release.set()
                next_release.set()
                client.stop()

    def test_wipe_zone_and_feed_loss_cancel_delayed_and_queued_marks(self):
        for boundary in ("wipe", "zone", "feed loss"):
            with self.subTest(boundary=boundary):
                client = TelestoClient(enabled=True, delay_base_ms=0, delay_plus_ms=0)
                host = OverlayHost([1000.0])
                host.prepare_zone()
                host._telesto_client = client
                host._status_lbl = SimpleNamespace(setText=lambda _: None,
                                                   setStyleSheet=lambda _: None)
                host._conn_btn = SimpleNamespace(setText=lambda _: None)
                host._zone_banner_text = lambda: ""
                host._umad_chain_reset = lambda clear_marks=False: (
                    client.clear_self(force=True) if clear_marks else None)
                entered, release = threading.Event(), threading.Event()
                sent = []

                def delay(_stopping):
                    entered.set()
                    release.wait(3)

                with patch.object(client, "_sleep_command_delay", side_effect=delay), \
                        patch.object(client, "_post", side_effect=lambda msg: sent.append(msg)):
                    client.start()
                    try:
                        client.mark_self("attack1")
                        self.assertTrue(entered.wait(2))
                        client.mark_self("attack2")
                        if boundary == "wipe":
                            host.wipe()
                        elif boundary == "zone":
                            host.raw_zone()
                        else:
                            host._on_status_changed(False, "Disconnected")
                        client.mark_self("attack3")
                        release.set()
                        self.assertTrue(wait_for(lambda: any(msg.get("payload", {}).get("command")
                                                             == "/mk attack3 <me>" for msg in sent)))
                        commands = [msg["payload"]["command"] for msg in sent]
                        self.assertNotIn("/mk attack1 <me>", commands)
                        self.assertNotIn("/mk attack2 <me>", commands)
                        if boundary == "wipe":
                            self.assertEqual(commands, ["/mk clear <me>", "/mk attack3 <me>"])
                    finally:
                        release.set()
                        client.stop()


if __name__ == "__main__":
    unittest.main()
