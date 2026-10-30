# Codex development guide — RadioRecorder Python v0.6.4

This directory contains the **active Python project**. Ignore the historical v0.5.0 source ZIP at the repository root. The Native C# edition is a separate experiment.

## Validate every change

- Run: `python -m unittest discover -s tests -q` from this directory.
- Keep compatibility with Python 3.12 on Windows and the GitHub Actions Windows build.
- Test edge cases for hourly transitions, FFmpeg failures, device disconnects, metadata delays, and disk write errors. Do not claim hardware validation from mocked unit tests.

## Critical program behavior

- Capture **audio only** from the user-selected Windows input device through FFmpeg/PortAudio/DirectShow.
- RadioBOSS HTTP API or ICY supplies **metadata only**; network failure should not interrupt local audio recording.
- The recording engine handles only the **current hour's** CUE metadata. Never rewrite old CUE files simply because current metadata changes.
- Hourly MP3 segments should not require stopping/restarting the input capture.
- In hourly split mode, exactly one MP3/CUE pair per wall-clock interval: safely APPEND complete MP3 frames to an existing current-hour MP3 after manual or automatic restart. Never use `_2`, `_3` for hourly sessions. Never truncate valid existing audio or overwrite a malformed/inconsistent CUE; fail safely instead. Preserve legacy single-file collision protection when splitting is disabled.
- FFmpeg should encode continuously into a pipe in split mode; the recorder's MP3 frame writer must not restart the capture at hour boundaries. Preserve prior-hour MP3 and CUE after rollover, and keep resumed CUE indexes aligned to cumulative recorded audio time (not missing wall-clock time).
- File retention must operate only on eligible MP3/CUE files; do not delete unrelated formats. Default retention is 90 days, but preserve existing user settings.
- Preserve the single-instance protection and Windows login scheduling.
- Preserve all current Tkinter window sizes, widgets, and visual layout unless the task explicitly requests a UI change.
- Keep device labels short and human-friendly in settings, and show only the device name in the main window.
- Persistent settings belong under `%LOCALAPPDATA%\RadioRecorder`, not beside the executable.

## Building

The repo-level `.github/workflows/windows-python.yml` builds this folder on Windows and uploads a portable EXE distribution including `_internal` and FFmpeg. The standard Python tests run before packaging.
