"""Native Windows per-pixel-alpha presenter for the approved HUD artwork."""
from __future__ import annotations

import ctypes
import sys

from PIL import Image, ImageChops

from main_hud_artwork import (
    HUD_LOGICAL_WIDTH,
    HUD_LOGICAL_WIDTH_WITHOUT_DEATHS,
    HUD_MAX_VISIBLE_ROWS,
    HUD_ROW_HEIGHT,
    HudRenderResult,
    MainHudRenderer,
    clamp_visible_rows,
)


if sys.platform == "win32":
    from ctypes import wintypes

    class _Point(ctypes.Structure):
        _fields_ = (("x", wintypes.LONG), ("y", wintypes.LONG))

    class _Size(ctypes.Structure):
        _fields_ = (("cx", wintypes.LONG), ("cy", wintypes.LONG))

    class _BlendFunction(ctypes.Structure):
        _fields_ = (
            ("BlendOp", ctypes.c_ubyte),
            ("BlendFlags", ctypes.c_ubyte),
            ("SourceConstantAlpha", ctypes.c_ubyte),
            ("AlphaFormat", ctypes.c_ubyte),
        )

    class _BitmapInfoHeader(ctypes.Structure):
        _fields_ = (
            ("biSize", wintypes.DWORD),
            ("biWidth", wintypes.LONG),
            ("biHeight", wintypes.LONG),
            ("biPlanes", wintypes.WORD),
            ("biBitCount", wintypes.WORD),
            ("biCompression", wintypes.DWORD),
            ("biSizeImage", wintypes.DWORD),
            ("biXPelsPerMeter", wintypes.LONG),
            ("biYPelsPerMeter", wintypes.LONG),
            ("biClrUsed", wintypes.DWORD),
            ("biClrImportant", wintypes.DWORD),
        )

    class _BitmapInfo(ctypes.Structure):
        _fields_ = (("bmiHeader", _BitmapInfoHeader), ("bmiColors", wintypes.DWORD * 3))


