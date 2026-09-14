import sqlite3
from pathlib import Path

SCHEMA='''
CREATE TABLE IF NOT EXISTS cards(
 id INTEGER PRIMARY KEY, card_code TEXT NOT NULL UNIQUE, status TEXT NOT NULL DEFAULT 'available'
 CHECK(status IN ('available','claimed')), claimed_qq TEXT, claimed_at TEXT, expire_at TEXT);
CREATE TABLE IF NOT EXISTS claims(
 id INTEGER PRIMARY KEY, qq TEXT NOT NULL, group_id TEXT NOT NULL, claim_date TEXT NOT NULL,
 card_id INTEGER NOT NULL UNIQUE REFERENCES cards(id), claimed_at TEXT NOT NULL,
 UNIQUE(qq,claim_date));
CREATE TABLE IF NOT EXISTS requests(
 request_id TEXT PRIMARY KEY, qq TEXT NOT NULL, claim_id INTEGER NOT NULL REFERENCES claims(id));
CREATE INDEX IF NOT EXISTS available_cards ON cards(status,id);
'''

def connect(path):
    db=sqlite3.connect(path,timeout=5,isolation_level=None)
    db.row_factory=sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    db.execute('PRAGMA busy_timeout=5000')
    return db

def initialize(path):
    Path(path).parent.mkdir(parents=True,exist_ok=True)
    with connect(path) as db:
        db.execute('PRAGMA journal_mode=WAL')
        db.executescript(SCHEMA)
    db.close()
