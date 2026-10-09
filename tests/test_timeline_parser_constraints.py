import unittest
from unittest.mock import patch

from PyQt6.QtCore import QCoreApplication

from nyaatriggers.timeline_engine import TimelineEngine
from nyaatriggers.timeline_parser import parse


APP = QCoreApplication.instance() or QCoreApplication([])


class TimelineConstraintTests(unittest.TestCase):
    def setUp(self):
        self.now = 1000.0
        clock = patch("nyaatriggers.timeline_engine._time.monotonic", side_effect=lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)
        self.engine = TimelineEngine()
        self.addCleanup(self.engine.reset)
        self.heard = []
        self.engine.tts.connect(self.heard.append)

    def load(self, clause):
        self.heard.clear()
        entry, = parse(f'100 "Cue" {clause} window 1000,1000')
        self.engine.load([entry])
        self.engine.start()
        return entry

    def cast(self, ident="ABCD", source="Boss"):
        self.engine.process_line(["20", "ts", "40000001", source, ident,
                                  "Attack", "10000001", "Player"])

    def test_incomplete_scalar_constraints_cannot_sync_unrelated_casts(self):
        for fields in ('id:', 'id: , source: "Boss"', 'source: "Boss", id:',
                       'id: "ABCD", source "Boss"', 'id: "ABCD", stray',
                       'id: ["ABCD"], source:'):
            with self.subTest(fields=fields):
                self.load(f"StartsUsing {{ {fields} }}")
                self.cast()
                self.assertEqual(self.heard, [])
                self.assertEqual(self.engine.current_time(), 0)

    def test_empty_quoted_constraints_match_only_empty_fields(self):
        for quote in ('"', "'"):
            with self.subTest(quote=quote):
                entry = self.load(f'StartsUsing {{ id: "ABCD", source: {quote}{quote} }}')
                self.assertEqual(entry.event_fields["source"], "")
                self.cast()
                self.assertEqual(self.heard, [])
                self.cast(source="")
                self.assertEqual(self.heard, ["Cue"])
                self.assertEqual(self.engine.current_time(), 100)

    def test_legacy_syntax_inside_quoted_chat_values_stays_literal(self):
        for quote in ('"', "'"):
            with self.subTest(quote=quote):
                entry = self.load(f"GameLog {{ line: {quote}sync /foo/{quote} }}")
                self.assertFalse(entry.legacy_sync)
                self.assertEqual(entry.event_fields, {"line": "sync /foo/"})
                self.engine.process_line(["00", "ts", "0044", "Boss", "unrelated"])
                self.assertEqual(self.heard, [])
                self.engine.process_line(["00", "ts", "0044", "Boss", "sync /foo/"])
                self.assertEqual(self.heard, ["Cue"])
                self.assertEqual(self.engine.current_time(), 100)

    def test_valid_scalar_and_array_constraints_preserve_all_alternatives(self):
        clauses = ('StartsUsing { id: ABCD, source: "Boss", }',
                   'StartsUsing { "id": ["ABCD", "ABCE"], \'source\': \'Boss\' }')
        for clause in clauses:
            with self.subTest(clause=clause):
                self.load(clause)
                self.cast("BEEF")
                self.cast(source="Other Boss")
                self.assertEqual(self.heard, [])
                self.cast("ABCE" if "ABCE" in clause else "ABCD")
                self.assertEqual(self.heard, ["Cue"])

    def test_real_legacy_clause_does_not_fabricate_jump_or_event_fields(self):
        entry, = parse('10 "Cue" sync /.*jump 5.*Ability { id: CAFE }.*/')
        self.assertTrue(entry.legacy_sync)
        self.assertIsNone(entry.jump)
        self.assertEqual(entry.event_type, "")
        self.assertEqual(entry.event_fields, {})

    def test_invalid_sync_does_not_discard_the_timed_cue(self):
        self.load('StartsUsing { id: }')
        self.cast()
        self.now += 100
        self.engine._tick()
        self.assertEqual(self.heard, ["Cue"])

    def test_quoted_event_literals_cannot_sync_ability_packets(self):
        clauses = ('"Ability { id: ABCD }"',
                   'StartsUsing { source: { name: "Ability { id: ABCD }" } }',
                   'StartsUsing { source: "Ability { id: ABCD } }')
        for clause in clauses:
            with self.subTest(clause=clause):
                entry = self.load(clause)
                self.engine.process_line(["21", "ts", "40000001", "Boss", "ABCD",
                                          "Attack", "10000001", "Player"])
                self.assertEqual(self.heard, [])
                self.assertEqual(self.engine.current_time(), 0)
                self.assertNotEqual(entry.event_type, "Ability")
                self.now += 100
                self.engine._tick()
                self.assertEqual(self.heard, ["Cue"])

    def test_quoted_window_literals_cannot_widen_a_real_sync_window(self):
        for quote in ('"', "'"):
            with self.subTest(quote=quote):
                self.heard.clear()
                text = f'100 "Cue" {quote}window 1000,1000{quote} StartsUsing {{ id: "ABCD" }}'
                entry, = parse(text)
                self.engine.load([entry])
                self.engine.start()
                self.cast()
                self.assertEqual(self.heard, [])
                self.assertEqual(self.engine.current_time(), 0)
                self.assertEqual((entry.window_before, entry.window_after), (2.5, 2.5))
                self.now += 100
                self.engine._tick()
                self.assertEqual(self.heard, ["Cue"])
                explicit, = parse(text + ' window 2,3')
                self.assertEqual((explicit.window_before, explicit.window_after), (2, 3))

    def test_event_syntax_inside_quoted_chat_values_stays_literal(self):
        for quote in ('"', "'"):
            with self.subTest(quote=quote):
                entry = self.load(f'GameLog {{ line: {quote}Ability {{ id: ABCD }}{quote} }}')
                self.assertEqual(entry.event_type, "GameLog")
                self.engine.process_line(["21", "ts", "40000001", "Boss", "ABCD",
                                          "Attack", "10000001", "Player"])
                self.assertEqual(self.heard, [])
                self.engine.process_line(["00", "ts", "0044", "Boss", "Ability { id: ABCD }"])
                self.assertEqual(self.heard, ["Cue"])


if __name__ == "__main__":
    unittest.main()
