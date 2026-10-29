"""Regression tests for defaults, Windows autostart, singleton and scheduler UI."""
import inspect
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import app
import autostart
import engine
from single_instance import SingleInstanceGuard, ERROR_ALREADY_EXISTS


class DefaultSettingsTests(unittest.TestCase):
    def test_default_stream_and_icy(self):
        self.assertEqual(engine.DEFAULTS['metadata_mode'], 'icy')
        self.assertEqual(engine.DEFAULTS['stream_url'], 'http://localhost:8000/')
        self.assertEqual(engine.DEFAULTS['metadata_icy_url'], 'http://localhost:8000/')

    def test_minimum_retention_days(self):
        self.assertEqual(engine.normalize_retention_days(90), 90)
        with self.assertRaises(ValueError):
            engine.normalize_retention_days(89)


class StartupCheckboxTests(unittest.TestCase):
    def test_when_startup_disabled_admin_flag_is_unchecked(self):
        self.assertEqual(autostart.normalize_startup_settings(False, True), (False, False))

    def test_disable_removes_both_sources_of_startup(self):
        with patch.object(autostart.sys, 'platform', 'win32'), \
             patch.object(autostart, '_task_exists', return_value=True), \
             patch.object(autostart, '_elevated_schtasks') as task, \
             patch.object(autostart, '_set_registry') as registry:
            autostart.configure_login(False, False, previous_elevated=True)
            task.assert_called_once_with('delete')
            registry.assert_called_once_with(False)

    def test_enable_normal_login_does_not_leave_admin_task(self):
        with patch.object(autostart.sys, 'platform', 'win32'), \
             patch.object(autostart, '_task_exists', return_value=True), \
             patch.object(autostart, '_elevated_schtasks') as task, \
             patch.object(autostart, '_set_registry') as registry:
            autostart.configure_login(True, False, previous_elevated=True)
            task.assert_called_once_with('delete')
            registry.assert_called_once_with(True)


class SingletonTests(unittest.TestCase):
    def test_refuses_second_instance(self):
        kernel = Mock()
        kernel.CreateMutexW.return_value = 222
        with SingleInstanceGuard(kernel=kernel, last_error=lambda: ERROR_ALREADY_EXISTS, windows=True) as guard:
            self.assertFalse(guard.acquire())
            self.assertIsNone(guard.handle)
        kernel.CloseHandle.assert_called_once_with(222)

    def test_new_instance_holds_and_releases_mutex(self):
        kernel = Mock()
        kernel.CreateMutexW.return_value = 333
        with SingleInstanceGuard(kernel=kernel, last_error=lambda: 0, windows=True) as guard:
            self.assertTrue(guard.acquire())
            self.assertTrue(guard.acquire())
            self.assertEqual(guard.handle, 333)
        kernel.CreateMutexW.assert_called_once()
        kernel.CloseHandle.assert_called_once_with(333)


class SchedulerLayoutTests(unittest.TestCase):
    def test_scheduler_keeps_original_no_scroll_grid(self):
        block = inspect.getsource(app.ScheduleWindow)
        self.assertNotIn('Scrollbar(', block)
        self.assertNotIn('Canvas(', block)
        self.assertIn('table.pack()', block)
        self.assertIn('for hour in range(24)', block)


if __name__ == '__main__':
    unittest.main()
