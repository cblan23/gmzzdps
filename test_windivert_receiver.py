import ctypes
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, Mock, patch

import windivert_receiver as divert

from windivert_receiver import (
    _normalise_driver_path,
    endpoint_needs_ipv6,
    locate_windivert_dll,
    windivert_filter,
)
from windows_hybrid_receiver import WindowsHybridReceiver


ROOT = Path(__file__).resolve().parent


def endpoint(address, port, protocol="udp", remote_address=""):
    return SimpleNamespace(
        local_address=address,
        local_port=port,
        protocol=protocol,
        remote_address=remote_address,
        remote_port=0,
    )


class FilterTests(unittest.TestCase):
    def test_dual_stack_wildcard_requires_ipv6_coverage(self):
        self.assertTrue(endpoint_needs_ipv6(endpoint("::", 32100)))
        self.assertFalse(endpoint_needs_ipv6(endpoint("::ffff:192.0.2.10", 32100)))

    def test_filter_is_port_scoped_and_receive_protocol_scoped(self):
        value = windivert_filter(
            [endpoint("::", 32100), endpoint("2001:db8::1", 32200, "tcp")]
        )
        self.assertIn("ipv6", value)
        self.assertIn("udp", value)
        self.assertIn("udp.SrcPort == 32100", value)
        self.assertIn("udp.DstPort == 32100", value)
        self.assertIn("tcp", value)
        self.assertIn("tcp.SrcPort == 32200", value)

    def test_source_tree_contains_reviewed_assets(self):
        value = locate_windivert_dll(ROOT)
        self.assertEqual(value.name, "WinDivert.dll")
        self.assertTrue(value.with_name("WinDivert64.sys").is_file())

    def test_stale_service_nt_path_is_normalised(self):
        self.assertEqual(
            _normalise_driver_path(r"\??\D:\Old App\WinDivert64.sys"),
            Path(r"D:\Old App\WinDivert64.sys"),
        )


class StaleServiceRecoveryTests(unittest.TestCase):
    def test_preflight_recovers_missing_file_and_missing_directory(self):
        for error in (2, 3):
            with self.subTest(error=error):
                library = Mock()
                library.WinDivertOpen.side_effect = [divert.INVALID_HANDLE_VALUE, 77]
                with patch.object(divert.ctypes, "get_last_error", return_value=error), \
                     patch.object(divert, "_remove_stale_windivert_service", return_value=True) as repair:
                    result = divert.probe_windivert(dll=library)
                self.assertTrue(result["available"])
                repair.assert_called_once_with()
                self.assertEqual(library.WinDivertOpen.call_count, 2)
                library.WinDivertClose.assert_called_once_with(77)

    def test_receiver_recovers_missing_file_and_missing_directory(self):
        for error in (2, 3):
            with self.subTest(error=error):
                receiver = object.__new__(divert.WinDivertReceiver)
                receiver.dll = Mock()
                receiver.dll.WinDivertOpen.side_effect = [divert.INVALID_HANDLE_VALUE, 77]
                with patch.object(divert.ctypes, "get_last_error", return_value=error), \
                     patch.object(divert, "_remove_stale_windivert_service", return_value=True) as repair:
                    result = receiver._open("ipv6 and udp")
                self.assertEqual(result.handle, 77)
                repair.assert_called_once_with()
                self.assertEqual(receiver.dll.WinDivertOpen.call_count, 2)
                for call in receiver.dll.WinDivertOpen.call_args_list:
                    self.assertEqual(call.args[-1], divert.WINDIVERT_FLAG_SNIFF | divert.WINDIVERT_FLAG_RECV_ONLY)

    def test_unrelated_errors_never_repair_or_retry(self):
        for error in (5, 577, 1275):
            with self.subTest(error=error):
                library = Mock()
                library.WinDivertOpen.return_value = divert.INVALID_HANDLE_VALUE
                with patch.object(divert.ctypes, "get_last_error", return_value=error), \
                     patch.object(divert, "_remove_stale_windivert_service") as repair:
                    with self.assertRaises(divert.WinDivertUnavailable):
                        divert.probe_windivert(dll=library)
                repair.assert_not_called()
                library.WinDivertOpen.assert_called_once()

    def test_failed_repair_is_not_retried(self):
        library = Mock()
        library.WinDivertOpen.return_value = divert.INVALID_HANDLE_VALUE
        with patch.object(divert.ctypes, "get_last_error", return_value=3), \
             patch.object(divert, "_remove_stale_windivert_service", return_value=False):
            with self.assertRaises(divert.WinDivertUnavailable):
                divert.probe_windivert(dll=library)
        library.WinDivertOpen.assert_called_once()

    def test_stale_registration_is_removed_only_when_service_is_stopped(self):
        for state, query_ok, expected in ((1, True, True), (4, True, False), (2, True, False), (1, False, False)):
            with self.subTest(state=state, query_ok=query_ok), tempfile.TemporaryDirectory() as temporary:
                registry = MagicMock()
                registry.QueryValueEx.return_value = (str(Path(temporary) / "gone" / "WinDivert64.sys"), 1)
                api = Mock()
                api.OpenSCManagerW.return_value = 10
                api.OpenServiceW.return_value = 20

                def query(_service, status):
                    ctypes.cast(status, ctypes.POINTER(divert._ServiceStatus)).contents.current_state = state
                    return int(query_ok)

                api.QueryServiceStatus.side_effect = query
                api.DeleteService.return_value = 1
                with patch.object(divert, "winreg", registry), \
                     patch.object(divert.ctypes, "WinDLL", return_value=api):
                    self.assertEqual(divert._remove_stale_windivert_service(), expected)
                self.assertEqual(api.DeleteService.call_count, int(expected))
                self.assertEqual(api.CloseServiceHandle.call_count, 2)

    def test_existing_driver_path_is_preserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            driver = Path(temporary) / "WinDivert64.sys"
            driver.touch()
            registry = MagicMock()
            registry.QueryValueEx.return_value = (str(driver), 1)
            with patch.object(divert, "winreg", registry), \
                 patch.object(divert.ctypes, "WinDLL") as api:
                self.assertFalse(divert._remove_stale_windivert_service())
            api.assert_not_called()


