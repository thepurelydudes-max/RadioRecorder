"""Headless lifecycle tests with controlled audio and network boundaries."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import queue
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import app


class Value:
    def __init__(self, value=None):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        if threading.current_thread() is not threading.main_thread():
            raise AssertionError('Tk state updated from a worker')
        self.value = value


class ControlledRecorder:
    def __init__(self):
        self.running = False
        self.process = None
        self.cfg = {}
        self.lock = threading.RLock()
        self.start_entered = threading.Event()
        self.release_start = threading.Event()
        self.stop_entered = threading.Event()
        self.stop_retried = threading.Event()
        self.release_stop = threading.Event()
        self.release_stop.set()
        self.calls = []
        self.fail_start = False
        self.stop_failures = 0

    def start(self, cfg, title):
        self.calls.append(('start', deepcopy(cfg), threading.current_thread()))
        self.start_entered.set()
        self.release_start.wait(.5)
        self.cfg = cfg.copy()
        if self.fail_start:
            raise RuntimeError('disconnected input')
        self.running = True
        self.process = SimpleNamespace(poll=lambda: None)

    def stop(self):
        self.calls.append(('stop', None, threading.current_thread()))
        self.stop_entered.set()
        if len([call for call in self.calls if call[0] == 'stop']) >= 2:
            self.stop_retried.set()
        if self.stop_failures:
            self.stop_failures -= 1
            raise RuntimeError('watcher still finalizing')
        self.release_stop.wait(.5)
        self.running = False
        self.process = None

    def update_retention(self, enabled, days):
        with self.lock:
            self.cfg.update(retention_enabled=enabled, retention_days=days)


class AsyncRecordingUITests(unittest.TestCase):
    def make_ui(self):
        ui = object.__new__(app.RadioRecorderUI)
        recorder = ControlledRecorder()
        metadata = SimpleNamespace(last_title='Initial', start=Mock(), stop=Mock())
        events = queue.Queue()
        cfg = dict(folder='recordings', source='input', source_kind='device',
                   channels=1, bitrate=128, sample_rate=48000,
                   metadata_mode='api', metadata_api_url='old-api', metadata_icy_url='',
                   metadata_password='', metadata_username='',
                   retention_enabled=True, retention_days=90, ask_on_exit=False,
                   auto_schedule=True, schedule=[[True]*24 for _ in range(7)])
        attrs = dict(cfg=cfg, recorder=recorder, metadata=metadata, events=events,
                     monitor=SimpleNamespace(running=False, close=Mock()),
                     auto=Value(True), status_var=Value(), elapsed=Value(),
                     _closing=False, _closed=False, _recording_action=None,
                     _recording_generation=0, _recording_automatic=False,
                     manual_pause_until=None, auto_retry_after=None,
                     _reload_settings=Mock(), _paint_recording=Mock(),
                     _show_level=Mock(), _log=Mock(), after=Mock(),
                     winfo_exists=Mock(return_value=True), destroy=Mock())
        for key, value in attrs.items():
            object.__setattr__(ui, key, value)
        control_type = getattr(app, 'RecordingControl', None)
        if control_type:
            object.__setattr__(ui, 'recording_control', control_type(
                recorder, metadata, lambda value: events.put(('recording_state', value)),
                ui.monitor.close))
        self.addCleanup(self.cleanup_ui, ui)
        return ui

    def cleanup_ui(self, ui):
        ui.recorder.release_start.set()
        ui.recorder.release_stop.set()
        control = ui.__dict__.get('recording_control')
        if control:
            control.close()
            control._thread.join(timeout=2)
            self.assertFalse(control._thread.is_alive(), 'lifecycle worker leaked')

    def pump_until(self, ui, predicate):
        deadline = time.monotonic() + 2
        while not predicate() and time.monotonic() < deadline:
            ui._drain()
            if predicate():
                break
            try:
                event = ui.events.get(timeout=max(.001, deadline - time.monotonic()))
            except queue.Empty:
                break
            ui.events.put(event)
        ui._drain()
        self.assertTrue(predicate(), 'lifecycle did not finish')

    def test_start_returns_before_device_probe_finishes(self):
        ui = self.make_ui()
        before = time.monotonic()
        ui._start()
        self.assertLess(time.monotonic() - before, .15)
        self.assertTrue(ui.recorder.start_entered.wait(.2))
        ui.recorder.release_start.set()
        self.pump_until(ui, lambda: ui._recording_action is None)
        self.assertTrue(ui.recorder.running)
        self.assertEqual(ui.status_var.get(), '● Запись идёт')
        self.assertIsNot(ui.recorder.calls[0][2], threading.main_thread())

    def test_stop_cancels_pending_start_without_metadata_connection(self):
        ui = self.make_ui()
        ui._start()
        self.assertTrue(ui.recorder.start_entered.wait(.2))
        with patch('app.datetime') as clock:
            clock.now.return_value = datetime(2026, 10, 27, 11, 23)
            ui._stop_manual()
            ui.recorder.release_start.set()
            self.pump_until(ui, lambda: ui._recording_action is None)
        self.assertFalse(ui.recorder.running)
        self.assertEqual(ui.manual_pause_until, datetime(2026, 10, 27, 12))
        self.assertTrue(ui.auto.get())
        ui.metadata.start.assert_not_called()
        self.assertIn('Пауза до 12:00', ui.status_var.get())

    def test_scheduler_cancels_pending_start_at_disabled_hour(self):
        ui = self.make_ui()
        ui._start(automatic=True)
        self.assertTrue(ui.recorder.start_entered.wait(.2))
        ui.cfg['schedule'][1][12] = False
        ui._schedule_check(datetime(2026, 10, 27, 12))
        ui.recorder.release_start.set()
        self.pump_until(ui, lambda: ui._recording_action is None)
        self.assertFalse(ui.recorder.running)
        ui.metadata.start.assert_not_called()

    @patch('app.windows_keep_awake')
    def test_close_waits_for_pending_start_cleanup_without_blocking_tk(self, awake):
        ui = self.make_ui()
        ui._start()
        self.assertTrue(ui.recorder.start_entered.wait(.2))
        before = time.monotonic()
        ui._on_close()
        self.assertLess(time.monotonic() - before, .15)
        ui.destroy.assert_not_called()
        ui._on_close()  # repeated window close cannot queue duplicate shutdowns
        ui.recorder.release_start.set()
        self.pump_until(ui, lambda: ui.destroy.called)
        self.assertFalse(ui.recorder.running)
        self.assertFalse(ui.auto.get())
        ui.monitor.close.assert_called_once()
        ui.metadata.start.assert_not_called()

    def test_start_during_stop_waits_and_uses_new_settings_snapshot(self):
        ui = self.make_ui()
        ui.recorder.release_start.set()
        ui._start()
        self.pump_until(ui, lambda: ui._recording_action is None)
        ui.recorder.release_stop.clear()
        ui._stop()
        self.assertTrue(ui.recorder.stop_entered.wait(.2))
        ui.cfg['source'] = 'second input'
        ui.cfg['metadata_api_url'] = 'new-api'
        ui._start()
        ui.cfg['source'] = 'third input'
        ui.recorder.release_stop.set()
        self.pump_until(ui, lambda: ui._recording_action is None)
        self.assertTrue(ui.recorder.running)
        starts = [call for call in ui.recorder.calls if call[0] == 'start']
        self.assertEqual([call[1]['source'] for call in starts], ['input', 'second input'])
        ui.metadata.start.assert_called_with('api', 'new-api', '', '')

    def test_retention_saved_during_start_is_applied_to_active_session(self):
        ui = self.make_ui()
        ui._start()
        self.assertTrue(ui.recorder.start_entered.wait(.2))
        self.assertIn('recording_control', ui.__dict__)
        ui.recording_control.update_retention(False, 180)
        ui.recorder.release_start.set()
        self.pump_until(ui, lambda: ui._recording_action is None)
        self.assertFalse(ui.recorder.cfg['retention_enabled'])
        self.assertEqual(ui.recorder.cfg['retention_days'], 180)

    @patch('app.messagebox.showerror')
    def test_automatic_start_failure_sets_backoff_without_dialog(self, showerror):
        ui = self.make_ui()
        ui.recorder.fail_start = True
        ui.recorder.release_start.set()
        ui._start(automatic=True)
        self.pump_until(ui, lambda: ui._recording_action is None)
        self.assertIsNotNone(ui.auto_retry_after)
        self.assertFalse(ui.recorder.running)
        showerror.assert_not_called()
        ui.metadata.start.assert_not_called()

    def test_heartbeat_does_not_duplicate_a_pending_start(self):
        ui = self.make_ui()
        ui._start(automatic=True)
        self.assertTrue(ui.recorder.start_entered.wait(.2))
        ui._heartbeat()
        self.assertEqual(len([c for c in ui.recorder.calls if c[0] == 'start']), 1)
        self.assertIn('Запуск', ui.status_var.get())

    @patch('app.windows_keep_awake')
    def test_close_retries_unfinished_stop_before_destroying_window(self, awake):
        ui = self.make_ui()
        ui.recorder.release_start.set()
        ui._start()
        self.pump_until(ui, lambda: ui._recording_action is None)
        ui.recorder.stop_failures = 1
        ui.recorder.release_stop.clear()
        ui._on_close()
        self.assertTrue(ui.recorder.stop_retried.wait(1))
        ui._drain()
        ui.destroy.assert_not_called()
        ui.recorder.release_stop.set()
        self.pump_until(ui, lambda: ui._closed)
        self.assertFalse(ui.recorder.running)


if __name__ == '__main__':
    unittest.main()
