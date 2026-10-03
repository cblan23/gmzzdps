"""Built-in passive capture preflight; no installed packet driver required."""

import unittest
from pathlib import Path
from unittest.mock import patch

import startup_bootstrap as bootstrap


class ElevationTests(unittest.TestCase):
    def test_frozen_relaunch_keeps_the_original_executable_path(self):
        executable, arguments = bootstrap.elevation_command(
            frozen=True,
            application_path=Path(r"D:\Tools\Dps-Logs.exe"),
            source_path=Path("ignored.pyw"),
            python_executable=Path("ignored-python.exe"),
            arguments=("--example", "two words"),
        )
        self.assertEqual(executable, Path(r"D:\Tools\Dps-Logs.exe"))
        self.assertEqual(arguments, ("--example", "two words"))

    def test_source_relaunch_uses_python_and_the_source_file(self):
        executable, arguments = bootstrap.elevation_command(
            frozen=False,
            application_path=Path("ignored.exe"),
            source_path=Path(r"D:\src\dps_meter.pyw"),
            python_executable=Path(r"C:\Python\python.exe"),
            arguments=("--example",),
        )
        self.assertEqual(executable, Path(r"C:\Python\python.exe"))
        self.assertEqual(arguments, (r"D:\src\dps_meter.pyw", "--example"))


class PassivePreflightTests(unittest.TestCase):
    def test_non_windows_is_rejected_before_opening_a_socket(self):
        bundle = Path("bundle")
        with patch.object(bootstrap.os, "name", "posix"), patch.object(
            bootstrap, "probe_windows_raw_socket"
        ) as raw:
            with self.assertRaisesRegex(bootstrap.StartupBootstrapError, "Windows"):
                bootstrap.ensure_passive_capture_ready(bundle)
        raw.assert_not_called()

    def test_admin_required_without_installing_any_driver(self):
        with patch.object(bootstrap.os, "name", "nt"), patch.object(
            bootstrap, "is_process_elevated", return_value=False
        ), patch.object(bootstrap, "probe_windows_raw_socket") as raw:
            with self.assertRaisesRegex(bootstrap.StartupBootstrapError, "管理员"):
                bootstrap.ensure_passive_capture_ready(Path("bundle"))
        raw.assert_not_called()

    def test_raw_socket_is_preferred_when_available(self):
        with patch.object(bootstrap.os, "name", "nt"), patch.object(
            bootstrap, "is_process_elevated", return_value=True
        ), patch.object(bootstrap, "list_local_ipv4", return_value=["127.0.0.1", "192.0.2.10"]), patch.object(
            bootstrap, "probe_windows_raw_socket", return_value={"capture_mode": "raw"}
        ) as raw, patch.object(bootstrap, "probe_windivert") as windivert:
            result = bootstrap.ensure_passive_capture_ready(Path("bundle"), reporter=lambda _: None)
        self.assertEqual(result["capture_source"], "windows_raw")
        self.assertTrue(result["traffic_validation_pending"])
        raw.assert_called_once_with("192.0.2.10")
        windivert.assert_not_called()

    def test_raw_unavailable_falls_back_to_bundled_receive_only_windivert(self):
        with patch.object(bootstrap.os, "name", "nt"), patch.object(
            bootstrap, "is_process_elevated", return_value=True
        ), patch.object(bootstrap, "list_local_ipv4", return_value=["192.0.2.10"]), patch.object(
            bootstrap, "probe_windows_raw_socket", side_effect=OSError("raw blocked")
        ), patch.object(bootstrap, "probe_windivert", return_value={"driver_version": "2.2"}) as windivert:
            result = bootstrap.ensure_passive_capture_ready(Path("bundle"), reporter=lambda _: None)
        windivert.assert_called_once()
        self.assertEqual(result["capture_source"], "windows_raw")
        self.assertEqual(result["fallback_source"], "windivert_receive_only")

    def test_no_available_source_fails_without_driver_maintenance(self):
        with patch.object(bootstrap.os, "name", "nt"), patch.object(
            bootstrap, "is_process_elevated", return_value=True
        ), patch.object(bootstrap, "list_local_ipv4", return_value=["192.0.2.10"]), patch.object(
            bootstrap, "probe_windows_raw_socket", side_effect=OSError("raw blocked")
        ), patch.object(bootstrap, "probe_windivert", side_effect=OSError("windivert blocked")):
            with self.assertRaisesRegex(bootstrap.StartupBootstrapError, "raw blocked.*windivert blocked"):
                bootstrap.ensure_passive_capture_ready(Path("bundle"), reporter=lambda _: None)

    def test_npcap_backend_cannot_be_selected(self):
        with self.assertRaises(bootstrap.StartupBootstrapError):
            bootstrap.ensure_passive_capture_ready(Path("bundle"), "npcap")
        self.assertFalse(hasattr(bootstrap, "ensure_npcap_ready"))
        self.assertFalse(hasattr(bootstrap, "probe_npcap_runtime"))


if __name__ == "__main__":
    unittest.main()
