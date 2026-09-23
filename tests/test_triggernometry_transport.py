"""Use slow local peers to challenge relay deadlines and shutdown."""

import threading
import unittest
from unittest.mock import patch

from nyaatriggers import triggernometry_telesto
from nyaatriggers.triggernometry_telesto import TriggernometryTelesto
from tests.test_transport_deadlines import HttpPeer, wait_for
from tests.test_triggernometry_telesto import FakeTelesto, envelope, post


class RelayBoundaries(unittest.TestCase):
    def test_trickling_replies_do_not_block_later_requests(self):
        for mode in ("body", "headers", "redirect"):
            with self.subTest(mode=mode):
                peer = HttpPeer(mode)
                relay = TriggernometryTelesto(lambda _: None, lambda _: None, peer.uri)
                relay.start()
                first = threading.Thread(target=relay.forward,
                                         args=(envelope("GetPartyMembers"),), daemon=True)
                second = threading.Thread(target=relay.forward,
                                          args=(envelope("GetPartyMembers"),), daemon=True)
                try:
                    first.start()
                    self.assertTrue(peer.started.wait(2))
                    second.start()
                    self.assertTrue(wait_for(lambda: len(peer.requests) >= 2, timeout=5.2),
                                    "A trickling response blocked the next request past four seconds")
                    second.join(1)
                    self.assertFalse(second.is_alive())
                finally:
                    peer.release.set()
                    first.join(2)
                    if second.ident:
                        second.join(2)
                    relay.close(wait=True)
                    peer.close()

    def test_close_interrupts_a_request_and_cleans_its_resources(self):
        for mode in ("body", "headers"):
            with self.subTest(mode=mode):
                peer = HttpPeer(mode)
                relay = TriggernometryTelesto(lambda _: None, lambda _: None, peer.uri)
                relay.start()
                worker = threading.Thread(target=relay.forward, args=(envelope(
                    "Subscribe", id="memory", type="memory", start="1", length="1"),), daemon=True)
                try:
                    worker.start()
                    self.assertTrue(peer.started.wait(2))
                    relay.close()
                    self.assertTrue(wait_for(relay.is_finished, timeout=1),
                                    "Shutdown remained blocked on the abandoned response")
                    worker.join(1)
                    self.assertFalse(worker.is_alive())
                    self.assertEqual(peer.requests[-1]["payload"][0]["type"], "Unsubscribe")
                finally:
                    peer.release.set()
                    worker.join(2)
                    relay.close(wait=True)
                    peer.close()

    def test_drawing_replacement_at_capacity_keeps_the_limit(self):
        with FakeTelesto() as peer, patch.object(triggernometry_telesto, "MAX_RESOURCES", 2):
            relay = TriggernometryTelesto(lambda _: None, lambda _: None, peer.url)
            relay.start()
            try:
                for name in ("one", "two"):
                    self.assertEqual(post(relay.url, envelope("EnableDoodle", name=name,
                                                              type="circle", radius="1"))[0], 200)
                self.assertEqual(post(relay.url, envelope("EnableDoodle", name="one",
                                                          type="circle", radius="2"))[0], 200)
                self.assertEqual(post(relay.url, envelope("EnableDoodle", name="three",
                                                          type="circle", radius="1"))[0], 400)
                self.assertEqual(len(peer.drawings), 2)
                self.assertEqual(peer.drawings[relay.prefix + "one"]["radius"], "2")
            finally:
                relay.close(wait=True)
            self.assertFalse(peer.drawings)

    def test_rejected_drawing_replacement_keeps_old_expiry_callback(self):
        received = []
        with FakeTelesto() as peer:
            relay = TriggernometryTelesto(received.append, self.fail, peer.url)
            relay.start()
            try:
                request = envelope("EnableDoodle", name="circle", type="circle",
                                   radius="1", notifyonexpiry=True)
                self.assertEqual(post(relay.url, request)[0], 200)
                old = peer.next("EnableDoodle")["payload"]
                with patch.object(relay, "_post", return_value=(503, b"")):
                    self.assertEqual(post(relay.url, envelope(
                        "EnableDoodle", name="circle", type="circle",
                        radius="2", notifyonexpiry=True))[0], 503)
                self.assertEqual(post(old["notifyonexpiry"], {
                    "notificationid": old["name"], "notificationtype": "doodleexpired"})[0], 200)
                self.assertEqual(len(received), 1)
                self.assertFalse(relay._drawings)
            finally:
                relay.close(wait=True)

    def test_rejected_new_drawing_does_not_consume_capacity(self):
        with FakeTelesto() as peer, patch.object(triggernometry_telesto, "MAX_RESOURCES", 1):
            relay = TriggernometryTelesto(lambda _: None, lambda _: None, peer.url)
            relay.start()
            try:
                with patch.object(relay, "_post", return_value=(503, b"")):
                    self.assertEqual(post(relay.url, envelope(
                        "EnableDoodle", name="failed", type="circle", radius="1"))[0], 503)
                self.assertEqual(post(relay.url, envelope(
                    "EnableDoodle", name="working", type="circle", radius="1"))[0], 200)
                self.assertIn(relay.prefix + "working", peer.drawings)
                self.assertNotIn(relay.prefix + "failed", peer.drawings)
                self.assertEqual(len(relay._owned_drawings), 1)
            finally:
                relay.close(wait=True)

    def test_rejected_drawing_disable_keeps_old_expiry_callback(self):
        received = []
        with FakeTelesto() as peer:
            relay = TriggernometryTelesto(received.append, self.fail, peer.url)
            relay.start()
            try:
                self.assertEqual(post(relay.url, envelope(
                    "EnableDoodle", name="circle", type="circle",
                    notifyonexpiry=True))[0], 200)
                old = peer.next("EnableDoodle")["payload"]
                with patch.object(relay, "_post", return_value=(503, b"")):
                    self.assertEqual(post(relay.url, envelope(
                        "DisableDoodle", name="circle"))[0], 503)
                self.assertEqual(post(old["notifyonexpiry"], {
                    "notificationid": old["name"], "notificationtype": "doodleexpired"})[0], 200)
                self.assertEqual(len(received), 1)
                self.assertFalse(relay._drawings)
            finally:
                relay.close(wait=True)

    def test_old_drawing_expiry_survives_a_pending_replacement(self):
        received = []
        with FakeTelesto() as peer:
            relay = TriggernometryTelesto(received.append, self.fail, peer.url)
            relay.start()
            try:
                self.assertEqual(post(relay.url, envelope(
                    "EnableDoodle", name="circle", type="circle",
                    radius="1", notifyonexpiry=True))[0], 200)
                old = peer.next("EnableDoodle")["payload"]
                entered = threading.Event()
                release = threading.Event()
                result = []
                original_post = relay._post

                def delayed(message):
                    entered.set()
                    release.wait(2)
                    return original_post(message)

                with patch.object(relay, "_post", side_effect=delayed):
                    worker = threading.Thread(target=lambda: result.append(post(
                        relay.url, envelope("EnableDoodle", name="circle", type="circle",
                                            radius="2", notifyonexpiry=True))), daemon=True)
                    worker.start()
                    try:
                        self.assertTrue(entered.wait(2))
                        peer.drawings.pop(old["name"])
                        self.assertEqual(post(old["notifyonexpiry"], {
                            "notificationid": old["name"],
                            "notificationtype": "doodleexpired"})[0], 200)
                        self.assertEqual(post(old["notifyonexpiry"], {
                            "notificationid": old["name"],
                            "notificationtype": "doodleexpired"})[0], 410)
                    finally:
                        release.set()
                        worker.join(2)
                self.assertEqual(result[0][0], 200)
                self.assertEqual(len(received), 1)
                new = peer.next("EnableDoodle")["payload"]
                self.assertEqual(post(new["notifyonexpiry"], {
                    "notificationid": new["name"],
                    "notificationtype": "doodleexpired"})[0], 200)
                self.assertEqual(len(received), 2)
            finally:
                relay.close(wait=True)

    def test_failed_pending_drawing_does_not_resurrect_an_expired_one(self):
        received = []
        with FakeTelesto() as peer:
            relay = TriggernometryTelesto(received.append, self.fail, peer.url)
            relay.start()
            try:
                self.assertEqual(post(relay.url, envelope(
                    "EnableDoodle", name="circle", type="circle",
                    notifyonexpiry=True))[0], 200)
                old = peer.next("EnableDoodle")["payload"]
                entered = threading.Event()
                release = threading.Event()
                result = []

                def delayed(_message):
                    entered.set()
                    release.wait(2)
                    return 503, b""

                with patch.object(relay, "_post", side_effect=delayed):
                    worker = threading.Thread(target=lambda: result.append(post(
                        relay.url, envelope("EnableDoodle", name="circle", type="circle",
                                            notifyonexpiry=True))), daemon=True)
                    worker.start()
                    try:
                        self.assertTrue(entered.wait(2))
                        peer.drawings.pop(old["name"])
                        self.assertEqual(post(old["notifyonexpiry"], {
                            "notificationid": old["name"],
                            "notificationtype": "doodleexpired"})[0], 200)
                    finally:
                        release.set()
                        worker.join(2)
                self.assertEqual(result[0][0], 503)
                self.assertEqual(len(received), 1)
                self.assertFalse(relay._drawings)
                self.assertEqual(post(relay.url, envelope("GetPartyMembers"))[0], 200)
                self.assertFalse(relay._owned_drawings)
            finally:
                relay.close(wait=True)

    def test_failed_replacement_does_not_resurrect_a_newly_expired_drawing(self):
        received = []
        with FakeTelesto() as peer:
            relay = TriggernometryTelesto(received.append, self.fail, peer.url)
            relay.start()
            try:
                self.assertEqual(post(relay.url, envelope(
                    "EnableDoodle", name="circle", type="circle",
                    notifyonexpiry=True))[0], 200)
                old = peer.next("EnableDoodle")["payload"]
                expiry_results = []

                def partial(message):
                    peer.apply(message)
                    new = message["payload"]
                    peer.drawings.pop(new["name"])
                    expiry_results.append(post(new["notifyonexpiry"], {
                        "notificationid": new["name"],
                        "notificationtype": "doodleexpired"})[0])
                    return 503, b""

                with patch.object(relay, "_post", side_effect=partial):
                    self.assertEqual(post(relay.url, envelope(
                        "EnableDoodle", name="circle", type="circle",
                        notifyonexpiry=True))[0], 503)
                self.assertEqual(expiry_results, [200])
                self.assertEqual(len(received), 1)
                self.assertFalse(relay._drawings)
                self.assertEqual(post(old["notifyonexpiry"], {
                    "notificationid": old["name"],
                    "notificationtype": "doodleexpired"})[0], 410)
                self.assertEqual(post(relay.url, envelope("GetPartyMembers"))[0], 200)
                self.assertFalse(relay._owned_drawings)
            finally:
                relay.close(wait=True)

    def test_subscription_replacement_at_capacity_keeps_the_limit(self):
        with FakeTelesto() as peer, patch.object(triggernometry_telesto, "MAX_RESOURCES", 2):
            relay = TriggernometryTelesto(lambda _: None, lambda _: None, peer.url)
            relay.start()
            try:
                for name in ("one", "two"):
                    self.assertEqual(post(relay.url, envelope(
                        "Subscribe", id=name, type="memory", start="1", length="1"))[0], 200)
                old = relay._subscriptions["one"]
                self.assertEqual(post(relay.url, envelope(
                    "Subscribe", id="one", type="memory", start="2", length="1"))[0], 200)
                new = relay._subscriptions["one"]
                self.assertNotEqual(new, old)
                self.assertNotIn(old, peer.subscriptions)
                self.assertEqual(peer.subscriptions[new]["start"], "2")
                self.assertEqual(len(relay._owned_subscriptions), 2)
                self.assertEqual(post(relay.url, envelope(
                    "Subscribe", id="three", type="memory", start="1", length="1"))[0], 400)
                self.assertEqual(len(peer.subscriptions), 2)
            finally:
                relay.close(wait=True)
            self.assertFalse(peer.subscriptions)

    def test_rejected_replacement_keeps_the_old_callback_and_capacity(self):
        received = []
        with FakeTelesto() as peer, patch.object(triggernometry_telesto, "MAX_RESOURCES", 2):
            relay = TriggernometryTelesto(received.append, self.fail, peer.url)
            relay.start()
            try:
                for name in ("one", "two"):
                    self.assertEqual(post(relay.url, envelope(
                        "Subscribe", id=name, type="memory", start="1", length="1"))[0], 200)
                old = relay._subscriptions["one"]
                with patch.object(relay, "_post", return_value=(503, b"")):
                    self.assertEqual(post(relay.url, envelope(
                        "Subscribe", id="one", type="memory", start="2", length="1"))[0], 503)
                self.assertEqual(post(relay.callback_url, {
                    "notificationid": old, "notificationtype": "memory"})[0], 200)
                self.assertEqual(len(received), 1)
                original_post = relay._post

                def reject_cleanup(message):
                    if (message.get("type") == "Bundle"
                            and all(item.get("type") == "Unsubscribe"
                                    for item in message["payload"])):
                        return 503, b""
                    return original_post(message)

                with patch.object(relay, "_post", side_effect=reject_cleanup):
                    self.assertEqual(post(relay.url, envelope("GetPartyMembers"))[0], 503)
                self.assertEqual(len(relay._owned_subscriptions), 3)
                self.assertEqual(post(relay.url, envelope(
                    "Subscribe", id="one", type="memory", start="3", length="1"))[0], 200)
                self.assertNotIn(old, peer.subscriptions)
                self.assertEqual(len(relay._owned_subscriptions), 2)
                self.assertEqual(post(relay.url, envelope("Unsubscribe", id="one"))[0], 200)
                self.assertEqual(post(relay.url, envelope(
                    "Subscribe", id="three", type="memory", start="1", length="1"))[0], 200)
                self.assertEqual(len(relay._owned_subscriptions), 2)
                self.assertEqual(len(peer.subscriptions), 2)
            finally:
                relay.close(wait=True)
            self.assertFalse(peer.subscriptions)

    def test_old_callback_survives_a_pending_replacement(self):
        received = []
        with FakeTelesto() as peer:
            relay = TriggernometryTelesto(received.append, self.fail, peer.url)
            relay.start()
            try:
                self.assertEqual(post(relay.url, envelope(
                    "Subscribe", id="one", type="memory", start="1", length="1"))[0], 200)
                old = relay._subscriptions["one"]
                entered = threading.Event()
                release = threading.Event()
                result = []

                def delayed(_message):
                    entered.set()
                    release.wait(2)
                    return 503, b""

                with patch.object(relay, "_post", side_effect=delayed):
                    worker = threading.Thread(target=lambda: result.append(post(
                        relay.url, envelope("Subscribe", id="one", type="memory",
                                            start="2", length="1"))), daemon=True)
                    worker.start()
                    try:
                        self.assertTrue(entered.wait(2))
                        self.assertEqual(post(relay.callback_url, {
                            "notificationid": old, "notificationtype": "memory"})[0], 200)
                    finally:
                        release.set()
                        worker.join(2)
                self.assertEqual(result[0][0], 503)
                self.assertEqual(len(received), 1)
            finally:
                relay.close(wait=True)


if __name__ == "__main__":
    unittest.main()
