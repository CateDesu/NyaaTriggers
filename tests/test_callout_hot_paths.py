import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

from PyQt6.QtCore import QThread

from nyaatriggers import record_store
from nyaatriggers.prog_session import ProgSessions
from nyaatriggers.record_store import RecordWriter, read_record, write_record
from nyaatriggers.trigger_engine import Trigger
from nyaatriggers.triggevent_bridge import TriggeventBridge
from nyaatriggers.triggernometry_bridge import TriggernometryBridge
from nyaatriggers.ui import instance_tab
from tests import test_session_ui
from tests.test_session_features import PLAYER, ability


class HotPathTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        test_session_ui.SessionUiTests.setUpClass()

    def setUp(self):
        self.fixture = test_session_ui.SessionUiTests('test_window_icon_loads_from_bundled_assets')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.window = self.fixture.window

    def test_dispatch_rejects_irrelevant_events_before_cached_zone_matching(self):
        window = self.window
        window._local_enabled = window._connected = True
        window._awaiting_zone_metadata = False
        window._zone_aliases = ('Arena',)
        active = Trigger(log_type='20|21', ability_id='CAFE', zone_regex='Arena',
                         tts_text='Out', cooldown_s=0)
        window._triggers = [Trigger(log_type='20', ability_id='CAFE', zone_regex='Other')
                            for _ in range(2000)] + [active]
        instance_tab._zone_pattern_matches.cache_clear()
        fields = ['20', 'ts', '40000001', 'Boss', 'BEEF', 'Cast', PLAYER, 'Player']
        with patch.object(instance_tab, '_safe_search', wraps=instance_tab._safe_search) as search, \
                patch('nyaatriggers.main_window.speak') as speech:
            window._dispatch_log_line(fields, '|'.join(fields))
            search.assert_not_called()
            fields[4] = 'CAFE'
            for _ in range(4):
                window._dispatch_log_line(fields, '|'.join(fields))
            self.assertEqual(search.call_count, 2)
            self.assertEqual(speech.call_count, 4)
            self.assertTrue(all(not t._last_fired for t in window._triggers[:-1]))

        active.zone_regex = 'Other'
        self.assertFalse(window._trigger_zone_matches(active))
        window._zone_aliases = ('Other',)
        self.assertTrue(window._trigger_zone_matches(active))
        window._zone_aliases = ('Arena',)
        window._awaiting_zone_metadata = True
        self.assertTrue(window._trigger_zone_matches(active))
        window._awaiting_zone_metadata = False
        self.assertFalse(window._trigger_zone_matches(active))

    def test_queued_native_callouts_keep_repeated_phrases(self):
        window = self.window
        window._connected = window._triggevent_mode = window._triggernometry_mode = True
        for kind, cls in (('triggevent', TriggeventBridge), ('triggernometry', TriggernometryBridge)):
            with self.subTest(engine=kind):
                bridge = cls(window)
                bridge._active, bridge._gen = True, 1
                setattr(window, '_' + kind, bridge)
                bridge.tts.connect(getattr(window, '_on_' + kind + '_tts'))
                errors = []

                def reader():
                    try:
                        for seq in (1, 2):
                            message = {'t': 'callout', 'id': str(seq), 'seq': seq,
                                       'tts': 'Out', 'tts_only': True}
                            bridge._dispatch(message, gen=1)
                    except Exception as exc:
                        errors.append(exc)

                with patch('nyaatriggers.ui.engines.speak') as speech:
                    worker = threading.Thread(target=reader)
                    worker.start()
                    worker.join(5)
                    self.assertFalse(worker.is_alive())
                    self.assertEqual(errors, [])
                    speech.assert_not_called()
                    self.fixture.app.processEvents()
                    self.assertEqual(speech.call_count, 2)
                bridge._active = False

    def test_slow_recap_storage_does_not_block_dispatch_and_close_flushes(self):
        window = self.window
        self.fixture.connect()
        window._prog_tab.start_button.click()
        window._on_in_combat(True, True)
        self.fixture.line(ability())
        sessions = window._prog_sessions
        sessions.poll_saves(wait=True)
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        actual = record_store.os.fsync
        threads = []

        def stalled(fd):
            threads.append(QThread.currentThread() == self.fixture.app.thread())
            entered.set()
            if not release.wait(5):
                raise OSError('Test writer was not released')
            actual(fd)

        with patch.object(record_store.os, 'fsync', side_effect=stalled):
            self.fixture.line(['25', 'ts', PLAYER, 'Player'])
            self.assertTrue(entered.wait(5))
            self.assertFalse(release.is_set())
            self.fixture.line(ability())
            self.assertEqual(window._recap_list.count(), 1)
            release.set()
            window.close()
        self.assertTrue(threads)
        self.assertFalse(any(threads))
        saved = ProgSessions(self.fixture.temp / 'prog_sessions')
        session = saved.sessions[0]
        pull = session['pulls'][0]
        self.assertEqual(pull['recap_count'], 1)
        recaps, errors = saved.recaps.load(session['id'], pull['id'])
        self.assertEqual(errors, [])
        self.assertEqual(len(recaps), 1)
        self.assertEqual(session['state'], 'ended')

    def test_writer_coalesces_snapshots_and_ignores_superseded_failure(self):
        writer = RecordWriter()
        self.addCleanup(writer.close)
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        directory = self.fixture.temp / 'writer'
        record = {'version': 1, 'id': str(uuid4()), 'value': 'old'}
        completed = []

        def stalled(directory, snapshot):
            entered.set()
            if not release.wait(5):
                raise OSError('Test writer was not released')
            raise OSError('Old write failed')

        writer.submit(directory, record, stalled, lambda error: completed.append(('old', error)))
        self.assertTrue(entered.wait(5))
        record['value'] = 'middle'
        writer.submit(directory, record, write_record, lambda error: completed.append(('middle', error)))
        record['value'] = 'latest'
        writer.submit(directory, record, write_record, lambda error: completed.append(('latest', error)))
        record['value'] = 'not submitted'
        release.set()
        writer.close()
        self.assertEqual(completed, [('latest', None)])
        self.assertEqual(read_record(directory / (record['id'] + '.json'))['value'], 'latest')

    def test_queued_late_recap_remains_reachable_if_final_summary_fails(self):
        window = self.window
        self.fixture.connect()
        window._prog_tab.start_button.click()
        sessions = window._prog_sessions
        sessions.poll_saves(wait=True)
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def stalled(directory, data):
            entered.set()
            if not release.wait(5):
                raise OSError('Test writer was not released')

        def save_summary(directory, data):
            if any(pull.get('recap_count') for pull in data['pulls']):
                raise OSError('Final summary failed')
            write_record(directory, data)

        sessions.writer.submit(self.fixture.temp / 'blocker', {'id': str(uuid4())},
                               stalled, lambda error: None)
        self.assertTrue(entered.wait(5))
        with patch('nyaatriggers.prog_session.write_record', side_effect=save_summary):
            window._on_in_combat(True, True)
            self.fixture.line(ability(pairs=[('33', '0')]))
            window._on_in_combat(False, False)
            self.assertEqual(sessions.current['pulls'], [])
            self.fixture.line(['25', 'ts', PLAYER, 'Player'])
            release.set()
            sessions.poll_saves(wait=True)

        self.assertIn('Final summary failed', sessions.save_error)
        restored = ProgSessions(sessions.directory)
        session = restored.sessions[0]
        self.assertEqual(len(session['pulls']), 1)
        pull = session['pulls'][0]
        recaps, errors = restored.recaps.load(session['id'], pull['id'])
        self.assertEqual(errors, [])
        self.assertEqual(len(recaps), 1)
        self.assertEqual(pull['recap_count'], 1)
        self.assertIn(session['id'], sessions.unsaved)

    def test_failed_async_saves_remain_visible_and_retry(self):
        sessions = self.window._prog_sessions
        with patch('nyaatriggers.prog_session.write_record', side_effect=OSError('Disk failed')):
            session = sessions.start('Raid', 1, 'Duty', False)
            sessions.poll_saves(wait=True)
            self.assertIn('Disk failed', sessions.save_error)
            self.assertIn(session['id'], sessions.unsaved)
        sessions.flush_pending(wait=True)
        self.assertFalse(sessions.save_error)
        self.assertFalse(sessions.unsaved)
        self.assertEqual(read_record(sessions.directory / (session['id'] + '.json'))['name'], 'Raid')
        sessions.end()
        sessions.poll_saves(wait=True)
        with patch('nyaatriggers.prog_session.write_record', side_effect=OSError('Archive failed')):
            self.assertFalse(sessions.set_archived(session, True))
            self.assertNotIn('archived', session)
            self.assertIn('Archive failed', sessions.save_error)
        sessions.flush_pending(wait=True)
        self.assertNotIn('archived', read_record(sessions.directory / (session['id'] + '.json')))

    def test_writer_bounds_backlog_and_keeps_latest_record_after_earlier_writes(self):
        writer = RecordWriter()
        self.addCleanup(writer.close)
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        order, completions = [], []
        directory = self.fixture.temp / 'writer'

        def write(directory, data):
            entered.set()
            if not release.wait(5):
                raise OSError('Test writer was not released')
            order.append(data['id'])

        records = [{'id': str(uuid4())} for _ in range(257)]
        complete = completions.append
        writer.submit(directory, records[0], write, complete)
        self.assertTrue(entered.wait(5))
        for record in records[1:256]:
            writer.submit(directory, record, write, complete)
        with self.assertRaisesRegex(OSError, 'queue is full'):
            writer.submit(directory, records[256], write, complete)
        writer.submit(directory, records[0], write, complete)
        with self.assertRaisesRegex(OSError, 'queue is full'):
            writer.submit(directory, records[1], write, complete)
        release.set()
        writer.close()
        self.assertEqual(order, [r['id'] for r in records[:256]] + [records[0]['id']])
        self.assertEqual(completions, [None] * 256)

    def test_older_success_cannot_clear_a_new_validation_failure(self):
        sessions = self.window._prog_sessions
        session = sessions.start('Raid', 1, 'Duty', False)
        session['name'] = None
        self.assertFalse(sessions.save(session))
        sessions.poll_saves(wait=True)
        self.assertIn('Invalid session name', sessions.save_error)
        self.assertIn(session['id'], sessions.unsaved)
        session['name'] = 'Repaired'
        sessions.flush_pending(wait=True)
        self.assertFalse(sessions.save_error)


if __name__ == '__main__':
    unittest.main()
