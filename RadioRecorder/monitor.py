"""Live preview of the audio *recording source* through selected Windows speakers.

The monitoring path is independent of the MP3 recording path. For Windows input
capture it uses PortAudio's shared-mode input/output streams, so monitoring does
not restart FFmpeg or interrupt the MP3 encoder. For an HTTP audio input, FFmpeg
is used to decode the stream to PCM without writing audio to disk.
"""
from __future__ import annotations

import difflib
from array import array
import time
from audio_levels import peak_dbfs
import re
import subprocess
import threading
from typing import Callable

try:
    import sounddevice as sd
except (ImportError, OSError):  # optional for those running only core/unit tests
    sd = None


DEFAULT_OUTPUT = 'Default'
AUTO_INPUT = 'Auto'


def _devices():
    if sd is None:
        raise RuntimeError('Для прослушивания установи sounddevice: python -m pip install sounddevice')
    return sd.query_devices(), sd.query_hostapis()


def _device_choices(kind: str) -> list[tuple[str, str]]:
    devices, apis = _devices()
    key = 'max_output_channels' if kind == 'output' else 'max_input_channels'
    return [
        (f"{dev['name']} — {apis[dev['hostapi']]['name']} (#{i})",
         f"{dev['name']} — {apis[dev['hostapi']]['name']} (#{i})")
        for i, dev in enumerate(devices) if int(dev[key]) > 0
    ]


def list_playback_devices() -> list[tuple[str, str]]:
    return [(DEFAULT_OUTPUT, DEFAULT_OUTPUT)] + _device_choices('output')


def list_monitor_input_devices() -> list[tuple[str, str]]:
    return [(AUTO_INPUT, AUTO_INPUT)] + _device_choices('input')


def resolve_device(choice: str, kind: str, devices: list | None = None,
                   apis: list | None = None) -> int | None:
    """Return stable in-process PortAudio index, raising for disconnected devices."""
    if choice in (DEFAULT_OUTPUT, AUTO_INPUT, '', None):
        return None
    if devices is None or apis is None:
        devices, apis = _devices()
    key = 'max_output_channels' if kind == 'output' else 'max_input_channels'
    for i, dev in enumerate(devices):
        if int(dev[key]) > 0 and choice == f"{dev['name']} — {apis[dev['hostapi']]['name']} (#{i})":
            return i
    # The PortAudio index changes when USB sound cards are disconnected or
    # Windows enumerates devices in another order. Match the SAME underlying
    # name AND host API, never silently switch to a different sound card.
    match = re.fullmatch(r'(.+) — (.+) \(#[0-9]+\)', str(choice))
    if match:
        old_name, old_api = match.groups()
        candidates = [i for i, dev in enumerate(devices)
                      if int(dev[key]) > 0 and dev['name'] == old_name
                      and apis[dev['hostapi']]['name'] == old_api]
        if len(candidates) == 1:
            return candidates[0]
        if len(candidates) > 1:
            raise RuntimeError(f'Несколько одинаковых устройств {old_name} ({old_api}). '
                               'Выбери нужное в настройках заново.')
    raise RuntimeError(f'Устройство {kind} больше не доступно: {choice}. Обнови список в настройках.')


def normalize_name(value: str) -> str:
    # DShow sometimes adds (Audio), case/spacing differ from PortAudio.
    return re.sub(r'[^\w]+', '', value, flags=re.UNICODE).casefold()


def best_input_for_source(source_label: str, devices: list) -> int | None:
    """Match DirectShow's *human-readable* name against PortAudio input names.

    Never pick an unrelated device silently. The user can override the input in
    the settings when Windows uses different naming across audio backends.
    """
    if not source_label.strip():
        return None
    sought = normalize_name(source_label)
    matches = []
    for i, dev in enumerate(devices):
        if int(dev['max_input_channels']) <= 0:
            continue
        candidate = normalize_name(dev['name'])
        ratio = difflib.SequenceMatcher(None, sought, candidate).ratio()
        if candidate == sought:
            ratio = 1.0
        matches.append((ratio, i))
    if not matches:
        return None
    ratio, result = max(matches)
    return result if ratio >= .72 else None


