#!/usr/bin/env python3
"""Minimal native Windows notification-area icon for the Tk application."""

from __future__ import annotations

import ctypes
import os
import sys
import threading
from ctypes import wintypes
from pathlib import Path
from typing import Callable


if sys.platform != "win32":
    raise RuntimeError("WindowsTrayIcon is only available on Windows")


HCURSOR = getattr(wintypes, "HCURSOR", wintypes.HANDLE)


WM_CLOSE = 0x0010
WM_DESTROY = 0x0002
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205
WM_CONTEXTMENU = 0x007B
WM_APP = 0x8000
WM_TRAY_CALLBACK = WM_APP + 37

NIM_ADD = 0x00000000
NIM_DELETE = 0x00000002
NIF_MESSAGE = 0x00000001
NIF_ICON = 0x00000002
NIF_TIP = 0x00000004

IMAGE_ICON = 1
LR_LOADFROMFILE = 0x00000010
LR_DEFAULTSIZE = 0x00000040
IDI_APPLICATION = 32512

MF_STRING = 0x00000000
MF_SEPARATOR = 0x00000800
TPM_RIGHTBUTTON = 0x0002
TPM_RETURNCMD = 0x0100

CMD_SHOW = 1001
CMD_EXIT = 1002
TRAY_ICON_ID = 1


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT),
        ("hIcon", wintypes.HICON),
        ("szTip", wintypes.WCHAR * 128),
        ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD),
        ("szInfo", wintypes.WCHAR * 256),
        ("uTimeoutOrVersion", wintypes.UINT),
        ("szInfoTitle", wintypes.WCHAR * 64),
        ("dwInfoFlags", wintypes.DWORD),
        ("guidItem", GUID),
        ("hBalloonIcon", wintypes.HICON),
    ]


WNDPROC = ctypes.WINFUNCTYPE(
    ctypes.c_ssize_t,
    wintypes.HWND,
    wintypes.UINT,
    wintypes.WPARAM,
    wintypes.LPARAM,
)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", HCURSOR),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("message", wintypes.UINT),
        ("wParam", wintypes.WPARAM),
        ("lParam", wintypes.LPARAM),
        ("time", wintypes.DWORD),
        ("pt", POINT),
    ]


user32 = ctypes.WinDLL("user32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
user32.RegisterClassW.restype = wintypes.ATOM
user32.CreateWindowExW.argtypes = [
    wintypes.DWORD,
    wintypes.LPCWSTR,
    wintypes.LPCWSTR,
    wintypes.DWORD,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    wintypes.HWND,
    wintypes.HMENU,
    wintypes.HINSTANCE,
    wintypes.LPVOID,
]
user32.CreateWindowExW.restype = wintypes.HWND
user32.DefWindowProcW.argtypes = [
    wintypes.HWND,
    wintypes.UINT,
    wintypes.WPARAM,
    wintypes.LPARAM,
]
user32.DefWindowProcW.restype = ctypes.c_ssize_t
user32.DestroyWindow.argtypes = [wintypes.HWND]
user32.DestroyWindow.restype = wintypes.BOOL
user32.PostMessageW.argtypes = [
    wintypes.HWND,
    wintypes.UINT,
    wintypes.WPARAM,
    wintypes.LPARAM,
]
user32.PostMessageW.restype = wintypes.BOOL
user32.GetMessageW.argtypes = [ctypes.POINTER(MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
user32.GetMessageW.restype = wintypes.BOOL
user32.TranslateMessage.argtypes = [ctypes.POINTER(MSG)]
user32.DispatchMessageW.argtypes = [ctypes.POINTER(MSG)]
user32.PostQuitMessage.argtypes = [ctypes.c_int]
user32.LoadImageW.argtypes = [
    wintypes.HINSTANCE,
    wintypes.LPCWSTR,
    wintypes.UINT,
    ctypes.c_int,
    ctypes.c_int,
    wintypes.UINT,
]
user32.LoadImageW.restype = wintypes.HANDLE
user32.LoadIconW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR]
user32.LoadIconW.restype = wintypes.HICON
user32.DestroyIcon.argtypes = [wintypes.HICON]
user32.GetCursorPos.argtypes = [ctypes.POINTER(POINT)]
user32.CreatePopupMenu.restype = wintypes.HMENU
user32.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR]
user32.TrackPopupMenu.argtypes = [
    wintypes.HMENU,
    wintypes.UINT,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    wintypes.HWND,
    wintypes.LPCVOID,
]
user32.TrackPopupMenu.restype = wintypes.UINT
user32.DestroyMenu.argtypes = [wintypes.HMENU]
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.RegisterWindowMessageW.argtypes = [wintypes.LPCWSTR]
user32.RegisterWindowMessageW.restype = wintypes.UINT
user32.UnregisterClassW.argtypes = [wintypes.LPCWSTR, wintypes.HINSTANCE]

shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]
shell32.Shell_NotifyIconW.restype = wintypes.BOOL
kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
kernel32.GetModuleHandleW.restype = wintypes.HMODULE


def _last_error(label: str) -> OSError:
    code = ctypes.get_last_error()
    return OSError(code, f"{label}: {ctypes.FormatError(code).strip()}")


