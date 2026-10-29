import unittest
from unittest.mock import MagicMock, patch
from app import RadioRecorderUI


class ExitConfirmationTests(unittest.TestCase):
    def setup_ui(self, recording, enabled):
        ui = object.__new__(RadioRecorderUI)
        # Tkinter prohibits ordinary attribute assignment before Tk is init.
        object.__setattr__(ui, 'cfg', {'ask_on_exit': enabled})
        object.__setattr__(ui, 'recorder', MagicMock(running=recording))
        object.__setattr__(ui, 'auto', MagicMock())
        object.__setattr__(ui, 'monitor', MagicMock())
        object.__setattr__(ui, 'metadata', MagicMock())
        object.__setattr__(ui, 'destroy', MagicMock())
        return ui

    @patch('app.windows_keep_awake')
    @patch('app.messagebox.askyesno', return_value=False)
    def test_stopped_cancel(self, ask, awake):
        ui = self.setup_ui(False, True)
        ui._on_close()
        ask.assert_called_once()
        self.assertIn('Закрыть RadioRecorder?', ask.call_args.args[1])
        ui.destroy.assert_not_called()
        ui.recorder.stop.assert_not_called()

    @patch('app.windows_keep_awake')
    @patch('app.messagebox.askyesno', return_value=True)
    def test_stopped_confirm(self, ask, awake):
        ui = self.setup_ui(False, True)
        ui._on_close()
        ask.assert_called_once()
        ui.destroy.assert_called_once()

    @patch('app.windows_keep_awake')
    @patch('app.messagebox.askyesno', return_value=True)
    def test_recording_message(self, ask, awake):
        ui = self.setup_ui(True, True)
        ui._on_close()
        self.assertIn('Остановить запись', ask.call_args.args[1])
        ui.recorder.stop.assert_called_once()

    @patch('app.windows_keep_awake')
    @patch('app.messagebox.askyesno')
    def test_disabled_does_not_prompt(self, ask, awake):
        ui = self.setup_ui(False, False)
        ui._on_close()
        ask.assert_not_called()
        ui.destroy.assert_called_once()


if __name__ == '__main__':
    unittest.main()
