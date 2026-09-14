from app.bot.faq import faq_command

def parse_command(segments,bot_qq):
    if not isinstance(segments,list):return None
    mentioned=False
    texts=[]
    other_mention=False
    for segment in segments:
        kind=segment.get('type')
        data=segment.get('data',{})
        if kind=='at':
            if str(data.get('qq'))==bot_qq:mentioned=True
            else:other_mention=True
        elif kind=='text':texts.append(str(data.get('text','')))
        elif kind!='reply':return None
    if not mentioned:return None
    command=''.join(texts).strip()
    if command in ('领卡','卡池','状态','补卡') and not other_mention:return command
    if command in ('查询','重置'):return command
    if not other_mention:return faq_command(command)
    return None
