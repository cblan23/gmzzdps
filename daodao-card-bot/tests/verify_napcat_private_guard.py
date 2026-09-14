"""Execute the deployed NapCat resolver with test-only account API stubs."""
import json
from pathlib import Path
import sys
import quickjs

source=Path(sys.argv[1]).read_text(encoding='utf-8')
function=source[source.index('async function F2('):source.index('\nfunction aye(',source.index('async function F2('))]
needle='if (!r) {\n      if (e.group_id)'
if needle in function:
    function=function.replace(needle,'if (!r) {\n      if (n !== 1 && e.group_id)')
assert 'if (n !== 1 && e.group_id)' in function
for uid,buddy,expected in ((None,False,'error'),('u_test',False,'temp'),('u_test',True,'private')):
    context=quickjs.Context()
    context.eval('var Te={KCHATTYPEGROUP:"group",KCHATTYPEC2C:"private",KCHATTYPETEMPC2CFROMGROUP:"temp"};'+function)
    context.eval('var result=null;var api={apis:{UserApi:{getUidByUinV2:async()=>'+json.dumps(uid)+'},'
       'FriendApi:{isBuddy:async()=>'+json.dumps(buddy)+'},MsgApi:{getTempChatInfo:async()=>({})}}};'
       'F2(api,{message_type:"private",user_id:"123456",group_id:"999999"},1)'
       '.then(x=>result=x.chatType).catch(()=>result="error");')
    while context.execute_pending_job():pass
    actual=context.eval('result')
    assert actual==expected,(actual,expected)
print('NapCat resolver verified: unknown identity fails closed, stranger uses temp chat, buddy uses private chat')
