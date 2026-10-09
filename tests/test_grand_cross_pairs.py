import math
import unittest

from nyaatriggers.umad_chains import (
    ACCELERATION_BOMB, FORKED_LIGHTNING, GrandCrossPairs, StatusPairs,
)


ACTORS = tuple(f"1000000{index}" for index in range(1, 9))
A, B, C, D, E, F, G, H = ACTORS


def markers(count):
    return {f"{kind}{index}": f"attack{offset + index}"
            for kind, offset in (("real", 0), ("fake", count))
            for index in range(1, count + 1)}


def gains(controller, actors, durations, now):
    actions = []
    status = next(iter(controller.ids))
    for actor, duration in zip(actors, durations):
        actions += controller.on_gain(status, actor, duration, now)
    return actions


class GrandCrossPairsTests(unittest.TestCase):
    def lightning(self, **kwargs):
        return GrandCrossPairs(FORKED_LIGHTNING, 2, markers(2), **kwargs)

    def bomb(self, **kwargs):
        return GrandCrossPairs(ACCELERATION_BOMB, 4, markers(4), **kwargs)

    def test_lightning_polarity_and_party_order(self):
        for tell, signs in (("462", ("attack1", "attack2")),
                            ("461", ("attack3", "attack4"))):
            with self.subTest(tell=tell):
                controller = self.lightning(slot_of={A: 2, B: 1}.get)
                self.assertEqual(controller.on_vfx(tell, 10), [])
                self.assertEqual(gains(controller, (A, B), (51, 51), 19), [
                    ("mark", B, signs[0]), ("mark", A, signs[1])])
                self.assertEqual(controller.outstanding(), [A, B])

    def test_lightning_orders_by_party_slot_even_with_different_durations(self):
        controller = self.lightning(slot_of={A: 1, B: 2}.get)
        controller.on_vfx("462", 10)
        self.assertEqual(gains(controller, (B, A), (51, 76), 19), [
            ("mark", A, "attack1"), ("mark", B, "attack2")])

    def test_bomb_collects_all_four_and_numbers_short_before_long(self):
        slots = {A: 1, B: 2, C: 3, D: 4}
        for tell, offset in (("462", 0), ("461", 4)):
            with self.subTest(tell=tell):
                controller = self.bomb(slot_of=slots.get)
                controller.on_vfx(tell, 10)
                self.assertEqual(gains(controller, (B, A, D), (76, 76, 51), 19), [])
                self.assertEqual(gains(controller, (C,), (51,), 19), [
                    ("mark", C, f"attack{offset + 1}"),
                    ("mark", D, f"attack{offset + 2}"),
                    ("mark", A, f"attack{offset + 3}"),
                    ("mark", B, f"attack{offset + 4}")])
                self.assertEqual(controller.flush(70), [("clear", D), ("clear", C)])
                self.assertEqual(controller.outstanding(), [A, B])
                self.assertEqual(controller.flush(95), [("clear", B), ("clear", A)])
                self.assertFalse(controller.needs_flush())

    def test_two_bomb_waves_retain_separate_polarity_and_expiration(self):
        controller = self.bomb()
        controller.on_vfx("462", 10, "first")
        self.assertEqual(len(gains(controller, (A, B, C, D), (51, 51, 76, 76), 19)), 4)
        controller.on_vfx("461", 25, "second")
        self.assertEqual(gains(controller, (H, G, F, E), (61, 61, 36, 36), 34), [
            ("mark", E, "attack5"), ("mark", F, "attack6"),
            ("mark", G, "attack7"), ("mark", H, "attack8")])
        self.assertEqual(controller.outstanding(), list(ACTORS))
        self.assertEqual(set(controller.flush(70)), {
            ("clear", A), ("clear", B), ("clear", E), ("clear", F)})
        self.assertEqual(controller.outstanding(), [C, D, G, H])
        self.assertEqual(set(controller.flush(95)), {
            ("clear", C), ("clear", D), ("clear", G), ("clear", H)})
        self.assertFalse(controller.needs_flush())

    def test_bomb_timer_jitter_does_not_change_party_order_within_each_pair(self):
        slots = {A: 1, B: 2, C: 3, D: 4}
        durations = {A: 51.0, B: 50.99, C: 76.0, D: 75.99}
        for actors in ((D, B, C, A), (C, A, D, B), (B, D, A, C)):
            with self.subTest(actors=actors):
                controller = self.bomb(slot_of=slots.get)
                controller.on_vfx("462", 10)
                self.assertEqual(gains(controller, actors,
                                       [durations[actor] for actor in actors], 19), [
                    ("mark", A, "attack1"), ("mark", B, "attack2"),
                    ("mark", C, "attack3"), ("mark", D, "attack4")])

    def test_same_polarity_signs_wait_for_prior_holder_loss(self):
        controller = self.lightning()
        controller.on_vfx("462", 10, "first")
        gains(controller, (A, B), (51, 51), 19)
        controller.on_vfx("462", 25, "second")
        self.assertEqual(gains(controller, (C, D), (61, 61), 34), [])
        self.assertEqual(controller.outstanding(), [A, B])
        self.assertEqual(controller.on_loss(FORKED_LIGHTNING, A, 65), [
            ("clear", A), ("mark", C, "attack1")])
        self.assertEqual(controller.on_loss(FORKED_LIGHTNING, B, 65), [
            ("clear", B), ("mark", D, "attack2")])
        self.assertEqual(controller.outstanding(), [C, D])

    def test_expiration_transfers_signs_without_received_losses(self):
        controller = self.lightning()
        controller.on_vfx("462", 10)
        gains(controller, (A, B), (51, 51), 19)
        controller.on_vfx("462", 25)
        gains(controller, (C, D), (61, 61), 34)
        self.assertEqual(controller.flush(70), [
            ("clear", A), ("clear", B),
            ("mark", C, "attack1"), ("mark", D, "attack2")])
        self.assertEqual(controller.flush(95), [("clear", C), ("clear", D)])

    def test_expired_waiting_carriers_never_receive_a_sign(self):
        controller = self.lightning()
        controller.on_vfx("462", 10)
        gains(controller, (A, B), (51, 51), 19)
        controller.on_vfx("462", 25)
        gains(controller, (C, D), (36, 36), 34)
        self.assertEqual(controller.flush(70), [("clear", A), ("clear", B)])
        self.assertFalse(controller.needs_flush())

    def test_loss_at_prior_pair_expiry_does_not_mark_the_lost_carrier(self):
        controller = self.lightning()
        controller.on_vfx("462", 10)
        gains(controller, (A, B), (51, 51), 19)
        controller.on_vfx("462", 25)
        gains(controller, (C, D), (61, 61), 34)
        self.assertEqual(controller.on_loss(FORKED_LIGHTNING, C, 70), [
            ("clear", A), ("clear", B), ("mark", D, "attack2")])
        self.assertEqual(controller.outstanding(), [D])

    def test_complete_wave_can_wait_for_delayed_tell(self):
        for count, controller in ((2, self.lightning()), (4, self.bomb())):
            with self.subTest(count=count):
                self.assertEqual(gains(controller, ACTORS[:count], [51] * count, 10), [])
                self.assertTrue(controller.needs_flush())
                self.assertEqual(controller.flush(11), [])
                actions = controller.on_vfx("461", 12)
                self.assertEqual(actions, [("mark", actor, f"attack{count + index}")
                                           for index, actor in enumerate(ACTORS[:count], 1)])

    def test_missing_tell_or_incomplete_wave_fails_closed(self):
        for tell, count in ((None, 2), ("462", 1)):
            with self.subTest(tell=tell):
                controller = self.lightning()
                if tell:
                    controller.on_vfx(tell, 10)
                self.assertEqual(gains(controller, ACTORS[:count], [51] * count, 11), [])
                self.assertEqual(controller.flush(17), [])
                self.assertFalse(controller.needs_flush())
                self.assertEqual(controller.on_vfx("461", 18), [])
                self.assertEqual(controller.outstanding(), [])

    def test_expired_tell_cannot_classify_later_gains(self):
        controller = self.lightning()
        controller.on_vfx("462", 10)
        self.assertEqual(gains(controller, (A, B), (51, 51), 23), [])
        self.assertEqual(controller.on_vfx("461", 24), [
            ("mark", A, "attack3"), ("mark", B, "attack4")])

    def test_duplicate_tell_and_gain_do_not_consume_the_next_wave(self):
        controller = self.lightning()
        controller.on_vfx("462", 10, "first")
        gains(controller, (A, B), (51, 51), 19)
        self.assertEqual(controller.on_vfx("462", 20, "first"), [])
        self.assertEqual(gains(controller, (A, B), (76, 76), 20), [])
        self.assertEqual(gains(controller, (C, D), (61, 61), 34), [])
        self.assertEqual(controller.on_vfx("461", 35, "second"), [
            ("mark", C, "attack3"), ("mark", D, "attack4")])
        self.assertEqual(controller._sets_done, 2)
        self.assertEqual(controller.on_vfx("462", 36, "first"), [])
        self.assertEqual(controller.on_vfx("461", 37, "third"), [])
        self.assertEqual(gains(controller, (E, F), (36, 36), 37), [])

    def test_extra_carrier_does_not_corrupt_a_wave_waiting_for_tell(self):
        controller = self.lightning()
        gains(controller, (A, B, C), (51, 51, 51), 10)
        self.assertEqual(controller.on_vfx("462", 11), [
            ("mark", A, "attack1"), ("mark", B, "attack2")])
        self.assertNotIn(C, controller.outstanding())

    def test_invalid_durations_never_form_a_wave(self):
        for duration in (None, "invalid", "", [], {}, 0, -1, math.nan, math.inf, -math.inf):
            with self.subTest(duration=duration):
                controller = self.lightning()
                controller.on_vfx("462", 10)
                self.assertEqual(gains(controller, (A, B), (51, duration), 11), [])
                self.assertEqual(controller.outstanding(), [])

    def test_duplicate_gain_cannot_extend_duration(self):
        controller = self.lightning()
        controller.on_vfx("462", 10)
        gains(controller, (A, B), (51, 51), 19)
        self.assertEqual(gains(controller, (A, B), (76, 76), 50), [])
        self.assertEqual(controller.flush(70), [("clear", A), ("clear", B)])

    def test_carrier_expiry_is_available_until_loss_or_expiration(self):
        controller = self.lightning()
        self.assertIsNone(controller.expires_at(A))
        controller.on_vfx("462", 10)
        gains(controller, (A, B), (51, 76), 19)
        self.assertEqual(controller.expires_at(A.lower()), 70)
        self.assertEqual(controller.expires_at(B), 95)
        controller.on_loss(FORKED_LIGHTNING, A, 20)
        self.assertIsNone(controller.expires_at(A))
        controller.flush(95)
        self.assertIsNone(controller.expires_at(B))

    def test_pending_loss_prevents_incomplete_wave_assignment(self):
        controller = self.bomb()
        controller.on_vfx("462", 10)
        gains(controller, (A, B, C), (51, 51, 76), 19)
        self.assertEqual(controller.on_loss(ACCELERATION_BOMB, A, 19.5), [])
        self.assertEqual(gains(controller, (D,), (76,), 20), [])
        self.assertEqual(controller.outstanding(), [])
        self.assertEqual(controller.flush(25), [])
        self.assertFalse(controller.needs_flush())

    def test_empty_incomplete_wave_retires_its_own_tell(self):
        for count, factory in ((2, self.lightning), (4, self.bomb)):
            for boundary in ("expiry", "loss"):
                with self.subTest(count=count, boundary=boundary):
                    controller = factory()
                    status = next(iter(controller.ids))
                    controller.on_vfx("462", 100, "old")
                    controller.on_gain(status, A, 1 if boundary == "expiry" else 51, 100)
                    actions = (controller.flush(101) if boundary == "expiry"
                               else controller.on_loss(status, A, 101))
                    self.assertEqual(actions, [])
                    actors = ACTORS[1:count + 1]
                    self.assertEqual(gains(controller, actors, [51] * count, 101.1), [])
                    self.assertEqual(controller.outstanding(), [])
                    self.assertEqual(controller.on_vfx("461", 101.2, "new"), [
                        ("mark", actor, f"attack{count + index}")
                        for index, actor in enumerate(actors, 1)])

    def test_empty_incomplete_wave_preserves_a_newer_tell(self):
        for count, factory in ((2, self.lightning), (4, self.bomb)):
            for boundary in ("expiry", "loss"):
                with self.subTest(count=count, boundary=boundary):
                    controller = factory()
                    status = next(iter(controller.ids))
                    controller.on_gain(status, A, 2 if boundary == "expiry" else 51, 100)
                    controller.on_vfx("461", 101, "new")
                    actions = (controller.flush(102) if boundary == "expiry"
                               else controller.on_loss(status, A, 102))
                    self.assertEqual(actions, [])
                    actors = ACTORS[1:count + 1]
                    self.assertEqual(gains(controller, actors, [51] * count, 102.1), [
                        ("mark", actor, f"attack{count + index}")
                        for index, actor in enumerate(actors, 1)])

    def test_assigned_loss_preserves_the_next_wave_tell(self):
        for count, factory in ((2, self.lightning), (4, self.bomb)):
            with self.subTest(count=count):
                controller = factory()
                status = next(iter(controller.ids))
                controller.on_vfx("462", 100, "first")
                gains(controller, ACTORS[:count], [51] * count, 100)
                controller.on_vfx("461", 101, "second")
                self.assertEqual(controller.on_loss(status, A, 102), [("clear", A)])
                actors = ACTORS[count:count * 2]
                self.assertEqual(gains(controller, actors, [51] * count, 102.1), [
                    ("mark", actor, f"attack{count + index}")
                    for index, actor in enumerate(actors, 1)])

    def test_stale_state_clears_marks_and_allows_a_new_phase(self):
        controller = self.lightning()
        controller.on_vfx("462", 10, "first")
        gains(controller, (A, B), (51, 51), 19)
        self.assertEqual(controller.on_vfx("461", 110, "first"), [
            ("clear", A), ("clear", B)])
        self.assertEqual(gains(controller, (C, D), (36, 36), 119), [
            ("mark", C, "attack3"), ("mark", D, "attack4")])
        controller.reset()
        self.assertEqual(controller.outstanding(), [])
        self.assertFalse(controller.needs_flush())

    def test_marker_changes_and_duplicate_signs_preserve_active_holders(self):
        controller = self.lightning()
        controller.set_markers({"real1": "square", "real2": "square"})
        controller.on_vfx("462", 10)
        self.assertEqual(gains(controller, (A, B), (51, 51), 19), [("mark", A, "square")])
        controller.set_markers({"real1": "circle", "real2": "triangle"})
        self.assertEqual(controller.flush(20), [("mark", B, "triangle")])
        self.assertEqual(controller._marked[A], "square")
        self.assertEqual(controller.on_loss(FORKED_LIGHTNING, A, 21), [("clear", A)])

    def test_blank_markers_leave_carriers_unassigned(self):
        controller = self.lightning()
        controller.set_markers({key: "" for key in markers(2)})
        controller.on_vfx("462", 10)
        self.assertEqual(gains(controller, (A, B), (51, 51), 19), [])
        controller.set_markers({"real1": "circle"})
        self.assertEqual(controller.flush(20), [("mark", A, "circle")])

    def test_loss_after_assigning_a_blank_sign_does_not_mark_the_lost_carrier(self):
        controller = self.lightning()
        controller.set_markers({key: "" for key in markers(2)})
        controller.on_vfx("462", 10)
        gains(controller, (A, B), (51, 51), 19)
        controller.set_markers({"real1": "circle"})
        self.assertEqual(controller.on_loss(FORKED_LIGHTNING, A, 20), [])
        self.assertEqual(controller.outstanding(), [])

    def test_unrelated_effects_and_vfx_have_no_side_effects(self):
        controller = self.lightning()
        self.assertEqual(controller.on_vfx("460", 10), [])
        self.assertEqual(controller.on_gain(ACCELERATION_BOMB, A, 51, 10), [])
        self.assertEqual(controller.on_loss(ACCELERATION_BOMB, A, 10), [])
        self.assertFalse(controller.needs_flush())
        self.assertEqual(controller._sets_done, 0)


