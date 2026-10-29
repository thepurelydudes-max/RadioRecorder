from __future__ import annotations
import os
import shutil
from pathlib import Path
import PyInstaller.__main__

ROOT = Path(__file__).resolve().parent
DIST = ROOT / 'dist'
BUILD = ROOT / 'build'
APP_NAME = 'RadioRecorder'

args = [
    str(ROOT / 'app.py'),
    '--noconfirm',
    '--clean',
    '--name', APP_NAME,
    '--windowed',
    '--collect-all', '_sounddevice_data',
    '--onedir',
    '--icon', str(ROOT / 'assets' / 'radiorecorder.ico'),
    '--add-data', f'{ROOT / "assets"}{";" if os.name == "nt" else ":"}assets',
]
# FFmpeg must be beside the actual EXE, not hidden inside PyInstaller's
# `_internal` directory where engine.ffmpeg_path() would not find it.
PyInstaller.__main__.run(args)
ffmpeg = ROOT / 'ffmpeg.exe'
if ffmpeg.exists():
    shutil.copy2(ffmpeg, DIST / APP_NAME / 'ffmpeg.exe')
