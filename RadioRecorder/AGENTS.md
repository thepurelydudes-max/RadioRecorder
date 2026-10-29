# Codex development guide — RadioRecorder Python v0.6.1

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
- Never overwrite an existing MP3/CUE pair; use safe session suffixes such as `_2`, `_3` when necessary.
- File retention must operate only on eligible MP3/CUE files; do not delete unrelated formats. Default retention is 90 days, but preserve existing user settings.
- Preserve the single-instance protection and Windows login scheduling.
- Preserve all current Tkinter window sizes, widgets, and visual layout unless the task explicitly requests a UI change.
- Keep device labels short and human-friendly in settings, and show only the device name in the main window.
- Persistent settings belong under `%LOCALAPPDATA%\RadioRecorder`, not beside the executable.

## Building

The repo-level `.github/workflows/windows-python.yml` builds this folder on Windows and uploads a portable EXE distribution including `_internal` and FFmpeg. The standard Python tests run before packaging.
