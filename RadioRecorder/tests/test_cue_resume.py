"""CUE continuation uses recorded audio time and preserves earlier entries."""
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

import cues
from cues import TrackEvent


EXISTING = ('REM DATE 2026\r\n'
            'REM COMMENT  "Created in RadioBOSS (Stream Archive)"\r\n'
            'FILE "hour.mp3" MP3\r\n'
            '  TRACK 01 AUDIO\r\n'
            '    TITLE "Earlier song"\r\n'
            '    INDEX 01 00:00:00\r\n'
            '  TRACK 02 AUDIO\r\n'
            '    TITLE "Before pause"\r\n'
            '    INDEX 01 02:00:00\r\n')


class CurrentCueTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(hasattr(cues, 'CurrentCue'), 'current-hour CUE continuation is missing')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'hour.cue'
        self.start = datetime(2026, 10, 29, 18, 30)

    def existing(self, text=EXISTING):
        self.path.write_bytes(b'\xef\xbb\xbf' + text.encode('utf-8'))
        return cues.CurrentCue(self.path, 'hour.mp3', 2026)

    def test_new_cue_starts_at_audio_zero(self):
        cue = cues.CurrentCue(self.path, 'hour.mp3', 2026)
        cue.resume(self.start, 0, 'Current song')
        cue.save()
        self.assertIn('TITLE "Current song"\r\n    INDEX 01 00:00:00', cue.text)
        self.assertEqual([(track.offset, track.title) for track in cue.tracks], [(0, 'Current song')])
        self.assertTrue(self.path.read_bytes().startswith(b'\xef\xbb\xbf'))

    def test_resume_preserves_prefix_and_excludes_pause_from_offsets(self):
        cue = self.existing()
        inode = self.path.stat().st_ino
        cue.resume(self.start, 300, 'After pause')
        cue.add_event(TrackEvent(self.start + timedelta(seconds=90), 'Next song'))
        cue.save()
        self.assertTrue(cue.text.startswith(EXISTING))
        self.assertIn('TITLE "After pause"\r\n    INDEX 01 05:00:00', cue.text)
        self.assertIn('TITLE "Next song"\r\n    INDEX 01 06:30:00', cue.text)
        self.assertEqual(self.path.stat().st_ino, inode)

    def test_fresh_process_resumes_existing_audio_offsets(self):
        cue = self.existing()
        cue.resume(self.start, 300, 'First resume')
        cue.save()
        prefix = self.path.read_bytes()
        restarted = cues.CurrentCue(self.path, 'hour.mp3', 2026)
        restarted.resume(self.start + timedelta(minutes=20), 420, 'Second resume')
        restarted.save()
        self.assertTrue(self.path.read_bytes().startswith(prefix))
        self.assertIn('TITLE "Second resume"\r\n    INDEX 01 07:00:00', restarted.text)

    def test_history_before_resume_cannot_duplicate_recovered_entries(self):
        cue = self.existing()
        cue.resume(self.start, 300, 'Before pause')
        before = cue.text
        for seconds, title in ((1200, 'Earlier song'), (600, 'Before pause')):
            self.assertFalse(cue.add_event(TrackEvent(self.start - timedelta(seconds=seconds),
                                                    title, 'API lastplayed')))
        self.assertEqual(cue.text, before)
        self.assertEqual(len(cue.tracks), 2)

    def test_playbackinfo_seeds_resume_seam_without_replacing_old_track_one(self):
        cue = self.existing()
        cue.resume(self.start, 300, 'Before pause')
        self.assertTrue(cue.add_event(TrackEvent(self.start - timedelta(minutes=3),
                                               'Already playing', 'API playbackinfo')))
        self.assertTrue(cue.text.startswith(EXISTING))
        self.assertEqual(cue.tracks[0].title, 'Earlier song')
        self.assertIn('TITLE "Already playing"\r\n    INDEX 01 05:00:00', cue.text)
        self.assertFalse(cue.add_event(TrackEvent(self.start - timedelta(minutes=3),
                                                'Already playing', 'API playbackinfo')))

    def test_current_span_late_metadata_is_sorted_without_changing_recovered_prefix(self):
        cue = self.existing()
        cue.resume(self.start, 300, 'Before pause')
        cue.add_event(TrackEvent(self.start + timedelta(seconds=20), 'Later'))
        cue.add_event(TrackEvent(self.start + timedelta(seconds=10), 'Earlier'))
        self.assertEqual([(t.offset, t.title) for t in cue.tracks[-2:]],
                         [(310, 'Earlier'), (320, 'Later')])
        self.assertTrue(cue.text.startswith(EXISTING))
        self.assertEqual(cue.last_title, 'Later')

    def test_late_intervening_title_retains_later_same_title_observation(self):
        cue = cues.CurrentCue(self.path, 'hour.mp3', 2026)
        cue.resume(self.start, 0, 'Song A')
        self.assertFalse(cue.add_event(TrackEvent(self.start + timedelta(seconds=20), 'Song A')))
        cue.add_event(TrackEvent(self.start + timedelta(seconds=10), 'Song B'))
        self.assertEqual([(track.offset, track.title) for track in cue.tracks],
                         [(0, 'Song A'), (10, 'Song B'), (20, 'Song A')])

    def test_repeated_poll_titles_do_not_consume_99_track_capacity(self):
        cue = self.existing()
        cue.resume(self.start, 300, 'Before pause')
        for second in range(1, 200):
            self.assertFalse(cue.add_event(TrackEvent(self.start + timedelta(seconds=second),
                                                    'Before pause', 'API playbackinfo')))
        self.assertTrue(cue.add_event(TrackEvent(self.start + timedelta(seconds=200), 'New song')))
        self.assertEqual(len(cue.tracks), 3)

    def test_recovered_tracks_count_toward_99_and_last_title_keeps_updating(self):
        cue = self.existing()
        cue.resume(self.start, 300, 'Before pause')
        for second in range(1, 151):
            cue.add_event(TrackEvent(self.start + timedelta(seconds=second), f'Song {second}'))
        self.assertEqual(len(cue.tracks), 99)
        self.assertNotIn('TRACK 100', cue.text)
        self.assertEqual(cue.last_title, 'Song 150')

    def test_99th_current_span_track_can_be_corrected_at_its_existing_offset(self):
        prefix = EXISTING[:EXISTING.index('  TRACK 01')]
        for number in range(1, 99):
            minutes, seconds = divmod(number - 1, 60)
            prefix += (f'  TRACK {number:02d} AUDIO\r\n'
                       f'    TITLE "Old {number}"\r\n'
                       f'    INDEX 01 {minutes:02d}:{seconds:02d}:00\r\n')
        cue = self.existing(prefix)
        cue.resume(self.start, 100, 'Guess')
        self.assertTrue(cue.add_event(TrackEvent(self.start, 'Correction', 'API playbackinfo')))
        self.assertEqual(len(cue.tracks), 99)
        self.assertEqual(cue.tracks[-1].title, 'Correction')
        self.assertTrue(cue.text.startswith(prefix))

    def test_same_instance_second_resume_freezes_first_span(self):
        cue = self.existing()
        cue.resume(self.start, 300, 'First resume')
        cue.add_event(TrackEvent(self.start + timedelta(seconds=20), 'First span last'))
        cue.save()
        prefix = cue.text
        cue.resume(self.start + timedelta(minutes=15), 360, 'Second resume')
        cue.add_event(TrackEvent(self.start + timedelta(seconds=5), 'Old history', 'API lastplayed'))
        self.assertTrue(cue.text.startswith(prefix))
        self.assertIn('TITLE "Second resume"\r\n    INDEX 01 06:00:00', cue.text)

    def test_missing_cue_for_existing_audio_does_not_invent_historical_title(self):
        cue = cues.CurrentCue(self.path, 'hour.mp3', 2026)
        cue.resume(self.start, 300, 'Now playing')
        self.assertEqual([(track.offset, track.title) for track in cue.tracks],
                         [(0, 'Неизвестный трек'), (300, 'Now playing')])

    def test_wrong_file_reference_is_rejected_without_modifying_existing_cue(self):
        original = EXISTING.replace('hour.mp3', 'another.mp3')
        self.path.write_bytes(original.encode('utf-8'))
        with self.assertRaisesRegex(ValueError, 'FILE'):
            cues.CurrentCue(self.path, 'hour.mp3', 2026)
        self.assertEqual(self.path.read_bytes(), original.encode('utf-8'))

    def test_incomplete_trailing_track_fails_without_truncating_existing_cue(self):
        original = (EXISTING + '  TRACK 03 AUDIO\r\n    TITLE "Interrupted').encode('utf-8')
        self.path.write_bytes(original)
        with self.assertRaisesRegex(ValueError, 'CUE'):
            cues.CurrentCue(self.path, 'hour.mp3', 2026)
        self.assertEqual(self.path.read_bytes(), original)

    def test_partial_structural_lines_cannot_be_appended_as_valid_cue(self):
        for tail in ('  TRACK 03 ', '    TITLE "Interrupted', '    INDEX 01 03:',
                     'FILE "unfinished', '  TRA'):
            with self.subTest(tail=tail):
                original = (EXISTING + tail).encode('utf-8')
                self.path.write_bytes(original)
                with self.assertRaisesRegex(ValueError, 'CUE|FILE'):
                    cues.CurrentCue(self.path, 'hour.mp3', 2026)
                self.assertEqual(self.path.read_bytes(), original)

    def test_invalid_output_basename_is_rejected_before_creating_file(self):
        for name in ('../hour.mp3', 'folder\\hour.mp3', 'bad"name.mp3'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                cues.CurrentCue(self.path, name, 2026)
        self.assertFalse(self.path.exists())

    def test_unchanged_save_keeps_mtime(self):
        cue = self.existing()
        cue.resume(self.start, 300, 'Before pause')
        before = self.path.stat().st_mtime_ns
        cue.save()
        self.assertEqual(self.path.stat().st_mtime_ns, before)


if __name__ == '__main__':
    unittest.main()
