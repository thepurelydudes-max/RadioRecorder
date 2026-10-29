"""Track metadata from RadioBOSS API, or ICY embedded stream titles.

Stream data is consumed/discarded; never stored or decoded as audio.
"""
from __future__ import annotations
from datetime import datetime, timedelta
import ntpath
import re
import threading
import time
from typing import Callable
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import URLError
import xml.etree.ElementTree as ET
from cues import TrackEvent


class MetadataError(Exception):
    pass


def title_from_track(track) -> str:
    """Return RadioBOSS broadcast name, or file title for jingles/voiced spots."""
    cast = (track.get('CASTTITLE') or '').strip()
    if cast:
        return cast
    artist = (track.get('ARTIST') or '').strip()
    title = (track.get('TITLE') or '').strip()
    result = ' - '.join(s for s in (artist, title) if s)
    if result:
        return result
    filename = ntpath.basename(track.get('FILENAME') or '')
    return ntpath.splitext(filename)[0]


def parse_icy_block(block: bytes) -> str | None:
    # Shoutcast/Icecast typically emit StreamTitle='artist - title';
    match = re.search(rb"StreamTitle='(.*?)';", block, re.DOTALL)
    if not match:
        return None
    raw = match.group(1).rstrip(b"\x00")
    for enc in ("utf-8", "cp1251", "latin-1"):
        try:
            title = raw.decode(enc).strip()
            return title or None
        except UnicodeDecodeError:
            pass
    return None


def parse_playbackinfo(xml: bytes, now: datetime | None = None) -> TrackEvent | None:
    root = ET.fromstring(xml)
    track = root.find("./CurrentTrack/TRACK")
    if track is None:
        return None
    state = root.find("./Playback")
    if state is not None and state.get("state", "play").lower() not in ("play", "playing"):
        return None
    title = title_from_track(track)
    if not title:
        return None
    now = now or datetime.now()
    # Playback pos is milliseconds as documented in sample outputs.
    try:
        ms = max(0, int(state.get("pos", "0"))) if state is not None else 0
        at = now - timedelta(milliseconds=ms)
    except ValueError:
        at = now
    return TrackEvent(at=at, title=title, source="API playbackinfo")


