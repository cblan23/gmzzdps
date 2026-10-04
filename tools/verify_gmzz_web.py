"""Exercise the public website in Chrome using its current real API data."""
from __future__ import annotations

import argparse
import base64
import json
from pathlib import Path
import subprocess
import time
import urllib.request

import websocket

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--url', default='https://gmzz.daodaogame.vip')
parser.add_argument('--output', type=Path, default=Path('.codex-tmp/gmzz-web-verification'))
parser.add_argument('--feature-ids', type=Path, help='Public encounter IDs to inspect for recovered equipment and rich skill data')
args = parser.parse_args()
output = args.output.resolve()
output.mkdir(parents=True, exist_ok=True)
profile = output / 'chrome-profile'
profile.mkdir(exist_ok=True)
(profile / 'DevToolsActivePort').unlink(missing_ok=True)
errors = []
requests = []
failures = []
checks = []
connection = None
sequence = 0
log = (output / 'chrome.log').open('w', encoding='utf-8')
browser = subprocess.Popen([
    r'C:\Program Files\Google\Chrome\Application\chrome.exe', '--headless=new',
    '--do-not-de-elevate', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
    '--disable-background-networking', '--remote-debugging-port=0',
    '--remote-allow-origins=http://localhost', f'--user-data-dir={profile}', 'about:blank',
], creationflags=subprocess.CREATE_NO_WINDOW, stdout=log, stderr=log)


def call(method, params=None):
    global sequence
    sequence += 1
    identifier = sequence
    connection.send(json.dumps({'id': identifier, 'method': method, 'params': params or {}}))
    while True:
        message = json.loads(connection.recv())
        if message.get('method') == 'Runtime.exceptionThrown':
            errors.append(message['params'])
        if message.get('method') == 'Network.requestWillBeSent':
            requests.append(message['params']['request']['url'])
        if message.get('method') == 'Network.responseReceived' and message['params']['response']['status'] >= 400:
            failures.append(message['params']['response'])
        if message.get('id') == identifier:
            if 'error' in message:
                raise RuntimeError(message['error'])
            return message.get('result', {})


def evaluate(expression):
    result = call('Runtime.evaluate', {'expression': expression, 'returnByValue': True, 'awaitPromise': True})
    if 'exceptionDetails' in result:
        raise RuntimeError(result['exceptionDetails'])
    return result.get('result', {}).get('value')


def ready(expression, timeout=25):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if evaluate(f'Boolean({expression})'):
            return
        time.sleep(.1)
    diagnostics = evaluate("[...document.images].filter(e=>!e.complete||!e.naturalWidth).map(e=>({src:e.src,loading:e.loading,complete:e.complete,rect:{top:e.getBoundingClientRect().top,width:e.getBoundingClientRect().width},hidden:!!e.closest('details:not([open])')}))")
    raise AssertionError(f'Page did not become ready: {expression}\n' + str(diagnostics)[:3000] + '\n' + str(evaluate('document.body.innerText'))[:1400])


def click(selector):
    expression = f"(() => {{const e=document.querySelector({json.dumps(selector)}); if(!e) throw Error('Missing control'); e.scrollIntoView({{block:'center'}}); const r=e.getBoundingClientRect(),x=r.x+r.width/2,y=r.y+r.height/2,hit=document.elementFromPoint(x,y);return {{x,y,ready:hit===e||e.contains(hit),hit:hit?.tagName}};}})()"
    deadline = time.monotonic() + 3
    while True:
        rectangle = evaluate(expression)
        if rectangle.pop('ready'):
            rectangle.pop('hit', None)
            break
        if time.monotonic() >= deadline:
            raise AssertionError(f'Control is covered: {selector}: {rectangle}')
        evaluate('new Promise(resolve=>requestAnimationFrame(resolve))')
    for kind in ('mousePressed', 'mouseReleased'):
        call('Input.dispatchMouseEvent', {'type': kind, **rectangle, 'button': 'left', 'clickCount': 1})
    evaluate('new Promise(resolve=>setTimeout(resolve,0))')


def route(hash_value, condition):
    evaluate(f'location.hash={json.dumps(hash_value)}')
    evaluate('new Promise(resolve=>setTimeout(resolve,0))')
    ready(condition)


def screenshot(name):
    evaluate('document.fonts.ready')
    evaluate('Promise.all(document.getAnimations().filter(a=>a.effect?.getTiming().iterations!==Infinity).map(a=>a.finished.catch(()=>{})))')
    data = call('Page.captureScreenshot', {'format': 'png', 'captureBeyondViewport': False})
    (output / name).write_bytes(base64.b64decode(data['data']))


