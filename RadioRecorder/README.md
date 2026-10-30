## RadioRecorder Python 0.6.5 — надёжная длительная запись

- **MP3 без лишних сбросов:** запись кодированных кадров в буфер, сброс в Windows примерно каждые 500 мс либо при накоплении 32 КиБ. При завершении часа/останова — полный сброс и синхронизация файла (`fsync`). Настройки аудиовхода 128–2048 сэмплов не связаны с буфером записи на диск.

- **Безопасная автоочистка:** удаляются только старые `.mp3`/`.cue` в папке архива (включая расширения в верхнем регистре), без удаления `.txt`, `.json`, плейлистов и папок. Минимальный срок 90 дней; уже сохранённые значения не изменяются.
- **Запись по часам:** при включённом почасовом разделении существующий MP3 текущего часа продолжается после перезапуска; следующий час получает новый файл без `_2`. Если разделение отключено, коллизии имён обрабатываются отдельно.
- **Потоки ICY/API:** старый запрос получает свой собственный сигнал остановки; новый сетевой поток начнёт работу только после завершения предыдущего. GUI не блокируется ожиданием HTTP.
- **Windows-аудио:** после переподключения USB тот же вход определяется по **имени устройства + реальному типу аудиоинтерфейса**, даже если индекс PortAudio изменился. При неоднозначном совпадении требуется ручной выбор.
- **Активный CUE:** существующий файл не перезаписывается и не обрезается; новые TRACK добавляются в конец. Ошибка повреждённого CUE не должна останавливать MP3.
- **Отказы Windows:** регистрация автозапуска с правами администратора выполняется в отдельном потоке, ошибки показываются пользователю и не подвешивают GUI. Оригинальный `settings.json` не заменяется при неуспешной регистрации.
- **Защита:** предупреждение и остановка записи при менее 256 МиБ свободного места, журнал диагностики `radiorecorder.log` (ротация, в папке пользовательских настроек) и попытка привязать FFmpeg к Windows Job Object, чтобы кодировщик завершался при аварийном выходе RadioRecorder. Если Windows откажет в привязке Job Object, запись продолжится с диагностическим предупреждением.

**Ограничения:** подстановка индивидуальных суффиксов для каждого отдельного часа без разрыва захвата не поддерживается штатным FFmpeg segment muxer; применяется один свободный суффикс на весь текущий сеанс. Если питание отключится в момент записи текстового CUE, самая последняя строка может быть неполной. Автоматическая защита не заменяет резервного копирования. Тесты выполнялись без физических аудиоустройств Windows: реальная длительная запись требует отдельной проверки.

## RadioRecorder Python 0.6.0 — только текущий MP3/CUE в памяти

- В ходе записи Recorder хранит только **один активный сегмент MP3** (его путь, без аудиоданных), метаданные текущего CUE (максимум 98 событий) и ограниченный временный буфер метаданных непосредственно у границы следующего часа. При появлении нового MP3 предыдущий CUE больше **никогда не перезаписывается**, его события и ссылка на старый сегмент удаляются из оперативного состояния рекордера.
- Текущий CUE создаётся одновременно с обнаружением текущего MP3 и обновляется **в том же файле**, без `os.replace`: исходная дата создания в Windows сохраняется; меняется только дата изменения активного CUE. После начала нового часа старые CUE остаются без изменений.
- Отказ от `known_paths` (который раньше перечитывал весь архив MP3 и хранил полный список путей). Новый сегмент определяется по ожидаемому имени файла FFmpeg, включая имя начатой посреди часа записи и новый суффикс `_2`, `_3` при перезапуске кодировщика.
- Просмотр архива открывается по запросу пользователя. Он **не загружает аудио или содержимое CUE** — только считывает имена файлов и размеры из папки; закрытие окна освобождает список. Автоочистка по-прежнему проверяет файлы на диске раз в сутки без сохранения каталога в памяти.
- Опоздавшие API-события предыдущего часа не исправляют закрытые CUE. Если RadioBOSS отдаёт позже важные метаданные за уже завершённый час, они намеренно не попадут в тот CUE. События, которые приходят незадолго до появления нового MP3 на границе часа, временно удерживаются в небольшом буфере и включаются только в новый CUE.
- Запись MP3 и её деление по часам выполняет FFmpeg; отдельный мониторинг Play, ICY/API, планировщик, параметры и внешний вид интерфейса сохранены.

