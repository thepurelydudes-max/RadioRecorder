"""Auto mode must survive Stop; manual pause lasts through the current hour."""
import unittest
from datetime import datetime
from unittest.mock import Mock, patch
from types import SimpleNamespace

from engine import auto_should_start, next_clock_hour
from app import RadioRecorderUI


class AutoModePolicyTests(unittest.TestCase):
    def setUp(self):
        self.cfg = {'auto_schedule': True, 'schedule': [[True] * 24 for _ in range(7)]}

    def test_stop_blocks_only_remaining_current_hour(self):
        stop_at = datetime(2026, 10, 27, 11, 23, 10)
        until = next_clock_hour(stop_at)
        self.assertEqual(until, datetime(2026, 10, 27, 12, 0))
        self.assertFalse(auto_should_start(self.cfg, datetime(2026,10,27,11,59,59), until))
        self.assertTrue(auto_should_start(self.cfg, datetime(2026,10,27,12,0), until))

    def test_stop_at_exact_hour_still_waits_full_hour(self):
        self.assertEqual(next_clock_hour(datetime(2026,10,27,12)), datetime(2026,10,27,13))

    def test_stop_before_midnight(self):
        self.assertEqual(next_clock_hour(datetime(2026,10,27,23,40)), datetime(2026,10,28,0))
        self.assertTrue(auto_should_start(self.cfg, datetime(2026,10,28,0), datetime(2026,10,28,0)))

    def test_next_hour_disabled_will_wait_for_next_allowed_hour(self):
        pause_until = datetime(2026,10,27,12)
        self.cfg['schedule'][1][12] = False
        self.assertFalse(auto_should_start(self.cfg, datetime(2026,10,27,12), pause_until))
        self.assertTrue(auto_should_start(self.cfg, datetime(2026,10,27,13), pause_until))

    def test_auto_persists_when_disabled(self):
        self.cfg['auto_schedule'] = False
        self.assertFalse(auto_should_start(self.cfg, datetime(2026,10,27,13)))

    def test_program_relaunch_has_no_manual_pause(self):
        self.assertTrue(auto_should_start(self.cfg, datetime(2026,10,27,11,23), None))


class AutoModeUITests(unittest.TestCase):
    def fake_ui(self):
        dummy = SimpleNamespace(
            auto=SimpleNamespace(get=lambda: True),
            cfg={'auto_schedule': True, 'schedule': [[True]*24 for _ in range(7)]},
            recorder=SimpleNamespace(running=False),
            manual_pause_until=None,
            auto_retry_after=None,
            _start=Mock(), _stop=Mock(), _log=Mock(),
            status_var=SimpleNamespace(set=Mock()),
        )
        return dummy

    def test_stop_does_not_uncheck_auto(self):
        ui = self.fake_ui()
        with patch('app.datetime') as clock:
            clock.now.return_value = datetime(2026,10,27,11,23)
            RadioRecorderUI._stop_manual(ui)
        self.assertEqual(ui.manual_pause_until, datetime(2026,10,27,12))
        ui._stop.assert_called_once()
        self.assertTrue(ui.auto.get())

    def test_no_immediate_restart_at_11_23_but_yes_at_noon(self):
        ui = self.fake_ui()
        ui.manual_pause_until = datetime(2026,10,27,12)
        RadioRecorderUI._schedule_check(ui, datetime(2026,10,27,11,23,1))
        ui._start.assert_not_called()
        RadioRecorderUI._schedule_check(ui, datetime(2026,10,27,12))
        ui._start.assert_called_once_with(automatic=True)

    def test_autostart_on_launch_when_hour_scheduled(self):
        ui = self.fake_ui()
        RadioRecorderUI._schedule_check(ui, datetime(2026,10,27,11,23))
        ui._start.assert_called_once_with(automatic=True)

    def test_autostart_ignores_unscheduled_hours(self):
        ui = self.fake_ui()
        ui.cfg['schedule'][1][11] = False
        RadioRecorderUI._schedule_check(ui, datetime(2026,10,27,11,23))
        ui._start.assert_not_called()


if __name__ == '__main__':
    unittest.main()
