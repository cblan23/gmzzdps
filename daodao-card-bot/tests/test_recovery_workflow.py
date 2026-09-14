import time
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from app import maintenance as m

class RecoveryTests(unittest.TestCase):
    def setUp(self):
        probe=patch.object(m,'qq_live_probe',new=AsyncMock(return_value={'ok':True,'checked_at':0}))
        probe.start();self.addCleanup(probe.stop)
        self.service=m.Maintenance(SimpleNamespace(api_url='https://example.invalid/api/card/claim',api_token='secret',admins={'1'}))
        self.service.operation={'id':'test-12345','state':'running'}
        snapshot=patch.object(self.service,'diagnostic_snapshot')
        snapshot.start();self.addCleanup(snapshot.stop)

    def test_online_recovery_never_restarts_qq_container(self):
        with patch.object(m,'qq_status',new=AsyncMock(return_value={'online':True})),patch.object(self.service,'_ensure_worker') as ensure,patch.object(m.subprocess,'run') as run:
            self.assertEqual(self.service._recover(),'online')
            ensure.assert_called_once();run.assert_not_called()

    def test_offline_recovery_restarts_once_then_waits_for_qr(self):
        with patch.object(m,'qq_status',new=AsyncMock(return_value={'online':False})),patch.object(self.service,'_wait',return_value={'qrcodeurl':'initial'}),patch.object(self.service,'_fresh_qr',return_value='scan'),patch.object(m.subprocess,'run') as run:
            self.assertEqual(self.service._recover(),'scan')
            self.assertEqual(run.call_count,1)
            self.assertEqual(run.call_args.args[0],['docker','restart','--timeout','15','daodao-card-bot-napcat-1'])

    def test_false_online_does_not_report_recovered_without_restarting(self):
        with patch.object(m,'qq_status',new=AsyncMock(return_value={'online':True})),patch.object(self.service,'check_live_qq',return_value=False),patch.object(self.service,'_wait',return_value={'qrcodeurl':'initial'}),patch.object(self.service,'_fresh_qr',return_value='scan'),patch.object(m.subprocess,'run') as run:
            self.assertEqual(self.service._recover(),'scan')
            self.assertEqual(run.call_args.args[0],['docker','restart','--timeout','15','daodao-card-bot-napcat-1'])
    def test_unresponsive_qq_cannot_be_confirmed_ready(self):
        with patch.object(self.service,'_wait'),patch.object(self.service,'check_live_qq',return_value=False),self.assertRaises(m.WebuiError):
            self.service._ensure_worker()

    def test_refresh_ack_without_changed_qr_fails(self):
        self.service.verified_qr=('old',time.monotonic())
        def wait(predicate,seconds):
            self.assertIsNone(predicate())
            raise m.WebuiError('timeout')
        with patch.object(m,'login_state',return_value={'isLogin':False,'qrcodeurl':'old'}),patch.object(m,'credential',return_value='credential'),patch.object(m,'webui'),patch.object(self.service,'_wait',side_effect=wait),self.assertRaises(m.WebuiError):
            self.service._fresh_qr()
        self.assertIsNone(self.service.verified_qr)

    def test_changed_qr_is_accepted_only_without_error(self):
        states=[{'qrcodeurl':'old'},{'qrcodeurl':'new','loginError':'expired'},{'qrcodeurl':'new'}]
        def wait(predicate,seconds):
            self.assertIsNone(predicate())
            return predicate()
        with patch.object(m,'login_state',side_effect=states),patch.object(m,'credential',return_value='credential'),patch.object(m,'webui'),patch.object(self.service,'_wait',side_effect=wait):
            self.assertEqual(self.service._fresh_qr(),'scan')
        self.assertEqual(self.service.verified_qr[0],'new');self.assertTrue(self.service.awaiting_login)

    def test_unverified_or_expired_qr_never_returned(self):
        with patch.object(m,'login_state',return_value={'qrcodeurl':'old'}):
            self.assertIsNone(self.service.login()['qr'])
            self.service.verified_qr=('old',time.monotonic()-91)
            self.assertIsNone(self.service.login()['qr'])

    def test_qr_replacement_does_not_reuse_previous_verified_picture(self):
        self.service.verified_qr=('old',time.monotonic())
        with patch.object(m,'login_state',return_value={'qrcodeurl':'different'}):
            self.assertTrue(self.service.login()['needs_refresh'])
            self.assertIsNone(self.service.login()['qr'])

    def test_scanned_login_automatically_finishes_once(self):
        self.service.awaiting_login=True;self.service.last_login_state=(True,'')
        self.service.operation['state']='awaiting_scan'
        with patch.object(self.service,'_ensure_worker') as ensure:
            self.service.complete_login_if_ready();self.service.complete_login_if_ready()
            ensure.assert_called_once()
        self.assertEqual(self.service.operation['state'],'done')

    def test_scanned_login_with_worker_failure_is_not_success(self):
        self.service.awaiting_login=True;self.service.last_login_state=(True,'')
        self.service.operation['state']='awaiting_scan'
        with patch.object(self.service,'_ensure_worker',side_effect=RuntimeError('worker unavailable')):
            self.service.complete_login_if_ready()
        self.assertEqual(self.service.operation['state'],'running')
        self.assertTrue(self.service.awaiting_login)
        with patch.object(self.service,'_ensure_worker') as ensure:
            self.service.complete_login_if_ready()
            ensure.assert_not_called()
            self.service.login_ready_retry_at=0
            self.service.complete_login_if_ready()
            ensure.assert_called_once()
        self.assertEqual(self.service.operation['state'],'done')

    def test_post_login_retry_has_deadline_without_restarting_qq(self):
        self.service.awaiting_login=True
        self.service.login_ready_deadline=time.monotonic()-1
        self.service.last_login_state=(False,'')
        with patch.object(m.subprocess,'run') as run:
            self.service.complete_login_if_ready()
            run.assert_not_called()
        self.assertFalse(self.service.awaiting_login)
        self.assertEqual(self.service.operation['state'],'failed')

    def test_restart_worker_does_not_claim_success_on_heartbeat_alone(self):
        with patch.object(self.service,'observe_login'),patch.object(self.service,'_wait'),patch.object(m.subprocess,'run'),patch.object(self.service,'_ensure_worker',side_effect=m.WebuiError('not ready')):
            self.service._perform('restart_bot')
        self.assertEqual(self.service.operation['state'],'failed')

    def test_duplicate_click_does_not_start_second_recovery(self):
        self.service.operation=None
        with patch.object(m.threading,'Thread') as thread:
            first=self.service.action('recover','same-id-123')
            again=self.service.action('recover','same-id-123')
            self.assertEqual(first,again);self.assertEqual(thread.call_count,1)
            with self.assertRaises(BlockingIOError):self.service.action('recover','new-id-456')

    def test_runtime_record_not_accepted_for_reused_pid(self):
        with patch.object(m.Path,'read_text',return_value='{"pid":42,"at":0,"online":true}'),patch.object(m.Path,'read_bytes',return_value=b'other-process\0'):
            self.assertFalse(m.worker_connection()['connected'])

    def test_remote_api_failure_never_restarts_local_card_api(self):
        with patch.dict(os.environ,{'CARD_API_MANAGED_LOCALLY':'0'}),patch.object(self.service,'_wait'),patch.object(m,'worker_connection',return_value={'connected':True}),patch.object(m,'CardApiClient') as client,patch.object(m,'services') as service_status,patch.object(m.subprocess,'run') as run:
            client.return_value.post.side_effect=RuntimeError('remote unavailable')
            with self.assertRaises(RuntimeError):self.service._ensure_worker()
            service_status.assert_not_called();run.assert_not_called()

if __name__=='__main__':unittest.main()
