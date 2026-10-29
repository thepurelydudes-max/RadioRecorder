"""Archive scans preserve the existing displayed rows without retaining paths."""
from pathlib import Path
import os
import queue
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

import app
from archive_listing import read_archive_rows


class ArchiveListingTests(unittest.TestCase):
    def test_rows_keep_reverse_filename_order_sizes_and_cue_marker(self):
        with tempfile.TemporaryDirectory() as name:
            folder = Path(name)
            (folder / 'a.mp3').write_bytes(b'a' * 1048576)
            (folder / 'z.mp3').write_bytes(b'z' * 2097152)
            (folder / 'a.cue').write_text('CUE', encoding='utf-8')
            (folder / 'notes.txt').write_text('unrelated', encoding='utf-8')
            self.assertEqual(read_archive_rows(folder), [
                f"{'z.mp3':<45}     2.0 МБ",
                f"{'a.mp3':<45}     1.0 МБ  [CUE]",
            ])

    def test_missing_folder_is_empty(self):
        with tempfile.TemporaryDirectory() as name:
            self.assertEqual(read_archive_rows(Path(name) / 'missing'), [])

    def test_filename_case_matches_local_path_glob_rules(self):
        with tempfile.TemporaryDirectory() as name:
            folder = Path(name)
            for filename in ('lower.mp3', 'UPPER.MP3', 'Mix.Mp3', '.hidden.mp3'):
                (folder / filename).write_bytes(b'x')
            expected = [path.name for path in sorted(folder.glob('*.mp3'), reverse=True)]
            actual = [row[:row.lower().rfind('.mp3') + 4]
                      for row in read_archive_rows(folder)]
            self.assertEqual(actual, expected)

    def test_unreadable_audio_remains_listed_with_zero_size(self):
        with tempfile.TemporaryDirectory() as name:
            folder = Path(name)
            audio = folder / 'gone.mp3'
            audio.write_bytes(b'x' * 1048576)
            real_scandir = os.scandir

            class UnreadableEntry:
                name = 'gone.mp3'
                path = str(audio)

                def stat(self):
                    raise FileNotFoundError(self.path)

            class DisappearingScan:
                def __enter__(self):
                    with real_scandir(folder) as entries:
                        list(entries)
                    audio.unlink()
                    return iter([UnreadableEntry()])

                def __exit__(self, *_args):
                    pass

            with patch('archive_listing.os.scandir', return_value=DisappearingScan()):
                self.assertEqual(read_archive_rows(folder), [f"{'gone.mp3':<45}     0.0 МБ"])


class Widget:
    def __init__(self, *args, **kwargs):
        self.options = kwargs
        self.items = []
        self.exists = True

    def pack(self, **kwargs):
        pass

    def config(self, **kwargs):
        self.options.update(kwargs)

    configure = config

    def title(self, *args):
        pass

    def geometry(self, *args):
        pass

    def resizable(self, *args):
        pass

    def bind(self, *args):
        pass

    def yview(self, *args):
        pass

    def set(self, *args):
        pass

    def winfo_exists(self):
        return self.exists

    def delete(self, *args):
        self.items.clear()

    def insert(self, index, *rows):
        if threading.current_thread() is not threading.main_thread():
            raise AssertionError('worker touched Tk listbox')
        if not self.exists:
            raise AssertionError('archive populated after close')
        self.items.extend(rows)


class AsyncArchiveTests(unittest.TestCase):
    def setUp(self):
        self.widgets = {}
        self.buttons = {}
        for target in ('tk.Toplevel', 'tk.Listbox', 'ttk.Label', 'ttk.Frame',
                       'ttk.Scrollbar', 'ttk.Button'):
            def build(*args, _target=target, **kwargs):
                widget = Widget(*args, **kwargs)
                self.widgets.setdefault(_target, []).append(widget)
                if _target == 'ttk.Button':
                    self.buttons[kwargs['text']] = kwargs['command']
                return widget
            self.addCleanup(patch.stopall)
            patch('app.' + target, side_effect=build).start()
        patch('app.apply_app_icon').start()
        self.ui = object.__new__(app.RadioRecorderUI)
        self.pending_after = []
        for key, value in dict(cfg={'folder': 'first'}, events=queue.Queue(),
                               _closed=False, _closing=False, _log=Mock(),
                               winfo_exists=lambda: True,
                               after=lambda delay, callback: self.pending_after.append(callback)).items():
            object.__setattr__(self.ui, key, value)

    def pump_until(self, predicate):
        deadline = time.monotonic() + 2
        while not predicate() and time.monotonic() < deadline:
            self.ui._drain()
            callbacks, self.pending_after = self.pending_after, []
            for callback in callbacks:
                if callback != self.ui._drain:
                    callback()
            if predicate():
                break
            try:
                event = self.ui.events.get(timeout=max(.001, deadline - time.monotonic()))
            except queue.Empty:
                break
            self.ui.events.put(event)
        self.assertTrue(predicate(), 'archive loading did not finish')

    def test_scan_runs_outside_tk_and_late_result_after_close_is_ignored(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def slow_scan(folder):
            self.assertIsNot(threading.current_thread(), threading.main_thread())
            entered.set()
            release.wait(.5)
            return ['row']

        with patch('app.read_archive_rows', side_effect=slow_scan):
            before = time.monotonic()
            self.ui._archive()
            self.assertLess(time.monotonic() - before, .15)
            self.assertTrue(entered.wait(.2))
            listing = self.widgets['tk.Listbox'][0]
            window = self.widgets['tk.Toplevel'][0]
            window.exists = listing.exists = False
            release.set()
            event = self.ui.events.get(timeout=1)
            self.ui.events.put(event)
            self.ui._drain()
            self.assertEqual(listing.items, [])

    def test_refresh_during_scan_uses_latest_folder_without_overlapping_scans(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        folders = []

        def scan(folder):
            folders.append(str(folder))
            if str(folder) == 'first':
                entered.set()
                release.wait(.5)
            return [str(folder) + '.mp3']

        with patch('app.read_archive_rows', side_effect=scan):
            self.ui._archive()
            self.assertTrue(entered.wait(.2))
            self.ui.cfg['folder'] = 'second'
            self.buttons['Обновить']()
            self.assertEqual(folders, ['first'])
            release.set()
            listing = self.widgets['tk.Listbox'][0]
            self.pump_until(lambda: listing.items == ['second.mp3'])
            self.assertEqual(folders, ['first', 'second'])


if __name__ == '__main__':
    unittest.main()
