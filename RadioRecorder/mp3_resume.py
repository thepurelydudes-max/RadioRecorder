"""Safe MP3 frame-level appending for resumed hourly recordings.

An MP3 stream is a succession of complete MPEG Layer III frames.  FFmpeg is
configured to emit MP3 without ID3 and Xing tags to stdout; no second encoder
or concatenation/remux pass is needed for the already-recorded audio.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os


@dataclass(frozen=True)
class FrameInfo:
    length: int
    duration: float


def frame_info(header: bytes) -> FrameInfo | None:
    if len(header) < 4 or header[0] != 0xff or header[1] & 0xe0 != 0xe0:
        return None
    version = (header[1] >> 3) & 3
    layer = (header[1] >> 1) & 3
    bitrate_index = (header[2] >> 4) & 15
    rate_index = (header[2] >> 2) & 3
    if version == 1 or layer != 1 or not 1 <= bitrate_index <= 14 or rate_index == 3:
        return None
    mpeg1 = version == 3
    rate = (44100, 48000, 32000)[rate_index]
    if version == 2:
        rate //= 2
    elif version == 0:
        rate //= 4
    kbps = ((0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 0)
            if mpeg1 else
            (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, 0))[bitrate_index]
    padding = (header[2] >> 1) & 1
    length = (144 if mpeg1 else 72) * kbps * 1000 // rate + padding
    if length < 24 or length > 4096:
        return None
    return FrameInfo(length, (1152 if mpeg1 else 576) / rate)


def id3v2_length(prefix: bytes) -> int:
    if len(prefix) < 10 or prefix[:3] != b'ID3':
        return 0
    values = prefix[6:10]
    if any(v & 0x80 for v in values):
        raise ValueError('Неверный размер ID3v2 в MP3')
    size = ((values[0] << 21) | (values[1] << 14) |
            (values[2] << 7) | values[3])
    if size > 16 * 1024 * 1024:
        raise ValueError('Слишком большой тег ID3v2')
    return 10 + size + (10 if prefix[5] & 0x10 else 0)


class FrameStream:
    """Feed arbitrary FFmpeg stdout chunks, emit only complete MPEG III frames."""

    def __init__(self):
        self.buffer = bytearray()
        self.started = False

    def feed(self, chunk: bytes):
        self.buffer.extend(chunk)
        if not self.started:
            if len(self.buffer) < 10:
                return []
            offset = id3v2_length(self.buffer[:10])
            if offset:
                if len(self.buffer) < offset:
                    return []
                del self.buffer[:offset]
            self.started = True
        frames = []
        while len(self.buffer) >= 4:
            info = frame_info(self.buffer[:4])
            if info is None:
                raise ValueError('FFmpeg передал данные, не являющиеся MP3-кадрами')
            if len(self.buffer) < info.length:
                break
            frames.append((bytes(self.buffer[:info.length]), info.duration))
            del self.buffer[:info.length]
        return frames


@dataclass(frozen=True)
class ExistingMP3:
    seconds: float
    valid_length: int
    total_length: int
    frames: int


def inspect_existing(path: Path) -> ExistingMP3:
    """Validate all frames before appending; never overwrite a valid frame."""
    if not path.exists():
        return ExistingMP3(0., 0, 0, 0)
    with path.open('rb') as fh:
        size = path.stat().st_size
        if not size:
            return ExistingMP3(0., 0, 0, 0)
        prefix = fh.read(10)
        pos = id3v2_length(prefix)
        if pos > size:
            raise ValueError('Оборванный тег MP3: ' + path.name)
        seconds, count = 0., 0
        while pos + 4 <= size:
            fh.seek(pos)
            header = fh.read(4)
            info = frame_info(header)
            if info is None:
                break
            if pos + info.length > size:
                break
            pos += info.length
            seconds += info.duration
            count += 1
        if count == 0:
            raise ValueError('Существующий MP3 не содержит целых кадров: ' + path.name)
        remain = size - pos
        if remain:
            fh.seek(pos)
            tail = fh.read(remain)
            # ID3v1 tags are file trailers and must not land in the middle of
            # a resumed file. Crash-truncated frames are preserved separately.
            if remain > 4096 and not (remain == 128 and tail[:3] == b'TAG'):
                raise ValueError('В существующем MP3 обнаружены повреждённые данные: ' + path.name)
        return ExistingMP3(seconds, pos, size, count)


class AppendingMP3:
    """One writer for one file; scan before opening and append complete frames."""

    def __init__(self, path: Path):
        self.path = path
        existing = inspect_existing(path)
        self.previous_seconds = existing.seconds
        self.seconds = existing.seconds
        self.frames = existing.frames
        self._file = None
        if path.exists():
            self._file = path.open('r+b')
            if existing.valid_length < existing.total_length:
                # Save removed trailing bytes before a crash recovery. Never
                # silently discard any bytes from a preexisting recording.
                self._file.seek(existing.valid_length)
                tail = self._file.read()
                saved = path.with_name(path.name + '.recovery-tail')
                with saved.open('xb') as backup:
                    backup.write(tail)
                    backup.flush()
                    os.fsync(backup.fileno())
                self._file.truncate(existing.valid_length)
            self._file.seek(0, os.SEEK_END)
        else:
            self._file = path.open('xb')

    def __enter__(self) -> 'AppendingMP3':
        return self

    def __exit__(self, _type, _value, _traceback) -> None:
        self.close()

    def append(self, frame: bytes, duration: float) -> None:
        info = frame_info(frame[:4])
        if info is None or info.length != len(frame):
            raise ValueError('Попытка записать неполный MP3-кадр')
        self._file.write(frame)
        self.seconds += duration
        self.frames += 1

    def flush(self) -> None:
        if self._file:
            self._file.flush()

    def close(self) -> None:
        if self._file:
            self._file.flush()
            self._refresh_existing_xing()
            os.fsync(self._file.fileno())
            self._file.close()
            self._file = None

    def _refresh_existing_xing(self) -> None:
        """Update FFmpeg's old Xing/Info duration and byte-count if present.

        Older (pre-resume) hourly MP3 files can have seekable Xing headers.
        Without this fix, players may report only the original duration after
        we append new frames. New streams use -write_xing 0.
        """
        fh = self._file
        fh.seek(0)
        start = id3v2_length(fh.read(10))
        fh.seek(start)
        header = fh.read(4)
        info = frame_info(header)
        if not info:
            return
        version = (header[1] >> 3) & 3
        mono = header[3] >> 6 == 3
        has_crc = not bool(header[1] & 1)
        side = (17 if mono else 32) if version == 3 else (9 if mono else 17)
        at = start + 4 + (2 if has_crc else 0) + side
        fh.seek(at)
        if fh.read(4) not in (b'Xing', b'Info'):
            return
        flags = int.from_bytes(fh.read(4), 'big')
        after = at + 8
        total_bytes = fh.seek(0, os.SEEK_END) - start
        if flags & 1:
            fh.seek(after)
            fh.write(min(self.frames, 0xffffffff).to_bytes(4, 'big'))
            after += 4
        if flags & 2:
            fh.seek(after)
            fh.write(min(total_bytes, 0xffffffff).to_bytes(4, 'big'))
            after += 4
        if flags & 4:
            # Update all 100 VBR seek positions too, rather than leaving the
            # old index pointing into a short, pre-resume version of the MP3.
            targets = [i * self.frames // 100 for i in range(100)]
            positions = [0] * 100
            pos, index, target_index = start, 0, 0
            while index < self.frames and target_index < 100:
                while target_index < 100 and index >= targets[target_index]:
                    positions[target_index] = min(255, (pos - start)*256//max(1, total_bytes))
                    target_index += 1
                fh.seek(pos)
                segment = frame_info(fh.read(4))
                if segment is None:
                    break
                pos += segment.length
                index += 1
            fh.seek(after)
            fh.write(bytes(positions))

