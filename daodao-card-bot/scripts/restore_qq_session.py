"""One normal QQ quick-login attempt using NapCat's existing saved session."""
import os
from scripts.check_webui_status import call,credential,state
if not state.get('isLogin'):
    result=call('QQLogin/SetQuickLogin',{'uin':os.environ['BOT_QQ']},credential)
    print('saved_session_login_accepted',result.get('code')==0)
