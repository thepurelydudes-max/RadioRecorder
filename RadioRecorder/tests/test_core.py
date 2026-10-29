import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch
from pathlib import Path

from cues import TrackEvent, cue_time, make_cue, write_cue_atomic
from engine import Recorder, Segment, clock_boundary, format_to_strftime, is_schedule_active, preview_name
from metadata import parse_icy_block, parse_lastplayed, parse_playbackinfo, title_from_track


class CueTest(unittest.TestCase):
    def test_identical_conventions_as_radioboss(self):
        start = datetime(2026, 9, 25, 22, 0)
        events = [TrackEvent(start, 'ВРЕМЯ - 22-00'),
                  TrackEvent(start + timedelta(seconds=8.41333), '5УТРА - Белая полоса'),
                  TrackEvent(start + timedelta(seconds=200.3), 'ДЖИНГЛ КОМЕТА ИИ ЛАУНЖ 10')]
        actual = make_cue('25-09-2026_22-00.mp3', start,
                          start+timedelta(hours=1), events)
        self.assertTrue(actual.startswith('REM DATE 2026\r\nREM COMMENT  "Created in RadioBOSS (Stream Archive)"\r\n'))
        self.assertIn('FILE "25-09-2026_22-00.mp3" MP3', actual)
        self.assertIn('  TRACK 02 AUDIO\r\n    TITLE "5УТРА - Белая полоса"\r\n    INDEX 01 00:08:31',actual)
        self.assertIn('  TRACK 03 AUDIO',actual)
        with tempfile.TemporaryDirectory() as temp:
            file = Path(temp) / '25-09-2026_22-00.cue'
            write_cue_atomic(file, actual)
            self.assertEqual(file.read_bytes()[:3], b'\xef\xbb\xbf')
            self.assertIn(b'\r\n', file.read_bytes())

    def test_spanning_hour_begins_with_old_title(self):
        start = datetime(2026,9,25,23)
        ev = [TrackEvent(start-timedelta(seconds=20), 'Большая песня'),
              TrackEvent(start+timedelta(seconds=80), 'Следующий трек')]
        cue = make_cue('23.mp3', start, start + timedelta(hours=1), ev)
        self.assertIn('TITLE "Большая песня"\r\n    INDEX 01 00:00:00', cue)
        self.assertIn('INDEX 01 01:20:00', cue)
        self.assertEqual(cue.count('  TRACK '),2)

    def test_cue_time(self):
        self.assertEqual(cue_time(0), '00:00:00')
        self.assertEqual(cue_time(60.999), '01:01:00')
        self.assertEqual(cue_time(59*60+59.999), '60:00:00')

    def test_filename(self):
        self.assertEqual(preview_name('%d-%m-%y_%hh-%mm', datetime(2026,10,27,11,0)),
                         '27-10-2026_11-00.mp3')
        self.assertEqual(format_to_strftime('%y%m%d-%hh%mm%ss'), '%Y%m%d-%H%M%S')
        with self.assertRaises(ValueError):
            format_to_strftime('../%d')


class MetadataTest(unittest.TestCase):
    def test_icy(self):
        payload = "StreamTitle='5УТРА - Белая полоса';StreamUrl='';".encode() + b'\x00' * 15
        self.assertEqual(parse_icy_block(payload), '5УТРА - Белая полоса')
        self.assertIsNone(parse_icy_block(b'garbage'))

    def test_api_playback(self):
        sample = b'<Info><CurrentTrack><TRACK ARTIST="ART" TITLE="SONG" CASTTITLE="ART - SONG" /></CurrentTrack><Playback state="play" pos="3000"/></Info>'
        item = parse_playbackinfo(sample, datetime(2026,10,27,11,0,10))
        self.assertEqual(item.title, 'ART - SONG')
        self.assertEqual(item.at, datetime(2026,10,27,11,0,7))

    def test_api_history_and_jingle(self):
        sample = (b'<LastPlayed><TRACK STARTTIME="2026-10-27 11:01:03" '
                  b'FILENAME="C:\\JINGLES\\JINGLE5.mp3"/></LastPlayed>')
        track = parse_lastplayed(sample)
        self.assertEqual(track[0].title, 'JINGLE5')
        self.assertEqual(track[0].at, datetime(2026,10,27,11,1,3))

    def test_schedule(self):
        cfg = {'schedule': [[True] *24 for _ in range(7)]}
        cfg['schedule'][1][11] = False  # Tuesday
        self.assertFalse(is_schedule_active(cfg, datetime(2026,10,27,11)))
        self.assertTrue(is_schedule_active(cfg, datetime(2026,10,27,12)))


