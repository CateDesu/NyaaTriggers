"""Metadata must retain its meaning across the feed and Qt boundaries."""

from contextlib import ExitStack
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt6.QtCore import QCoreApplication

from nyaatriggers.cactbot_reader import CactbotReader
from nyaatriggers.pull_capture import PullCapture
from nyaatriggers.ws_client import WSClient

APP = QCoreApplication.instance() or QCoreApplication([])
CAST = "20|ts|40000001|Boss|ABCD|Cast|10000001|Player|"


class FeedMetadataTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(patch("nyaatriggers.drop_log._LOG_FILE", self.root / "drops.log"))

    def capture(self):
        cap = PullCapture(self.root)
        self.addCleanup(cap.close)
        cap.context = lambda: ("Fight", "Arena")
        cap.on_zone_changed(100, "Arena")
        cap.set_recording(True)
        cap.on_log_line(CAST)
        self.assertTrue(cap._in_pull)
        return cap

    def test_duplicate_zone_metadata_keeps_the_capture_open(self):
        cap = self.capture()
        original = cap._path
        cap.on_zone_changed(100, "Arena")
        self.assertTrue(cap._in_pull)
        cap.on_raw_message('{"type":"after"}')
        self.assertEqual(cap._path, original)
        self.assertIn("after", original.read_text())

    def test_zone_name_metadata_for_the_same_id_keeps_the_capture(self):
        cap = self.capture()
        original = cap._path
        cap.on_zone_changed(100, "Localized arena")
        self.assertTrue(cap._in_pull)
        self.assertEqual(cap._path, original)

    def test_changed_id_ends_capture_and_late_name_keeps_the_next_pull(self):
        cap = self.capture()
        old = cap._path
        cap.on_zone_changed(200, "")
        self.assertFalse(cap._in_pull)
        self.assertEqual(json.loads(old.with_suffix(".meta.json").read_text())["outcome"], "reset")
        cap.context = lambda: ("New fight", "")
        cap.on_log_line(CAST)
        cap.on_zone_changed(200, "New arena")
        self.assertTrue(cap._in_pull)
        self.assertNotEqual(cap._path, old)

    def test_name_only_boundary_drops_the_old_id_before_late_metadata(self):
        cap = self.capture()
        cap.on_zone_changed(0, "New arena")
        self.assertFalse(cap._in_pull)
        cap.context = lambda: ("New fight", "New arena")
        cap.on_log_line(CAST)
        cap.on_zone_changed(200, "New arena")
        self.assertTrue(cap._in_pull)

    def test_zone_changes_while_recording_is_off_do_not_leave_an_old_id(self):
        cap = self.capture()
        cap.set_recording(False)
        cap.on_log_line("01|ts|C8|New arena|")
        cap.context = lambda: ("New fight", "New arena")
        cap.set_recording(True)
        cap.on_log_line(CAST)
        cap.on_zone_changed(200, "New arena")
        self.assertTrue(cap._in_pull)

    def test_raw_zone_boundary_still_ends_capture_when_identity_repeats(self):
        cap = self.capture()
        old = cap._path
        cap.on_log_line("01|ts|64|Arena|")
        self.assertFalse(cap._in_pull)
        self.assertEqual(json.loads(old.with_suffix(".meta.json").read_text())["outcome"], "reset")
        cap.on_log_line(CAST)
        cap.on_zone_changed(100, "Arena")
        self.assertTrue(cap._in_pull)

    def test_first_zone_metadata_after_reconnect_keeps_the_new_capture(self):
        cap = self.capture()
        cap.on_status_changed(False, "Disconnected")
        cap.on_status_changed(True, "Connected")
        cap.on_log_line(CAST)
        current = cap._path
        cap.on_zone_changed(0, "")
        cap.on_zone_changed(200, "New arena")
        self.assertTrue(cap._in_pull)
        self.assertEqual(cap._path, current)

    def test_valid_feed_ids_reach_signals_without_changing(self):
        client = WSClient()
        zones, players = [], []
        client.zone_changed.connect(lambda ident, name: zones.append((ident, name)))
        client.primary_player.connect(lambda ident, name: players.append((ident, name)))
        client._on_message('{"type":"ChangeZone","zoneID":1363,"zoneName":"Arena"}')
        client._on_message('{"type":"ChangePrimaryPlayer","charID":268435457,"charName":"Player"}')
        self.assertEqual(zones, [(1363, "Arena")])
        self.assertEqual(players, [(0x10000001, "Player")])
        self.assertEqual(client._player_id, 0x10000001)

    def test_raw_player_identity_reaches_later_recordings_and_combatants(self):
        client = WSClient()
        client._on_message('{"type":"ChangePrimaryPlayer","charID":268435457,"charName":"Old"}')
        client._on_message('{"type":"LogLine","rawLine":"02|ts|10000002|New|"}')
        state = [json.loads(message) for message in client.state_snapshot()]
        player = next(message for message in state if message["type"] == "ChangePrimaryPlayer")
        with self.subTest(consumer="recordings"):
            self.assertEqual((player["charID"], player["charName"]), (0x10000002, "New"))
        with self.subTest(consumer="combatants"):
            self.assertEqual(client._player_id, 0x10000002)

    def test_ids_outside_the_qt_range_cannot_wrap_into_other_ids(self):
        client = WSClient()
        for kind, key, signal in (("ChangeZone", "zoneID", client.zone_changed),
                                  ("ChangePrimaryPlayer", "charID", client.primary_player)):
            received = []
            def receive(ident, name):
                received.append(ident)
            signal.connect(receive)
            try:
                for value in (2**31, 2**32 + 1, 10**100, -2**50, -1):
                    with self.subTest(kind=kind, value=value):
                        client._on_message(json.dumps({"type": kind, key: value}))
                        self.assertEqual(received[-1], 0)
                        if kind == "ChangePrimaryPlayer":
                            self.assertEqual(client._player_id, 0)
            finally:
                signal.disconnect(receive)

    def test_nested_cactbot_messages_cannot_escape_the_callback(self):
        reader = CactbotReader()
        spoken = []
        reader.tts.connect(spoken.append)
        for kind in ("say", "popup", "status", "triggers"):
            with self.subTest(kind=kind):
                reader._on_message(kind, "[" * 2000 + "]" * 2000)
        reader._on_message("say", '{"text":"Keep going"}')
        self.assertEqual(spoken, ["Keep going"])


if __name__ == "__main__":
    unittest.main()
