"""Verify authenticated recovery on an already-online bot without sending QQ messages."""
import base64,json,subprocess,time,urllib.request
from pathlib import Path
pid=subprocess.check_output(['systemctl','show','gmzz-dps-monitor.service','-p','MainPID','--value'],text=True).strip()
env=dict(row.split(b'=',1) for row in Path('/proc/'+pid+'/environ').read_bytes().split(b'\0') if b'=' in row)
authorization='Basic '+base64.b64encode(env.get(b'GMZZ_MONITOR_ADMIN_USER',b'admin')+b':'+env[b'GMZZ_MONITOR_ADMIN_PASSWORD']).decode()
def request(path,body=None,csrf=None):
    headers={'Authorization':authorization,'Origin':'https://daodaogame.vip'}
    if body is not None:headers['Content-Type']='application/json';headers['X-Daodao-CSRF']=csrf
    req=urllib.request.Request('https://daodaogame.vip/api/v1/dps/admin/qq/'+path,headers=headers,
                              data=json.dumps(body).encode() if body is not None else None)
    with urllib.request.urlopen(req,timeout=18) as response:return json.load(response)
def identity():
    return [subprocess.check_output(command,text=True).strip() for command in (
        ['docker','inspect','--format={{.State.StartedAt}}','daodao-card-bot-napcat-1'],
        ['systemctl','show','daodao-card-bot.service','-p','MainPID','--value'])]
state=request('status')
assert state['qq']['online'] is True and state['worker']['connected'] is True,'Do not disturb an offline bot during this verification'
before=identity()
rid='verify-online-'+str(time.time_ns())
request('action',{'action':'recover','request_id':rid},state['csrf'])
for _ in range(25):
    state=request('status')
    if state['operation']['state']!='running':break
    time.sleep(1)
assert state['operation']['state']=='done',state['operation']['message']
assert before==identity(),'Healthy online recovery must not restart QQ or worker'
assert state['worker']['connected'] is True
print(json.dumps({'online_recovery_verified':True,'qq_online':state['qq']['online'],'worker_connected':True,
                  'no_qq_restart':True,'no_worker_restart':True,'no_qq_messages_sent':True,
                  'cards':state['cards']}))
