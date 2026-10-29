"""RadioBOSS-compatible CUE sheets. No external dependencies."""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
import math
import os
import re
import tempfile


@dataclass(frozen=True)
class TrackEvent:
    at: datetime
    title: str
    source: str = ""


def cue_time(seconds: float) -> str:
    """Convert seconds to CUE MM:SS:FF (75 CD frames per second)."""
    frames = max(0, round(seconds * 75))
    minutes, remainder = divmod(frames, 75 * 60)
    secs, ff = divmod(remainder, 75)
    return f"{minutes:02d}:{secs:02d}:{ff:02d}"


def cue_escape(value: str) -> str:
    # CUE quoted strings cannot have embedded unescaped double quotes.
    return re.sub(r"[\r\n\t]+", " ", value).replace('"', "'").strip()


def make_cue(mp3_name: str, start: datetime, end: datetime,
             events: list[TrackEvent], initial_title: str = "Неизвестный трек") -> str:
    """Use the format and indentation found in the supplied RadioBOSS CUE."""
    before = sorted((e for e in events if e.at <= start), key=lambda e: e.at)
    selected = before[-1].title if before else initial_title
    out = [
        f"REM DATE {start.year}",
        'REM COMMENT  "Created in RadioBOSS (Stream Archive)"',
        f'FILE "{cue_escape(mp3_name)}" MP3',
        '  TRACK 01 AUDIO',
        f'    TITLE "{cue_escape(selected)}"',
        '    INDEX 01 00:00:00',
    ]
    num = 1
    last = selected
    for ev in sorted(events, key=lambda x: x.at):
        if not start < ev.at < end:
            continue
        title = ev.title.strip()
        if not title or title == last:
            continue
        num += 1
        # Maximum 99 tracks is a traditional CUE compatibility limit.
        if num > 99:
            break
        out.extend([
            f'  TRACK {num:02d} AUDIO',
            f'    TITLE "{cue_escape(title)}"',
            f'    INDEX 01 {cue_time((ev.at - start).total_seconds())}',
        ])
        last = title
    return "\r\n".join(out) + "\r\n"


def write_cue_atomic(path: Path, text: str) -> None:
    """Produce UTF-8 BOM + CRLF like the sample; replace atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix='.cue_', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as output:
            output.write(b'\xef\xbb\xbf' + text.encode('utf-8'))
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def write_active_cue(path: Path, text: str, previous_text: str | None = None) -> None:
    """Update only the active CUE without changing its creation timestamp.

    Most metadata changes append new TRACK lines. Append just the new bytes,
    which preserves all earlier tracks if power fails during the append.
    If a title was corrected retroactively we must rewrite the same file.
    There is no background store/cache of prior hours.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    data = b'\xef\xbb\xbf' + text.encode('utf-8')
    if (previous_text and text.startswith(previous_text) and path.exists() and
            path.stat().st_size == len(b'\xef\xbb\xbf' + previous_text.encode('utf-8'))):
        extra = text[len(previous_text):].encode('utf-8')
        if extra:
            with open(path, 'ab') as file:
                file.write(extra)
                file.flush()
                os.fsync(file.fileno())
        return
    with open(path, 'r+b' if path.exists() else 'wb') as file:
        file.seek(0)
        file.write(data)
        file.truncate()
        file.flush()
        os.fsync(file.fileno())


@dataclass(frozen=True)
class CueTrack:
    """A title positioned in the MP3's audio timeline, excluding capture gaps."""
    offset: float
    title: str

    @property
    def offset_seconds(self) -> float:
        return self.offset