## RadioRecorder Python 0.5.9 — шаблон имени и автозапуск

- Поле **Формат имени файла** теперь показывает **живой пример** результата при каждом изменении шаблона. Допускается перестановка `%d` (день), `%m` (месяц), `%y` (полный год), `%hh` (часы), `%mm` (минуты), `%ss` (секунды). Например `%y%m%d-%hh%mm%ss` → `20261027-112345.mp3`. Для CUE используется тот же базовый файл.
- Исходный шаблон **`%d-%m-%y_%hh-%mm`** остаётся значением по умолчанию. Проверка некорректных шаблонов работает до сохранения.
- В **настройках** галочка называется **«Сворачивать при автозапуске»**. Сворачивание действует только при запуске с Windows с аргументом `--autostart`, который добавляется в задание Планировщика или запись Run. Ручной двойной щелчок всегда открывает окно. Уже существовавший автозапуск из более ранних версий нужно **однократно переоформить через настройки** (выключить «Запускать с Windows», сохранить, снова включить и сохранить), чтобы обновить команду. Повышенный автозапуск при повторном включении потребует согласия UAC для создания задания.
- Уменьшена только ширина полей и правого края окна **Настройки**; разделы, планировщик, главное окно и их расположение не менялись. Перемещение или изменение размеров окон мышью по-прежнему запрещено, где это было предусмотрено.
- **Новая установка**: 90 дней автоочистки. Сохранённые значения (например, 365 дней) **не заменяются** на 90 при обновлении.

## Python 0.5.7 UI correction

Fixed right-panel overflow in Settings without resizing or rearranging windows; simplified admin startup label and made checkbox selectable; Mono/Stereo display labels with numeric backend channel values; new-install retention default is 90 days. Existing saved user settings are preserved. Original scheduler layout unchanged.

<!-- The Python branch is independent from the native C# branch. -->
# RadioRecorder Python 0.5.6

Portable Windows MP3 radio capture with **wall-clock hourly splitting** (e.g. 12:00, 13:00, independent of when capture started) and RadioBOSS-compatible CUE sheets from **independent** metadata polling.

**Русская инструкция:** [QUICKSTART_RU.md](QUICKSTART_RU.md).

## Development

Runtime uses Python standard library, **sounddevice/PortAudio for monitoring**, and an external `ffmpeg.exe` adjacent to the executable. Development without packaging:

```powershell
py -3.12 -m pip install -r requirements.txt
py -3.12 app.py
```

Install `ffmpeg.exe` in the project directory or put `ffmpeg` on PATH. On Windows the GUI discovers DirectShow audio inputs via `ffmpeg -list_devices true -f dshow -i dummy`.

### Tests

```powershell
py -3.12 -m unittest discover -s tests -v
```

### Live monitoring (v0.3)

The main window's **▶** button toggles preview on the next press, and stays ▶ (green while on). It never records. It starts/stops preview of the selected *recording source* without starting/stopping MP3 recording. **Rec** is separate and shows a bright red round indicator while FFmpeg records; the Stop button has a 12px square (both icons share the same bounds). Output selection is in Settings → Воспроизведение (speaker/headphones). When recording from a Windows line input, the app opens its PortAudio input in shared mode and plays it through the selected output (no new FFmpeg recorder). If automatic DirectShow-to-PortAudio name matching fails, choose an explicit monitoring input there. For HTTP audio input, preview decodes the HTTP stream via FFmpeg. Monitoring may not work for devices exclusively locked by Windows/another application; it reports an error without stopping MP3 recording.

**Note:** monitoring the selected *local line input* is distinct from using the RadioBOSS ICY endpoint **for metadata only**. If you want to hear the broadcast stream itself, choose HTTP as the recording input; do not confuse it with the ICY metadata URL.

### Persistent Auto mode (v0.4.1)

Manual **Stop** ends only the current audio session, not the checked **Auto** mode. The scheduler skips automatic restarts for the remainder of the current wall-clock hour and retries at the next allowed scheduled hour. **Rec** can override this pause. The checkbox setting is persistent: reopening the program resumes automatic recording when the current hour is enabled.

