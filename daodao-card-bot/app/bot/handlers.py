import asyncio
import hashlib
import logging
import time
import secrets
from app.utils.diagnostics import emit, trace
from collections import OrderedDict
from app.config import qq_id
from app.bot.commands import parse_command
from app.bot.faq import answer
from app.bot.delivery import GroupDeliveryError
from app.services.card_service import CardError

log=logging.getLogger('daodao.bot')

class BotHandler:
    def __init__(self,settings,service,api):
        self.settings,self.service,self.api=settings,service,api
        self.slots=asyncio.Semaphore(8)
        self.pending=0
        self.faq_recent=OrderedDict()
    async def group_reply(self,group,qq,text):
        started=time.monotonic()
        try:
            await asyncio.wait_for(self.api.send_group_msg(group,[{'type':'at','data':{'qq':qq}},
                {'type':'text','data':{'text':' '+text}}]),12)
            emit('worker','group_reply',qq=qq,group=group,outcome='confirmed',elapsed_ms=int((time.monotonic()-started)*1000))
        except GroupDeliveryError as error:
            emit('worker','group_reply',qq=qq,group=group,outcome='failed',retcode=error.code,stage=error.stage,native_result=error.native.get('result'),elapsed_ms=int((time.monotonic()-started)*1000))
            log.warning('group_reply_failed qq=%s group=%s code=%s stage=%s native=%s',qq,group,error.code,error.stage,error.native)
        except Exception as error:
            emit('worker','group_reply',qq=qq,group=group,outcome='failed',error_type=type(error).__name__,elapsed_ms=int((time.monotonic()-started)*1000))
            log.warning('group_reply_failed qq=%s group=%s type=%s',qq,group,type(error).__name__)
    async def handle(self,event):
        token=trace.set(secrets.token_hex(8))
        started=time.monotonic()
        try:
            await self._handle(event)
        finally:
            emit('worker','dispatch_finished',elapsed_ms=int((time.monotonic()-started)*1000))
            trace.reset(token)

    async def _handle(self,event):
        try:
            qq,group=qq_id(event.user_id),qq_id(event.group_id)
            if str(event.self_id)!=self.settings.bot_qq or qq==self.settings.bot_qq or getattr(event,'anonymous',None):return
            message=event.message.to_list() if hasattr(event.message,'to_list') else event.message
            command=parse_command(message,self.settings.bot_qq)
            if command is None:return
            emit('worker','command_received',qq=qq,group=group,message_id=getattr(event,'message_id',None),command='faq' if command.startswith('faq:') else 'claim' if command=='领卡' else 'admin',pending=self.pending)
            if group not in self.settings.groups:
                emit('worker','command_rejected',qq=qq,group=group,stage='group_whitelist')
                await self.group_reply(group,qq,'当前群暂未开放同行卡领取。');return
            log.info('command qq=%s group=%s command=%s',qq,group,command)
            if command.startswith('faq:'):
                now=time.monotonic();key=(group,qq)
                if now-self.faq_recent.get(key,-1000)<3:
                    emit('worker','command_rejected',qq=qq,group=group,stage='faq_throttle')
                    return
                self.faq_recent[key]=now;self.faq_recent.move_to_end(key)
                while len(self.faq_recent)>512:self.faq_recent.popitem(last=False)
                response=answer(command)
                if response is not None:await self.group_reply(group,qq,response)
                return
            if command!='领卡':
                if qq not in self.settings.admins:
                    emit('worker','command_rejected',qq=qq,group=group,stage='admin_permission')
                    await self.group_reply(group,qq,'该命令仅管理员可用。');return
                if command in ('卡池','状态'):
                    data=await self.service.stats(qq)
                    remaining='按需生成' if data.get('remaining') is None else str(int(data['remaining']))
                    await self.group_reply(group,qq,f"今日卡池\n总数：{int(data['total'])}\n已领取：{int(data['claimed'])}\n剩余：{remaining}\n今日领取：{int(data['today_claimed'])}")
                else:await self.group_reply(group,qq,'该操作暂未开放，请通过服务器管理工具处理。')
                return
            if self.pending>=64:
                emit('worker','command_rejected',qq=qq,group=group,stage='queue_full',pending=self.pending)
                await self.group_reply(group,qq,'领取请求较多，请稍后重试。');return
            self.pending+=1
            try:
                async with self.slots:
                    request_id=hashlib.sha256(f'{self.settings.bot_qq}:{group}:{qq}:{event.message_id}'.encode()).hexdigest()
                    data=await self.service.claim(qq,group,request_id)
                    emit('worker','card_allocated',qq=qq,group=group,outcome='repeat' if data['already_claimed'] else 'first')
                    log.info('claim_%s qq=%s group=%s', 'repeat' if data['already_claimed'] else 'first',qq,group)
                    text='🎫 今日同行卡\n\n'+data['card']+'\n\n'
                    if data.get('expire_at'):text+='同行契约至：\n'+str(data['expire_at'])+'\n\n'
                    elif data.get('activation_policy')=='first_login':text+='机器人发卡专用 请注意登录时需要点击登录而不是试用\n\n'
                    text+='今日已领取成功。\n请勿将卡号分享给其他人。\n\n叨叨诡秘 Dps-Logs'
                    try:
                        result=await asyncio.wait_for(self.api.send_private_msg(qq,[{'type':'text','data':{'text':text}}],group_id=group),12)
                        if not result:raise RuntimeError('send not confirmed')
                    except Exception as error:
                        emit('worker','private_delivery',qq=qq,group=group,outcome='failed',error_type=type(error).__name__)
                        log.warning('private_failed qq=%s group=%s',qq,group)
                        await self.group_reply(group,qq,'临时私聊发送失败，请检查群临时会话设置，或添加机器人好友后重新发送「领卡」。');return
                    log.info('private_success qq=%s group=%s',qq,group)
                    emit('worker','private_delivery',qq=qq,group=group,outcome='confirmed')
                    await self.group_reply(group,qq,'今日同行卡已重新发送，请查看私聊 ✓' if data['already_claimed'] else '已发送，请查看私聊 ✓')
            finally:self.pending-=1
        except CardError as error:
            emit('worker','card_failure',error_type=error.code if error.code in ('CARD_POOL_EMPTY','API_UNAVAILABLE','API_INVALID_RESPONSE','API_REJECTED') else 'REJECTED')
            log.warning('api_failed code=%s',error.code if error.code in ('CARD_POOL_EMPTY','API_UNAVAILABLE') else 'REJECTED')
            await self.group_reply(group,qq,'今日同行卡暂时已经领完，请等待补充。' if error.code=='CARD_POOL_EMPTY' else '发卡服务暂时不可用，请稍后重试，不会重复扣卡。')
        except Exception as error:
            emit('worker','handler_failure',error_type=type(error).__name__)
            log.error('handler_error type=%s',type(error).__name__)
