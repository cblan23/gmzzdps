"""Small, side-effect-free helpers for crisp Windows/Tk rendering.

Tk can be created successfully while the process is still DPI-unaware.  In
that state Windows bitmap-scales the whole window on a 125% (or secondary)
display, which makes canvas text and icons look soft.  Keep the Windows calls
in this module so every GUI entry point uses the same initialization order.
"""

from __future__ import annotations

import ctypes
import sys
from typing import Any


PER_MONITOR_AWARE_V2 = -4
DEFAULT_DPI = 96
MIN_DPI = 72
MAX_DPI = 384

# Values returned by GetAwarenessFromDpiAwarenessContext.  Keeping these
# names here makes the fallback logic explicit and gives the diagnostic tool
# a stable, privacy-safe way to report the process's DPI state.
DPI_AWARENESS_UNAWARE = 0
DPI_AWARENESS_SYSTEM = 1
DPI_AWARENESS_PER_MONITOR = 2


def _clamp_dpi(value: object, fallback: int = DEFAULT_DPI) -> int:
    try:
        dpi = int(round(float(value)))
    except (TypeError, ValueError, OverflowError):
        dpi = int(fallback)
    return max(MIN_DPI, min(MAX_DPI, dpi))


def _window_handle(window: Any) -> int:
    """Return a Tk window handle without requiring tkinter at import time."""
    try:
        if window is None or not bool(window.winfo_exists()):
            return 0
        return int(window.winfo_id() or 0)
    except (AttributeError, OSError, TypeError, ValueError):
        return 0


def _current_thread_awareness(user32: Any) -> int:
    """Return the current thread awareness, or ``UNAVAILABLE`` on old Windows."""

    try:
        get_context = user32.GetThreadDpiAwarenessContext
        get_context.argtypes = ()
        get_context.restype = ctypes.c_void_p
        get_awareness = user32.GetAwarenessFromDpiAwarenessContext
        get_awareness.argtypes = (ctypes.c_void_p,)
        get_awareness.restype = ctypes.c_int
        context = get_context()
        if not context:
            return -1
        return int(get_awareness(context))
    except (AttributeError, OSError, TypeError, ValueError):
        return -1


def current_dpi_awareness(platform: object = None) -> int:
    """Read the current thread's Windows DPI awareness level.

    ``-1`` means that the platform/API is unavailable.  The helper does not
    change process state and is safe to call from diagnostics.
    """

    current_platform = sys.platform if platform is None else str(platform)
    if current_platform != "win32":
        return DPI_AWARENESS_PER_MONITOR
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
    except (AttributeError, OSError, TypeError, ValueError):
        return -1
    return _current_thread_awareness(user32)


def enable_windows_dpi_awareness(platform: object = None) -> bool:
    """Opt the process into per-monitor DPI rendering before Tk starts.

    The call is deliberately idempotent.  A packaged executable may already
    have a manifest, in which case Windows returns ``ERROR_ACCESS_DENIED`` for
    the process-level setter; the existing thread awareness is then treated as
    success instead of falling back to bitmap scaling.
    """

    current_platform = sys.platform if platform is None else str(platform)
    if current_platform != "win32":
        return True

    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
    except (AttributeError, OSError, TypeError, ValueError):
        return False

    def current_awareness() -> int:
        return _current_thread_awareness(user32)

    # Prefer PMv2.  It keeps child/overlay windows and monitor transitions
    # aligned with the top-level meter.
    try:
        setter = user32.SetProcessDpiAwarenessContext
        setter.argtypes = (ctypes.c_void_p,)
        setter.restype = ctypes.c_bool
        if bool(setter(ctypes.c_void_p(PER_MONITOR_AWARE_V2))):
            process_ready = True
        else:
            # A process can already be system-aware because of an embedded
            # manifest.  That is not enough for a borderless window moved to
            # a 125% monitor, so keep trying the per-monitor fallbacks below.
            process_ready = current_awareness() >= DPI_AWARENESS_PER_MONITOR
    except (AttributeError, OSError, TypeError, ValueError):
        process_ready = False

    if not process_ready:
        # Windows 8.1 fallback.  ``2`` means per-monitor aware.
        try:
            setter = ctypes.WinDLL("shcore", use_last_error=True).SetProcessDpiAwareness
            setter.argtypes = (ctypes.c_int,)
            setter.restype = ctypes.c_long
            result = int(setter(2))
            process_ready = (
                result in (0, -2147024891)
                or current_awareness() >= DPI_AWARENESS_PER_MONITOR
            )
        except (AttributeError, OSError, TypeError, ValueError):
            process_ready = False

    if not process_ready:
        # Windows 7 fallback.  This is system-aware rather than per-monitor,
        # but it is still preferable to a bitmap-scaled process.
        try:
            setter = user32.SetProcessDPIAware
            setter.argtypes = ()
            setter.restype = ctypes.c_bool
            process_ready = bool(setter()) or current_awareness() >= DPI_AWARENESS_SYSTEM
        except (AttributeError, OSError, TypeError, ValueError):
            process_ready = current_awareness() >= DPI_AWARENESS_SYSTEM

    # A thread can retain an older context even after the process default is
    # changed.  Set the GUI thread explicitly when the API is available.
    thread_ready = False
    try:
        setter = user32.SetThreadDpiAwarenessContext
        setter.argtypes = (ctypes.c_void_p,)
        setter.restype = ctypes.c_void_p
        setter(ctypes.c_void_p(PER_MONITOR_AWARE_V2))
        thread_ready = current_awareness() >= DPI_AWARENESS_PER_MONITOR
    except (AttributeError, OSError, TypeError, ValueError):
        pass

    return bool(
        process_ready
        or thread_ready
        or current_awareness() >= DPI_AWARENESS_SYSTEM
    )


