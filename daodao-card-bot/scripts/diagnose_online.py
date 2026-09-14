"""Read-only online-state check; account metadata alone is not login proof."""
import asyncio
import json
from app.config import Settings

async def main():
    import websockets
    settings = Settings.read()
    async with websockets.connect(settings.ws_url, additional_headers={
        'Authorization': 'Bearer ' + settings.ws_token}, open_timeout=8) as ws:
        for action in ('get_status', 'get_login_info'):
            await ws.send(json.dumps({'action': action, 'params': {}, 'echo': action}))
            deadline = asyncio.get_running_loop().time() + 10
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise TimeoutError('Status response deadline')
                reply = json.loads(await asyncio.wait_for(ws.recv(), remaining))
                if reply.get('echo') != action:
                    continue
                data = reply.get('data') or {}
                result = {'action': action, 'retcode': reply.get('retcode'), 'status': reply.get('status')}
                if action == 'get_status':
                    result.update(online=data.get('online'), good=data.get('good'))
                else:
                    result['correct_account'] = str(data.get('user_id')) == settings.bot_qq
                    result['note'] = 'Account metadata does not prove QQ online'
                print(json.dumps(result), flush=True)
                break

if __name__ == '__main__':
    asyncio.run(main())
