"""Regressions: a full broadcast day retains only the active hour's CUE."""
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from cues import TrackEvent, write_active_cue
from engine import DEFAULTS, Recorder
from mp3_stream import MP3Frame


def frame():
    return MP3Frame(b'\xff\xfb\x94\xc0' + b'\x00' * 380, 1152, 48000, 1)


class ActiveCueOnlyTests(unittest.TestCase):
    def setUp(self):
        self.started = datetime(2026, 10, 27, 18, 5)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)
        self.log = []
        self.recorder = Recorder(self.log.append)
        self.recorder.cfg = dict(DEFAULTS, folder=str(self.folder),
                                 channels=1, sample_rate=48000,
                                 retention_enabled=False)
        self.recorder.running = True
        self.recorder.recording_started = self.started
        self.recorder._process_spawn_at = self.started
        self.recorder.last_title = 'До начала'
        self.addCleanup(self.recorder._close_audio_output)

    def put(self, at, count=100):
        for _ in range(count):
            self.recorder._write_mp3_frame(frame(), at)
        self.recorder._sync_output()
        return self.recorder.segment.path

    def test_only_current_cue_changes_old_ones_never_rewritten(self):
        first = self.put(self.started)
        cue18 = first.with_suffix('.cue')
        self.assertTrue(cue18.is_file())
        self.recorder.add_event(TrackEvent(self.started + timedelta(seconds=1), 'Песня в 18'))
        self.assertIn('Песня в 18', cue18.read_text(encoding='utf-8-sig'))
        old_content = cue18.read_bytes()
        old_stat = cue18.stat()

        next_hour = datetime(2026, 10, 27, 19)
        with patch('cues.write_active_cue', wraps=write_active_cue) as writer:
            second = self.put(next_hour)
            self.assertEqual(writer.call_count, 1)
            self.assertEqual(writer.call_args.args[0], second.with_suffix('.cue'))
            self.recorder.add_event(TrackEvent(next_hour + timedelta(seconds=1), 'Песня в 19'))
            self.assertEqual(writer.call_count, 2)
            self.assertTrue(all(c.args[0] == second.with_suffix('.cue') for c in writer.call_args_list))
            # Delayed history cannot touch the finalized hour.
            self.recorder.add_event(TrackEvent(datetime(2026, 10, 27, 18, 45),
                                               'Поздняя информация', 'API lastplayed'))
            self.assertEqual(writer.call_count, 2)

        self.assertEqual(cue18.read_bytes(), old_content)
        self.assertEqual(cue18.stat().st_mtime_ns, old_stat.st_mtime_ns)
        cue19 = second.with_suffix('.cue').read_text(encoding='utf-8-sig')
        self.assertIn('Песня в 19', cue19)
        self.assertIn('TITLE "Песня в 18"', cue19)  # still playing at 19:00
        self.assertEqual(cue19.count('  TRACK '), 2)
        self.assertEqual(self.recorder.segment.path, second)
        self.assertEqual(len(self.recorder._current_cue._events), 1)
        self.assertFalse(hasattr(self.recorder, 'segments'))
        self.assertFalse(hasattr(self.recorder, 'known_paths'))

    def test_boundary_event_waits_for_new_hour_mp3(self):
        first = self.put(self.started)
        old = first.with_suffix('.cue')
        original = old.read_bytes()
        next_hour = datetime(2026, 10, 27, 19)
        self.recorder.add_event(TrackEvent(next_hour + timedelta(seconds=1), 'Новый час'))
        self.assertEqual(old.read_bytes(), original)
        self.assertEqual(len(self.recorder._pending_events), 1)
        second = self.put(next_hour)
        self.assertIn('Новый час', second.with_suffix('.cue').read_text(encoding='utf-8-sig'))
        self.assertEqual(len(self.recorder._pending_events), 0)
        self.assertEqual(old.read_bytes(), original)

    def test_many_hours_do_not_accumulate_old_mp3_or_events(self):
        self.put(self.started)
        first_hour = self.started.replace(minute=0)
        for offset in range(1, 24):
            at = first_hour + timedelta(hours=offset)
            self.put(at)
            self.recorder.add_event(TrackEvent(at + timedelta(seconds=1), f'Час {offset}'))
            self.assertLessEqual(len(self.recorder._current_cue._events), 1)
            self.assertLessEqual(len(self.recorder._current_cue.tracks), 2)
            self.assertEqual(self.recorder.segment.started, at)
            self.assertEqual(self.recorder.events, [])
        self.assertEqual(len(list(self.folder.glob('*.cue'))), 24)
        self.recorder._close_audio_output()
        self.recorder._forget_active_segment()
        self.assertIsNone(self.recorder.segment)
        self.assertEqual(self.recorder.events, [])
        self.assertEqual(self.recorder._pending_events, [])
        self.assertIsNone(self.recorder._active_cue_text)
        self.assertIsNone(self.recorder._current_cue)

    def test_recorder_does_not_scan_whole_archive(self):
        with patch.object(Path, 'glob', side_effect=AssertionError('Must not scan archive')), \
                patch.object(Path, 'iterdir', side_effect=AssertionError('Must not scan archive')):
            self.put(self.started)
            self.put(datetime(2026, 10, 27, 19))
        self.assertIsNotNone(self.recorder.segment)

    def test_ffmpeg_restarts_in_same_hour_append_audio_and_cue(self):
        at = datetime(2026, 10, 27, 19)
        self.recorder._process_spawn_at = at
        self.recorder.recording_started = at
        old_file = self.put(at)
        self.recorder._close_audio_output()
        old_audio = old_file.read_bytes()
        old_cue = old_file.with_suffix('.cue')
        before = old_cue.read_bytes()
        inode = old_cue.stat().st_ino

        resumed = at + timedelta(minutes=22)
        self.recorder._process_spawn_at = resumed
        self.recorder.add_event(TrackEvent(resumed - timedelta(minutes=1),
                                           'После сбоя', 'API playbackinfo'))
        self.assertEqual(old_cue.read_bytes(), before)
        self.assertEqual(self.put(resumed), old_file)
        self.recorder.add_event(TrackEvent(resumed + timedelta(seconds=1), 'После восстановления'))
        self.recorder._close_audio_output()
        self.assertTrue(old_file.read_bytes().startswith(old_audio))
        self.assertEqual(old_file.stat().st_size, 2 * len(old_audio))
        self.assertTrue(old_cue.read_bytes().startswith(before))
        self.assertEqual(old_cue.stat().st_ino, inode)
        text = old_cue.read_text(encoding='utf-8-sig')
        self.assertIn('TITLE "После сбоя"\n    INDEX 01 00:02:30', text)
        self.assertIn('TITLE "После восстановления"\n    INDEX 01 00:03:30', text)
        self.assertEqual(len(list(self.folder.glob('*.mp3'))), 1)
        self.assertFalse(list(self.folder.glob('*_2*')))

    def test_next_hour_waits_for_first_frame_before_creating_output(self):
        original = self.put(self.started)
        cue = original.with_suffix('.cue')
        before = cue.read_bytes()
        self.recorder._sync_output()
        self.assertEqual(self.recorder.segment.path, original)
        self.assertFalse((self.folder / '27-10-2026_19-00.mp3').exists())
        self.assertEqual(len(list(self.folder.glob('*.cue'))), 1)
        self.put(datetime(2026, 10, 27, 19, 0, 2))
        self.assertEqual(cue.read_bytes(), before)
        self.assertEqual(self.recorder.segment.path.name, '27-10-2026_19-00.mp3')

    def test_api_playbackinfo_can_set_initial_title_without_old_cues(self):
        self.recorder.last_title = 'Неизвестный трек'
        mp3 = self.put(self.started)
        self.recorder.add_event(TrackEvent(datetime(2026, 10, 27, 18, 2),
                                           'Уже звучит', 'API playbackinfo'))
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
