import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timedelta
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from app.services.local_card_service import CHINA_TZ
from app.services.local_card_service import CardPool,LocalCardService
from app.services.card_service import CardError
from app.bot.handlers import BotHandler
from app.bot.commands import parse_command

class PoolTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.pool=CardPool(str(Path(self.tmp.name)/'pool.db'))
        self.pool.import_cards([f'DAODAO-TEST-{i:08}' for i in range(40)])
    def tearDown(self):self.tmp.cleanup()
    def test_same_qq_concurrent(self):
        with ThreadPoolExecutor(max_workers=20) as executor:
            rows=list(executor.map(lambda i:self.pool.claim('123456','999999',str(i)),range(20)))
        self.assertEqual(len({r['card'] for r in rows}),1)
        self.assertEqual(sum(not r['already_claimed'] for r in rows),1)
        self.assertEqual(self.pool.stats()['claimed'],1)
    def test_twenty_members_get_distinct_cards(self):
        with ThreadPoolExecutor(max_workers=20) as executor:
            rows=list(executor.map(lambda i:self.pool.claim(str(100000+i),'999999',str(i)),range(20)))
        self.assertEqual(len({r['card'] for r in rows}),20)
    def test_midnight_retry_and_next_day(self):
        now=datetime(2026,9,12,23,59,59,tzinfo=CHINA_TZ)
        self.pool.clock=lambda:now
        first=self.pool.claim('123456','999999','original')
        now+=timedelta(seconds=2)
        retry=self.pool.claim('123456','999999','original')
        second=self.pool.claim('123456','999999','new-message')
        self.assertEqual(first['card'],retry['card'])
        self.assertNotEqual(first['card'],second['card'])
    def test_empty_and_import_idempotent(self):
        empty=CardPool(str(Path(self.tmp.name)/'empty.db'))
        with self.assertRaises(CardError) as e:empty.claim('123456','999999','one')
        self.assertEqual(e.exception.code,'CARD_POOL_EMPTY')
        self.assertEqual(self.pool.import_cards(['DAODAO-TEST-00000000']),0)

class BotTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.service=LocalCardService(str(Path(self.tmp.name)/'bot.db'))
        self.service.pool.import_cards(['DAODAO-PRIVATE-CARD-0001'])
        self.groups=[];self.private=[];self.fail=False
        outer=self
        class Api:
            async def send_group_msg(self,group,message):outer.groups.append(message);return '1'
            async def send_private_msg(self,qq,message,*,group_id):
                assert group_id=='999999'
                outer.private.append((qq,message))
                if outer.fail:raise RuntimeError('failure')
                return '2'
        self.handler=BotHandler(SimpleNamespace(bot_qq='555555',groups={'999999'},admins={'777777'}),self.service,Api())
    async def asyncTearDown(self):self.tmp.cleanup()
    def event(self,group='999999',qq='123456',text='领卡'):
        return SimpleNamespace(self_id='555555',user_id=qq,group_id=group,message_id='100',anonymous=None,
            message=[{'type':'at','data':{'qq':'555555'}},{'type':'text','data':{'text':text}}])
    async def test_failed_private_keeps_original_card(self):
        self.fail=True
        await self.handler.handle(self.event())
        self.fail=False
        await self.handler.handle(self.event())
        self.assertEqual(self.service.pool.stats()['claimed'],1)
        self.assertEqual(self.private[0],self.private[1])
        self.assertNotIn('DAODAO-PRIVATE',str(self.groups))
        self.assertIn('重新发送',str(self.groups[-1]))
    async def test_foreign_group_and_non_admin(self):
        await self.handler.handle(self.event(group='888888'))
        await self.handler.handle(self.event(text='卡池'))
        await self.handler.handle(self.event(text='领卡 654321'))
        self.assertEqual(self.service.pool.stats()['claimed'],0)
        self.assertFalse(self.private)
    def test_requires_actual_at_segment(self):
        self.assertIsNone(parse_command([{'type':'text','data':{'text':'@叨叨助手 领卡'}}],'555555'))

if __name__=='__main__':unittest.main()
