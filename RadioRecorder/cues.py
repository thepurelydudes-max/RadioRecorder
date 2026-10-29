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
