import queue
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock
from test_combat_model import MODULE,DpsWindow
from test_main_hud_behavior import completed_record
from licensing import LicensingConnectionError,HeartbeatResult

class RecoveryIntegrationTests(unittest.TestCase):
    def test_transient_failure_then_success_never_requests_logout(self):
        stop=threading.Event()
        messages=queue.Queue()
        service=Mock()
        service.heartbeat.side_effect=[LicensingConnectionError('offline'),HeartbeatResult(True)]
        worker=MODULE['LicenseHeartbeatWorker'](service,messages,stop)
        waits=[]
        def wait(delay):
            waits.append(delay)
            if len(waits)==2:stop.set()
        stop.wait=wait
        worker.run()
        emitted=[messages.get_nowait()[0] for _ in range(messages.qsize())]
        self.assertEqual(emitted,['license_reconnecting','license_reconnected'])

    def test_explicit_denial_stops_without_retry(self):
        messages=queue.Queue();service=Mock()
        service.heartbeat.return_value=HeartbeatResult(False,message='card revoked')
        worker=MODULE['LicenseHeartbeatWorker'](service,messages,threading.Event())
        worker.run()
        self.assertEqual(messages.get_nowait(),('license_required','card revoked'))
        self.assertEqual(service.heartbeat.call_count,1)

    def test_expired_lease_drains_archives_once_then_resumes_after_renewal(self):
        window=object.__new__(DpsWindow)
        window.closing=False;window.authorization_resetting=False
        window.stop_event=threading.Event()
        window.worker=Mock();window.worker.is_alive.return_value=False
        policy=Mock();policy.must_pause.return_value=True;policy.reconnecting=True
        policy.successful_renewals=0
        window.heartbeat_worker=SimpleNamespace(recovery=policy)
        window.licensing=SimpleNamespace(session=object())
        window.model=Mock();window.model.build_combat_record.return_value=completed_record()
        window._ingest_pending_capture_messages=Mock(return_value=True)
        window._remember_main_battle_result=Mock()
        window._flush_combat_history=Mock()
        window._start_capture=Mock()
        window.status_label=Mock()
        window._check_license_network_recovery()
        self.assertTrue(window.stop_event.is_set())
        self.assertTrue(window.license_network_paused)
        window._flush_combat_history.assert_called_once_with(force=True)
        window._check_license_network_recovery()
        self.assertEqual(window.model.reset.call_count,1)
        policy.must_pause.return_value=False;policy.reconnecting=False
        # A stop event must not restart with the same old grant.
        window._check_license_network_recovery()
        window._start_capture.assert_not_called()
        policy.successful_renewals=1
        window._check_license_network_recovery()
        window._start_capture.assert_called_once()
        self.assertFalse(window.license_network_paused)
