"""Deploy maintenance after staging verification; preserve current nginx edits."""
import base64,json,os,shutil,subprocess,sys,time,urllib.request
from pathlib import Path

stage=Path(sys.argv[1]).resolve(strict=True)
assert stage.parent==Path('/tmp') and stage.name.startswith('daodao-maintenance-')
root=Path('/opt/daodao-card-bot')
stamp=time.strftime('%Y%m%d-%H%M%S')
backup=root/'backups'/('maintenance-'+stamp)
backup.mkdir(parents=True,mode=0o700)
files={'maintenance.py':root/'app/maintenance.py','qq-maintenance.js':root/'app/qq-maintenance.js',
       'daodao-qq-maintenance.service':Path('/etc/systemd/system/daodao-qq-maintenance.service')}
nginx=Path('/etc/nginx/conf.d/daodao-domain.conf')
original=nginx.read_text()
marker='# DAODAO_QQ_MAINTENANCE'
assert marker not in original
anchor='    location = /dps-monitor {\n'
assert original.count(anchor)==1
routes='''    # DAODAO_QQ_MAINTENANCE
    location ^~ /api/v1/dps/admin/qq/ {
        client_max_body_size 2k;
        proxy_pass http://127.0.0.1:8771;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_connect_timeout 2s;
        proxy_read_timeout 20s;
    }
    location = /dps-monitor/qq-maintenance.js {
        proxy_pass http://127.0.0.1:8771;
        proxy_set_header Host $host;
        proxy_connect_timeout 2s;
        proxy_read_timeout 10s;
    }

'''
inject='''        proxy_set_header Accept-Encoding "";
        sub_filter '</body>' '<script src="/dps-monitor/qq-maintenance.js?v=20260912-1"></script></body>';
        sub_filter_once on;
'''
patched=original.replace(anchor,routes+anchor+inject)
shutil.copy2(nginx,backup/'daodao-domain.conf')
for name,destination in files.items():
    if destination.exists():shutil.copy2(destination,backup/name)
    shutil.copyfile(stage/name,destination)
    destination.chmod(0o644)
subprocess.run([str(root/'.venv/bin/python'),'-m','py_compile',str(root/'app/maintenance.py')],check=True)
subprocess.run(['systemctl','daemon-reload'],check=True)
subprocess.run(['systemctl','enable','--now','daodao-qq-maintenance.service'],check=True)

# Read admin credentials only into memory; never print them.
pid=subprocess.check_output(['systemctl','show','gmzz-dps-monitor.service','-p','MainPID','--value'],text=True).strip()
env=dict(item.split(b'=',1) for item in Path('/proc/'+pid+'/environ').read_bytes().split(b'\0') if b'=' in item)
user=env.get(b'GMZZ_MONITOR_ADMIN_USER',b'admin')
password=env[b'GMZZ_MONITOR_ADMIN_PASSWORD']
authorization='Basic '+base64.b64encode(user+b':'+password).decode()
def get(url):
    request=urllib.request.Request(url,headers={'Authorization':authorization})
    with urllib.request.urlopen(request,timeout=18) as response:return response.read()
try:
    data=None
    for _ in range(10):
        try:
            data=json.loads(get('http://127.0.0.1:8771/api/v1/dps/admin/qq/status'));break
        except OSError:time.sleep(.5)
    assert data and data['bot_qq']=='3035610294' and 'csrf' in data
    nginx.write_text(patched)
    subprocess.run(['nginx','-t'],check=True)
    subprocess.run(['systemctl','reload','nginx'],check=True)
    for _ in range(12):
        page=get('https://daodaogame.vip/dps-monitor').decode()
        if '/dps-monitor/qq-maintenance.js?v=20260912-1' in page:break
        time.sleep(.5)
    assert '/dps-monitor/qq-maintenance.js?v=20260912-1' in page
    status=json.loads(get('https://daodaogame.vip/api/v1/dps/admin/qq/status'))
    assert status['bot_qq']=='3035610294'
    script=get('https://daodaogame.vip/dps-monitor/qq-maintenance.js')
    assert script==(root/'app/qq-maintenance.js').read_bytes()
    assert subprocess.check_output(['systemctl','show','gmzz-dps-monitor.service','-p','MainPID','--value'],text=True).strip()==pid
except BaseException:
    nginx.write_text(original)
    subprocess.run(['nginx','-t'],check=True)
    subprocess.run(['systemctl','reload','nginx'],check=True)
    print('Maintenance UI deployment rolled back; backup='+str(backup))
    raise
print(json.dumps({'deployed':True,'backup':str(backup),'qq':status['qq'],'services':status['services'],
                  'cards':status['cards'],'dps_service_not_restarted':True,'download_pause_preserved':'TEMPORARY_UPDATE_DOWNLOAD_PAUSE_20260912' in patched}))
