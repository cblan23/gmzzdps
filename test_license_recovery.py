import unittest
from types import SimpleNamespace
from unittest.mock import patch
from license_recovery import LicenseRecovery

class RecoveryTests(unittest.TestCase):
    def session(self, expiry=1000):
        return SimpleNamespace(runtime_capability=SimpleNamespace(expires_at=expiry), expires_at=None)

    def test_network_failure_keeps_valid_lease_and_obeys_three_minutes(self):
        policy=LicenseRecovery(); policy.last_success=10
        policy.failed()
        self.assertFalse(policy.must_pause(self.session(), now=189, wall=100))
        self.assertTrue(policy.must_pause(self.session(), now=190, wall=100))

    def test_lease_expiry_is_never_extended_by_grace(self):
        policy=LicenseRecovery(); policy.last_success=10
        self.assertTrue(policy.must_pause(self.session(100), now=11, wall=100))

    def test_recovery_resets_backoff(self):
        policy=LicenseRecovery()
        with patch('license_recovery.random.uniform', return_value=1):
            self.assertEqual([policy.failed() for _ in range(6)], [2,4,8,16,20,20])
            policy.succeeded()
            self.assertFalse(policy.reconnecting)
            self.assertEqual(policy.failed(),2)

    def test_card_deadline_is_respected(self):
        from datetime import datetime,timezone
        policy=LicenseRecovery(); policy.last_success=10
        session=self.session()
        session.expires_at=datetime.fromtimestamp(50,timezone.utc)
        self.assertTrue(policy.must_pause(session,now=11,wall=51))
