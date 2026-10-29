"""Regression: no previously recorded CUE bytes may ever be rewritten."""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
import tempfile
import unittest

from cues import TrackEvent, make_cue, read_existing_cue, write_active_cue


class CueAppendOnlyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)
        self.path = self.folder / '29-10-2026_18-00.cue'
        self.mp3 = self.path.with_suffix('.mp3').name
        self.start = datetime(2026, 10, 29, 18, 0)

    def render(self, events=(), initial='Первая песня'):
        return make_cue(self.mp3, self.start, self.start + timedelta(hours=1),
                        list(events), initial_title=initial)

    def test_preexisting_header_and_title_cannot_be_rewritten(self):
        write_active_cue(self.path, self.render())
        before = self.path.read_bytes()
        altered = self.render(initial='Другой начальный заголовок')
        write_active_cue(self.path, altered, previous_text=self.render())
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(read_existing_cue(self.path)[0], 'Первая песня')

    def test_restore_changed_initial_title_only_appends_new_tracks(self):
        old = self.render([TrackEvent(self.start + timedelta(seconds=10), 'Вторая')])
        write_active_cue(self.path, old)
        before = self.path.read_bytes()
        newer = self.render([
            TrackEvent(self.start + timedelta(seconds=10), 'Вторая'),
            TrackEvent(self.start + timedelta(seconds=25), 'Третья'),
        ], initial='Позднее исправление')
        write_active_cue(self.path, newer, previous_text=old)
        after = self.path.read_bytes()
        self.assertTrue(after.startswith(before))
        self.assertEqual(after.count(b'  TRACK '), 3)
        self.assertIn(b'TRACK 03 AUDIO', after)
        self.assertEqual(read_existing_cue(self.path)[0], 'Первая песня')

    def test_old_title_reindexed_after_restart_is_untouched(self):
        older = self.render([TrackEvent(self.start + timedelta(seconds=10), 'Вторая')])
        write_active_cue(self.path, older)
        before = self.path.read_bytes()
        new = self.render([
            TrackEvent(self.start + timedelta(seconds=10, milliseconds=30), 'Вторая'),
            TrackEvent(self.start + timedelta(seconds=35), 'Третья')
        ])
        write_active_cue(self.path, new)
        after = self.path.read_bytes()
        self.assertTrue(after.startswith(before))
        self.assertEqual(after.count(b'  TRACK '), 3)
        self.assertEqual([x[1] for x in read_existing_cue(self.path)[1]],
                         ['Вторая', 'Третья'])

    def test_duplicate_metadata_after_restart_not_added_twice(self):
        old = self.render([TrackEvent(self.start + timedelta(seconds=10), 'Вторая')])
        write_active_cue(self.path, old)
        before = self.path.read_bytes()
        write_active_cue(self.path, old)
        write_active_cue(self.path, old, previous_text='outdated cache')
        self.assertEqual(self.path.read_bytes(), before)

    def test_delayed_older_metadata_does_not_corrupt_index_order(self):
        old = self.render([TrackEvent(self.start + timedelta(seconds=35), 'Вторая')])
        write_active_cue(self.path, old)
        before = self.path.read_bytes()
        delayed = self.render([
            TrackEvent(self.start + timedelta(seconds=12), 'Опоздавший трек'),
            TrackEvent(self.start + timedelta(seconds=35), 'Вторая')])
        write_active_cue(self.path, delayed)
        self.assertEqual(self.path.read_bytes(), before)

    def test_bad_existing_cue_is_not_replaced_or_truncated(self):
        self.path.write_bytes(b'\xef\xbb\xbfREM DATE 2026\r\nBROKEN')
        before = self.path.read_bytes()
        with self.assertRaises(ValueError):
            write_active_cue(self.path, self.render())
        self.assertEqual(self.path.read_bytes(), before)

    def test_cue_for_different_mp3_is_not_extended(self):
        wrong = make_cue('wrong.mp3', self.start,
                         self.start + timedelta(hours=1), [],
                         initial_title='Исходный')
        write_active_cue(self.path, wrong)
        before = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, 'FILE'):
            write_active_cue(self.path, self.render([
                TrackEvent(self.start + timedelta(seconds=20), 'Вторая')]))
        self.assertEqual(self.path.read_bytes(), before)

    def test_exact_addition_keeps_previous_bytes(self):
        old = self.render()
        write_active_cue(self.path, old)
        before = self.path.read_bytes()
        new = self.render([TrackEvent(self.start + timedelta(seconds=20), 'Новая')])
        write_active_cue(self.path, new, previous_text=old)
        after = self.path.read_bytes()
        self.assertTrue(after.startswith(before))
        self.assertEqual(after.count(b'  TRACK '), 2)
        self.assertIn(b'INDEX 01 00:20:00', after)


if __name__ == '__main__':
    unittest.main()
