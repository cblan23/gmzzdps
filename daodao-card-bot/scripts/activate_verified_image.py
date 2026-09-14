"""Select verified offline-loaded official image without exposing other config."""
from pathlib import Path
import subprocess
import json
image='mlikiowa/napcat-docker:daodao-verified'
identity=json.loads(subprocess.check_output(['docker','image','inspect',image],text=True))[0]['Id']
expected='sha256:b132563daa43114c9796540fc5ed25843d62c78676e8bef7f9bfa639d1c1467c'
manifest='sha256:406611383c31cc102665207b13cf0a4c2b463e27e300ba6ee5e7cb29adabd93f'
# Docker's containerd image store exposes the verified manifest as image ID;
# the classic store exposes its config digest. Both are pinned above.
if identity not in (expected,manifest):raise RuntimeError('Loaded image does not match verified registry image')
path=Path('.env')
lines=path.read_text().splitlines()
lines=[('NAPCAT_IMAGE='+image) if line.startswith('NAPCAT_IMAGE=') else line for line in lines]
path.write_text('\n'.join(lines)+'\n')
print('Verified official image selected:',identity)
