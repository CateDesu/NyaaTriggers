"""Replay encounter endings and wipes through the real meter and overlay sender."""

from types import SimpleNamespace
import unittest
from unittest.mock import patch

from nyaatriggers.dps_meter import DpsMeter
from nyaatriggers.plugin_link import _DpsProtocol, clear_frame, dps_frame, timeline_frame
from nyaatriggers.ui.dps_tab import DpsTabMixin
from nyaatriggers.ui.instance_tab import InstanceTabMixin
from nyaatriggers.ui.timeline_tab import TimelineTabMixin


class OverlayHost(DpsTabMixin, InstanceTabMixin, TimelineTabMixin):
    def __init__(self, now, native_retention=True):
        self.frames = []
        self.protocol = _DpsProtocol(native_retention)
        self._dps_meter = DpsMeter(clock=lambda: now[0])
        self._dps_meter.set_me('10000001')
        self._dps_meter.note_job(0x10000001, 31)
        self._dps_meter.on_encounter_end = self._on_meter_encounter_end
        self._dps_overlay_live = False
        self._settings = {}
        self._triggers = []
        self._dps_history = []
        self._local_enabled = False
        self._seq_runners = []
        self._timeline = SimpleNamespace(
            upcoming=lambda: [], has_schedule=lambda: True,
            process_line=lambda fields: None, reset=lambda: None)
        self._automark_rules = []
        self._automark_pending = {}
        self._automark_active = {}
        self._automark_pairs = SimpleNamespace(reset=lambda: None)
        self._plugin_link = SimpleNamespace(
            is_connected=lambda: True,
            send_dps=lambda *a, **kw: self.emit(dps_frame(*a, **kw)),
            send_timeline=lambda rows: self.emit(timeline_frame(rows)),
            send_clear=lambda **kw: self.emit(clear_frame(**kw)))
        for name in ('_update_live_dps', '_maybe_fetch_fflogs', '_clear_callout_dedup',
                     '_refresh_dps_history_list', '_clear_status_timers', '_clear_seq_runners',
                     '_umad_chain_reset', '_umad_gaze_reset',
                     '_append_ability_line'):
            setattr(self, name, lambda *a, **kw: None)

    def emit(self, frame):
        self.frames.extend(self.protocol.frames(frame))

    def prepare_zone(self):
        self._current_zone = 'Old Arena'
        self._current_zone_id = 100
        self._match_zone = self._current_zone
        self._zone_aliases = (self._current_zone,)
        self._connected = True
        self._actor_jobs = {}
        self._umad_actor_names = {}
        self._umad_chain_enabled = False
        self._mute_until_zone = False
        self._zone_lbl = SimpleNamespace(setText=lambda *a: None)
        for name in ('_append_zone_to_ability_log', '_refresh_zone_column',
                     '_refresh_telesto_party', '_redetect_zone_fight'):
            setattr(self, name, lambda *a: None)
        self._fight_tag_for_zone = lambda zone: ('', '')
        self._load_timeline_for_zone = lambda zone: self._push_timeline_to_plugin()
        self._dps_meter.set_zone_metadata(self._current_zone)

    def raw_zone(self, name='New Arena', zone_id='65'):
        fields = ['01', 'ts', zone_id, name]
        self._dispatch_log_line(fields, '|'.join(fields))

    def hit(self, amount):
        fields = ['21', 'ts', '10000001', 'Player', 'A1', 'Hit', '40000010',
                  'Boss', '0003', f'{amount << 16:X}'] + ['0'] * 14
        self._dispatch_log_line(fields, '|'.join(fields))

    def wipe(self):
        self._dispatch_log_line(['33', 'ts', '0', '4000000F'], '33|ts|0|4000000F')


