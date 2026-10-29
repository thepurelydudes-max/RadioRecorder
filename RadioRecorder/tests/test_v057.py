"""Regression tests for the v0.5.7 settings-only changes."""
from __future__ import annotations
import inspect
import unittest
from unittest.mock import Mock

import app
import engine


class FakeVar:
    def __init__(self, value):
        self.value = value
    def get(self):
        return self.value
    def set(self, value):
        self.value = value


class SettingsV057Tests(unittest.TestCase):
    def test_new_install_retention_is_90_days(self):
        self.assertTrue(engine.DEFAULTS['retention_enabled'])
        self.assertEqual(engine.DEFAULTS['retention_days'], 90)
        self.assertEqual(engine.normalize_retention_days(90), 90)
        with self.assertRaises(ValueError):
            engine.normalize_retention_days(89)

    def test_channel_presentation_maps_back_to_ints(self):
        self.assertEqual(app.channels_to_label(1), 'Mono')
        self.assertEqual(app.channels_to_label('2'), 'Stereo')
        self.assertEqual(app.label_to_channels('Mono'), 1)
        self.assertEqual(app.label_to_channels('Stereo'), 2)
        for s in ('1', '2', 'Surround', ''):
            with self.assertRaises(ValueError):
                app.label_to_channels(s)
        source = inspect.getsource(app.SettingsWindow._record)
        self.assertIn("('channels', ('Mono', 'Stereo'), 9)", source)
        self.assertNotIn('Каналы: 1 = моно, 2 = стерео', source)
        self.assertIn("new['channels'] = label_to_channels(self.channel_label.get())",
                      inspect.getsource(app.SettingsWindow._save))

    def test_short_admin_label_and_clickable_control(self):
        src = inspect.getsource(app.SettingsWindow._general)
        self.assertIn("('start_with_admin', 'Запуск с правами администратора')", src)
        self.assertNotIn('первый раз UAC', src)
        self.assertNotIn('Planировщик', src)
        admin = FakeVar(False)
        startup = FakeVar(False)
        controls = Mock()
        fake_window = Mock()
        fake_window.v = lambda key: {'start_with_windows': startup, 'start_with_admin': admin}[key]
        fake_window.admin_checkbox = controls
        app.SettingsWindow._sync_startup_controls(fake_window)
        controls.configure.assert_called_once_with(state='normal')
        admin.set(True)
        app.SettingsWindow._on_admin_toggle(fake_window)
        self.assertTrue(startup.get())
        startup.set(False)
        app.SettingsWindow._sync_startup_controls(fake_window)
        self.assertFalse(admin.get())

    def test_settings_window_stays_fixed_size_and_has_no_hscroll(self):
        source = inspect.getsource(app.SettingsWindow._construct)
        self.assertIn('width=845', source)
        self.assertNotIn("orient='horizontal'", source)
        self.assertIn('wraplength=385', inspect.getsource(app.SettingsWindow._playback))
        self.assertIn('self.resizable(False, False)', inspect.getsource(app.SettingsWindow))


if __name__ == '__main__':
    unittest.main()