class FakeReceiver:
    def __init__(self, endpoints, **kwargs):
        self.endpoints = list(endpoints)
        self.versions = frozenset(kwargs.get("versions", ()))
        self.handles = {"handle": (SimpleNamespace(timestamp_resolution_ns=1), 101, "")}
        self.local_addresses = set()
        self.counters = {}
        self.closed = False

    def refresh(self, endpoints):
        self.endpoints = list(endpoints)

    def next_frame(self):
        return None

    def statistics(self, _read=None):
        return {"available": True, "interfaces": {"handle": {"available": True}}, "dropped": 0}

    def close(self):
        self.closed = True


class HybridSelectionTests(unittest.TestCase):
    def test_ipv4_uses_raw_only(self):
        raw = Mock(side_effect=FakeReceiver)
        divert = Mock(side_effect=FakeReceiver)
        receiver = WindowsHybridReceiver(
            [endpoint("192.0.2.10", 32100)], raw_factory=raw, windivert_factory=divert
        )
        self.assertIsNotNone(receiver.raw)
        self.assertIsNone(receiver.windivert)
        divert.assert_not_called()
        frame = object()
        receiver.raw.next_frame = Mock(return_value=frame)
        self.assertIs(receiver.next_frame(), frame)
        receiver.close()

    def test_dual_stack_uses_both_sources(self):
        raw = Mock(side_effect=FakeReceiver)
        divert = Mock(side_effect=FakeReceiver)
        receiver = WindowsHybridReceiver(
            [endpoint("::", 32100)], raw_factory=raw, windivert_factory=divert
        )
        self.assertIsNotNone(receiver.raw)
        self.assertIsNotNone(receiver.windivert)
        self.assertEqual(receiver.windivert.versions, frozenset({6}))
        receiver.close()

    def test_raw_failure_uses_windivert_for_both_versions(self):
        raw = Mock(side_effect=RuntimeError("raw blocked"))
        divert = Mock(side_effect=FakeReceiver)
        receiver = WindowsHybridReceiver(
            [endpoint("192.0.2.10", 32100)], raw_factory=raw, windivert_factory=divert
        )
        self.assertIsNone(receiver.raw)
        self.assertEqual(receiver.windivert.versions, frozenset({4}))
        receiver.close()

    def test_runtime_raw_failure_switches_to_windivert_without_restart(self):
        raw = Mock(side_effect=FakeReceiver)
        divert = Mock(side_effect=FakeReceiver)
        receiver = WindowsHybridReceiver(
            [endpoint("192.0.2.10", 32100)], raw_factory=raw, windivert_factory=divert
        )
        first_raw = receiver.raw
        first_raw.next_frame = Mock(side_effect=RuntimeError("raw receive blocked"))

        self.assertIsNone(receiver.next_frame())
        self.assertTrue(first_raw.closed)
        self.assertIsNone(receiver.raw)
        self.assertEqual(receiver.windivert.versions, frozenset({4}))
        self.assertEqual(receiver.counters["capture_adapter_errors"], 1)
        frame = object()
        receiver.windivert.next_frame = Mock(return_value=frame)
        self.assertIs(receiver.next_frame(), frame)
        receiver.refresh([endpoint("192.0.2.10", 32100)])
        self.assertEqual(raw.call_count, 1)
        receiver.close()

    def test_runtime_raw_failure_reports_windivert_block(self):
        receiver = WindowsHybridReceiver(
            [endpoint("192.0.2.10", 32100)],
            raw_factory=FakeReceiver,
            windivert_factory=Mock(side_effect=RuntimeError("WinDivert 错误 1275")),
        )
        receiver.raw.next_frame = Mock(side_effect=RuntimeError("raw receive blocked"))

        with self.assertRaisesRegex(RuntimeError, "1275"):
            receiver.next_frame()
        receiver.close()


if __name__ == "__main__":
    unittest.main()
