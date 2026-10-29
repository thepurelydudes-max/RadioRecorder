"""Compute audio peak in dBFS from 16-bit signed little-endian PCM blocks."""
from array import array
import math
import sys


def peak_dbfs(pcm: bytes | memoryview) -> float:
    raw = bytes(pcm)
    if len(raw) < 2:
        return -60.0
    samples = array('h')
    samples.frombytes(raw[:len(raw) - len(raw) % 2])
    if sys.byteorder != 'little':
        samples.byteswap()
    peak = max((abs(value) for value in samples), default=0)
    if peak == 0:
        return -60.0
    return max(-60.0, min(0.0, 20 * math.log10(peak / 32768)))
