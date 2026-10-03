"""No-Npcap start/fallback/release boundary tests; fully mocked system APIs."""
import ast
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import npcap_capture_process as pipeline
import startup_bootstrap as bootstrap
from windows_raw_receiver import RawSocketUnavailable


ROOT = Path(__file__).resolve().parent


class StartupTests(unittest.TestCase):
    def test_no_npcap_installed_starts_without_loading_or_maintaining_driver(self):
        with patch.object(bootstrap.os, "name", "nt"), \
             patch.object(bootstrap, "is_process_elevated", return_value=True), \
             patch.object(bootstrap, "list_local_ipv4", return_value=("192.0.2.10",)), \
             patch.object(bootstrap, "probe_windows_raw_socket", return_value={"available": True, "capture_mode": 3}), \
             patch.object(bootstrap, "probe_windivert") as windivert:
            result = bootstrap.ensure_passive_capture_ready(ROOT, reporter=lambda _text: None)
        self.assertEqual(result["capture_source"], "windows_raw")
        windivert.assert_not_called()
        self.assertFalse(hasattr(bootstrap, "ensure_npcap_ready"))

    def test_permission_error_does_not_attempt_driver_install(self):
        with patch.object(bootstrap.os, "name", "nt"), \
             patch.object(bootstrap, "is_process_elevated", return_value=False), \
             patch.object(bootstrap, "probe_windows_raw_socket") as raw:
            with self.assertRaisesRegex(bootstrap.StartupBootstrapError, "管理员"):
                bootstrap.ensure_passive_capture_ready(ROOT, reporter=lambda _text: None)
        raw.assert_not_called()

    def test_builtin_failure_uses_bundled_windivert_without_npcap(self):
        with patch.object(bootstrap.os, "name", "nt"), \
             patch.object(bootstrap, "is_process_elevated", return_value=True), \
             patch.object(bootstrap, "list_local_ipv4", return_value=("192.0.2.10",)), \
             patch.object(bootstrap, "probe_windows_raw_socket", side_effect=RawSocketUnavailable("blocked")), \
             patch.object(bootstrap, "probe_windivert", return_value={"available": True, "driver_version": "2.2"}):
            result = bootstrap.ensure_passive_capture_ready(ROOT, reporter=lambda _text: None)
        self.assertTrue(result["fallback"])
        self.assertEqual(result["fallback_source"], "windivert_receive_only")

    def test_unavailable_sources_produce_diagnostic_not_installation_loop(self):
        with patch.object(bootstrap.os, "name", "nt"), \
             patch.object(bootstrap, "is_process_elevated", return_value=True), \
             patch.object(bootstrap, "list_local_ipv4", return_value=("192.0.2.10",)), \
             patch.object(bootstrap, "probe_windows_raw_socket", side_effect=RawSocketUnavailable("blocked")), \
             patch.object(bootstrap, "probe_windivert", side_effect=RawSocketUnavailable("windivert blocked")):
            with self.assertRaisesRegex(bootstrap.StartupBootstrapError, "blocked"):
                bootstrap.ensure_passive_capture_ready(ROOT, reporter=lambda _text: None)

    def test_explicit_npcap_backend_is_rejected(self):
        with self.assertRaises(bootstrap.StartupBootstrapError):
            bootstrap.ensure_passive_capture_ready(ROOT, "npcap", reporter=lambda _text: None)


