"""Read sanitized QQ failure, then request one normal QR refresh."""
import json,os,re
from scripts.check_webui_status import call,credential,state

def safe(value):
    text=str(value or '')
    for key in ('NAPCAT_WEBUI_TOKEN','NAPCAT_TOKEN','CARD_API_TOKEN'):
        secret=os.environ.get(key)
        if secret:text=text.replace(secret,'[redacted]')
    text=re.sub(r'https?://\S+','[link omitted]',text)
    return text[:1200]

print(json.dumps({'login_error':safe(state.get('loginError'))},ensure_ascii=True))
if not state.get('isLogin'):
    reply=call('QQLogin/RefreshQRcode',{},credential)
    print(json.dumps({'refresh_code':reply.get('code'),'message':safe(reply.get('message'))}))
