#!/usr/bin/env python3
"""Keep one DPS client UI active in a Windows user session."""

from __future__ import annotations

import ctypes
import sys
import time
from ctypes import wintypes


ERROR_ALREADY_EXISTS = 183
MUTEX_NAME = "Local\\DaodaoMysteryDpsLogs"
TRAY_CLASS_PREFIX = "GMZZDpsTray_"
WM_APP = 0x8000
WM_LBUTTONUP = 0x0202
WM_TRAY_CALLBACK = WM_APP + 37
TRAY_ICON_ID = 1


class SingleInstanceGuard:
    def __init__(self, name: str = MUTEX_NAME):
        self.name = str(name)
        self.handle = 0
        self.already_running = False

    def acquire(self) -> "SingleInstanceGuard":
        if sys.platform != "win32" or self.handle:
            return self
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_mutex = kernel32.CreateMutexW
        create_mutex.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR)
        create_mutex.restype = wintypes.HANDLE
        ctypes.set_last_error(0)
        handle = int(create_mutex(None, False, self.name) or 0)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        self.already_running = ctypes.get_last_error() == ERROR_ALREADY_EXISTS
        if self.already_running:
            kernel32.CloseHandle(wintypes.HANDLE(handle))
        else:
            self.handle = handle
        return self

    def close(self) -> None:
        handle = self.handle
        self.handle = 0
        if not handle or sys.platform != "win32":
            return
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CloseHandle(wintypes.HANDLE(handle))

    def __enter__(self) -> "SingleInstanceGuard":
        return self.acquire()

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.close()


def activate_existing_instance(attempts: int = 5) -> bool:
    """Ask an existing client's tray window to restore its main window."""
    if sys.platform != "win32":
        return False
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HWND, wintypes.LPARAM
    )
    get_class_name = user32.GetClassNameW
    get_class_name.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
    get_class_name.restype = ctypes.c_int
    post_message = user32.PostMessageW
    post_message.argtypes = (
        wintypes.HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
    )
    post_message.restype = wintypes.BOOL
    maximum_attempts = max(1, int(attempts))

    for attempt in range(maximum_attempts):
        found = False

        @callback_type
        def visit(hwnd, _lparam):
            nonlocal found
            class_name = ctypes.create_unicode_buffer(160)
            if get_class_name(hwnd, class_name, len(class_name)) <= 0:
                return True
            if not class_name.value.startswith(TRAY_CLASS_PREFIX):
                return True
            found = bool(
                post_message(
                    hwnd,
                    WM_TRAY_CALLBACK,
                    TRAY_ICON_ID,
                    WM_LBUTTONUP,
                )
            )
            return not found

        user32.EnumWindows(visit, 0)
        if found:
            return True
        if attempt + 1 < maximum_attempts:
            time.sleep(0.1)
    return False
