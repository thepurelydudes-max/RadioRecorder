"""RadioBOSS-compatible CUE sheets. No external dependencies."""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
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


_CUE_TRACK = re.compile(
    r'(?m)^  TRACK (\d{2}) AUDIO\r?\n'
    r'    TITLE "([^"\r\n]*)"\r?\n'
    r'    INDEX 01 (\d+):(\d{2}):(\d{2})(?:\r?\n|$)'
)


def _cue_entries(text: str, path: Path) -> list[tuple[float, str]]:
    """Parse contiguous track indexes without changing or normalizing CUE text."""
    tracks = _CUE_TRACK.findall(text)
    if not text.startswith('REM DATE ') or not tracks or len(tracks) != text.count('  TRACK '):
        raise ValueError('Существующий CUE имеет неизвестный формат: ' + path.name)
    entries: list[tuple[float, str]] = []
    for i, (num, title, mm, ss, ff) in enumerate(tracks, 1):
        if int(num) != i or int(ss) >= 60 or int(ff) >= 75:
            raise ValueError('Неверная нумерация или время CUE: ' + path.name)
        offset = int(mm) * 60 + int(ss) + int(ff) / 75
        if (i == 1 and offset != 0) or (entries and offset < entries[-1][0]):
            raise ValueError('Неверный порядок индексов CUE: ' + path.name)
        entries.append((offset, title))
    return entries


def _cue_file_name(text: str) -> str | None:
    matched = re.search(r'(?m)^FILE "([^"\r\n]+)" MP3\r?$', text)
    return matched.group(1) if matched else None


def write_active_cue(path: Path, text: str, previous_text: str | None = None) -> None:
    """Create a new CUE once, otherwise ONLY append new tracks.

    A resumed hour's existing CUE is authoritative. Never seek to its start,
    truncate it, or rewrite existing TITLE/INDEX lines, even when newly
    reconstructed events disagree with previous metadata. Past or duplicate
    events are ignored, and new track numbers continue from the last on disk.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        # Exclusive creation prevents accidentally replacing a concurrently
        # created CUE. The first complete CUE is written once.
        with path.open('xb') as file:
            file.write(b'\xef\xbb\xbf' + text.encode('utf-8'))
            file.flush()
            os.fsync(file.fileno())
        return

    existing = path.read_bytes()
    if not existing.startswith(b'\xef\xbb\xbf'):
        raise ValueError('CUE без ожидаемого UTF-8 BOM, оставлен без изменений: ' + path.name)
    original = existing[3:].decode('utf-8')
    if original == text:
        return

    # Common fast path: our newly generated text is an exact extension.
    # This is also used by the earlier in-place append regression.
    if text.startswith(original):
        suffix = text[len(original):]
        if suffix:
            with path.open('ab') as file:
                file.write(suffix.encode('utf-8'))
                file.flush()
                os.fsync(file.fileno())
        return

    # A restart can regenerate earlier entries with slightly different
    # timing/initial-title metadata. Treat disk content as immutable instead
    # of falling back to seek(0)/truncate(). Only append truly newer tracks.
    recorded = _cue_entries(original, path)
    expected = _cue_entries(text, path)
    if _cue_file_name(original) != _cue_file_name(text):
        raise ValueError('Несовпадение FILE в CUE: ' + path.name)
    if len(recorded) >= 99:
        return
    last_offset, last_title = recorded[-1]
    additions: list[str] = []
    for offset, title in expected[1:]:
        if offset <= last_offset + 1e-7 or title == last_title:
            continue
        if len(recorded) + len(additions) // 3 >= 99:
            break
        number = len(recorded) + len(additions) // 3 + 1
        additions.extend([
            f'  TRACK {number:02d} AUDIO',
            f'    TITLE "{cue_escape(title)}"',
            f'    INDEX 01 {cue_time(offset)}',
        ])
        last_offset, last_title = offset, title
    if additions:
        newline = '\r\n' if b'\r\n' in existing else '\n'
        prefix = '' if original.endswith(('\r', '\n')) else newline
        extra = (prefix + newline.join(additions) + newline).encode('utf-8')
        with path.open('ab') as file:
            file.write(extra)
            file.flush()
            os.fsync(file.fileno())

def read_existing_cue(path: Path) -> tuple[str, list[tuple[float, str]], str] | None:
    """Read a previously recorded hour's CUE before adding new MP3 frames.

    Existing INDEX positions describe *recorded audio time*, not wall-clock
    time. Reconstruct those exact offsets on recovery, including offline gaps.
    Refuse malformed CUEs rather than overwriting a user's existing metadata.
    """
    if not path.exists():
        return None
    text = path.read_text(encoding='utf-8-sig')
    tracks = re.findall(
        r'(?m)^  TRACK (\d{2}) AUDIO\r?\n'
        r'    TITLE "([^"\r\n]*)"\r?\n'
        r'    INDEX 01 (\d+):(\d{2}):(\d{2})',
        text)
    if not tracks or not text.startswith('REM DATE '):
        raise ValueError('Невозможно восстановить существующий CUE: ' + path.name)
    if len(tracks) != text.count('  TRACK '):
        raise ValueError('Неполный CUE: ' + path.name)
    offsets = []
    for number, title, minutes, seconds, frames in tracks:
        if not 0 <= int(seconds) < 60 or not 0 <= int(frames) < 75:
            raise ValueError('Неверный индекс CUE: ' + path.name)
        offsets.append((int(minutes)*60 + int(seconds) + int(frames)/75, title))
    if tracks[0][0] != '01' or offsets[0][0] != 0:
        raise ValueError('CUE без начальной дорожки: ' + path.name)
    return offsets[0][1], offsets[1:], text
