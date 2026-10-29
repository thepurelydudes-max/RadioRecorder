"""Regression tests for 24/7 reliability fixes introduced in 0.6.1."""
from __future__ import annotations
from datetime import datetime, timedelta
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from engine import Recorder, choose_output_pattern, ensure_disk_space, format_to_strftime
from cues import TrackEvent, write_active_cue
from metadata import MetadataMonitor
from monitor import resolve_device


class CleanupSafetyTests(unittest.TestCase):
    def test_only_mp3_and_cue_deleted_non_matching_files_untouched(self):
        with tempfile.TemporaryDirectory() as d:
            folder = Path(d)
            values = ['old-a.mp3', 'old-b.cue', 'old-c.MP3', 'old-d.CUE',
                      'notes.txt', 'config.json', 'old.mp3.tmp', 'playlist.m3u']
            cutoff = time.time() - 95*86400
            for name in values:
                p = folder / name
                p.write_bytes(b'untouched if unrelated')
                os.utime(p, (cutoff, cutoff))
            r = Recorder(lambda _: None)
            r.cfg = {'retention_enabled': True, 'retention_days': 90, 'folder': d}
            self.assertEqual(r.cleanup_old(), 4)
            for name in values[:4]:
                self.assertFalse((folder/name).exists())
            for name in values[4:]:
                self.assertTrue((folder/name).exists())


class NamingSafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)
        self.at = datetime(2026, 10, 27, 18, 25)
        self.fmt = format_to_strftime('%d-%m-%y_%hh-%mm')

    def test_future_19_00_collision_reserves_suffix_before_18_00_start(self):
        old = self.folder / '27-10-2026_19-00.mp3'
        old.write_bytes(b'OLD_MP3_DO_NOT_TOUCH')
        cue = old.with_suffix('.cue')
        cue.write_bytes(b'OLD_CUE_DO_NOT_TOUCH')
        pattern = choose_output_pattern(self.folder, self.fmt, self.at, 60)
        self.assertEqual(pattern, self.fmt + '.mp3')
        self.assertEqual(old.read_bytes(), b'OLD_MP3_DO_NOT_TOUCH')
        self.assertEqual(cue.read_bytes(), b'OLD_CUE_DO_NOT_TOUCH')

    def test_suffix_three_if_two_occupied_even_if_only_future_hour(self):
        for suffix in ['', '_2']:
            (self.folder / f'27-10-2026_19-00{suffix}.mp3').write_bytes(b'a')
        pattern = choose_output_pattern(self.folder, self.fmt, self.at, 60)
        self.assertEqual(pattern, self.fmt + '.mp3')

    def test_collision_with_cue_alone_protected(self):
        (self.folder / '27-10-2026_19-00.cue').write_text('OLD')
        self.assertEqual(choose_output_pattern(self.folder, self.fmt, self.at, 60),
                         self.fmt + '.mp3')

    def test_at_exact_clock_boundary_no_false_duplicate(self):
        self.assertEqual(choose_output_pattern(self.folder, self.fmt,
                                               self.at.replace(minute=0, second=0), 60),
                         self.fmt + '.mp3')

    def test_nonunique_template_across_hours_rejected(self):
        pattern = format_to_strftime('%d-%m-%y')
        with self.assertRaisesRegex(ValueError, 'одинаковые имена'):
            choose_output_pattern(self.folder, pattern, self.at, 60)

    def test_nonunique_template_across_days_rejected(self):
        pattern = format_to_strftime('%hh-%mm')
        with self.assertRaisesRegex(ValueError, 'одинаковые имена'):
            choose_output_pattern(self.folder, pattern, self.at, 60)

    def test_when_no_segmentation_off_only_current_name_matters(self):
        p = self.folder / '27-10-2026_18-25.mp3'
        p.write_bytes(b'old')
        self.assertEqual(choose_output_pattern(self.folder, self.fmt,
                                               self.at, 60, False), self.fmt+'_2.mp3')

    def test_ffmpeg_command_uses_session_suffix_for_all_hours(self):
        (self.folder / '27-10-2026_19-00.mp3').write_bytes(b'old')
        recorder = Recorder(lambda _: None)
        recorder.cfg = {
            'folder': str(self.folder), 'filename_format': '%d-%m-%y_%hh-%mm',
            'split_enabled': True, 'split_minutes': 60, 'sample_rate': 48000,
            'bitrate': 128, 'channels': 1, 'source_kind': 'device', 'source':'Line In'
        }
        with patch('engine.datetime', wraps=datetime) as clock, patch('engine.ffmpeg_path', return_value='ffmpeg.exe'):
            clock.now.return_value = self.at
            cmd = recorder._build_command()
        self.assertEqual(cmd[-1], 'pipe:1')
        self.assertIn('-write_xing', cmd)
        self.assertEqual(cmd[cmd.index('-id3v2_version')+1], '0')
        self.assertIsNone(recorder._filename_template)


