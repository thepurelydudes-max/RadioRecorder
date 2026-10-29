# RadioRecorder

Radio-air recording application for Windows. Two independent editions:

- **Python v0.6.2 (Tkinter, current):** [editable source code](RadioRecorder/) · [Russian quick start](RadioRecorder/QUICKSTART_RU.md) · [tests](RadioRecorder/tests/).
- **Native 1.2 (C# / WinForms, experimental):** source archive `RadioRecorder-Native-1.2-source.zip`.

## Current Python source for Codex

**Use [RadioRecorder/](RadioRecorder/) as the Python project root.** The old `RadioRecorder-Python-0.5.0-source.zip` at repository root is a historical archive, **not** the current source.

Python v0.6.2 uses the selected local Windows input for MP3 recording and RadioBOSS HTTP API or ICY exclusively for track metadata. It creates hourly MP3/CUE files and appends the same hour after Stop/Rec or recovery, without session suffixes. It manages a recording schedule and includes file-retention and Windows-startup features.

### Tests

```bash
cd RadioRecorder
python -m pip install -r requirements.txt
python -m unittest discover -s tests -q
```

### Windows EXE

[GitHub Actions: build Python Windows x64](https://github.com/thepurelydudes-max/RadioRecorder/actions/workflows/windows-python.yml) runs when `RadioRecorder/**` source changes and can also be started manually with *Run workflow*. Once complete, download the `RadioRecorder-Python-Windows-x64` artifact.

The EXE distribution is a directory including `_internal` and `ffmpeg.exe`. Do not deploy the EXE without its accompanying dependencies.

### Engineering safeguards

Do not change the existing Tkinter UI layout without an explicit request. Keep audio recording independent of the metadata network. Preserve hourly MP3/CUE alignment, prevent deletion of unrelated archive files, and run the tests after each change.

**Hardware qualification:** Passing tests is not a substitute for testing with actual Windows input devices and running a long-duration recording.

### Native Windows edition

[Native Windows build](https://github.com/thepurelydudes-max/RadioRecorder/actions/workflows/windows-native.yml). Native source is currently retained as a separate ZIP and is not the Python project used by Codex.
