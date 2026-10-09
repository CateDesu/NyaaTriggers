import itertools
import unittest

from nyaatriggers.umad_chains import (
    ACCRETION, CRUST, DPS, STALE_S, SUPPORT, AccretionQueue, BlackHoleChains,
)


FIRST, SECOND, OTHER = "10FF0001", "10FF0002", "10FF0003"
ASSIGNMENT = ((ACCRETION, FIRST), ("BBC", FIRST),
              (ACCRETION, SECOND), ("BBD", SECOND))


def assign(controller, now=100):
    return [action for effect, actor in ASSIGNMENT
            for action in controller.on_gain(effect, actor, now)]


class AccretionQueueTests(unittest.TestCase):
    def test_complete_membership_and_explicit_ranks_work_in_any_arrival_order(self):
        for events in itertools.permutations(ASSIGNMENT):
            with self.subTest(events=events):
                controller = AccretionQueue()
                for effect, actor in events[:-1]:
                    self.assertEqual(controller.on_gain(effect, actor, 100), [])
                    self.assertEqual(controller.flush(100.1), [])
                self.assertEqual(controller.on_gain(*events[-1], 100),
                                 [("mark", FIRST, "ignore1")])
                self.assertEqual(controller.outstanding(), [FIRST])

    def test_only_crust_completion_hands_off_and_duplicate_losses_are_safe(self):
        controller = AccretionQueue()
        self.assertEqual(assign(controller), [("mark", FIRST, "ignore1")])
        for effect in (ACCRETION, "BBC", "154C", "154D"):
            self.assertEqual(controller.on_gain(effect, FIRST, 105), [])
            self.assertEqual(controller.on_loss(effect, FIRST, 106), [])
        self.assertEqual(controller.flush(130), [])
        self.assertEqual(controller.outstanding(), [FIRST])
        self.assertEqual(controller.on_loss(CRUST, FIRST, 131),
                         [("clear", FIRST), ("mark", SECOND, "ignore2")])
        self.assertEqual(controller.on_loss(CRUST, FIRST, 132), [])
        self.assertEqual(controller.on_loss(ACCRETION, SECOND, 133), [])
        self.assertEqual(controller.on_loss(CRUST, SECOND, 160), [("clear", SECOND)])
        self.assertEqual(controller.on_loss(CRUST, SECOND, 161), [])
        self.assertEqual(controller.outstanding(), [])
        self.assertFalse(controller.needs_flush())

    def test_completion_before_delayed_collection_never_marks_completed_actor(self):
        for completed in ((FIRST,), (SECOND,), (FIRST, SECOND)):
            with self.subTest(completed=completed):
                controller = AccretionQueue()
                controller.on_gain("BBC", FIRST, 100)
                controller.on_gain("BBD", SECOND, 100)
                for actor in completed:
                    controller.on_loss(CRUST, actor, 101)
                controller.on_gain(ACCRETION, FIRST, 102)
                actions = controller.on_gain(ACCRETION, SECOND, 102)
                expected = [] if len(completed) == 2 else [
                    ("mark", SECOND if FIRST in completed else FIRST,
                     "ignore2" if FIRST in completed else "ignore1")]
                self.assertEqual(actions, expected)
                if completed == (SECOND,):
                    self.assertEqual(controller.on_loss(CRUST, FIRST, 103), [("clear", FIRST)])

    def test_duplicate_statuses_do_not_repeat_marks_or_resurrect_completed_carriers(self):
        controller = AccretionQueue()
        assign(controller)
        for effect, actor in ASSIGNMENT:
            self.assertEqual(controller.on_gain(effect, actor, 101), [])
        controller.on_loss(CRUST, FIRST, 130)
        for effect in (ACCRETION, "BBC", CRUST):
            self.assertEqual(controller.on_gain(effect, FIRST, 131), [])
        self.assertEqual(controller.outstanding(), [SECOND])

    def test_missing_or_ambiguous_carriers_remain_unmarked(self):
        cases = (
            ((ACCRETION, FIRST), ("BBC", FIRST)),
            ((ACCRETION, FIRST), (ACCRETION, SECOND), ("BBC", FIRST)),
            ((ACCRETION, FIRST), (ACCRETION, SECOND), ("BBC", FIRST), ("BBC", SECOND)),
            ((ACCRETION, FIRST), (ACCRETION, SECOND), (ACCRETION, OTHER),
             ("BBC", FIRST), ("BBD", SECOND)),
        )
        for events in cases:
            with self.subTest(events=events):
                controller = AccretionQueue()
                for effect, actor in events:
                    self.assertEqual(controller.on_gain(effect, actor, 100), [])
                self.assertEqual(controller.flush(130), [])
                self.assertEqual(controller.outstanding(), [])

    def test_noncarriers_with_the_same_rank_do_not_change_the_pair(self):
        controller = AccretionQueue()
        controller.on_gain("BBC", OTHER, 100)
        controller.on_gain(CRUST, OTHER, 100)
        self.assertEqual(assign(controller), [("mark", FIRST, "ignore1")])
        self.assertEqual(controller.on_loss(CRUST, OTHER, 120), [])
        self.assertEqual(controller.outstanding(), [FIRST])

    def test_conflicting_membership_clears_current_mark_and_fails_closed(self):
        for effect, actor in ((ACCRETION, OTHER), ("BBD", FIRST)):
            with self.subTest(effect=effect, actor=actor):
                controller = AccretionQueue()
                assign(controller)
                self.assertEqual(controller.on_gain(effect, actor, 101), [("clear", FIRST)])
                self.assertEqual(controller.on_loss(CRUST, FIRST, 102), [])
                self.assertEqual(controller.outstanding(), [])

    def test_stale_gain_loss_and_flush_clear_without_using_old_membership(self):
        for action in ("gain", "loss", "flush"):
            with self.subTest(action=action):
                controller = AccretionQueue()
                assign(controller)
                now = 100 + STALE_S + 0.1
                if action == "gain":
                    actions = controller.on_gain(ACCRETION, OTHER, now)
                elif action == "loss":
                    actions = controller.on_loss(CRUST, FIRST, now)
                else:
                    actions = controller.flush(now)
                self.assertEqual(actions, [("clear", FIRST)])
                self.assertEqual(controller.outstanding(), [])

    def test_flush_does_not_extend_stale_lifetime(self):
        controller = AccretionQueue()
        assign(controller)
        for now in (120, 150, 180):
            self.assertEqual(controller.flush(now), [])
        self.assertEqual(controller.flush(190.1), [("clear", FIRST)])
        self.assertFalse(controller.needs_flush())

    def test_reset_discards_completion_and_old_holders(self):
        controller = AccretionQueue()
        assign(controller)
        controller.on_loss(CRUST, FIRST, 130)
        controller.reset()
        self.assertEqual(controller.outstanding(), [])
        self.assertFalse(controller.needs_flush())
        self.assertEqual(controller.on_loss(CRUST, SECOND, 131), [])
        self.assertEqual(assign(controller, 132), [("mark", FIRST, "ignore1")])

    def test_completed_instance_rearms_from_a_new_assignment_burst(self):
        controller = AccretionQueue()
        assign(controller)
        controller.on_loss(CRUST, FIRST, 130)
        controller.on_loss(CRUST, SECOND, 160)
        self.assertEqual(assign(controller, 180), [("mark", FIRST, "ignore1")])

    def test_custom_and_unassigned_markers_keep_completion_order(self):
        controller = AccretionQueue({"first": "", "second": "bind2"})
        self.assertEqual(assign(controller), [])
        self.assertEqual(controller.outstanding(), [])
        self.assertEqual(controller.on_loss(CRUST, FIRST, 130), [("mark", SECOND, "bind2")])
        self.assertEqual(controller.on_loss(CRUST, SECOND, 160), [("clear", SECOND)])

    def test_effect_and_actor_normalization(self):
        controller = AccretionQueue()
        for effect, actor in ASSIGNMENT[:-1]:
            controller.on_gain("0x0" + effect.lower(), " " + actor.lower() + " ", 100)
        self.assertEqual(controller.on_gain("0BBD", SECOND.lower(), 100),
                         [("mark", FIRST, "ignore1")])
        self.assertEqual(controller.on_loss("0x154e", FIRST.lower(), 130),
                         [("clear", FIRST), ("mark", SECOND, "ignore2")])

    def test_unsupported_effects_do_not_extend_the_instance(self):
        controller = AccretionQueue()
        assign(controller)
        self.assertEqual(controller.on_gain("566", FIRST, 180), [])
        self.assertEqual(controller.on_loss("566", FIRST, 185), [])
        self.assertEqual(controller.flush(190.1), [("clear", FIRST)])


