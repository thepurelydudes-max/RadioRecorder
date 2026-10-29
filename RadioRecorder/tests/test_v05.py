"""Cross-platform checks for Python v0.5 capture, metering and elevated startup."""
import unittest
import xml.etree.ElementTree as ET
from unittest.mock import patch
from array import array

from audio_levels import peak_dbfs
from autostart import make_task_xml
from app import list_recording_choices
from engine import Recorder
from monitor import AudioMonitor


class PeakMeterTests(unittest.TestCase):
    def test_empty_and_silence(self):
        self.assertEqual(peak_dbfs(b''), -60)
        self.assertEqual(peak_dbfs(b'\x00\x00' * 100), -60)

    def test_level_changes_with_audio(self):
        small = (array('h', [2000] * 100)).tobytes()
        big = (array('h', [18000] * 100)).tobytes()
        self.assertLess(peak_dbfs(small), peak_dbfs(big))
        self.assertGreater(peak_dbfs(big), -10)

    def test_monitor_sends_audio_peak(self):
        levels = []
        monitor = AudioMonitor(lambda *_: None, levels.append)
        monitor._report_level((array('h', [20000] * 20)).tobytes())
        self.assertEqual(len(levels), 1)
        self.assertGreater(levels[0], -10)


class InputChoiceTests(unittest.TestCase):
    def test_dshow_compatibility_and_windows_fallback(self):
        with patch('app.list_audio_devices', return_value=[('Line In DirectShow', 'dshow-guid')]), \
             patch('app.list_monitor_input_devices', return_value=[('Auto', 'Auto'), ('Line In — WASAPI (#1)', 'Line In — WASAPI (#1)')]):
            inputs = list_recording_choices('ffmpeg.exe')
        self.assertEqual(inputs[0], ('Line In DirectShow', 'dshow-guid'))
        self.assertEqual(inputs[1], ('Line In — WASAPI (#1)', 'portaudio:Line In — WASAPI (#1)'))

    def test_without_ffmpeg_still_shows_windows_inputs(self):
        with patch('app.list_audio_devices', side_effect=AssertionError('should not call')), \
             patch('app.list_monitor_input_devices', return_value=[('Auto', 'Auto'), ('Microphone — MME (#5)', 'Microphone — MME (#5)')]):
            inputs = list_recording_choices('')
        self.assertEqual(inputs[0][1], 'portaudio:Microphone — MME (#5)')

    def test_ffmpeg_uses_stdin_for_portaudio_capture(self):
        r = Recorder(lambda *_: None)
        r.cfg = {'source_kind': 'device', 'source': 'portaudio:Line In — WASAPI (#1)',
                 'sample_rate': 48000, 'channels': 1, 'bitrate': 128,
                 'filename_format': '%d-%m-%y_%hh-%mm', 'folder': '.',
                 'split_minutes': 60, 'split_enabled': True}
        with patch('engine.ffmpeg_path', return_value='ffmpeg.exe'):
            cmd = r._build_command()
        self.assertIn('pipe:0', cmd)
        self.assertIn('s16le', cmd)
        self.assertNotIn('dshow', cmd)


class ElevatedStartupTests(unittest.TestCase):
    def test_task_runs_on_login_as_interactive_user_with_highest_rights(self):
        sid = 'S-1-5-21-123-456-789-1001'
        task = ET.fromstring(make_task_xml(sid, r'C:\Program Files\RadioRecorder\RadioRecorder.exe'))
        ns = {'task': 'http://schemas.microsoft.com/windows/2004/02/mit/task'}
        self.assertEqual(task.findtext('.//task:RunLevel', namespaces=ns), 'HighestAvailable')
        self.assertEqual(task.findtext('.//task:LogonType', namespaces=ns), 'InteractiveToken')
        self.assertEqual(task.findtext('.//task:LogonTrigger/task:UserId', namespaces=ns), sid)
        self.assertEqual(task.findtext('.//task:ExecutionTimeLimit', namespaces=ns), 'PT0S')

if __name__ == '__main__':
    unittest.main()

class StartupPersistenceTests(unittest.TestCase):
    def test_elevated_autostart_config_survives_restart(self):
        from engine import load_config, save_config
        from tempfile import TemporaryDirectory
        from pathlib import Path
        with TemporaryDirectory() as d:
            with patch('engine.SETTINGS_FILE', Path(d) / 'settings.json'):
                cfg = load_config()
                cfg['start_with_windows'] = True
                cfg['start_with_admin'] = True
                save_config(cfg)
                restored = load_config()
                self.assertTrue(restored['start_with_admin'])
                self.assertTrue(restored['start_with_windows'])
