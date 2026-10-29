"""Concurrency and filesystem regressions for conservative optimization."""
from datetime import datetime
from pathlib import Path
import tempfile
import threading
import os
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from engine import DEFAULTS, Recorder, Segment, choose_output_pattern
from mp3_stream import MP3AppendFile, MP3FormatError


class NamingOptimizationTests(unittest.TestCase):
    def test_hourly_selection_avoids_thousands_of_stat_calls(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(Path, 'exists', autospec=True, side_effect=Path.exists) as exists:
                result = choose_output_pattern(Path(folder), '%d-%m-%Y_%H-%M',
                                               datetime(2026, 10, 29, 12, 15), 60)
            self.assertEqual(result, '%d-%m-%Y_%H-%M.mp3')
            self.assertLess(exists.call_count, 10)

    def test_directory_target_cannot_be_written_or_select_suffix(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / '29-10-2026_13-00.cue').mkdir()
            directory = root / '29-10-2026_13-00.mp3'
            directory.mkdir()
            marker = directory / 'keep.txt'
            marker.write_bytes(b'archive')
            self.assertEqual(choose_output_pattern(root, '%d-%m-%Y_%H-%M',
                             datetime(2026, 10, 29, 12, 15), 60),
                             '%d-%m-%Y_%H-%M.mp3')
            with self.assertRaises(OSError):
                MP3AppendFile(directory, 48000, 1)
            self.assertEqual(marker.read_bytes(), b'archive')
            self.assertTrue((root / '29-10-2026_13-00.cue').is_dir())

    def test_symlink_target_cannot_be_written_or_select_suffix(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            target = root / 'target'
            target.write_bytes(b'archive')
            link = root / '29-10-2026_13-00.mp3'
            try:
                link.symlink_to(target)
            except OSError:
                self.skipTest('symlinks unavailable')
            self.assertEqual(choose_output_pattern(root, '%d-%m-%Y_%H-%M',
                             datetime(2026, 10, 29, 12, 15), 60),
                             '%d-%m-%Y_%H-%M.mp3')
            with self.assertRaises(MP3FormatError):
                MP3AppendFile(link, 48000, 1)
            self.assertTrue(link.is_symlink())
            self.assertEqual(target.read_bytes(), b'archive')

    def test_existing_uppercase_extension_does_not_select_suffix(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            existing = root / '29-10-2026_13-00.MP3'
            existing.write_bytes(b'archive')
            self.assertEqual(choose_output_pattern(root, '%d-%m-%Y_%H-%M',
                             datetime(2026, 10, 29, 12, 15), 60),
                             '%d-%m-%Y_%H-%M.mp3')
            self.assertEqual(existing.read_bytes(), b'archive')


class WatchFailureTests(unittest.TestCase):
    def test_terminate_error_still_kills_and_finalizes_cue(self):
        recorder = Recorder(lambda _: None)
        recorder.running = True
        recorder.cfg = {'folder': '.', 'retention_enabled': False}
        process = Mock()
        process.poll.return_value = None
        process.terminate.side_effect = OSError('terminate failed')
        recorder.process = process
        with patch.object(recorder, '_start_cleanup_worker'), patch.object(
                recorder, '_watch_loop', side_effect=OSError('disk failed')), patch.object(
                recorder, '_sync_output') as sync, patch.object(
                recorder, '_update_current_cue') as cue:
            recorder._watch()
        process.kill.assert_called_once()
        sync.assert_called_once()
        cue.assert_called_once()
        self.assertFalse(recorder.running)

    def test_failed_kill_keeps_process_owned_for_retry(self):
        recorder = Recorder(lambda _: None)
        recorder.running = True
        process = Mock()
        process.poll.return_value = None
        process.terminate.side_effect = OSError('terminate failed')
        process.kill.side_effect = OSError('kill failed')
        recorder.process = process
        with self.assertRaisesRegex(RuntimeError, 'FFmpeg'):
            recorder.stop()
        self.assertIs(recorder.process, process)

    def test_watch_error_stops_encoder_and_clears_running(self):
        logs = []
        recorder = Recorder(logs.append)
        recorder.cfg = {'folder': '.', 'retention_enabled': False}
        recorder.running = True
        process = Mock()
        process.poll.return_value = None
        recorder.process = process
        with patch('engine.ensure_disk_space'), patch.object(
                recorder, '_sync_output', side_effect=OSError('disk unavailable')):
            recorder._watch()
        self.assertFalse(recorder.running)
        self.assertTrue(recorder.stop_requested.is_set())
        process.terminate.assert_called_once()
        process.wait.assert_called()
        self.assertTrue(any('disk unavailable' in line for line in logs))

    def test_stop_timeout_does_not_allow_reusing_live_watcher(self):
        recorder = Recorder(lambda _: None)
        recorder.running = True
        recorder.cfg = {'folder': '.'}
        recorder.watch_thread = Mock()
        recorder.watch_thread.is_alive.return_value = True
        with self.assertRaisesRegex(RuntimeError, 'заверш'):
            recorder.stop()
        # A timed-out watch worker still owns the old session. A new start
        # must fail rather than silently return success or replace its process.
        with self.assertRaisesRegex(RuntimeError, 'заверш'):
            recorder.start({'source': 'input'})


class CleanupConcurrencyTests(unittest.TestCase):
    def make_recorder(self, root):
        recorder = Recorder(lambda _: None)
        recorder.cfg = {'folder': str(root), 'retention_enabled': True,
                        'retention_days': 90}
        recorder.running = True
        return recorder

    def test_retention_saved_during_start_is_used_before_watch_begins(self):
        recorder = Recorder(lambda _: None)
        seen = []
        with tempfile.TemporaryDirectory() as folder:
            config = dict(DEFAULTS, folder=folder, source='input', retention_days=90)
            def save_new_policy(_):
                recorder.update_retention(True, 365)
            with patch('engine.ffmpeg_path', return_value='ffmpeg'), patch(
                    'engine.writable_recording_folder', side_effect=save_new_policy), patch(
                    'engine.ensure_disk_space'), patch.object(recorder, '_spawn'), patch.object(
                    recorder, '_watch', side_effect=lambda: seen.append(recorder.cfg.copy())):
                recorder.start(config)
                recorder.watch_thread.join(1)
            self.assertEqual(recorder.cfg['retention_days'], 365)
            self.assertEqual(seen[0]['retention_days'], 365)

    def test_slow_scan_does_not_hold_metadata_lock(self):
        with tempfile.TemporaryDirectory() as folder:
            recorder = self.make_recorder(Path(folder))
            entered, release = threading.Event(), threading.Event()
            def blocked_scan(_):
                entered.set()
                release.wait(2)
                return iter(())
            with patch.object(Path, 'iterdir', blocked_scan):
                worker = threading.Thread(target=recorder.cleanup_old)
                worker.start()
                try:
                    self.assertTrue(entered.wait(1))
                    acquired = recorder.lock.acquire(timeout=.1)
                    if acquired:
                        recorder.lock.release()
                    self.assertTrue(acquired, 'cleanup blocked metadata/segment lock')
                finally:
                    release.set()
                    worker.join(2)

    def test_cancelled_scan_never_deletes_or_marks_day_complete(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            old = root / 'old.mp3'
            old.touch()
            recorder = self.make_recorder(root)
            cancel = threading.Event()
            cancel.set()
            with patch('engine.RETENTION_STATE_FILE', root / 'state.json'):
                self.assertFalse(recorder.cleanup_if_due(cancel=cancel))
                self.assertTrue(old.exists())
                self.assertFalse((root / 'state.json').exists())

    def test_background_cleanup_is_cancelled_on_stop(self):
        with tempfile.TemporaryDirectory() as folder:
            recorder = self.make_recorder(Path(folder))
            entered, release = threading.Event(), threading.Event()
            def blocked_cleanup(*args, **kwargs):
                entered.set()
                release.wait(2)
            with patch.object(recorder, 'cleanup_if_due', side_effect=blocked_cleanup):
                recorder._start_cleanup_worker()
                try:
                    self.assertTrue(entered.wait(1))
                    recorder.stop()
                    self.assertTrue(recorder._cleanup_cancel.is_set())
                finally:
                    release.set()
                    recorder._cleanup_thread.join(2)
                self.assertFalse(recorder._cleanup_thread.is_alive())

    def test_active_file_is_rechecked_after_scan_started(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            old = root / 'active.mp3'
            old.write_bytes(b'protect')
            os.utime(old, (0, 0))
            recorder = self.make_recorder(root)
            def entries(_):
                recorder.segment = Segment(old, datetime.now())
                yield old
            with patch.object(Path, 'iterdir', entries):
                self.assertEqual(recorder.cleanup_old(), 0)
            self.assertEqual(old.read_bytes(), b'protect')

    def test_parallel_daily_requests_delete_only_once(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            recorder = self.make_recorder(root)
            (root / 'old.mp3').touch()
            os.utime(root / 'old.mp3', (0, 0))
            barrier = threading.Barrier(3)
            results = []
            def cleanup():
                barrier.wait()
                results.append(recorder.cleanup_if_due())
            with patch('engine.RETENTION_STATE_FILE', root / 'state.json'):
                workers = [threading.Thread(target=cleanup) for _ in range(2)]
                for worker in workers:
                    worker.start()
                barrier.wait()
                for worker in workers:
                    worker.join(2)
                self.assertEqual(sorted(results), [False, True])
                self.assertFalse((root / 'old.mp3').exists())

    def test_changed_retention_during_scan_preserves_new_policy(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            old = root / 'old.mp3'
            old.touch()
            now = datetime.now()
            stamp = now.timestamp() - 100 * 86400
            os.utime(old, (stamp, stamp))
            recorder = self.make_recorder(root)
            def entries(_):
                with recorder.lock:
                    recorder.cfg['retention_days'] = 365
                yield old
            with patch.object(Path, 'iterdir', entries), patch(
                    'engine.RETENTION_STATE_FILE', root / 'state.json'):
                self.assertFalse(recorder.cleanup_if_due(now, cancel=threading.Event()))
                self.assertTrue(old.exists())
                self.assertFalse((root / 'state.json').exists())


class CaptureOverflowTests(unittest.TestCase):
    def test_overflows_reported_without_dropping_pcm_and_without_log_flood(self):
        logs = []
        recorder = Recorder(logs.append)
        recorder.cfg = {'source': 'portaudio:input', 'channels': 1, 'sample_rate': 48000}
        proc = Mock()
        proc.poll.return_value = None
        blocks = []
        def read(_):
            blocks.append(b'\x01\x00' * 1024)
            if len(blocks) == 5:
                recorder.stop_requested.set()
            return blocks[-1], True
        stream = Mock()
        stream.read.side_effect = read
        context = Mock()
        context.__enter__ = Mock(return_value=stream)
        context.__exit__ = Mock(return_value=False)
        sd = SimpleNamespace(query_devices=lambda: [], query_hostapis=lambda: [],
                             RawInputStream=lambda **kwargs: context)
        with patch.dict(sys.modules, {'sounddevice': sd}), patch('monitor.resolve_device', return_value=0):
            recorder._capture_portaudio(proc)
        self.assertEqual(proc.stdin.buffer.write.call_count, 5)
        self.assertEqual(recorder.input_overflows, 5)
        self.assertEqual(sum('переполн' in line.lower() for line in logs), 1)


if __name__ == '__main__':
    unittest.main()
