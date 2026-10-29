import unittest
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

from monitor import AudioMonitor, best_input_for_source, normalize_name, resolve_device


class DeviceMatchTest(unittest.TestCase):
    def setUp(self):
        self.devices = [
            {'name': 'Line In (Realtek(R) Audio)', 'hostapi': 0, 'max_input_channels': 2, 'max_output_channels': 0},
            {'name': 'Speakers (USB AUDIO CODEC)', 'hostapi': 1, 'max_input_channels': 0, 'max_output_channels': 2},
            {'name': 'Line 1 (Virtual Audio Cable)', 'hostapi': 1, 'max_input_channels': 2, 'max_output_channels': 0},
            {'name': 'Speakers (Realtek(R) Audio)', 'hostapi': 0, 'max_input_channels': 0, 'max_output_channels': 2},
        ]
        self.apis = [{'name': 'MME'}, {'name': 'Windows WASAPI'}]

    def test_default_output(self):
        self.assertIsNone(resolve_device('Default', 'output', self.devices, self.apis))

    def test_exact_output(self):
        self.assertEqual(resolve_device('Speakers (USB AUDIO CODEC) — Windows WASAPI (#1)',
                                        'output', self.devices, self.apis), 1)

    def test_missing_device_errors(self):
        with self.assertRaises(RuntimeError):
            resolve_device('Speakers (Unknown) — MME (#999)', 'output', self.devices, self.apis)

    def test_auto_input_matches_label_not_wrong_speaker(self):
        self.assertEqual(best_input_for_source('Line 1 (Virtual Audio Cable)', self.devices), 2)
        self.assertEqual(best_input_for_source('Line In (Realtek(R) Audio)', self.devices), 0)
        self.assertIsNone(best_input_for_source('Completely missing source', self.devices))

    def test_normalization(self):
        self.assertEqual(normalize_name('Line 1 (Virtual Audio Cable)'),
                         normalize_name('line 1 virtual-audio cable'))


class MonitoringBehaviorTest(unittest.TestCase):
    def test_play_toggle_never_starts_or_stops_recorder(self):
        from app import RadioRecorderUI
        class FakeMonitor:
            running = False
            def start(self, cfg, ffmpeg, devices):
                self.running = True
            def stop(self):
                self.running = False
        class FakeStatus:
            def set(self, value):
                self.value = value
        paints = []
        mock_recorder = SimpleNamespace(start=lambda *a: self.fail('Play started recording'),
                                        stop=lambda *a: self.fail('Play stopped recording'))
        ui = SimpleNamespace(monitor=FakeMonitor(), cfg={'source_kind':'device','source_label':'Line In'},
                             monitor_status=FakeStatus(), _paint_monitor=lambda active: paints.append(active),
                             recorder=mock_recorder)
        with patch('app.ffmpeg_path', return_value='ffmpeg.exe'):
            RadioRecorderUI._toggle_monitor(ui)
            self.assertTrue(ui.monitor.running)
            RadioRecorderUI._toggle_monitor(ui)
            self.assertFalse(ui.monitor.running)
        self.assertEqual(paints, [True, False])

    def test_rapid_play_stop_play_restarts_monitor(self):
        first_started = threading.Event()
        second_started = threading.Event()
        release_first = threading.Event()
        callbacks = []
        monitor = AudioMonitor(lambda active, msg: callbacks.append(active))
        count = []
        def mock_device(input_id, output_id, input_info, output_info):
            count.append(1)
            monitor.on_state(True, 'connected')
            if len(count) == 1:
                first_started.set()
                monitor._stop.wait(timeout=2)
                release_first.wait(timeout=2)
            else:
                second_started.set()
                monitor._stop.wait(timeout=2)
        devices = [{'name': 'Line In', 'hostapi': 0, 'max_input_channels': 1, 'max_output_channels': 0},
                   {'name': 'Speakers', 'hostapi': 0, 'max_input_channels': 0, 'max_output_channels': 2}]
        apis = [{'name':'MME'}]
        with patch('monitor.sd', object()), patch('monitor._devices', return_value=(devices,apis)), \
             patch.object(monitor, '_run_device', side_effect=mock_device):
            cfg = {'source_kind':'device', 'monitor_input':'Line In — MME (#0)',
                   'monitor_device':'Speakers — MME (#1)'}
            monitor.start(cfg, 'ffmpeg.exe')
            self.assertTrue(first_started.wait(timeout=1.5))
            monitor.stop()
            monitor.start(cfg, 'ffmpeg.exe')
            self.assertTrue(monitor.running)
            release_first.set()
            self.assertTrue(second_started.wait(timeout=1.5))
            self.assertGreaterEqual(len(count), 2)
            monitor.close()
            self.assertFalse(monitor.running)


if __name__ == '__main__':
    unittest.main()
