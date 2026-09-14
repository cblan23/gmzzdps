"""Consistent backups of the real issuance ledger and optional local test pool."""
import os
import sqlite3
from datetime import datetime
from pathlib import Path
os.umask(0o077)
root=Path('backups');root.mkdir(exist_ok=True)
stamp=datetime.now().strftime('%Y%m%d-%H%M%S')
for source in (Path('/var/lib/gmzz-dps-monitor/sessions.sqlite3'),Path('data/cards.db')):
    if not source.exists():continue
    dest=root/(source.stem+'-'+stamp+'.db')
    if dest.exists():raise RuntimeError('Backup path already exists')
    src=sqlite3.connect(source.resolve().as_uri()+'?mode=ro',uri=True)
    out=sqlite3.connect(dest)
    try:
        src.backup(out)
        if out.execute('PRAGMA quick_check').fetchone()[0]!='ok':raise RuntimeError('Backup check failed')
    finally:src.close();out.close()
    print('Verified database backup:',dest)
