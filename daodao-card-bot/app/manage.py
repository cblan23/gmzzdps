import argparse
import os
import sqlite3
from pathlib import Path
from datetime import datetime
from app.config import load_env
from app.services.local_card_service import CardPool

def main():
    os.umask(0o077);load_env()
    p=argparse.ArgumentParser()
    p.add_argument('command',choices=['init','import','stats','backup'])
    p.add_argument('--file',type=Path)
    args=p.parse_args()
    path=os.getenv('CARD_DB','data/cards.db')
    pool=CardPool(path)
    if args.command=='import':
        if not args.file:p.error('--file required')
        print('imported',pool.import_cards(args.file.read_text(encoding='utf-8-sig').splitlines()))
    elif args.command=='stats':print(pool.stats())
    elif args.command=='backup':
        Path('backups').mkdir(exist_ok=True)
        dest=Path('backups')/('cards-'+datetime.now().strftime('%Y%m%d-%H%M%S')+'.db')
        if dest.exists():raise ValueError('Backup already exists')
        with sqlite3.connect(path) as src,sqlite3.connect(dest) as out:src.backup(out)
        print('backup',dest)

if __name__=='__main__':main()
