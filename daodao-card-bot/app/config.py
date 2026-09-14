import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

def load_env(path='.env'):
    file=Path(path)
    if file.is_file():
        for line in file.read_text(encoding='utf-8-sig').splitlines():
            line=line.strip()
            if not line or line.startswith('#'): continue
            key,sep,value=line.partition('=')
            if not sep or not re.fullmatch(r'[A-Z_]+',key): raise ValueError('Invalid configuration line')
            os.environ.setdefault(key,value.strip().strip('"').strip("'"))

def qq_id(value):
    text=str(value)
    if not re.fullmatch(r'[1-9][0-9]{4,15}',text): raise ValueError('Invalid QQ/group ID')
    return text

def ids(value):
    return frozenset(qq_id(v.strip()) for v in value.split(',') if v.strip())

@dataclass(frozen=True)
class Settings:
    bot_qq: str
    admins: frozenset
    groups: frozenset
    ws_url: str
    ws_token: str
    api_url: str
    api_token: str
    mode: str
    db: str

    @classmethod
    def read(cls,bot=False):
        load_env()
        s=cls(os.getenv('BOT_QQ',''),ids(os.getenv('ADMIN_QQS','')),ids(os.getenv('ALLOWED_GROUPS','')),
              os.getenv('NAPCAT_WS_URL','ws://127.0.0.1:3001'),os.getenv('NAPCAT_TOKEN',''),
              os.getenv('CARD_API_URL','https://daodaogame.vip/api/card/claim'),os.getenv('CARD_API_TOKEN',''),
              os.getenv('CARD_MODE','remote'),os.getenv('CARD_DB','data/cards.db'))
        if len(s.api_token)<32: raise ValueError('Configure CARD_API_TOKEN (32+ characters)')
        tokens=[s.api_token,s.ws_token,os.getenv('NAPCAT_WEBUI_TOKEN','')]
        nonempty=[v for v in tokens if v]
        if len(set(nonempty))!=len(nonempty):raise ValueError('Use distinct API, WebSocket and WebUI tokens')
        if s.mode not in ('remote','local'): raise ValueError('Invalid CARD_MODE')
        endpoint=urlsplit(s.api_url)
        if endpoint.scheme!='https' and not (endpoint.scheme=='http' and endpoint.hostname in ('127.0.0.1','localhost')):
            raise ValueError('Card API requires HTTPS or loopback HTTP')
        ws=urlsplit(s.ws_url)
        if ws.scheme not in ('ws','wss') or ws.hostname not in ('127.0.0.1','localhost','napcat'):
            raise ValueError('WebSocket must be loopback or napcat Docker service')
        if ws.username or ws.password or ws.query: raise ValueError('WebSocket credentials belong in the token setting')
        if bot:
            qq_id(s.bot_qq)
            if not s.groups or not s.admins or len(s.ws_token)<32:
                raise ValueError('Configure groups, admins and WebSocket token before starting bot')
        return s