class WindowsTrayIcon:
    def __init__(
        self,
        title: str,
        icon_path: str | Path,
        callback: Callable[[str], None],
    ):
        self.title = str(title)[:127]
        self.icon_path = Path(icon_path)
        self.callback = callback
        self.hwnd = 0
        self.hicon = 0
        self._owns_icon = False
        self._class_name = f"GMZZDpsTray_{os.getpid()}_{id(self):x}"
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._error: BaseException | None = None
        self._wndproc = WNDPROC(self._window_proc)
        self._nid: NOTIFYICONDATAW | None = None
        self._taskbar_created = user32.RegisterWindowMessageW("TaskbarCreated")

    def _emit(self, action: str) -> None:
        try:
            self.callback(action)
        except Exception:
            pass

    def _show_menu(self, hwnd: int) -> None:
        point = POINT()
        if not user32.GetCursorPos(ctypes.byref(point)):
            return
        menu = user32.CreatePopupMenu()
        if not menu:
            return
        try:
            user32.AppendMenuW(menu, MF_STRING, CMD_SHOW, "显示窗口")
            user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
            user32.AppendMenuW(menu, MF_STRING, CMD_EXIT, "退出")
            user32.SetForegroundWindow(hwnd)
            command = user32.TrackPopupMenu(
                menu,
                TPM_RIGHTBUTTON | TPM_RETURNCMD,
                point.x,
                point.y,
                0,
                hwnd,
                None,
            )
            if command == CMD_SHOW:
                self._emit("tray_restore")
            elif command == CMD_EXIT:
                self._emit("tray_exit")
        finally:
            user32.DestroyMenu(menu)

    def _add_icon(self) -> bool:
        return bool(
            self._nid
            and shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(self._nid))
        )

    def _window_proc(self, hwnd, message, wparam, lparam):
        if message == self._taskbar_created:
            self._add_icon()
            return 0
        if message == WM_TRAY_CALLBACK:
            mouse_message = int(lparam) & 0xFFFF
            if mouse_message in (WM_LBUTTONUP, WM_LBUTTONDBLCLK):
                self._emit("tray_restore")
            elif mouse_message in (WM_RBUTTONUP, WM_CONTEXTMENU):
                self._show_menu(hwnd)
            return 0
        if message == WM_CLOSE:
            user32.DestroyWindow(hwnd)
            return 0
        if message == WM_DESTROY:
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, message, wparam, lparam)

    def _run(self) -> None:
        instance = kernel32.GetModuleHandleW(None)
        registered = False
        try:
            window_class = WNDCLASSW()
            window_class.lpfnWndProc = self._wndproc
            window_class.hInstance = instance
            window_class.lpszClassName = self._class_name
            if not user32.RegisterClassW(ctypes.byref(window_class)):
                raise _last_error("RegisterClassW(tray)")
            registered = True
            # A zero-sized hidden top-level window is sufficient for tray
            # callback messages and avoids taskbar representation.
            hwnd = user32.CreateWindowExW(
                0,
                self._class_name,
                self.title,
                0,
                0,
                0,
                0,
                0,
                None,
                None,
                instance,
                None,
            )
            if not hwnd:
                raise _last_error("CreateWindowExW(tray)")
            self.hwnd = int(hwnd)
            if self.icon_path.is_file():
                self.hicon = int(
                    user32.LoadImageW(
                        None,
                        str(self.icon_path),
                        IMAGE_ICON,
                        0,
                        0,
                        LR_LOADFROMFILE | LR_DEFAULTSIZE,
                    )
                    or 0
                )
                self._owns_icon = bool(self.hicon)
            if not self.hicon:
                self.hicon = int(
                    user32.LoadIconW(None, ctypes.cast(IDI_APPLICATION, wintypes.LPCWSTR))
                    or 0
                )
            nid = NOTIFYICONDATAW()
            nid.cbSize = ctypes.sizeof(nid)
            nid.hWnd = hwnd
            nid.uID = TRAY_ICON_ID
            nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
            nid.uCallbackMessage = WM_TRAY_CALLBACK
            nid.hIcon = self.hicon
            nid.szTip = self.title
            self._nid = nid
            if not self._add_icon():
                raise _last_error("Shell_NotifyIconW(NIM_ADD)")
            self._ready.set()
            message = MSG()
            while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
                user32.TranslateMessage(ctypes.byref(message))
                user32.DispatchMessageW(ctypes.byref(message))
        except BaseException as exc:
            self._error = exc
            self._ready.set()
        finally:
            if self._nid is not None:
                shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._nid))
            if self._owns_icon and self.hicon:
                user32.DestroyIcon(self.hicon)
            self.hwnd = 0
            self.hicon = 0
            if registered:
                user32.UnregisterClassW(self._class_name, instance)

    def start(self) -> "WindowsTrayIcon":
        if self._thread and self._thread.is_alive():
            return self
        self._thread = threading.Thread(
            target=self._run, name="GMZZWindowsTray", daemon=False
        )
        self._thread.start()
        if not self._ready.wait(3.0):
            raise RuntimeError("system tray icon initialization timed out")
        if self._error:
            raise RuntimeError(f"system tray icon initialization failed: {self._error}")
        return self

    def stop(self) -> None:
        hwnd = self.hwnd
        if hwnd:
            user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
        thread = self._thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=3.0)

    @property
    def alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive() and self.hwnd)
