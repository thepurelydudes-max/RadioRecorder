"""Regressions: a full broadcast day never leaves past CUEs in working memory."""
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from cues import TrackEvent, write_active_cue
from engine import Recorder


class Clock:
    now = datetime(2026, 10, 27, 18, 5)

    @classmethod
    def now_fn(cls):
        return cls.now


class ActiveCueOnlyTests(unittest.TestCase):
    def setUp(self):
        Clock.now = datetime(2026, 10, 27, 18, 5)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)
        self.log = []
        self.recorder = Recorder(self.log.append)
        self.recorder.cfg = {'folder': str(self.folder), 'split_enabled': True,
                             'split_minutes': 60, 'cue_shift_sec': 0}
        self.recorder.running = True
        self.recorder.recording_started = Clock.now
        self.recorder._process_spawn_at = Clock.now
        self.recorder._filename_template = '%d-%m-%Y_%H-%M.mp3'
        self.recorder.initial_title = 'До начала'
        self.clock_patch = patch('engine.datetime', wraps=datetime)
        mocked = self.clock_patch.start()
        mocked.now.side_effect = Clock.now_fn
        self.addCleanup(self.clock_patch.stop)

    def create_mp3(self, hour, minute=0):
        path = self.folder / f'27-10-2026_{hour:02d}-{minute:02d}.mp3'
        path.write_bytes(b'MP3 audio placeholder')
        return path

    def test_only_current_cue_changes_old_ones_never_rewritten(self):
        first = self.create_mp3(18, 5)
        self.recorder._discover_segments()
        cue18 = first.with_suffix('.cue')
        self.assertTrue(cue18.is_file())
        Clock.now = datetime(2026, 10, 27, 18, 30)
        self.recorder.add_event(TrackEvent(Clock.now, 'Песня в 18:30'))
        self.assertIn('Песня в 18:30', cue18.read_text(encoding='utf-8-sig'))
        old_content = cue18.read_bytes()
        old_stat = cue18.stat()

        Clock.now = datetime(2026, 10, 27, 19)
        second = self.create_mp3(19)
        with patch('engine.write_active_cue', wraps=write_active_cue) as writer:
            self.recorder._discover_segments()
            self.assertEqual(writer.call_count, 1)
            self.assertEqual(writer.call_args.args[0], second.with_suffix('.cue'))
            Clock.now = datetime(2026, 10, 27, 19, 15)
            self.recorder.add_event(TrackEvent(Clock.now, 'Песня в 19:15'))
            self.assertEqual(writer.call_count, 2)
            self.assertTrue(all(c.args[0] == second.with_suffix('.cue') for c in writer.call_args_list))
            # Delayed API history for 18:45 cannot touch the finalized hour.
            self.recorder.add_event(TrackEvent(datetime(2026, 10, 27, 18, 45), 'Поздняя информация'))
            self.assertEqual(writer.call_count, 2)

        self.assertEqual(cue18.read_bytes(), old_content)
        self.assertEqual(cue18.stat().st_mtime_ns, old_stat.st_mtime_ns)
        cue19 = second.with_suffix('.cue').read_text(encoding='utf-8-sig')
        self.assertIn('Песня в 19:15', cue19)
        self.assertIn('TITLE "Песня в 18:30"', cue19)  # still playing at 19:00
        self.assertEqual(cue19.count('  TRACK '), 2)
        self.assertEqual(self.recorder.segment.path, second)
        self.assertEqual(len(self.recorder.events), 1)
        self.assertFalse(hasattr(self.recorder, 'segments'))
        self.assertFalse(hasattr(self.recorder, 'known_paths'))

    def test_boundary_event_waits_for_new_hour_mp3(self):
        self.create_mp3(18, 5)
        self.recorder._discover_segments()
        old = self.recorder.segment.path.with_suffix('.cue')
        original = old.read_bytes()
        Clock.now = datetime(2026, 10, 27, 19, 0, 1)
        self.recorder.add_event(TrackEvent(Clock.now, 'Новый час'))
        self.assertEqual(old.read_bytes(), original)
        self.assertEqual(len(self.recorder._pending_events), 1)
        self.create_mp3(19)
        self.recorder._discover_segments()
        new = self.recorder.segment.path.with_suffix('.cue')
        self.assertIn('Новый час', new.read_text(encoding='utf-8-sig'))
        self.assertEqual(len(self.recorder._pending_events), 0)
        self.assertEqual(old.read_bytes(), original)

    def test_many_hours_do_not_accumulate_old_mp3_or_events(self):
        self.create_mp3(18, 5)
        self.recorder._discover_segments()
        for hour in range(19, 24):
            Clock.now = datetime(2026, 10, 27, hour)
            self.create_mp3(hour)
            self.recorder._discover_segments()
            self.recorder.add_event(TrackEvent(Clock.now + timedelta(seconds=3), f'Час {hour}'))
            self.assertLessEqual(len(self.recorder.events), 1)
            self.assertEqual(self.recorder.segment.started.hour, hour)
        self.assertEqual(len(list(self.folder.glob('*.cue'))), 6)
        self.recorder._forget_active_segment()
        self.assertIsNone(self.recorder.segment)
        self.assertEqual(self.recorder.events, [])
        self.assertEqual(self.recorder._pending_events, [])
        self.assertIsNone(self.recorder._active_cue_text)

    def test_recorder_does_not_scan_whole_archive(self):
        self.create_mp3(18, 5)
        with patch.object(Path, 'glob', side_effect=AssertionError('Must not scan archive')):
            self.recorder._discover_segments()
        self.assertIsNotNone(self.recorder.segment)

    def test_ffmpeg_restarts_in_the_same_hour_without_touching_old_cue(self):
        Clock.now = datetime(2026, 10, 27, 19)
        self.recorder._process_spawn_at = Clock.now
        self.recorder.recording_started = Clock.now
        old_file = self.create_mp3(19)
        self.recorder._discover_segments()
        old_cue = old_file.with_suffix('.cue')
        before = old_cue.read_bytes()

        # The encoder restarts with '_part2' but the local hour is still 19.
        Clock.now = datetime(2026, 10, 27, 19, 22)
        self.recorder._process_spawn_at = Clock.now
        self.recorder._filename_template = '%d-%m-%Y_%H-%M_part2.mp3'
        fresh = self.folder / '27-10-2026_19-22_part2.mp3'
        fresh.write_bytes(b'new encoder output')
        self.recorder._discover_segments()
        self.assertEqual(self.recorder.segment.path, fresh)
        self.assertEqual(self.recorder.segment.started, Clock.now)
        self.recorder.add_event(TrackEvent(Clock.now + timedelta(minutes=1), 'После сбоя'))
        self.assertEqual(old_cue.read_bytes(), before)
        self.assertIn('После сбоя', fresh.with_suffix('.cue').read_text(encoding='utf-8-sig'))

    def test_previous_hour_filename_is_not_reopened_before_new_output_exists(self):
        original = self.create_mp3(18, 5)
        self.recorder._discover_segments()
        Clock.now = datetime(2026, 10, 27, 19, 0, 2)
        self.recorder._discover_segments()
        self.assertEqual(self.recorder.segment.path, original)
        self.assertEqual(len(list(self.folder.glob('*.cue'))), 1)

    def test_api_playbackinfo_can_set_initial_title_without_old_cues(self):
        mp3 = self.create_mp3(18, 5)
        self.recorder.initial_title = 'Неизвестный трек'
        self.recorder._discover_segments()
        self.recorder.add_event(TrackEvent(datetime(2026, 10, 27, 18, 2), 'Уже звучит', 'API playbackinfo'))
        text = mp3.with_suffix('.cue').read_text(encoding='utf-8-sig')
        self.assertIn('TITLE "Уже звучит"', text)
        self.assertEqual(text.count('  TRACK '), 1)

    def test_live_cue_edit_preserves_same_file(self):
        path = self.folder / 'active.cue'
        write_active_cue(path, 'REM DATE 2026\r\n')
        inode = path.stat().st_ino
        write_active_cue(path, 'REM DATE 2026\r\nTRACK 01 AUDIO\r\n')
        self.assertEqual(path.stat().st_ino, inode)
        self.assertEqual(path.read_bytes()[:3], b'\xef\xbb\xbf')
        self.assertIn(b'\r\nTRACK', path.read_bytes())


if __name__ == '__main__':
    unittest.main()
