"""Compact audio-device captions without changing the exact capture IDs.

Windows/PortAudio device identifiers include a host API and index. These must
remain unchanged in saved settings, even when the corresponding combobox only
shows a concise device name and a driver/API name.
"""
from __future__ import annotations

import re

# This is a PRESENTATION pattern. Do not change the raw PortAudio device string.
_HOST_SUFFIX = re.compile(
    r"\s*[\u2014\u2013-]\s*(?:Windows\s+)?"
    r"(?P<host>WASAPI|DirectSound|MME|WDM-KS|ASIO|DirectShow)"
    r"(?:\s*\(#\d+\))?\s*$",
    re.IGNORECASE,
)
_REPEAT_IN_BRACKETS = re.compile(r"^(.+?)\s+\(([^()]*)\)$")
_INLINE_DRIVER = re.compile(r"\s+(?:Windows\s+)?(?:WASAPI|DirectSound|MME|WDM-KS|ASIO)$", re.I)
_API_NAMES = {
    'wasapi': 'WASAPI',
    'directsound': 'DirectSound',
    'mme': 'MME',
    'wdm-ks': 'WDM-KS',
    'asio': 'ASIO',
    'directshow': 'DirectShow',
}


def _parse_label(value: str) -> tuple[str, str | None]:
    """Parse a list label, dropping only a known host API and its numeric index."""
    name = str(value or '').strip()
    if name.casefold().startswith('portaudio:'):
        name = name[len('portaudio:'):].strip()
    match = _HOST_SUFFIX.search(name)
    host = _API_NAMES[match.group('host').casefold()] if match else None
    base = name[:match.start()].strip() if match else name

    # Certain drivers expose e.g. "ESI MAYA CH1/2 (ESI MAYA CH1/2 MME)".
    # Drop the parenthesized suffix ONLY when it repeats the full real name.
    repeated = _REPEAT_IN_BRACKETS.fullmatch(base)
    if repeated:
        actual = repeated.group(1).strip()
        alias = _INLINE_DRIVER.sub('', repeated.group(2).strip()).strip()
        normal = lambda s: re.sub(r'\s+', ' ', s.strip()).casefold()
        if normal(actual) == normal(alias):
            base = actual
    return base or 'Неизвестное устройство', host


def friendly_device_name(value: str) -> str:
    """Main-window device caption: short real device name, no API/index."""
    return _parse_label(value)[0]


def _unique_rows(choices: list[tuple[str, str]], captions: list[str]) -> list[tuple[str, str]]:
    """Keep all choices selectable even if different inputs share the same name/API.

    Only genuinely indistinguishable entries get a short (2) disambiguator;
    raw enumerator numbers such as (#11) are never exposed.
    """
    used: set[str] = set()
    result: list[tuple[str, str]] = []
    for (_, identifier), base in zip(choices, captions):
        label = base
        n = 2
        while label in used:
            label = f'{base} ({n})'
            n += 1
        used.add(label)
        result.append((label, identifier))
    return result


def display_device_choices(choices: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Show only real device names in the main window; keep exact backend IDs."""
    return _unique_rows(choices, [friendly_device_name(label) for label, _ in choices])


def selection_label(current: str, formatted: list[tuple[str, str]],
                    original: list[tuple[str, str]]) -> str | None:
    """Resolve persisted exact raw IDs or legacy labels to fresh UI labels."""
    for label, identifier in formatted:
        if current == identifier:
            return label
    for label, _ in formatted:
        if current == label:
            return label
    for (label, _), (original_label, _) in zip(formatted, original):
        if current == original_label:
            return label
    return None


def settings_device_choices(choices: list[tuple[str, str]],
                            mark_directshow: bool = False) -> list[tuple[str, str]]:
    """Settings caption: '<device> — <API>', e.g. 'ESI MAYA CH1/2 — WASAPI'.

    DirectShow FFmpeg entries get an explicit label. Never change the internal
    PortAudio device strings, which are used by resolve_device() and Recorder.
    """
    captions: list[str] = []
    for original, identifier in choices:
        base, host = _parse_label(original)
        if mark_directshow and not str(identifier).startswith('portaudio:') and not host:
            host = 'DirectShow'
        captions.append(f'{base} — {host}' if host else base)
    return _unique_rows(choices, captions)