class SourceSelectionTests(unittest.TestCase):
    def test_raw_receiver_never_loads_wpcap(self):
        source = Mock()
        with patch.object(pipeline, "WindowsHybridReceiver", return_value=source), \
             patch.object(pipeline, "BufferedReceiver") as buffer, \
             patch.object(pipeline.shadow_capture, "load_wpcap") as loader:
            pipeline._open_packet_receiver("windows_raw", [object()])
        buffer.assert_called_once_with(source, None, backend_name="Windows Native IPv4/IPv6")
        loader.assert_not_called()

    def test_raw_failure_does_not_retry_with_npcap(self):
        stop = Mock(is_set=lambda: False)
        snapshots = []
        iterations = iter([False, False, False, True])
        with patch.object(pipeline.os, "name", "nt"), \
             patch.object(pipeline, "_should_stop", side_effect=lambda *_args: next(iterations)), \
             patch.object(pipeline, "_runtime_active", return_value=True), \
             patch.object(pipeline, "_interruptible_wait"), \
             patch.object(pipeline, "_session", side_effect=RawSocketUnavailable("blocked")) as session, \
             patch.object(pipeline.shadow_capture, "load_wpcap", return_value=object()) as loader, \
             patch.object(pipeline, "_put", side_effect=lambda _q, kind, payload=None: snapshots.append((kind, payload))):
            pipeline._capture_forever(stop, object(), Mock(), None, None, capture_source="windows_raw")
        self.assertEqual(session.call_count, 1)
        self.assertEqual(session.call_args_list[0].kwargs["capture_source"], "windows_raw")
        loader.assert_not_called()
        self.assertTrue(any(kind == "fatal" for kind, _payload in snapshots))

    def test_no_fallback_validation_never_loads_driver(self):
        snapshots = []
        with patch.object(pipeline.os, "name", "nt"), \
             patch.object(pipeline, "_should_stop", return_value=False), \
             patch.object(pipeline, "_runtime_active", return_value=True), \
             patch.object(pipeline, "_session", side_effect=RawSocketUnavailable("blocked")), \
             patch.object(pipeline.shadow_capture, "load_wpcap") as loader, \
             patch.object(pipeline, "_put", side_effect=lambda _q, kind, payload=None: snapshots.append((kind, payload))):
            pipeline._capture_forever(Mock(), object(), Mock(), None, None,
                capture_source="windows_raw", allow_npcap_fallback=False)
        loader.assert_not_called()
        self.assertTrue(any(kind == "fatal" for kind, _payload in snapshots))

    def test_actual_source_is_reported_without_claiming_npcap_active(self):
        state = pipeline._capture_state_fields("windows_raw")
        self.assertTrue(state["passive_capture_active"])
        self.assertTrue(state["raw_socket_capture_active"])
        self.assertFalse(state["npcap_capture_active"])


class ReleaseBoundaryTests(unittest.TestCase):
    def test_default_build_does_not_require_installer(self):
        text = (ROOT / "build_exe.ps1").read_text(encoding="utf-8")
        self.assertIn('$PacketCapture = "windows_raw"', text)
        self.assertNotIn('$NpcapInstaller', text)
        self.assertNotIn('$UseNpcapBackend', text)
        self.assertIn('--include-module=windows_raw_receiver', text)
        self.assertIn('--include-module=windivert_receiver', text)
        self.assertIn('--include-module=windows_hybrid_receiver', text)
        self.assertIn('--include-module=windows_capture_process', text)
        self.assertIn('Target = "windivert/WinDivert.dll"', text)
        self.assertIn('Target = "windivert/WinDivert64.sys"', text)
        self.assertIn('passive_capture = $true', text)

    def test_receiver_has_no_transmit_or_process_access_calls(self):
        trees = [
            ast.parse((ROOT / name).read_text(encoding="utf-8"))
            for name in ("windows_raw_receiver.py", "windivert_receiver.py")
        ]
        forbidden = {"send", "sendall", "sendto", "connect", "WSASend", "WSASendTo",
                     "WinDivertSend", "WinDivertSendEx", "OpenProcess", "ReadProcessMemory",
                     "WriteProcessMemory", "VirtualAllocEx"}
        calls = {node.func.attr for tree in trees for node in ast.walk(tree)
                 if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)}
        self.assertFalse(calls & forbidden, calls & forbidden)

    def test_default_windows_client_disables_npcap_fallback(self):
        from windows_capture_process import CaptureProcessClient
        with patch.object(pipeline.CaptureProcessClient, "__init__") as initialize:
            CaptureProcessClient(runtime_capability=object())
        self.assertFalse(initialize.call_args.kwargs["allow_npcap_fallback"])

    def test_settlement_matcher_and_history_are_still_shared(self):
        from windows_capture_process import CaptureProcessClient
        self.assertTrue(issubclass(CaptureProcessClient, pipeline.CaptureProcessClient))


if __name__ == "__main__":
    unittest.main()
