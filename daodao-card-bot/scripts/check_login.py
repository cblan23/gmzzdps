import asyncio,json,os
from app.config import Settings

async def main():
    import websockets
    s=Settings.read()
    async with websockets.connect(s.ws_url,additional_headers={'Authorization':'Bearer '+s.ws_token},open_timeout=8) as ws:
        await ws.send(json.dumps(dict(action='get_login_info',params={},echo='check-login')))
        while True:
            response=json.loads(await asyncio.wait_for(ws.recv(),10))
            if response.get('echo')=='check-login':
                data=response.get('data') or {}
                print(json.dumps(dict(connected=True,logged_in=response.get('retcode')==0,
                     correct_account=str(data.get('user_id'))==s.bot_qq),ensure_ascii=False))
                return
asyncio.run(main())
