"""Regression coverage for retention minimum, writable configuration and audio fallback."""
import json
import os
import sys
import tempfile
import types
import unittest
from array import array
from pathlib import Path
from unittest.mock import Mock, patch

import engine
from engine import Recorder, normalize_retention_days
from monitor import remix_pcm


class RetentionMinimumTests(unittest.TestCase):
    def test_minimum_exact(self):
        self.assertEqual(normalize_retention_days(90), 90)
        self.assertEqual(normalize_retention_days('365'), 365)
        for n in [0, 1, 30, 89, 3651, 'oops', None]:
            with self.subTest(n=n), self.assertRaises(ValueError):
                normalize_retention_days(n)

    def test_legacy_value_is_not_allowed_to_decrease_retention(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            old = root / 'legacy'
            old.mkdir()
            (old / 'settings.json').write_text(json.dumps({'retention_days': 7, 'auto_schedule': True}), encoding='utf-8')
            new = root / 'user' / 'settings.json'
            with patch('engine.APP_DIR', old), patch('engine.SETTINGS_FILE', new):
                cfg = engine.load_config()
                self.assertEqual(cfg['retention_days'], 90)
                self.assertTrue(cfg['auto_schedule'])
                engine.save_config(cfg)
                self.assertTrue(new.exists())
                self.assertIn('"retention_days": 7', (old / 'settings.json').read_text())

    def test_cleanup_rejects_unsafe_minimum(self):
        with tempfile.TemporaryDirectory() as d:
            r = Recorder(lambda _: None)
            r.cfg = {'folder': d, 'retention_enabled': True, 'retention_days': 89}
            with self.assertRaises(ValueError):
                r.cleanup_old()


class WritableSettingsTests(unittest.TestCase):
    def test_user_data_location_on_windows(self):
        with patch.object(engine.sys, 'platform', 'win32'), patch.dict(os.environ, {'LOCALAPPDATA': 'C:/Users/Test/AppData/Local'}):
            self.assertTrue(str(engine.user_data_dir()).replace('\\', '/').endswith('RadioRecorder'))
            self.assertIn('AppData', str(engine.user_data_dir()))

    def test_atomic_saved_settings_survive_failed_replacement(self):
        with tempfile.TemporaryDirectory() as d:
            file = Path(d) / 'prefs' / 'settings.json'
            with patch('engine.SETTINGS_FILE', file):
                engine.save_config({'retention_days': 90, 'channels': 1})
                original = file.read_bytes()
                with patch('engine.os.replace', side_effect=PermissionError('access denied')):
                    with self.assertRaises(PermissionError):
                        engine.save_config({'retention_days': 120, 'channels': 2})
                self.assertEqual(file.read_bytes(), original)
                self.assertEqual(list(file.parent.glob('*.tmp')), [])

    def test_output_preflight_fails_before_rec_shows_running(self):
        with tempfile.TemporaryDirectory() as d, patch('engine.ffmpeg_path', return_value='ffmpeg.exe'):
            recorder = Recorder(lambda _: None)
            cfg = dict(engine.DEFAULTS, source='Line In', source_kind='device', folder=d)
            with patch('engine.tempfile.NamedTemporaryFile', side_effect=PermissionError('access denied')):
                with self.assertRaisesRegex(RuntimeError, 'Нет прав записи'):
                    recorder.start(cfg)
            self.assertFalse(recorder.running)
            self.assertIsNone(recorder.process)


class InputFallbackTests(unittest.TestCase):
    def make_fake(self, cap=1, supported_rate=48000):
        attempts = []
        def raw_stream(**kw):
            attempts.append((kw['channels'], kw['samplerate']))
            if kw['channels'] > cap or kw['samplerate'] != supported_rate:
                raise ValueError('Invalid number of channels or sample rate')
            class Stream:
                def __enter__(self): return self
                def __exit__(self, *_): return None
            return Stream()
        fake = types.SimpleNamespace(
            RawInputStream=raw_stream,
            query_devices=lambda: [{'name': 'Line In', 'max_input_channels': cap, 'default_samplerate': supported_rate}],
            query_hostapis=lambda: [{'name': 'Windows WASAPI'}])
        return fake, attempts

    def recorder(self):
        messages = []
        r = Recorder(messages.append)
        r.cfg = dict(engine.DEFAULTS, source='portaudio:Line In — Windows WASAPI (#0)',
                     source_kind='device', channels=2, sample_rate=48000, folder='.')
        return r, messages

    def test_stereo_falls_back_to_mono_before_ffmpeg(self):
        fake, attempts = self.make_fake(cap=1)
        rec, messages = self.recorder()
        with patch.dict(sys.modules, {'sounddevice': fake}), patch('monitor.resolve_device', return_value=0):
            rec._prepare_portaudio()
        self.assertEqual(rec.cfg['_capture_channels'], 1)
        self.assertTrue(any('моно' in line for line in messages))
        with patch('engine.ffmpeg_path', return_value='ffmpeg.exe'):
            cmd = rec._build_command()
        self.assertEqual(cmd[cmd.index('-ac')+1], '1')
        self.assertIn('pipe:0', cmd)

    def test_native_input_rate_uses_ffmpeg_resample(self):
        fake, _ = self.make_fake(cap=1, supported_rate=44100)
        rec, messages = self.recorder()
        with patch.dict(sys.modules, {'sounddevice': fake}), patch('monitor.resolve_device', return_value=0):
            rec._prepare_portaudio()
        self.assertEqual(rec.cfg['_input_rate'], 44100)
        with patch('engine.ffmpeg_path', return_value='ffmpeg.exe'):
            cmd = rec._build_command()
        first_rate = cmd.index('-ar')
        self.assertEqual(cmd[first_rate+1], '44100')
        self.assertEqual(cmd[cmd.index('-ar', first_rate+1)+1], '48000')

    def test_unusable_input_never_starts_ffmpeg(self):
        fake, _ = self.make_fake(cap=1, supported_rate=22050)
        fake.RawInputStream = Mock(side_effect=OSError('device busy'))
        rec, _ = self.recorder()
        with tempfile.TemporaryDirectory() as d, patch.dict(sys.modules, {'sounddevice': fake}), \
             patch('monitor.resolve_device', return_value=0), \
             patch('engine.ffmpeg_path', return_value='ffmpeg.exe'), \
             patch('engine.subprocess.Popen') as popen:
            cfg = dict(rec.cfg, folder=d)
            with self.assertRaisesRegex(RuntimeError, 'Не удалось открыть аудиовход'):
                rec.start(cfg)
            popen.assert_not_called()
            self.assertFalse(rec.running)


class PreviewCompatibilityTests(unittest.TestCase):
    def test_mono_to_stereo(self):
        data = array('h', [-500, 0, 1200]).tobytes()
        output = array('h')
        output.frombytes(remix_pcm(data, 1, 2))
        self.assertEqual(list(output), [-500, -500, 0, 0, 1200, 1200])

    def test_stereo_to_mono(self):
        data = array('h', [1000, 3000, -1000, -3000]).tobytes()
        output = array('h')
        output.frombytes(remix_pcm(data, 2, 1))
        self.assertEqual(list(output), [2000, -2000])

    def test_directsound_monitor_tries_mono_when_stereo_rejected(self):
        from monitor import AudioMonitor
        records = []
        attempts = []
        monitor = AudioMonitor(lambda active, message: records.append((active, message)))
        output_data = []
        class Input:
            def __init__(self, **kw):
                attempts.append((kw['channels'], kw['latency']))
                if kw['channels'] != 1:
                    raise OSError('DirectSound does not accept stereo capture')
            def __enter__(self): return self
            def __exit__(self, *_): return False
            def read(self, size):
                monitor._stop.set()
                return array('h', [1000] * size).tobytes(), False
        class Output:
            def __init__(self, **kw):
                if kw['channels'] != 2:
                    raise OSError('Speakers only available as stereo')
            def __enter__(self): return self
            def __exit__(self, *_): return False
            def write(self, data): output_data.append(data)
        fake = types.SimpleNamespace(RawInputStream=Input, RawOutputStream=Output)
        with patch('monitor.sd', fake):
            monitor._run_device(0, 1,
                                {'max_input_channels': 2, 'default_samplerate': 48000},
                                {'max_output_channels': 2, 'default_samplerate': 48000})
        self.assertEqual(attempts[0], (2, 'high'))
        self.assertTrue((1, 'high') in attempts)
        self.assertTrue(records[0][0])
        self.assertEqual(len(output_data[0]), 1024 * 2 * 2)

    def test_same_channels_untouched(self):
        data = array('h', [100, 250]).tobytes()
        self.assertEqual(remix_pcm(data, 2, 2), data)


if __name__ == '__main__':
    unittest.main()
