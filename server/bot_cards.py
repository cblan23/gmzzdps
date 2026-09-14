"""QQ daily issuance in the SAME transaction/database as real DPS cards."""
from datetime import datetime,timezone,timedelta
import sqlite3
CHINA=timezone(timedelta(hours=8))
CARD_TYPE='bot_8h'
CARD_NOTE='机器人发卡专用'
DURATION=8*3600

def initialize(connection):
    connection.execute('''CREATE TABLE IF NOT EXISTS bot_card_claims(
        id INTEGER PRIMARY KEY, qq TEXT NOT NULL,group_id TEXT NOT NULL,claim_date TEXT NOT NULL,
        card_hash TEXT NOT NULL UNIQUE REFERENCES cards(card_hash),claimed_at REAL NOT NULL,
        UNIQUE(qq,claim_date))''')
    connection.execute('''CREATE TABLE IF NOT EXISTS bot_card_requests(
        request_id TEXT PRIMARY KEY,qq TEXT NOT NULL,claim_id INTEGER NOT NULL REFERENCES bot_card_claims(id))''')

def claim(connection,qq,group,request_id,timestamp,generate,digest):
    day=datetime.fromtimestamp(timestamp,CHINA).date().isoformat()
    previous=connection.execute('SELECT c.* FROM bot_card_requests r JOIN bot_card_claims c ON c.id=r.claim_id WHERE request_id=?',(request_id,)).fetchone()
    if previous and previous['qq']!=qq:raise ValueError('request conflict')
    previous=previous or connection.execute('SELECT * FROM bot_card_claims WHERE qq=? AND claim_date=?',(qq,day)).fetchone()
    repeated=previous is not None
    if previous:claim_id=previous['id']
    else:
        for _ in range(10):
            key=generate();card_hash=digest(key)
            try:
                connection.execute('''INSERT INTO cards(card_hash,card_key,card_suffix,duration_seconds,created_at,permanent,note,remark)
                    VALUES(?,?,?,?,?,0,?,?)''',(card_hash,key,key[-4:],DURATION,timestamp,CARD_NOTE,'QQ群自动领取'))
                break
            except sqlite3.IntegrityError:continue
        else:raise RuntimeError('Unable to allocate unique card')
        claim_id=connection.execute('INSERT INTO bot_card_claims(qq,group_id,claim_date,card_hash,claimed_at) VALUES(?,?,?,?,?)',
                  (qq,group,day,card_hash,timestamp)).lastrowid
    connection.execute('INSERT OR IGNORE INTO bot_card_requests(request_id,qq,claim_id) VALUES(?,?,?)',(request_id,qq,claim_id))
    card=connection.execute('SELECT c.card_key,c.expires_at,c.duration_seconds,q.claim_date FROM bot_card_claims q JOIN cards c ON c.card_hash=q.card_hash WHERE q.id=?',(claim_id,)).fetchone()
    expiry=datetime.fromtimestamp(card['expires_at'],CHINA).strftime('%Y-%m-%d %H:%M:%S') if card['expires_at'] else None
    return dict(success=True,already_claimed=repeated,card=card['card_key'],expire_at=expiry,
                duration_seconds=card['duration_seconds'],activation_policy='first_login',claim_date=card['claim_date'])

def stats(connection,timestamp):
    day=datetime.fromtimestamp(timestamp,CHINA).date().isoformat()
    count=connection.execute('SELECT COUNT(*),SUM(claim_date=?) FROM bot_card_claims',(day,)).fetchone()
    return dict(success=True,total=count[0],claimed=count[0],remaining=None,today_claimed=count[1] or 0,mode='generated')
