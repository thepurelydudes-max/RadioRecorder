"""RadioRecorder: continuous Windows capture -> FFmpeg -> hourly MP3 append.

All worker callbacks are invoked from background threads. Tk callbacks must be
marshalled to the UI thread with a queue.
"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timedelta
import json
import filecmp
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
from cues import TrackEvent, CurrentCue
from mp3_stream import MP3AppendFile, MP3Frame, iter_mp3_frames
from audio_levels import peak_dbfs
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


class _RetentionChanged(Exception):
    """Retry an incomplete cleanup using the newly saved retention policy."""


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
    """Validate unique clock-period names; reuse each period without suffixes."""
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
    # The output worker appends only the selected clock period; no suffix is
    # selected for either this hour or any future hour.
    return fmt + '.mp3'


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
        self._job_handle = None
        self._cleanup_cancel = threading.Event()
        self._cleanup_thread: threading.Thread | None = None
        self._cleanup_run_lock = threading.Lock()
        self._retention_override: tuple[bool, int] | None = None
        self.input_overflows = 0
        self._output_thread: threading.Thread | None = None
        self._mp3_writer: MP3AppendFile | None = None
        self._current_cue: CurrentCue | None = None
        self._capture_active = False
        self._span_wall_start: datetime | None = None
        self._span_audio_base = 0.0
        self._single_path: Path | None = None
        self._observed_title_at: datetime | None = None

    def update_retention(self, enabled: bool, days: int) -> None:
        """Keep a settings save effective even while start is validating input."""
        days = normalize_retention_days(days)
        with self.lock:
            self._retention_override = (enabled, days)
            self.cfg.update(retention_enabled=enabled, retention_days=days)

    def start(self, config: dict, initial_title: str = "") -> None:
        if (self._output_thread is not None and self._output_thread.is_alive()
                and (not self.running or self.stop_requested.is_set())):
            raise RuntimeError('Предыдущая запись MP3 ещё завершается')
        if (self.watch_thread is not None and self.watch_thread.is_alive()
                and self.stop_requested.is_set()):
            raise RuntimeError('Предыдущая запись ещё завершается. Повтори запуск после её завершения.')
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
        with self.lock:
            self.cfg = config.copy()
            if self._retention_override is not None:
                enabled, days = self._retention_override
                self.cfg.update(retention_enabled=enabled, retention_days=days)
        self.recording_started = datetime.now()
        self.segment = None
        self.events.clear()
        self._pending_events.clear()
        self._active_cue_text = None
        self._filename_template = None
        self._process_spawn_at = None
        self._single_path = None
        self._observed_title_at = None
        self.last_title = initial_title or 'Неизвестный трек'
        self.initial_title = self.last_title
        self.stop_requested.clear()
        self._cleanup_cancel.set()
        self._cleanup_cancel = threading.Event()
        self.next_warning_sent = False
        self.input_overflows = 0
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
        cmd = [ffmpeg_path(), '-hide_banner', '-nostdin', '-loglevel', 'info']
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
        cmd += ['-map', '0:a:0', '-vn', '-ac', str(c.get('_capture_channels', c['channels'])), '-ar', str(c['sample_rate'])]
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
        # Keep the validated pattern for clock-period output naming.
        self._filename_template = pat
        self._process_spawn_at = now
        # Complete MP3 frames are routed to append-opened hourly files by the
        # output worker. Each frame is independent across a clock boundary.
        cmd += ['-reservoir', '0', '-write_xing', '0', '-id3v2_version', '0',
                '-flush_packets', '1', '-f', 'mp3', 'pipe:1']
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
                                               channels=channels, dtype='int16', blocksize=1024):
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
        self._join_audio_output()
        if (self.cfg.get('source_kind') == 'device' and
                str(self.cfg.get('source', '')).startswith('portaudio:')):
            self._prepare_portaudio()
        flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
        use_portaudio = (self.cfg.get('source_kind') == 'device' and
                         str(self.cfg.get('source', '')).startswith('portaudio:'))
        command = self._build_command()  # validation before launching FFmpeg
        if self._job_handle is not None:
            release_job(self._job_handle)  # previous encoder already stopped
            self._job_handle = None
        self.process = subprocess.Popen(command,
                                        stdin=subprocess.PIPE if use_portaudio else subprocess.DEVNULL,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                        text=True, errors='replace', bufsize=1,
                                        creationflags=flags)
        try:
            self._job_handle = attach_kill_on_exit_job(self.process)
        except Exception as exc:
            self.on_status('Windows: защита от зависшего FFmpeg недоступна: ' + str(exc))
        threading.Thread(target=self._read_stderr, args=(self.process,),
                         name="ffmpeg-log", daemon=True).start()
        self._output_thread = threading.Thread(target=self._read_mp3_output,
                                               args=(self.process,),
                                               name='mp3-output', daemon=True)
        self._output_thread.start()
        if use_portaudio:
            proc = self.process
            self._audio_thread = threading.Thread(target=self._capture_portaudio,
                                                  args=(proc,), name='portaudio-capture', daemon=True)
            self._audio_thread.start()

    def _capture_portaudio(self, proc: subprocess.Popen) -> None:
        """Push only locally captured PCM to FFmpeg; HTTP remains metadata-only."""
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
            with sd.RawInputStream(samplerate=rate, device=device_id,
                                   channels=channels, dtype='int16', blocksize=1024) as stream:
                last_meter = 0.0
                last_overflow_warning = float('-inf')
                while not self.stop_requested.is_set() and proc.poll() is None:
                    data, overflow = stream.read(1024)
                    if overflow:
                        self.input_overflows += 1
                        now = time.monotonic()
                        if now - last_overflow_warning >= 60:
                            last_overflow_warning = now
                            self.on_status('Переполнение аудиобуфера Windows/PortAudio: '
                                           f'возможны пропуски звука (событий: {self.input_overflows})')
                    if time.monotonic() - last_meter >= 0.12:
                        last_meter = time.monotonic()
                        self.on_meter(peak_dbfs(data))
                    proc.stdin.buffer.write(data)
                    proc.stdin.buffer.flush()
        except (OSError, RuntimeError, ValueError, ImportError) as exc:
            if not self.stop_requested.is_set():
                self.on_status('Ошибка захвата Windows/PortAudio: ' + str(exc))
                if proc.poll() is None:
                    proc.terminate()
        finally:
            if proc.stdin:
                try:
                    proc.stdin.close()
                except (OSError, ValueError):
                    pass

    def _read_stderr(self, process: subprocess.Popen) -> None:
        if process.stderr is None:
            return
        recent_lines: list[str] = []
        try:
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
        finally:
            process.stderr.close()
        if process.poll() and not self.stop_requested.is_set():
            self.on_status('FFmpeg завершился: ' + ' | '.join(recent_lines[-3:])[-380:])

    def _watch(self) -> None:
        """Always retire a failed session, including unexpected filesystem errors."""
        try:
            self._start_cleanup_worker()
            self._watch_loop()
        except Exception as exc:
            self.on_status('Ошибка наблюдения за записью: ' + str(exc))
        finally:
            self.stop_requested.set()
            self._cleanup_cancel.set()
            try:
                proc = self.process
                if proc is not None:
                    self._stop_encoder(proc)
            except Exception as exc:
                self.on_status('Ошибка завершения FFmpeg: ' + str(exc))
            try:
                self._join_audio_output()
                self._sync_output()
                with self.lock:
                    self._update_current_cue()
            except Exception as exc:
                self.on_status('Ошибка завершения записи: ' + str(exc))
            finally:
                if self._output_thread is None or not self._output_thread.is_alive():
                    with self.lock:
                        self._forget_active_segment()
                self.running = False

    def _watch_loop(self) -> None:
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
                self._sync_output()
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
            self._sync_output()
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
            self.stop_requested.wait(0.25)

    def _start_cleanup_worker(self) -> None:
        """Slow archive scans must never delay segment discovery or metadata."""
        cancel = self._cleanup_cancel

        def cleanup_worker():
            while not cancel.is_set():
                try:
                    self.cleanup_if_due(cancel=cancel)
                except Exception as exc:
                    self.on_status('Ошибка фоновой автоочистки: ' + str(exc))
                if cancel.wait(1):
                    break

        self._cleanup_thread = threading.Thread(target=cleanup_worker,
                                                name='archive-cleanup', daemon=True)
        self._cleanup_thread.start()

    def _segment_end(self) -> datetime:
        """Boundary of the active MP3; later metadata belongs to the NEXT CUE."""
        if not self.segment or not self.cfg.get('split_enabled', True):
            return datetime.max
        minutes = int(self.cfg.get('split_minutes', 60))
        return clock_boundary(self.segment.started, minutes) + timedelta(minutes=minutes)

    def _forget_active_segment(self) -> None:
        """Release segment/CUE metadata as soon as capture finishes."""
        self.segment = None
        self.events.clear()
        self._pending_events.clear()
        self._active_cue_text = None
        self._filename_template = None
        self._process_spawn_at = None
        self._current_cue = None

    def _sync_output(self) -> None:
        """Flush the one active output; never enumerate the archive."""
        with self.lock:
            if self._mp3_writer is not None:
                self._mp3_writer.flush()

    def _join_audio_output(self) -> None:
        worker = self._output_thread
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=3)
            if worker.is_alive():
                raise RuntimeError('Запись MP3 ещё завершается; дождись окончания записи')

    def _close_audio_output(self) -> None:
        with self.lock:
            self._capture_active = False
            writer, self._mp3_writer = self._mp3_writer, None
            try:
                if writer is not None:
                    writer.close()
            finally:
                self._update_current_cue()

    def _write_mp3_frame(self, frame: MP3Frame, at: datetime) -> None:
        """Route a complete independent frame into its clock period's MP3."""
        with self.lock:
            frame_written = False
            split = self.cfg.get('split_enabled', True)
            bucket = clock_boundary(at, int(self.cfg.get('split_minutes', 60))) if split else None
            fmt = format_to_strftime(self.cfg['filename_format'])
            if split:
                path = Path(self.cfg['folder']) / (bucket.strftime(fmt) + '.mp3')
            else:
                if self._single_path is None:
                    stamp = self.recording_started or self._process_spawn_at or at
                    self._single_path = Path(self.cfg['folder']) / (stamp.strftime(fmt) + '.mp3')
                path = self._single_path
            if self._mp3_writer is None or self.segment is None or self.segment.path != path:
                self._close_audio_output()
                if path.is_symlink():
                    raise ValueError('MP3 является символической ссылкой; запись в него запрещена')
                cue_path = path.with_suffix('.cue')
                if (not path.exists() or path.stat().st_size == 0) and cue_path.exists():
                    # The metadata cannot describe a new audio timeline. Keep
                    # it recoverable, then create a fresh zero-based CUE.
                    backup = cue_path.with_suffix('.cue.orphan')
                    if not backup.exists() or not filecmp.cmp(cue_path, backup, shallow=False):
                        if backup.exists():
                            target = tempfile.NamedTemporaryFile(
                                prefix=cue_path.name + '.orphan-', suffix='.bak',
                                dir=cue_path.parent, delete=False)
                            backup = Path(target.name)
                        else:
                            target = backup.open('xb')
                        with target:
                            with cue_path.open('rb') as source:
                                shutil.copyfileobj(source, target)
                            target.flush()
                            os.fsync(target.fileno())
                    cue_path.unlink()
                    self.on_status('MP3 отсутствует: прежний CUE сохранён в ' + backup.name)
                cue = CurrentCue(cue_path, path.name, at.year)
                writer = MP3AppendFile(path, frame.sample_rate, frame.channels)
                try:
                    span_start = max(bucket, self._process_spawn_at or at) if split else (self._process_spawn_at or at)
                    audio_base = writer.duration
                    cue.resume(span_start, audio_base, self.last_title)
                    # Validate and persist the first resumed frame before any
                    # CUE seam can claim that this capture has actually begun.
                    writer.write(frame)
                    writer.sync()
                    cue.save()
                except Exception:
                    writer.close()
                    raise
                self._mp3_writer = writer
                self._current_cue = cue
                self._span_wall_start = span_start
                self._span_audio_base = audio_base
                self.segment = Segment(path, bucket or span_start)
                self.events.clear()
                self._capture_active = True
                self.on_status(('Продолжение файла: ' if audio_base else 'Новый файл: ') + path.name)
                frame_written = True
            if not frame_written:
                self._mp3_writer.write(frame)
            self._apply_pending_events()

    def _apply_pending_events(self) -> None:
        """Never persist a CUE index for audio the encoder has not delivered."""
        if self._current_cue is None or self._mp3_writer is None:
            return
        pending, self._pending_events = self._pending_events, []
        changed = False
        for event in sorted(pending, key=lambda item: item.at):
            position = self._span_audio_base + max(0, (event.at - self._span_wall_start).total_seconds())
            if event.at >= self._segment_end() or position > self._mp3_writer.duration:
                self._pending_events.append(event)
            else:
                changed = self._current_cue.add_event(event) or changed
        if changed:
            self._update_current_cue()

    def _read_mp3_output(self, proc: subprocess.Popen) -> None:
        try:
            if proc.stdout is None:
                raise RuntimeError('Не открыт выход MP3 для FFmpeg')
            source = getattr(proc.stdout, 'buffer', proc.stdout)
            for frame in iter_mp3_frames(source):
                self._write_mp3_frame(frame, datetime.now())
        except Exception as exc:
            if not self.stop_requested.is_set():
                self.on_status('Ошибка записи MP3: ' + str(exc))
            if proc.poll() is None:
                try:
                    proc.terminate()
                except OSError:
                    pass  # watcher owns the bounded termination/kill fallback
        finally:
            try:
                self._close_audio_output()
            except Exception as exc:
                self.on_status('Ошибка завершения MP3/CUE: ' + str(exc))
            if proc.stdout is not None:
                proc.stdout.close()

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
            span_start = self._span_wall_start if self._capture_active else self._process_spawn_at
            observed = shifted.at
            if span_start is not None and observed < span_start:
                observed = span_start if event.source == 'API playbackinfo' else None
            if observed is not None and (self._observed_title_at is None or observed >= self._observed_title_at):
                self._observed_title_at = observed
                self.last_title = shifted.title
            # Metadata received while reconnecting seeds the next capture span,
            # but may not invent audio or CUE tracks during the interruption.
            if not self._capture_active or self._current_cue is None:
                self._pending_events.append(shifted)
                self._pending_events = self._pending_events[-98:]
                return
            self._pending_events.append(shifted)
            self._pending_events = self._pending_events[-98:]
            self._apply_pending_events()

    def _update_current_cue(self) -> None:
        if self._current_cue is not None:
            if self._mp3_writer is not None:
                self._mp3_writer.sync()
            self._current_cue.save()

    def _stop_encoder(self, proc: subprocess.Popen) -> None:
        """Use the same bounded termination and kill fallback on every exit."""
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
                # Retain ownership so a later Stop/close can retry rather than
                # launch another encoder or hide a still-running process.
                raise RuntimeError('FFmpeg не завершился штатно: ' + str(exc)) from exc

    def stop(self) -> None:
        self.stop_requested.set()
        self._cleanup_cancel.set()
        proc = self.process
        if proc is not None:
            self._stop_encoder(proc)
        if self.watch_thread and self.watch_thread is not threading.current_thread():
            self.watch_thread.join(timeout=3)
            if self.watch_thread.is_alive():
                raise RuntimeError('Поток записи ещё завершается; новый сеанс пока недоступен')
        self._join_audio_output()
        self.process = None
        if self._job_handle is not None:
            release_job(self._job_handle)
            self._job_handle = None
        self.running = False
        with self.lock:
            self._update_current_cue()
            self._forget_active_segment()
        self.on_status('Запись остановлена')

    def cleanup_if_due(self, now: datetime | None = None, *,
                       cancel: threading.Event | None = None) -> bool:
        """Do cleanup at most once per local calendar day while recording.

        The daily check runs when recording starts (if not yet done today),
        and again after each subsequent midnight while recording remains on.
        An atomic state file prevents another cleanup after an app restart.
        """
        if cancel is not None and cancel.is_set():
            return False
        with self._cleanup_run_lock:
            if cancel is not None and cancel.is_set():
                return False
            with self.lock:
                config = self.cfg.copy()
                if not self.running or not config.get('retention_enabled', False):
                    return False
            now = now or datetime.now()
            folder = Path(config['folder']).expanduser().resolve()
            key = str(folder).casefold() if os.name == 'nt' else str(folder)
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
                deleted = self.cleanup_old(now, config=config, cancel=cancel)
            except _RetentionChanged:
                return False
            except (OSError, ValueError) as exc:
                self._retention_retry_after = time.monotonic() + 300  # avoid repeating errors
                self.on_status('Автоочистка не завершена: ' + str(exc))
                return False
            if cancel is not None and cancel.is_set():
                return False  # partial/cancelled scans must be retried
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

    def cleanup_old(self, now: datetime | None = None, *, config: dict | None = None,
                    cancel: threading.Event | None = None) -> int:
        """Delete only expired MP3/CUE files in the recordings directory.

        Retention uses whole calendar days: before midnight (today - N days).
        Never recurse, follow symlinks, or delete an open audio/CUE file.
        Call directly for tests; normal operation goes via cleanup_if_due().
        """
        if config is None:
            with self.lock:
                config = self.cfg.copy()
        if not config.get('retention_enabled', False):
            return 0
        now = now or datetime.now()
        days = normalize_retention_days(config.get('retention_days', 90))
        folder = Path(config.get('folder', '')).expanduser().resolve()
        if (not folder.is_dir() or folder == folder.parent or
            folder == Path.home().resolve() or
            folder == APP_DIR.resolve() or folder in APP_DIR.resolve().parents):
            raise ValueError('Нужна отдельная папка архива; нельзя очищать папку программы, домашнюю или корень диска')
        deadline = (now.replace(hour=0, minute=0, second=0, microsecond=0)
                    - timedelta(days=days)).timestamp()
        count = 0
        for file in folder.iterdir():
            if cancel is not None and cancel.is_set():
                break
            if file.suffix.lower() not in ('.mp3', '.cue'):
                continue
            try:
                if file.is_symlink() or not file.is_file() or file.stat().st_mtime >= deadline:
                    continue
                resolved = file.resolve()
                # The active segment may change during the scan. Recheck at
                # deletion time; never hold this lock over directory iteration.
                with self.lock:
                    if cancel is not None and cancel.is_set():
                        break
                    if cancel is not None and any(self.cfg.get(key) != config.get(key)
                            for key in ('folder', 'retention_enabled', 'retention_days')):
                        raise _RetentionChanged()
                    active = ({self.segment.path.resolve(),
                               self.segment.path.with_suffix('.cue').resolve()}
                              if self.segment else set())
                    if resolved in active or file.is_symlink() or file.stat().st_mtime >= deadline:
                        continue
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

