"""Read-only production checks plus rejected invalid actions; never sends QQ messages."""
import base64,json,subprocess,urllib.request,urllib.error
from pathlib import Path
pid=subprocess.check_output(['systemctl','show','gmzz-dps-monitor.service','-p','MainPID','--value'],text=True).strip()
env=dict(row.split(b'=',1) for row in Path('/proc/'+pid+'/environ').read_bytes().split(b'\0') if b'=' in row)
user=env.get(b'GMZZ_MONITOR_ADMIN_USER',b'admin');password=env[b'GMZZ_MONITOR_ADMIN_PASSWORD']
authorization='Basic '+base64.b64encode(user+b':'+password).decode()
base='https://daodaogame.vip/api/v1/dps/admin/qq/'
def request(path,auth=True,body=None,csrf=None):
    headers={'Origin':'https://daodaogame.vip'}
    if auth:headers['Authorization']=authorization
    if csrf:headers['X-Daodao-CSRF']=csrf
    if body is not None:headers['Content-Type']='application/json'
    req=urllib.request.Request(base+path,headers=headers,data=json.dumps(body).encode() if body is not None else None)
    try:response=urllib.request.urlopen(req,timeout=18)
    except urllib.error.HTTPError as error:response=error
    with response:
        raw=response.read()
        assert password not in raw
        return response.status,json.loads(raw)
assert request('status',auth=False)[0]==401
code,state=request('status');assert code==200
for secret_name in (b'NAPCAT_TOKEN',b'CARD_API_TOKEN',b'NAPCAT_WEBUI_TOKEN'):
    if env.get(secret_name):assert env[secret_name].decode() not in json.dumps(state)
assert request('action',body={'action':'restart_bot','request_id':'verification-no-csrf'})[0]==403
assert request('action',body={'action':'not_a_valid_action','request_id':'verification-invalid-action'},csrf=state['csrf'])[0]==400
code,login=request('login');assert code==200 and 'qr' in login
assert 'Credential' not in login
print(json.dumps({'admin_required':True,'csrf_required':True,'invalid_action_rejected':True,'credentials_not_exposed':True,'qq_online':state['qq']['online'],'login_online':login['online'],'qr_available':bool(login['qr']),'card_api':state['cards'],'no_qq_messages_sent':True}))
