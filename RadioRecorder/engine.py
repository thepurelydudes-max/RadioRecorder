"""RadioRecorder: Windows DirectShow or PortAudio -> FFmpeg continuous MP3 encoder.

All worker callbacks are invoked from background threads. Tk callbacks must be
marshalled to the UI thread with a queue.
"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timedelta
import json
import queue
import tempfile
import sys
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
import time
from typing import Callable
from cues import TrackEvent, make_cue, write_active_cue, read_existing_cue
from mp3_resume import FrameStream, AppendingMP3
from audio_levels import peak_dbfs

# WASAPI / PortAudio suggested input buffering (not the read block duration).
PORTAUDIO_LATENCY_SECONDS = 0.250
PORTAUDIO_BLOCK_FRAMES = 512
PORTAUDIO_PIPE_QUEUE_SECONDS = 2.0  # transient FFmpeg stalls; bounded memory
from ffmpeg_job import attach_kill_on_exit_job, release_job

APP_DIR = Path(__file__).resolve().parent
if getattr(sys, 'frozen', False):
    APP_DIR = Path(sys.executable).resolve().parent


def user_data_dir() -> Path:
    """Per-user writable settings location, independent of EXE drive/folder."""
    if sys.platform == 'win32':
        base = os.environ.get('LOCALAPPDATA') or str(Path.home() / 'AppData' / 'Local')
        return Path(base) / 'RadioRecorder'
    return Path(os.environ.get('XDG_CONFIG_HOME', str(Path.home() / '.config'))) / 'RadioRecorder'


DATA_DIR = user_data_dir()
SETTINGS_FILE = DATA_DIR / 'settings.json'
RETENTION_STATE_FILE = DATA_DIR / 'retention_state.json'


def atomic_json_write(path: Path, obj: dict) -> None:
    """Keep previous settings safe on crashes, and avoid writing to Program Files."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_name = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         prefix=path.name + '.', suffix='.tmp',
                                         delete=False) as f:
            temp_name = f.name
            json.dump(obj, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_name, path)
    finally:
        if temp_name:
            Path(temp_name).unlink(missing_ok=True)


def writable_recording_folder(folder: Path) -> None:
    """Fail early with meaningful error before showing an active REC indicator."""
    try:
        folder.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(prefix='.radiorecorder-check-', dir=folder) as _:
            pass
    except OSError as exc:
        raise RuntimeError(f'Нет прав записи в папку архива {folder}: {exc}. '
                           'Выбери папку в Документах или на другом доступном диске.') from exc


DISK_MIN_FREE_BYTES = 256 * 1024 * 1024  # preserve headroom for Windows


def ensure_disk_space(folder: Path) -> int:
    """Never start recording on an almost-full disk."""
    free = shutil.disk_usage(folder).free
    if free < DISK_MIN_FREE_BYTES:
        raise RuntimeError(f'Недостаточно места на диске для записи: {free // (1024*1024)} МБ. '
                           'Освободи хотя бы 256 МБ.')
    return free


def normalize_retention_days(value) -> int:
    """Never permit automatic deletion under the 90-day minimum."""
    try:
        days = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError('Срок хранения должен быть целым числом, минимум 90 дней') from exc
    if not 90 <= days <= 3650:
        raise ValueError('Срок хранения должен быть от 90 до 3650 дней')
    return days



DEFAULTS = {
    "source": "", "source_kind": "device", "stream_url": "http://localhost:8000/",
    "sample_rate": 48000, "bitrate": 128, "channels": 1,
    "folder": str(Path.home() / "Music" / "RadioRecorder"),
    "filename_format": "%d-%m-%y_%hh-%mm",
    "split_enabled": True, "split_minutes": 60,
    "metadata_mode": "icy", "metadata_api_url": "http://127.0.0.1:9001",
    "metadata_username": "", "metadata_password": "", "metadata_icy_url": "http://localhost:8000/",
    "cue_shift_sec": 0.0,
    "schedule": [[True for _ in range(24)] for _ in range(7)],
    "auto_schedule": False, "start_with_windows": False, "start_with_admin": False, "minimize_on_start": False,
    "ask_on_exit": True, "reboot_on_error": True,
    "keep_awake": False, "retention_enabled": True, "retention_days": 90,
    "silence_enabled": False, "silence_seconds": 10, "silence_db": -60,
    "silence_email_only_recording": True, "email_to": "",
    "smtp_server": "", "smtp_port": 587, "smtp_user": "", "smtp_password": "", "smtp_tls": True,
    "instance_name": "", "monitor_device": "Default", "monitor_input": "Auto", "source_label": "",
}


def load_config() -> dict:
    cfg = json.loads(json.dumps(DEFAULTS))
    try:
        settings_path = SETTINGS_FILE
        if not settings_path.is_file() and (APP_DIR / 'settings.json').is_file():
            # Migrate legacy portable installs only on first run. Never write
            # back into Program Files, and never discard the original file.
            settings_path = APP_DIR / 'settings.json'
        data = json.loads(settings_path.read_text(encoding='utf-8'))
        if isinstance(data, dict):
            cfg.update({key: value for key, value in data.items() if key in DEFAULTS})
    except (OSError, ValueError):
        pass
    if cfg.get('retention_days', 90) is None:
        cfg['retention_days'] = 90
    try:
        cfg['retention_days'] = max(90, min(3650, int(cfg['retention_days'])))
    except (TypeError, ValueError):
        cfg['retention_days'] = 90
    if len(cfg.get('schedule', [])) != 7 or any(len(row) != 24 for row in cfg['schedule']):
        cfg['schedule'] = json.loads(json.dumps(DEFAULTS['schedule']))
    return cfg


