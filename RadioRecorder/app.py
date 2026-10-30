"""RadioRecorder - portable, Russian-language Windows radio recording interface."""
from __future__ import annotations

import ctypes
from datetime import datetime, timedelta
import os
from pathlib import Path
import queue
import shutil
import sys
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from tkinter.scrolledtext import ScrolledText
import traceback
import logging
from logging.handlers import RotatingFileHandler

from engine import (APP_DIR, DATA_DIR, DEFAULTS, SETTINGS_FILE, Recorder,
                    AUDIO_BUFFER_CHOICES, ffmpeg_path,
                    format_to_strftime, is_schedule_active, auto_should_start,
                    next_clock_hour, list_audio_devices, load_config, preview_name, save_config)
from metadata import MetadataMonitor
from monitor import AudioMonitor, list_playback_devices, list_monitor_input_devices
from alerts import send_silence_email
from autostart import configure_login, normalize_startup_settings
from single_instance import SingleInstanceGuard, show_existing_window
from device_names import friendly_device_name, settings_device_choices, selection_label

VERSION = "0.6.4"
TITLE = f"RadioRecorder ({VERSION})"


def create_diagnostic_logger() -> logging.Logger:
    logger = logging.getLogger('RadioRecorder')
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            handler = RotatingFileHandler(DATA_DIR / 'radiorecorder.log', maxBytes=1_000_000,
                                          backupCount=2, encoding='utf-8')
            handler.setFormatter(logging.Formatter('%(asctime)s %(message)s'))
            logger.addHandler(handler)
        except OSError:
            # Lack of disk space must never crash the GUI/logger itself.
            pass
    return logger




def is_windows_autostart_launch(argv: list[str] | None = None) -> bool:
    """Manual launches remain visible; Windows login launch passes a marker."""
    return '--autostart' in (sys.argv[1:] if argv is None else argv)



def channels_to_label(value: int | str) -> str:
    """Presentation-only channel selector; persisted settings remain numeric."""
    return 'Stereo' if str(value) == '2' else 'Mono'


def label_to_channels(value: str) -> int:
    if value == 'Mono':
        return 1
    if value == 'Stereo':
        return 2
    raise ValueError('Выбери Mono или Stereo')


def find_asset(filename: str) -> Path | None:
    """Find icon in source, PyInstaller --onedir and --onefile layouts."""
    locations = [APP_DIR / 'assets' / filename]
    if getattr(sys, '_MEIPASS', None):
        locations.append(Path(sys._MEIPASS) / 'assets' / filename)
    for item in locations:
        if item.is_file():
            return item
    return None


def apply_app_icon(window: tk.Tk) -> None:
    """Show branding in the titlebar/Alt+Tab/taskbar for packaged GUI."""
    icon_png = find_asset('radiorecorder_64.png')
    icon_ico = find_asset('radiorecorder.ico')
    # iconphoto is cross-platform; on Windows iconbitmap gives Win32 an ICO
    # containing multiple resolutions. Set it *after* iconphoto to avoid reset.
    if icon_png:
        try:
            photo = tk.PhotoImage(file=str(icon_png), master=window)
            window._app_icon_ref = photo
            window.iconphoto(True, photo)
        except tk.TclError:
            pass
    if sys.platform == 'win32' and icon_ico:
        try:
            window.iconbitmap(default=str(icon_ico))
        except tk.TclError:
            pass


def configure_windows_app_id() -> None:
    """Ask Windows to use this program's taskbar icon (not pythonw.exe)."""
    if sys.platform == 'win32':
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID('FARLINER.RadioRecorder')
        except (AttributeError, OSError):
            pass


def list_recording_choices(ffmpeg: str) -> list[tuple[str, str]]:
    """Show both proven FFmpeg DirectShow inputs and native Windows/PortAudio inputs.

    The PortAudio entries are not passed as DirectShow IDs: Recorder captures
    those as PCM and streams it into the same MP3 encoder.
    """
    choices = list_audio_devices(ffmpeg) if ffmpeg else []
    try:
        # Unlike DirectShow device listing, PortAudio can enumerate real
        # Windows audio inputs even when ffmpeg.exe is temporarily missing.
        for label, _ in list_monitor_input_devices():
            if label != 'Auto':
                choices.append((label, 'portaudio:' + label))
    except Exception:
        pass  # Preserve old DirectShow devices if optional sounddevice fails.
    return choices


def run_quiet_thread(fn, ui_queue):
    def wrapped():
        try:
            fn()
        except Exception as exc:
            ui_queue.put(("log", f"Ошибка: {exc}"))
    threading.Thread(target=wrapped, daemon=True).start()


def apply_run_at_login(enable: bool, elevated: bool = False,
                       previous_elevated: bool = False) -> None:
    """Normal startup via registry; admin startup via Windows Task Scheduler."""
    configure_login(enable, elevated, previous_elevated)


def windows_keep_awake(enabled: bool) -> None:
    if sys.platform != 'win32':
        return
    try:
        # ES_CONTINUOUS, ES_SYSTEM_REQUIRED
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000001 if enabled else 0x80000000)
    except OSError:
        pass