def get_window_dpi(window: Any = None, platform: object = None) -> int:
    """Read the physical DPI for a Tk window, with a stable 96-DPI fallback."""

    current_platform = sys.platform if platform is None else str(platform)
    if current_platform != "win32":
        return DEFAULT_DPI
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
    except (AttributeError, OSError, TypeError, ValueError):
        return DEFAULT_DPI

    hwnd = _window_handle(window)
    # Query the window first even while it is withdrawn.  A Tk HWND already
    # has its requested geometry at that point, and modern Windows associates
    # it with the nearest monitor.  Falling back to GetDpiForSystem first made
    # a window saved on a 125% secondary display build its initial fonts at the
    # primary display's 96 DPI.  Windows then bitmap-scaled that first surface.
    if hwnd:
        try:
            get_dpi = user32.GetDpiForWindow
            get_dpi.argtypes = (ctypes.c_void_p,)
            get_dpi.restype = ctypes.c_uint
            value = int(get_dpi(ctypes.c_void_p(hwnd)) or 0)
            if value:
                return _clamp_dpi(value)
        except (AttributeError, OSError, TypeError, ValueError):
            pass
    try:
        get_dpi = user32.GetDpiForSystem
        get_dpi.argtypes = ()
        get_dpi.restype = ctypes.c_uint
        value = int(get_dpi() or 0)
        if value:
            return _clamp_dpi(value)
    except (AttributeError, OSError, TypeError, ValueError):
        pass
    return DEFAULT_DPI


def configure_tk_dpi_scaling(root: Any, platform: object = None) -> int:
    """Set Tk's point scale and return the window's physical DPI."""

    dpi = get_window_dpi(root, platform=platform)
    desired = dpi / 72.0
    # ``tk scaling -displayof`` is important for per-monitor-aware Tk: the
    # global value can describe the monitor where the process started rather
    # than the monitor currently containing this borderless window.
    try:
        current = float(root.tk.call("tk", "scaling", "-displayof", str(root)))
    except Exception:
        current = 0.0
    # Avoid needless Tcl updates.  Repeated writes can make every widget
    # recalculate its geometry and create visible jitter while dragging.
    if abs(current - desired) > 0.01:
        try:
            root.tk.call("tk", "scaling", "-displayof", str(root), desired)
        except Exception:
            try:
                root.tk.call("tk", "scaling", desired)
            except Exception:
                pass
    return dpi


def logical_pixels_to_physical(logical_pixels: object, dpi: object = DEFAULT_DPI) -> int:
    """Convert a 96-DPI logical pixel size to a whole physical pixel size."""

    try:
        pixels = float(logical_pixels)
    except (TypeError, ValueError, OverflowError):
        pixels = 1.0
    try:
        display_dpi = _clamp_dpi(dpi)
    except (TypeError, ValueError, OverflowError):
        display_dpi = DEFAULT_DPI
    return max(1, int(round(pixels * display_dpi / DEFAULT_DPI)))


def tk_font_size_for_pixels(logical_pixels: object) -> int:
    """Convert a logical pixel size to a Tk point size.

    Tk positive sizes are points and therefore remain vector-rendered while
    Tk applies the display scale.  Keeping this conversion in one place avoids
    mixing negative pixel fonts (which do not follow DPI) with point fonts.
    """

    try:
        pixels = float(logical_pixels)
    except (TypeError, ValueError, OverflowError):
        pixels = 1.0
    return max(1, int(round(pixels * 72.0 / DEFAULT_DPI)))


def tk_font_spec(
    family: str, logical_pixels: object, weight: str = "normal"
) -> tuple[str, int, str]:
    """Build a DPI-friendly Tk font tuple for legacy widget call sites."""

    return (
        str(family),
        tk_font_size_for_pixels(logical_pixels),
        str(weight or "normal"),
    )