def save_config(config: dict) -> None:
    validated = dict(config)
    validated['retention_days'] = normalize_retention_days(validated.get('retention_days', 90))
    atomic_json_write(SETTINGS_FILE, validated)


def ffmpeg_path() -> str:
    local = APP_DIR / "ffmpeg.exe"
    if local.is_file():
        return str(local)
    return shutil.which("ffmpeg") or ""


def format_to_strftime(pattern: str) -> str:
    """Convert RadioLogger %y%m%d-%hh%mm%ss syntax to FFmpeg strftime."""
    if not pattern.strip():
        raise ValueError("Укажи формат имени файла")
    tokens = {"%hh": "%H", "%mm": "%M", "%ss": "%S",
              "%y": "%Y", "%m": "%m", "%d": "%d"}
    token_pattern = re.compile("|".join(re.escape(t) for t in tokens))
    output = token_pattern.sub(lambda m: tokens[m.group()], pattern)
    # Unknown printf tags, path traversal and Windows-invalid names are rejected.
    if re.search(r'%(?![YmdHMS])', output) or output.endswith('%'):
        raise ValueError("Неизвестная переменная в имени файла")
    if any(ch in pattern for ch in '<>:"/\\|?*') or '..' in pattern:
        raise ValueError("Недопустимые символы в имени файла")
    if '%' not in output:
        raise ValueError("Добавь в имя дату и время, чтобы файлы не перезаписывались")
    return output


def preview_name(pattern: str, at: datetime | None = None) -> str:
    return (at or datetime.now()).strftime(format_to_strftime(pattern)) + ".mp3"


