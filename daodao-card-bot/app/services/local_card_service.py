import asyncio
import re
from datetime import datetime,timezone,timedelta
CHINA_TZ=timezone(timedelta(hours=8), 'Asia/Shanghai')
from app.database.database import connect,initialize
from app.services.card_service import CardError

class CardPool:
    def __init__(self,path,clock=None):
        self.path=path
        self.clock=clock or (lambda:datetime.now(CHINA_TZ))
        initialize(path)

    def import_cards(self,codes):
        codes=list(dict.fromkeys(c.strip() for c in codes if c.strip()))
        if any(not re.fullmatch(r'[A-Za-z0-9-]{10,128}',c) for c in codes): raise CardError('INVALID_CARD_IMPORT')
        db=connect(self.path)
        try:
            db.execute('BEGIN IMMEDIATE')
            before=db.total_changes
            db.executemany('INSERT OR IGNORE INTO cards(card_code) VALUES(?)',[(c,) for c in codes])
            added=db.total_changes-before
            db.commit()
            return added
        except BaseException:
            db.rollback();raise
        finally:db.close()

    def claim(self,qq,group_id,request_id):
        now=self.clock().astimezone(CHINA_TZ)
        day=now.date().isoformat()
        db=connect(self.path)
        try:
            db.execute('BEGIN IMMEDIATE')
            previous=db.execute('SELECT c.* FROM requests r JOIN claims c ON c.id=r.claim_id WHERE r.request_id=?',(request_id,)).fetchone()
            if previous and previous['qq']!=qq: raise CardError('REQUEST_CONFLICT')
            previous=previous or db.execute('SELECT * FROM claims WHERE qq=? AND claim_date=?',(qq,day)).fetchone()
            repeat=previous is not None
            if not previous:
                card=db.execute("SELECT * FROM cards WHERE status='available' AND (expire_at IS NULL OR expire_at>?) ORDER BY id LIMIT 1",(now.isoformat(),)).fetchone()
                if not card: raise CardError('CARD_POOL_EMPTY')
                db.execute("UPDATE cards SET status='claimed',claimed_qq=?,claimed_at=? WHERE id=?",(qq,now.isoformat(),card['id']))
                claim_id=db.execute('INSERT INTO claims(qq,group_id,claim_date,card_id,claimed_at) VALUES(?,?,?,?,?)',
                    (qq,group_id,day,card['id'],now.isoformat())).lastrowid
            else:claim_id=previous['id']
            db.execute('INSERT OR IGNORE INTO requests(request_id,qq,claim_id) VALUES(?,?,?)',(request_id,qq,claim_id))
            row=db.execute('SELECT c.card_code,c.expire_at,q.claim_date FROM claims q JOIN cards c ON c.id=q.card_id WHERE q.id=?',(claim_id,)).fetchone()
            db.commit()
            return dict(success=True,already_claimed=repeat,card=row['card_code'],expire_at=row['expire_at'],claim_date=row['claim_date'])
        except BaseException:
            db.rollback();raise
        finally:db.close()

    def stats(self):
        db=connect(self.path)
        try:
            row=db.execute("SELECT COUNT(*) AS total,SUM(status='claimed') AS claimed,SUM(status='available') AS remaining FROM cards").fetchone()
            today=db.execute('SELECT COUNT(*) FROM claims WHERE claim_date=?',(self.clock().astimezone(CHINA_TZ).date().isoformat(),)).fetchone()[0]
            return dict(success=True,total=row['total'],claimed=row['claimed'] or 0,remaining=row['remaining'] or 0,today_claimed=today)
        finally:db.close()

class LocalCardService:
    def __init__(self,path):self.pool=CardPool(path)
    async def claim(self,qq,group_id,request_id):return await asyncio.to_thread(self.pool.claim,qq,group_id,request_id)
    async def stats(self,admin_qq):return await asyncio.to_thread(self.pool.stats)
