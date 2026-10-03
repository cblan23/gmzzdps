from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

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


if __name__ == "__main__":
    unittest.main()