class SettingsWindow(tk.Toplevel):
    def __init__(self, master: 'RadioRecorderUI'):
        super().__init__(master)
        self.master_ui = master
        self.title('Настройки — RadioRecorder')
        apply_app_icon(self)
        self.resizable(False, False)
        self.transient(master)
        self.grab_set()
        self.protocol('WM_DELETE_WINDOW', self._close_if_idle)
        c = master.cfg
        self.variables: dict[str, tk.Variable] = {}
        for key in ('source', 'stream_url', 'folder', 'filename_format', 'instance_name',
                    'metadata_api_url', 'metadata_username', 'metadata_password',
                    'metadata_icy_url', 'email_to', 'smtp_server', 'smtp_user', 'smtp_password',
                    'monitor_device', 'monitor_input', 'source_label'):
            self.variables[key] = tk.StringVar(value=str(c.get(key, '')))
        for key in ('bitrate', 'sample_rate', 'channels', 'buffer_samples',
                    'split_minutes', 'retention_days',
                    'silence_seconds', 'silence_db', 'smtp_port'):
            self.variables[key] = tk.StringVar(value=str(c.get(key, DEFAULTS[key])))
        for key in ('split_enabled', 'retention_enabled', 'start_with_windows', 'minimize_on_start',
                    'ask_on_exit', 'reboot_on_error', 'keep_awake', 'silence_enabled',
                    'silence_email_only_recording', 'smtp_tls', 'start_with_admin'):
            self.variables[key] = tk.BooleanVar(value=bool(c.get(key)))
        self.variables['source_kind'] = tk.StringVar(value=c.get('source_kind', 'device'))
        self.variables['metadata_mode'] = tk.StringVar(value=c.get('metadata_mode', 'icy'))
        self.variables['cue_shift_sec'] = tk.StringVar(value=str(c.get('cue_shift_sec', 0)))
        self.filename_preview = tk.StringVar(value='')
        self.v('filename_format').trace_add('write', self._update_filename_preview)
        self._update_filename_preview()
        self.channel_label = tk.StringVar(value=channels_to_label(c.get('channels', 1)))
        self.input_devices = []
        self.original_input_devices = []
        self.playback_devices = []
        self.monitor_input_devices = []
        self._construct()
        self._sync_startup_controls()
        self.after(100, self._scan_devices)
        self.after(140, self._scan_playback_devices)

    def v(self, key):
        return self.variables[key]

    def _construct(self):
        outer = ttk.Frame(self, padding=8)
        outer.pack(fill='both', expand=True)
        body = ttk.Frame(outer)
        body.pack(fill='both', expand=True)
        # A fixed-height, scrollable settings panel stays usable on laptops,
        # even when Windows uses 125-150% text scaling.
        view = tk.Canvas(body, borderwidth=0, highlightthickness=0,
                         width=845, height=max(280, min(610, self.winfo_screenheight()-160)))
        scrollbar = ttk.Scrollbar(body, orient='vertical', command=view.yview)
        view.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side='right', fill='y')
        view.pack(side='left', fill='both', expand=True)
        frame = ttk.Frame(view)
        view.create_window((0, 0), window=frame, anchor='nw')
        frame.bind('<Configure>', lambda e: view.configure(scrollregion=view.bbox('all')))
        def wheel(ev):
            view.yview_scroll(int(-ev.delta / 120), 'units')
        view.bind('<MouseWheel>', wheel)
        frame.bind('<MouseWheel>', wheel)
        left = ttk.Frame(frame)
        right = ttk.Frame(frame)
        left.grid(row=0, column=0, sticky='n', padx=(0, 7))
        right.grid(row=0, column=1, sticky='n')
        self._source(left)
        self._record(left)
        self._storage(left)
        self._retention(left)
        self._metadata(right)
        self._playback(right)
        self._alerts(right)
        self._general(right)
        # The original two-column layout stays intact. Size the fixed canvas
        # to the actual content instead of leaving an unused strip on the right.
        frame.update_idletasks()
        view.configure(width=min(845, max(680, frame.winfo_reqwidth() + 8)))
        buttons = ttk.Frame(outer)
        buttons.pack(fill='x', pady=(9, 0))
        ttk.Button(buttons, text='Сохранить', width=13, command=self._save).pack(side='right', padx=3)
        ttk.Button(buttons, text='Отмена', width=13, command=self._close_if_idle).pack(side='right', padx=3)

    def _source(self, parent):
        group = ttk.LabelFrame(parent, text='Источник записи', padding=7)
        group.pack(fill='x', pady=(0, 6))
        row = ttk.Frame(group)
        row.pack(fill='x')
        ttk.Radiobutton(row, text='Устройство Windows', variable=self.v('source_kind'),
                        value='device').pack(side='left')
        ttk.Radiobutton(row, text='HTTP-поток', variable=self.v('source_kind'),
                        value='stream').pack(side='left', padx=8)
        self.devices_box = ttk.Combobox(group, textvariable=self.v('source'), width=40,
                                        state='readonly')
        self.devices_box.pack(fill='x', pady=3)
        ttk.Button(group, text='Обновить список устройств', command=self._scan_devices).pack(anchor='w')
        ttk.Label(group, text='URL аудиопотока (только если выбран HTTP):').pack(anchor='w', pady=(4, 0))
        ttk.Entry(group, width=43, textvariable=self.v('stream_url')).pack(fill='x')

    def _scan_devices(self):
        self.devices_box['values'] = ('Поиск устройств Windows...',)
        def scan():
            devs = list_recording_choices(ffmpeg_path())
            self.master_ui.events.put(('devices', (self, devs)))
        run_quiet_thread(scan, self.master_ui.events)

    def _devices_ready(self, devs):
        if not self.winfo_exists():
            return
        # Settings show host API (WASAPI/MME/DirectSound/DirectShow).
        # Actual capture identifiers stay untouched.
        self.original_input_devices = devs
        self.input_devices = settings_device_choices(devs, mark_directshow=True)
        self.devices_box['values'] = [label for label, _ in self.input_devices]
        label = selection_label(self.v('source').get(), self.input_devices, devs)
        if label is not None:
            self.v('source').set(label)
        if not devs:
            self.master_ui._log('Аудиовходы не найдены. Проверь устройства записи Windows и разрешение на микрофон')

    def _playback(self, parent):
        group = ttk.LabelFrame(parent, text='Воспроизведение / прослушивание', padding=7)
        group.pack(fill='x', pady=(0, 6))
        ttk.Label(group, text='Устройство вывода звука (колонки / наушники):').pack(anchor='w')
        self.output_box = ttk.Combobox(group, textvariable=self.v('monitor_device'), width=38,
                                       state='readonly', values=('Default',))
        self.output_box.pack(fill='x', pady=(2, 4))
        ttk.Label(group, text='Вход для прослушивания (при записи с устройства):').pack(anchor='w')
        self.monitor_input_box = ttk.Combobox(group, textvariable=self.v('monitor_input'), width=38,
                                              state='readonly', values=('Auto',))
        self.monitor_input_box.pack(fill='x', pady=(2, 4))
        ttk.Button(group, text='Обновить устройства воспроизведения',
                   command=self._scan_playback_devices).pack(anchor='w')
        ttk.Label(group, text='Кнопка ▶ в главном окне включает прослушивание.').pack(anchor='w', pady=(4, 0))
        ttk.Label(group, text='Auto: вход прослушивания определяется по источнику записи.',
                  wraplength=385).pack(anchor='w')

    def _scan_playback_devices(self):
        def scan():
            try:
                outputs = list_playback_devices()
                inputs = list_monitor_input_devices()
                self.master_ui.events.put(('playback_devices', (self, outputs, inputs, None)))
            except Exception as exc:
                self.master_ui.events.put(('playback_devices', (self, [('Default', 'Default')],
                                                       [('Auto', 'Auto')], str(exc))))
        run_quiet_thread(scan, self.master_ui.events)

    def _playback_devices_ready(self, outputs, inputs, error):
        if not self.winfo_exists():
            return
        self.playback_devices = settings_device_choices(outputs)
        self.monitor_input_devices = settings_device_choices(inputs)
        self.output_box['values'] = [label for label, _ in self.playback_devices]
        self.monitor_input_box['values'] = [label for label, _ in self.monitor_input_devices]
        for key, formatted, original in (
                ('monitor_device', self.playback_devices, outputs),
                ('monitor_input', self.monitor_input_devices, inputs)):
            label = selection_label(self.v(key).get(), formatted, original)
            if label is not None:
                self.v(key).set(label)
        if error:
            self.master_ui._log('Список устройств воспроизведения: ' + error)

    def _record(self, parent):
        group = ttk.LabelFrame(parent, text='Формат записи', padding=7)
        group.pack(fill='x', pady=(0, 6))
        row = ttk.Frame(group)
        row.pack(fill='x')
        ttk.Label(row, text='MP3').pack(side='left', padx=(0, 9))
        for name, vals, w in [('sample_rate', ('22050', '32000', '44100', '48000'), 8),
                               ('bitrate', ('64', '96', '128', '160', '192', '256', '320'), 5),
                               ('channels', ('Mono', 'Stereo'), 9)]:
            variable = self.channel_label if name == 'channels' else self.v(name)
            ttk.Combobox(row, textvariable=variable, values=vals,
                         state='readonly', width=w).pack(side='left', padx=3)
            if name == 'bitrate':
                ttk.Label(row, text='kbps').pack(side='left')
        # Single setting; latency in milliseconds follows automatically
        # from sample rate and selected sample buffer.
        buffer_row = ttk.Frame(group)
        buffer_row.pack(fill='x', pady=(5, 0))
        ttk.Label(buffer_row, text='Буфер аудиозахвата:').pack(side='left', padx=(0, 6))
        ttk.Combobox(buffer_row, textvariable=self.v('buffer_samples'),
                     values=tuple(str(v) for v in AUDIO_BUFFER_CHOICES),
                     state='readonly', width=7).pack(side='left')
        ttk.Label(buffer_row, text='сэмплов (мс рассчитываются автоматически)').pack(
            side='left', padx=(6, 0))

    def _update_filename_preview(self, *_args):
        """Show the resulting MP3 basename while the template is edited."""
        template = self.v('filename_format').get()
        try:
            rendered = preview_name(template)
        except (ValueError, OverflowError, OSError):
            rendered = 'Некорректный формат имени файла'
        self.filename_preview.set('Пример: ' + rendered)

    def _storage(self, parent):
        group = ttk.LabelFrame(parent, text='Сохранение', padding=7)
        group.pack(fill='x', pady=(0, 6))
        ttk.Label(group, text='Папка:').pack(anchor='w')
        row = ttk.Frame(group)
        row.pack(fill='x')
        ttk.Entry(row, textvariable=self.v('folder'), width=38).pack(side='left', fill='x', expand=True)
        ttk.Button(row, text='...', width=3, command=self._pick_folder).pack(side='left', padx=(4, 0))
        ttk.Label(group, text='Формат имени файла:').pack(anchor='w', pady=(5, 0))
        ttk.Entry(group, textvariable=self.v('filename_format'), width=43).pack(fill='x')
        ttk.Label(group, textvariable=self.filename_preview, wraplength=340,
                  justify='left').pack(anchor='w')
        ttk.Label(group, text='%d — день; %m — месяц; %y — год; %hh — часы; %mm — минуты; %ss — секунды',
                  wraplength=340, justify='left').pack(anchor='w')
        ttk.Checkbutton(group, text='Разделять запись каждые', variable=self.v('split_enabled')).pack(side='left', pady=5)
        ttk.Spinbox(group, from_=1, to=1440, increment=1, width=5,
                    textvariable=self.v('split_minutes')).pack(side='left', padx=5)
        ttk.Label(group, text='минут').pack(side='left')

    def _retention(self, parent):
        group = ttk.LabelFrame(parent, text='Автоудаление', padding=7)
        group.pack(fill='x')
        row = ttk.Frame(group)
        row.pack(fill='x')
        ttk.Checkbutton(row, text='Удалять записи старше', variable=self.v('retention_enabled')).pack(side='left')
        ttk.Spinbox(row, from_=90, to=3650, width=6, textvariable=self.v('retention_days')).pack(side='left', padx=5)
        ttk.Label(row, text='дней').pack(side='left')
        ttk.Label(group, text='После 00:00 ежедневно удаляются все старые файлы в папке архива (без подпапок).',
                  wraplength=350).pack(anchor='w', pady=(4, 0))

    def _metadata(self, parent):
        group = ttk.LabelFrame(parent, text='Метаданные RadioBOSS → CUE', padding=7)
        group.pack(fill='x', pady=(0, 6))
        row = ttk.Frame(group)
        row.pack(anchor='w')
        for value, name in [('api', 'HTTP API'), ('icy', 'ICY из потока'), ('none', 'Выкл.')]:
            ttk.Radiobutton(row, text=name, variable=self.v('metadata_mode'),
                            value=value).pack(side='left', padx=(0, 6))
        ttk.Label(group, text='Адрес RadioBOSS API (обычно порт 9001):').pack(anchor='w', pady=(4, 0))
        ttk.Entry(group, width=39, textvariable=self.v('metadata_api_url')).pack(fill='x')
        row = ttk.Frame(group)
        row.pack(fill='x', pady=4)
        ttk.Label(row, text='Логин:').pack(side='left')
        ttk.Entry(row, width=14, textvariable=self.v('metadata_username')).pack(side='left', padx=3)
        ttk.Label(row, text='Пароль:').pack(side='left', padx=(5, 0))
        ttk.Entry(row, width=15, textvariable=self.v('metadata_password'), show='•').pack(side='left', padx=3)
        ttk.Label(group, text='URL потока с ICY-метаданными (обычно порт 8000):').pack(anchor='w')
        ttk.Entry(group, width=39, textvariable=self.v('metadata_icy_url')).pack(fill='x')
        row = ttk.Frame(group)
        row.pack(fill='x', pady=(5, 0))
        ttk.Label(row, text='Коррекция времени CUE, сек:').pack(side='left')
        ttk.Entry(row, textvariable=self.v('cue_shift_sec'), width=7).pack(side='left', padx=4)
        ttk.Label(group, text='API точнее: история треков с метками начала.').pack(anchor='w', pady=(4, 0))

    def _alerts(self, parent):
        group = ttk.LabelFrame(parent, text='Уведомления', padding=7)
        group.pack(fill='x', pady=(0, 6))
        ttk.Checkbutton(group, text='Отправлять уведомление при тишине', variable=self.v('silence_enabled')).pack(anchor='w')
        ttk.Checkbutton(group, text='Отправлять только если идёт запись',
                        variable=self.v('silence_email_only_recording')).pack(anchor='w')
        row = ttk.Frame(group)
        row.pack(fill='x')
        ttk.Label(row, text='Длительность тишины:').pack(side='left')
        ttk.Spinbox(row, from_=1, to=3600, textvariable=self.v('silence_seconds'), width=4).pack(side='left', padx=3)
        ttk.Label(row, text='сек; порог:').pack(side='left')
        ttk.Spinbox(row, from_=-100, to=0, textvariable=self.v('silence_db'), width=5).pack(side='left', padx=3)
        ttk.Label(row, text='dB').pack(side='left')
        ttk.Label(group, text='Email получателя:').pack(anchor='w')
        ttk.Entry(group, textvariable=self.v('email_to')).pack(fill='x')
        ttk.Label(group, text='SMTP сервер / порт:').pack(anchor='w', pady=(3, 0))
        row = ttk.Frame(group)
        row.pack(fill='x')
        ttk.Entry(row, width=25, textvariable=self.v('smtp_server')).pack(side='left', padx=(0, 4))
        ttk.Entry(row, width=6, textvariable=self.v('smtp_port')).pack(side='left')
        ttk.Label(group, text='SMTP пользователь / пароль:').pack(anchor='w', pady=(3, 0))
        row = ttk.Frame(group)
        row.pack(fill='x')
        ttk.Entry(row, width=19, textvariable=self.v('smtp_user')).pack(side='left', padx=(0, 4))
        ttk.Entry(row, width=19, textvariable=self.v('smtp_password'), show='•').pack(side='left')
        ttk.Checkbutton(group, text='STARTTLS', variable=self.v('smtp_tls')).pack(anchor='w')

    def _general(self, parent):
        group = ttk.LabelFrame(parent, text='Общие', padding=7)
        group.pack(fill='x')
        choices = [
            ('start_with_windows', 'Запускать с Windows'),
            ('start_with_admin', 'Запуск с правами администратора'),
            ('minimize_on_start', 'Сворачивать при автозапуске'),
            ('ask_on_exit', 'Спрашивать при закрытии'),
            ('reboot_on_error', 'Перезапуск при зависании или ошибке'),
            ('keep_awake', 'Запретить переход компьютера в спящий режим'),
        ]
        for key, label in choices:
            option = ttk.Checkbutton(group, text=label, variable=self.v(key),
                                     command=(self._sync_startup_controls if key == 'start_with_windows' else
                                              self._on_admin_toggle if key == 'start_with_admin' else None))
            option.pack(anchor='w')
            if key == 'start_with_admin':
                self.admin_checkbox = option
        ttk.Label(group, text='Название экземпляра:').pack(anchor='w', pady=(3, 0))
        ttk.Entry(group, textvariable=self.v('instance_name')).pack(fill='x')

    def _sync_startup_controls(self):
        # The admin checkbox is always clickable. Elevated autostart needs
        # Windows autostart; disabling the latter clears elevated mode.
        if not self.v('start_with_windows').get():
            self.v('start_with_admin').set(False)
        self.admin_checkbox.configure(state='normal')

    def _on_admin_toggle(self):
        # Make an explicit admin selection effective instead of silently
        # discarding it when the Windows startup box was still unchecked.
        if self.v('start_with_admin').get():
            self.v('start_with_windows').set(True)

    def _pick_folder(self):
        folder = filedialog.askdirectory(parent=self, initialdir=self.v('folder').get() or str(Path.home()))
        if folder:
            self.v('folder').set(folder)

    def _close_if_idle(self):
        if not getattr(self, '_saving', False):
            self.destroy()

    def _save(self):
        if getattr(self, '_saving', False):
            return
        try:
            new = self.master_ui.cfg.copy()
            for key, var in self.variables.items():
                new[key] = var.get()
            # The visible Mono/Stereo labels must never reach the FFmpeg args.
            new['channels'] = label_to_channels(self.channel_label.get())
            for key in ('bitrate', 'sample_rate', 'channels', 'buffer_samples',
                        'split_minutes', 'retention_days',
                        'silence_seconds', 'silence_db', 'smtp_port'):
                new[key] = int(new[key])
            new['cue_shift_sec'] = float(new['cue_shift_sec'])
            if new['bitrate'] not in (64, 96, 128, 160, 192, 256, 320):
                raise ValueError('Неверный битрейт')
            if new['buffer_samples'] not in AUDIO_BUFFER_CHOICES:
                raise ValueError('Размер буфера: выбери от 128 до 2048 сэмплов')
            if not 1 <= new['split_minutes'] <= 1440:
                raise ValueError('Интервал от 1 до 1440 минут')
            if not 90 <= new['retention_days'] <= 3650:
                raise ValueError('Срок хранения от 90 до 3650 дней')
            format_to_strftime(new['filename_format'])
            if not new['folder'].strip():
                raise ValueError('Укажи папку для записи')
            if new['source_kind'] == 'device':
                for index, (label, internal) in enumerate(self.input_devices):
                    if self.v('source').get() in (label, internal):
                        new['source'] = internal
                        # Keep the real device name for DirectShow-to-monitor
                        # matching, not a UI-only duplicate number like (2).
                        original = self.original_input_devices[index][0]
                        new['source_label'] = friendly_device_name(original)
                        break
            for key, choices in (('monitor_device', self.playback_devices),
                                 ('monitor_input', self.monitor_input_devices)):
                for label, value in choices:
                    if self.v(key).get() in (label, value):
                        new[key] = value
                        break
            new['start_with_windows'], new['start_with_admin'] = normalize_startup_settings(
                new['start_with_windows'], new['start_with_admin'])
            old = self.master_ui.cfg
            registration_changed = (
                (new['start_with_windows'], new['start_with_admin']) !=
                (old.get('start_with_windows'), old.get('start_with_admin', False)) or
                (new['start_with_windows'] and
                 new['minimize_on_start'] != old.get('minimize_on_start', False)))
            if registration_changed:
                # UAC/PowerShell may take minutes. Never freeze the Tk thread
                # while Windows is awaiting permission or Task Scheduler fails.
                self._saving = True
                self.configure(cursor='watch')
                previous_admin = bool(old.get('start_with_admin', False) and
                                      old.get('start_with_windows', False))

                def register():
                    try:
                        apply_run_at_login(new['start_with_windows'],
                                           new['start_with_admin'], previous_admin)
                        error = None
                    except Exception as exc:
                        error = str(exc)
                    self.master_ui.events.put(('startup_save_done', (self, new, error)))

                threading.Thread(target=register, name='startup-settings', daemon=True).start()
            else:
                self._finish_save(new)
        except (ValueError, OSError, RuntimeError) as exc:
            messagebox.showerror('Ошибка настроек', str(exc), parent=self)

    def _finish_save(self, new: dict, error: str | None = None):
        self._saving = False
        try:
            self.configure(cursor='')
        except tk.TclError:
            return
        if error:
            messagebox.showerror('Ошибка настройки автозапуска', error, parent=self)
            return
        try:
            save_config(new)
            self.master_ui.cfg = new
            # Retention settings apply immediately while current audio source
            # and MP3 settings are switched at the next recording start.
            if self.master_ui.recorder.running:
                with self.master_ui.recorder.lock:
                    self.master_ui.recorder.cfg.update(
                        retention_enabled=new['retention_enabled'],
                        retention_days=new['retention_days'])
            windows_keep_awake(bool(new.get('keep_awake')))
            self.master_ui._reload_settings()
            self.master_ui._log('Настройки сохранены. Изменения аудиоформата и буфера — со следующего запуска записи.')
            self.destroy()
        except (ValueError, OSError, RuntimeError) as exc:
            messagebox.showerror('Ошибка настроек', str(exc), parent=self)


