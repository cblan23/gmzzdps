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

    def test_normal_start_uses_restored_hook_backend(self):
        result = self.run_backend()
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual(data['backend'], 'legacy')
        self.assertEqual(data['module'], 'capture_process')
        self.assertTrue(data['legacy_loaded'])
        self.assertEqual(data['data'], '')
        self.assertEqual(data['version'], '')

    def test_invalid_manifest_does_not_choose_a_backend(self):
        result = self.run_backend({'backend': 'unknown'})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Unsupported capture backend', result.stderr)

    def test_isolated_variant_is_still_explicit(self):
        result = self.run_backend({'backend': 'npcap', 'data_directory': 'GMZZDpsMeterNpcap'})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['data'], 'GMZZDpsMeterNpcap')
        self.assertEqual(json.loads(result.stdout)['module'], 'npcap_capture_process')
        self.assertFalse(json.loads(result.stdout)['legacy_loaded'])


if __name__ == '__main__':
    unittest.main()
