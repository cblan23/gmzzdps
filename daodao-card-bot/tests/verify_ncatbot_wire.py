"""Real pinned NcatBot + local OneBot test double, no QQ messages sent."""
import asyncio,json,os,sys,tempfile,time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import websockets
from app.services.local_card_service import CardPool

async def main():
    actions=[]
    connected=0
    async def server(ws):
        nonlocal connected
        connected+=1
        await ws.send(json.dumps(dict(time=int(time.time()),self_id=555555,post_type='meta_event',meta_event_type='lifecycle',sub_type='connect')))
        if connected==1:
            await ws.wait_closed();return
        await asyncio.sleep(.2)
        event=dict(time=int(time.time()),self_id=555555,post_type='message',message_type='group',sub_type='normal',
            message_id=1,user_id=123456,group_id=999999,anonymous=None,
            message=[{'type':'at','data':{'qq':'555555'}},{'type':'text','data':{'text':'领卡'}}],
            raw_message='[CQ:at,qq=555555]领卡',font=0,sender=dict(user_id=123456,nickname='test',card='',role='member',sex='unknown',age=0,area='',level='',title=''))
        await ws.send(json.dumps(event))
        try:
            async for raw in ws:
                request=json.loads(raw);actions.append(request)
                await ws.send(json.dumps(dict(status='ok',retcode=0,data={'message_id':123},echo=request['echo'])))
        except websockets.ConnectionClosed:pass
    with tempfile.TemporaryDirectory() as folder:
        root=Path(folder)
        (root/'data').mkdir()
        pool=CardPool(str(root/'data/pool.db'));pool.import_cards(['DAODAO-WIRE-PRIVATE-0001'])
        async with websockets.serve(server,'127.0.0.1',0) as listener:
            env=dict(os.environ,BOT_QQ='555555',ADMIN_QQS='777777',ALLOWED_GROUPS='999999',
                NAPCAT_WS_URL=f'ws://127.0.0.1:{listener.sockets[0].getsockname()[1]}',
                NAPCAT_TOKEN='Token_Test_123456789012345678901234',CARD_API_TOKEN='Api_Test_1234567890123456789012345',
                CARD_MODE='local',CARD_DB=str(root/'data/pool.db'),
                PYTHONPATH=str(Path(__file__).resolve().parents[1]))
            process=await asyncio.create_subprocess_exec(sys.executable,'-m','app.main',cwd=root,env=env,
                stdout=asyncio.subprocess.DEVNULL,stderr=asyncio.subprocess.DEVNULL)
            try:
                for _ in range(100):
                    if len(actions)>=2 or process.returncode is not None:break
                    await asyncio.sleep(.2)
                assert [a['action'] for a in actions[:2]]==['send_private_msg','send_group_msg'], (len(actions),process.returncode,
                    (root/'logs/card-bot.log').read_text() if (root/'logs/card-bot.log').exists() else 'no log')
                assert str(actions[0]['params']['user_id'])=='123456'
                assert actions[0]['params']['group_id']=='999999'
                assert actions[0]['params']['message_type']=='private'
                assert 'DAODAO-WIRE' not in json.dumps(actions[1])
                assert not (root/'config.yaml').exists()
                for path in (root/'logs').glob('*'):
                    content=path.read_text(errors='replace')
                    assert env['NAPCAT_TOKEN'] not in content and 'DAODAO-WIRE-PRIVATE-0001' not in content
                print('Real NcatBot integration passed: group event -> private card -> safe group receipt; logs clean')
            finally:
                if process.returncode is None:process.terminate()
                await process.wait()

if __name__=='__main__':asyncio.run(main())
