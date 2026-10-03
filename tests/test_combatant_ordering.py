import json
import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt6.QtWidgets import QApplication

from nyaatriggers.combatant_responses import CombatantResponses, request_tag
from nyaatriggers.triggevent_bridge import TriggeventBridge
from nyaatriggers.triggevent_recovery import TriggeventRecovery
from nyaatriggers.ws_client import WSClient

APP = QApplication.instance() or QApplication([])


def reply(sequence, rows, full=True):
    return {"rseq": request_tag("allCombatants" if full else "specificCombatants", sequence),
            "combatants": [{"ID": ident, "CurrentHP": hp} for ident, hp in rows]}


def values(data):
    return {row["ID"]: row["CurrentHP"] for row in data["combatants"]}


class CombatantOrderingTests(unittest.TestCase):
    def test_zone_boundary_retires_outstanding_full_and_specific_replies(self):
        ws = WSClient()
        sent, engine, raw = [], [], []
        ws.engine_message.connect(engine.append)
        ws.raw_message.connect(raw.append)
        with patch.object(ws._ws, "isValid", return_value=True), \
                patch.object(ws._ws, "sendTextMessage", side_effect=sent.append):
            ws.request_combatants_once()
            ws.request_engine_combatants([1])
            ws._flush_combatant_requests()
            old_tags = [json.loads(item)["rseq"] for item in sent]
            zone = json.dumps({"type": "LogLine", "line":
                              ["01", "2026-09-26T05:00:01Z", "2", "Same name", "checksum"]})
            ws._on_message(zone)
            for tag in old_tags:
                ws._on_message(json.dumps({"rseq": tag, "combatants": [{"ID": 1}]}))
            self.assertEqual(engine, [zone])
            ws.request_combatants_once()
            fresh = json.dumps({"rseq": json.loads(sent[-1])["rseq"], "combatants": []})
            ws._on_message(fresh)
            self.assertEqual(json.loads(engine[-1])["rseq"], "allCombatants")
        replay = CombatantResponses()
        self.assertEqual([accepted for frame in raw
                          if (accepted := replay.normalize_line(frame)) is not None], engine)

    def test_zone_formats_share_capture_replay_and_live_retirement(self):
        line = "01|2026-09-26T05:00:01Z|2|New|checksum"
        for zone in (line, json.dumps({"type": "LogLine", "rawLine": line}),
                     json.dumps({"type": "LogLine", "raw_line": line}),
                     json.dumps({"type": "broadcast", "msgtype": "LogLine", "msg": line})):
            with self.subTest(zone=zone):
                ws = WSClient()
                engine, raw = [], []
                ws.engine_message.connect(engine.append)
                ws.raw_message.connect(raw.append)
                old = ws._combatant_tag("allCombatants")
                ws._on_message(zone)
                ws._on_message(json.dumps({"rseq": old, "combatants": []}))
                self.assertEqual(engine, [zone])
                replay = CombatantResponses()
                self.assertEqual([frame for item in raw
                                  if (frame := replay.normalize_line(item)) is not None], engine)

    def test_midzone_capture_accepts_tagged_rows_until_its_first_boundary(self):
        source, replay = CombatantResponses(), CombatantResponses()
        source.observe_raw("01|2026-09-26T05:00:00Z|1|Same name|checksum")
        tag = request_tag("specificCombatants", 20, source.zone_token)
        self.assertEqual(values(replay.accept({"rseq": tag, "combatants": [{"ID": 1, "CurrentHP": 40}]})),
                         {1: 40})
        replay.observe_raw("01|2026-09-26T05:00:01Z|1|Same name|checksum")
        self.assertEqual(replay._partials, {})
        self.assertIsNone(replay.accept({"rseq": request_tag("allCombatants", 21, source.zone_token),
                                         "combatants": [{"ID": 1, "CurrentHP": 50}]}))
        fresh = request_tag("allCombatants", 22, replay.zone_token)
        self.assertEqual(values(replay.accept({"rseq": fresh, "combatants": []})), {})

    def test_typed_zone_and_invalid_raw_zone_do_not_retire_requests(self):
        for zone in ({"type": "ChangeZone", "zoneID": 2},
                     {"type": "LogLine", "rawLine": "01|now|nothex|New"},
                     {"type": "LogLine", "rawLine": "01|now|-1|New"},
                     {"type": "LogLine", "rawLine": "01|now|100000000|New"},
                     {"type": "LogLine", "rawLine": "01|now|2"}):
            with self.subTest(zone=zone):
                ws = WSClient()
                engine = []
                ws.engine_message.connect(engine.append)
                tag = ws._combatant_tag("allCombatants")
                ws._on_message(json.dumps(zone))
                ws._on_message(json.dumps({"rseq": tag, "combatants": []}))
                self.assertEqual(len(engine), 2)
                self.assertEqual(json.loads(engine[-1])["rseq"], "allCombatants")

    def test_repeated_raw_boundary_retires_old_requests_before_new_callback_requests(self):
        ws = WSClient()
        zone = json.dumps({"type": "LogLine", "rawLine": "01|2026-09-26T05:00:01Z|2|New|checksum"})
        ws._on_message(zone)
        old = ws._combatant_tag("allCombatants")
        engine, tags = [], []
        ws.engine_message.connect(engine.append)
        ws.engine_message.connect(lambda frame: tags.append(ws._combatant_tag("allCombatants"))
                                  if frame == zone else None)
        ws._on_message(zone)
        ws._on_message(json.dumps({"rseq": old, "combatants": []}))
        ws._on_message(json.dumps({"rseq": tags[-1], "combatants": []}))
        self.assertEqual(len(engine), 2)
        self.assertEqual(json.loads(engine[-1])["rseq"], "allCombatants")

    def test_zone_boundary_during_recovery_drops_late_roster_before_buffering(self):
        ws, bridge = WSClient(), TriggeventBridge()
        bridge._active = True
        bridge._gen = 1
        recovery = TriggeventRecovery(bridge, ws, lambda: None)
        recovery._on_status(True, "Starting", 1)
        old = ws._combatant_tag("allCombatants")
        zone = json.dumps({"type": "LogLine", "rawLine": "01|2026-09-26T05:00:01Z|2|New|checksum"})
        ws._on_message(zone)
        ws._on_message(json.dumps({"rseq": old, "combatants": [{"ID": 1}]}))
        self.assertEqual(recovery._pending, [zone])

    def test_delayed_full_reply_cannot_undo_damage_or_resurrect_removed_actor(self):
        responses = CombatantResponses()
        self.assertEqual(values(responses.accept(reply(2, [(1, 40000)]))), {1: 40000})
        self.assertIsNone(responses.accept(reply(1, [(1, 50000), (2, 100)])))
        self.assertIsNone(responses.accept(reply(2, [(1, 50000)])))
        self.assertEqual(values(responses.accept(reply(3, []))), {})

    def test_full_and_specific_replies_order_each_shared_actor(self):
        responses = CombatantResponses()
        responses.accept(reply(2, [(1, 40000), (3, 30)], full=False))
        self.assertEqual(values(responses.accept(reply(1, [(1, 50000), (2, 20)]))),
                         {1: 40000, 2: 20, 3: 30})
        responses.accept(reply(4, [(2, 10)], full=False))
        self.assertEqual(values(responses.accept(reply(3, [(1, 30000), (2, 15)], full=False))),
                         {1: 30000})
        self.assertEqual(values(responses.accept(reply(5, [(1, 20000)]))), {1: 20000})
        self.assertIsNone(responses.accept(reply(4, [(2, 5), (3, 5)], full=False)))
        self.assertEqual(responses._partials, {})

    def test_partial_cache_is_bounded_without_allowing_forgotten_rows_to_regress(self):
        for limits in ({"_MAX_PARTIAL_ROWS": 2}, {"_MAX_PARTIAL_BYTES": 40}):
            with self.subTest(limits=limits), patch.multiple("nyaatriggers.combatant_responses", **limits):
                responses = CombatantResponses()
                for sequence in range(1, 8):
                    responses.accept(reply(sequence, [(sequence, 100)], full=False))
                self.assertLessEqual(len(responses._partials), 2)
                self.assertGreater(responses._floor, 0)
                self.assertIsNone(responses.accept(reply(responses._floor, [])))
                self.assertIsNone(responses.accept(reply(responses._floor, [(1, 999)], full=False)))
                self.assertEqual(values(responses.accept(reply(8, []))), {})
                self.assertEqual(responses._bytes, 0)

    def test_legacy_and_unrelated_frames_keep_their_exact_replay_bytes(self):
        responses = CombatantResponses()
        for raw in (' {"rseq":"allCombatants", "combatants":[]} ',
                    '{"rseq":"specificCombatants","combatants":[]}',
                    '{"type":"ChangeZone","zoneID":1363}', '[1,2]', 'not json',
                    '{"rseq":"nyaa:allCombatants:bad","combatants":[]}'):
            self.assertEqual(responses.normalize_line(raw), raw)
        self.assertEqual(json.loads(responses.normalize_line(json.dumps(reply(2, []))))["rseq"],
                         "allCombatants")
        self.assertIsNone(responses.normalize_line(json.dumps(reply(1, []))))

    def test_capture_replay_sends_canonical_ordered_responses_to_the_engine(self):
        from tests.test_replay_pull import ReplayTests

        frames = [reply(2, [(1, 40)]), reply(1, [(1, 50), (2, 10)]),
                  reply(4, [(1, 30), (3, 9)], full=False), reply(3, [(1, 35), (2, 10)]),
                  reply(5, [(1, 20)]), reply(4, [(2, 10)], full=False)]
        code, _elapsed, output = ReplayTests().run_replay(
            [json.dumps(frame) for frame in frames],
            """import json, sys
roster = {}
for raw in sys.stdin:
    frame = json.loads(raw)
    assert frame['rseq'] in ('allCombatants', 'specificCombatants')
    if frame['rseq'] == 'allCombatants':
        roster.clear()
    roster.update({row['ID']: row['CurrentHP'] for row in frame['combatants']})
assert roster == {1: 20}, roster
print(json.dumps({'t': 'callout', 'text': 'ordered roster'}))
""", "--expect", "ordered roster")
        self.assertEqual(code, 0, output)

    def test_raw_capture_is_verbatim_once_and_engine_frames_precede_metadata(self):
        ws = WSClient()
        raw, engines, snapshots, order = [], [], [], []
        ws.raw_message.connect(raw.append)
        ws.engine_message.connect(engines.append)
        ws.combatants.connect(snapshots.append)
        ws.engine_message.connect(lambda _raw: order.append(tuple(ws.state_snapshot())))
        fresh, stale = json.dumps(reply(2, [(1, 40)])), json.dumps(reply(1, [(1, 50)]))
        ws._on_message(fresh)
        ws._on_message(stale)
        zone = '{"type":"ChangeZone","zoneID":1363}'
        ws._on_message(zone)
        self.assertEqual(raw, [fresh, stale, zone])
        self.assertEqual(len(engines), 2)
        self.assertEqual(json.loads(engines[0])["rseq"], "allCombatants")
        self.assertEqual(order, [(), ()])
        self.assertEqual(snapshots[0]["list"][0]["hp"], 40)
        self.assertEqual(len(snapshots), 1)

    def test_sidecar_only_restart_does_not_accept_an_outstanding_old_response(self):
        ws = WSClient()
        bridge = TriggeventBridge()
        bridge._active = True
        bridge._gen = 1
        recovery = TriggeventRecovery(bridge, ws, lambda: None)
        recovery._on_status(True, "Starting", 1)
        recovery._live = True
        ws._on_message(json.dumps(reply(2, [(1, 40)])))
        self.assertEqual(json.loads(bridge._wq.get_nowait())["rseq"], "allCombatants")
        bridge._gen = 2
        recovery._on_status(True, "Starting", 2)
        ws._on_message(json.dumps(reply(1, [(1, 50)])))
        self.assertEqual(recovery._pending, [])
        ws._on_message(json.dumps(reply(3, [(1, 30)])))
        self.assertEqual(values(json.loads(recovery._pending[0])), {1: 30})

    def test_request_tags_never_reuse_a_sequence_after_feed_reconnect(self):
        ws = WSClient()
        sent, engine = [], []
        ws.engine_message.connect(engine.append)
        with patch.object(ws._ws, "isValid", return_value=True), \
                patch.object(ws._ws, "sendTextMessage", side_effect=sent.append):
            ws.request_combatants_once()
            ws.request_engine_combatants([1])
            ws._flush_combatant_requests()
            ws._on_disconnected()
            ws.request_combatants_once()
        self.assertEqual([json.loads(raw)["rseq"] for raw in sent],
                         ["nyaa:allCombatants:1:", "nyaa:specificCombatants:2:", "nyaa:allCombatants:3:"])
        ws._on_message(json.dumps(reply(2, [(1, 50)], full=False)))
        self.assertEqual(engine, [])
        ws._on_message(json.dumps(reply(3, [(1, 40)])))
        self.assertEqual(len(engine), 1)


if __name__ == "__main__":
    unittest.main()
