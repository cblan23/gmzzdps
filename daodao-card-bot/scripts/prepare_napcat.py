"""Render NapCat runtime config from .env; never print credentials."""
import json
import os
from pathlib import Path
from app.config import Settings

os.umask(0o077)
settings=Settings.read()
token=os.environ.get('NAPCAT_WEBUI_TOKEN','')
if len(token)<32 or len(settings.ws_token)<32:raise ValueError('Set both NapCat tokens to 32+ characters')
path=Path('data/napcat');path.mkdir(parents=True,exist_ok=True)
onebot={'network':{'httpServers':[],'httpSseServers':[],'httpClients':[],
    'websocketServers':[{'enable':True,'name':'daodao-local','host':'0.0.0.0','port':3001,
      'reportSelfMessage':False,'enableForcePushEvent':True,'messagePostFormat':'array',
      'token':settings.ws_token,'debug':False,'heartInterval':30000}], 'websocketClients':[],'plugins':[]},
      'musicSignUrl':'','enableLocalFile2Url':False,'parseMultMsg':False}
for name in ('onebot11.json',*(['onebot11_'+settings.bot_qq+'.json'] if settings.bot_qq else [])):
    (path/name).write_text(json.dumps(onebot),encoding='utf-8')
(path/'webui.json').write_text(json.dumps(dict(host='0.0.0.0',prefix='',port=6099,token=token,loginRate=3)),encoding='utf-8')
# Disable upstream console/file event logs which can contain private cards.
(path/'napcat.json').write_text(json.dumps(dict(fileLog=False,consoleLog=False,fileLogLevel='error',consoleLogLevel='error')),
                              encoding='utf-8')
print('NapCat configuration prepared; values hidden')