class WindowsLayeredPresenter:
    """Own the DIB backing a Tk top-level and present premultiplied RGBA."""

    WS_EX_LAYERED = 0x00080000
    WS_EX_TOOLWINDOW = 0x00000080
    WS_EX_APPWINDOW = 0x00040000
    ULW_ALPHA = 0x00000002
    AC_SRC_ALPHA = 0x01

    def __init__(self, window) -> None:
        if sys.platform != "win32":
            raise OSError("UpdateLayeredWindow is only available on Windows")
        self.window = window
        self.user32 = ctypes.windll.user32
        self.gdi32 = ctypes.windll.gdi32
        self._configure_signatures()
        self.hwnd = self._root_handle(window)
        self.screen_dc = self.user32.GetDC(None)
        self.memory_dc = self.gdi32.CreateCompatibleDC(self.screen_dc)
        self.bitmap = None
        self.previous_bitmap = None
        self.bits = ctypes.c_void_p()
        self.size = (0, 0)
        self.closed = False
        self._reset_layered_style()

    def _configure_signatures(self) -> None:
        self.user32.GetParent.argtypes = [wintypes.HWND]
        self.user32.GetParent.restype = wintypes.HWND
        self.user32.GetDC.argtypes = [wintypes.HWND]
        self.user32.GetDC.restype = wintypes.HDC
        self.user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
        self.user32.UpdateLayeredWindow.argtypes = [
            wintypes.HWND,
            wintypes.HDC,
            ctypes.POINTER(_Point),
            ctypes.POINTER(_Size),
            wintypes.HDC,
            ctypes.POINTER(_Point),
            wintypes.COLORREF,
            ctypes.POINTER(_BlendFunction),
            wintypes.DWORD,
        ]
        self.user32.UpdateLayeredWindow.restype = wintypes.BOOL
        self.gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
        self.gdi32.CreateCompatibleDC.restype = wintypes.HDC
        self.gdi32.CreateDIBSection.argtypes = [wintypes.HDC, ctypes.POINTER(_BitmapInfo), wintypes.UINT, ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.DWORD]
        self.gdi32.CreateDIBSection.restype = wintypes.HBITMAP
        self.gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
        self.gdi32.SelectObject.restype = wintypes.HGDIOBJ
        self.gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
        self.gdi32.DeleteDC.argtypes = [wintypes.HDC]

    @staticmethod
    def _root_handle(window) -> int:
        window.update_idletasks()
        widget = int(window.winfo_id())
        parent = int(ctypes.windll.user32.GetParent(widget) or 0)
        return parent or widget

    def _extended_style(self) -> int:
        getter = getattr(self.user32, "GetWindowLongPtrW", self.user32.GetWindowLongW)
        getter.argtypes = [wintypes.HWND, ctypes.c_int]
        getter.restype = ctypes.c_ssize_t
        return int(getter(self.hwnd, -20))

    def _set_extended_style(self, value: int) -> None:
        setter = getattr(self.user32, "SetWindowLongPtrW", self.user32.SetWindowLongW)
        setter.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
        setter.restype = ctypes.c_ssize_t
        setter(self.hwnd, -20, int(value))

    def _reset_layered_style(self) -> None:
        style = self._extended_style()
        # Tk's -alpha/-transparentcolor uses SetLayeredWindowAttributes.
        # Toggle WS_EX_LAYERED once so UpdateLayeredWindow can own the surface.
        self._set_extended_style(style & ~self.WS_EX_LAYERED)
        self._set_extended_style(
            (style | self.WS_EX_LAYERED | self.WS_EX_TOOLWINDOW) & ~self.WS_EX_APPWINDOW
        )

    def _ensure_bitmap(self, width: int, height: int) -> None:
        if self.size == (width, height) and self.bitmap and self.bits.value:
            return
        if self.bitmap:
            self.gdi32.SelectObject(self.memory_dc, self.previous_bitmap)
            self.gdi32.DeleteObject(self.bitmap)
            self.bitmap = None
        info = _BitmapInfo()
        info.bmiHeader.biSize = ctypes.sizeof(_BitmapInfoHeader)
        info.bmiHeader.biWidth = width
        info.bmiHeader.biHeight = -height
        info.bmiHeader.biPlanes = 1
        info.bmiHeader.biBitCount = 32
        info.bmiHeader.biCompression = 0
        self.bits = ctypes.c_void_p()
        self.bitmap = self.gdi32.CreateDIBSection(
            self.screen_dc,
            ctypes.byref(info),
            0,
            ctypes.byref(self.bits),
            None,
            0,
        )
        if not self.bitmap or not self.bits.value:
            raise ctypes.WinError()
        previous = self.gdi32.SelectObject(self.memory_dc, self.bitmap)
        if self.previous_bitmap is None:
            self.previous_bitmap = previous
        self.size = (width, height)

    @staticmethod
    def _premultiplied_bgra(image: Image.Image) -> bytes:
        red, green, blue, alpha = image.convert("RGBA").split()
        return Image.merge(
            "RGBA",
            (
                ImageChops.multiply(blue, alpha),
                ImageChops.multiply(green, alpha),
                ImageChops.multiply(red, alpha),
                alpha,
            ),
        ).tobytes()

    def present(self, image: Image.Image, *, opacity: float = 1.0) -> None:
        if self.closed:
            return
        # Tk can replace the wrapper HWND when changing window styles. Keep
        # the already-allocated DIB, but reattach it to the current wrapper.
        current_hwnd = self._root_handle(self.window)
        if current_hwnd != self.hwnd:
            self.hwnd = current_hwnd
            self._reset_layered_style()
        rgba = image.convert("RGBA")
        width, height = rgba.size
        self._ensure_bitmap(width, height)
        pixels = self._premultiplied_bgra(rgba)
        ctypes.memmove(self.bits, pixels, len(pixels))
        point = _Point(int(self.window.winfo_x()), int(self.window.winfo_y()))
        size = _Size(width, height)
        source = _Point(0, 0)
        alpha = max(0, min(255, int(round(float(opacity) * 255))))
        blend = _BlendFunction(0, 0, alpha, self.AC_SRC_ALPHA)
        ok = self.user32.UpdateLayeredWindow(
            self.hwnd,
            self.screen_dc,
            ctypes.byref(point),
            ctypes.byref(size),
            self.memory_dc,
            ctypes.byref(source),
            0,
            ctypes.byref(blend),
            self.ULW_ALPHA,
        )
        if not ok:
            raise ctypes.WinError()

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        if self.bitmap:
            if self.previous_bitmap:
                self.gdi32.SelectObject(self.memory_dc, self.previous_bitmap)
            self.gdi32.DeleteObject(self.bitmap)
            self.bitmap = None
        if self.memory_dc:
            self.gdi32.DeleteDC(self.memory_dc)
            self.memory_dc = None
        if self.screen_dc:
            self.user32.ReleaseDC(None, self.screen_dc)
            self.screen_dc = None
        self.bits = ctypes.c_void_p()

    def __del__(self) -> None:
        try:
            self.close()
        except (AttributeError, OSError):
            pass
