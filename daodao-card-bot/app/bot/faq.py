"""Owner-approved fixed answers. No generation, network lookup or card allocation."""
import unicodedata

FAQ = (
    ('download', '哪里下载', ('哪里下载','下载地址','下载'),
     '请下载群文件里的「叨叨诡秘 Dps-Logs」，建议使用最新版本。'),
    ('update', '怎么更新', ('怎么更新','升级','更新'),
     '打开设置，检查更新并按提示下载。更新失败时，可以下载群文件中的最新版，关闭旧程序后运行新版。'),
    ('card', '怎么领卡', ('怎么领卡','领取卡号','卡号'),
     '在本群「@叨叨助手」发送 领卡，卡号会私聊给你。\nhttps://wzyp.cn/shop/QDAWZ5KZ 如果您觉得不错也非常非常感谢您进行打赏。'),
    ('font', '怎么放大字体', ('怎么放大字体','字太小','字体','字体大小','UI缩放'),
     '打开设置，找到「字体大小／UI缩放」，调大后即可查看效果。如果只想显示更多队员，可以拖动主窗口右下角调整高度。'),
    ('settings', '怎么打开设置', ('怎么打开设置','怎么配置','设置'),
     '点击 DPS 主窗口底部的齿轮图标打开设置，可以调整显示内容、字体大小、快捷键等。'),
    ('no_data', '为什么没数据', ('为什么没数据','没有秒伤','没数据'),
     '先管理员身份运行程序，可以先打木桩。如果还是没有请@群主'),
    ('clear', '怎么清空', ('怎么清空','重置数据','清空'),
     '点击主窗口底部的清除按钮，或使用设置中配置的清除快捷键。战斗期间不允许清除；开启评分预览后，清除会回到评分显示。'),
    ('window', '窗口不见了', ('窗口不见了','怎么显示'),
     '检查任务栏右下角托盘图标，或使用你设置的显示／隐藏快捷键，尝试恢复窗口'),
    ('move', '怎么移动', ('怎么移动','窗口拖不动','移动窗口'),
     '先检查主窗口是否锁定，解锁后拖动窗口的非按钮区域即可移动'),
    ('topmost', '怎么置顶', ('怎么置顶','被游戏挡住','置顶'),
     '点击主窗口底部的置顶图标切换置顶状态'),
    ('action_mode', '动作模式不小心点到', ('动作模式不小心点到','动作模式','锁定'),
     '点击主窗口底部的锁定按钮'),
    ('history', '战斗记录在哪里', ('战斗记录在哪里','看技能详情','战斗记录','技能详情'),
     '打开设置里的「战斗记录」，选择对应战斗查看详情。部分队友技能数据可能暂时不完整。'),
    ('upload', '为什么不能上传', ('为什么不能上传','没有上传按钮','不能上传'),
     '当前只允许符合条件的胜利战斗上传，木桩、失败或不符合条件的记录不显示上传按钮。已上传的记录不能重复点击上传。'),
    ('feedback', '怎么反馈', ('怎么反馈','遇到Bug','反馈','bug'),
     '使用软件里的反馈功能，尽量选择出问题的战斗，并填写发生过程。提交后把反馈编号发群里，便于定位。'),
)

def normalize(text):
    return unicodedata.normalize('NFKC', text).strip().rstrip('?!。！？，,；;').strip().casefold()

ALIASES = {normalize(alias): key for key, _, aliases, _ in FAQ for alias in aliases}
ANSWERS = {key: answer for key, _, _, answer in FAQ}
HELP = ('叨叨助手 · 使用帮助\n\n'
        '领卡：@叨叨助手 领卡（卡号只通过私聊发送）\n\n'
        + '\n'.join(f'{index}. {title}' for index, (_, title, _, _) in enumerate(FAQ, 1))
        + '\n\n请 @叨叨助手 后发送问题，或发送「帮助 1」至「帮助 14」查看对应回答。\n'
          '例如：@叨叨助手 怎么更新\n帮助入口：@叨叨助手 帮助')

def faq_command(text):
    value=normalize(text)
    if value in ('','帮助','菜单','使用帮助','常见问题','功能','功能菜单','help'):
        return 'faq:help'
    for prefix in ('帮助','问题'):
        if value.startswith(prefix):
            number=value[len(prefix):].strip()
            if number.isascii() and number.isdigit() and 1<=int(number)<=len(FAQ):
                return 'faq:'+FAQ[int(number)-1][0]
    key=ALIASES.get(value)
    return 'faq:'+key if key else None

def answer(command):
    return HELP if command=='faq:help' else ANSWERS.get(command.removeprefix('faq:'))
