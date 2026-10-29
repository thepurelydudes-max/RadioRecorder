"""Frame-safe appending, including real files made by FFmpeg."""
import io
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import unittest

try:
    from mp3_stream import MP3AppendFile, MP3FormatError, iter_mp3_frames
except ImportError:
    MP3AppendFile = None
    MP3FormatError = ValueError
    iter_mp3_frames = None


def frame(rate=48000, channels=1, bitrate=128, payload=0):
    """A complete MPEG-1 Layer III frame with an empty bit reservoir."""
    rates = [44100, 48000, 32000]
    bitrates = [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320]
    header = (0xFFE00000 | (3 << 19) | (1 << 17) | (1 << 16)
              | (bitrates.index(bitrate) << 12) | (rates.index(rate) << 10)
              | ((3 if channels == 1 else 0) << 6))
    size = 144 * bitrate * 1000 // rate
    body = bytearray([payload] * (size - 4))
    body[:32] = bytes(32)
    return header.to_bytes(4, 'big') + body


class ShortReads(io.BytesIO):
    def read(self, size=-1):
        if size < 0 or size > 65536:
            raise AssertionError('unbounded stream read')
        return super().read(min(size, 7))


class MP3StreamTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(MP3AppendFile, 'MP3 append implementation is missing')

    def test_stream_yields_only_complete_frames_from_short_reads(self):
        data = frame() + frame(payload=1)
        frames = list(iter_mp3_frames(ShortReads(data + frame()[:47])))
        self.assertEqual([item.data for item in frames], [data[:384], data[384:]])
        self.assertEqual((frames[0].samples, frames[0].sample_rate, frames[0].channels),
                         (1152, 48000, 1))

    def test_resume_appends_audio_and_counts_encoded_time_without_pause(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'hour.mp3'
            old = frame() * 3
            path.write_bytes(old)
            with MP3AppendFile(path, 48000, 1) as output:
                self.assertAlmostEqual(output.duration, 3 * 1152 / 48000)
                output.write(next(iter_mp3_frames(io.BytesIO(frame(payload=2)))))
                self.assertAlmostEqual(output.duration, 4 * 1152 / 48000)
            self.assertEqual(path.read_bytes(), old + frame(payload=2))
            self.assertEqual(list(Path(folder).iterdir()), [path])

    def test_partial_final_frame_is_repaired_but_valid_frames_are_preserved(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'hour.mp3'
            path.write_bytes(frame() * 2 + frame()[:100])
            with MP3AppendFile(path, 48000, 1) as output:
                self.assertEqual(output.info.end, 768)
                self.assertEqual(output.info.repaired_bytes, 100)
                self.assertAlmostEqual(output.duration, .048)
            self.assertEqual(path.read_bytes(), frame() * 2)

    def test_incompatible_or_corrupt_existing_audio_is_never_modified(self):
        cases = [frame(rate=44100), frame() + frame(channels=2),
                 frame() + b'corrupt' + frame(), frame() + b'bad']
        with tempfile.TemporaryDirectory() as folder:
            for index, original in enumerate(cases):
                with self.subTest(index=index):
                    path = Path(folder) / f'{index}.mp3'
                    path.write_bytes(original)
                    with self.assertRaises(MP3FormatError):
                        MP3AppendFile(path, 48000, 1)
                    self.assertEqual(path.read_bytes(), original)

    def test_xing_counts_are_updated_and_stale_toc_and_gapless_are_disabled(self):
        header = bytearray(frame())
        offset = 4 + 17
        header[offset:offset + 16] = b'Info' + struct.pack('>III', 15, 2, 1152)
        header[offset + 16:offset + 116] = bytes(range(100))
        original = bytes(header) + frame() * 2
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'hour.mp3'
            path.write_bytes(original)
            with MP3AppendFile(path, 48000, 1) as output:
                self.assertAlmostEqual(output.duration, .048)
                output.write(next(iter_mp3_frames(io.BytesIO(frame()))))
                output.flush()
                data = path.read_bytes()
                self.assertEqual(data[offset:offset + 4], b'Xing')
                self.assertEqual(struct.unpack('>III', data[offset + 4:offset + 16]),
                                 (3, 3, 1536))
                self.assertEqual(data[384:1152], original[384:])
            with MP3AppendFile(path, 48000, 1) as output:
                self.assertAlmostEqual(output.duration, .072)

    def test_id3_tags_do_not_become_audio_or_middle_of_file_metadata(self):
        id3 = b'ID3\x04\x00\x00\x00\x00\x00\x05hello'
        old = id3 + frame() + b'TAG' + bytes(125)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'hour.mp3'
            path.write_bytes(old)
            with MP3AppendFile(path, 48000, 1) as output:
                output.write(next(iter_mp3_frames(io.BytesIO(frame()))))
            self.assertEqual(path.read_bytes(), id3 + frame() * 2)

    def test_invalid_new_frame_is_rejected_before_any_file_change(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'hour.mp3'
            original = frame()
            path.write_bytes(original)
            with MP3AppendFile(path, 48000, 1) as output:
                wrong = next(iter_mp3_frames(io.BytesIO(frame(rate=44100))))
                with self.assertRaises(MP3FormatError):
                    output.write(wrong)
            self.assertEqual(path.read_bytes(), original)

    def test_a_frame_depending_on_another_frames_reservoir_is_rejected(self):
        data = bytearray(frame())
        data[4] = 1
        item = next(iter_mp3_frames(io.BytesIO(data)))
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'hour.mp3'
            with MP3AppendFile(path, 48000, 1) as output:
                with self.assertRaisesRegex(MP3FormatError, 'reservoir'):
                    output.write(item)
            self.assertEqual(path.read_bytes(), b'')

    def test_bitrate_change_without_xing_is_rejected_to_preserve_player_duration(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'hour.mp3'
            original = frame(bitrate=64) * 3
            path.write_bytes(original)
            with MP3AppendFile(path, 48000, 1) as output:
                with self.assertRaisesRegex(MP3FormatError, 'bitrate'):
                    output.write(next(iter_mp3_frames(io.BytesIO(frame(bitrate=192)))))
            self.assertEqual(path.read_bytes(), original)

    def test_truncated_id3_does_not_get_replaced_or_treated_as_audio(self):
        original = b'ID3\x04\x00\x00\x00\x00\x01\x00short'
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'hour.mp3'
            path.write_bytes(original)
            with self.assertRaises(MP3FormatError):
                MP3AppendFile(path, 48000, 1)
            self.assertEqual(path.read_bytes(), original)

    def test_short_frame_header_can_be_repaired_after_valid_audio(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'hour.mp3'
            for length in (1, 2, 3):
                with self.subTest(length=length):
                    path.write_bytes(frame() + frame()[:length])
                    with MP3AppendFile(path, 48000, 1) as output:
                        self.assertEqual(output.info.repaired_bytes, length)
                    self.assertEqual(path.read_bytes(), frame())


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg unavailable')
class RealFFmpegAppendTests(unittest.TestCase):
    def test_legacy_ffmpeg_header_resumes_with_correct_duration_and_decodable_audio(self):
        self.assertIsNotNone(MP3AppendFile, 'MP3 append implementation is missing')
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'hour.mp3'
            base = [shutil.which('ffmpeg'), '-hide_banner', '-loglevel', 'error',
                    '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000',
                    '-t', '0.2', '-c:a', 'libmp3lame', '-b:a', '128k', '-ac', '1']
            subprocess.run(base + ['-y', str(path)], check=True, capture_output=True)
            tail = subprocess.run(base + ['-reservoir', '0', '-write_xing', '0',
                                  '-id3v2_version', '0', '-f', 'mp3', 'pipe:1'],
                                  check=True, capture_output=True).stdout
            with MP3AppendFile(path, 48000, 1) as output:
                for item in iter_mp3_frames(io.BytesIO(tail)):
                    output.write(item)
                duration = output.duration
            result = subprocess.run([shutil.which('ffprobe'), '-v', 'error',
                    '-show_entries', 'format=duration', '-of', 'csv=p=0', str(path)],
                    check=True, capture_output=True, text=True)
            self.assertAlmostEqual(float(result.stdout), duration, places=5)
            decoded = subprocess.run([shutil.which('ffmpeg'), '-hide_banner',
                    '-loglevel', 'error', '-i', str(path), '-f', 's16le', '-'],
                    check=True, capture_output=True)
            self.assertAlmostEqual(len(decoded.stdout) / 2 / 48000, duration, places=5)
            self.assertNotIn(b'invalid concatenated', decoded.stderr)


if __name__ == '__main__':
    unittest.main()
