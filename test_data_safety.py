"""Regression tests for persisted data and encounter lifecycle boundaries."""

import contextlib
import io
import json
import os
from pathlib import Path
import queue
import subprocess
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import zipfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt6.QtCore import QObject
from PyQt6.QtWidgets import QApplication

import app_common as ac
from convert_event_trigger import parse_hex_ids
from dps_meter import DpsMeter
import fflogs
import plugin_link
from pull_capture import PullCapture
from telesto_client import TelestoClient
from timeline_engine import TimelineEngine
from timeline_parser import parse
from tools import extract_strings
from tools import gen_callout_stub
from triggernometry_bridge import _ByteQueue as TriggernometryQueue
from triggevent_bridge import _ByteQueue as TriggeventQueue
import tts
from ui.dps_tab import DpsTabMixin
from ui.settings_tab import SettingsTabMixin
from ui.triggers_tab import TriggersTabMixin
import updater

_app = QApplication.instance() or QApplication([])


class TriggerHost(QObject, TriggersTabMixin):
    def __init__(self):
        super().__init__()
        self._triggers = []

    def _refresh_table(self):
        pass


def ability(flags, amount, source="10000001"):
    return ["21", "ts", source, "Player", "A1", "Ability", "40000001",
            "Enemy", flags, f"{amount << 16:X}"] + ["0"] * 39


