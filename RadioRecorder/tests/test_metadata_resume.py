"""Current API snapshots remain available when resuming an audio capture span."""
from datetime import datetime
import unittest
from unittest.mock import patch

from cues import TrackEvent
from metadata import MetadataMonitor


NOW = datetime(2026, 10, 29, 12, 30)


def playback(title, position=10_000):
    return (f'<Info><Playback state="play" pos="{position}"/>'
            f'<CurrentTrack><TRACK TITLE="{title}"/></CurrentTrack></Info>').encode()


def history(*tracks):
    return ('<History>' + ''.join(
        f'<TRACK STARTTIME="2026-10-29 {at}" TITLE="{title}"/>'
        for at, title in tracks) + '</History>').encode()


def run_polls(monitor, polls):
    """Run actual parsing/delivery while replacing only HTTP and poll delays."""
    signal = monitor.stop_signal
    poll_index = 0

    def api_get(base, password, action, username):
        nonlocal poll_index
        current, recent = polls[poll_index]
        if action == 'playbackinfo':
            return current
        if action == 'getlastplayed':
            poll_index += 1
            return recent
        raise AssertionError(f'unexpected metadata request: {action}')

    def wait(_timeout):
        if poll_index >= len(polls):
            signal.set()

    with patch('metadata.api_get', side_effect=api_get), \
            patch('metadata.datetime', wraps=datetime) as clock, \
            patch.object(signal, 'wait', side_effect=wait):
        clock.now.return_value = NOW
        monitor._api_loop('http://radio.invalid', 'password', 'user', signal)


class MetadataResumeTests(unittest.TestCase):
    def test_current_snapshot_is_delivered_even_when_history_has_identical_event(self):
        events = []
        monitor = MetadataMonitor(events.append, lambda _: None)
        run_polls(monitor, [(playback('Already playing'),
                             history(('12:29:50', 'Already playing')))])

        self.assertEqual(events, [
            TrackEvent(datetime(2026, 10, 29, 12, 29, 50),
                       'Already playing', 'API lastplayed'),
            TrackEvent(datetime(2026, 10, 29, 12, 29, 50),
                       'Already playing', 'API playbackinfo'),
        ])

    def test_snapshot_follows_title_changes_without_repeating_for_timestamp_jitter(self):
        events = []
        monitor = MetadataMonitor(events.append, lambda _: None)
        initial = [('12:29:00', 'Previous'), ('12:29:50', 'Song A')]
        expanded = [*initial, ('12:29:20', 'Short jingle')]
        run_polls(monitor, [
            (playback('Song A'), history(*initial)),
            (playback('Song A', 9_900), history(*expanded)),
            (playback('Song B', 1_000), history(*expanded, ('12:29:59', 'Song B'))),
        ])

        snapshots = [event for event in events if event.source == 'API playbackinfo']
        self.assertEqual(snapshots, [
            TrackEvent(datetime(2026, 10, 29, 12, 29, 50), 'Song A', 'API playbackinfo'),
            TrackEvent(datetime(2026, 10, 29, 12, 29, 59), 'Song B', 'API playbackinfo'),
        ])
        self.assertEqual([event.title for event in events
                          if event.source == 'API lastplayed'],
                         ['Previous', 'Song A', 'Short jingle', 'Song B'])

    def test_new_generation_delivers_the_same_current_snapshot_again(self):
        events = []

        def on_event(event):
            events.append(event)
            if event.source == 'API playbackinfo':
                monitor.stop()

        def api_get(base, password, action, username):
            if action == 'playbackinfo':
                return playback('Still playing')
            if action == 'getlastplayed':
                return history(('12:29:50', 'Still playing'))
            raise AssertionError(f'unexpected metadata request: {action}')

        monitor = MetadataMonitor(on_event, lambda _: None)
        with patch('metadata.api_get', side_effect=api_get), \
                patch('metadata.datetime', wraps=datetime) as clock:
            clock.now.return_value = NOW
            try:
                for _ in range(2):
                    monitor.start('api', 'http://radio.invalid', 'password', 'user')
                    monitor.thread.join(timeout=1)
                    self.assertFalse(monitor.thread.is_alive(),
                                     'current playback snapshot was not delivered')
            finally:
                monitor.stop()
                if monitor.thread:
                    monitor.thread.join(timeout=1)

        self.assertEqual([event.title for event in events
                          if event.source == 'API playbackinfo'],
                         ['Still playing', 'Still playing'])


if __name__ == '__main__':
    unittest.main()
