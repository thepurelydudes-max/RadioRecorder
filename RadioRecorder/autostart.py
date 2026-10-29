"""Optional Windows login startup, including highest-rights scheduled task.

Elevated task registration requests UAC once. Future interactive logins
run the task without another consent dialog. No global UAC setting changes.
"""
from __future__ import annotations

import csv
import io
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

TASK_NAME = 'RadioRecorder'
REG_RUN = r'Software\Microsoft\Windows\CurrentVersion\Run'
NS = 'http://schemas.microsoft.com/windows/2004/02/mit/task'


def app_command(autostart: bool = False) -> tuple[str, str]:
    marker = ' --autostart' if autostart else ''
    if getattr(sys, 'frozen', False):
        return str(Path(sys.executable).resolve()), marker.strip()
    pythonw = Path(sys.executable).with_name('pythonw.exe')
    if not pythonw.exists():
        pythonw = Path(sys.executable)
    return str(pythonw), '"' + str(Path(__file__).with_name('app.py').resolve()) + '"' + marker


def current_user_sid() -> str:
    flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    result = subprocess.run(['whoami', '/user', '/fo', 'csv', '/nh'],
                            text=True, capture_output=True, check=True,
                            timeout=12, creationflags=flags)
    rows = list(csv.reader(io.StringIO(result.stdout)))
    if not rows or len(rows[0]) < 2 or not re.fullmatch(r'S-1-5-(?:\d+-)+\d+', rows[0][-1]):
        raise RuntimeError('Не удалось определить SID пользователя Windows')
    return rows[0][-1]


def make_task_xml(sid: str, exe: str, args: str = '') -> bytes:
    """Task Scheduler 2.0 interactive-token task with elevated run level."""
    ET.register_namespace('', NS)
    def child(parent, tag, value=None, **attrs):
        node = ET.SubElement(parent, f'{{{NS}}}{tag}', attrs)
        if value is not None:
            node.text = value
        return node
    root = ET.Element(f'{{{NS}}}Task', {'version': '1.2'})
    triggers = child(root, 'Triggers')
    trigger = child(triggers, 'LogonTrigger')
    child(trigger, 'Enabled', 'true')
    child(trigger, 'UserId', sid)
    principals = child(root, 'Principals')
    principal = child(principals, 'Principal', id='Author')
    child(principal, 'UserId', sid)
    child(principal, 'LogonType', 'InteractiveToken')
    child(principal, 'RunLevel', 'HighestAvailable')
    settings = child(root, 'Settings')
    child(settings, 'MultipleInstancesPolicy', 'IgnoreNew')
    child(settings, 'DisallowStartIfOnBatteries', 'false')
    child(settings, 'StopIfGoingOnBatteries', 'false')
    child(settings, 'ExecutionTimeLimit', 'PT0S')
    child(settings, 'AllowHardTerminate', 'true')
    child(settings, 'Enabled', 'true')
    child(settings, 'StartWhenAvailable', 'true')
    actions = child(root, 'Actions', Context='Author')
    action = child(actions, 'Exec')
    child(action, 'Command', exe)
    if args:
        child(action, 'Arguments', args)
    child(action, 'WorkingDirectory', str(Path(exe).parent))
    return ET.tostring(root, encoding='utf-16', xml_declaration=True)


def _elevated_schtasks(action: str, xml_file: str | None = None) -> None:
    """Start a single elevated Task Scheduler change and check its exit code."""
    if action == 'create':
        args = f'/Create /TN "{TASK_NAME}" /XML "{xml_file}" /F'
    else:
        args = f'/Delete /TN "{TASK_NAME}" /F'
    # The elevated child inherits its launcher's interactive desktop.
    escaped = args.replace("'", "''")
    script = ("$ErrorActionPreference='Stop'; "
              f"$p=Start-Process -FilePath 'schtasks.exe' -ArgumentList '{escaped}' "
              "-Verb RunAs -Wait -PassThru; exit $p.ExitCode")
    flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    result = subprocess.run(['powershell.exe', '-NoProfile', '-Command', script],
                            capture_output=True, text=True, timeout=120,
                            creationflags=flags)
    if result.returncode != 0:
        raise RuntimeError('Не удалось настроить повышенный автозапуск. Подтверди UAC. '
                           + (result.stderr or result.stdout)[-400:])


def _set_registry(enabled: bool) -> None:
    import winreg
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, REG_RUN, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            exe, args = app_command(autostart=True)
            command = '"' + exe + '"' + (' ' + args if args else '')
            winreg.SetValueEx(key, TASK_NAME, 0, winreg.REG_SZ, command)
        else:
            try:
                winreg.DeleteValue(key, TASK_NAME)
            except FileNotFoundError:
                pass


def normalize_startup_settings(enabled: bool, elevated: bool) -> tuple[bool, bool]:
    """An admin login task cannot remain checked if login startup is off."""
    enabled = bool(enabled)
    return enabled, bool(elevated) if enabled else False


def _task_exists() -> bool:
    """Look for an old scheduled task even if settings.json says it is off."""
    if sys.platform != 'win32':
        return False
    flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    check = subprocess.run(['schtasks.exe', '/Query', '/TN', TASK_NAME],
                           capture_output=True, text=True, errors='replace',
                           timeout=12, creationflags=flags)
    return check.returncode == 0


def configure_login(enabled: bool, elevated: bool, previous_elevated: bool = False) -> None:
    """Synchronize both startup registrations with the checkbox state.

    Always remove the old scheduled task when switching to normal startup
    or disabling autostart, including stale tasks from older program versions.
    Do not ask for UAC when there is no task to remove.
    """
    if sys.platform != 'win32':
        return
    enabled, elevated = normalize_startup_settings(enabled, elevated)
    if enabled and elevated:
        exe, args = app_command(autostart=True)
        sid = current_user_sid()
        file = None
        try:
            with tempfile.NamedTemporaryFile(suffix='.xml', prefix='RadioRecorder-',
                                             delete=False) as f:
                file = f.name
                f.write(make_task_xml(sid, exe, args))
            _elevated_schtasks('create', file)
        finally:
            if file:
                Path(file).unlink(missing_ok=True)
        _set_registry(False)  # prevents a second unelevated launch
    else:
        if _task_exists():
            _elevated_schtasks('delete')
        _set_registry(enabled)
