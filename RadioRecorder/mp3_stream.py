"""Bounded-memory MPEG Layer III framing and safe current-file appending.

The encoder must emit headerless MP3 with ``-reservoir 0 -write_xing 0
-id3v2_version 0``. Frame boundaries can then become hour boundaries without
restarting input capture or making a new hour depend on its predecessor.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os
import struct
from typing import BinaryIO, Iterator


class MP3FormatError(ValueError):
    """An existing file cannot safely be continued with the selected format."""


@dataclass(frozen=True)
class MP3Frame:
    data: bytes
    samples: int
    sample_rate: int
    channels: int


@dataclass(frozen=True)
class MP3Info:
    duration: float
    sample_rate: int
    channels: int
    end: int
    frames: int = 0
    samples: int = 0
    audio_start: int = 0
    xing_offset: int | None = None
    repaired_bytes: int = 0
    bitrate: int | None = None


@dataclass(frozen=True)
class _Header:
    size: int
    samples: int
    sample_rate: int
    channels: int
    side_offset: int
    side_size: int
    version: int
    bitrate: int


_BITRATES_1 = (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 0)
_BITRATES_2 = (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, 0)


def _header(data: bytes) -> _Header:
    if len(data) != 4:
        raise MP3FormatError('Incomplete MP3 frame header')
    word = int.from_bytes(data, 'big')
    version, layer = (word >> 19) & 3, (word >> 17) & 3
    bitrate_index, rate_index = (word >> 12) & 15, (word >> 10) & 3
    if (word >> 21 != 0x7ff or version == 1 or layer != 1
            or bitrate_index in (0, 15) or rate_index == 3):
        raise MP3FormatError('Invalid or unsupported MP3 Layer III frame')
    rate = (44100, 48000, 32000)[rate_index] // (1 if version == 3 else 2 if version == 2 else 4)
    bitrate = (_BITRATES_1 if version == 3 else _BITRATES_2)[bitrate_index] * 1000
    channels = 1 if (word >> 6) & 3 == 3 else 2
    side_size = (17 if channels == 1 else 32) if version == 3 else (9 if channels == 1 else 17)
    side_offset = 4 + (0 if (word >> 16) & 1 else 2)
    size = (144 if version == 3 else 72) * bitrate // rate + ((word >> 9) & 1)
    if size < side_offset + side_size:
        raise MP3FormatError('MP3 frame is shorter than its side information')
    return _Header(size, 1152 if version == 3 else 576, rate, channels,
                   side_offset, side_size, version, bitrate)


def _read_exact(source: BinaryIO, count: int) -> bytes:
    result = bytearray()
    while len(result) < count:
        part = source.read(min(count - len(result), 65536))
        if not part:
            break
        result.extend(part)
    return bytes(result)


def iter_mp3_frames(source: BinaryIO) -> Iterator[MP3Frame]:
    """Yield complete headerless MP3 frames, dropping a killed encoder's tail.

    No search for sync words is performed: unexpected bytes signal an encoder
    or format error instead of quietly discarding recorded audio.
    """
    while True:
        prefix = _read_exact(source, 4)
        if len(prefix) < 4:
            return
        header = _header(prefix)
        body = _read_exact(source, header.size - 4)
        if len(body) != header.size - 4:
            return
        yield MP3Frame(prefix + body, header.samples, header.sample_rate, header.channels)


def _partial_header(data: bytes) -> bool:
    """Only a plausible unfinished header may be removed from an old file."""
    if not data or data[0] != 0xff:
        return False
    if len(data) > 1 and (data[1] & 0xe0 != 0xe0 or (data[1] >> 3) & 3 == 1
                          or (data[1] >> 1) & 3 != 1):
        return False
    if len(data) > 2 and ((data[2] >> 4) in (0, 15) or (data[2] >> 2) & 3 == 3):
        return False
    return True


def _scan_existing(file: BinaryIO, rate: int, channels: int) -> MP3Info:
    """Validate one selected hour in a streaming pass before any mutation."""
    file.seek(0, os.SEEK_END)
    size = file.tell()
    file.seek(0)
    prefix = file.read(10)
    start = 0
    if prefix.startswith(b'ID3'):
        if len(prefix) < 10 or prefix[3] not in (2, 3, 4) or any(x & 0x80 for x in prefix[6:10]):
            raise MP3FormatError('Invalid existing ID3 header; file preserved')
        tag_size = sum(value << (7 * (3 - index)) for index, value in enumerate(prefix[6:10]))
        start = 10 + tag_size + (10 if prefix[3] == 4 and prefix[5] & 0x10 else 0)
        if start > size:
            raise MP3FormatError('Incomplete existing ID3 tag; file preserved')
    pos, frames, samples, xing_offset, bitrate = start, 0, 0, None, None
    first = True
    while pos < size:
        file.seek(pos)
        remaining = size - pos
        prefix = file.read(min(4, remaining))
        if remaining == 128 and prefix.startswith(b'TAG'):
            break  # ID3v1 is a trailer, never leave it between audio sessions.
        if len(prefix) < 4:
            if frames and _partial_header(prefix):
                break
            raise MP3FormatError('Unrecognized data after MP3 audio; file preserved')
        try:
            header = _header(prefix)
        except MP3FormatError as exc:
            raise MP3FormatError(f'{exc} at byte {pos}; file preserved') from exc
        if header.sample_rate != rate or header.channels != channels:
            raise MP3FormatError('Existing MP3 sample rate or channels differ; file preserved')
        if header.size > remaining:
            if not frames:
                raise MP3FormatError('Existing MP3 has no complete audio frame; file preserved')
            break
        body = file.read(header.size - 4)
        data = prefix + body
        marker = header.side_offset + header.side_size
        is_xing = first and data[marker:marker + 4] in (b'Xing', b'Info')
        if is_xing:
            if marker + 25 > len(data):
                raise MP3FormatError('Incomplete Xing header; file preserved')
            xing_offset = pos + marker
        else:
            if bitrate is not None and header.bitrate != bitrate and xing_offset is None:
                raise MP3FormatError('Existing MP3 has variable bitrate without Xing; file preserved')
            bitrate = header.bitrate
            frames += 1
            samples += header.samples
        pos += header.size
        first = False
    return MP3Info(samples / rate, rate, channels, pos, frames, samples,
                   start, xing_offset, size - pos, bitrate)


class MP3AppendFile:
    """Append complete independent frames to exactly one existing hour path.

    ``info`` describes the file when opened; ``duration`` includes frames
    subsequently written. Call ``flush`` periodically (for example once per
    second), and ``close`` at hour changes and after the stdout reader drains.
    The caller owns synchronization; do not write or close from two threads.
    """

    def __init__(self, path: Path, sample_rate: int, channels: int):
        self.path = Path(path)
        if sample_rate <= 0 or channels not in (1, 2):
            raise MP3FormatError('Unsupported recording sample rate or channel count')
        if self.path.is_symlink():
            raise MP3FormatError('Recording target is a symbolic link; file preserved')
        try:
            self._file = self.path.open('r+b', buffering=65536)
        except FileNotFoundError:
            self._file = self.path.open('x+b', buffering=65536)
        try:
            self.info = _scan_existing(self._file, sample_rate, channels)
            self._samples = self.info.samples
            self._frames = self.info.frames
            self._end = self.info.end
            self._bitrate = self.info.bitrate
            self._dirty = False
            if self.info.repaired_bytes:
                self._file.truncate(self.info.end)
            self._file.seek(self.info.end)
        except BaseException:
            self._file.close()
            raise

    @property
    def duration(self) -> float:
        return self._samples / self.info.sample_rate

    @property
    def closed(self) -> bool:
        return self._file.closed

    def write(self, frame: MP3Frame) -> None:
        header = _header(frame.data[:4])
        if (len(frame.data) != header.size or header.sample_rate != self.info.sample_rate
                or header.channels != self.info.channels or frame.samples != header.samples
                or frame.sample_rate != header.sample_rate or frame.channels != header.channels):
            raise MP3FormatError('New MP3 frame differs from the existing recording format')
        side = header.side_offset
        reservoir = ((frame.data[side] << 1) | (frame.data[side + 1] >> 7)
                     if header.version == 3 else frame.data[side])
        if reservoir:
            raise MP3FormatError('MP3 encoder must disable its bit reservoir (-reservoir 0)')
        if (self.info.xing_offset is None and self._bitrate is not None
                and header.bitrate != self._bitrate):
            raise MP3FormatError('Existing MP3 bitrate differs; preserve the same bitrate within an hour')
        written = self._file.write(frame.data)
        if written != len(frame.data):
            raise OSError('Incomplete write to recording file')
        self._samples += header.samples
        self._frames += 1
        self._end += written
        self._bitrate = header.bitrate
        self._dirty = True

    def flush(self) -> None:
        if self._dirty and self.info.xing_offset is not None:
            # Preserve the dedicated Xing frame, update length, and disable
            # the old TOC and single-session LAME delay/padding extension.
            # A resumed file may contain different bitrates, hence "Xing".
            self._file.seek(self.info.xing_offset)
            self._file.write(b'Xing' + struct.pack('>III', 3, self._frames,
                             self._end - self.info.audio_start) + bytes(9))
            self._file.seek(self._end)
        self._file.flush()
        self._dirty = False

    def close(self) -> None:
        if not self._file.closed:
            try:
                self.flush()
                os.fsync(self._file.fileno())
            finally:
                self._file.close()

    def sync(self) -> None:
        """Persist audio before a CUE index can refer to it across a crash."""
        self.flush()
        os.fsync(self._file.fileno())

    def __enter__(self) -> MP3AppendFile:
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()
