"""Same-hour output survives manual and encoder restarts without suffixes."""
from datetime import datetime, timedelta
from pathlib import Path
import tempfile
import io
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch

from engine import DEFAULTS, Recorder, choose_output_pattern
from cues import TrackEvent, make_cue


def frame():
    from mp3_stream import MP3Frame
    return MP3Frame(b'\xff\xfb\x94\xc0' + b'\x00' * 380, 1152, 48000, 1)


class HourlyAppendTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.r = Recorder(lambda _: None)
        self.r.cfg = dict(DEFAULTS, folder=str(self.root), sample_rate=48000,
                          channels=1, retention_enabled=False)
        self.r.running = True
        self.r.last_title = 'First'
        self.r._process_spawn_at = datetime(2026, 10, 29, 18)
        self.addCleanup(lambda: self.r._close_audio_output())

    def put(self, at, count=1):
        for _ in range(count):
            self.r._write_mp3_frame(frame(), at)

    def test_same_hour_reopen_appends_existing_bytes_and_cue(self):
        at = datetime(2026, 10, 29, 18)
        self.put(at, 100)
        self.r._close_audio_output()
        path = self.root / '29-10-2026_18-00.mp3'
        prefix = path.read_bytes()
        cue = path.with_suffix('.cue')
        cue_prefix = cue.read_bytes()
        self.r._process_spawn_at = at + timedelta(minutes=2)
        self.r.last_title = 'Resumed'
        self.put(at + timedelta(minutes=2), 5)
        self.r._close_audio_output()
        self.assertTrue(path.read_bytes().startswith(prefix))
        self.assertEqual(len(path.read_bytes()), len(prefix) + 5 * 384)
        self.assertTrue(cue.read_bytes().startswith(cue_prefix))
        self.assertIn('TITLE "Resumed"', cue.read_text(encoding='utf-8-sig'))
        self.assertIn('INDEX 01 00:02:30', cue.read_text(encoding='utf-8-sig'))
        self.assertEqual(len(list(self.root.glob('*.mp3'))), 1)
        self.assertFalse(list(self.root.glob('*_2*')))

    def test_first_start_mid_hour_creates_hour_base_and_next_hour_normal(self):
        at = datetime(2026, 10, 29, 18, 2)
        self.r._process_spawn_at = at
        self.put(at)
        old = self.root / '29-10-2026_18-00.mp3'
        old_cue = old.with_suffix('.cue').read_bytes()
        self.put(datetime(2026, 10, 29, 19))
        self.r._close_audio_output()
        self.assertTrue(old.exists())
        self.assertTrue((self.root / '29-10-2026_19-00.mp3').exists())
        self.assertEqual(old.with_suffix('.cue').read_bytes(), old_cue)

    def test_event_after_pause_uses_audio_duration_and_old_history_ignored(self):
        at = datetime(2026, 10, 29, 18)
        self.put(at, 100)
        self.r._close_audio_output()
        self.r._process_spawn_at = at + timedelta(minutes=2)
        self.put(self.r._process_spawn_at, 100)
        self.r.add_event(TrackEvent(at + timedelta(seconds=20), 'Old history', 'API lastplayed'))
        self.r.add_event(TrackEvent(at + timedelta(minutes=2, seconds=1), 'New title'))
        text = (self.root / '29-10-2026_18-00.cue').read_text(encoding='utf-8-sig')
        self.assertNotIn('Old history', text)
        self.assertIn('TITLE "New title"', text)
        self.assertIn('INDEX 01 00:03:30', text)

    def test_encoder_output_is_tagless_pipe_with_independent_frames(self):
        self.r.cfg.update(source='Line In', source_kind='device')
        with patch('engine.ffmpeg_path', return_value='ffmpeg'):
            cmd = self.r._build_command()
        self.assertEqual(cmd[-1], 'pipe:1')
        for option in ('-write_xing', '-id3v2_version', '-reservoir'):
            self.assertEqual(cmd[cmd.index(option) + 1], '0')
        self.assertNotIn('-segment_time', cmd)

    def test_metadata_ahead_of_encoded_audio_is_not_saved_past_eof(self):
        at = datetime(2026, 10, 29, 18)
        self.put(at, 100)  # 2.4 seconds of encoded sound
        self.r.add_event(TrackEvent(at + timedelta(seconds=3), 'Not yet encoded'))
        cue = self.root / '29-10-2026_18-00.cue'
        self.assertNotIn('Not yet encoded', cue.read_text(encoding='utf-8-sig'))
        self.r._close_audio_output()  # encoder stopped before reaching this title
        self.r._process_spawn_at = at + timedelta(minutes=2)
        self.r.last_title = 'Resumed'
        self.put(self.r._process_spawn_at)
        self.assertIn('Resumed', cue.read_text(encoding='utf-8-sig'))

    def test_naming_never_selects_suffix_even_if_hour_already_exists(self):
        (self.root / '29-10-2026_18-00.mp3').touch()
        fmt = '%d-%m-%Y_%H-%M'
        self.assertEqual(choose_output_pattern(self.root, fmt,
                         datetime(2026, 10, 29, 18, 2), 60), fmt + '.mp3')

    def test_title_after_cue_cap_is_carried_to_next_hour(self):
        at = datetime(2026, 10, 29, 18)
        self.put(at, 100)
        for index in range(1, 101):
            self.r.add_event(TrackEvent(at + timedelta(seconds=index / 100), f'Title {index}'))
        self.assertEqual(len(self.r._current_cue.tracks), 99)
        self.assertEqual(self.r.last_title, 'Title 100')
        self.put(at + timedelta(hours=1))
        self.assertIn('Title 100', (self.root / '29-10-2026_19-00.cue').read_text(encoding='utf-8-sig'))

    def test_ffmpeg_log_pipe_is_closed_after_process_eof(self):
        proc = Mock()
        proc.stderr = io.StringIO('encoder log\n')
        proc.poll.return_value = 0
        self.r._read_stderr(proc)
        self.assertTrue(proc.stderr.closed)

    def test_cue_save_flushes_corresponding_audio_before_it_can_be_reopened(self):
        at = datetime(2026, 10, 29, 18)
        self.put(at, 10)
        self.r.add_event(TrackEvent(at + timedelta(seconds=.1), 'Committed'))
        path = self.root / '29-10-2026_18-00.mp3'
        self.assertEqual(path.stat().st_size, 10 * 384)

    def test_pending_icy_title_is_seeded_into_next_hour(self):
        at = datetime(2026, 10, 29, 18, 59, 59)
        self.r._process_spawn_at = at
        self.put(at, 40)
        self.r.add_event(TrackEvent(at + timedelta(seconds=.99), 'Boundary title', 'ICY'))
        self.put(at + timedelta(seconds=1))
        cue = self.root / '29-10-2026_19-00.cue'
        self.assertIn('Boundary title', cue.read_text(encoding='utf-8-sig'))

    def test_rejected_bitrate_does_not_append_cue_seam(self):
        from mp3_stream import MP3Frame
        at = datetime(2026, 10, 29, 18)
        self.put(at, 100)
        self.r._close_audio_output()
        cue = self.root / '29-10-2026_18-00.cue'
        before = cue.read_bytes()
        self.r.last_title = 'Must not be committed'
        self.r._process_spawn_at = at + timedelta(minutes=2)
        changed_rate = MP3Frame(b'\xff\xfb\xa4\xc0' + b'\0' * 476, 1152, 48000, 1)
        with self.assertRaises(ValueError):
            self.r._write_mp3_frame(changed_rate, self.r._process_spawn_at)
        self.assertEqual(cue.read_bytes(), before)

    def test_missing_audio_creates_new_pair_and_preserves_orphan_cue_backup(self):
        at = datetime(2026, 10, 29, 18)
        cue = self.root / '29-10-2026_18-00.cue'
        original = make_cue('29-10-2026_18-00.mp3', at, at + timedelta(hours=1),
                            [TrackEvent(at + timedelta(seconds=60), 'Old second')],
                            initial_title='Old orphan')
        cue.write_bytes(b'\xef\xbb\xbf' + original.encode())
        self.put(at)
        self.assertEqual(self.r._current_cue.tracks[0].title, 'First')
        self.assertEqual(len(self.r._current_cue.tracks), 1)
        self.assertEqual(cue.with_suffix('.cue.orphan').read_bytes(), b'\xef\xbb\xbf' + original.encode())

    def test_interrupted_orphan_backup_is_reusable_without_overwriting_it(self):
        at = datetime(2026, 10, 29, 18)
        cue = self.root / '29-10-2026_18-00.cue'
        old = make_cue('29-10-2026_18-00.mp3', at, at + timedelta(hours=1), [], 'Old')
        data = b'\xef\xbb\xbf' + old.encode()
        cue.write_bytes(data)
        backup = cue.with_suffix('.cue.orphan')
        backup.write_bytes(data)  # previous process stopped before removing orphan
        self.put(at)
        self.assertEqual(backup.read_bytes(), data)
        self.assertEqual(self.r._current_cue.tracks[0].title, 'First')

    def test_abrupt_process_exit_leaves_pair_appendable(self):
        script = '''
import os,sys
from pathlib import Path
from datetime import datetime,timedelta
from engine import Recorder,DEFAULTS
from cues import TrackEvent
from mp3_stream import MP3Frame
r=Recorder(lambda _:None)
r.cfg=dict(DEFAULTS,folder=sys.argv[1],channels=1,sample_rate=48000)
r.running=True
r.last_title='Before crash'
at=datetime(2026,10,29,18)
r._process_spawn_at=at
f=MP3Frame(b'\\xff\\xfb\\x94\\xc0'+b'\\x00'*380,1152,48000,1)
for _ in range(10):r._write_mp3_frame(f,at)
r.add_event(TrackEvent(at+timedelta(seconds=.1),'Committed before crash'))
os._exit(0)
'''
        subprocess.run([sys.executable, '-c', script, str(self.root)],
                       cwd=Path(__file__).resolve().parents[1], check=True, timeout=10)
        path = self.root / '29-10-2026_18-00.mp3'
        prefix = path.read_bytes()
        self.assertEqual(len(prefix), 3840)
        self.r._process_spawn_at = datetime(2026, 10, 29, 18, 2)
        self.r.last_title = 'After crash'
        self.put(self.r._process_spawn_at)
        self.r._close_audio_output()
        self.assertTrue(path.read_bytes().startswith(prefix))
        text = path.with_suffix('.cue').read_text(encoding='utf-8-sig')
        self.assertIn('Committed before crash', text)
        self.assertIn('After crash', text)


if __name__ == '__main__':
    unittest.main()
