import json
import urllib.request
from app.config import Settings
from app.clients.card_api_client import CardApiClient
s=Settings.read()
assert s.bot_qq=='3035610294'
assert s.admins=={'1806525'}
assert s.groups=={'165966739','1094925831','732363944'}
client=CardApiClient(s.api_url,s.api_token)
result=client.post(s.api_url.rsplit('/',1)[0]+'/stats',{'admin_qq':'1806525'})
assert result.get('mode')=='generated'
assert result['remaining'] is None
with urllib.request.urlopen('http://127.0.0.1:8766/api/v1/dps/health',timeout=5) as r:
    assert json.load(r)['ok']
print(json.dumps(dict(configured=True,groups=len(s.groups),api='generated-8h',
                      claimed=result['claimed'],dps_health=True),ensure_ascii=False))
