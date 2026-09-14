"""Pin a fail-closed private route; refuses unknown upstream code shapes."""
import hashlib
from pathlib import Path
import subprocess
import os
import zipfile
root=Path('/opt/daodao-card-bot')
dest=root/'data/runtime/napcat.mjs'
dest.parent.mkdir(parents=True,exist_ok=True)
original=root/'data/runtime/napcat.original.mjs'
if not original.exists():
    from app.config import load_env
    load_env()
    image=os.environ.get('NAPCAT_IMAGE','mlikiowa/napcat-docker:daodao-verified')
    temporary=subprocess.check_output(['docker','create',image],text=True).strip()
    if len(temporary)!=64 or not all(c in '0123456789abcdef' for c in temporary):
        raise RuntimeError('Invalid temporary container ID')
    try:
        copied=subprocess.run(['docker','cp',temporary+':/app/napcat/napcat.mjs',str(original)],capture_output=True)
        if copied.returncode:
            archive=root/'data/runtime/napcat-upstream.zip'
            subprocess.run(['docker','cp',temporary+':/app/NapCat.Shell.zip',str(archive)],check=True)
            with zipfile.ZipFile(archive) as bundle:original.write_bytes(bundle.read('napcat.mjs'))
    finally:
        subprocess.run(['docker','rm',temporary],check=True,stdout=subprocess.DEVNULL)
text=original.read_text()
start=text.index('async function F2(')
end=text.index('\nfunction aye(',start)
function=text[start:end]
needle='if (!r) {\n      if (e.group_id)'
assert function.count(needle)==1,'Unknown NapCat private-route version; refusing patch'
patched=function.replace(needle,'if (!r) {\n      if (n !== 1 && e.group_id)')
dest.write_text(text[:start]+patched+text[end:])
dest.chmod(0o644)
print('Private-route guard prepared; sha256='+hashlib.sha256(dest.read_bytes()).hexdigest())
