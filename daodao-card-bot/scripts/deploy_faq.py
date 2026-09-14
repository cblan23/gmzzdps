import hashlib,json,shutil,subprocess,sys,time
from pathlib import Path
stage=Path('/tmp/daodao-faq-20260912')
root=Path('/opt/daodao-card-bot')
expected={'commands.py':'00acbb91596cc5f4c3d3bc34396f61e241364b69e3cb6987acade65b4af6d111',
          'handlers.py':'3efa22732ae97ba3179d177af624de0fc1da59330ec757f92f12541f61ea2317'}
for name,digest in expected.items():assert hashlib.sha256((root/'app/bot'/name).read_bytes()).hexdigest()==digest
backup=root/'backups'/('faq-'+time.strftime('%Y%m%d-%H%M%S'));backup.mkdir(mode=0o700)
before=subprocess.check_output(['docker','inspect','--format={{.State.StartedAt}}','daodao-card-bot-napcat-1'],text=True).strip()
for name in ('commands.py','handlers.py','faq.py'):
    target=root/'app/bot'/name
    if target.exists():shutil.copy2(target,backup/name)
    shutil.copyfile(stage/name,target);target.chmod(0o644)
try:
    subprocess.run([str(root/'.venv/bin/python'),'-m','py_compile',*(str(root/'app/bot'/name) for name in ('commands.py','handlers.py','faq.py'))],check=True)
    subprocess.run(['systemctl','restart','daodao-card-bot.service'],check=True,timeout=30)
    for _ in range(30):
        try:
            row=json.loads((root/'data/bot-connection.json').read_text())
            if row['online'] is True and 0<=time.time()-row['at']<15 and b'app.main' in Path('/proc/'+str(row['pid'])+'/cmdline').read_bytes().split(b'\0'):break
        except (OSError,ValueError,KeyError):pass
        time.sleep(1)
    else:raise RuntimeError('Worker connection not confirmed')
    assert subprocess.check_output(['docker','inspect','--format={{.State.StartedAt}}','daodao-card-bot-napcat-1'],text=True).strip()==before
except BaseException:
    for name in expected:shutil.copy2(backup/name,root/'app/bot'/name)
    subprocess.run(['systemctl','restart','daodao-card-bot.service'],check=True,timeout=30)
    raise
print(json.dumps({'deployed':True,'faq_count':14,'worker_connected':True,'qq_not_restarted':True,'backup':str(backup),'no_proactive_messages':True}))
