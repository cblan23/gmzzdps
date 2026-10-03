"""Import audit in an isolated interpreter. Does not start the app/game capture."""
import json
from pathlib import Path
import subprocess
import sys
import unittest

ROOT=Path(__file__).resolve().parent


class ProductionBoundaryTests(unittest.TestCase):
    def test_main_import_never_loads_invasive_capture_or_entity_scanner(self):
        script = """
import runpy,sys,json
runpy.run_path('dps_meter.pyw',run_name='v030_import_audit')
forbidden=['capture_process','damage_hook','network_capture','inline_capture',
           'team_stats_request_hook','frida','npcap_entity_metadata']
print(json.dumps([m for m in forbidden if m in sys.modules]))
"""
        result=subprocess.run([sys.executable,'-c',script],cwd=ROOT,capture_output=True,text=True,timeout=30)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(json.loads(result.stdout),[])

    def test_build_keeps_passive_transport_and_bundles_bounded_rpc_bridges(self):
        text=(ROOT/'build_exe.ps1').read_text(encoding='utf-8')
        self.assertNotIn('--include-module=capture_process',text)
        self.assertIn('--include-module=npcap_entity_metadata',text)
        self.assertIn('--include-module=network_capture',text)
        self.assertIn('--include-module=inline_capture',text)
        self.assertIn('--include-module=team_stats_request_hook',text)
        self.assertNotIn('--nofollow-import-to=network_capture',text)
        self.assertNotIn('--nofollow-import-to=inline_capture',text)
        self.assertNotIn('--nofollow-import-to=team_stats_request_hook',text)
        self.assertIn('--include-module=startup_bootstrap',text)
        self.assertNotIn('_npcap/$NpcapPayloadTargetName',text)
        self.assertNotIn('Npcap installer must be version 1.89',text)
        self.assertNotIn('$UseNpcapBackend',text)
        self.assertIn('backend = "windows_raw"',text)
        self.assertIn('passive_capture = $true',text)
        self.assertIn('server_requests_added = 2',text)
        self.assertIn('game_process_access = "query_read_write_bounded_rpc_hooks"',text)
        self.assertIn(
            '--include-data-files=$ZstdRestoreDll=npcap_zstd_restore.dll',
            text,
        )
        self.assertIn('Target = "npcap_zstd_restore.dll"', text)

    def test_portable_entrypoint_owns_elevation_and_passive_preflight(self):
        main=(ROOT/'dps_meter.pyw').read_text(encoding='utf-8')
        bootstrap=(ROOT/'startup_bootstrap.py').read_text(encoding='utf-8')
        self.assertIn('prepare_windows_startup()',main)
        self.assertIn('relaunch_as_admin(',main)
        self.assertIn('ensure_passive_capture_ready(BUNDLE_DIR, CAPTURE_BACKEND_NAME)',main)
        self.assertNotIn('LocalApplicationData),\n        "Programs"',bootstrap)
        self.assertNotIn('DaodaoDpsLogs',bootstrap)

    def test_runtime_target_identity_reader_is_read_only_and_closed(self):
        text=(ROOT/'npcap_capture_process.py').read_text(encoding='utf-8')
        self.assertIn('PassiveEntityMetadataReader(pid, target_profile)',text)
        self.assertIn('metadata_reader.close()',text)
        self.assertIn('target_profile=capability.profile',text)
        self.assertNotIn('VirtualAllocEx',text)
        self.assertNotIn('WriteProcessMemory',text)

    def test_production_uses_established_history_ui_not_preview_window(self):
        text=(ROOT/'dps_meter.pyw').read_text(encoding='utf-8')
        self.assertIn('SettlementHistoryAdapter',text)
        self.assertNotIn('SettlementHistoryWindow',text)

    def test_preview_uses_full_backend_shell_and_both_recorded_wipes(self):
        text=(ROOT/'tools'/'preview_official_settlement_history.py').read_text(
            encoding='utf-8'
        )
        self.assertIn('window._build_history_window()',text)
        self.assertNotIn('window._build_backend_history_page(root)',text)
        self.assertIn('second-start-first-wipe-evidence.json',text)
        self.assertIn('third-start-second-wipe-skills.json',text)

if __name__=='__main__':unittest.main()
