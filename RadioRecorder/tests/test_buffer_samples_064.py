"""One user-facing sample buffer setting, automatically derived ms latency."""
from __future__ import annotations

import inspect
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import app
import engine
from engine import (Recorder, AUDIO_BUFFER_CHOICES, audio_buffer_samples,
                    audio_buffer_latency_ms, audio_buffer_latency_seconds)


class BufferSamplesSettingsTests(unittest.TestCase):
    def test_one_setting_with_allowed_values_and_default(self):
        self.assertEqual(engine.DEFAULTS['buffer_samples'], 512)
        self.assertEqual(AUDIO_BUFFER_CHOICES, (128, 256, 512, 1024, 2048))
        self.assertEqual(audio_buffer_samples({}), 512)
        for value in AUDIO_BUFFER_CHOICES:
            self.assertEqual(audio_buffer_samples({'buffer_samples': str(value)}), value)
        for value in (0, -1, 32, 4096, 'wrong', None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                audio_buffer_samples({'buffer_samples': value})

    def test_latency_scales_with_sample_rate_and_selected_block(self):
        self.assertEqual(audio_buffer_latency_ms(256, 48000), 128)
        self.assertEqual(audio_buffer_latency_ms(512, 48000), 256)
        self.assertEqual(audio_buffer_latency_ms(1024, 48000), 512)
        self.assertEqual(audio_buffer_latency_ms(2048, 48000), 1024)
        self.assertEqual(audio_buffer_latency_ms(512, 44100), 279)
        self.assertAlmostEqual(audio_buffer_latency_seconds(512, 44100),
                               (24 * 512) / 44100)
        with self.assertRaises(ValueError):
            audio_buffer_latency_seconds(512, 0)

    def test_saved_setting_survives_reload_and_older_configs_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg_file = root / 'settings.json'
            empty_app = root / 'other'
            empty_app.mkdir()
            with patch('engine.SETTINGS_FILE', cfg_file), patch('engine.APP_DIR', empty_app):
                cfg = dict(engine.DEFAULTS, buffer_samples=2048)
                engine.save_config(cfg)
                self.assertEqual(json.loads(cfg_file.read_text())['buffer_samples'], 2048)
                self.assertEqual(engine.load_config()['buffer_samples'], 2048)
                cfg_file.write_text('{"sample_rate": 44100}', encoding='utf-8')
                self.assertEqual(engine.load_config()['buffer_samples'], 512)
                cfg_file.write_text('{"buffer_samples": 9999}', encoding='utf-8')
                self.assertEqual(engine.load_config()['buffer_samples'], 512)

    def test_directshow_buffer_latency_follows_samples_not_fixed_ms(self):
        with tempfile.TemporaryDirectory() as tmp:
            recorder = Recorder(lambda _: None)
            recorder.cfg = dict(engine.DEFAULTS,
                                source_kind='device', source='Line In',
                                folder=tmp, buffer_samples=2048,
                                sample_rate=48000)
            with patch('engine.ffmpeg_path', return_value='ffmpeg.exe'):
                cmd = recorder._build_command()
            self.assertEqual(cmd[cmd.index('-audio_buffer_size') + 1], '1024')

    def test_user_ui_shows_only_samples_not_separate_ms_setting(self):
        record_src = inspect.getsource(app.SettingsWindow._record)
        save_src = inspect.getsource(app.SettingsWindow._save)
        self.assertIn("text='Буфер аудиозахвата:'", record_src)
        self.assertIn("textvariable=self.v('buffer_samples')", record_src)
        self.assertNotIn("textvariable=self.v('buffer_ms')", record_src)
        self.assertIn("'buffer_samples'", save_src)


if __name__ == '__main__':
    unittest.main()