def parse_lastplayed(xml: bytes) -> list[TrackEvent]:
    root = ET.fromstring(xml)
    events = []
    for item in root.findall(".//TRACK"):
        timestamp = (item.get("STARTTIME") or "").strip()
        if not timestamp:
            continue
        try:
            at = datetime.strptime(timestamp, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        title = title_from_track(item)
        if title:
            events.append(TrackEvent(at=at, title=title, source="API lastplayed"))
    return sorted(events, key=lambda item: item.at)


def api_url(base: str, password: str, action: str, username: str = "") -> str:
    # Do not put the API password into logs or GUI.
    params = {"pass": password, "action": action}
    if username:
        params["user"] = username
    return base.rstrip("/") + "/?" + urlencode(params)


def api_get(base: str, password: str, action: str, username: str = "") -> bytes:
    with urlopen(Request(api_url(base, password, action, username),
                         headers={"User-Agent": "RadioRecorder/0.1"}),
                 timeout=5) as reply:
        payload = reply.read(2_000_000)
    if not payload.lstrip().startswith(b"<?xml") and not payload.lstrip().startswith(b"<"):
        raise MetadataError("API вернул не XML (проверь порт и пароль)")
    return payload


class MetadataMonitor:
    def __init__(self, on_event: Callable[[TrackEvent], None], on_status: Callable[[str], None]):
        self.on_event = on_event
        self.on_status = on_status
        self.stop_signal = threading.Event()
        self.thread: threading.Thread | None = None
        self.last_title: str | None = None
        self.seen: set[tuple[str, datetime, str]] = set()
        self._seen_hour: datetime | None = None
        self._guard = threading.RLock()

    def start(self, mode: str, url: str, password: str = "", username: str = "") -> None:
        with self._guard:
            previous = self.thread
            self.stop_signal.set()  # always cancel previous generation
            if mode == 'none':
                return
            signal = threading.Event()  # NEVER clear previous worker's event
            self.stop_signal = signal
            self.last_title = None
            self.seen.clear()
            self._seen_hour = None
            target = self._api_loop if mode == 'api' else self._icy_loop

            def sequential_worker():
                # ICY read can be blocking for up to its network timeout. Do
                # not establish a second HTTP connection before it exits.
                if previous and previous.is_alive() and previous is not threading.current_thread():
                    previous.join()
                if not signal.is_set():
                    target(url, password, username, signal)

            worker = threading.Thread(target=sequential_worker, name='metadata', daemon=True)
            self.thread = worker
            worker.start()

    def stop(self) -> None:
        with self._guard:
            self.stop_signal.set()
            # Do not hold the Tk UI up while ICY is blocking in a network read.
            # A future start waits for this worker before it connects anew.
            # Keep the outgoing thread handle. The next start must wait for
            # the old ICY connection to finish before it opens another one.

    def _emit(self, event: TrackEvent, signal: threading.Event) -> None:
        if signal.is_set() or self.stop_signal is not signal:
            return
        # Cache only the current hour's deduplication keys, not older CUE data.
        hour = datetime.now().replace(minute=0, second=0, microsecond=0)
        if hour != self._seen_hour:
            self.seen.clear()
            self._seen_hour = hour
        # A live playback snapshot can seed a resumed CUE even when its
        # matching history entry predates the new audio capture span.
        key = (event.title, event.at, event.source)
        if key in self.seen:
            return
        self.seen.add(key)
        if len(self.seen) > 200:
            self.seen.clear()
            self.seen.add(key)
        self.last_title = event.title
        self.on_event(event)

    def _api_loop(self, base: str, password: str, username: str, signal: threading.Event) -> None:
        last_error = ""
        last_playback_title = None
        while not signal.is_set():
            try:
                # Authoritative start times cover short jingles that a polling
                # observer could otherwise miss between checks.
                current = parse_playbackinfo(api_get(base, password, "playbackinfo", username))
                history = parse_lastplayed(api_get(base, password, "getlastplayed", username))
                now = datetime.now()
                # Only this hour matters for a live CUE. The older API
                # history is never retained or fed back to finalized files.
                this_hour = now.replace(minute=0, second=0, microsecond=0)
                recent = [item for item in history if
                          this_hour <= item.at <= now + timedelta(seconds=30)]
                for item in recent:
                    self._emit(item, signal)
                if current and current.title != last_playback_title:
                    # Send one current snapshot for each generation/title,
                    # independently of history. Position jitter on later polls
                    # must not create additional playback observations.
                    self._emit(current, signal)
                    last_playback_title = current.title
                if last_error:
                    self.on_status("API RadioBOSS восстановлен")
                    last_error = ""
                signal.wait(2)
            except (URLError, TimeoutError, OSError, ET.ParseError, ValueError, MetadataError) as exc:
                error = type(exc).__name__ + ": " + str(exc)
                if error != last_error:
                    self.on_status("Ошибка API: " + error)
                    last_error = error
                signal.wait(5)

    def _icy_loop(self, url: str, _password: str, _username: str, signal: threading.Event) -> None:
        last_error = ""
        while not signal.is_set():
            try:
                req = Request(url, headers={"Icy-MetaData": "1", "User-Agent": "RadioRecorder/0.1"})
                with urlopen(req, timeout=10) as reply:
                    header = reply.headers.get("icy-metaint")
                    if not header or not header.isdigit() or int(header) < 1:
                        raise MetadataError("Нет заголовка icy-metaint: поток не передаёт ICY-метаданные")
                    interval = int(header)
                    self.on_status("ICY: подключение установлено, аудиобайты отбрасываются")
                    last_error = ""
                    while not signal.is_set():
                        remaining = interval
                        while remaining > 0 and not signal.is_set():
                            chunk = reply.read(min(remaining, 16 * 1024))
                            if not chunk:
                                raise EOFError("HTTP-поток завершён")
                            remaining -= len(chunk)
                        if signal.is_set():
                            break
                        length = reply.read(1)
                        if not length:
                            raise EOFError("HTTP-поток завершён")
                        needed = length[0] * 16
                        payload = b""
                        while len(payload) < needed:
                            chunk = reply.read(needed - len(payload))
                            if not chunk:
                                raise EOFError("Метаданные оборвались")
                            payload += chunk
                        title = parse_icy_block(payload)
                        if title and title != self.last_title:
                            self._emit(TrackEvent(at=datetime.now(), title=title, source="ICY"), signal)
            except (URLError, TimeoutError, OSError, EOFError, MetadataError) as exc:
                error = type(exc).__name__ + ": " + str(exc)
                if error != last_error:
                    self.on_status("Ошибка ICY: " + error)
                    last_error = error
                signal.wait(5)

