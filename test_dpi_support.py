import unittest
from unittest import mock

import dpi_support


class _FakeTk:
    def __init__(self, value=1.3333333):
        self.value = value
        self.calls = []

    def call(self, *args):
        self.calls.append(args)
        if args[:3] == ("tk", "scaling", "-displayof") and len(args) == 4:
            return self.value
        if args[:2] == ("tk", "scaling") and len(args) == 3:
            return self.value
        if args[:3] == ("tk", "scaling", "-displayof") and len(args) == 5:
            self.value = float(args[-1])
            return self.value
        if args[:2] == ("tk", "scaling") and len(args) == 3:
            self.value = float(args[-1])
            return self.value
        raise AssertionError(args)


class _FakeRoot:
    def __init__(self):
        self.tk = _FakeTk()

    def __str__(self):
        return "."


class _FakeWin32Function:
    def __init__(self, callback):
        self.callback = callback
        self.argtypes = None
        self.restype = None

    def __call__(self, *args):
        return self.callback(*args)


class _FakeWindow:
    @staticmethod
    def winfo_exists():
        return True

    @staticmethod
    def winfo_id():
        return 1234

    @staticmethod
    def winfo_ismapped():
        return False


class DpiSupportTests(unittest.TestCase):
    def test_logical_pixels_are_rounded_to_physical_pixels(self):
        self.assertEqual(dpi_support.logical_pixels_to_physical(20, 96), 20)
        self.assertEqual(dpi_support.logical_pixels_to_physical(20, 120), 25)
        self.assertEqual(dpi_support.logical_pixels_to_physical(9, 144), 14)
        self.assertEqual(dpi_support.logical_pixels_to_physical("bad", 120), 1)

    def test_font_spec_uses_positive_point_size(self):
        family, size, weight = dpi_support.tk_font_spec(
            "Microsoft YaHei UI", 14, "bold"
        )
        self.assertEqual(family, "Microsoft YaHei UI")
        self.assertEqual(size, 10)
        self.assertEqual(weight, "bold")
        self.assertGreater(size, 0)

    def test_tk_scaling_is_applied_per_display(self):
        root = _FakeRoot()
        # Patch only the OS query; the Tcl call path remains real and
        # verifies that the display-specific form is used.
        original = dpi_support.get_window_dpi
        dpi_support.get_window_dpi = lambda *_args, **_kwargs: 120
        try:
            self.assertEqual(dpi_support.configure_tk_dpi_scaling(root), 120)
        finally:
            dpi_support.get_window_dpi = original
        self.assertIn(
            ("tk", "scaling", "-displayof", ".", 120 / 72.0),
            root.tk.calls,
        )

    def test_hidden_window_prefers_window_dpi_over_system_dpi(self):
        system_calls = []
        user32 = type("User32", (), {})()
        user32.GetDpiForWindow = _FakeWin32Function(lambda _hwnd: 120)

        def get_system():
            system_calls.append(True)
            return 96

        user32.GetDpiForSystem = _FakeWin32Function(get_system)
        with mock.patch.object(dpi_support.ctypes, "WinDLL", return_value=user32):
            self.assertEqual(
                dpi_support.get_window_dpi(_FakeWindow(), platform="win32"),
                120,
            )
        self.assertEqual(system_calls, [])

    def test_non_windows_awareness_is_deterministic_and_side_effect_free(self):
        self.assertEqual(
            dpi_support.current_dpi_awareness("linux"),
            dpi_support.DPI_AWARENESS_PER_MONITOR,
        )
        self.assertTrue(dpi_support.enable_windows_dpi_awareness("linux"))


if __name__ == "__main__":
    unittest.main()
