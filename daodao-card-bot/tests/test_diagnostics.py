import json
import tempfile
import unittest
from pathlib import Path
from app.utils.diagnostics import clean_fields, recent


class DiagnosticTests(unittest.TestCase):
    def test_allowlist_discards_credentials_payloads_and_newlines(self):
        result=clean_fields({'qq':'123456','elapsed_ms':30,'retcode':1200,
          'token':'secret','card':'GMZZSECRET','text':'chat','error_type':'oops\nsecret',
          'online':True,'http_status':'Bearer secret','message_id':'secret'})
        self.assertEqual(result,{'qq':'123456','elapsed_ms':30,'retcode':1200,'online':True})

    def test_export_revalidates_and_bounds_rotated_logs(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            (root/'diagnostic-worker.jsonl').write_text(json.dumps({'at':3,'event':'command_received','token':'secret','trace':'1234567890abcdef','qq':'123456'})+'\ninvalid\n')
            (root/'diagnostic-ingress.jsonl.1').write_text(json.dumps({'at':2,'event':'connected'})+'\n')
            rows=recent(1,root)
            self.assertEqual(len(rows),1)
            self.assertEqual(rows[0]['at'],3)
            self.assertNotIn('token',rows[0])
            self.assertEqual(rows[0]['trace'],'1234567890abcdef')

    def test_missing_logs_are_empty(self):
        with tempfile.TemporaryDirectory() as folder:self.assertEqual(recent(root=Path(folder)),[])
