import tempfile
import unittest
from pathlib import Path
from app.recovery_policy import RecoveryPolicy, health_state


class PolicyTests(unittest.TestCase):
    def test_failures_must_be_consecutive(self):
        with tempfile.TemporaryDirectory() as folder:
            p=RecoveryPolicy(Path(folder)/'budget.json')
            p.record_probe(False);p.record_probe(False)
            self.assertFalse(p.eligible(10000,True,False))
            p.record_probe(True);p.record_probe(False)
            self.assertEqual(p.failures,1)
            p.record_probe(False);p.record_probe(False)
            self.assertTrue(p.eligible(10000,True,False))
            self.assertFalse(p.eligible(10000,False,False))
            self.assertFalse(p.eligible(10000,True,True))

    def test_budget_survives_process_restart_and_clock_rollback(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'budget.json'
            p=RecoveryPolicy(path)
            self.assertTrue(p.reserve(10000))
            p=RecoveryPolicy(path)
            for _ in range(3):p.record_probe(False)
            self.assertFalse(p.eligible(10001,True,False))
            self.assertFalse(p.eligible(9000,True,False))
            self.assertTrue(p.eligible(11800,True,False))

    def test_corrupt_budget_fails_closed(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'budget.json';path.write_text('invalid')
            p=RecoveryPolicy(path)
            for _ in range(3):p.record_probe(False)
            self.assertFalse(p.eligible(10000,True,False))

    def test_health_precedence_and_stale_probe(self):
        probe={'ok':True,'checked_at':1000}
        self.assertEqual(health_state(True,True,probe,None,1001),'interfaces_ready')
        self.assertEqual(health_state(True,True,probe,None,1101),'checking')
        self.assertEqual(health_state(True,True,{'ok':False,'checked_at':1000},None,1001),'qq_unresponsive')
        self.assertEqual(health_state(True,False,probe,{'state':'awaiting_scan'},1001),'awaiting_scan')
        self.assertEqual(health_state(False,False,probe,{'state':'running'},1001),'recovering')
