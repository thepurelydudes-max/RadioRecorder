"""v0.5.8 regression tests: filename preview, defaults, automatic-start flag."""
from __future__ import annotations
from datetime import datetime
import inspect
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import app
import autostart
import engine


class DummyVar:
    def __init__(self, value=''):
        self.value = value
    def get(self):
        return self.value
    def set(self, value):
        self.value = value


class FilenamePreviewTests(unittest.TestCase):
    def test_seconds_supported_and_placeholder_order_editable(self):
        self.assertEqual(engine.DEFAULTS['filename_format'], '%d-%m-%y_%hh-%mm')
        when = datetime(2026, 10, 27, 11, 23, 45)
        self.assertEqual(engine.preview_name('%y%m%d-%hh%mm%ss', when),
                         '20261027-112345.mp3')
        self.assertEqual(engine.preview_name('%ss_%d.%m.%y_%mm-%hh', when),
                         '45_27.10.2026_23-11.mp3')
        self.assertEqual(engine.preview_name(engine.DEFAULTS['filename_format'], when),
                         '27-10-2026_11-23.mp3')

    def test_preview_refreshes_when_template_changes_and_handles_errors(self):
        current = DummyVar('%d-%m-%y_%hh-%mm')
        output = DummyVar()
        fake = SimpleNamespace(v=lambda key: current, filename_preview=output)
        app.SettingsWindow._update_filename_preview(fake)
        self.assertTrue(output.get().startswith('Пример: '))
        self.assertTrue(output.get().endswith('.mp3'))
        current.set('%y%m%d-%hh%mm%ss')
        app.SettingsWindow._update_filename_preview(fake)
        self.assertNotIn('%ss', output.get())
        self.assertNotIn('%y', output.get())
        current.set('%xyz')
        app.SettingsWindow._update_filename_preview(fake)
        self.assertIn('Некорректный формат', output.get())

    def test_widget_uses_variable_trace_not_static_sample(self):
        initialize = inspect.getsource(app.SettingsWindow.__init__)
        storage = inspect.getsource(app.SettingsWindow._storage)
        self.assertIn("trace_add('write', self._update_filename_preview)", initialize)
        self.assertIn('textvariable=self.filename_preview', storage)
        self.assertIn('%ss — секунды', storage)
        self.assertNotIn('27-10-2026_11-00.mp3', storage)


class RetentionTests(unittest.TestCase):
    def test_new_install_default_is_90_without_replacing_saved_365(self):
        self.assertEqual(engine.DEFAULTS['retention_days'], 90)
        with tempfile.TemporaryDirectory() as tmp:
            settings = Path(tmp) / 'settings.json'
            settings.write_text(json.dumps({'retention_days':365,
                                            'filename_format':'%y%m%d-%hh%mm%ss'}), encoding='utf-8')
            with patch.object(engine, 'SETTINGS_FILE', settings):
                cfg = engine.load_config()
            self.assertEqual(cfg['retention_days'], 365)
            self.assertEqual(cfg['filename_format'], '%y%m%d-%hh%mm%ss')
            self.assertEqual(json.loads(settings.read_text())['retention_days'], 365)


class WindowsStartupTests(unittest.TestCase):
    def test_only_windows_autostart_marker_triggers_minimize(self):
        self.assertFalse(app.is_windows_autostart_launch([]))
        self.assertTrue(app.is_windows_autostart_launch(['--autostart']))
        self.assertFalse(app.is_windows_autostart_launch(['--other']))
        init = inspect.getsource(app.RadioRecorderUI.__init__)
        self.assertIn("if self.cfg.get('minimize_on_start') and is_windows_autostart_launch():", init)
        self.assertNotIn("if self.cfg.get('minimize_on_start'):\n", init)
        self.assertIn('Сворачивать при автозапуске', inspect.getsource(app.SettingsWindow._general))

    def test_frozen_autostart_command_marks_login_launch(self):
        with patch.object(sys, 'frozen', True, create=True), \
             patch.object(sys, 'executable', r'C:\\RadioRecorder\\RadioRecorder.exe'):
            exe, args = autostart.app_command(autostart=True)
            self.assertEqual(args, '--autostart')
            self.assertEqual(autostart.app_command(autostart=False)[1], '')
            xml = autostart.make_task_xml('S-1-5-21-1-2-3-1000', exe, args)
            self.assertIn(b'--autostart', xml.decode('utf-16').encode('utf-8'))

    def test_script_autostart_command_marks_login_launch(self):
        with patch.object(sys, 'frozen', False, create=True):
            exe, args = autostart.app_command(autostart=True)
            self.assertTrue(args.endswith(' --autostart'))
            self.assertIn('app.py', args)
            self.assertTrue(exe.endswith(('pythonw.exe', 'python', 'python.exe')))

    def test_both_registration_paths_set_marker(self):
        a = inspect.getsource(autostart.configure_login)
        b = inspect.getsource(autostart._set_registry)
        self.assertIn('app_command(autostart=True)', a)
        self.assertIn('app_command(autostart=True)', b)
        save = inspect.getsource(app.SettingsWindow._save)
        self.assertIn("new['minimize_on_start'] != old.get('minimize_on_start', False)", save)


if __name__ == '__main__':
    unittest.main()
