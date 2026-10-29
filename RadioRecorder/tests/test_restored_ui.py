"""Visual-layout regression tests: only sizing lock changes the v0.5.4 UI."""
from __future__ import annotations
import inspect
import unittest
import app


class OriginalWindowAppearanceTests(unittest.TestCase):
    def test_main_window_is_original_650_by_385_and_fixed(self):
        body = inspect.getsource(app.RadioRecorderUI.__init__)
        self.assertIn("self.geometry('650x385')", body)
        self.assertIn('self.resizable(False, False)', body)
        self.assertNotIn('constrain_window', body)

    def test_main_uses_original_direct_frame_not_canvas(self):
        body = inspect.getsource(app.RadioRecorderUI._build)
        self.assertIn('wrap = ttk.Frame(self, padding=8)', body)
        self.assertIn("wrap.pack(fill='both', expand=True)", body)
        self.assertNotIn('main_canvas', body)
        self.assertNotIn('outer_x', body)
        self.assertNotIn('outer_y', body)

    def test_settings_keep_previous_fixed_canvas_vertical_scroll(self):
        body = inspect.getsource(app.SettingsWindow)
        self.assertIn('self.resizable(False, False)', body)
        self.assertIn('width=845', body)
        self.assertIn('height=max(280, min(610, self.winfo_screenheight()-160))', body)
        self.assertNotIn("orient='horizontal'", inspect.getsource(app.SettingsWindow._construct))
        self.assertNotIn('constrain_window', body)

    def test_archive_original_size_and_single_row_buttons(self):
        body = inspect.getsource(app.RadioRecorderUI._archive)
        self.assertIn("window.geometry('650x450')", body)
        self.assertIn('window.resizable(False, False)', body)
        self.assertIn("text='Воспроизвести MP3'", body)
        self.assertIn("text='Открыть папку'", body)
        self.assertNotIn('.grid(', body)

    def test_scheduler_original_grid_fixed_and_without_scrollbars(self):
        body = inspect.getsource(app.ScheduleWindow)
        self.assertIn('self.resizable(False, False)', body)
        self.assertIn('for hour in range(24):', body)
        self.assertNotIn('Scrollbar(', body)
        self.assertNotIn('Canvas(', body)

    def test_autostart_controls_still_sync_and_single_instance_still_on(self):
        settings = inspect.getsource(app.SettingsWindow)
        self.assertIn('_sync_startup_controls()', settings)
        main = inspect.getsource(app.main)
        self.assertIn('SingleInstanceGuard()', main)


if __name__ == '__main__':
    unittest.main()
