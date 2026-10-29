"""Resume Recording regressions: exactly one MP3/CUE per clock hour."""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cues import TrackEvent, read_existing_cue
from engine import Recorder, format_to_strftime, choose_output_pattern
from mp3_resume import AppendingMP3, FrameStream, frame_info, inspect_existing


HEADER = bytes.fromhex('fffb90c4')
INFO = frame_info(HEADER)
FRAME = HEADER + bytes(INFO.length - 4)


def config(folder):
    return {
        'folder': str(folder), 'filename_format': '%d-%m-%y_%hh-%mm',
        'split_enabled': True, 'split_minutes': 60,
        'source_kind': 'device', 'source': 'Line In',
        'channels': 1, 'sample_rate': 44100, 'bitrate': 128,
        'cue_shift_sec': 0,
    }


class FrameWriterTests(unittest.TestCase):
    def test_stream_handles_chunked_frames(self):
        stream = FrameStream()
        self.assertEqual(stream.feed(FRAME[:8]), [])
        self.assertEqual(stream.feed(FRAME[8:-10]), [])
        self.assertEqual(stream.feed(FRAME[-10:]), [(FRAME, INFO.duration)])

    def test_preserves_existing_mp3_bytes_and_resumes(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'test.mp3'
            path.write_bytes(FRAME)
            with AppendingMP3(path) as writer:
                writer.append(FRAME, INFO.duration)
            self.assertEqual(path.read_bytes(), FRAME * 2)
            self.assertEqual(inspect_existing(path).frames, 2)

    def test_truncated_tail_saved_separately_before_append(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'test.mp3'
            path.write_bytes(FRAME + FRAME[:19])
            writer = AppendingMP3(path)
            writer.append(FRAME, INFO.duration)
            writer.close()
            self.assertEqual(path.read_bytes(), FRAME * 2)
            self.assertEqual((Path(d)/'test.mp3.recovery-tail').read_bytes(), FRAME[:19])

    def test_invalid_existing_file_is_never_modified(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'bad.mp3'
            path.write_bytes(b'not an mp3 file')
            with self.assertRaises(ValueError):
                AppendingMP3(path)
            self.assertEqual(path.read_bytes(), b'not an mp3 file')

    def test_legacy_xing_frame_count_and_bytes_repaired(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'legacy.mp3'
            first = bytearray(FRAME)
            at = 4 + 17  # mono MPEG-1 without CRC
            first[at:at+4] = b'Info'
            first[at+4:at+8] = (3).to_bytes(4, 'big')
            first[at+8:at+12] = (1).to_bytes(4, 'big')
            first[at+12:at+16] = len(FRAME).to_bytes(4, 'big')
            path.write_bytes(first)
            writer = AppendingMP3(path)
            writer.append(FRAME, INFO.duration)
            writer.close()
            result = path.read_bytes()
            self.assertEqual(int.from_bytes(result[at+8:at+12], 'big'), 2)
            self.assertEqual(int.from_bytes(result[at+12:at+16], 'big'), 2 * len(FRAME))


class HourlyResumeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)
        self.fmt = format_to_strftime('%d-%m-%y_%hh-%mm')

    def _recorder(self):
        rec = Recorder(lambda _: None)
        rec.cfg = config(self.folder)
        rec.running = True
        return rec

    def test_one_file_when_manually_resumed_and_at_next_hour(self):
        first = self._recorder()
        start = datetime(2026, 10, 29, 18)
        with patch('engine.datetime', wraps=datetime) as clock:
            clock.now.return_value = start
            for _ in range(220):
                first._append_encoded_frame(FRAME, INFO.duration)
            first.add_event(TrackEvent(start + timedelta(seconds=2), 'Первая песня'))
            first._active_mp3.close()
            saved = self.folder/'29-10-2026_18-00.mp3'
            original = saved.read_bytes()
            old_cue = saved.with_suffix('.cue').read_bytes()

            recovered = self._recorder()
            clock.now.return_value = start + timedelta(minutes=2)
            for _ in range(200):
                recovered._append_encoded_frame(FRAME, INFO.duration)
            recovered.add_event(TrackEvent(start + timedelta(minutes=2, seconds=2),
                                           'После восстановления'))
            self.assertTrue(saved.read_bytes().startswith(original))
            self.assertEqual(len(saved.read_bytes()), 420 * len(FRAME))
            restored = read_existing_cue(saved.with_suffix('.cue'))
            self.assertIn('Первая песня', [title for _, title in restored[1]])
            self.assertIn('После восстановления', [title for _, title in restored[1]])
            self.assertEqual(len(list(self.folder.glob('*18-00*.mp3'))), 1)

            clock.now.return_value = start + timedelta(hours=1)
            recovered._append_encoded_frame(FRAME, INFO.duration)
            self.assertEqual(len(list(self.folder.glob('*.mp3'))), 2)
            self.assertTrue((self.folder/'29-10-2026_19-00.mp3').is_file())
            self.assertNotEqual(saved.with_suffix('.cue').read_bytes(), old_cue)
            finalized = saved.with_suffix('.cue').read_bytes()
            recovered._active_mp3.close()
            self.assertEqual(saved.with_suffix('.cue').read_bytes(), finalized)
            self.assertFalse(list(self.folder.glob('*_2.mp3')))

    def test_existing_cue_without_audio_must_not_be_destroyed(self):
        rec = self._recorder()
        path = self.folder/'29-10-2026_18-00.cue'
        path.write_bytes(b'user existing cue')
        with patch('engine.datetime', wraps=datetime) as clock:
            clock.now.return_value = datetime(2026, 10, 29, 18, 2)
            with self.assertRaisesRegex(RuntimeError, 'CUE'):
                rec._append_encoded_frame(FRAME, INFO.duration)
        self.assertEqual(path.read_bytes(), b'user existing cue')

    def test_new_hour_name_not_infected_by_previous_collision(self):
        fmt = self.fmt
        (self.folder/'29-10-2026_19-00.mp3').write_bytes(FRAME)
        self.assertEqual(choose_output_pattern(
            self.folder, fmt, datetime(2026, 10, 29, 18, 1), 60), fmt+'.mp3')


if __name__ == '__main__':
    unittest.main()