class ScheduleWindow(tk.Toplevel):
    def __init__(self, master: 'RadioRecorderUI'):
        super().__init__(master)
        self.main = master
        self.title('Планировщик — RadioRecorder')
        apply_app_icon(self)
        self.resizable(False, False)
        self.schedule = [list(day) for day in master.cfg['schedule']]
        ttk.Label(self, text='Часы для записи (отмеченные зелёным будут записываться):\n'
                             'Для выделения используйте пробел, двойной клик или правый клик мыши.',
                  padding=8).pack(anchor='w')
        table = ttk.Frame(self, padding=(8, 0, 8, 8))
        table.pack()
        days = ('Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс')
        ttk.Label(table, text='', width=12).grid(row=0, column=0)
        for d, day in enumerate(days):
            ttk.Label(table, text=day, anchor='center', width=7).grid(row=0, column=d + 1)
        self.cells = {}
        for hour in range(24):
            tk.Label(table, text=f'{hour:02d}:00 - {(hour+1)%24:02d}:00', width=12,
                     anchor='w', bg='#eaeaea', relief='solid', borderwidth=1).grid(row=hour+1, column=0, sticky='nsew')
            for d in range(7):
                btn = tk.Label(table, width=7, height=1, bg='#a8ff9a' if self.schedule[d][hour] else '#ffffff',
                               borderwidth=1, relief='solid', cursor='hand2')
                btn.grid(row=hour+1, column=d+1, sticky='nsew')
                btn.bind('<Button-1>', lambda _, x=d, y=hour: self._toggle(x,y))
                btn.bind('<Button-3>', lambda _, x=d, y=hour: self._toggle(x,y))
                self.cells[(d,hour)] = btn
        self.protocol('WM_DELETE_WINDOW', self._save)

    def _toggle(self, d, hour):
        self.schedule[d][hour] = not self.schedule[d][hour]
        self.cells[(d,hour)]['bg'] = '#a8ff9a' if self.schedule[d][hour] else '#ffffff'
        self.main.cfg['schedule'] = self.schedule
        save_config(self.main.cfg)

    def _save(self):
        self.main.cfg['schedule'] = self.schedule
        save_config(self.main.cfg)
        self.destroy()


