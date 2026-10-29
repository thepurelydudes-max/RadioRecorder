"""Exercise real FFmpeg where available; no physical audio device is used."""
from datetime import datetime, timedelta
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import wave

from engine import DEFAULTS, Recorder
from mp3_stream import MP3AppendFile, iter_mp3_frames


@unittest.skipUnless(shutil.which('ffmpeg'), 'FFmpeg is not installed')
class RecordingAppendIntegrationTests(unittest.TestCase):
    def test_two_complete_encoder_sessions_append_one_mp3_and_cue(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / 'source.wav'
            with wave.open(str(source), 'wb') as audio:
                audio.setnchannels(1)
                audio.setsampwidth(2)
                audio.setframerate(48000)
                audio.writeframes(b'\x00\x00' * 48000)
            recorder = Recorder(lambda _: None)
            cfg = dict(DEFAULTS, source_kind='stream', stream_url=str(source),
                       folder=str(root / 'archive'), retention_enabled=False,
                       reboot_on_error=False, channels=1, sample_rate=48000)
            self.addCleanup(recorder.stop)
            prefix = None
            for title in ('Before pause', 'After pause'):
                recorder.start(cfg, title)
                recorder.watch_thread.join(timeout=10)
                self.assertFalse(recorder.watch_thread.is_alive())
                recorder.stop()
                paths = list((root / 'archive').glob('*.mp3'))
                self.assertEqual(len(paths), 1)
                current = paths[0].read_bytes()
                if prefix is None:
                    prefix = current
                else:
                    self.assertTrue(current.startswith(prefix))
                    self.assertGreater(len(current), len(prefix))
            with MP3AppendFile(paths[0], 48000, 1) as recorded:
                self.assertGreater(recorded.duration, 2)
                self.assertLess(recorded.duration, 2.2)
            cue = paths[0].with_suffix('.cue').read_text(encoding='utf-8-sig')
            self.assertIn('Before pause', cue)
            self.assertIn('After pause', cue)
            self.assertEqual(cue.count('  TRACK '), 2)
            self.assertFalse(list((root / 'archive').glob('*_2*')))

    def test_hour_boundary_uses_same_capture_and_each_file_decodes(self):
        command = [shutil.which('ffmpeg'), '-v', 'error', '-f', 'lavfi',
                   '-i', 'sine=frequency=440:sample_rate=48000:duration=1',
                   '-ac', '1', '-c:a', 'libmp3lame', '-b:a', '128k',
                   '-reservoir', '0', '-write_xing', '0', '-id3v2_version', '0',
                   '-f', 'mp3', 'pipe:1']
        encoded = subprocess.run(command, capture_output=True, check=True, timeout=10).stdout
        import io
        frames = list(iter_mp3_frames(io.BytesIO(encoded)))
        self.assertGreater(len(frames), 20)
        with tempfile.TemporaryDirectory() as folder:
            recorder = Recorder(lambda _: None)
            recorder.cfg = dict(DEFAULTS, folder=folder, sample_rate=48000, channels=1)
            recorder.running = True
            recorder.last_title = 'Boundary'
            start = datetime(2026, 10, 29, 18, 59, 59)
            recorder._process_spawn_at = start
            self.addCleanup(recorder._close_audio_output)
            for index, frame in enumerate(frames):
                recorder._write_mp3_frame(frame, start if index < len(frames)//2
                                          else start + timedelta(seconds=1))
            recorder._close_audio_output()
            paths = sorted(Path(folder).glob('*.mp3'))
            self.assertEqual([path.name for path in paths],
                             ['29-10-2026_18-00.mp3', '29-10-2026_19-00.mp3'])
            for path in paths:
                decoded = subprocess.run([shutil.which('ffmpeg'), '-v', 'error',
                                          '-i', str(path), '-f', 'null', '-'],
                                         capture_output=True, timeout=10)
                self.assertEqual(decoded.returncode, 0, decoded.stderr)
                self.assertEqual(decoded.stderr, b'')


if __name__ == '__main__':
    unittest.main()
