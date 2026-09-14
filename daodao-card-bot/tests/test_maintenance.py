import base64
import json
import os
import threading
import unittest
import urllib.error
import urllib.request
from unittest.mock import Mock, patch
from http.server import ThreadingHTTPServer
from app.maintenance import Maintenance, make_handler, safe_text

class HttpTests(unittest.TestCase):
    def setUp(self):
        self.maintenance=Mock(csrf='test-csrf-value')
        self.maintenance.status.return_value={'qq':{'online':False},'csrf':'test-csrf-value'}
        self.maintenance.action.return_value={'state':'running'}
        self.server=ThreadingHTTPServer(('127.0.0.1',0),make_handler(self.maintenance,'admin','test-password'))
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.url='http://127.0.0.1:'+str(self.server.server_port)+'/api/v1/dps/admin/qq/'
    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join()
    def request(self,path,body=None,auth=True,csrf=None,origin=None):
        headers={}
        if auth:headers['Authorization']='Basic '+base64.b64encode(b'admin:test-password').decode()
        if body is not None:headers['Content-Type']='application/json'
        if csrf:headers['X-Daodao-CSRF']=csrf
        if origin:headers['Origin']=origin
        request=urllib.request.Request(self.url+path,data=json.dumps(body).encode() if body is not None else None,headers=headers)
        try:response=urllib.request.urlopen(request,timeout=3)
        except urllib.error.HTTPError as error:response=error
        with response:return response.status,json.load(response)
    def test_status_requires_existing_admin_credentials(self):
        self.assertEqual(self.request('status',auth=False)[0],401)
        self.maintenance.status.assert_not_called()
        self.assertEqual(self.request('status')[1]['qq']['online'],False)
    def test_diagnostics_requires_admin_and_contains_only_events(self):
        self.assertEqual(self.request('diagnostics',auth=False)[0],401)
        with patch('app.maintenance.recent',return_value=[{'event':'connected','at':1}]) as read:
            code,data=self.request('diagnostics')
            self.assertEqual(code,200)
            self.assertEqual(data['events'],[{'event':'connected','at':1}])
            read.assert_called_once_with(3000)
    def test_restart_without_csrf_is_rejected(self):
        self.assertEqual(self.request('action',{'action':'restart_bot','request_id':'test-id-123'},origin='https://daodaogame.vip')[0],403)
        self.maintenance.action.assert_not_called()
    def test_cross_origin_restart_is_rejected(self):
        self.assertEqual(self.request('action',{'action':'restart_bot','request_id':'test-id-123'},csrf='test-csrf-value',origin='https://attacker.invalid')[0],403)
        self.maintenance.action.assert_not_called()
    def test_verified_action_is_forwarded_once(self):
        self.assertEqual(self.request('action',{'action':'restart_bot','request_id':'test-id-123'},csrf='test-csrf-value',origin='https://daodaogame.vip')[0],202)
        self.maintenance.action.assert_called_once_with('restart_bot','test-id-123')

class PolicyTests(unittest.TestCase):
    def test_credential_reused_across_repeated_checks(self):
        from app import maintenance
        with patch.object(maintenance,'_CREDENTIAL_CACHE',None),patch.object(maintenance,'_CREDENTIAL_RETRY_AT',0),patch.dict(os.environ,{'NAPCAT_WEBUI_TOKEN':'test-token'}),patch.object(maintenance,'webui',return_value={'Credential':'test-session'}) as api:
            for _ in range(20):self.assertEqual(maintenance.credential(),'test-session')
            api.assert_called_once()
    def test_failed_authentication_is_not_retried_by_each_poll(self):
        from app import maintenance
        with patch.object(maintenance,'_CREDENTIAL_CACHE',None),patch.object(maintenance,'_CREDENTIAL_RETRY_AT',0),patch.dict(os.environ,{'NAPCAT_WEBUI_TOKEN':'test-token'}),patch.object(maintenance,'webui',side_effect=maintenance.WebuiError('limited')) as api:
            for _ in range(2):
                with self.assertRaises(maintenance.WebuiError):maintenance.credential()
            api.assert_called_once()
    def test_restart_targets_only_fixed_bot_service(self):
        service=Maintenance(Mock())
        service.operation={'state':'running'}
        with patch.object(service,'observe_login'),patch.object(service,'_wait',return_value=True),patch.object(service,'_ensure_worker') as ensure,patch('app.maintenance.subprocess.run') as run:
            service._perform('restart_bot')
            ensure.assert_called_once()
        self.assertEqual(run.call_args.args[0],['systemctl','restart','daodao-card-bot.service'])
        self.assertEqual(service.operation['state'],'done')
    def test_napcat_restart_preserves_volumes(self):
        service=Maintenance(Mock())
        service.operation={'state':'running'}
        with patch.object(service,'observe_login'),patch.object(service,'_wait',return_value={'qrcodeurl':'initial'}),patch.object(service,'_fresh_qr',return_value='scan'),patch('app.maintenance.subprocess.run') as run:
            service._perform('restart_napcat')
        self.assertEqual(run.call_args.args[0],['docker','restart','--timeout','15','daodao-card-bot-napcat-1'])
        self.assertEqual(service.operation['state'],'awaiting_scan')
    def test_arbitrary_command_is_not_an_action(self):
        service=Maintenance(Mock())
        with self.assertRaises(ValueError):service.action('restart nginx; echo bad','test-id-123')
        self.assertIsNone(service.operation)
    def test_credentials_and_cards_are_redacted(self):
        with patch.dict(os.environ,{'NAPCAT_TOKEN':'a-private-value'}):
            text=safe_text('a-private-value GMZZABCDEFGHIJKLMNOPQRSTUVWX https://example.test/token')
        self.assertNotIn('a-private-value',text)
        self.assertNotIn('GMZZ',text)
        self.assertNotIn('https://',text)
    def test_login_does_not_return_webui_credential_or_qr_when_online(self):
        with patch('app.maintenance.credential',return_value='private'),patch('app.maintenance.webui',return_value={'isLogin':True,'qrcodeurl':'secret-qr'}):
            self.assertEqual(Maintenance(Mock()).login(),{'online':True,'error':'','qr':None})
    def test_offline_state_is_not_inferred_from_account_metadata(self):
        service=Maintenance(Mock())
        self.assertIsNone(service.cache)
        # Login endpoint reports the actual WebUI isLogin value, not a cached QQ ID.
        with patch('app.maintenance.credential',return_value='private'),patch('app.maintenance.webui',return_value={'isLogin':False,'user_id':'3035610294'}):
            self.assertFalse(service.login()['online'])

if __name__=='__main__':unittest.main()
