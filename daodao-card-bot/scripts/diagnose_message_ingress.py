"""Bounded read-only OneBot diagnostic. Never logs raw chat or sends messages."""
import asyncio,json,time
from collections import Counter
from app.config import Settings
from app.bot.commands import parse_command
from app.maintenance import safe_text

async def main():
    import websockets
    settings=Settings.read()
    counts=Counter();commands=[];results=[]
    async with websockets.connect(settings.ws_url,additional_headers={'Authorization':'Bearer '+settings.ws_token},open_timeout=5) as ws:
        actions=[('get_group_list',{})]+[('get_group_member_info',{'group_id':int(group),'user_id':int(settings.bot_qq),'no_cache':True}) for group in sorted(settings.groups)]
        for i,(action,params) in enumerate(actions):
            await ws.send(json.dumps({'action':action,'params':params,'echo':'diag-'+str(i)}))
        deadline=time.monotonic()+25
        while time.monotonic()<deadline:
            try:row=json.loads(await asyncio.wait_for(ws.recv(),min(3,max(.01,deadline-time.monotonic()))))
            except asyncio.TimeoutError:continue
            if str(row.get('echo','')).startswith('diag-'):
                index=int(row['echo'].split('-')[1]);data=row.get('data')
                result={'action':actions[index][0],'retcode':row.get('retcode'),'status':row.get('status')}
                if row.get('retcode')!=0:result['message']=safe_text(row.get('message',row.get('wording','')))
                if index==0 and isinstance(data,list):
                    result['allowed_groups_present']=[str(x.get('group_id')) for x in data if str(x.get('group_id')) in settings.groups]
                elif isinstance(data,dict):
                    result.update(group=actions[index][1]['group_id'],role=data.get('role'),shut_up_timestamp=data.get('shut_up_timestamp'))
                results.append(result)
            else:
                counts[str(row.get('post_type','unknown'))+':'+str(row.get('message_type',row.get('meta_event_type','')))]+=1
                if row.get('message_type')=='group' and str(row.get('group_id')) in settings.groups:
                    segments=row.get('message',[])
                    try:command=parse_command(segments,settings.bot_qq)
                    except Exception:command='parse_error'
                    has_at=isinstance(segments,list) and any(x.get('type')=='at' and str(x.get('data',{}).get('qq'))==settings.bot_qq for x in segments if isinstance(x,dict))
                    if has_at:commands.append({'at':row.get('time'),'group':row.get('group_id'),'message_id':row.get('message_id'),'command':command})
        print(json.dumps({'seconds':25,'events':counts,'bot_mentions':commands,'api_checks':results,'no_messages_sent':True}))

if __name__=='__main__':asyncio.run(main())
