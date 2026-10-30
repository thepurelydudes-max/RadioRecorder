"""Buffered MP3 output: fewer explicit flushes with crash-recovery invariants."""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import os
import unittest

from engine import Recorder
from mp3_resume import (
    AppendingMP3, MP3_FLUSH_INTERVAL_SECONDS, MP3_FLUSH_THRESHOLD_BYTES,
    frame_info, inspect_existing,
)

HEADER = bytes.fromhex('fffb90c4')
INFO = frame_info(HEADER)
FRAME = HEADER + bytes(INFO.length - 4)


class BufferedMP3Tests(unittest.TestCase):
    def test_flush_first_frame_then_every_half_second_not_every_frame(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'hour.mp3'
            now = [100.]
            with patch('mp3_resume.time.monotonic', side_effect=lambda: now[0]):
                writer = AppendingMP3(path)
                try:
                    with patch.object(writer, 'flush', wraps=writer.flush) as flushed:
                        writer.append(FRAME, INFO.duration)
                        self.assertTrue(writer.flush_if_due())
                        self.assertEqual(flushed.call_count, 1)
                        # Intermediate MPEG frames remain in the buffered writer.
                        for _ in range(10):
                            now[0] += 0.02
                            writer.append(FRAME, INFO.duration)
                            self.assertFalse(writer.flush_if_due())
                        self.assertEqual(flushed.call_count, 1)
                        now[0] += MP3_FLUSH_INTERVAL_SECONDS
                        writer.append(FRAME, INFO.duration)
                        self.assertTrue(writer.flush_if_due())
                        self.assertEqual(flushed.call_count, 2)
                        now[0] += 5
                        self.assertFalse(writer.flush_if_due())  # no pending bytes
                        self.assertEqual(flushed.call_count, 2)
                finally:
                    writer.close()
            self.assertEqual(inspect_existing(path).frames, 12)

    def test_large_buffer_is_flushed_before_half_second(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'hour.mp3'
            now = [10.]
            with patch('mp3_resume.time.monotonic', side_effect=lambda: now[0]):
                writer = AppendingMP3(path)
                try:
                    writer.append(FRAME, INFO.duration)
                    self.assertTrue(writer.flush_if_due())
                    count = 0
                    while writer._bytes_since_flush < MP3_FLUSH_THRESHOLD_BYTES:
                        writer.append(FRAME, INFO.duration)
                        count += 1
                    self.assertTrue(writer.flush_if_due())
                    self.assertEqual(writer._bytes_since_flush, 0)
                    self.assertEqual(path.stat().st_size, (count + 1) * len(FRAME))
                finally:
                    writer.close()

    def test_flush_does_not_use_fsync_during_capture_but_close_does(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'hour.mp3'
            with patch('mp3_resume.os.fsync', wraps=os.fsync) as synced:
                writer = AppendingMP3(path)
                writer.append(FRAME, INFO.duration)
                writer.flush_if_due()
                for _ in range(100):
                    writer.append(FRAME, INFO.duration)
                    writer.flush_if_due()
                self.assertEqual(synced.call_count, 0)
                writer.close()
                self.assertEqual(synced.call_count, 1)
            self.assertEqual(inspect_existing(path).frames, 101)

    def test_crash_recovery_keeps_complete_frames_after_buffered_resume(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'hour.mp3'
            path.write_bytes(FRAME * 3 + FRAME[:16])
            writer = AppendingMP3(path)
            writer.append(FRAME, INFO.duration)
            writer.flush_if_due()
            writer.close()
            self.assertEqual(path.read_bytes(), FRAME * 4)
            self.assertEqual(Path(str(path)+'.recovery-tail').read_bytes(), FRAME[:16])

    def test_hour_rollover_flushes_final_partial_batch(self):
        with TemporaryDirectory() as directory:
            base = datetime(2026, 10, 30, 13, 0, 0)
            now = [100.0]
            rec = Recorder(lambda _: None)
            rec.cfg = dict(folder=directory, filename_format='%d-%m-%y_%hh-%mm',
                           split_enabled=True, split_minutes=60, source_kind='device',
                           source='Line In', channels=1, sample_rate=44100,
                           bitrate=128, cue_shift_sec=0)
            with patch('mp3_resume.time.monotonic', side_effect=lambda: now[0]):
                with patch('engine.datetime', wraps=datetime) as clock:
                    clock.now.return_value = base
                    for _ in range(3):
                        rec._append_encoded_frame(FRAME, INFO.duration)
                    clock.now.return_value = base + timedelta(hours=1)
                    rec._append_encoded_frame(FRAME, INFO.duration)
                    rec._active_mp3.close()
            old = Path(directory) / '30-10-2026_13-00.mp3'
            new = Path(directory) / '30-10-2026_14-00.mp3'
            self.assertEqual(old.read_bytes(), FRAME * 3)
            self.assertEqual(new.read_bytes(), FRAME)
            self.assertTrue(old.with_suffix('.cue').exists())


if __name__ == '__main__':
    unittest.main()
