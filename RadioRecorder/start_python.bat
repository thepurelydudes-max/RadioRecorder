@echo off
cd /d "%~dp0"
where pythonw >nul 2>nul
if errorlevel 1 (
    echo Python не найден. Используйте сборку RadioRecorder.exe из GitHub Actions.
    pause
    exit /b 1
)
start "" pythonw "%~dp0app.py"
