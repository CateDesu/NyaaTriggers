import math
import unittest

from nyaatriggers.umad_chains import COMPRESSED_WATER, FORKED_LIGHTNING, GrandCrossSpreads


A, B, C, D = (f"1000000{index}" for index in range(1, 5))


def gains(controller, effect, actors, duration, now):
    actions = []
    for actor in actors:
        actions += controller.on_gain(effect, actor, duration, now)
    return actions


class GrandCrossSpreadTests(unittest.TestCase):
    def test_real_lightning_and_fake_water_are_the_only_spread_targets(self):
        for tell, spread, stack in (("462", FORKED_LIGHTNING, COMPRESSED_WATER),
                                     ("461", COMPRESSED_WATER, FORKED_LIGHTNING)):
            for stack_first in (False, True):
                with self.subTest(tell=tell, stack_first=stack_first):
                    controller = GrandCrossSpreads(slot_of={A: 2, B: 1, C: 3, D: 4}.get)
                    self.assertEqual(controller.on_vfx(tell, 10), [])
                    if stack_first:
                        self.assertEqual(gains(controller, stack, (C, D), 51, 19), [])
                    self.assertEqual(gains(controller, spread, (A, B), 51, 19), [
                        ("mark", B, "ignore1"), ("mark", A, "ignore2")])
                    if not stack_first:
                        self.assertEqual(gains(controller, stack, (C, D), 51, 19), [])
                    self.assertEqual(controller.outstanding(), [A, B])

    def test_long_pair_waits_for_short_pair_even_when_it_arrives_first(self):
        controller = GrandCrossSpreads()
        controller.on_vfx("462", 10)
        self.assertEqual(gains(controller, FORKED_LIGHTNING, (A, B), 76, 19), [])
        controller.on_vfx("461", 25)
        self.assertEqual(gains(controller, COMPRESSED_WATER, (D, C), 36, 34), [
            ("mark", C, "ignore1"), ("mark", D, "ignore2")])
        self.assertEqual(controller.flush(70), [
            ("clear", C), ("clear", D), ("mark", A, "ignore1"), ("mark", B, "ignore2")])
        self.assertEqual(controller.expires_at(A), 95)
        self.assertEqual(controller.flush(95), [("clear", A), ("clear", B)])
        self.assertFalse(controller.needs_flush())

    def test_one_loss_does_not_release_signs_to_the_long_pair(self):
        controller = GrandCrossSpreads()
        controller.on_vfx("462", 10)
        gains(controller, FORKED_LIGHTNING, (A, B), 51, 19)
        controller.on_vfx("462", 25)
        self.assertEqual(gains(controller, FORKED_LIGHTNING, (C, D), 61, 34), [])
        self.assertEqual(controller.on_loss(FORKED_LIGHTNING, A, 69), [("clear", A)])
        self.assertEqual(controller.outstanding(), [B])
        self.assertEqual(controller.on_loss(FORKED_LIGHTNING, B, 69), [
            ("clear", B), ("mark", C, "ignore1"), ("mark", D, "ignore2")])
        self.assertEqual(controller.on_loss(FORKED_LIGHTNING, A, 70), [])

    def test_same_actor_can_have_spreads_from_two_different_statuses(self):
        controller = GrandCrossSpreads()
        controller.on_vfx("462", 10)
        gains(controller, FORKED_LIGHTNING, (A, B), 51, 19)
        controller.on_vfx("461", 25)
        gains(controller, COMPRESSED_WATER, (A, C), 61, 34)
        self.assertEqual(controller.expires_at(A), 70)
        self.assertEqual(controller.flush(70), [
            ("clear", A), ("clear", B), ("mark", A, "ignore1"), ("mark", C, "ignore2")])
        self.assertEqual(controller.expires_at(A), 95)
        self.assertEqual(controller.on_loss(FORKED_LIGHTNING, A, 70.1), [])
        self.assertEqual(controller.on_loss(COMPRESSED_WATER, A, 94), [("clear", A)])

    def test_both_waves_can_wait_for_a_delayed_tell(self):
        controller = GrandCrossSpreads()
        self.assertEqual(gains(controller, FORKED_LIGHTNING, (A, B), 51, 19), [])
        self.assertEqual(controller.on_vfx("462", 20), [
            ("mark", A, "ignore1"), ("mark", B, "ignore2")])
        self.assertEqual(gains(controller, COMPRESSED_WATER, (C, D), 61, 34), [])
        self.assertEqual(controller.on_vfx("461", 35), [])
        self.assertEqual(controller.flush(70), [
            ("clear", A), ("clear", B), ("mark", C, "ignore1"), ("mark", D, "ignore2")])

    def test_missing_tell_and_incomplete_pair_never_mark(self):
        for tell, actors in ((None, (A, B)), ("462", (A,))):
            with self.subTest(tell=tell):
                controller = GrandCrossSpreads()
                if tell:
                    controller.on_vfx(tell, 10)
                self.assertEqual(gains(controller, FORKED_LIGHTNING, actors, 51, 19), [])
                self.assertEqual(controller.flush(25), [])
                self.assertEqual(controller.on_vfx("461", 26), [])
                self.assertEqual(controller.outstanding(), [])
                self.assertFalse(controller.needs_flush())

    def test_missing_short_pair_never_promotes_the_long_pair(self):
        controller = GrandCrossSpreads()
        controller.on_vfx("462", 10)
        gains(controller, FORKED_LIGHTNING, (A, B), 76, 19)
        self.assertEqual(controller.flush(70), [])
        self.assertEqual(controller.flush(95), [])
        self.assertFalse(controller.needs_flush())

    def test_repeats_do_not_consume_tells_or_extend_expiry(self):
        controller = GrandCrossSpreads()
        controller.on_vfx("462", 10, "first")
        gains(controller, FORKED_LIGHTNING, (A, B), 51, 19)
        self.assertEqual(controller.on_vfx("462", 20, "first"), [])
        self.assertEqual(gains(controller, FORKED_LIGHTNING, (A, B), 76, 20), [])
        controller.on_vfx("461", 25, "second")
        self.assertEqual(gains(controller, FORKED_LIGHTNING, (A, B), 76, 34), [])
        gains(controller, COMPRESSED_WATER, (C, D), 61, 34)
        self.assertEqual(controller.on_vfx("462", 40, "third"), [])
        self.assertEqual(controller.flush(70), [
            ("clear", A), ("clear", B), ("mark", C, "ignore1"), ("mark", D, "ignore2")])

    def test_stray_spread_gains_do_not_blacklist_the_next_valid_pair(self):
        for tell, effect in (("462", FORKED_LIGHTNING), ("461", COMPRESSED_WATER)):
            with self.subTest(tell=tell):
                controller = GrandCrossSpreads()
                controller.on_vfx(tell, 6)
                gains(controller, effect, (A, B), 51, 10)
                self.assertEqual(gains(controller, effect, (C, D), 61, 11), [])
                controller.on_vfx(tell, 21)
                self.assertEqual(gains(controller, effect, (C, D), 61, 25), [])
                self.assertEqual(controller.on_loss(effect, A, 60), [("clear", A)])
                self.assertEqual(controller.on_loss(effect, B, 60), [
                    ("clear", B), ("mark", C, "ignore1"), ("mark", D, "ignore2")])

    def test_late_stack_duplicates_cannot_become_the_next_waves_spreads(self):
        for first, second, spread, stack in (("462", "461", FORKED_LIGHTNING, COMPRESSED_WATER),
                                             ("461", "462", COMPRESSED_WATER, FORKED_LIGHTNING)):
            with self.subTest(first=first):
                controller = GrandCrossSpreads()
                controller.on_vfx(first, 6)
                gains(controller, spread, (A, B), 51, 10)
                self.assertEqual(gains(controller, stack, (C, D), 51, 11), [])
                controller.on_vfx(second, 21)
                self.assertEqual(gains(controller, stack, (C, D), 61, 25), [])
                actors = ("10000005", "10000006")
                self.assertEqual(gains(controller, stack, actors, 61, 25), [])
                self.assertEqual(controller.flush(61), [
                    ("clear", A), ("clear", B),
                    ("mark", actors[0], "ignore1"), ("mark", actors[1], "ignore2")])

    def test_old_incomplete_pair_loss_or_expiry_preserves_the_newer_tell(self):
        for expiry in (False, True):
            with self.subTest(expiry=expiry):
                controller = GrandCrossSpreads()
                controller.on_gain(FORKED_LIGHTNING, A, 2 if expiry else 51, 100)
                controller.on_vfx("461", 101)
                actions = controller.flush(102) if expiry else controller.on_loss(FORKED_LIGHTNING, A, 102)
                self.assertEqual(actions, [])
                self.assertEqual(gains(controller, COMPRESSED_WATER, (B, C), 36, 102.1), [
                    ("mark", B, "ignore1"), ("mark", C, "ignore2")])

    def test_distinct_wave_tells_in_one_processing_tick_keep_their_polarity(self):
        controller = GrandCrossSpreads()
        controller.on_vfx("461", 100, "first")
        self.assertEqual(gains(controller, COMPRESSED_WATER, (A, B), 76, 100), [])
        controller.on_vfx("462", 100, "second")
        self.assertEqual(gains(controller, FORKED_LIGHTNING, (C, D), 36, 100), [
            ("mark", C, "ignore1"), ("mark", D, "ignore2")])
        self.assertEqual(controller.flush(136), [
            ("clear", C), ("clear", D), ("mark", A, "ignore1"), ("mark", B, "ignore2")])

    def test_expired_or_chaos_tells_cannot_classify_spreads(self):
        for tell, gain_time in (("460", 19), ("45F", 19), ("462", 23)):
            with self.subTest(tell=tell):
                controller = GrandCrossSpreads()
                controller.on_vfx(tell, 10)
                self.assertEqual(gains(controller, FORKED_LIGHTNING, (A, B), 51, gain_time), [])
                self.assertEqual(controller.flush(gain_time + 6), [])
                self.assertEqual(controller.outstanding(), [])

    def test_unrelated_status_loss_cannot_clear_a_spread(self):
        controller = GrandCrossSpreads()
        controller.on_vfx("462", 10)
        gains(controller, FORKED_LIGHTNING, (A, B), 51, 19)
        self.assertEqual(controller.on_loss(COMPRESSED_WATER, A, 20), [])
        self.assertEqual(controller.on_loss("15AA", A, 20), [])
        self.assertEqual(controller.outstanding(), [A, B])

    def test_expired_waiting_pair_never_receives_marks(self):
        controller = GrandCrossSpreads()
        controller.on_vfx("462", 10)
        gains(controller, FORKED_LIGHTNING, (A, B), 51, 19)
        controller.on_vfx("461", 25)
        gains(controller, COMPRESSED_WATER, (C, D), 61, 34)
        self.assertEqual(controller.flush(96), [("clear", A), ("clear", B)])
        self.assertFalse(controller.needs_flush())

    def test_invalid_durations_do_not_complete_a_pair(self):
        for duration in (None, "invalid", [], 0, -1, math.nan, math.inf):
            with self.subTest(duration=duration):
                controller = GrandCrossSpreads()
                controller.on_vfx("462", 10)
                controller.on_gain(FORKED_LIGHTNING, A, 51, 19)
                self.assertEqual(controller.on_gain(FORKED_LIGHTNING, B, duration, 19), [])
                self.assertEqual(controller.outstanding(), [])

    def test_loss_before_delayed_tell_cannot_complete_the_pair(self):
        controller = GrandCrossSpreads()
        gains(controller, FORKED_LIGHTNING, (A, B), 51, 19)
        controller.on_loss(FORKED_LIGHTNING, A, 20)
        self.assertEqual(controller.on_vfx("462", 21), [])
        self.assertEqual(controller.outstanding(), [])

    def test_reset_and_stale_cleanup_discard_waiting_pairs(self):
        for reset in (True, False):
            with self.subTest(reset=reset):
                controller = GrandCrossSpreads()
                controller.on_vfx("462", 10)
                gains(controller, FORKED_LIGHTNING, (A, B), 51, 19)
                controller.on_vfx("461", 25)
                gains(controller, COMPRESSED_WATER, (C, D), 61, 34)
                if reset:
                    controller.reset()
                else:
                    self.assertEqual(controller.flush(125), [("clear", A), ("clear", B)])
                self.assertEqual(controller.outstanding(), [])
                self.assertFalse(controller.needs_flush())
                controller.on_vfx("462", 130)
                self.assertEqual(gains(controller, FORKED_LIGHTNING, (A, B), 51, 139), [
                    ("mark", A, "ignore1"), ("mark", B, "ignore2")])


if __name__ == "__main__":
    unittest.main()
