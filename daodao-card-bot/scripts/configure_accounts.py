"""Set nonsecret deployment identities; preserve tokens and other settings."""
import argparse
from pathlib import Path
from app.config import qq_id,ids
p=argparse.ArgumentParser()
p.add_argument('--bot',required=True)
p.add_argument('--admins',required=True)
p.add_argument('--groups')
a=p.parse_args()
updates=dict(BOT_QQ=qq_id(a.bot),ADMIN_QQS=','.join(sorted(ids(a.admins))))
if a.groups is not None:updates['ALLOWED_GROUPS']=','.join(sorted(ids(a.groups)))
path=Path('.env');lines=path.read_text().splitlines()
for key,value in updates.items():
    if any(line.startswith(key+'=') for line in lines):lines=[key+'='+value if line.startswith(key+'=') else line for line in lines]
    else:lines.append(key+'='+value)
path.write_text('\n'.join(lines)+'\n')
print('Configured account/group identities; tokens preserved')