class SegmentationTest(unittest.TestCase):
    def test_split_at_hour_not_after_60_minutes(self):
        self.assertEqual(clock_boundary(datetime(2026, 10, 27, 11, 23), 60),
                         datetime(2026, 10, 27, 11, 0))
        self.assertEqual(clock_boundary(datetime(2026, 10, 27, 12, 7), 60),
                         datetime(2026, 10, 27, 12, 0))
        self.assertEqual(clock_boundary(datetime(2026, 10, 27, 13, 0), 60),
                         datetime(2026, 10, 27, 13, 0))

    def test_build_pipes_independent_frames_for_clocktime_split(self):
        import tempfile
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as folder, patch('engine.ffmpeg_path', return_value='ffmpeg.exe'):
            recorder = Recorder(lambda msg: None)
            recorder.cfg = {'source_kind': 'device', 'source': 'Line In (USB AUDIO CODEC)',
                            'folder': folder, 'channels': 1, 'sample_rate': 48000,
                            'bitrate': 128, 'filename_format': '%d-%m-%y_%hh-%mm',
                            'split_enabled': True, 'split_minutes': 60}
            cmd = recorder._build_command()
            self.assertEqual(cmd[-2:], ['mp3', 'pipe:1'])
            for option in ('-reservoir', '-write_xing', '-id3v2_version'):
                self.assertEqual(cmd[cmd.index(option) + 1], '0')
            self.assertNotIn('-segment_time', cmd)
            self.assertNotIn('-segment_atclocktime', cmd)
            self.assertNotIn('-strftime', cmd)


