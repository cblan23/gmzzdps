"""Report login state without printing management credentials or QR URLs."""
import hashlib,json,os,urllib.request
from app.config import load_env
load_env()
def call(path,body,credential=None):
    headers={'Content-Type':'application/json'}
    if credential:headers['Authorization']='Bearer '+credential
    req=urllib.request.Request('http://127.0.0.1:6099/api/'+path,
                              data=json.dumps(body).encode(),headers=headers)
    with urllib.request.urlopen(req,timeout=8) as response:return json.load(response)
login=call('auth/login',{'hash':hashlib.sha256((os.environ['NAPCAT_WEBUI_TOKEN']+'.napcat').encode()).hexdigest()})
credential=(login.get('data') or {}).get('Credential')
if not credential:raise RuntimeError('WebUI authentication not available')
state=(call('QQLogin/CheckLoginStatus',{},credential).get('data') or {})
print(json.dumps(dict(logged_in=bool(state.get('isLogin')),offline=bool(state.get('isOffline')),
    has_login_error=bool(state.get('loginError')),has_qr=bool(state.get('qrcodeurl')))))
