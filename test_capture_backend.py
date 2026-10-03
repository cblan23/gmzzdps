"""Verify explicit backend selection and keep passive builds free of hooks."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent


class CaptureBackendTests(unittest.TestCase):
    def run_backend(self, variant=None):
        with tempfile.TemporaryDirectory() as directory:
            if variant is not None:
                Path(directory, '_capture_variant.json').write_text(json.dumps(variant), encoding='utf-8')
            command = (
                'import sys,json; sys._MEIPASS=sys.argv[1]; import capture_backend as c; '
                'print(json.dumps({"backend":c.CAPTURE_BACKEND_NAME,"module":c.CaptureProcessClient.__module__,'
                '"data":c.CAPTURE_DATA_DIRECTORY,"version":c.CAPTURE_DISPLAY_VERSION,'
                '"legacy_loaded":any(m in sys.modules for m in '
                '["capture_process","network_capture","damage_hook","inline_capture","team_stats_request_hook"])}))'
            )
            return subprocess.run([sys.executable, '-c', command, directory], cwd=ROOT,
                                  capture_output=True, text=True, timeout=30)

    def test_normal_start_is_passive_and_loads_no_hook_backend(self):
        result = self.run_backend()
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual(data['backend'], 'windows_raw')
        self.assertEqual(data['module'], 'windows_capture_process')
        self.assertFalse(data['legacy_loaded'])
        self.assertEqual(data['data'], '')
        self.assertEqual(data['version'], '')

    def test_invalid_manifest_does_not_choose_a_backend(self):
        result = self.run_backend({'backend': 'unknown'})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Unsupported capture backend', result.stderr)

    def test_old_manifest_cannot_reenable_active_capture(self):
        result = self.run_backend({'backend': 'legacy'})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('requires the built-in Windows Raw Socket backend', result.stderr)

    def test_windows_manifest_uses_receive_only_adapter_without_hooks(self):
        result = self.run_backend({'backend': 'windows_raw'})
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual(data['module'], 'windows_capture_process')
        self.assertFalse(data['legacy_loaded'])

    def test_old_npcap_variant_cannot_reenable_driver(self):
        result = self.run_backend({'backend': 'npcap', 'data_directory': 'GMZZDpsMeterNpcap'})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Unsupported capture backend: npcap', result.stderr)


if __name__ == '__main__':
    unittest.main()