class StatusPairExpiryTests(unittest.TestCase):
    def test_compound_requires_both_constituents_before_their_deadlines(self):
        pairs = StatusPairs(("ABC", "DEF"))
        pairs.on_gain("ABC", A, 10, 3)
        pairs.on_gain("DEF", A, 12, 60)
        self.assertTrue(pairs.holds_all(A, ("ABC", "DEF"), 12))
        self.assertEqual(pairs.expires_at(A, ("ABC", "DEF")), 13)
        self.assertFalse(pairs.holds_all(A, ("ABC", "DEF"), 13))
        pairs.on_loss("ABC", A)
        self.assertEqual(pairs.expires_at(A, ("ABC", "DEF")), 72)
        pairs.reset()
        self.assertIsNone(pairs.expires_at(A, ("ABC", "DEF")))

    def test_refreshed_constituent_replaces_its_original_deadline(self):
        pairs = StatusPairs(("ABC", "DEF"))
        pairs.on_gain("ABC", A, 10, 3)
        pairs.on_gain("DEF", A, 12, 60)
        pairs.on_gain("ABC", A, 12, 6)
        self.assertTrue(pairs.holds_all(A, ("ABC", "DEF"), 14))
        self.assertEqual(pairs.expires_at(A, ("ABC", "DEF")), 18)

    def test_unknown_duration_retains_the_existing_stale_limit(self):
        for duration in (None, "invalid", 0, -1, math.nan, math.inf):
            with self.subTest(duration=duration):
                pairs = StatusPairs(("ABC", "DEF"))
                pairs.on_gain("ABC", A, 10, duration)
                pairs.on_gain("DEF", A, 10, duration)
                self.assertIsNone(pairs.expires_at(A, ("ABC", "DEF")))
                self.assertTrue(pairs.holds_all(A, ("ABC", "DEF"), 100))
                self.assertFalse(pairs.holds_all(A, ("ABC", "DEF"), 101))


if __name__ == "__main__":
    unittest.main()
