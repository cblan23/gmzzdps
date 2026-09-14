from app.clients.card_api_client import CardApiClient
from app.services.card_service import CardError
import re

class RemoteCardApiService:
    def __init__(self,url,token):self.client=CardApiClient(url,token)
    async def claim(self,qq,group_id,request_id):
        data=await self.client.request(self.client.url,dict(qq=qq,group_id=group_id,request_id=request_id))
        if not re.fullmatch(r'[A-Za-z0-9-]{10,128}',str(data.get('card',''))):raise CardError('API_INVALID_RESPONSE')
        if not isinstance(data.get('already_claimed'),bool):raise CardError('API_INVALID_RESPONSE')
        return data
    async def stats(self,admin_qq):
        return await self.client.request(self.client.url.rsplit('/',1)[0]+'/stats',dict(admin_qq=admin_qq))
