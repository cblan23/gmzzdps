import asyncio
import json
import random
import urllib.error
import urllib.request
import time
from app.utils.diagnostics import emit, trace
from app.services.card_service import CardError

class CardApiClient:
    def __init__(self,url,token):self.url,self.token=url,token
    def post(self,url,payload):
        started=time.monotonic()
        channel='worker' if trace.get() else 'maintenance'
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self,*args,**kwargs):return None
        req=urllib.request.Request(url,data=json.dumps(payload).encode(),headers={
            'Authorization':'Bearer '+self.token,'Content-Type':'application/json'},method='POST')
        try:
            with urllib.request.build_opener(NoRedirect).open(req,timeout=8) as r:raw=r.read(16385)
        except urllib.error.HTTPError as e:
            emit(channel,'card_http',http_status=e.code,outcome='failed',elapsed_ms=int((time.monotonic()-started)*1000))
            if e.code>=500 or e.code==429:raise CardError('API_UNAVAILABLE') from None
            raise CardError('API_REJECTED') from None
        except (OSError,urllib.error.URLError) as error:
            emit(channel,'card_http',outcome='failed',error_type=type(error).__name__,elapsed_ms=int((time.monotonic()-started)*1000))
            raise CardError('API_UNAVAILABLE') from None
        emit(channel,'card_http',http_status=200,outcome='received',elapsed_ms=int((time.monotonic()-started)*1000))
        try:
            value=json.loads(raw)
            if not isinstance(value,dict):raise ValueError()
        except (ValueError,TypeError):
            emit(channel,'card_json',outcome='invalid')
            raise CardError('API_INVALID_RESPONSE') from None
        if value.get('success') is not True:raise CardError(str(value.get('code','API_REJECTED')))
        return value
    async def request(self,url,payload):
        for attempt in range(3):
            try:return await asyncio.to_thread(self.post,url,payload)
            except CardError as error:
                if error.code!='API_UNAVAILABLE' or attempt==2:raise
                emit('worker','card_retry',attempt=attempt+1)
                await asyncio.sleep(random.uniform(.5,1)*(2**attempt))
