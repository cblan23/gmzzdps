import unittest
from scripts.prepare_kick_diagnostics import patch_source

class DiagnosticPatchTests(unittest.TestCase):
    def test_preserves_original_callback_and_private_guard(self):
        anchor='      const i = `[KickedOffLine] [${r.tipsTitle}] ${r.tipsDesc}`;'
        source='private_guard\n'+anchor+'\nthis.selfInfo.online = false; emit(i);'
        result=patch_source(source)
        self.assertTrue(result.startswith('private_guard\n'))
        self.assertTrue(result.endswith('this.selfInfo.online = false; emit(i);'))
        self.assertIn('Number.isSafeInteger',result)
        self.assertNotIn('JSON.stringify(r)',result)
    def test_unexpected_or_double_patch_rejected(self):
        with self.assertRaises(AssertionError):patch_source('different source')

if __name__=='__main__':unittest.main()