The EXE/taskbar logo now loads packaged resources from PyInstaller's `_MEIPASS` when needed, while app settings and `ffmpeg.exe` stay beside the executable. This is still a bundled Python application; migrating it to native C#/Win32 requires a separate rewrite.

### Automatic daily retention (v0.3)

When MP3 recording is running, the recorder removes **only expired MP3/CUE files directly inside the recordings folder**. It runs once per local date after midnight, or upon starting recording later that day when no cleanup has run. Expiration is based on last modification time with a midnight/day cutoff: at `2026-10-27 00:00`, the minimum 90 days of retention means removing files with timestamps **before `2026-07-29 00:00`**. It does not recurse into folders, follow symlinks, or delete currently open and recently updated MP3/CUE segments. It refuses dangerous folder targets such as the app folder, home folder, and drive roots. Daily execution is persisted in `%LOCALAPPDATA%\RadioRecorder\retention_state.json` (Windows). Changing the retention period while recording applies immediately; changing the recordings folder takes effect on the next recording start.

**Warning:** old MP3/CUE files in the chosen folder will be permanently deleted even if they were created by another recorder. Choose a dedicated recordings directory.

### Portable EXE build on Windows

This source package includes **`build_windows.bat`** and **`build_windows.py`**. On a Windows PC with Python 3.11+ installed:

```powershell
py -m pip install -r requirements.txt pyinstaller
py build_windows.py
```

Or just double-click `build_windows.bat`. The result will be a portable folder `dist\RadioRecorder\` with `RadioRecorder.exe`, `_internal\`, and bundled assets. If `ffmpeg.exe` is placed in the project root before the build, it is copied into the built application automatically.

### Project files

| File | Role |
| --- | --- |
| `app.py` | Windows-style Russian GUI, schedule, archive, settings |
| `engine.py` | DirectShow capture, FFmpeg segmenter, recovery, CUE association |
| `metadata.py` | RadioBOSS HTTP API `getlastplayed`, `playbackinfo`, or ICY parser |
| `cues.py` | UTF-8 BOM / CRLF CUE generator with MM:SS:75fps frame indices |
| `alerts.py` | Optional SMTP warnings for recording silence |
| `monitor.py` | Independent live audio monitoring / PortAudio output selection |
| `requirements.txt` | sounddevice for input/output preview |
| `.github/workflows/windows-build.yml` | Portable ZIP build |
| `tests/test_core.py`, `tests/test_monitor.py` | CUE, filename, retention and monitor toggle tests |

### Data, privacy, operational cautions

- User settings are saved in `%LOCALAPPDATA%\RadioRecorder\settings.json` (Windows), or `~/.config/RadioRecorder/settings.json` otherwise; legacy settings beside the EXE are read when no user profile copy exists; the API and SMTP secrets are stored **unencrypted**. Don't publish it.
- An ICY stream client **must consume** audio bytes to reach embedded titles; those bytes are discarded, not recorded. RadioBOSS HTTP API is metadata-only.
- RadioBOSS recording and Audio Input internals are not used. RadioRecorder independently records the Windows line input. Monitoring is a separate shared-mode capture and requires an available PortAudio device.
- CUE data is a best-effort event timeline using timestamps from RadioBOSS, or times of ICY detection. Precise subsecond sync requires equipment/clock calibration and is *not* guaranteed.
- ffmpeg segment muxing can introduce small frame-boundary differences; use the generated recordings to calibrate delay.
- On unexpected power loss the currently open MP3 may be incomplete, but previously closed hourly MP3 files remain independent. Hardware/input interruptions cannot be magically repaired. The recorder retries if configured.

License for new source code has not been assigned; choose a license when creating your public repository. FFmpeg's licensing is separate (see https://ffmpeg.org/legal.html).


### Python v0.5 updates
- Retains the existing FFmpeg DirectShow path and exposes all PortAudio Windows audio inputs as an alternative PCM capture method.
- Play monitoring produces a real dBFS level meter; recording meter remains independent.
- Optional elevated login autostart via Windows Task Scheduler, with initial UAC confirmation (and UAC when disabling).
- Improved icon propagation to main, settings, schedule and archive windows.
- Audio PCM comes from Windows input; HTTP RadioBOSS supplies metadata to CUE only.

### Python v0.5.1 display-only changes
- The main window displays `Источник: Line In (Realtek Audio)`, without PortAudio, WASAPI, MME, or backend indices.
- Input, output and monitor selections show friendly device names. Duplicate names are numbered `(2)`, `(3)` to keep each choice selectable.
- Backend identifiers in settings remain unchanged. No changes to MP3 capture, CUE collection, monitoring logic, or auto schedule.


## Подписи аудиоустройств (v0.5.2)

В окне **Настройки** показываются названия устройств **вместе с интерфейсом захвата**: `Windows WASAPI`, `MME`, `Windows DirectSound`, `WDM-KS`, `DirectShow` (если он доступен). Для PortAudio дополнительно показывается номер устройства `#N`, чтобы различать одинаковые входы. В **главном окне** в строке «Источник» показывается только название устройства — без обозначения интерфейса. Настоящие идентификаторы записи не менялись.