def check(name):
    checks.append(name)
    print(json.dumps({'passed': name}, ensure_ascii=True), flush=True)


try:
    deadline = time.monotonic() + 20
    port_file = profile / 'DevToolsActivePort'
    while not port_file.exists() and time.monotonic() < deadline:
        if browser.poll() is not None:
            raise RuntimeError('Chrome did not start; see chrome.log')
        time.sleep(.1)
    port = int(port_file.read_text().splitlines()[0])
    with urllib.request.urlopen(f'http://127.0.0.1:{port}/json/list', timeout=5) as response:
        target = next(item for item in json.load(response) if item['type'] == 'page')
    connection = websocket.create_connection(target['webSocketDebuggerUrl'], origin='http://localhost', timeout=30)
    for domain in ('Page', 'Runtime', 'Network'):
        call(f'{domain}.enable')
    call('Emulation.setDeviceMetricsOverride', {'width': 1280, 'height': 900, 'deviceScaleFactor': 1, 'mobile': False})
    call('Page.navigate', {'url': args.url.rstrip('/') + '/'})
    ready("document.readyState==='complete' && document.querySelector('#historySummary')?.textContent.includes('场历史')")
    statistics = evaluate("fetch('/api/v1/dps/public/statistics').then(r=>r.json()).then(d=>d.statistics)")
    history = evaluate("fetch('/api/v1/dps/public/records?limit=25').then(r=>r.json())")
    assert history['total'] == statistics['history_encounters'] > statistics['encounters']
    assert str(history['total']) in evaluate("document.querySelector('#historySummary').textContent.replaceAll(',','')")
    screenshot('home-desktop.png')
    check('homepage real history and qualified counts')

    click('#uploadStatusBtn')
    ready("!document.querySelector('#uploadPopover').hidden")
    assert evaluate("document.querySelector('#lastUpload').textContent") != '正在读取'
    call('Input.dispatchKeyEvent', {'type': 'keyDown', 'key': 'Escape', 'code': 'Escape', 'windowsVirtualKeyCode': 27})
    assert evaluate("document.querySelector('#uploadPopover').hidden")
    click('.nav a[href="#history"]')
    ready("document.querySelectorAll('.history-row:not(.history-head)').length===25")
    screenshot('history-desktop.png')
    check('upload popover and complete historical list')

    first_title = evaluate("document.querySelector('.history-row:not(.history-head) .boss-name').textContent")
    click('.history-pagination button:last-child')
    ready("location.hash.includes('offset=25') && document.querySelector('.history-pagination span')?.textContent.startsWith('2 /')")
    next_page = evaluate("fetch('/api/v1/dps/public/records?limit=25&offset=25').then(r=>r.json())")
    assert evaluate("document.querySelector('.history-row:not(.history-head) .boss-name').textContent") == next_page['records'][0]['boss_name']
    click('.history-pagination button:first-child')
    ready("location.hash.includes('offset=0') && document.querySelector('.history-pagination span')?.textContent.startsWith('1 /')")
    check('historical pagination with real next-page records')

    click('.history-row:not(.history-head) .row-open')
    ready("document.querySelectorAll('.participant-row').length>0 && document.querySelector('.skill-events')")
    first = history['records'][0]
    detail = evaluate(f"fetch('/api/v1/dps/public/encounters/{first['encounter_id']}').then(r=>r.json()).then(d=>d.encounter)")
    assert evaluate("document.querySelector('.encounter-name').textContent") == detail['boss_name']
    assert evaluate("document.querySelectorAll('.participant-row[data-slot]').length") == len(detail['participants'])
    assert first['encounter_id'] in evaluate('location.hash')
    assert evaluate("document.querySelector('.qualification-note').textContent")
    for participant in detail['participants']:
        if participant['stats'].get('skills'):
            click(f'.participant-row[data-slot="{participant["slot"]}"]')
            assert participant['display_name'] in evaluate("document.querySelector('.participant-overview').textContent")
            break
    rich = next((p for p in detail['participants'] if len(p['stats'].get('skill_timeline', [])) > 50), None)
    if rich:
        click(f'.participant-row[data-slot="{rich["slot"]}"]')
        click('.skill-events summary')
        assert evaluate("document.querySelectorAll('.event-table tbody tr').length") == 50
        click('.skill-event-body .history-pagination button:last-child')
        assert evaluate("document.querySelector('.skill-event-body .history-pagination span').textContent").startswith('2 /')
    if detail['data'].get('team_dps_timeline'):
        assert evaluate("document.querySelector('.history-chart polyline').getAttribute('points').split(' ').length") == len(detail['data']['team_dps_timeline'])
        evaluate("document.querySelector('.history-chart svg').focus()")
        readout = evaluate("document.querySelector('.chart-readout').textContent")
        call('Input.dispatchKeyEvent', {'type': 'keyDown', 'key': 'ArrowLeft', 'code': 'ArrowLeft', 'windowsVirtualKeyCode': 37})
        assert readout != evaluate("document.querySelector('.chart-readout').textContent")
    click('.mode-tab:nth-child(2)')
    assert evaluate("document.querySelector('.mode-tab.is-active').textContent") == '治疗 HPS'
    click('.mode-tab:nth-child(3)')
    assert evaluate("document.querySelector('.mode-tab.is-active').textContent") == '承伤 DT'
    click('.mode-tab:first-child')
    evaluate("document.querySelector('.portal-scroll').scrollTop=0")
    screenshot('battle-desktop.png')
    check('battle detail, team members, individual skills, events, curve and metric tabs')

    assert evaluate("[...document.querySelectorAll('.metric-label')].some(e=>e.textContent==='团队死亡次数')")
    for participant in detail['participants']:
        expected_deaths = f"{participant['stats']['deaths']:,} 次" if participant['stats'].get('deaths') is not None else '未记录'
        assert evaluate(f"document.querySelector('.participant-row[data-slot=\"{participant['slot']}\"] .death-count').textContent") == expected_deaths
    assert '匿名' not in evaluate('document.body.innerText')
    assert evaluate("document.querySelector('.profession-icon').alt") in ('歌颂者', '观众', '占卜家', '仲裁人', '学徒', '战士', '窥秘人', '未知职业')
    check('actual death counts, missing death coverage and career selection names')

    feature_ids = json.loads(args.feature_ids.read_text(encoding='utf-8')) if args.feature_ids else [r['encounter_id'] for r in history['records'][:10]]
    features = evaluate(f"Promise.all({json.dumps(feature_ids)}.map(id=>fetch('/api/v1/dps/public/encounters/'+id).then(r=>r.json()).then(d=>d.encounter)))")
    gear = next(((battle, p) for battle in features for p in battle['participants'] if p['stats'].get('equipment_snapshot', {}).get('equipment')), None)
    luck = next(((battle, p) for battle in features for p in battle['participants'] if p['stats'].get('critical_luck') and p['stats'].get('skills')), None)
    assert luck is not None, 'No real encounter with critical luck samples was verified'
    if args.feature_ids:
        assert gear is not None, 'No restored equipment snapshot was found'
    for kind, chosen in (('equipment', gear), ('critical_luck', luck)):
        if chosen is None:
            continue
        battle, participant = chosen
        route(f"battle?id={battle['encounter_id']}", f"location.hash.includes('{battle['encounter_id']}') && document.querySelector('.encounter-name')?.textContent==={json.dumps(battle['boss_name'])} && document.querySelector('.skill-events')")
        click(f'.participant-row[data-slot="{participant["slot"]}"]')
        if kind == 'equipment':
            snapshot = participant['stats']['equipment_snapshot']
            assert evaluate("document.querySelectorAll('.equipment-item').length") == len(snapshot['equipment'])
            click('.equipment-item summary')
            assert evaluate("document.querySelector('.equipment-item').open")
            assert evaluate("document.querySelector('.equipment-item .equipment-body').textContent")
            ready("[...document.querySelectorAll('.equipment-icon')].every(e=>e.complete && e.naturalWidth>0)")
            assert evaluate("[...document.querySelectorAll('.equipment-icon')].some(e=>e.src.includes('/assets/equipment/'))")
            screenshot('equipment-desktop.png')
            check('restored battle equipment, original item icons, affixes and attributes')
        else:
            model = participant['stats']['critical_luck']
            assert model['verdict'] in evaluate("document.querySelector('.luck-verdict').textContent")
            assert str(model['known_hits']) in evaluate("document.querySelector('.luck-panel').textContent.replaceAll(',','')")
            assert evaluate("document.querySelector('.luck-panel .luck-observed')!==null")
            sampled_skill = next(s for s in participant['stats']['skills'] if s.get('damage', 0)>0 and any(e['skill_id']==s['skill_id'] and e['damage']>0 for e in participant['stats']['skill_timeline']))
            click(f'.skill-detail[data-skill-id="{sampled_skill["skill_id"]}"] summary')
            ready("document.querySelector('.skill-detail[open] .per-skill-events')")
            assert '暴击率' in evaluate("document.querySelector('.skill-detail[open]').textContent")
            assert '平均每次' in evaluate("document.querySelector('.skill-detail[open]').textContent")
            assert evaluate("document.querySelector('.skill-detail[open] .skill-detail-body svg')!==null")
            click('.skill-detail[open] .per-skill-events summary')
            assert evaluate("document.querySelectorAll('.skill-detail[open] .event-table tbody tr').length") > 0
            ready("[...document.querySelectorAll('.skill-icon')].filter(e=>{const r=e.getBoundingClientRect();return e.checkVisibility()&&r.width>0&&r.bottom>0&&r.top<innerHeight}).every(e=>e.complete && e.naturalWidth>0)")
            assert evaluate("document.querySelectorAll('.composition-segment').length") == len([s for s in participant['stats']['skills'] if s.get('damage', 0)>0])
            screenshot('skills-desktop.png')
            evaluate("document.querySelector('.luck-panel').scrollIntoView({block:'start'})")
            screenshot('critical-luck-desktop.png')
            check('real crit luck distribution, expanded skill metrics, damage histogram and per-skill events')
    click('.portal-back')
    ready("location.hash.startsWith('#history') && document.querySelector('.history-filters')")

    named = evaluate("fetch('/api/v1/dps/public/records?eligibility=included').then(r=>r.json()).then(d=>d.records.find(r=>r.public_mode!=='unrecorded'))")
    if named:
        route('home', "!document.querySelector('.portal-shell').classList.contains('is-visible')")
        evaluate(f"document.querySelector('.search input').value={json.dumps(named['display_name'])}")
        click('.search button')
        ready(f"document.querySelector('.history-filters input[name=q]')?.value==={json.dumps(named['display_name'])} && document.querySelector('.history-row:not(.history-head)')")
        assert named['display_name'] in evaluate("document.querySelector('.portal-title').textContent")
        check('public character search from homepage and detail return navigation')

    route('history?eligibility=not_eligible', "document.querySelector('.history-filters select[name=eligibility]')?.value==='not_eligible' && document.querySelector('.history-row:not(.history-head)')")
    click('.history-row:not(.history-head) .row-open')
    ready("document.querySelector('.qualification-note') && document.querySelector('.participant-row')")
    assert '未纳入正式统计' in evaluate("document.querySelector('.qualification-note').textContent")
    check('non-ranking historical detail with actual qualification reasons')

    route('history?profession=1200001', "document.querySelector('.history-filters select[name=profession]')?.value==='1200001' && document.querySelector('.history-row:not(.history-head)')")
    filtered = evaluate("fetch('/api/v1/dps/public/records?profession=1200001').then(r=>r.json())")
    assert str(filtered['total']) in evaluate("document.querySelector('.section-meta').textContent.replaceAll(',','')")
    check('profession filter against the complete database')

    route('history?q=%3Cimg%20src%3Dx%20onerror%3Dalert(1)%3E', "document.querySelector('.history-filters input[name=q]')?.value.includes('<img')")
    ready("document.querySelector('.portal-content')?.textContent.includes('没有符合条件')")
    assert evaluate("document.querySelectorAll('img[src=x]').length") == 0
    check('search empty state and untrusted names rendered as text')

    route('records', "document.querySelector('.record-row') && document.querySelector('.history-filters')")
    ranking = evaluate("fetch('/api/v1/dps/public/leaderboards?limit=500').then(r=>r.json()).then(d=>d.leaderboards)")
    assert evaluate("document.querySelectorAll('.record-row').length") == len(ranking)
    chosen = ranking[0]
    evaluate(f"document.querySelector('.history-filters select[name=boss]').value={json.dumps(chosen['boss_name'])}; document.querySelector('.history-filters select[name=boss]').dispatchEvent(new Event('change'))")
    ready("document.querySelector('.record-row') && document.querySelector('.history-filters select[name=boss]')?.value!==''")
    assert evaluate("[...document.querySelectorAll('.record-row .boss-name')].every(e=>e.textContent===document.querySelector('select[name=boss]').value)")
    check('qualified Boss leaderboard and filters')

    route('insights', "document.querySelector('.performance-filter select[aria-label=Boss]') && document.querySelector('.performance-source')")
    assert '真实' in evaluate("document.querySelector('.performance-source').textContent")
    assert '演示' not in evaluate("document.querySelector('.portal-content').textContent")
    ready("document.querySelector('.performance-row') || document.querySelector('.performance-empty')")
    screenshot('professions-desktop.png')
    route('insights?boss=name%3A不存在的首领', "document.querySelector('.performance-empty') && document.querySelector('.performance-source')")
    assert '真实' in evaluate("document.querySelector('.performance-source').textContent")
    check('real profession statistics and empty sample state without invented fallback')

    for width, height in ((1024, 768), (760, 700), (390, 844)):
        call('Emulation.setDeviceMetricsOverride', {'width': width, 'height': height, 'deviceScaleFactor': 1, 'mobile': False})
        route('home', "!document.querySelector('.portal-shell').classList.contains('is-visible')")
        assert evaluate("document.documentElement.scrollWidth<=innerWidth")
        assert evaluate("document.querySelector('#uploadStatusBtn').getBoundingClientRect().right<=innerWidth")
        assert evaluate("document.querySelector('#uploadStatusBtn').getBoundingClientRect().bottom<=document.querySelector('.header').getBoundingClientRect().bottom")
        click('#uploadStatusBtn')
        assert evaluate("document.querySelector('#uploadPopover').getBoundingClientRect().right<=innerWidth")
        click('#manualUploadBtn')
        ready("document.querySelectorAll('.share-step').length===4")
        assert evaluate("document.documentElement.scrollWidth<=innerWidth")
        route('history', "document.querySelector('.history-row:not(.history-head)')")
        assert evaluate("document.documentElement.scrollWidth<=innerWidth")
        if width == 390:
            screenshot('history-mobile.png')
            click('.history-row:not(.history-head) .row-open')
            ready("document.querySelector('.participant-row') && document.querySelector('.skill-events')")
            assert evaluate("document.documentElement.scrollWidth<=innerWidth")
            evaluate("document.querySelector('.portal-scroll').scrollTop=0")
            screenshot('battle-mobile.png')
            if gear:
                battle, participant = gear
                route(f"battle?id={battle['encounter_id']}", "document.querySelector('.skill-events')")
                click(f'.participant-row[data-slot="{participant["slot"]}"]')
                click('.equipment-item summary')
                assert evaluate("document.querySelector('.equipment-grid').scrollWidth<=innerWidth")
                screenshot('equipment-mobile.png')
            battle, participant = luck
            route(f"battle?id={battle['encounter_id']}", "document.querySelector('.skill-events')")
            click(f'.participant-row[data-slot="{participant["slot"]}"]')
            sampled_skill = next(s for s in participant['stats']['skills'] if s.get('damage', 0)>0 and any(e['skill_id']==s['skill_id'] and e['damage']>0 for e in participant['stats']['skill_timeline']))
            click(f'.skill-detail[data-skill-id="{sampled_skill["skill_id"]}"] summary')
            ready("document.querySelector('.skill-detail[open] .per-skill-events')")
            assert evaluate("document.querySelector('.skill-detail[open]').getBoundingClientRect().right<=innerWidth")
            screenshot('skills-mobile.png')
            evaluate("document.querySelector('.luck-panel').scrollIntoView({block:'start'})")
            screenshot('critical-luck-mobile.png')
            route('home', "!document.querySelector('.portal-shell').classList.contains('is-visible')")
            screenshot('home-mobile.png')
        check(f'responsive navigation, upload help and content at {width}x{height}')
    route('battle?id=invalid', "document.querySelector('.portal-status')?.textContent.includes('地址无效')")
    route('home', "!document.querySelector('.portal-shell').classList.contains('is-visible')")
    assert not errors, errors
    assert not failures, failures
    external = [url for url in requests if url.startswith(('http:', 'https:')) and not url.startswith(args.url.rstrip('/') + '/')]
    assert not external, external
    check('no JavaScript exceptions, HTTP resource failures or external resource requests')
    report = {'status': 'passed', 'url': args.url, 'history_encounters': history['total'],
              'qualified_encounters': statistics['encounters'], 'checks': checks,
              'javascript_errors': len(errors), 'http_failures': len(failures)}
    (output / 'result.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=True, indent=2), flush=True)
finally:
    if connection is not None:
        connection.close()
    browser.terminate()
    try:
        browser.wait(timeout=10)
    except subprocess.TimeoutExpired:
        browser.kill()
    log.close()