class RadioRecorderUI(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(TITLE)
        apply_app_icon(self)
        self.geometry('650x385')
        self.resizable(False, False)
        self.cfg = load_config()
        self.diag_logger = create_diagnostic_logger()
        self.events = queue.Queue()
        self.recorder = Recorder(lambda s: self.events.put(('log', s)),
                                 lambda seconds: self.events.put(('elapsed', seconds)),
                                 lambda db: self.events.put(('meter', db)), self._silence_alert)
        self.metadata = MetadataMonitor(self._on_metadata, lambda s: self.events.put(('log', s)))
        self.monitor = AudioMonitor(lambda active, msg: self.events.put(('monitor_state', (active, msg))),
                                    lambda db: self.events.put(('monitor_meter', db)))
        self.monitor_active = False
        self._current_db = -60.0
        self.elapsed = tk.StringVar(value='0:00:00')
        self.source_title = tk.StringVar(value='')
        self.codec_title = tk.StringVar(value='Кодек: MP3 128 kbps Mono')
        self.disk_title = tk.StringVar(value='Осталось на диске: —')
        self.auto = tk.BooleanVar(value=bool(self.cfg.get('auto_schedule')))
        # Stop pauses automatic restarts only until the NEXT clock hour.
        # It does not uncheck Auto and the pause is intentionally not saved:
        # reopening the program starts Auto immediately when scheduled.
        self.manual_pause_until: datetime | None = None
        self.auto_retry_after: datetime | None = None
        self.loglines = []
        self._build()
        self._reload_settings()
        self.protocol('WM_DELETE_WINDOW', self._on_close)
        self.after(150, self._drain)
        self.after(1200, self._heartbeat)
        windows_keep_awake(bool(self.cfg.get('keep_awake')))
        self._log('RadioRecorder запущен')
        if self.cfg.get('minimize_on_start') and is_windows_autostart_launch():
            self.after(200, self.iconify)
        if self.auto.get():
            self.after(1500, self._schedule_check)

    def _build(self):
        bar = tk.Menu(self)
        prog = tk.Menu(bar, tearoff=False)
        prog.add_command(label='Настройки...', command=self._settings)
        prog.add_command(label='Открыть папку записей', command=self._open_folder)
        prog.add_separator()
        prog.add_command(label='Выход', command=self._on_close)
        bar.add_cascade(label='RadioRecorder', menu=prog)
        help_menu = tk.Menu(bar, tearoff=False)
        help_menu.add_command(label='О программе', command=lambda: messagebox.showinfo('RadioRecorder',
            f'RadioRecorder {VERSION}\nЛокальная запись MP3 и раздельные CUE из метаданных RadioBOSS.'))
        bar.add_cascade(label='Справка', menu=help_menu)
        self.config(menu=bar)
        wrap = ttk.Frame(self, padding=8)
        wrap.pack(fill='both', expand=True)
        top = ttk.Frame(wrap)
        top.pack(fill='x')
        lef = ttk.Frame(top)
        lef.pack(side='left', fill='both', expand=True)
        ttk.Label(lef, textvariable=self.source_title, font=('Segoe UI', 10, 'bold')).pack(anchor='w')
        ttk.Label(lef, textvariable=self.codec_title).pack(anchor='w', pady=2)
        ttk.Label(lef, textvariable=self.disk_title).pack(anchor='w', pady=2)
        right = ttk.Frame(top)
        right.pack(side='right', anchor='ne')
        ttk.Label(right, textvariable=self.elapsed, font=('Consolas', 12), relief='sunken', padding=3).pack(side='left', padx=5)
        ttk.Label(right, text='Прослушать:').pack(side='left', padx=(3, 1))
        # Play stays Play on BOTH presses; it never starts/stops MP3 recording.
        self.playbtn = tk.Button(right, text='▶', width=3, command=self._toggle_monitor,
                                 font=('Segoe UI Symbol', 11), fg='#303030',
                                 activeforeground='#1d7a37', relief='raised')
        self.playbtn.pack(side='left')
        ctr = ttk.Frame(wrap)
        ctr.pack(fill='x', pady=6)
        ttk.Button(ctr, text='Планировщик', width=15, command=self._scheduler).pack(side='left', padx=(0, 5))
        ttk.Button(ctr, text='Архив', width=12, command=self._archive).pack(side='left', padx=3)
        ttk.Checkbutton(ctr, text='Авто', variable=self.auto, command=self._toggle_auto).pack(side='left', padx=6)
        ttk.Button(ctr, text='Настройки', width=10, command=self._settings).pack(side='left', padx=3)
        self.rec_idle_icon = self._make_button_icon('circle', '#777777')
        self.rec_live_icon = self._make_button_icon('circle', '#e31d22')
        self.stop_icon = self._make_button_icon('square', '#111111')
        self.rec_button = ttk.Button(ctr, text='Rec', image=self.rec_idle_icon,
                                     compound='left', width=9, command=self._start)
        self.rec_button.pack(side='right', padx=3)
        ttk.Button(ctr, text='Stop', image=self.stop_icon, compound='left',
                   width=9, command=self._stop_manual).pack(side='right', padx=3)
        self.log_widget = ScrolledText(wrap, wrap='word', height=9, state='disabled', font=('Consolas', 9))
        self.log_widget.pack(fill='both', expand=True)
        self.log_widget.tag_configure('status', foreground='#144a80')
        footer = ttk.Frame(wrap)
        footer.pack(fill='x', pady=(6, 0))
        self.status_var = tk.StringVar(value='● Остановлено')
        ttk.Label(footer, textvariable=self.status_var).pack(side='left')
        self.monitor_status = tk.StringVar(value='Прослушивание: выкл.')
        ttk.Label(footer, textvariable=self.monitor_status).pack(side='left', padx=15)
        self.meter = ttk.Progressbar(footer, maximum=100, length=130)
        self.meter.pack(side='right')
        self.level_text = tk.StringVar(value='— dBFS')
        ttk.Label(footer, textvariable=self.level_text, width=10).pack(side='right', padx=3)
        ttk.Label(footer, text='Уровень:').pack(side='right', padx=4)

    def _make_button_icon(self, shape: str, color: str) -> tk.PhotoImage:
        """Rec circle and Stop square occupy the same 12×12 pixel area."""
        icon = tk.PhotoImage(width=18, height=18, master=self)
        for y in range(3, 15):
            for x in range(3, 15):
                if shape == 'square' or (x - 8.5) ** 2 + (y - 8.5) ** 2 <= 6 ** 2:
                    icon.put(color, (x, y))
        return icon

    def _paint_recording(self, active: bool):
        self.rec_button.configure(image=self.rec_live_icon if active else self.rec_idle_icon)

    def _paint_monitor(self, active: bool):
        # Keep the Play symbol in both states. Green means monitor enabled.
        self.playbtn.configure(text='▶', fg='#137a34' if active else '#303030',
                               relief='sunken' if active else 'raised')

    def _toggle_monitor(self):
        if self.monitor.running:
            self.monitor.stop()
            self._paint_monitor(False)
            self.monitor_status.set('Прослушивание: выключается')
            return
        try:
            # Resolve legacy DirectShow GUIDs only for monitoring; NEVER
            # invoke self._start(), metadata.start() or Recorder.start().
            devices = list_audio_devices(ffmpeg_path()) if (self.cfg.get('source_kind') == 'device'
                      and not self.cfg.get('source_label') and ffmpeg_path()) else []
            self.monitor_status.set('Прослушивание: подключение')
            self.monitor.start(self.cfg, ffmpeg_path(), devices)
            self._paint_monitor(True)
        except Exception as exc:
            self._paint_monitor(False)
            self.monitor_status.set('Прослушивание: ошибка')
            self._log('Не удалось включить прослушивание: ' + str(exc))
            messagebox.showerror('Прослушивание', str(exc), parent=self)

    def _reload_settings(self):
        c = self.cfg
        source = (friendly_device_name(c.get('source_label') or c['source'])
                  if c['source_kind'] == 'device' else c['stream_url'])
        self.source_title.set(('Источник: ' + source)[:85])
        active_channels = (self.recorder.cfg.get('_capture_channels', c['channels'])
                           if self.recorder.running else c['channels'])
        self.codec_title.set(f"Кодек: MP3 {c['bitrate']} kbps " + ('Моно' if active_channels==1 else 'Стерео') + f" {c['sample_rate']} Hz")
        self._update_disk()

    def _update_disk(self):
        try:
            folder = Path(self.cfg['folder'])
            folder.mkdir(parents=True, exist_ok=True)
            free = shutil.disk_usage(folder).free
            kbps = max(1, int(self.cfg['bitrate']))
            days = free / (kbps * 1000 / 8) / 86400
            self.disk_title.set(f'Осталось на диске: примерно {days:.1f} дней')
        except OSError:
            self.disk_title.set('Осталось на диске: нет данных')

    def _log(self, message):
        try:
            self.diag_logger.info('%s', str(message))
        except Exception:
            pass  # Never let log I/O interrupt an ongoing recording.
        s = datetime.now().strftime('[%Y-%m-%d %H:%M:%S] ') + str(message)
        self.log_widget.configure(state='normal')
        self.log_widget.insert('end', s + '\n', 'status')
        self.log_widget.see('end')
        # Avoid unlimited growth on 24/7 operation.
        lines = int(self.log_widget.index('end-1c').split('.')[0])
        if lines > 1300:
            self.log_widget.delete('1.0', '300.0')
        self.log_widget.configure(state='disabled')

    def _silence_alert(self):
        if not self.cfg.get('silence_enabled'):
            return
        if self.cfg.get('silence_email_only_recording', True) and not self.recorder.running:
            return
        def notify():
            try:
                send_silence_email(self.cfg)
                self.events.put(('log', 'Письмо о тишине отправлено'))
            except Exception as exc:
                self.events.put(('log', 'Ошибка email-уведомления: ' + str(exc)))
        run_quiet_thread(notify, self.events)

    def _on_metadata(self, event):
        self.recorder.add_event(event)
        self.events.put(('track', event.title))

    def _drain(self):
        try:
            for _ in range(200):
                what, value = self.events.get_nowait()
                if what == 'log':
                    self._log(value)
                elif what == 'track':
                    self._log('CUE: ' + value)
                elif what == 'elapsed':
                    secs = int(value)
                    self.elapsed.set(f'{secs//3600}:{(secs//60)%60:02d}:{secs%60:02d}')
                elif what == 'meter':
                    if not self.monitor.running:
                        self._show_level(value)
                elif what == 'monitor_meter':
                    if self.monitor.running:
                        self._show_level(value)
                elif what == 'devices':
                    window, devs = value
                    try:
                        window._devices_ready(devs)
                    except tk.TclError:
                        pass
                elif what == 'playback_devices':
                    window, outputs, inputs, error = value
                    try:
                        window._playback_devices_ready(outputs, inputs, error)
                    except tk.TclError:
                        pass
                elif what == 'startup_save_done':
                    window, new_settings, error = value
                    try:
                        if window.winfo_exists():
                            window._finish_save(new_settings, error)
                    except tk.TclError:
                        pass
                elif what == 'monitor_state':
                    active, msg = value
                    self.monitor_active = active
                    # Stale stop events can arrive after a rapid re-enable.
                    if active or not self.monitor.running:
                        self._paint_monitor(active)
                        self.monitor_status.set('Прослушивание: вкл.' if active else 'Прослушивание: выкл.')
                    if not active and not self.recorder.running:
                        self._show_level(-60.0)
                    self._log(msg)
        except queue.Empty:
            pass
        if self.winfo_exists():
            self.after(150, self._drain)

    def _show_level(self, db: float) -> None:
        self._current_db = db
        self.meter['value'] = max(0, min(100, (db + 60) * 100 / 60))
        self.level_text.set(f'{db:.1f} dBFS')

    def _start(self, automatic: bool = False):
        if self.recorder.running:
            return
        if not automatic:
            # A deliberate Rec cancels the manual pause and can start now.
            self.manual_pause_until = None
            self.auto_retry_after = None
        try:
            self.recorder.start(self.cfg, self.metadata.last_title or '')
            self._reload_settings()  # show mono when a stereo input fell back to one channel
            c = self.cfg
            self.metadata.start(c.get('metadata_mode', 'none'),
                c['metadata_api_url'] if c.get('metadata_mode') == 'api' else c['metadata_icy_url'],
                c.get('metadata_password', ''), c.get('metadata_username', ''))
            self.status_var.set('● Запись идёт')
            self._paint_recording(True)
        except Exception as exc:
            self._log('Ошибка запуска: ' + str(exc))
            if automatic:
                # No error dialog every heartbeat while no sound card is set.
                self.auto_retry_after = datetime.now() + timedelta(seconds=30)
                self.status_var.set('● Ошибка автозаписи — повтор через 30 сек')
            else:
                messagebox.showerror('Не удалось начать запись', str(exc), parent=self)

    def _stop(self):
        self.metadata.stop()
        self.recorder.stop()
        self._reload_settings()
        self.status_var.set('● Остановлено')
        self._paint_recording(False)
        if not self.monitor.running:
            self._show_level(-60.0)
        self.elapsed.set('0:00:00')

    def _stop_manual(self):
        if self.auto.get():
            # Do NOT switch Auto off. Suppress automatic start until next HH:00.
            self.manual_pause_until = next_clock_hour(datetime.now())
            self._log('Stop: Авто остаётся включённым; следующая попытка записи с '
                      + self.manual_pause_until.strftime('%d.%m %H:%M') + ' по расписанию')
        self._stop()
        if self.auto.get() and self.manual_pause_until:
            self.status_var.set('● Пауза до ' + self.manual_pause_until.strftime('%H:%M') + ' (Авто)')

    def _toggle_auto(self):
        self.cfg['auto_schedule'] = self.auto.get()
        save_config(self.cfg)
        if self.auto.get():
            # Explicitly re-enabling Auto means resume following schedule now.
            self.manual_pause_until = None
            self.auto_retry_after = None
            self._log('Автоматическая запись включена')
            self._schedule_check()
        else:
            self.manual_pause_until = None
            self._log('Автоматическая запись выключена')

    def _schedule_check(self, now: datetime | None = None):
        if not self.auto.get():
            return
        now = now or datetime.now()
        needed = is_schedule_active(self.cfg, now)
        if not needed and self.recorder.running:
            self._log('Планировщик: время записи закончилось')
            self._stop()
        elif (not self.recorder.running and
              auto_should_start(self.cfg, now, self.manual_pause_until) and
              (self.auto_retry_after is None or now >= self.auto_retry_after)):
            self.manual_pause_until = None
            self.auto_retry_after = None
            self._start(automatic=True)

    def _heartbeat(self):
        if not self.winfo_exists():
            return
        self._schedule_check()
        # Do not claim recording solely on running flag if ffmpeg has exited.
        proc = self.recorder.process
        if self.recorder.running and (proc is None or proc.poll() is not None):
            self.status_var.set('● Переподключение')
            self._paint_recording(False)
        elif self.recorder.running:
            self.status_var.set('● Запись идёт')
            self._paint_recording(True)
        else:
            if self.auto.get() and self.manual_pause_until and datetime.now() < self.manual_pause_until:
                self.status_var.set('● Пауза до ' + self.manual_pause_until.strftime('%H:%M') + ' (Авто)')
            else:
                self.status_var.set('● Остановлено')
            self._paint_recording(False)
        self.after(1500, self._heartbeat)

    def _settings(self):
        SettingsWindow(self)

    def _scheduler(self):
        ScheduleWindow(self)

    def _open_folder(self):
        folder = Path(self.cfg['folder'])
        folder.mkdir(parents=True, exist_ok=True)
        if sys.platform == 'win32':
            os.startfile(str(folder))
        else:
            messagebox.showinfo('Папка записей', str(folder))

    def _archive(self):
        window = tk.Toplevel(self)
        apply_app_icon(window)
        window.title('Архив — RadioRecorder')
        window.geometry('650x450')
        window.resizable(False, False)
        ttk.Label(window, text=f"Записи: {self.cfg['folder']}", padding=7).pack(anchor='w')
        rows = ttk.Frame(window, padding=6)
        rows.pack(fill='both', expand=True)
        listing = tk.Listbox(rows, selectmode='browse', font=('Consolas', 10))
        listing.pack(side='left', fill='both', expand=True)
        scroll = ttk.Scrollbar(rows, orient='vertical', command=listing.yview)
        scroll.pack(side='right', fill='y')
        listing.config(yscrollcommand=scroll.set)
        def refresh():
            # The archive is just an on-demand directory listing. Do not
            # keep a second list of MP3 paths in application memory.
            listing.delete(0, 'end')
            for item in sorted(Path(self.cfg['folder']).glob('*.mp3'), reverse=True):
                try:
                    size = item.stat().st_size / 1024 / 1024
                except OSError:
                    size = 0
                listing.insert('end', f'{item.name:<45} {size:>7.1f} МБ' + ('  [CUE]' if item.with_suffix('.cue').exists() else ''))
        def open_selected(cue=False):
            if not listing.curselection():
                return
            line = listing.get(listing.curselection()[0])
            # Last '.mp3' is the actual extension; the size/CUE decoration
            # has no .mp3 substring. Do not cache file Path objects.
            end = line.lower().rfind('.mp3')
            if end < 0:
                return
            item = Path(self.cfg['folder']) / line[:end + 4]
            if cue:
                item = item.with_suffix('.cue')
                if not item.exists():
                    messagebox.showwarning('Архив', 'CUE не найден', parent=window)
                    return
            if sys.platform == 'win32':
                os.startfile(str(item))
            else:
                messagebox.showinfo('Файл', str(item), parent=window)
        buttons = ttk.Frame(window, padding=7)
        buttons.pack(fill='x')
        ttk.Button(buttons, text='Воспроизвести MP3', command=open_selected).pack(side='left', padx=4)
        ttk.Button(buttons, text='Открыть CUE', command=lambda: open_selected(True)).pack(side='left', padx=4)
        ttk.Button(buttons, text='Обновить', command=refresh).pack(side='left', padx=4)
        ttk.Button(buttons, text='Открыть папку', command=self._open_folder).pack(side='right', padx=4)
        listing.bind('<Double-Button-1>', lambda e: open_selected())
        refresh()

    def _on_close(self):
        # The confirmation preference must apply to ANY exit, even if the
        # recorder is currently stopped. Recording only changes the wording.
        if self.cfg.get('ask_on_exit', True):
            prompt = ('Остановить запись и закрыть RadioRecorder?'
                      if self.recorder.running else 'Закрыть RadioRecorder?')
            if not messagebox.askyesno('RadioRecorder', prompt, parent=self):
                return
        self.auto.set(False)
        self.monitor.close()
        self.metadata.stop()
        self.recorder.stop()
        windows_keep_awake(False)
        self.destroy()


def main():
    # Acquire the lock before constructing Tk, starting capture or loading a
    # saved Auto schedule. Prevent duplicate FFmpeg recorders and CUE writers.
    with SingleInstanceGuard() as guard:
        if not guard.acquire():
            show_existing_window(TITLE)
            return
        configure_windows_app_id()
        app = RadioRecorderUI()
        app.mainloop()


if __name__ == '__main__':
    main()
