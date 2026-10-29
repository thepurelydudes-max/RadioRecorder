"""Presentation-only audio-device names (never alter saved capture identifiers)."""
import unittest
from unittest.mock import Mock

from device_names import friendly_device_name, display_device_choices, settings_device_choices, selection_label
from app import SettingsWindow, RadioRecorderUI


class FriendlyAudioNamesTest(unittest.TestCase):
    def test_strips_all_portaudio_driver_suffixes(self):
        for value in (
            'portaudio:Line In (Realtek Audio) — Windows WASAPI (#11)',
            'Line In (Realtek Audio) — MME (#0)',
            'Line In (Realtek Audio) — Windows DirectSound (#5)',
            'Line In (Realtek Audio) — Windows WDM-KS (#7)',
        ):
            self.assertEqual(friendly_device_name(value), 'Line In (Realtek Audio)')

    def test_preserves_actual_ordinary_device_names(self):
        self.assertEqual(friendly_device_name('USB AUDIO CODEC'), 'USB AUDIO CODEC')
        self.assertEqual(friendly_device_name('Line 1 (Virtual Audio Cable)'), 'Line 1 (Virtual Audio Cable)')
        self.assertEqual(friendly_device_name('Auto'), 'Auto')

    def test_hides_backend_but_keeps_identifiers_and_disambiguates(self):
        original = [
            ('Line In — Windows WASAPI (#7)', 'portaudio:Line In — Windows WASAPI (#7)'),
            ('Line In — MME (#3)', 'portaudio:Line In — MME (#3)'),
            ('Line In', 'dshow-guid'),
        ]
        display = display_device_choices(original)
        self.assertEqual([x[0] for x in display], ['Line In', 'Line In (2)', 'Line In (3)'])
        self.assertEqual([x[1] for x in display], [x[1] for x in original])
        self.assertEqual(selection_label(original[1][1], display, original), 'Line In (2)')
        self.assertEqual(selection_label('Line In — MME (#3)', display, original), 'Line In (2)')

    def test_settings_combo_shows_host_api_and_preserves_selection(self):
        chosen = 'portaudio:Line In — Windows WASAPI (#1)'
        original = [('Line In — MME (#2)', 'portaudio:Line In — MME (#2)'),
                    ('Line In — Windows WASAPI (#1)', chosen)]
        var = Mock()
        var.get.return_value = chosen
        window = Mock()
        window.winfo_exists.return_value = True
        window.v.return_value = var
        window.devices_box = {}
        SettingsWindow._devices_ready(window, original)
        self.assertEqual(window.devices_box['values'], ['Line In — MME', 'Line In — WASAPI'])
        var.set.assert_called_with('Line In — WASAPI')
        self.assertEqual(window.input_devices[1][1], chosen)

    def test_settings_identifies_directshow_without_changing_backend(self):
        choices = [('Line In (Realtek Audio)', '@device_cm_guid'),
                   ('Line In (Realtek Audio) — Windows WASAPI (#11)',
                    'portaudio:Line In (Realtek Audio) — Windows WASAPI (#11)'),
                   ('Line In (Realtek Audio) — MME (#1)',
                    'portaudio:Line In (Realtek Audio) — MME (#1)')]
        formatted = settings_device_choices(choices, mark_directshow=True)
        self.assertEqual(formatted[0][0], 'Line In (Realtek Audio) — DirectShow')
        self.assertEqual(formatted[1][0], 'Line In (Realtek Audio) — WASAPI')
        self.assertEqual(formatted[2][0], 'Line In (Realtek Audio) — MME')
        self.assertEqual([id for _, id in formatted], [id for _, id in choices])

    def test_settings_playback_keeps_driver_label(self):
        choices = [('Speakers — Windows DirectSound (#4)', 'Speakers — Windows DirectSound (#4)'),
                   ('Default', 'Default')]
        self.assertEqual(settings_device_choices(choices), [('Speakers — DirectSound', choices[0][1]), ('Default', 'Default')])

    def test_main_window_displays_only_source_name(self):
        window = Mock()
        window.cfg = dict(source_kind='device', source='portaudio:Line In — Windows WASAPI (#9)',
                          source_label='Line In — Windows WASAPI (#9)',
                          bitrate=128, channels=1, sample_rate=48000)
        RadioRecorderUI._reload_settings(window)
        window.source_title.set.assert_called_once_with('Источник: Line In')


if __name__ == '__main__':
    unittest.main()
