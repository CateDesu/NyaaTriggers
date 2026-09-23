"""Keep automarker actor slots stable when Telesto returns a malformed roster."""

import unittest

from nyaatriggers.telesto_client import TelestoClient


class TelestoPartySlotsTests(unittest.TestCase):
    def test_zero_order_is_an_unknown_party_position(self):
        client = TelestoClient(enabled=True)
        client._update_party_slots(
            b'{"response":[{"order":0,"actor":"10000001"},'
            b'{"order":1,"actor":"10000002"}]}')
        self.assertEqual(client.party_slot_count(), 0)
        self.assertFalse(client.mark_actor("10000002", "attack1"))

    def test_party_order_is_the_slot_even_when_entries_have_gaps(self):
        client = TelestoClient(enabled=True)
        client._update_party_slots(
            b'{"response":[{"order":4,"actor":"10000002"},'
            b'{"order":2,"actor":"10000001"}]}')
        self.assertEqual(client.slot_of_actor("10000001"), 2)
        self.assertEqual(client.slot_of_actor("10000002"), 4)
        self.assertTrue(client.mark_actor("10000001", "attack1"))
        self.assertEqual(client._queue.get_nowait()[0]["payload"]["command"],
                         "/mk attack1 <2>")

    def test_missing_order_keeps_previous_slots_and_marker_target(self):
        client = TelestoClient(enabled=True)
        client._update_party_slots(
            b'{"response":[{"order":"2","actor":"10000001"},'
            b'{"order":"1","actor":"10000002"}]}')
        self.assertEqual(client.slot_of_actor("10000002"), 1)
        self.assertEqual(client.slot_of_actor("10000001"), 2)

        client._update_party_slots(
            b'{"response":[{"order":"2","actor":"10000001"},'
            b'{"actor":"10000002"}]}')
        self.assertEqual(client.slot_of_actor("10000002"), 1)
        self.assertEqual(client.slot_of_actor("10000001"), 2)
        self.assertTrue(client.mark_actor("10000002", "attack1"))
        self.assertEqual(client._queue.get_nowait()[0]["payload"]["command"],
                         "/mk attack1 <1>")

    def test_malformed_first_roster_does_not_create_slots(self):
        client = TelestoClient(enabled=True)
        client._update_party_slots(
            b'{"response":[{"order":"1","actor":"10000001"},'
            b'{"order":"1","actor":"10000002"}]}')
        self.assertEqual(client.party_slot_count(), 0)
        self.assertFalse(client.mark_actor("10000001", "attack1"))

    def test_empty_roster_clears_previous_slots(self):
        client = TelestoClient(enabled=True)
        client._update_party_slots(
            b'{"response":[{"order":"1","actor":"10000001"}]}')
        self.assertEqual(client.party_slot_count(), 1)
        client._update_party_slots(b'{"response":[]}')
        self.assertEqual(client.party_slot_count(), 0)


if __name__ == "__main__":
    unittest.main()
