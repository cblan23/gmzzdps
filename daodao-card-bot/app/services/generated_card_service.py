"""Dedicated API delegates issuance to the existing DPS backend transaction."""
from app.clients.card_api_client import CardApiClient

class GeneratedCardPool:
    def __init__(self,token):self.client=CardApiClient('http://127.0.0.1:8766/api/v1/dps/bot/claim',token)
    def claim(self,qq,group_id,request_id):
        return self.client.post(self.client.url,dict(qq=qq,group_id=group_id,request_id=request_id))
    def stats_for_admin(self,admin_qq):
        return self.client.post('http://127.0.0.1:8766/api/v1/dps/bot/stats',dict(admin_qq=admin_qq))
