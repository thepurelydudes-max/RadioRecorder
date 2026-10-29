import unittest
from unittest.mock import Mock, MagicMock
from app import SettingsWindow, RadioRecorderUI


class SettingsDriverLabelsTest(unittest.TestCase):
    def test_playback_combo_keeps_wasapi_mme_tags(self):
        window = Mock()
        window.winfo_exists.return_value = True
        window.output_box = MagicMock()
        window.monitor_input_box = MagicMock()
        out = [('Default', 'Default'), ('Speakers — Windows WASAPI (#8)', 'Speakers — Windows WASAPI (#8)')]
        inp = [('Auto', 'Auto'), ('Mic — MME (#2)', 'Mic — MME (#2)')]
        SettingsWindow._playback_devices_ready(window, out, inp, None)
        self.assertEqual(window.output_box.__setitem__.call_args.args[1], ['Default', 'Speakers — WASAPI'])
        self.assertEqual(window.monitor_input_box.__setitem__.call_args.args[1], ['Auto', 'Mic — MME'])

    def test_main_caption_has_no_host_api(self):
        window = Mock()
        window.cfg = dict(source_kind='device',
                          source='portaudio:Mic — Windows WASAPI (#8)',
                          source_label='Mic — Windows WASAPI (#8)',
                          bitrate=128, channels=2, sample_rate=48000)
        RadioRecorderUI._reload_settings(window)
        window.source_title.set.assert_called_once_with('Источник: Mic')


if __name__ == '__main__':
    unittest.main()