class USBReenumerationTests(unittest.TestCase):
    def test_changed_index_same_device_and_api_resolves(self):
        devices = [{'name': 'Other', 'hostapi': 0, 'max_input_channels': 1},
                   {'name': 'ESI MAYA', 'hostapi': 1, 'max_input_channels': 2}]
        apis = [{'name':'MME'}, {'name':'Windows WASAPI'}]
        self.assertEqual(resolve_device('ESI MAYA — Windows WASAPI (#99)', 'input', devices, apis), 1)

    def test_no_silent_switch_to_wrong_host_api(self):
        devices = [{'name':'ESI MAYA','hostapi':0,'max_input_channels':2}]
        apis = [{'name':'MME'}, {'name':'Windows WASAPI'}]
        with self.assertRaisesRegex(RuntimeError, 'не доступно'):
            resolve_device('ESI MAYA — Windows WASAPI (#99)', 'input', devices, apis)

    def test_two_identical_names_raise_not_randomly_picked(self):
        devices = [{'name':'ESI MAYA', 'hostapi':0,'max_input_channels':2},
                   {'name':'ESI MAYA', 'hostapi':0,'max_input_channels':2}]
        with self.assertRaisesRegex(RuntimeError, 'Несколько одинаковых'):
            resolve_device('ESI MAYA — MME (#99)', 'input', devices, [{'name':'MME'}])


class MetadataLifecycleTests(unittest.TestCase):
    def test_next_connection_waits_until_old_worker_finishes(self):
        started = []
        first_started = threading.Event()
        unblock = threading.Event()
        monitor = MetadataMonitor(lambda event: None, lambda msg: None)

        def controlled_loop(url, password, username, signal):
            started.append(url)
            if url == 'first':
                first_started.set()
                unblock.wait(timeout=2)

        monitor._icy_loop = controlled_loop
        try:
            monitor.start('icy', 'first')
            self.assertTrue(first_started.wait(1))
            monitor.stop()
            monitor.start('icy', 'second')
            self.assertEqual(started, ['first'])
            unblock.set()
            until = time.monotonic() + 2
            while time.monotonic() < until and len(started) < 2:
                time.sleep(.01)
            self.assertEqual(started, ['first', 'second'])
        finally:
            monitor.stop()
            unblock.set()

    def test_stop_prevents_late_icy_metadata_delivery(self):
        emitted = []
        m = MetadataMonitor(emitted.append, lambda _:None)
        previous = m.stop_signal
        m.stop()
        m._emit(TrackEvent(datetime.now(), 'too late'), previous)
        self.assertEqual(emitted, [])


class ActiveCueRobustnessTests(unittest.TestCase):
    def test_append_current_track_preserves_old_bytes_and_inode(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d)/'18-00.cue'
            before = 'REM DATE 2026\r\n  TRACK 01 AUDIO\r\n'
            next_text = before + '  TRACK 02 AUDIO\r\n'
            write_active_cue(path, before)
            inode = path.stat().st_ino
            old = path.read_bytes()
            write_active_cue(path, next_text, previous_text=before)
            self.assertEqual(path.stat().st_ino, inode)
            self.assertEqual(path.read_bytes(), old + b'  TRACK 02 AUDIO\r\n')

    def test_interrupted_append_preserves_original_and_refuses_replacement(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d)/'active.cue'
            first = 'REM DATE 2026\r\n'
            second = first + '  TRACK 02 AUDIO\r\n'
            write_active_cue(path, first)
            with path.open('ab') as f:
                f.write(b'   PARTIAL GARBAGE')
            original = path.read_bytes()
            # A damaged partial CUE must not be truncated or silently replaced.
            with self.assertRaises(ValueError):
                write_active_cue(path, second, previous_text=first)
            self.assertEqual(path.read_bytes(), original)


class DiskHeadroomTests(unittest.TestCase):
    def test_refuse_start_when_disk_nearly_full(self):
        usage = type('Usage', (), {'free':100*1024*1024})()
        with patch('engine.shutil.disk_usage', return_value=usage):
            with self.assertRaisesRegex(RuntimeError, 'Недостаточно места'):
                ensure_disk_space(Path('.'))

    def test_non_windows_job_attachment_is_noop(self):
        from ffmpeg_job import attach_kill_on_exit_job
        import sys
        if sys.platform != 'win32':
            self.assertIsNone(attach_kill_on_exit_job(None))


if __name__ == '__main__':
    unittest.main()
