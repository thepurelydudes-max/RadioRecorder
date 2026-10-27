# RadioRecorder

Windows radio-air logger. Two independent editions:

- **Native 1.2 (C# / WinForms):** Windows input audio → MP3 through FFmpeg; RadioBOSS HTTP API or ICY metadata → matching hourly CUE. Native .NET EXE + DLLs, no Python.
- **Python 0.5 (Tkinter):** continues the original working Python 0.4.1 codebase, preserving DirectShow input recording while adding PortAudio input fallback, Play signal meter, icons and elevated Windows login startup.

## Windows downloads (GitHub Actions)

1. [Native build — successfully completed](https://github.com/thepurelydudes-max/RadioRecorder/actions/runs/40493785636) → **Artifacts** → `RadioRecorder-Native-Windows-x64`.
2. [Python build](https://github.com/thepurelydudes-max/RadioRecorder/actions/workflows/windows-python.yml) → upload the file `RadioRecorder-Python-0.5.0-source.zip` into the repository root using **Add file → Upload files**. This triggers its own Windows build; after success download `RadioRecorder-Python-Windows-x64`.

Native source currently is in `RadioRecorder-Native-1.2-source.zip`. Two compile issues in that ZIP are automatically patched inside the Native GitHub build workflow. Updated corrected source is available as v1.2.1 upon request.

Both versions need the complete extracted distribution folder, including FFmpeg. Audio from the selected Windows input is independent of RadioBOSS HTTP metadata. A network outage should not stop local MP3 capture. Before using the application continuously in a broadcast chain, verify capture, device reconnect, CUE alignment and retention on a real Windows system.

### Automatic startup

The Native and Python code both support login startup with highest available rights via Windows Task Scheduler, requiring UAC consent for initial installation. The saved Auto checkbox starts recording only during allowed scheduled hours. Manual Stop pauses until the next allowed clock hour, without disabling Auto.