class RetentionTest(unittest.TestCase):
    def test_removes_only_expired_mp3_cue_in_recordings_folder(self):
        with tempfile.TemporaryDirectory() as root:
            base = Path(root)
            folder = base / 'recordings'
            folder.mkdir()
            old_audio = folder / '20-10-2026_11-00.mp3'
            old_cue = folder / '20-10-2026_11-00.cue'
            recent_audio = folder / '27-10-2026_11-00.mp3'
            recent_cue = folder / '27-10-2026_11-00.cue'
            old_other = folder / 'unrelated.txt'
            old_orphan = folder / 'old_orphan.mp3'
            nested = folder / 'nested'
            nested.mkdir()
            nested_file = nested / 'old.log'
            outside = base / '20-10-2026_11-00.mp3'
            for item in (old_audio, old_cue, old_other, old_orphan, recent_audio, recent_cue, nested_file, outside):
                item.write_bytes(b'file')
            old_time = time.time() - 95 * 86400
            for item in (old_audio, old_cue, old_other, old_orphan, nested_file, outside):
                os.utime(item, (old_time, old_time))
            messages = []
            recorder = Recorder(messages.append)
            recorder.cfg = {'folder': str(folder), 'retention_days': 90, 'retention_enabled': True}
            deleted = recorder.cleanup_old()
            self.assertEqual(deleted, 3)
            self.assertFalse(old_audio.exists())
            self.assertFalse(old_cue.exists())
            self.assertTrue(old_other.exists())
            self.assertFalse(old_orphan.exists())
            self.assertTrue(nested_file.exists())
            self.assertTrue(recent_audio.exists())
            self.assertTrue(recent_cue.exists())
            self.assertTrue(outside.exists())
            self.assertEqual(len(messages), 3)

    def test_does_not_delete_open_recording_but_deletes_orphan_audio(self):
        with tempfile.TemporaryDirectory() as root:
            folder = Path(root)
            current = folder / 'current.mp3'
            current_cue = folder / 'current.cue'
            orphan = folder / 'orphan.mp3'
            for item in (current, current_cue, orphan):
                item.write_bytes(b'file')
                old_time = time.time() - 95 * 86400
                os.utime(item, (old_time, old_time))
            recorder = Recorder(lambda _: None)
            recorder.cfg = {'folder': str(folder), 'retention_days': 90, 'retention_enabled': True}
            recorder.segment = Segment(current, datetime.now())
            recorder.cleanup_old()
            self.assertTrue(current.exists())
            self.assertTrue(current_cue.exists())
            self.assertFalse(orphan.exists())

    def test_does_not_delete_files_newer_than_calendar_day_cutoff(self):
        with tempfile.TemporaryDirectory() as root:
            folder = Path(root)
            days_old = folder / 'older_day.mp3'
            same_day = folder / 'boundary_day.cue'
            newer = folder / 'recent.mp3'
            for item in (days_old, same_day, newer):
                item.write_bytes(b'content')
            dt = datetime(2026, 10, 27, 11)
            os.utime(days_old, (datetime(2026, 7, 28, 23).timestamp(),) * 2)
            os.utime(same_day, (datetime(2026, 7, 29, 11).timestamp(),) * 2)
            os.utime(newer, (datetime(2026, 10, 26, 11).timestamp(),) * 2)
            recorder = Recorder(lambda _: None)
            recorder.cfg = {'folder': str(folder), 'retention_days': 90, 'retention_enabled': True}
            self.assertEqual(recorder.cleanup_old(dt), 1)
            self.assertFalse(days_old.exists())
            self.assertTrue(same_day.exists())
            self.assertTrue(newer.exists())
            self.assertEqual(recorder.cleanup_old(datetime(2026, 10, 28, 0, 1)), 1)
            self.assertFalse(same_day.exists())

    def test_cleanup_daily_after_midnight_and_once_per_date_even_after_restart(self):
        with tempfile.TemporaryDirectory() as root:
            base = Path(root)
            folder = base / 'records'
            folder.mkdir()
            state = base / 'daily_state.json'
            old = folder / 'old.mp3'
            old.write_bytes(b'old')
            os.utime(old, (datetime(2026, 7, 28).timestamp(),) * 2)
            cfg = {'folder': str(folder), 'retention_days': 90, 'retention_enabled': True}
            log = []
            with patch('engine.RETENTION_STATE_FILE', state):
                one = Recorder(log.append)
                one.cfg = cfg
                self.assertFalse(one.cleanup_if_due(datetime(2026, 10, 27, 12)))  # stopped
                one.running = True
                self.assertTrue(one.cleanup_if_due(datetime(2026, 10, 27, 23, 59)))
                self.assertFalse(old.exists())
                self.assertFalse(one.cleanup_if_due(datetime(2026, 10, 27, 23, 59, 59)))
                two = Recorder(log.append)
                two.cfg = cfg
                two.running = True
                self.assertFalse(two.cleanup_if_due(datetime(2026, 10, 27, 23, 59)))
                self.assertTrue(two.cleanup_if_due(datetime(2026, 10, 28, 0, 0, 1)))
                self.assertFalse(two.cleanup_if_due(datetime(2026, 10, 28, 16)))
                self.assertEqual(state.read_text(encoding='utf8').count('2026-10-28'), 1)

    def test_skip_symlink_and_subfolders_and_active_cue(self):
        with tempfile.TemporaryDirectory() as root:
            base = Path(root)
            folder = base / 'records'
            folder.mkdir()
            outside = base / 'outside.txt'
            outside.write_text('safety', encoding='utf8')
            link = folder / 'external.txt'
            try:
                link.symlink_to(outside)
            except OSError:  # Windows unprivileged symlink creation
                link = None
            folder_file = folder / 'active.mp3'
            cue = folder / 'active.cue'
            for f in (folder_file, cue):
                f.write_text('recording')
                os.utime(f, (datetime(2026, 7, 28).timestamp(),) * 2)
            recorder = Recorder(lambda _: None)
            recorder.cfg = {'folder': str(folder), 'retention_days': 90, 'retention_enabled': True}
            recorder.segment = Segment(folder_file, datetime(2026, 7, 28))
            recorder.cleanup_old(datetime(2026, 10, 27))
            self.assertTrue(folder_file.exists())
            self.assertTrue(cue.exists())
            self.assertTrue(outside.exists())
            if link is not None:
                self.assertTrue(link.is_symlink())

    def test_protect_application_directory(self):
        import engine
        with tempfile.TemporaryDirectory() as root:
            folder = Path(root)
            recorder = Recorder(lambda _: None)
            recorder.cfg = {'folder': str(folder), 'retention_days': 90, 'retention_enabled': True}
            with patch('engine.APP_DIR', folder):
                with self.assertRaises(ValueError):
                    recorder.cleanup_old(datetime(2026, 10, 27))

    def test_disabled_retention_does_nothing(self):
        with tempfile.TemporaryDirectory() as root:
            mp3 = Path(root) / 'archive.mp3'
            cue = mp3.with_suffix('.cue')
            for item in (mp3, cue):
                item.write_bytes(b'file')
                old_time = time.time() - 95 * 86400
                os.utime(item, (old_time, old_time))
            recorder = Recorder(lambda _: None)
            recorder.cfg = {'folder': root, 'retention_days': 90, 'retention_enabled': False}
            recorder.cleanup_old()
            self.assertTrue(mp3.exists())
            self.assertTrue(cue.exists())


if __name__ == '__main__':
    unittest.main()