def remix_pcm(pcm: bytes | memoryview, inputs: int, outputs: int) -> bytes:
    """Convert 16-bit little-endian stereo/mono for independent output devices."""
    pcm = bytes(pcm)
    if inputs == outputs:
        return pcm
    samples = array('h')
    samples.frombytes(pcm)
    if inputs == 1 and outputs == 2:
        mixed = array('h')
        for x in samples:
            mixed.extend((x, x))
        return mixed.tobytes()
    if inputs == 2 and outputs == 1:
        mixed = array('h', ((int(samples[i]) + int(samples[i+1])) // 2
                             for i in range(0, len(samples)-1, 2)))
        return mixed.tobytes()
    raise ValueError('Можно преобразовывать только 1 и 2 канала')


class AudioMonitor:
    """Separate start/stop live preview; does not touch the active Recorder."""
    def __init__(self, on_state: Callable[[bool, str], None],
                 on_level: Callable[[float], None] | None = None):
        self.on_state = on_state
        self.on_level = on_level or (lambda db: None)
        self._level_updated = 0.0
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._process: subprocess.Popen | None = None
        self._guard = threading.RLock()
        self._desired = False
        self._pending = None
        self._closed = False

    @property
    def running(self) -> bool:
        with self._guard:
            return self._desired

    def start(self, config: dict, ffmpeg: str, dshow_devices: list[tuple[str, str]] | None = None):
        if sd is None:
            raise RuntimeError('Не установлен sounddevice. Установи: python -m pip install sounddevice')
        if config.get('source_kind') == 'stream' and (not ffmpeg or not config.get('stream_url')):
            raise RuntimeError('Для прослушивания HTTP-потока нужен FFmpeg и URL потока')
        args = (config.copy(), ffmpeg, dshow_devices or [])
        with self._guard:
            if self._closed:
                raise RuntimeError('Прослушивание уже завершено')
            self._desired = True
            if self._thread and self._thread.is_alive():
                # A second Play while the old worker is winding down must
                # start the new monitor when that worker exits.
                if self._stop.is_set():
                    self._pending = args
                return
            self._launch(args)

    def _launch(self, args):
        """Caller holds _guard."""
        self._pending = None
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, args=args,
                                        name='audio-monitor', daemon=True)
        self._thread.start()

    def _report_level(self, pcm: bytes | memoryview) -> None:
        now = time.monotonic()
        if now - self._level_updated >= 0.09:
            self._level_updated = now
            self.on_level(peak_dbfs(pcm))

    def _run(self, config: dict, ffmpeg: str, dshow_devices: list[tuple[str, str]]):
        try:
            devices, apis = _devices()
            output_choice = config.get('monitor_device', DEFAULT_OUTPUT)
            output_id = resolve_device(output_choice, 'output', devices, apis)
            out_info = devices[output_id] if output_id is not None else sd.query_devices(kind='output')
            if config.get('source_kind') == 'stream':
                self._run_http(config, ffmpeg, output_id, out_info)
            else:
                source_label = config.get('source_label', '')
                if not source_label:
                    for label, internal in dshow_devices:
                        if internal == config.get('source') or label == config.get('source'):
                            source_label = label
                            break
                user_choice = config.get('monitor_input', AUTO_INPUT)
                if user_choice not in (AUTO_INPUT, '', None):
                    input_id = resolve_device(user_choice, 'input', devices, apis)
                elif str(config.get('source', '')).startswith('portaudio:'):
                    input_id = resolve_device(config['source'][10:], 'input', devices, apis)
                else:
                    input_id = best_input_for_source(source_label or config.get('source', ''), devices)
                if input_id is None:
                    raise RuntimeError('Не найден вход для прослушивания. Выбери его в Настройки → Воспроизведение → Вход прослушивания.')
                self._run_device(input_id, output_id, devices[input_id], out_info)
        except Exception as exc:
            if not self._stop.is_set():
                self.on_state(False, 'Ошибка прослушивания: ' + str(exc))
        finally:
            with self._guard:
                self._process = None
                pending = self._pending if self._desired and not self._closed else None
                if pending is None:
                    self._desired = False
                # Old worker no longer owns the monitor thread slot. A new
                # Play click may therefore launch immediately, even if this
                # thread hasn't actually returned yet.
                self._thread = None
                self._pending = None
                self._stop.set()
            self.on_state(False, 'Прослушивание выключено')
            if pending is not None:
                with self._guard:
                    if self._desired and not self._closed and self._thread is None:
                        self._launch(pending)

    def _run_device(self, input_id, output_id, input_info, output_info):
        """Try multiple supported configurations; old MME/DirectSound often reject low latency.

        Input and output channel counts may differ. Convert the PCM samples
        while monitoring, keeping the MP3 recorder completely separate.
        """
        max_in = min(2, int(input_info['max_input_channels']))
        max_out = min(2, int(output_info['max_output_channels']))
        if min(max_in, max_out) < 1:
            raise RuntimeError('Выбранное устройство не поддерживает передачу звука')
        rates = list(dict.fromkeys([round(input_info['default_samplerate']),
                                    round(output_info['default_samplerate']), 48000, 44100]))
        # Use high/default latency for compatibility with legacy DirectSound/MME.
        # Try stereo first and mono where the host rejects stereo capture.
        pairs = list(dict.fromkeys((i, o)
                    for i in (2, 1) if i <= max_in
                    for o in (2, 1) if o <= max_out))
        problems = []
        for rate in rates:
            for in_channels, out_channels in pairs:
                try:
                    with sd.RawInputStream(samplerate=rate, device=input_id,
                                           channels=in_channels, blocksize=1024,
                                           dtype='int16', latency='high') as source:
                        with sd.RawOutputStream(samplerate=rate, device=output_id,
                                                channels=out_channels, blocksize=1024,
                                                dtype='int16', latency='high') as dest:
                            self.on_state(True, 'Прослушивание включено (Windows-аудиовход → выход)')
                            while not self._stop.is_set():
                                data, overflow = source.read(1024)
                                self._report_level(data)
                                dest.write(remix_pcm(data, in_channels, out_channels))
                            return
                except Exception as exc:
                    if self._stop.is_set():
                        return
                    problems.append(f'{rate} Гц {in_channels}→{out_channels}: {exc}')
        raise RuntimeError('Устройство не удалось открыть для прослушивания. '
                           'Попробуй этот же вход через Windows WASAPI в настройках. '
                           + '; '.join(problems[-4:])[-450:])

    def _run_http(self, config, ffmpeg, output_id, output_info):
        channels = min(2, int(output_info['max_output_channels']))
        rates = [round(output_info['default_samplerate']), 48000, 44100]
        audio_output = None
        last_exc = None
        for rate in dict.fromkeys(rates):
            try:
                audio_output = sd.RawOutputStream(samplerate=rate, device=output_id,
                                                  channels=channels, dtype='int16', latency='high')
                audio_output.start()
                break
            except Exception as exc:
                last_exc = exc
        if audio_output is None:
            raise RuntimeError('Не удалось открыть устройство воспроизведения: ' + str(last_exc))
        try:
            command = [ffmpeg, '-hide_banner', '-nostdin', '-loglevel', 'error',
                       '-i', config['stream_url'], '-vn', '-ac', str(channels),
                       '-ar', str(rate), '-f', 's16le', '-']
            flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
            proc = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, creationflags=flags)
            with self._guard:
                self._process = proc
            self.on_state(True, 'Прослушивание HTTP-аудиопотока включено')
            try:
                block = 1024 * channels * 2
                while not self._stop.is_set():
                    data = proc.stdout.read(block)
                    if not data:
                        if not self._stop.is_set():
                            raise RuntimeError('HTTP-поток завершился или недоступен')
                        break
                    full_frames = len(data) // (channels * 2) * (channels * 2)
                    if full_frames:
                        self._report_level(data[:full_frames])
                        audio_output.write(data[:full_frames])
            finally:
                if proc.poll() is None:
                    proc.terminate()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
                if proc.stdout:
                    proc.stdout.close()
        finally:
            audio_output.stop()
            audio_output.close()

    def stop(self):
        with self._guard:
            self._desired = False
            self._pending = None
            self._stop.set()
            proc = self._process
        if proc and proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass

    def close(self):
        with self._guard:
            self._closed = True
        self.stop()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=2)
