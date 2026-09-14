"""Use NapCat's group-context private API; never route card text to a group."""
import json
import time
from app.utils.diagnostics import emit
class GroupDeliveryError(RuntimeError):
    def __init__(self, code, stage, native=None):
        super().__init__('Group response not confirmed')
        self.code=code
        self.stage=stage
        self.native=native or {}

class DeliveryApi:
    def __init__(self,api):self.api=api
    async def call(self,path,params):
        started=time.monotonic()
        stage='private' if path=='/send_private_msg' else 'group'
        try:
            response=await self.api.async_callback(path,params)
            emit('worker','qq_send_api',stage=stage,retcode=response.get('retcode') if isinstance(response,dict) else None,outcome='returned',elapsed_ms=int((time.monotonic()-started)*1000))
            return response
        except BaseException as error:
            emit('worker','qq_send_api',stage=stage,outcome='failed',error_type=type(error).__name__,elapsed_ms=int((time.monotonic()-started)*1000))
            raise
    async def send_group_msg(self,group,message):
        response=await self.call('/send_group_msg',dict(
            group_id=str(group),message=message,timeout=5000))
        if not isinstance(response,dict):raise GroupDeliveryError('invalid_response','unknown')
        code=response.get('retcode')
        if response.get('status')!='ok' or code!=0:
            # Log fixed categories, never raw exception text/message payloads.
            detail=str(response.get('message',''))+' '+str(response.get('wording',''))
            stage='send_confirmation' if 'sendMsg' in detail else 'member_lookup' if 'fetchUserDetailInfo' in detail else 'unknown'
            native={}
            if 'EventRet:' in detail:
                try:
                    raw,_=json.JSONDecoder().raw_decode(detail.split('EventRet:',1)[1].lstrip())
                    if isinstance(raw,dict):
                        native={key:value for key,value in raw.items() if key in ('result','errCode','errorCode','sendStatus') and isinstance(value,(int,bool))}
                except (ValueError,TypeError):pass
            raise GroupDeliveryError(code if isinstance(code,int) else 'unknown',stage,native)
        result=(response.get('data') or {}).get('message_id')
        if not result:raise GroupDeliveryError('missing_id','send_confirmation')
        return str(result)
    async def send_private_msg(self,qq,message,*,group_id):
        response=await self.call('/send_private_msg',dict(
            user_id=str(qq),group_id=str(group_id),message_type='private',message=message))
        if not isinstance(response,dict) or response.get('retcode')!=0 or response.get('status')!='ok':
            raise RuntimeError('Private delivery rejected')
        data=response.get('data') or {}
        if not data.get('message_id'):raise RuntimeError('Private delivery not confirmed')
        return str(data['message_id'])
