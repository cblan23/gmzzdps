import os
import secrets
from pathlib import Path
os.umask(0o077)
for folder in ('data','data/napcat','data/qq','data/plugins','logs','backups'):
    Path(folder).mkdir(parents=True,exist_ok=True)
if not Path('.env').exists():
    value=Path('.env.example').read_text()
    for key in ('NAPCAT_TOKEN','NAPCAT_WEBUI_TOKEN','CARD_API_TOKEN'):
        value=value.replace(key+'=\n',key+'='+secrets.token_urlsafe(36)+'\n')
    Path('.env').write_text(value)
print('Deployment directories and .env ready; secrets not displayed')