class DataSafetyTests(unittest.TestCase):
    def test_settings_cap_preserves_oversized_file_for_recovery(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'settings.json'
            content = json.dumps({'ws_url': 'x' * 64})
            path.write_text(content)
            host = SimpleNamespace(_settings={})
            with patch.object(ac, '_SETTINGS_FILE', path), \
                 patch('ui.settings_tab._MAX_SETTINGS_BYTES', 32):
                SettingsTabMixin._load_settings(host)
            self.assertEqual(host._settings, {})
            self.assertEqual(path.read_text(), content)
            reason, backup = host._settings_load_warning
            self.assertIn('exceeds', reason)
            self.assertEqual(backup.read_text(), content)

    def test_concurrent_queue_producers_respect_byte_budget(self):
        original_put = queue.Queue.put_nowait
        for cls in (TriggeventQueue, TriggernometryQueue):
            with self.subTest(queue=cls.__module__):
                q = cls(maxsize=10, maxbytes=3)
                barrier = threading.Barrier(2)
                outcomes = []

                def put_together(instance, item):
                    barrier.wait(timeout=2)
                    return original_put(instance, item)

                def produce():
                    try:
                        q.put_nowait('ab')
                        outcomes.append('sent')
                    except queue.Full:
                        outcomes.append('full')

                with patch.object(queue.Queue, 'put_nowait', put_together):
                    threads = [threading.Thread(target=produce) for _ in range(2)]
                    for thread in threads:
                        thread.start()
                    for thread in threads:
                        thread.join(3)
                        self.assertFalse(thread.is_alive())
                self.assertCountEqual(outcomes, ['sent', 'full'])
                self.assertEqual(q.unfinished_tasks, 1)
                self.assertEqual(q.get_nowait(), 'ab')
                self.assertEqual(q._nbytes, 0)
                self.assertTrue(q.empty())

    def test_capture_and_metadata_are_private_at_creation(self):
        with tempfile.TemporaryDirectory() as folder:
            recorder = PullCapture(Path(folder))
            old_umask = os.umask(0o022)
            try:
                recorder._begin()
                recorder._write('{"raw": "party names"}')
                path = recorder._path
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                recorder._finalize('ended')
                self.assertEqual(path.with_suffix('.meta.json').stat().st_mode & 0o777, 0o600)
            finally:
                os.umask(old_umask)
                recorder._finalize('ended')

    def test_callout_stub_failure_keeps_previous_file_and_warns_on_duplicate(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / 'triggers.json'
            source.write_text(json.dumps([
                {'id': 'same', 'tts_text': 'First'},
                {'id': 'same', 'tts_text': 'Second'}]))
            out = Path(folder) / 'template.json'
            out.write_text('previous template')
            warnings = io.StringIO()
            with patch.object(gen_callout_stub, '_TRIGGERS', source), \
                 patch.object(gen_callout_stub.os, 'replace', side_effect=OSError('disk failed')), \
                 contextlib.redirect_stderr(warnings):
                with self.assertRaises(OSError):
                    gen_callout_stub.main(['--out', str(out)])
            self.assertIn('duplicate trigger ID', warnings.getvalue())
            self.assertEqual(out.read_text(), 'previous template')
            self.assertEqual(list(Path(folder).glob('*.tmp')), [])

    def test_fflogs_concurrent_fetches_share_cache_and_refresh_token(self):
        for shared_client in (True, False):
            with self.subTest(shared_client=shared_client), \
                 patch.object(fflogs.FflogsClient, '_zones_cache', None):
                counts = {'tokens': 0, 'zones': 0, 'rankings': 0}
                count_lock = threading.Lock()

                def http(url, headers, body, timeout):
                    time.sleep(0.01)
                    with count_lock:
                        if url == fflogs.TOKEN_URL:
                            counts['tokens'] += 1
                            data = {'access_token': f"token{counts['tokens']}", 'expires_in': 3600}
                        elif 'worldData' in json.loads(body)['query']:
                            counts['zones'] += 1
                            data = {'data': {'worldData': {'zones': [{'id': 1, 'name': 'Fight'}]}}}
                        else:
                            counts['rankings'] += 1
                            if shared_client and counts['rankings'] == 1:
                                return 401, b'{}'
                            self.assertNotEqual(headers['Authorization'], 'Bearer None')
                            data = {'data': {'characterData': {'character': {
                                'zoneRankings': {'rankPercent': 90, 'bestAmount': 123}}}}}
                        return 200, json.dumps(data).encode()

                first = fflogs.FflogsClient('id', 'secret', http_post=http)
                second = first if shared_client else fflogs.FflogsClient('id', 'secret', http_post=http)
                barrier = threading.Barrier(2)
                results = []

                def fetch(client):
                    barrier.wait(timeout=2)
                    results.append(client.fetch_best('Player', 'server', 'eu', 'Fight'))

                threads = [threading.Thread(target=fetch, args=(client,)) for client in (first, second)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(3)
                    self.assertFalse(thread.is_alive())
                self.assertEqual(results, [{'percent': 90, 'amount': 123, 'zone': 'Fight'}] * 2)
                self.assertEqual(counts['zones'], 1)
                self.assertEqual(counts['tokens'], 2)

    def test_corrupt_local_save_preserves_text_and_warns_once(self):
        with tempfile.TemporaryDirectory() as folder, contextlib.ExitStack() as stack:
            root = Path(folder)
            for name in ("TRIGGERS_FILE", "TRIGGERS_LOCAL_FILE", "RETIRED_FILE",
                         "_REPO_TRIGGERS_FILE", "_REPO_RETIRED_FILE", "_REPO_TRIGGERS_VERSION"):
                stack.enter_context(patch.object(ac, name, root / name))
            warning = stack.enter_context(patch.object(ac.QMessageBox, "warning"))
            ac.TRIGGERS_FILE.write_text('[]')
            ac.TRIGGERS_LOCAL_FILE.write_text('{"triggers": []}')
            original = ac.TRIGGERS_LOCAL_FILE.stat()
            host = TriggerHost()
            host._load_triggers()
            broken = '{"triggers": [recoverable hand edit'
            ac.TRIGGERS_LOCAL_FILE.write_text(broken)
            host._maybe_reload_triggers()
            warning.assert_not_called()
            host._save_triggers()
            host._save_triggers()
            host._maybe_reload_triggers()
            self.assertTrue(host._local_corrupt)
            self.assertEqual(ac.TRIGGERS_LOCAL_FILE.read_text(), broken)
            self.assertEqual(warning.call_count, 1)
            backups = [p for p in root.iterdir() if '.bad' in p.name]
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_text(), broken)
            ac.TRIGGERS_LOCAL_FILE.write_text('{"triggers": []}')
            os.utime(ac.TRIGGERS_LOCAL_FILE,
                     ns=(original.st_atime_ns, original.st_mtime_ns))
            self.assertEqual(host._trigger_files_stamp(), host._triggers_mtime)
            host._maybe_reload_triggers()
            self.assertFalse(host._local_corrupt)
            host._save_triggers()
            self.assertEqual(json.loads(ac.TRIGGERS_LOCAL_FILE.read_text())["triggers"], [])

    def test_corrupt_repo_override_does_not_block_reload(self):
        with tempfile.TemporaryDirectory() as folder, contextlib.ExitStack() as stack:
            root = Path(folder)
            for name in ("TRIGGERS_FILE", "TRIGGERS_LOCAL_FILE", "RETIRED_FILE",
                         "_REPO_TRIGGERS_FILE", "_REPO_RETIRED_FILE", "_REPO_TRIGGERS_VERSION"):
                stack.enter_context(patch.object(ac, name, root / name))
            ac.TRIGGERS_FILE.write_text('[]')
            ac.TRIGGERS_LOCAL_FILE.write_text('{"triggers": []}')
            ac._REPO_TRIGGERS_VERSION.write_text(json.dumps(ac._VERSION))
            host = TriggerHost()
            host._load_triggers()
            ac._REPO_TRIGGERS_FILE.write_text('corrupt')
            ac.TRIGGERS_FILE.write_text('[{"id": "new", "name": "Bundled update"}]')
            host._maybe_reload_triggers()
            self.assertEqual([t.id for t in host._triggers], ["new"])
            self.assertEqual(host._triggers_mtime, host._trigger_files_stamp())

    def test_unreadable_catalog_is_never_rewritten(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'lang').mkdir()
            (root / 'program.py').write_text('_("Hello")\n')
            path = root / 'lang/ja.json'
            with patch.object(extract_strings, '_REPO', root), \
                    contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                for broken in ['{"Hello": "こんにちは",}', '[]']:
                    path.write_text(broken)
                    for args in [[], ['--check'], ['--prune']]:
                        self.assertEqual(extract_strings.main(args), 1)
                        self.assertEqual(path.read_text(), broken)
                path.write_text('{"Hello": "こんにちは"}')
                self.assertEqual(extract_strings.main(['--prune']), 0)
                self.assertEqual(json.loads(path.read_text()), {"Hello": "こんにちは"})

    def test_blank_venv_uses_home_default(self):
        with tempfile.TemporaryDirectory() as folder, contextlib.ExitStack() as stack:
            root = Path(folder)
            default = root / '.venv/ffxiv'
            (default / 'bin').mkdir(parents=True)
            (default / 'bin/python').touch()
            stack.enter_context(patch.object(Path, 'home', return_value=root))
            for name, value in {'_FFXIV_VENV': None, '_venv_sps': [],
                                '_stale_venv_sps': set(), '_piper_voice': None,
                                '_piper_failed': False, '_piper_epoch': 0}.items():
                stack.enter_context(patch.object(tts, name, value))
            stack.enter_context(patch.object(tts, '_purge_stale_venv_modules'))
            tts.set_venv_path('  ')
            self.assertEqual(tts._FFXIV_VENV, default)
            self.assertEqual(tts._venv_python(), str(default / 'bin/python'))

    def test_map_effect_and_battle_talk_select_branches(self):
        for text, fields, destination in [
            ('10 "--sync--" MapEffect { flags: "00020001", location: "1B" } jump 100',
             ['257', 'ts', 'instance', '00020001', '1B'], 100),
            ('10 "--sync--" BattleTalk2 { instanceContentTextId: "484E", npcNameId: "123" } jump 200',
             ['267', 'ts', 'npc', 'instance', '123', '484E'], 200),
        ]:
            engine = TimelineEngine()
            engine.load(parse(text))
            engine.start()
            engine._t0 = time.monotonic() - 10
            wrong = list(fields)
            wrong[-1] = 'wrong'
            engine.process_line(wrong)
            self.assertAlmostEqual(engine.current_time(), 10, delta=.1)
            engine.process_line(fields)
            self.assertAlmostEqual(engine.current_time(), destination, delta=.1)
            engine.reset()

    def test_mixed_radix_ids_and_legacy_hash(self):
        self.assertEqual(parse_hex_ids('value = {35905, 0x1, 0x8C42}, suppressMs=999999'),
                         ['8C41', '8C42'])
        self.assertEqual(parse_hex_ids('value = {35_905, 0x8C42}, suppressMs=0x8C43'),
                         ['8C41', '8C42'])
        self.assertEqual(parse_hex_ids('suppressMs=35905'), [])
        self.assertEqual(parse_hex_ids('{0x007D, 35905}'), ['007D', '8C41'])
        self.assertEqual(parse_hex_ids('{0x_}'), [])
        entry = parse('5 "x" sync /say #hi \\/ there/ window 30,30 Ability { id: "8C01" } # comment')[0]
        self.assertTrue(entry.legacy_sync)
        self.assertEqual(entry.event_type, 'Ability')
        self.assertEqual(entry.window_before, 30)

    def test_healing_pauses_only_in_the_display(self):
        now = [1000.0]
        meter = DpsMeter(clock=lambda: now[0])
        meter.set_idle_timeout(15)
        meter.set_me('10000001')
        meter.process(ability('3', 100))
        now[0] += 1
        meter.process(ability('4', 300))
        now[0] += 30
        before = meter.snapshot()['Combatant']['Player']['enchps']
        meter.process(ability('4', 100))
        hot = ['24', 'ts', '10000001', 'Player', 'HoT', '0', '64'] + ['0'] * 10
        hot += ['10000001', 'Player']
        meter.process(hot)
        self.assertEqual(meter.snapshot()['Combatant']['Player']['enchps'], before)
        self.assertEqual(meter.overlay_rows()[0][4], before)
        meter.finalize()
        self.assertEqual(meter.snapshot()['Combatant']['Player']['healed'], 500)

    def test_roster_survives_player_churn(self):
        meter = DpsMeter()
        party = 0x10000002
        meter.note_job(party, 31)
        for i in range(1100):
            meter.process(['03', 'ts', f'{0x10001000 + i:X}', 'Passerby', '21', '5A', '0'])
        meter.process(ability('3', 100, source=f'{party:X}'))
        self.assertEqual(meter.snapshot()['Encounter']['damage'], 100)
        self.assertLessEqual(len(meter._jobs), 1024)
        meter.feed_lost()
        self.assertFalse(meter._is_player(party))

    def test_empty_pull_ends_overlay_without_recording(self):
        meter = DpsMeter()
        meter.set_me('10000001')
        frames = []
        host = SimpleNamespace(
            _update_live_dps=lambda: None, _dps_meter=meter, _dps_overlay_live=False,
            _plugin_link=SimpleNamespace(is_connected=lambda: True,
                                        send_dps=lambda *a, **kw: frames.append(kw['show'])))
        ended = Mock()
        meter.on_encounter_end = ended
        meter.process(ability('1', 0))
        DpsTabMixin._dps_tick(host)
        meter.finalize()
        DpsTabMixin._dps_tick(host)
        DpsTabMixin._dps_tick(host)
        self.assertEqual(frames, [True, False])
        ended.assert_not_called()

    def test_quit_waits_for_earlier_writer(self):
        release = threading.Event()
        finished = threading.Event()
        earlier = threading.Thread(target=lambda: (release.wait(3), finished.set()))
        newer = threading.Thread(target=lambda: None)
        earlier.start()
        newer.start()
        newer.join()
        host = SimpleNamespace(_dps_meter=SimpleNamespace(current=None),
                               _dps_write_threads=[earlier, newer])
        quit_done = threading.Event()
        quitting = threading.Thread(target=lambda: (DpsTabMixin._finalize_live_encounter(host),
                                                     quit_done.set()))
        quitting.start()
        try:
            self.assertFalse(quit_done.wait(.05))
        finally:
            release.set()
            quitting.join(3)
            earlier.join(3)
        self.assertTrue(quit_done.is_set() and finished.is_set())

    def test_retention_keeps_the_new_capture_after_clock_rollback(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for i in range(20):
                (root / f'2026-09-10_{i:02}.jsonl').touch()
            current = root / '2026-09-09_current.jsonl'
            current.write_text('new capture')
            current.with_suffix('.meta.json').write_text('metadata')
            PullCapture._prune(None, root, keep=current)
            self.assertEqual(current.read_text(), 'new capture')
            self.assertTrue(current.with_suffix('.meta.json').exists())
            self.assertEqual(len(list(root.glob('*.jsonl'))), 20)

    def test_renamed_windows_exe_survives_staging_and_swap(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            install = root / 'program'
            install.mkdir()
            (install / '_internal').mkdir()
            (install / '_internal/old').touch()
            (install / 'Cats.exe').write_text('old')
            archive = root / 'update.zip'
            with zipfile.ZipFile(archive, 'w') as z:
                z.writestr('NyaaTriggers/NyaaTriggers.exe', 'new')
                z.writestr('NyaaTriggers/_internal/python3.dll', 'new runtime')
            with patch.object(updater.subprocess, 'Popen', return_value=SimpleNamespace(poll=lambda: None)) as launch, \
                    patch.object(updater.time, 'sleep'):
                ok, message = updater.apply_frozen_windows(archive, install, 'Cats.exe', version='9.8.7.6')
            self.assertTrue(ok, message)
            cmd = launch.call_args.args[0]
            staged = Path(cmd[0])
            self.assertEqual(staged.name, 'Cats.exe')
            self.assertEqual(staged.read_text(), 'new')
            self.assertEqual((staged.parent / updater._STAGED_VERSION_NAME).read_text(), '9.8.7.6')
            self.assertEqual(updater._mark_rejected(install, staged.parent), '9.8.7.6')
            with patch.object(updater, '_wait_for_pid_exit', return_value=True), \
                    patch.object(updater, '_relaunch_and_verify', return_value=True):
                updater.finish_windows_update(install, staged.parent, 123, 'Cats.exe')
            self.assertEqual((install / 'Cats.exe').read_text(), 'new')
            self.assertFalse((install / 'NyaaTriggers.exe').exists())

    def test_bad_windows_exe_name_is_rejected_before_waiting(self):
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(updater, '_wait_for_pid_exit') as wait:
            root = Path(folder)
            for name in ['../outside.exe', '..\\outside.exe', 'C:\\outside.exe', 'C:outside.exe']:
                updater.finish_windows_update(root, root.parent / '.nyaa-update-test', 123, name)
            wait.assert_not_called()

    def test_release_cache_survives_failed_publish(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'release.json'
            old = '{"tag":"old"}'
            path.write_text(old)
            release = updater.Release(tag='new', version='2', html_url='unused')
            with patch.object(updater, '_release_cache_path', return_value=path), \
                    patch.object(updater.os, 'replace', side_effect=OSError('write failed')):
                updater._write_release_cache(release)
            self.assertEqual(path.read_text(), old)
            self.assertEqual(list(Path(folder).iterdir()), [path])

    def test_notification_exit_failure_is_logged(self):
        result = subprocess.CompletedProcess(['aplay'], 1, stderr=b'audio device busy')
        with patch.object(tts.platform, 'system', return_value='Linux'), \
                patch.object(tts, '_wav_seconds', return_value=1), \
                patch.object(tts.subprocess, 'run', return_value=result), \
                patch.object(tts, 'log_drop') as log:
            tts._play_wav_detached('unused.wav')
            tts._play_wav_bytes_detached(b'unused')
            self.assertEqual(log.call_count, 2)
            self.assertTrue(all('audio device busy' in call.args[1] for call in log.call_args_list))


class WorkerLifecycleTests(unittest.TestCase):
    def test_old_plugin_connect_cannot_publish_or_consume_replacement_frames(self):
        for fail in [False, True]:
            with self.subTest(fail=fail):
                link = plugin_link.PluginLink()
                first_entered, second_entered = threading.Event(), threading.Event()
                first_release, second_release = threading.Event(), threading.Event()
                delivered = threading.Event()
                closed = []
                sent = []
                calls = []

                def connect():
                    generation = len(calls)
                    calls.append(generation)
                    entered, release = ((first_entered, first_release) if generation == 0
                                        else (second_entered, second_release))
                    entered.set()
                    release.wait(3)
                    if generation == 0 and fail:
                        raise RuntimeError('old connect failed')
                    ws = SimpleNamespace(close=lambda: closed.append(generation))
                    return ws, str(generation)

                def send(ws, frame):
                    sent.append(frame)
                    delivered.set()

                link._connect = connect
                link._send = send
                link._drain_inbound = lambda *a: None
                link.start()
                old_thread = link._thread
                try:
                    self.assertTrue(first_entered.wait(2))
                    old_queue = link._queue
                    link.stop(join_timeout=0)
                    link.start()
                    self.assertTrue(second_entered.wait(2))
                    link.send_alert('new alert')
                    self.assertIsNot(old_queue, link._queue)
                    first_release.set()
                    old_thread.join(2)
                    self.assertFalse(old_thread.is_alive())
                    self.assertFalse(link.is_connected())
                    self.assertNotIn('old connect failed', link.last_status()[1])
                    self.assertEqual(link._queue.qsize(), 1)
                    self.assertEqual(sent, [])
                    second_release.set()
                    self.assertTrue(delivered.wait(2))
                    self.assertEqual(len(sent), 1)
                    self.assertEqual(link.plugin_version(), '1')
                finally:
                    first_release.set()
                    second_release.set()
                    link.stop()
                    old_thread.join(3)

    def test_telesto_disable_discards_old_marks_but_keeps_forced_clears(self):
        for reenable in [False, True]:
            with self.subTest(reenable=reenable):
                client = TelestoClient(enabled=True, delay_base_ms=0, delay_plus_ms=0)
                sent = []
                delivered = threading.Event()
                client._post = lambda msg: (sent.append(msg), delivered.set())
                client.send_game_command('/mk attack1 <me>')
                client.send_game_command('/mk clear <me>', force=True)
                client.set_enabled(False)
                if reenable:
                    client.set_enabled(True)
                client.start()
                try:
                    self.assertTrue(delivered.wait(2))
                finally:
                    client.stop()
                self.assertEqual(len(sent), 1)
                self.assertEqual(sent[0]['payload']['command'], '/mk clear <me>')


if __name__ == '__main__':
    unittest.main()
