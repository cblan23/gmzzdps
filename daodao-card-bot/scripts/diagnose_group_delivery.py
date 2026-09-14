"""Read recent bot delivery metadata only; no message text or outgoing messages."""
import asyncio,json,time
from app.config import Settings
async def main():
    import websockets
    s=Settings.read()
    async with websockets.connect(s.ws_url,additional_headers={'Authorization':'Bearer '+s.ws_token},open_timeout=5) as ws:
        for group in sorted(s.groups):
            await ws.send(json.dumps({'action':'get_group_msg_history','params':{'group_id':int(group),'count':30},'echo':group}))
        deadline=time.monotonic()+20;pending=set(s.groups)
        while pending and time.monotonic()<deadline:
            try:row=json.loads(await asyncio.wait_for(ws.recv(),max(.01,deadline-time.monotonic())))
            except asyncio.TimeoutError:break
            group=str(row.get('echo',''))
            if group not in pending:continue
            pending.remove(group);data=row.get('data') or {};messages=data.get('messages',[]) if isinstance(data,dict) else []
            own=[{k:r.get(k) for k in ('message_id','time','message_seq','real_id')} for r in messages if str(r.get('user_id',(r.get('sender') or {}).get('user_id')))==s.bot_qq]
            recent=[{'time':r.get('time'),'message_id':r.get('message_id'),'from_bot':str(r.get('user_id',(r.get('sender') or {}).get('user_id')))==s.bot_qq} for r in messages[-5:]]
            print(json.dumps({'group':group,'retcode':row.get('retcode'),'message_count':len(messages),'own_recent_messages':own,'recent_message_metadata':recent}),flush=True)
        print(json.dumps({'timed_out_groups':sorted(pending),'no_messages_sent':True}))
if __name__=='__main__':asyncio.run(main())
