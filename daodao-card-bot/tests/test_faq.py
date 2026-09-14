import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
from app.bot.faq import FAQ, HELP, answer
from app.bot.commands import parse_command
from app.bot.handlers import BotHandler

def message(text,at=True):
    return ([{'type':'at','data':{'qq':'555555'}}] if at else [])+[{'type':'text','data':{'text':text}}]

class FaqCommandsTests(unittest.TestCase):
    def test_all_approved_questions_and_numbered_menu(self):
        self.assertEqual(len(FAQ),14)
        for index,(key,title,aliases,response) in enumerate(FAQ,1):
            for alias in aliases:
                with self.subTest(alias=alias):
                    self.assertEqual(parse_command(message(alias+'？'),'555555'),'faq:'+key)
                    self.assertEqual(answer('faq:'+key),response)
            self.assertEqual(parse_command(message(f'帮助 {index}'),'555555'),'faq:'+key)
            self.assertIn(title,HELP)
    def test_help_entrances(self):
        for text in ('帮助','菜单','使用帮助','常见问题','help',''):
            self.assertEqual(parse_command(message(text),'555555'),'faq:help')
    def test_original_commands_keep_priority(self):
        for text in ('领卡','卡池','状态','查询','重置','补卡'):
            self.assertEqual(parse_command(message(text),'555555'),text)
        self.assertIsNone(parse_command(message('领卡 123456'),'555555'))
    def test_no_at_and_unrelated_chat_ignored(self):
        self.assertIsNone(parse_command(message('怎么更新',False),'555555'))
        self.assertIsNone(parse_command(message('随便聊聊天'),'555555'))
    def test_other_mention_and_images_do_not_trigger(self):
        self.assertIsNone(parse_command(message('下载')+[{'type':'at','data':{'qq':'888888'}}],'555555'))
        self.assertIsNone(parse_command(message('下载')+[{'type':'image','data':{}}],'555555'))
    def test_donation_url_is_exact_and_not_card_command(self):
        self.assertIn('https://wzyp.cn/shop/QDAWZ5KZ',answer('faq:card'))
        self.assertEqual(parse_command(message('卡号'),'555555'),'faq:card')

class FaqHandlerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.api=SimpleNamespace(send_group_msg=AsyncMock(return_value='1'),send_private_msg=AsyncMock(return_value='2'))
        self.service=SimpleNamespace(claim=AsyncMock(),stats=AsyncMock())
        self.settings=SimpleNamespace(bot_qq='555555',groups={'999999'},admins={'777777'})
        self.handler=BotHandler(self.settings,self.service,self.api)
    def event(self,text,group='999999',qq='123456'):
        return SimpleNamespace(self_id='555555',user_id=qq,group_id=group,message_id='1',anonymous=None,message=message(text))
    async def test_faq_never_allocates_or_sends_private_card(self):
        for index,(_,title,_,response) in enumerate(FAQ):
            await self.handler.handle(self.event(title,qq=str(123456+index)))
            self.assertEqual(self.api.send_group_msg.call_args.args[1][1]['data']['text'],' '+response)
        self.service.claim.assert_not_awaited();self.service.stats.assert_not_awaited();self.api.send_private_msg.assert_not_awaited()
    async def test_whitelist_checked_before_faq(self):
        await self.handler.handle(self.event('帮助',group='888888'))
        self.assertNotIn('使用帮助',str(self.api.send_group_msg.call_args))
        self.service.claim.assert_not_awaited()
    async def test_duplicate_question_throttled_without_blocking_claim(self):
        await self.handler.handle(self.event('下载'))
        await self.handler.handle(self.event('下载'))
        self.assertEqual(self.api.send_group_msg.await_count,1)
        self.service.claim.return_value={'card':'TEST-PRIVATE-CARD','already_claimed':False}
        await self.handler.handle(self.event('领卡'))
        self.service.claim.assert_awaited_once()
        self.assertNotIn('TEST-PRIVATE-CARD',str(self.api.send_group_msg.call_args_list))

if __name__=='__main__':unittest.main()
