import asyncio
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from http.server import ThreadingHTTPServer
from urllib.request import Request,urlopen
from urllib.error import HTTPError
from app.api import make_handler
from app.services.local_card_service import CardPool
from app.services.remote_card_service import RemoteCardApiService

class ApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.pool=CardPool(str(Path(self.tmp.name)/'api.db'))
        self.pool.import_cards(['DAODAO-API-CARD-0001'])
        self.token='test-token-'*5
        self.server=ThreadingHTTPServer(('127.0.0.1',0),make_handler(
            SimpleNamespace(api_token=self.token,groups={'999999'},admins={'777777'}),self.pool))
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.url=f'http://127.0.0.1:{self.server.server_port}/api/card/claim'
    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join();self.tmp.cleanup()
    def request(self,token,group):
        request=Request(self.url,data=json.dumps(dict(qq='123456',group_id=group)).encode(),
                        headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'})
        try:
            with urlopen(request,timeout=3) as r:return r.status,json.load(r)
        except HTTPError as e:return e.code,json.load(e)
    def test_auth_and_server_group_whitelist(self):
        self.assertEqual(self.request('bad','999999')[0],401)
        self.assertEqual(self.request(self.token,'888888')[0],403)
        self.assertEqual(self.pool.stats()['claimed'],0)
    def test_remote_client_end_to_end(self):
        async def run():
            service=RemoteCardApiService(self.url,self.token)
            first=await service.claim('123456','999999','one')
            again=await service.claim('123456','999999','two')
            self.assertEqual(first['card'],again['card'])
            self.assertTrue(again['already_claimed'])
            self.assertEqual((await service.stats('777777'))['claimed'],1)
        asyncio.run(run())
