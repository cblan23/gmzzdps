"""Deploy recovery UI/worker telemetry without logging out the online QQ account."""
import json,os,shutil,subprocess,sys,time
from pathlib import Path

stage=Path(sys.argv[1]).resolve(strict=True)
assert stage.parent==Path('/tmp') and stage.name.startswith('daodao-recovery-')
root=Path('/opt/daodao-card-bot')
backup=root/'backups'/('recovery-workflow-'+time.strftime('%Y%m%d-%H%M%S'))
backup.mkdir(mode=0o700)
names=('main.py','maintenance.py','qq-maintenance.js')
before=subprocess.check_output(['docker','inspect','--format={{.State.StartedAt}}','daodao-card-bot-napcat-1'],text=True).strip()
for name in names:
    shutil.copy2(root/'app'/name,backup/name)
    shutil.copyfile(stage/name,root/'app'/name)
    (root/'app'/name).chmod(0o644)
try:
    subprocess.run([str(root/'.venv/bin/python'),'-m','py_compile',str(root/'app/main.py'),str(root/'app/maintenance.py')],check=True)
    subprocess.run(['systemctl','restart','daodao-card-bot.service'],check=True,timeout=30)
    subprocess.run(['systemctl','restart','daodao-qq-maintenance.service'],check=True,timeout=20)
    for _ in range(30):
        try:
            row=json.loads((root/'data/bot-connection.json').read_text())
            if row.get('online') is True and 0<=time.time()-row['at']<20:
                args=Path('/proc/'+str(row['pid'])+'/cmdline').read_bytes().split(b'\0')
                if b'app.main' in args:break
        except (OSError,ValueError,KeyError):pass
        time.sleep(1)
    else:raise RuntimeError('Fresh worker connection not confirmed')
    after=subprocess.check_output(['docker','inspect','--format={{.State.StartedAt}}','daodao-card-bot-napcat-1'],text=True).strip()
    assert before==after,'QQ container unexpectedly restarted'
except BaseException:
    for name in names:shutil.copy2(backup/name,root/'app'/name)
    subprocess.run(['systemctl','restart','daodao-card-bot.service'],check=True,timeout=30)
    subprocess.run(['systemctl','restart','daodao-qq-maintenance.service'],check=True,timeout=20)
    print('Recovery workflow rolled back; QQ session retained')
    raise
print(json.dumps({'deployed':True,'backup':str(backup),'worker_connected':True,'qq_container_not_restarted':True}))