class CurrentCue:
    """Continue one active CUE while keeping recovered capture spans immutable.

    Only this hour's document is loaded. A fresh capture span maps wall-clock
    metadata to ``audio_base + (event.at - wall_start)``; paused intervals never
    enter that timeline. The caller supplies audio_base from complete MP3 frames.
    """

    def __init__(self, path: Path, mp3_name: str, year: int):
        if (not mp3_name or re.search(r'[<>:"/\\|?*\r\n]', mp3_name)
                or mp3_name in ('.', '..') or not mp3_name.lower().endswith('.mp3')):
            raise ValueError('Недопустимое имя FILE в CUE')
        self.path = Path(path)
        self.mp3_name = mp3_name
        self.year = year
        self.tracks: list[CueTrack] = []
        self.last_title = 'Неизвестный трек'
        self._prefix_text = ''
        self._prefix_tracks: list[CueTrack] = []
        self._saved_text: str | None = None
        self._wall_start: datetime | None = None
        self._audio_base = 0.0
        self._span_title = self.last_title
        self._events: list[TrackEvent] = []
        self._latest_at: datetime | None = None
        if self.path.exists():
            # A CUE contains at most 99 titles. Refuse malformed/oversized input
            # rather than risking the existing document during continuation.
            with self.path.open('rb') as source:
                data = source.read(1024 * 1024 + 1)
            if len(data) > 1024 * 1024:
                raise ValueError('CUE слишком велик для безопасного продолжения')
            try:
                text = data.decode('utf-8-sig')
            except UnicodeDecodeError as exc:
                raise ValueError('CUE не является корректным UTF-8') from exc
            self._prefix_tracks = self._parse_tracks(text)
            self.tracks = list(self._prefix_tracks)
            self._prefix_text = text
            self._saved_text = text
            self.last_title = self.tracks[-1].title

    def _parse_tracks(self, text: str) -> list[CueTrack]:
        known = {'REM', 'FILE', 'TRACK', 'TITLE', 'INDEX', 'PERFORMER', 'SONGWRITER',
                 'ISRC', 'FLAGS', 'PREGAP', 'POSTGAP', 'CATALOG', 'CDTEXTFILE'}
        for line in text.splitlines():
            if line.strip() and line.split()[0] not in known:
                raise ValueError('CUE содержит незавершённую или неизвестную строку')
        files = re.findall(r'^\s*FILE[ \t]+"([^"\r\n]+)"[ \t]+MP3[ \t]*\r?$',
                           text, re.MULTILINE)
        if (files != [self.mp3_name]
                or len(re.findall(r'^[ \t]*FILE\b', text, re.MULTILINE)) != 1):
            raise ValueError('FILE существующего CUE не совпадает с MP3')
        markers = list(re.finditer(r'^[ \t]*TRACK[ \t]+(\d+)[ \t]+(\w+)[ \t]*\r?$',
                                   text, re.MULTILINE))
        if (not 1 <= len(markers) <= 99
                or len(re.findall(r'^[ \t]*TRACK\b', text, re.MULTILINE)) != len(markers)):
            raise ValueError('CUE должен содержать от 1 до 99 дорожек')
        tracks = []
        for number, marker in enumerate(markers, 1):
            end = markers[number].start() if number < len(markers) else len(text)
            block = text[marker.end():end]
            titles = re.findall(r'^[ \t]*TITLE[ \t]+"([^"\r\n]*)"[ \t]*\r?$',
                                block, re.MULTILINE)
            indexes = re.findall(r'^[ \t]*INDEX[ \t]+01[ \t]+(\d+):(\d{2}):(\d{2})[ \t]*\r?$',
                                 block, re.MULTILINE)
            if (int(marker.group(1)) != number or marker.group(2) != 'AUDIO'
                    or len(titles) != 1 or len(indexes) != 1
                    or len(re.findall(r'^[ \t]*TITLE\b', block, re.MULTILINE)) != 1
                    or len(re.findall(r'^[ \t]*INDEX\b', block, re.MULTILINE)) != 1):
                raise ValueError('CUE содержит незавершённую или неверную дорожку')
            minutes, seconds, frames = map(int, indexes[0])
            offset = minutes * 60 + seconds + frames / 75
            if (seconds >= 60 or frames >= 75 or (number == 1 and offset != 0)
                    or (tracks and offset < tracks[-1].offset)):
                raise ValueError('CUE содержит неверный INDEX')
            tracks.append(CueTrack(offset, titles[0]))
        return tracks

    def resume(self, wall_start: datetime, audio_base: float, title: str) -> None:
        """Freeze the previous span and start mapping metadata at audio_base."""
        if not math.isfinite(audio_base) or audio_base < 0:
            raise ValueError('Неверное время продолжения CUE')
        if self.tracks and self.tracks[-1].offset > audio_base + 1 / 75:
            raise ValueError('INDEX существующего CUE выходит за конец MP3')
        if self._wall_start is not None:
            self._prefix_text = self.text
            self._prefix_tracks = list(self.tracks)
        self._wall_start = wall_start
        self._audio_base = audio_base
        self._span_title = cue_escape(title) or 'Неизвестный трек'
        self.last_title = self._span_title
        self._latest_at = wall_start
        self._events.clear()
        self._rebuild_tracks()

    def _rebuild_tracks(self) -> None:
        tracks = list(self._prefix_tracks)
        if not tracks:
            tracks.append(CueTrack(0, self._span_title if self._audio_base == 0
                                   else 'Неизвестный трек'))
        candidates = [CueTrack(self._audio_base, self._span_title)]
        candidates.extend(CueTrack(self._audio_base + (event.at - self._wall_start).total_seconds(),
                                   event.title) for event in self._events)
        for track in candidates:
            if track.title != tracks[-1].title:
                # Two observations at a seam describe the same audible point.
                if (len(tracks) > len(self._prefix_tracks)
                        and track.offset == tracks[-1].offset):
                    tracks[-1] = track
                elif len(tracks) < 99:
                    tracks.append(track)
                else:
                    break
        self.tracks = tracks

    def add_event(self, event: TrackEvent) -> bool:
        """Accept this capture span's metadata; return whether CUE text changed."""
        if self._wall_start is None:
            raise ValueError('Сначала начни новый интервал CUE через resume')
        title = cue_escape(event.title)
        if not title:
            return False
        before = self.text
        if event.at < self._wall_start:
            if event.source != 'API playbackinfo':
                return False
            self._span_title = title
            if self._latest_at == self._wall_start:
                self.last_title = title
        else:
            if self._latest_at is None or event.at >= self._latest_at:
                self._latest_at = event.at
                self.last_title = title
            candidate = TrackEvent(event.at, title, event.source)
            if not any(item.at == candidate.at and item.title == title for item in self._events):
                ordered = sorted(self._events + [candidate], key=lambda item: item.at)
                # Keep the first and latest observation of each title run.
                # A later same-title observation matters if delayed metadata
                # subsequently reveals an intervening song. Repeated polling
                # still uses bounded memory and never consumes track capacity.
                compact = []
                for item in ordered:
                    if (len(compact) >= 2 and item.title == compact[-1].title
                            and item.title == compact[-2].title):
                        compact[-1] = item
                    else:
                        compact.append(item)
                self._events = compact[:198]
        self._rebuild_tracks()
        return self.text != before

    @property
    def text(self) -> str:
        prefix = self._prefix_text
        if not prefix:
            prefix = (f'REM DATE {self.year}\r\n'
                      'REM COMMENT  "Created in RadioBOSS (Stream Archive)"\r\n'
                      f'FILE "{cue_escape(self.mp3_name)}" MP3\r\n')
        new_tracks = self.tracks[len(self._prefix_tracks):]
        if new_tracks and not prefix.endswith('\n'):
            prefix += '\r\n'
        out = [prefix]
        for number, track in enumerate(new_tracks, len(self._prefix_tracks) + 1):
            out.append(f'  TRACK {number:02d} AUDIO\r\n'
                       f'    TITLE "{cue_escape(track.title)}"\r\n'
                       f'    INDEX 01 {cue_time(track.offset)}\r\n')
        return ''.join(out)

    def save(self) -> None:
        """Append ordinary updates, retaining the same current-hour CUE file."""
        text = self.text
        if self.tracks and text != self._saved_text:
            write_active_cue(self.path, text, self._saved_text)
            self._saved_text = text