### Исправление 0.5.3

Если включено «Спрашивать при закрытии», RadioRecorder всегда запрашивает подтверждение: при работающей записи — с предупреждением об остановке; в режиме ожидания — перед выходом. Если галочка снята, закрытие выполняется без запроса.

### Изменения 0.5.4 (совместимость Windows)

- Минимальный срок автоудаления — **90 дней**. Значение меньше 90 отклоняется при сохранении и удалении; старые конфигурации поднимаются до 90 дней. Архив удаляется ежедневно после полуночи, только при активной записи.
- Запись через PortAudio до запуска FFmpeg проверяет доступность входа. Если 2 канала не открываются, автоматически пробуется 1 канал. Если вход не работает на выбранной частоте, используется совместимая входная частота с пересчётом в FFmpeg; это отражается в журнале.
- Play перебирает частоты и варианты моно/стерео, с совместимой задержкой для старых DirectSound/MME. Это не гарантирует совместимость со всеми драйверами; Windows WASAPI остаётся предпочтительным выбором.
- `settings.json` и `retention_state.json` сохраняются атомарно в `%LOCALAPPDATA%\RadioRecorder`, а не в защищённой папке с EXE. Старые настройки считываются при первом запуске после обновления. **Пароли API и SMTP сохраняются в открытом виде — защищай профиль пользователя.**
- Перед включением Rec проверяются разрешения на запись в папку архива. Если доступа нет, показывается понятная ошибка. Повышенные права обычно не нужны для захвата звука, но политика устройства и разрешения Windows могут влиять на доступ.
- Подтверждение закрытия проверяется независимо от того, активна ли запись (как в 0.5.3).

Чтобы проверить на настоящем ПК, запусти запись с моно-входа, Play для DirectSound/MME/WASAPI, разъедини устройство, проверь последующее восстановление и MP3/CUE при смене часа. В среде разработки эти аппаратные сценарии не воспроизводятся.

### Изменения 0.5.5

- «Запускать с Windows» и «Автозапуск с правами администратора» согласованы: снятие основной галочки снимает дополнительную; при сохранении удаляются устаревшие задания Планировщика и автозапуск из реестра.
- Защита от второго экземпляра через системный mutex Windows; повторный запуск пытается показать первое окно, не запуская второй рекордер.
- Новый профиль по умолчанию: ICY и http://localhost:8000/ для URL потока и метаданных. Ранее сохранённые настройки не затираются.
- В v0.5.5 экспериментально изменялись размеры и прокрутка окон. В v0.5.6 эти изменения отменены и восстановлен исходный интерфейс v0.5.4; планировщик по-прежнему без прокрутки.

### Обновление 0.5.6 — восстановление исходного оформления

Восстановлены **точные размеры и структура окон v0.5.4** (без увеличенных окон, внешних полос прокрутки, перераспределения кнопок и нового вида архива):

- Главное окно: **650×385**, изменение размеров запрещено.
- Архив: **650×450**, изменение размеров запрещено; кнопки в один ряд, как раньше.
- Настройки: прежняя двухколоночная компоновка, прежние размеры области и только исходная вертикальная прокрутка, окно фиксировано.
- Планировщик: прежняя сетка 24×7, фиксированное окно, **никакой прокрутки**.

Все функциональные изменения 0.5.5 сохранены: защита от двух копий, управление автозапуском, ICY по умолчанию и настройки записи.
