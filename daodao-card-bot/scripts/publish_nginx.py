"""Add only dedicated card API locations; validate and gracefully reload."""
from pathlib import Path
import shutil
import subprocess
import time
target=Path('/etc/nginx/conf.d/daodao-domain.conf')
text=target.read_text()
marker='    # Daodao Card Bot API (loopback upstream)'
if marker in text:
    print('Card API route already installed')
    raise SystemExit(0)
needle='    listen 443 ssl;'
assert text.count(needle)==1
block='''
    # Daodao Card Bot API (loopback upstream)
    location ~ ^/api/card/(claim|stats)$ {
        limit_except POST { deny all; }
        client_max_body_size 4k;
        proxy_pass http://127.0.0.1:8770;
        proxy_connect_timeout 2s;
        proxy_read_timeout 12s;
        proxy_send_timeout 12s;
        proxy_request_buffering on;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
'''
backup=target.with_name(target.name+'.before-card-bot-'+time.strftime('%Y%m%d-%H%M%S'))
assert not backup.exists()
shutil.copy2(target,backup)
try:
    target.write_text(text.replace(needle,needle+block))
    subprocess.run(['nginx','-t'],check=True)
    subprocess.run(['systemctl','reload','nginx'],check=True)
except BaseException:
    shutil.copy2(backup,target)
    raise
print('Card API route installed; backup='+str(backup))
