"""Regression coverage for reliable PortAudio/WASAPI recording at 512/250 ms."""
from __future__ import annotations
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch
import sys
import types
import unittest

from engine import (
    Recorder, PORTAUDIO_BLOCK_FRAMES, PORTAUDIO_LATENCY_SECONDS,
    PORTAUDIO_PIPE_QUEUE_SECONDS,
)
from mp3_resume import AppendingMP3, frame_info


class InputBufferTests(unittest.TestCase):
    def test_block_size_and_latency_are_separate(self):
        self.assertEqual(PORTAUDIO_BLOCK_FRAMES, 512)
        self.assertEqual(PORTAUDIO_LATENCY_SECONDS, .250)
        self.assertGreater(PORTAUDIO_LATENCY_SECONDS,
                           PORTAUDIO_BLOCK_FRAMES / 48000)
        self.assertGreater(PORTAUDIO_PIPE_QUEUE_SECONDS, 1.0)

    def test_portaudio_probe_requests_250ms_not_tiny_latency(self):
        recorder = Recorder(lambda _: None)
        recorder.cfg = {
            'source': 'portaudio:VAC Line 1 — Windows WASAPI (#0)',
            'sample_rate': 48000, 'channels': 2,
        }
        class Input:
            def __enter__(self):
                return self
            def __exit__(self, *_):
                pass
        calls = []
        def open_input(**kwargs):
            calls.append(kwargs)
            return Input()
        fake = types.SimpleNamespace(
            query_devices=lambda: [
                {'name': 'VAC Line 1', 'max_input_channels': 2,
                 'default_samplerate': 48000, 'hostapi': 0}],
            query_hostapis=lambda: [{'name': 'Windows WASAPI'}],
            RawInputStream=open_input,
        )
        with patch.dict(sys.modules, {'sounddevice': fake}):
            recorder._prepare_portaudio()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]['blocksize'], 512)
        self.assertEqual(calls[0]['latency'], .250)
        self.assertEqual(recorder.cfg['_capture_channels'], 2)

    def test_stereo_mp3_remains_stereo_after_mono_input_fallback(self):
        with TemporaryDirectory() as tmp:
            recorder = Recorder(lambda _: None)
            recorder.cfg = {
                'source_kind': 'device',
                'source': 'portaudio:VAC Line 1 — Windows WASAPI (#0)',
                'sample_rate': 48000, '_input_rate': 44100,
                'channels': 2, '_capture_channels': 1,
                'bitrate': 128, 'folder': tmp,
                'filename_format': '%d-%m-%y_%hh-%mm',
                'split_enabled': True, 'split_minutes': 60,
            }
            with patch('engine.ffmpeg_path', return_value='ffmpeg.exe'):
                command = recorder._build_command()
            self.assertEqual(command[command.index('-f') + 1], 's16le')
            self.assertEqual(command[command.index('-ac')+1], '1')
            self.assertEqual(command[command.index('-ac', command.index('-map'))+1], '2')
            self.assertEqual(command[command.index('-ar')+1], '44100')
            self.assertEqual(command[command.index('-ar', command.index('-map'))+1], '48000')

    def test_mp3_append_rejects_mixed_mono_stereo(self):
        mono_header = bytes.fromhex('fffb90c4')
        stereo_header = bytes.fromhex('fffb9004')
        mono_info = frame_info(mono_header)
        stereo_info = frame_info(stereo_header)
        self.assertIsNotNone(mono_info)
        self.assertIsNotNone(stereo_info)
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / 'hour.mp3'
            path.write_bytes(mono_header + bytes([0]) * (mono_info.length - 4))
            before = path.read_bytes()
            writer = AppendingMP3(path)
            try:
                with self.assertRaisesRegex(ValueError, 'Формат нового MP3'):
                    writer.append(stereo_header +
                                  bytes([0]) * (stereo_info.length - 4),
                                  stereo_info.duration)
            finally:
                writer.close()
            self.assertEqual(path.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
