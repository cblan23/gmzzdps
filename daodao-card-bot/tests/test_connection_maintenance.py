import os
import unittest
from unittest.mock import Mock,patch
from app import maintenance as m

class MaintenanceTests(unittest.TestCase):
    def test_early_refresh_before_expiry(self):
        with patch.object(m,'_CREDENTIAL_CACHE',('old',3000)),patch.object(m,'_CREDENTIAL_RETRY_AT',0),patch.object(m.time,'monotonic',return_value=2701),patch.dict(os.environ,{'NAPCAT_WEBUI_TOKEN':'test'}),patch.object(m,'webui',return_value={'Credential':'new'}) as api:
            self.assertEqual(m.credential(proactive=True),'new');api.assert_called_once()
    def test_failed_early_refresh_keeps_still_valid_credential(self):
        with patch.object(m,'_CREDENTIAL_CACHE',('old',3000)),patch.object(m,'_CREDENTIAL_RETRY_AT',0),patch.object(m.time,'monotonic',return_value=2701),patch.dict(os.environ,{'NAPCAT_WEBUI_TOKEN':'test'}),patch.object(m,'webui',side_effect=RuntimeError('network')) as api:
            self.assertEqual(m.credential(proactive=True),'old')
            self.assertEqual(m.credential(proactive=True),'old')
            api.assert_called_once()
    def test_expired_credential_never_returned_on_failure(self):
        with patch.object(m,'_CREDENTIAL_CACHE',('old',3000)),patch.object(m,'_CREDENTIAL_RETRY_AT',0),patch.object(m.time,'monotonic',return_value=3001),patch.dict(os.environ,{'NAPCAT_WEBUI_TOKEN':'test'}),patch.object(m,'webui',side_effect=RuntimeError('network')):
            with self.assertRaises(RuntimeError):m.credential(proactive=True)
    def test_qq_kick_never_triggers_automatic_restart(self):
        service=m.Maintenance(Mock());service.last_login_state=(False,'KickedOffLine')
        with patch.object(m,'credential'),patch.object(service,'_ensure_worker') as ensure,patch.object(m.subprocess,'run') as run:
            service.maintain_connections()
            ensure.assert_not_called();run.assert_not_called()
    def test_worker_failure_debounced_and_recovery_rate_limited(self):
        service=m.Maintenance(Mock());service.last_login_state=(True,'');service.worker_recovery_after=0
        with patch.object(m,'credential'),patch.object(m,'worker_connection',return_value={'connected':False}),patch.object(service,'_ensure_worker') as ensure,patch.object(m.time,'monotonic') as clock:
            clock.return_value=100;service.maintain_connections();ensure.assert_not_called()
            clock.return_value=120;service.maintain_connections();ensure.assert_not_called()
            clock.return_value=146;service.maintain_connections();ensure.assert_called_once()
            clock.return_value=160;service.maintain_connections();ensure.assert_called_once()
    def test_healthy_worker_not_restarted(self):
        service=m.Maintenance(Mock());service.last_login_state=(True,'')
        with patch.object(m,'credential'),patch.object(m,'worker_connection',return_value={'connected':True}),patch.object(service,'_ensure_worker') as ensure:
            service.maintain_connections();ensure.assert_not_called()

if __name__=='__main__':unittest.main()