class SeparatedBlackHoleChainsTests(unittest.TestCase):
    def test_role_queues_exclude_accretion_without_emitting_the_old_sign(self):
        roles = {"D1": DPS, "D2": DPS, "D3": DPS, FIRST: DPS,
                 "S1": SUPPORT, "S2": SUPPORT, "S3": SUPPORT, SECOND: SUPPORT}
        controller = BlackHoleChains(roles.get, include_accretion=False)
        events = [(ACCRETION, FIRST), (ACCRETION, SECOND)]
        for actor, rank in (("D1", "BBC"), ("D2", "BBD"), ("D3", "BBE"),
                            ("S1", "BBC"), ("S2", "BBD"), ("S3", "BBE"),
                            (FIRST, "BBC"), (SECOND, "BBD")):
            events += [(rank, actor), (CRUST, actor)]
        actions = [action for effect, actor in events
                   for action in controller.on_gain(effect, actor, 100)]
        self.assertEqual(sorted(actions), sorted([("mark", "D1", "attack1"),
                                                ("mark", "S1", "attack2")]))
        self.assertFalse(controller.has_open_queues())
        self.assertEqual(controller.outstanding(), ["D1", "S1"])
        self.assertEqual(controller.on_loss(CRUST, FIRST, 130), [])
        self.assertEqual(controller.on_loss(CRUST, "D1", 131), [("mark", "D2", "attack1")])
        self.assertEqual(controller.on_loss(CRUST, "S1", 132), [("mark", "S2", "attack2")])

    def test_unknown_roles_still_fail_closed(self):
        controller = BlackHoleChains(lambda _actor: None, include_accretion=False)
        for effect, actor in ASSIGNMENT:
            controller.on_gain(effect, actor, 100)
            controller.on_gain(CRUST, actor, 100)
        self.assertEqual(controller.flush(101), [])
        self.assertTrue(controller.has_open_queues())
        self.assertEqual(controller.outstanding(), [])

    def test_role_queues_wait_for_both_accretion_carriers(self):
        roles = {"D1": DPS, "D2": DPS, "D3": DPS, FIRST: DPS,
                 "S1": SUPPORT, "S2": SUPPORT, "S3": SUPPORT, SECOND: SUPPORT}
        controller = BlackHoleChains(roles.get, include_accretion=False)
        for actor, rank in (("D1", "BBC"), ("D2", "BBD"), ("D3", "BBE"),
                            ("S1", "BBC"), ("S2", "BBD"), ("S3", "BBE"),
                            (FIRST, "BBC"), (SECOND, "BBD")):
            self.assertEqual(controller.on_gain(rank, actor, 100), [])
            self.assertEqual(controller.on_gain(CRUST, actor, 100), [])
        self.assertEqual(controller.on_gain(ACCRETION, FIRST, 100), [])
        self.assertEqual(controller.flush(101), [])
        self.assertEqual(sorted(controller.on_gain(ACCRETION, SECOND, 102)),
                         sorted([("mark", "D1", "attack1"), ("mark", "S1", "attack2")]))


if __name__ == "__main__":
    unittest.main()