class OverlayRetentionTests(unittest.TestCase):
    def setUp(self):
        self.now = [1000.0]
        clock = patch('time.monotonic', side_effect=lambda: self.now[0])
        clock.start()
        self.addCleanup(clock.stop)
        self.host = OverlayHost(self.now)

    def completed_pull(self):
        self.host._dps_meter.set_in_combat(True, True)
        self.host.hit(10000)
        self.host._dps_tick()
        self.host.wipe()
        self.host._dps_meter.set_in_combat(False, False)
        self.host.frames.clear()

    def test_final_frame_includes_damage_since_the_last_timer_tick(self):
        self.host._dps_meter.set_in_combat(True, True)
        self.host.hit(10000)
        self.host._dps_tick()
        self.now[0] += 0.2
        self.host.hit(5000)
        self.host.wipe()
        final = self.host.frames[-3]
        self.assertIs(final['show'], False)
        self.assertEqual(final['enc']['dps'], 15000)
        self.assertIs(final['enc']['hasDamage'], True)
        self.assertEqual(final['rows'], [['Player', 'MCH', 15000, 100, 0, True, 0]])
        self.assertEqual(self.host.frames[-2:], [{'c': 'clear', 'keepDps': True}] * 2)

    def test_short_pull_sends_its_result_without_a_live_damage_frame(self):
        self.completed_pull()
        self.now[0] += 60
        self.host._dps_meter.set_in_combat(True, True)
        self.host._dps_tick()
        self.now[0] += 0.2
        self.host.hit(5000)
        self.host.wipe()
        self.assertEqual(self.host.frames[-3]['enc']['dps'], 5000)
        self.assertIs(self.host.frames[-3]['show'], False)
        self.assertEqual(len(self.host.frames), 4)

    def test_empty_and_repeated_wipes_preserve_the_result_without_a_time_limit(self):
        self.completed_pull()
        self.now[0] += 60
        self.host._dps_meter.set_in_combat(True, True)
        self.host.wipe()
        self.now[0] += 60
        self.host.wipe()
        self.host._dps_tick()
        self.assertEqual(self.host.frames, [{'c': 'clear', 'keepDps': True}] * 4)

    def test_wipe_cleanup_preserves_dps_in_each_callout_mode(self):
        for local, global_on, cactbot in ((True, True, False), (True, False, False),
                                          (False, True, False), (False, True, True)):
            with self.subTest(local=local, global_on=global_on, cactbot=cactbot):
                host = OverlayHost(self.now)
                host._local_enabled = local
                host._global_local_on_flag = global_on
                host._cactbot_mode = cactbot
                host._dps_meter.set_in_combat(True, True)
                host.hit(5000)
                host.wipe()
                self.assertEqual(host.frames[0]['enc']['dps'], 5000)
                clears = [frame for frame in host.frames if frame['c'] == 'clear']
                self.assertTrue(clears)
                self.assertTrue(all(frame['keepDps'] for frame in clears))

    def test_combat_end_timeline_reset_preserves_final_snapshot(self):
        self.host._in_game_combat = False
        self.host._timeline_reset_on_combat_end = True
        self.host._on_in_combat(True, True)
        self.host.hit(5000)
        self.host._on_in_combat(False, False)
        self.assertEqual(self.host.frames[0]['enc']['dps'], 5000)
        self.assertIs(self.host.frames[0]['show'], False)
        self.assertEqual(self.host.frames[1:], [{'c': 'clear', 'keepDps': True}] * 2)

    def test_normal_end_sends_final_values_and_snapshot_identity(self):
        self.host._dps_meter.set_in_combat(True, True)
        self.host.hit(5000)
        self.host._dps_meter.set_in_combat(False, False)
        self.assertEqual(len(self.host.frames), 1)
        final = self.host.frames[0]
        self.assertIs(final['show'], False)
        self.assertEqual(final['enc']['dps'], 5000)
        snapshot = self.host._dps_history[0]['snapshot']
        self.host._dps_meter.set_me('10000002')
        self.assertEqual(self.host._dps_meter.overlay_rows(snapshot), final['rows'])
        self.assertTrue(final['rows'][0][5])

    def test_legacy_wipes_restore_final_values_in_each_callout_mode(self):
        for local, global_on, cactbot in ((True, True, False), (True, False, False),
                                          (False, True, False), (False, True, True)):
            with self.subTest(local=local, global_on=global_on, cactbot=cactbot):
                host = OverlayHost(self.now, native_retention=False)
                host._local_enabled = local
                host._global_local_on_flag = global_on
                host._cactbot_mode = cactbot
                host._dps_meter.set_in_combat(True, True)
                host.hit(5000)
                host.wipe()
                clears = [i for i, f in enumerate(host.frames) if f['c'] == 'clear']
                self.assertTrue(clears)
                for i in clears:
                    self.assertEqual(host.frames[i + 1]['enc']['dps'], 5000)
                    self.assertIs(host.frames[i + 1]['show'], True)
                    self.assertEqual(host.frames[i + 2], {'c': 'dps', 'show': False})

    def test_legacy_empty_pulls_and_repeated_wipes_keep_the_last_result(self):
        host = OverlayHost(self.now, native_retention=False)
        host.hit(5000)
        host.wipe()
        host.frames.clear()
        self.now[0] += 60
        host._dps_meter.set_in_combat(True, True)
        host._dps_tick()
        self.assertEqual(host.frames, [])
        host.wipe()
        self.now[0] += 60
        host.wipe()
        live = [f for f in host.frames if f.get('show') is True]
        self.assertTrue(live)
        self.assertTrue(all(f['enc']['dps'] == 5000 for f in live))
        self.assertEqual(host.frames[-1], {'c': 'dps', 'show': False})

    def test_legacy_incoming_damage_replaces_the_held_result(self):
        host = OverlayHost(self.now, native_retention=False)
        host.hit(10000)
        host.wipe()
        host.frames.clear()
        fields = ['21', 'ts', '40000010', 'Boss', 'A1', 'Hit', '10000001',
                  'Player', '0003', f'{5000 << 16:X}'] + ['0'] * 14
        host._dispatch_log_line(fields, '|'.join(fields))
        host._dps_tick()
        self.assertEqual(len(host.frames), 1)
        self.assertIs(host.frames[0]['show'], True)
        self.assertIs(host.frames[0]['enc']['hasDamage'], True)
        self.assertEqual(host.frames[0]['rows'][0][2], 0)

    def test_legacy_timeline_cleanup_keeps_an_active_pull_live(self):
        host = OverlayHost(self.now, native_retention=False)
        host.hit(5000)
        host._dps_tick()
        host._push_timeline_to_plugin()
        self.assertEqual(host.frames[-2], {'c': 'clear', 'keepDps': True})
        self.assertIs(host.frames[-1]['show'], True)
        self.assertEqual(host.frames[-1]['enc']['dps'], 5000)

    def test_zone_boundaries_clear_both_protocols_in_either_order(self):
        for native in (True, False):
            for order in ('metadata-first', 'raw-first', 'same-name', 'metadata-only'):
                with self.subTest(native=native, order=order):
                    host = OverlayHost(self.now, native_retention=native)
                    host.prepare_zone()
                    host._local_enabled = True
                    host.hit(10000)
                    host._dps_tick()
                    if order == 'metadata-first':
                        host._on_ws_zone_changed(101, 'New Arena')
                        host.raw_zone()
                    elif order == 'raw-first':
                        host.raw_zone()
                        host._on_ws_zone_changed(101, 'New Arena')
                    elif order == 'same-name':
                        host.raw_zone('Old Arena', '64')
                    else:
                        host._on_ws_zone_changed(101, 'New Arena')
                        host._dps_meter.set_in_combat(False, False)
                    self.assertIsNone(host._dps_meter.current)
                    self.assertEqual(len(host._dps_history), 1)
                    self.assertTrue(host._dps_history[0]['snapshot']['Combatant']['Player']['is_self'])
                    boundary = max(i for i, f in enumerate(host.frames)
                                   if f['c'] == 'clear' and not f['keepDps'])
                    host._dps_tick()
                    host.wipe()
                    self.assertFalse(any(f.get('rows') for f in host.frames[boundary + 1:]))
                    self.assertTrue(any(f['c'] == 'timeline' for f in host.frames[boundary + 1:]))
                    host._dps_meter.set_me('10000001')
                    host.hit(7000)
                    host._dps_tick()
                    self.assertEqual(host.frames[-1]['enc']['dps'], 7000)

    def test_duplicate_metadata_preserves_the_current_pull(self):
        self.host.prepare_zone()
        self.host.hit(10000)
        current = self.host._dps_meter.current
        self.host._on_ws_zone_changed(100, 'Old Arena')
        self.assertIs(self.host._dps_meter.current, current)
        self.assertEqual(self.host.frames, [])

    def test_feed_disconnect_clears_after_finalizing_in_both_protocols(self):
        for native in (True, False):
            for live_tick in (True, False):
                for complete_first in (True, False):
                    with self.subTest(native=native, live_tick=live_tick,
                                      complete_first=complete_first):
                        host = OverlayHost(self.now, native_retention=native)
                        host.prepare_zone()
                        label = SimpleNamespace(setText=lambda *a: None,
                                                setStyleSheet=lambda *a: None)
                        host._status_lbl = host._conn_btn = label
                        host.hit(10000)
                        if live_tick:
                            host._dps_tick()
                        if complete_first:
                            host.wipe()
                        host._on_status_changed(False, 'Feed lost')
                        self.assertIsNone(host._dps_meter.current)
                        self.assertEqual(len(host._dps_history), 1)
                        snapshot = host._dps_history[0]['snapshot']
                        self.assertEqual(snapshot['Encounter']['damage'], 10000)
                        self.assertTrue(snapshot['Combatant']['Player']['is_self'])
                        boundary = max(i for i, f in enumerate(host.frames)
                                       if f['c'] == 'clear' and not f['keepDps'])
                        host._dps_tick()
                        host.wipe()
                        host._on_status_changed(True, 'Reconnected')
                        self.assertFalse(any(f.get('rows') for f in host.frames[boundary + 1:]))


if __name__ == '__main__':
    unittest.main()
