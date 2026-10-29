@echo off
setlocal
cd /d "%~dp0"
echo === RadioRecorder Windows build ===
where py >nul 2>nul
if errorlevel 1 (
  echo Python launcher 'py' ne naiden. Ustanovi Python 3.11+.
  pause
  exit /b 1
)
py -m pip install --upgrade pip
py -m pip install -r requirements.txt pyinstaller
py build_windows.py
if errorlevel 1 (
  echo.
  echo Sborshchik vernul oshibku.
  pause
  exit /b 1
)
echo.
echo Gotovo. Papka so sborkoi: dist\RadioRecorder\
pause
