"""Install reviewed generation module and add bot .env to existing backend."""
from pathlib import Path
import hashlib
import os
import shutil
import subprocess
import time
import urllib.request
import json
stage=Path('/tmp/daodao-generation')
target=Path('/opt/gmzz-dps-monitor/dps_monitor_server.py')
expected='f2f20074c6d47c029f01d77347a384e011599cdd6d1ea3d54e888af8f13ad93f'
assert hashlib.sha256(target.read_bytes()).hexdigest()==expected,'Backend changed; re-review before deploy'
stamp=time.strftime('%Y%m%d-%H%M%S')
backup=target.with_name(target.name+'.before-bot-generation-'+stamp)
shutil.copy2(target,backup)
module=target.with_name('bot_cards.py')
assert not module.exists()
dropin=Path('/etc/systemd/system/gmzz-dps-monitor.service.d/daodao-card-bot.conf')
assert not dropin.exists()
dropin.parent.mkdir(parents=True,exist_ok=True)
try:
    shutil.copyfile(stage/'bot_cards.py',module);os.chmod(module,0o644)
    shutil.copyfile(stage/'dps_monitor_server.py',target)
    dropin.write_text('[Service]\nEnvironmentFile=/opt/daodao-card-bot/.env\n')
    subprocess.run(['systemctl','daemon-reload'],check=True)
    subprocess.run(['systemctl','restart','gmzz-dps-monitor.service'],check=True,timeout=30)
    for _ in range(20):
        try:
            with urllib.request.urlopen('http://127.0.0.1:8766/api/v1/dps/health',timeout=2) as r:
                assert json.load(r)['ok'];break
        except Exception:time.sleep(.25)
    else:raise RuntimeError('Health check failed')
except BaseException:
    shutil.copy2(backup,target)
    if dropin.exists():dropin.unlink()
    subprocess.run(['systemctl','daemon-reload'],check=True)
    subprocess.run(['systemctl','restart','gmzz-dps-monitor.service'],check=True,timeout=30)
    raise
print('Generation backend installed; backup='+str(backup))
