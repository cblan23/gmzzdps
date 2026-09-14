"""Independent authoritative card API. No QQ framework dependency."""
import hmac
import json
import logging
import os
import re
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from app.config import Settings,qq_id
from app.services.local_card_service import CardPool
from app.services.card_service import CardError
from app.utils.logging import configure

class CardApiServer(ThreadingHTTPServer):
    daemon_threads=True
    def __init__(self,*args,**kwargs):
        self.slots=threading.BoundedSemaphore(24)
        super().__init__(*args,**kwargs)
    def process_request(self,request,address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:super().process_request(request,address)
        except BaseException:self.slots.release();raise
    def process_request_thread(self,request,address):
        try:super().process_request_thread(request,address)
        finally:self.slots.release()

def make_handler(settings,pool):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def reply(self,code,data):
            raw=json.dumps(data,ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header('Content-Type','application/json; charset=utf-8')
            self.send_header('Content-Length',str(len(raw)))
            self.send_header('Cache-Control','no-store')
            self.send_header('X-Content-Type-Options','nosniff')
            self.end_headers()
            try:self.wfile.write(raw)
            except OSError:pass
        def setup(self):
            super().setup();self.connection.settimeout(12)
        def do_GET(self):
            self.reply(200,dict(ok=True)) if self.path=='/health' else self.reply(404,dict(success=False))
        def do_POST(self):
            if not hmac.compare_digest(self.headers.get('Authorization',''),'Bearer '+settings.api_token):
                self.reply(401,dict(success=False,code='UNAUTHORIZED'));return
            try:
                length=int(self.headers.get('Content-Length','0'))
                if not 0<length<=4096:raise ValueError()
                value=json.loads(self.rfile.read(length))
                if not isinstance(value,dict):raise ValueError()
                if self.path=='/api/card/claim':
                    qq,group=qq_id(value.get('qq','')),qq_id(value.get('group_id',''))
                    if group not in settings.groups:
                        self.reply(403,dict(success=False,code='GROUP_NOT_ALLOWED'));return
                    rid=str(value.get('request_id',''))
                    if not rid:
                        # Simple external callers still get QQ/date idempotency.
                        import uuid
                        rid=uuid.uuid4().hex
                    if not re.fullmatch(r'[a-zA-Z0-9:_-]{1,128}',rid):raise ValueError()
                    result=pool.claim(qq,group,rid)
                    logging.getLogger('daodao.api').info('claim qq=%s group=%s repeat=%s',qq,group,result['already_claimed'])
                    self.reply(200,result)
                elif self.path=='/api/card/stats':
                    if qq_id(value.get('admin_qq','')) not in settings.admins:
                        self.reply(403,dict(success=False,code='ADMIN_REQUIRED'));return
                    self.reply(200,pool.stats_for_admin(str(value['admin_qq'])) if hasattr(pool,'stats_for_admin') else pool.stats())
                else:self.reply(404,dict(success=False,code='NOT_FOUND'))
            except CardError as error:self.reply(200,dict(success=False,code=error.code))
            except (ValueError,TypeError):self.reply(400,dict(success=False,code='BAD_REQUEST'))
            except sqlite3.OperationalError:self.reply(503,dict(success=False,code='DATABASE_BUSY'))
            except Exception:
                logging.getLogger('daodao.api').error('request_error')
                self.reply(500,dict(success=False,code='INTERNAL_ERROR'))
    return Handler

def main():
    os.umask(0o077)
    configure()
    settings=Settings.read()
    if os.getenv('CARD_ISSUANCE','generated')=='generated':
        from app.services.generated_card_service import GeneratedCardPool
        pool=GeneratedCardPool(settings.api_token)
    else:pool=CardPool(settings.db)
    server=CardApiServer((os.getenv('CARD_API_BIND','127.0.0.1'),int(os.getenv('CARD_API_PORT','8770'))),make_handler(settings,pool))
    server.daemon_threads=True
    logging.getLogger('daodao.api').info('api_started groups=%s',len(settings.groups))
    server.serve_forever()

if __name__=='__main__':main()
