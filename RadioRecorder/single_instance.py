"""Windows session-scoped single-instance guard.

The kernel releases a named mutex if a process crashes, so stale lock files
cannot prevent the next RadioRecorder launch. No elevated privileges needed.
"""
from __future__ import annotations

import ctypes
import sys

MUTEX_NAME = r'Local\FARLINER.RadioRecorder.SingleInstance'
ERROR_ALREADY_EXISTS = 183


class SingleInstanceGuard:
    def __init__(self, kernel=None, last_error=None, windows=None):
        self.windows = sys.platform == 'win32' if windows is None else bool(windows)
        self.kernel = kernel
        self.last_error = last_error
        self.handle = None

    def acquire(self) -> bool:
        if not self.windows:
            return True
        if self.handle is not None:
            return True
        kernel = self.kernel or ctypes.WinDLL('kernel32', use_last_error=True)
        if self.kernel is None:
            kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
            kernel.CreateMutexW.restype = ctypes.c_void_p
            kernel.CloseHandle.argtypes = [ctypes.c_void_p]
            kernel.CloseHandle.restype = ctypes.c_bool
        handle = kernel.CreateMutexW(None, False, MUTEX_NAME)
        # Read GetLastError immediately; other system calls can overwrite it.
        code = self.last_error() if self.last_error else ctypes.get_last_error()
        if not handle:
            raise OSError(code or 1, 'Не удалось создать блокировку экземпляра RadioRecorder')
        if code == ERROR_ALREADY_EXISTS:
            kernel.CloseHandle(handle)
            return False
        self.kernel = kernel
        self.handle = handle
        return True

    def close(self) -> None:
        if self.handle is not None:
            try:
                self.kernel.CloseHandle(self.handle)
            finally:
                self.handle = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def show_existing_window(window_title: str) -> bool:
    """Best effort: restore first instance instead of launching a second GUI."""
    if sys.platform != 'win32':
        return False
    try:
        api = ctypes.WinDLL('user32', use_last_error=True)
        api.FindWindowW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p]
        api.FindWindowW.restype = ctypes.c_void_p
        api.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
        api.SetForegroundWindow.argtypes = [ctypes.c_void_p]
        hwnd = api.FindWindowW(None, window_title)
        if not hwnd:
            return False
        api.ShowWindow(hwnd, 9)  # SW_RESTORE
        api.SetForegroundWindow(hwnd)
        return True
    except (OSError, AttributeError):
        return False