def clock_boundary(at: datetime, minutes: int = 60) -> datetime:
    """Wall-clock split boundary measured from local midnight, not start time.

    For minutes=60 an input at 12:07 belongs to the file that started at
    12:00. The first recording of a session may begin mid-hour; subsequent
    files always use the scheduled wall-clock boundary.
    """
    if not 1 <= minutes <= 1440:
        raise ValueError("Неверный интервал разделения")
    midnight = at.replace(hour=0, minute=0, second=0, microsecond=0)
    elapsed = at.hour * 60 + at.minute
    return midnight + timedelta(minutes=elapsed // minutes * minutes)


def list_audio_devices(ffmpeg: str) -> list[tuple[str, str]]:
    """(label, device selector). The alternative DirectShow name is unique."""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        run = subprocess.run([ffmpeg, "-hide_banner", "-list_devices", "true",
                              "-f", "dshow", "-i", "dummy"],
                             capture_output=True, text=True, errors="replace",
                             timeout=8, creationflags=flags)
    except (OSError, subprocess.TimeoutExpired):
        return []
    is_audio = False
    devices: list[tuple[str, str]] = []
    for line in run.stderr.splitlines():
        if "DirectShow audio devices" in line:
            is_audio = True
            continue
        if "DirectShow video devices" in line:
            is_audio = False
            continue
        if not is_audio:
            continue
        alt = re.search(r'Alternative name "([^"]+)"', line)
        if alt and devices:
            label, _ = devices[-1]
            devices[-1] = (label, alt.group(1))
            continue
        item = re.search(r'\]\s+"([^"]+)"', line)
        if item:
            devices.append((item.group(1), item.group(1)))
    return devices


def choose_output_pattern(folder: Path, fmt: str, at: datetime, minutes: int,
                          split_enabled: bool = True) -> str:
    """Reserve an FFmpeg strftime name across upcoming boundaries.

    FFmpeg owns hourly segmentation and cannot be asked to select _2 for a
    single conflicting future hour while keeping its capture unbroken. We
    therefore select one collision-free suffix for the current FFmpeg session.
    Upcoming names are checked on disk once at start, not cached as a catalog.
    """
    if not split_enabled:
        stamps = [at]
        names = [at.strftime(fmt)]
    else:
        if not 1 <= minutes <= 1440:
            raise ValueError('Неверный интервал разделения')
        boundary = clock_boundary(at, minutes)
        # For ordinary hourly recording, preflight the entire 90-day
        # retention horizon (plus one day). With tiny split intervals cap at
        # 2500 probes to keep startup responsive on network/disk volumes.
        stamps = [at]
        moment = boundary + timedelta(minutes=minutes)
        end = at + timedelta(minutes=min(91 * 24 * 60, 2500 * minutes))
        while moment <= end:
            if moment > at:
                stamps.append(moment)
            moment += timedelta(minutes=minutes)
        names = [stamp.strftime(fmt) for stamp in stamps]
        if len(names) != len(set(names)):
            raise ValueError('Шаблон имени создаёт одинаковые имена для разных часов. '
                             'Добавь в формат день, месяц, год, часы и при необходимости минуты.')
    if split_enabled:
        # Hourly writer appends complete frames to existing files.
        return fmt + '.mp3'
    for n in range(1, 10001):
        suffix = '' if n == 1 else f'_{n}'
        if all(not (folder / (name + suffix + '.mp3')).exists()
               and not (folder / (name + suffix + '.cue')).exists()
               for name in names):
            return fmt + suffix + '.mp3'
    raise RuntimeError('Слишком много совпадающих имён MP3/CUE в папке записи')


@dataclass
class Segment:
    path: Path
    started: datetime


class Recorder:
    def __init__(self, on_status: Callable[[str], None],
                 on_elapsed: Callable[[float], None] | None = None,
                 on_meter: Callable[[float], None] | None = None,
                 on_silence: Callable[[], None] | None = None):
        self.on_status = on_status
        self.on_elapsed = on_elapsed or (lambda t: None)
        self.on_meter = on_meter or (lambda db: None)
        self.on_silence = on_silence or (lambda: None)
        self.last_silence_alert = 0.0
        self.lock = threading.RLock()
        self.stop_requested = threading.Event()
        self.process: subprocess.Popen | None = None
        self.watch_thread: threading.Thread | None = None
        self.cfg: dict = {}
        self.segment: Segment | None = None
        # Only the active segment is retained. Finished MP3/CUE files stay on
        # disk; neither their paths nor their metadata are cached by Recorder.
        self.events: list[TrackEvent] = []  # current CUE only, at most 98 entries
        self._pending_events: list[TrackEvent] = []  # bounded boundary race buffer
        self._active_cue_text: str | None = None
        self._filename_template: str | None = None
        self._process_spawn_at: datetime | None = None
        self.last_title = "Неизвестный трек"
        self.initial_title = "Неизвестный трек"
        self.recording_started: datetime | None = None
        self.running = False
        self.next_warning_sent = False
        self._audio_thread: threading.Thread | None = None
        self._output_thread: threading.Thread | None = None
        self._active_mp3: AppendingMP3 | None = None
        self._active_hour: datetime | None = None
        self._output_error: str | None = None
        self._job_handle = None

    def start(self, config: dict, initial_title: str = "") -> None:
        if self.running:
            return
        ffmpeg = ffmpeg_path()
        if not ffmpeg:
            raise RuntimeError("ffmpeg.exe не найден. Положи его рядом с программой")
        if not config.get("source") and config.get("source_kind") != "stream":
            raise RuntimeError("Выбери вход записи (Line In / Microphone / VAC)")
        output = Path(config["folder"]).expanduser()
        writable_recording_folder(output)
        ensure_disk_space(output)
        if not config.get('split_enabled', True) and int(config.get('split_minutes', 60)) != 60:
            self.on_status("Разделение отключено: будет один файл")
        self.cfg = config.copy()
        self.recording_started = datetime.now()
        self.segment = None
        self.events.clear()
        self._pending_events.clear()
        self._active_cue_text = None
        self._filename_template = None
        self._process_spawn_at = None
        self._active_hour = None
        self._active_mp3 = None
        self._output_error = None
        self.last_title = initial_title or 'Неизвестный трек'
        self.initial_title = self.last_title
        self.stop_requested.clear()
        self.next_warning_sent = False
        self.running = True
        try:
            self._spawn()
        except Exception:
            self.running = False
            self.stop_requested.set()
            raise
        self.watch_thread = threading.Thread(target=self._watch, name="ffmpeg-watch", daemon=True)
        self.watch_thread.start()
        self.on_status("Запись запущена")

    def _build_command(self) -> list[str]:
        c = self.cfg
        cmd = [ffmpeg_path(), '-hide_banner', '-nostdin', '-loglevel', 'info', '-n']
        if c.get('source_kind') == 'stream':
            cmd.extend(['-i', c['stream_url']])
        elif str(c.get('source', '')).startswith('portaudio:'):
            # PortAudio captures the exact input listed by Windows, feeding raw
            # PCM to FFmpeg's *existing* hourly segmenter. No network audio.
            cmd.extend(['-f', 's16le', '-ar', str(c.get('_input_rate', c['sample_rate'])),
                        '-ac', str(c.get('_capture_channels', c['channels'])), '-i', 'pipe:0'])
        else:
            cmd.extend(['-thread_queue_size', '1024', '-f', 'dshow',
                        '-audio_buffer_size', '250', '-i', 'audio=' + c['source']])
        cmd += ['-map', '0:a:0', '-vn',
                # Output stays fixed even if PortAudio falls back to mono
                # capture. Changing MP3 channel layouts mid-file is unsafe.
                '-ac', str(c['channels']), '-ar', str(c['sample_rate'])]
        filters = ['ebur128=peak=true']
        if c.get('silence_enabled'):
            filters.insert(0, 'silencedetect=noise=' + str(int(c['silence_db'])) +
                           'dB:d=' + str(int(c['silence_seconds'])))
        cmd += ['-af', ','.join(filters), '-codec:a', 'libmp3lame',
                '-b:a', str(c['bitrate']) + 'k']
        fmt = format_to_strftime(c['filename_format'])
        now = datetime.now()
        mins = int(c.get('split_minutes', 60)) if c.get('split_enabled', True) else 60
        pat = choose_output_pattern(Path(c['folder']), fmt, now, mins,
                                    bool(c.get('split_enabled', True)))
        self._filename_template = None if c.get('split_enabled', True) else pat
        self._process_spawn_at = now
        if c.get('split_enabled', True):
            # Continuous encoder; Python routes complete MPEG frames to
            # precisely one MP3 per clock interval, even across restarts.
            cmd += ['-write_xing', '0', '-id3v2_version', '0', '-f', 'mp3', 'pipe:1']
        else:
            cmd += ['-f', 'mp3', str(Path(c['folder']) / datetime.now().strftime(pat))]
        return cmd

    def _prepare_portaudio(self) -> None:
        """Probe Windows input before creating FFmpeg. Mono and native-rate fallback.

        Audio device APIs may report stereo support, but reject actually opening
        a stereo stream. Probe real shared-mode opening to catch these cases.
        """
        try:
            import sounddevice as sd
            from monitor import resolve_device
            devices, apis = sd.query_devices(), sd.query_hostapis()
            device_id = resolve_device(self.cfg['source'].split(':', 1)[1], 'input', devices, apis)
            info = devices[device_id]
            max_channels = int(info['max_input_channels'])
            requested = int(self.cfg['channels'])
            rate = int(self.cfg['sample_rate'])
            native = int(round(info['default_samplerate']))
            if max_channels < 1:
                raise RuntimeError('У аудиоустройства нет входных каналов')
            rates = list(dict.fromkeys([rate, native, 48000, 44100]))
            channel_choices = [ch for ch in (requested, 1) if ch <= max_channels]
            errors = []
            for channels in channel_choices:
                for input_rate in rates:
                    try:
                        with sd.RawInputStream(device=device_id, samplerate=input_rate,
                                               channels=channels, dtype='int16',
                                               blocksize=PORTAUDIO_BLOCK_FRAMES,
                                               latency=PORTAUDIO_LATENCY_SECONDS):
                            pass
                        self.cfg['_capture_channels'] = channels
                        self.cfg['_input_rate'] = input_rate
                        if channels != requested:
                            self.on_status(f'Вход не поддерживает {requested} канала: автоматически выбрано моно (1 канал)')
                        if input_rate != rate:
                            self.on_status(f'Вход открыт на {input_rate} Гц, FFmpeg преобразует в {rate} Гц')
                        return
                    except Exception as exc:
                        errors.append(f'{channels} канал., {input_rate} Гц: {exc}')
            raise RuntimeError('Не удалось открыть аудиовход Windows. '
                               'Проверь доступ к микрофону и занятость устройства. ' + '; '.join(errors[-3:]))
        except (ImportError, OSError) as exc:
            raise RuntimeError('Не удалось инициализировать аудиовход Windows: ' + str(exc)) from exc

    def _spawn(self) -> None:
        if (self.cfg.get('source_kind') == 'device' and
                str(self.cfg.get('source', '')).startswith('portaudio:')):
            self._prepare_portaudio()
        flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
        use_portaudio = (self.cfg.get('source_kind') == 'device' and
                         str(self.cfg.get('source', '')).startswith('portaudio:'))
        command = self._build_command()  # validation before launching FFmpeg
        if self._output_thread and self._output_thread.is_alive():
            self._output_thread.join(timeout=5)
            if self._output_thread.is_alive():
                raise RuntimeError('Предыдущий MP3-писатель ещё завершает запись')
        if self._job_handle is not None:
            release_job(self._job_handle)  # previous encoder already stopped
            self._job_handle = None
        self.process = subprocess.Popen(command,
                                        stdin=subprocess.PIPE if use_portaudio else subprocess.DEVNULL,
                                        stdout=(subprocess.PIPE if self.cfg.get('split_enabled', True)
                                                else subprocess.DEVNULL),
                                        stderr=subprocess.PIPE,
                                        text=True, errors='replace', bufsize=1,
                                        creationflags=flags)
        try:
            self._job_handle = attach_kill_on_exit_job(self.process)
        except Exception as exc:
            self.on_status('Windows: защита от зависшего FFmpeg недоступна: ' + str(exc))
        threading.Thread(target=self._read_stderr, args=(self.process,),
                         name="ffmpeg-log", daemon=True).start()
        if self.cfg.get('split_enabled', True):
            self._output_thread = threading.Thread(
                target=self._encoded_output_worker, args=(self.process,),
                name="mp3-hourly-writer", daemon=True)
            self._output_thread.start()
        if use_portaudio:
            proc = self.process
            self._audio_thread = threading.Thread(target=self._capture_portaudio,
                                                  args=(proc,), name='portaudio-capture', daemon=True)
            self._audio_thread.start()

    def _encoded_output_worker(self, proc: subprocess.Popen) -> None:
        """Drain FFmpeg stdout and atomically append complete MP3 frames."""
        scanner = FrameStream()
        try:
            if proc.stdout is None:
                raise RuntimeError('FFmpeg не открыл выходной MP3-поток')
            source = getattr(proc.stdout, 'buffer', proc.stdout)
            reader = getattr(source, 'read1', source.read)
            while True:
                data = reader(4096)
                if not data:
                    break
                for frame, duration in scanner.feed(data):
                    self._append_encoded_frame(frame, duration)
        except (OSError, ValueError, RuntimeError) as exc:
            self._output_error = str(exc)
            self.on_status('Безопасная дозапись остановлена: ' + str(exc))
            self.stop_requested.set()
            if proc.poll() is None:
                proc.terminate()
        finally:
            with self.lock:
                if self._active_mp3 is not None:
                    try:
                        self._active_mp3.close()
                    except OSError as exc:
                        self.on_status('Ошибка закрытия MP3: ' + str(exc))
                    self._active_mp3 = None

    def _append_encoded_frame(self, frame: bytes, duration: float) -> None:
        now = datetime.now()
        minutes = int(self.cfg.get('split_minutes', 60))
        hour = clock_boundary(now, minutes)
        with self.lock:
            if self._active_mp3 is None or self._active_hour != hour:
                if self._active_mp3 is not None:
                    self._update_current_cue()
                    self._active_mp3.close()
                    self._active_mp3 = None
                fmt = format_to_strftime(self.cfg['filename_format'])
                path = Path(self.cfg['folder']) / (hour.strftime(fmt) + '.mp3')
                if not path.exists() and path.with_suffix('.cue').exists():
                    raise RuntimeError('CUE существует без MP3, запись не перезаписана: ' + path.name)
                # Check both files before opening an existing one for append.
                saved = read_existing_cue(path.with_suffix('.cue'))
                writer = AppendingMP3(path)
                if saved is not None and any(
                        offset > writer.previous_seconds + 2 for offset, _ in saved[1]):
                    writer.close()
                    raise ValueError('Индексы CUE выходят за пределы MP3: ' + path.name)
                # Audio positions are contiguous, even when wall-clock capture
                # had an outage. Shift the origin when resuming a partial hour.
                origin = now - timedelta(seconds=writer.previous_seconds)
                initial_title = self.last_title
                restored: list[TrackEvent] = []
                previous_cue = None
                if saved is not None:
                    initial_title, offsets, previous_cue = saved
                    restored = [TrackEvent(origin + timedelta(seconds=offset),
                                           title, 'resume') for offset, title in offsets][:98]
                elif writer.previous_seconds:
                    self.on_status('MP3 восстановлен без старого CUE: ' + path.name)
                pending = list(self._pending_events)
                self._pending_events.clear()
                self._active_mp3 = writer
                self._active_hour = hour
                self.segment = Segment(path, origin)
                self.events = restored
                self.initial_title = initial_title
                self._active_cue_text = previous_cue
                self.last_title = restored[-1].title if restored else initial_title
                self.on_status(('Продолжена запись: ' if writer.previous_seconds else 'Новый файл: ') + path.name)
                if previous_cue is None:
                    self._update_current_cue()
                for event in pending:
                    if event.at < origin:
                        event = TrackEvent(origin, event.title, event.source)
                    self._add_encoded_event(event)
            if self._active_mp3 is not None:
                self._active_mp3.append(frame, duration)
                # Buffering stays bounded to FFmpeg's pipe; flush so size
                # watchdog and archive listing see live recording progress.
                self._active_mp3.flush()

    def _add_encoded_event(self, event: TrackEvent) -> None:
        if not self.segment or event.at < self.segment.started:
            return
        if event.at >= self._segment_end():
            self._pending_events.append(event)
            return
        if len(self.events) < 98 and not any(
                e.at == event.at and e.title == event.title for e in self.events):
            self.events.append(event)
            self.events.sort(key=lambda e: e.at)
            self.last_title = event.title
            self._update_current_cue()

    def _pump_portaudio(self, proc: subprocess.Popen,
                        blocks: queue.Queue[bytes],
                        capture_done: threading.Event) -> None:
        """Never let a slow FFmpeg stdin stall the time-critical capture loop.

        The queue is deliberately bounded. After a prolonged stall we log an
        error and restart FFmpeg instead of silently dropping PCM samples.
        """
        try:
            if proc.stdin is None:
                raise RuntimeError('Нет входного канала FFmpeg')
            pipe = getattr(proc.stdin, 'buffer', proc.stdin)
            while (not capture_done.is_set() or not blocks.empty()):
                if proc.poll() is not None:
                    return
                try:
                    pcm = blocks.get(timeout=0.05)
                except queue.Empty:
                    continue
                pipe.write(pcm)
                pipe.flush()
        except (OSError, RuntimeError, ValueError) as exc:
            if not self.stop_requested.is_set() and proc.poll() is None:
                self.on_status('Ошибка передачи PCM в FFmpeg: ' + str(exc))
                proc.terminate()
        finally:
            if proc.stdin is not None:
                try:
                    proc.stdin.close()
                except (OSError, ValueError):
                    pass

    def _capture_portaudio(self, proc: subprocess.Popen) -> None:
        """Capture at 250 ms target latency; buffer short FFmpeg stalls safely."""
        capture_done = threading.Event()
        writer: threading.Thread | None = None
        try:
            import sounddevice as sd
            from monitor import resolve_device
            devices, apis = sd.query_devices(), sd.query_hostapis()
            choice = self.cfg['source'].split(':', 1)[1]
            device_id = resolve_device(choice, 'input', devices, apis)
            channels = int(self.cfg.get('_capture_channels', self.cfg['channels']))
            rate = int(self.cfg.get('_input_rate', self.cfg['sample_rate']))
            if proc.stdin is None:
                raise RuntimeError('Не открыт канал PCM для FFmpeg')
            # 2 seconds of PCM at most, not a fixed delay. Read remains
            # unblocked while the separate writer waits on a slow FFmpeg.
            capacity = max(16, int(rate * PORTAUDIO_PIPE_QUEUE_SECONDS /
                                    PORTAUDIO_BLOCK_FRAMES + 0.999))
            blocks: queue.Queue[bytes] = queue.Queue(maxsize=capacity)
            writer = threading.Thread(target=self._pump_portaudio,
                                      args=(proc, blocks, capture_done),
                                      name='portaudio-ffmpeg-pipe', daemon=True)
            writer.start()
            with sd.RawInputStream(samplerate=rate, device=device_id,
                                   channels=channels, dtype='int16',
                                   blocksize=PORTAUDIO_BLOCK_FRAMES,
                                   latency=PORTAUDIO_LATENCY_SECONDS) as stream:
                self.on_status('Аудиовход: буфер 250 мс, очередь PCM до 2 сек')
                last_meter = 0.0
                last_overflow_notice = 0.0
                overflow_at: list[float] = []
                near_full_reported = False
                while not self.stop_requested.is_set() and proc.poll() is None:
                    data, overflow = stream.read(PORTAUDIO_BLOCK_FRAMES)
                    now = time.monotonic()
                    if overflow:
                        # Data has ALREADY been lost by the host API. Increasing
                        # the queue cannot restore it, so report each episode.
                        overflow_at = [t for t in overflow_at if now - t <= 10]
                        overflow_at.append(now)
                        if now - last_overflow_notice >= 3:
                            self.on_status('Переполнение аудиовхода PortAudio: потеряны сэмплы')
                            last_overflow_notice = now
                        if len(overflow_at) >= 3:
                            raise RuntimeError('3 переполнения аудиовхода за 10 секунд; '
                                               'перезапуск захвата вместо записи треска')
                    if now - last_meter >= 0.12:
                        last_meter = now
                        self.on_meter(peak_dbfs(data))
                    try:
                        blocks.put_nowait(bytes(data))
                    except queue.Full as exc:
                        raise RuntimeError('FFmpeg не успевает принимать PCM: очередь '
                                           'заполнена за 2 секунды, нужен перезапуск') from exc
                    if blocks.qsize() >= max(8, 3 * capacity // 4):
                        if not near_full_reported:
                            self.on_status('Предупреждение: очередь PCM заполнена более чем на 75%')
                            near_full_reported = True
                    elif blocks.qsize() < capacity // 4:
                        near_full_reported = False
        except (OSError, RuntimeError, ValueError, ImportError) as exc:
            if not self.stop_requested.is_set():
                self.on_status('Ошибка захвата Windows/PortAudio: ' + str(exc))
                if proc.poll() is None:
                    proc.terminate()
        finally:
            capture_done.set()
            if writer and writer is not threading.current_thread():
                writer.join(timeout=2)
                if writer.is_alive() and proc.poll() is None:
                    # A blocked pipe must not hang the recorder on stop.
                    proc.terminate()

    def _read_stderr(self, process: subprocess.Popen) -> None:
        if process.stderr is None:
            return
        recent_lines: list[str] = []
        for line in process.stderr:
            line = line.strip()
            if not line:
                continue
            recent_lines.append(line)
            if len(recent_lines) > 8:
                recent_lines.pop(0)
            # EBU loudness is approximate indication, not a calibrated dBFS VU.
            meter = re.search(r'FTPK:\s*(-?\d+(?:\.\d+)?)', line)
            if meter:
                try:
                    self.on_meter(float(meter.group(1)))
                except ValueError:
                    pass
            if 'silence_start:' in line:
                self.on_status('Обнаружена тишина: ' + line.split('silence_start:')[-1].strip())
                if time.monotonic() - self.last_silence_alert >= 60:
                    self.last_silence_alert = time.monotonic()
                    self.on_silence()
            if any(s in line.lower() for s in ('error opening input', 'error opening output',
                                                 'could not find', 'invalid argument',
                                                 'cannot open', 'no such device',
                                                 'permission denied', 'device busy')):
                self.on_status('FFmpeg: ' + line[-220:])
        if process.poll() and not self.stop_requested.is_set():
            self.on_status('FFmpeg завершился: ' + ' | '.join(recent_lines[-3:])[-380:])

    def _watch(self) -> None:
        last_bytes = -1
        last_growth = time.monotonic()
        next_disk_check = time.monotonic()
        while not self.stop_requested.is_set():
            if time.monotonic() >= next_disk_check:
                next_disk_check = time.monotonic() + 30
                try:
                    ensure_disk_space(Path(self.cfg['folder']))
                except RuntimeError as exc:
                    self.on_status('Остановка записи: ' + str(exc))
                    self.stop_requested.set()
                    proc = self.process
                    if proc and proc.poll() is None:
                        proc.terminate()
                    break
                except OSError as exc:
                    self.on_status('Проверка диска: ' + str(exc))
            proc = self.process
            if proc is not None and proc.poll() is not None:
                # FFmpeg may have flushed a last segment during shutdown.
                self._discover_segments()
                if self._output_thread and self._output_thread.is_alive():
                    self._output_thread.join(timeout=5)
                    if self._output_thread.is_alive():
                        self.on_status('MP3-писатель ещё занят, повторный FFmpeg не запускаем')
                        self.stop_requested.set()
                        break
                if self.cfg.get('reboot_on_error'):
                    self.on_status('Ошибка записи; повторное подключение через 5 секунд')
                    if self.stop_requested.wait(5):
                        break
                    try:
                        self._spawn()
                        self.on_status('Рекордер перезапущен')
                    except Exception as exc:
                        self.on_status('Ошибка перезапуска: ' + str(exc))
                    continue
                self.on_status('Запись остановлена из-за ошибки FFmpeg')
                break
            self._discover_segments()
            # MP3 output should grow even when source sound is silent.
            try:
                current_file = self.segment.path if self.segment else None
                size = current_file.stat().st_size if current_file else 0
            except OSError:
                size = 0
            if size != last_bytes:
                last_bytes = size
                last_growth = time.monotonic()
            elif time.monotonic() - last_growth > 35:
                self.on_status('Нет новых аудиоданных 35 сек: перезапуск кодировщика')
                if proc is not None and proc.poll() is None:
                    proc.terminate()
                last_growth = time.monotonic()
            if self.recording_started:
                self.on_elapsed((datetime.now() - self.recording_started).total_seconds())
            self.cleanup_if_due()
            self.stop_requested.wait(0.25)
        self._discover_segments()
        if self._output_thread and self._output_thread.is_alive():
            self._output_thread.join(timeout=5)
        with self.lock:
            self._update_current_cue()
            self._forget_active_segment()
        self.running = False

    def _segment_end(self) -> datetime:
        """Boundary of the active MP3; later metadata belongs to the NEXT CUE."""
        if not self.segment or not self.cfg.get('split_enabled', True):
            return datetime.max
        minutes = int(self.cfg.get('split_minutes', 60))
        if self._active_hour is not None:
            return self._active_hour + timedelta(minutes=minutes)
        return clock_boundary(self.segment.started, minutes) + timedelta(minutes=minutes)

    def _forget_active_segment(self) -> None:
        """Release segment/CUE metadata as soon as capture finishes."""
        self.segment = None
        self.events.clear()
        self._pending_events.clear()
        self._active_cue_text = None
        self._filename_template = None
        self._process_spawn_at = None
        self._active_hour = None

    def _discover_segments(self) -> None:
        """Probe only expected FFmpeg output names, never cache the archive.

        The segment muxer generates filenames using strftime and local clock
        intervals. The first file may start mid-hour, so its initial timestamp
        is checked as well. No glob of thousands of archived MP3s is needed.
        """
        if self.cfg.get('split_enabled', True) and self._filename_template is None:
            # The MP3 writer already knows the active segment exactly.
            return
        with self.lock:
            pattern = self._filename_template
            if not pattern or not self._process_spawn_at:
                return
            now = datetime.now()
            folder = Path(self.cfg['folder'])
            moments = [self._process_spawn_at]
            if self.cfg.get('split_enabled', True):
                minutes = int(self.cfg.get('split_minutes', 60))
                boundary = clock_boundary(now, minutes)
                moments += [boundary, boundary - timedelta(minutes=minutes)]
                # The first output or a reconnect may appear after the minute
                # recorded just before Popen; check 'now' only in that case.
                if self.segment is None or self._process_spawn_at > self.segment.started:
                    moments.append(now)
            elif self.segment is None:
                moments.append(now)
            # Prefer the most recent existing file and avoid duplicate paths.
            choices: dict[Path, datetime] = {}
            for stamp in moments:
                path = folder / stamp.strftime(pattern)
                if path not in choices:
                    choices[path] = stamp
            selected = None
            for path, stamp in sorted(choices.items(), key=lambda entry: entry[1], reverse=True):
                if self.segment is not None and path == self.segment.path:
                    continue
                try:
                    if path.is_file() and path.stat().st_size > 0:
                        selected = path, stamp
                        break
                except OSError:
                    continue
            if selected is None:
                return
            path, stamp = selected
            if self.segment is not None:
                # Old files cannot become the active recording again.
                if stamp <= self.segment.started:
                    return
                boundary = clock_boundary(now, int(self.cfg.get('split_minutes', 60)))
                spawned_path = folder / self._process_spawn_at.strftime(pattern)
                if path == spawned_path and self._process_spawn_at > self.segment.started:
                    start = self._process_spawn_at  # FFmpeg restarted mid-hour
                elif boundary > self.segment.started:
                    start = boundary
                else:
                    return
                # Old CUE has already been saved on each accepted event.
                # Never touch it once a new MP3 has appeared.
                before = [e for e in self.events if e.at < start]
                initial_title = before[-1].title if before else self.initial_title
                carry = [e for e in self.events if e.at >= start]
                carry += [e for e in self._pending_events if e.at >= start]
            else:
                start = self._process_spawn_at
                initial_title = self.initial_title
                carry = [e for e in self._pending_events if e.at >= start]
            self.segment = Segment(path, start)
            self.initial_title = initial_title
            self.events = sorted(carry, key=lambda e: e.at)[:98]
            self._pending_events.clear()
            self._active_cue_text = None
            self.on_status('Новый файл: ' + path.name)
            self._update_current_cue()

    def add_event(self, event: TrackEvent) -> None:
        with self.lock:
            if not self.running:
                return
            try:
                sec = float(self.cfg.get('cue_shift_sec', 0))
            except (ValueError, TypeError):
                sec = 0
            shifted = TrackEvent(event.at + timedelta(seconds=sec), event.title, event.source)
            if not shifted.title.strip():
                return
            # The hour may have just changed; find the new MP3 first.
            self._discover_segments()
            if self.segment is None:
                self.last_title = shifted.title
                self.initial_title = shifted.title
                return
            if shifted.at < self.segment.started:
                # A track started before this recording may still be playing.
                # Playbackinfo identifies it without touching any older CUE.
                if (event.source == 'API playbackinfo'
                        and shifted.title != self.initial_title):
                    self.initial_title = shifted.title
                    self.last_title = shifted.title
                    self._update_current_cue()
                return
            if shifted.at >= self._segment_end():
                # Audio muxer may be milliseconds behind ICY/API event. Hold
                # just a few new-hour events until its new output is visible.
                if not any(e.at == shifted.at and e.title == shifted.title for e in self._pending_events):
                    self._pending_events.append(shifted)
                    self._pending_events = sorted(self._pending_events, key=lambda e: e.at)[-98:]
                self.last_title = shifted.title
                return
            if any(e.at == shifted.at and e.title == shifted.title for e in self.events):
                return
            if len(self.events) >= 98:
                # CUE format allows 99 tracks including the initial one.
                self.last_title = shifted.title
                return
            self.events.append(shifted)
            self.events.sort(key=lambda e: e.at)
            self.last_title = self.events[-1].title
            self._update_current_cue()

    def _update_current_cue(self) -> None:
        """Only edit the open hour's CUE, preserving its Windows creation time."""
        if self.segment is None:
            return
        cue_text = make_cue(self.segment.path.name, self.segment.started,
                            self._segment_end(), self.events,
                            initial_title=self.initial_title)
        if cue_text == self._active_cue_text:
            return
        try:
            write_active_cue(self.segment.path.with_suffix('.cue'), cue_text, self._active_cue_text)
            self._active_cue_text = cue_text
        except (OSError, ValueError) as exc:
            self.on_status('Ошибка CUE (старое содержимое сохранено): ' + str(exc))

    def stop(self) -> None:
        self.stop_requested.set()
        proc = self.process
        if proc is not None:
            try:
                if proc.poll() is None:
                    proc.terminate()
                proc.wait(timeout=4)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    if proc.poll() is None:
                        proc.kill()
                    proc.wait(timeout=2)
                except (OSError, subprocess.TimeoutExpired) as exc:
                    self.on_status('FFmpeg не завершился штатно: ' + str(exc))
        if self.watch_thread and self.watch_thread is not threading.current_thread():
            self.watch_thread.join(timeout=3)
        if self._output_thread and self._output_thread is not threading.current_thread():
            self._output_thread.join(timeout=5)
        self.process = None
        if self._job_handle is not None:
            release_job(self._job_handle)
            self._job_handle = None
        self.running = False
        with self.lock:
            self._update_current_cue()
            self._forget_active_segment()
        self.on_status('Запись остановлена')

    def cleanup_if_due(self, now: datetime | None = None) -> bool:
        """Do cleanup at most once per local calendar day while recording.

        The daily check runs when recording starts (if not yet done today),
        and again after each subsequent midnight while recording remains on.
        An atomic state file prevents another cleanup after an app restart.
        """
        if not self.running or not self.cfg.get('retention_enabled', False):
            return False
        now = now or datetime.now()
        folder = Path(self.cfg['folder']).expanduser().resolve()
        key = str(folder).casefold() if os.name == 'nt' else str(folder)
        with self.lock:
            desired_state = {'folder': key, 'date': now.date().isoformat()}
            if getattr(self, '_retention_last_in_memory', None) == desired_state:
                return False
            if time.monotonic() < getattr(self, '_retention_retry_after', 0.0):
                return False
            try:
                state = json.loads(RETENTION_STATE_FILE.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                state = {}
            if not isinstance(state, dict):
                state = {}
            if state.get('folder') == key and state.get('date') == now.date().isoformat():
                self._retention_last_in_memory = desired_state
                return False
            try:
                deleted = self.cleanup_old(now)
            except (OSError, ValueError) as exc:
                self._retention_retry_after = time.monotonic() + 300  # avoid repeating errors
                self.on_status('Автоочистка не завершена: ' + str(exc))
                return False
            # Persist only when deletion scanning completed; log any I/O
            # error, but suppress repeated scans in this process.
            state = desired_state
            self._retention_last_in_memory = state
            try:
                atomic_json_write(RETENTION_STATE_FILE, state)
            except OSError as exc:
                self.on_status('Не удалось сохранить дату автоочистки: ' + str(exc))
            self.on_status(f'Ежедневная автоочистка: удалено файлов: {deleted}')
            return True

    def cleanup_old(self, now: datetime | None = None) -> int:
        """Delete only expired MP3/CUE files in the recordings directory.

        Retention uses whole calendar days: before midnight (today - N days).
        Never recurse, follow symlinks, or delete an open audio/CUE file.
        Call directly for tests; normal operation goes via cleanup_if_due().
        """
        if not self.cfg.get('retention_enabled', False):
            return 0
        now = now or datetime.now()
        days = normalize_retention_days(self.cfg.get('retention_days', 90))
        folder = Path(self.cfg.get('folder', '')).expanduser().resolve()
        if (not folder.is_dir() or folder == folder.parent or
            folder == Path.home().resolve() or
            folder == APP_DIR.resolve() or folder in APP_DIR.resolve().parents):
            raise ValueError('Нужна отдельная папка архива; нельзя очищать папку программы, домашнюю или корень диска')
        deadline = (now.replace(hour=0, minute=0, second=0, microsecond=0)
                    - timedelta(days=days)).timestamp()
        count = 0
        with self.lock:
            # Only the currently open MP3/CUE are protected. Past hours are
            # never held in the Recorder or modified during cleanup.
            active = {self.segment.path.resolve(), self.segment.path.with_suffix('.cue').resolve()} if self.segment else set()
            for file in folder.iterdir():
                if (file.is_symlink() or not file.is_file() or
                        file.suffix.lower() not in ('.mp3', '.cue') or
                        file.resolve() in active):
                    continue
                try:
                    if file.stat().st_mtime < deadline:
                        file.unlink()
                        count += 1
                        self.on_status('Удалён старый файл: ' + file.name)
                except OSError as exc:
                    self.on_status('Автоудаление ' + file.name + ': ' + str(exc))
        return count


def next_clock_hour(now: datetime) -> datetime:
    """Next hour strictly AFTER manual Stop; roll 23:xx across midnight."""
    return now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)


def auto_should_start(cfg: dict, now: datetime,
                      manual_pause_until: datetime | None = None) -> bool:
    """Auto stays enabled after Stop, but cannot restart in the same hour.

    Once the pause expires, normal weekly scheduler rules apply; if the next
    hour is disabled, keep waiting for the next enabled scheduled hour.
    """
    return (bool(cfg.get('auto_schedule', False))
            and (manual_pause_until is None or now >= manual_pause_until)
            and is_schedule_active(cfg, now))


def is_schedule_active(cfg: dict, at: datetime | None = None) -> bool:
    at = at or datetime.now()
    return bool(cfg.get('schedule', DEFAULTS['schedule'])[at.weekday()][at.hour])
