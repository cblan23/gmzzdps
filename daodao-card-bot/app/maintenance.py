"""Authenticated, loopback-only QQ maintenance; never expose service tokens."""
import asyncio
import base64
import binascii
import hashlib
import hmac
import io
import json
import logging
from logging.handlers import TimedRotatingFileHandler
import os
import re
import secrets
import subprocess
import threading
import time
import urllib.request
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlsplit

from app.api import CardApiServer
from app.config import Settings
from app.utils.diagnostics import emit, recent
from app.recovery_policy import RecoveryPolicy, health_state
from app.clients.card_api_client import CardApiClient

PREFIX = '/api/v1/dps/admin/qq/'
ACTIONS = {'recover', 'restart_bot', 'restart_napcat', 'refresh_qr', 'quick_login'}
ORIGINS = {'https://daodaogame.vip', 'https://www.daodaogame.vip'}
_CREDENTIAL_LOCK = threading.Lock()
_CREDENTIAL_CACHE = None
_CREDENTIAL_RETRY_AT = 0.0
_CREDENTIAL_REFRESH_MARGIN = 300.0

class WebuiError(RuntimeError):
    pass

def reset_credential():
    global _CREDENTIAL_CACHE, _CREDENTIAL_RETRY_AT
    with _CREDENTIAL_LOCK:
        _CREDENTIAL_CACHE=None
        _CREDENTIAL_RETRY_AT=0

def login_state():
    return webui('QQLogin/CheckLoginStatus', {}, credential()) or {}

def worker_connection():
    try:
        row=json.loads(Path('data/bot-connection.json').read_text())
        pid=int(row['pid'])
        # The record must belong to the running bot worker, not a reused PID.
        args=Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\0')
        valid=b'app.main' in args and 0<=time.time()-float(row['at'])<=75
        return {'connected':bool(valid and row.get('online') is True),'last_seen':row['at'] if valid else None}
    except (OSError,ValueError,KeyError,TypeError):
        return {'connected':False,'last_seen':None}

def safe_text(value):
    text = str(value or '')
    for name in ('NAPCAT_WEBUI_TOKEN', 'NAPCAT_TOKEN', 'CARD_API_TOKEN', 'GMZZ_MONITOR_ADMIN_PASSWORD'):
        secret = os.environ.get(name, '')
        if secret:
            text = text.replace(secret, '[redacted]')
    text = re.sub(r'GMZZ[A-Za-z0-9]{20,}|https?://\S+', '[redacted]', text)
    text = re.sub(r'[\x00-\x1f\x7f]', ' ', text)
    return text[:500]

def webui(path, body, credential=None):
    headers = {'Content-Type': 'application/json'}
    if credential:
        headers['Authorization'] = 'Bearer ' + credential
    request = urllib.request.Request('http://127.0.0.1:6099/api/' + path,
        data=json.dumps(body).encode(), headers=headers)
    with urllib.request.urlopen(request, timeout=8) as response:
        value = json.loads(response.read(256 * 1024))
    if not isinstance(value, dict) or value.get('code') != 0:
        message = value.get('message') if isinstance(value,dict) else None
        if message == 'Unauthorized' and credential:
            global _CREDENTIAL_CACHE
            with _CREDENTIAL_LOCK:
                if _CREDENTIAL_CACHE and _CREDENTIAL_CACHE[0] == credential:
                    _CREDENTIAL_CACHE = None
        allowed = {'login rate limit':'管理登录请求过于频繁，请等待一分钟',
                   'Unauthorized':'管理会话已失效，请稍后重试',
                   'QQ Is Logined':'QQ 登录进程仍保留旧状态，请检查在线状态或重启 QQ 容器'}
        raise WebuiError(allowed.get(message,'NapCat 管理接口未确认操作成功'))
    return value.get('data')

def credential(*, proactive=False):
    global _CREDENTIAL_CACHE, _CREDENTIAL_RETRY_AT
    with _CREDENTIAL_LOCK:
        now=time.monotonic()
        valid = bool(_CREDENTIAL_CACHE and now < _CREDENTIAL_CACHE[1])
        refresh_due = bool(valid and proactive and now >= _CREDENTIAL_CACHE[1]-_CREDENTIAL_REFRESH_MARGIN)
        if valid and not refresh_due:
            return _CREDENTIAL_CACHE[0]
        if now < _CREDENTIAL_RETRY_AT:
            if valid:return _CREDENTIAL_CACHE[0]
            raise WebuiError('管理认证暂不可用，请等待一分钟再试')
        _CREDENTIAL_RETRY_AT=now+65
        hashed = hashlib.sha256((os.environ['NAPCAT_WEBUI_TOKEN'] + '.napcat').encode()).hexdigest()
        try:
            data = webui('auth/login', {'hash': hashed}) or {}
            token = data.get('Credential')
            if not isinstance(token, str) or not token:
                raise WebuiError('管理认证未完成')
        except Exception:
            if valid and time.monotonic() < _CREDENTIAL_CACHE[1]:
                logging.getLogger('qq-maintenance').warning('management_session_refresh_deferred')
                return _CREDENTIAL_CACHE[0]
            raise
        _CREDENTIAL_CACHE=(token,now+3000)
        _CREDENTIAL_RETRY_AT=0
        logging.getLogger('qq-maintenance').info('management_session_refreshed')
        return token

async def qq_status(settings):
    import websockets
    async with websockets.connect(settings.ws_url, additional_headers={
            'Authorization': 'Bearer ' + settings.ws_token}, open_timeout=4) as socket:
        await socket.send(json.dumps({'action': 'get_status', 'params': {}, 'echo': 'maintenance-status'}))
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            row = json.loads(await asyncio.wait_for(socket.recv(), max(.01, deadline-time.monotonic())))
            if row.get('echo') == 'maintenance-status':
                data = row.get('data') or {}
                return {'connected': True, 'online': data.get('online') if row.get('retcode') == 0 else None,
                        'good': data.get('good')}
    raise RuntimeError('QQ status timeout')

async def qq_live_probe(settings):
    """Read-only uncached self-member query; heartbeat alone is not liveness."""
    import websockets
    groups=sorted(settings.groups)[:2]
    passed=0
    async with websockets.connect(settings.ws_url,additional_headers={'Authorization':'Bearer '+settings.ws_token},open_timeout=4) as ws:
        for index,group in enumerate(groups):
            echo='live-probe-'+str(index)
            await ws.send(json.dumps({'action':'get_group_member_info','params':{
                'group_id':int(group),'user_id':int(settings.bot_qq),'no_cache':True},'echo':echo}))
            deadline=time.monotonic()+7
            while time.monotonic()<deadline:
                try:row=json.loads(await asyncio.wait_for(ws.recv(),max(.01,deadline-time.monotonic())))
                except asyncio.TimeoutError:break
                if row.get('echo')==echo:
                    if row.get('retcode')==0 and str((row.get('data') or {}).get('user_id'))==settings.bot_qq:
                        passed+=1
                    break
    return {'ok':passed>0,'successful_queries':passed,'checked_at':time.time(),
            'note':'Read-only QQ interface health, not proof of message delivery'}

def services():
    rows = {}
    for name in ('daodao-card-bot.service', 'daodao-card-api.service'):
        result = subprocess.run(['systemctl', 'show', name, '-p', 'ActiveState', '-p', 'SubState'],
                                capture_output=True, text=True, timeout=4)
        rows[name] = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
    result = subprocess.run(['docker', 'inspect', '--format', '{{json .State}}', 'daodao-card-bot-napcat-1'],
                            capture_output=True, text=True, timeout=4)
    data = json.loads(result.stdout) if result.returncode == 0 else {}
    rows['napcat'] = {key: data.get(key) for key in ('Running', 'Restarting', 'OOMKilled', 'StartedAt')}
    return rows

def recent_logs():
    path = Path('logs/card-bot.log')
    if not path.exists():
        return []
    with path.open('rb') as stream:
        stream.seek(max(0, path.stat().st_size - 65536))
        if stream.tell():
            stream.readline()
        lines = stream.read().decode('utf-8', errors='replace').splitlines()
    allowed = re.compile(r'^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d[,\.]\d+ (INFO|WARNING|ERROR) '
                         r'(command|claim|claim_first|claim_repeat|private_success|private_failed|group_reply_failed|'
                         r'api_failed|handler_error|bot_starting|napcat_connected|qq_offline_scan_may_be_required|'
                         r'worker_stopped|websocket_worker_disconnected)\b')
    return [safe_text(line) for line in lines if allowed.match(line)][-60:]

class Maintenance:
    def __init__(self, settings):
        self.settings = settings
        self.csrf = secrets.token_urlsafe(32)
        self.operation = None
        self.lock = threading.Lock()
        self.status_lock = threading.Lock()
        self.cache = None
        self.cache_time = 0
        self.last_action = -1000.0
        self.request_ids = deque(maxlen=64)
        self.login_events = deque(maxlen=12)
        self.last_login_state = None
        self.verified_qr = None
        self.awaiting_login = False
        self.login_ready_deadline = 0
        self.login_ready_retry_at = 0
        self.worker_disconnected_since = None
        self.worker_recovery_after = time.monotonic()+60
        self.management_check_after = 0
        self.live_probe={'ok':None,'checked_at':0}
        self.live_probe_after=0
        self.live_probe_lock=threading.Lock()
        self.recovery_policy=RecoveryPolicy()

    def check_live_qq(self):
        with self.live_probe_lock:
            started=time.monotonic()
            try:self.live_probe=asyncio.run(qq_live_probe(self.settings))
            except Exception:self.live_probe={'ok':False,'checked_at':time.time()}
            self.live_probe_after=time.monotonic()+60
            self.recovery_policy.record_probe(self.live_probe['ok'] is True)
            logging.getLogger('qq-maintenance').info('qq_interface_probe ok=%s',self.live_probe['ok'])
            emit('maintenance','qq_probe',outcome='ok' if self.live_probe['ok'] else 'failed',elapsed_ms=int((time.monotonic()-started)*1000))
            if not self.live_probe['ok']:self.diagnostic_snapshot()
            return self.live_probe['ok'] is True

    def diagnostic_snapshot(self):
        """Capture typed runtime state before recovery destroys the evidence."""
        try:
            result=subprocess.run(['docker','inspect','--format','{{json .State}}','daodao-card-bot-napcat-1'],capture_output=True,text=True,timeout=4,check=True)
            state=json.loads(result.stdout)
            emit('maintenance','container_snapshot',running=state.get('Running'),oom=state.get('OOMKilled'),exit_code=state.get('ExitCode'))
        except Exception as error:emit('maintenance','snapshot_failed',error_type=type(error).__name__)

    def observe_login(self):
        try:
            qq = asyncio.run(qq_status(self.settings))
            online, error = qq.get('online'), ''
        except Exception:
            online, error = None, '登录状态接口暂不可用'
        if online is not True:
            try:
                state = webui('QQLogin/CheckLoginStatus', {}, credential()) or {}
                error = safe_text(state.get('loginError'))
            except Exception:
                error = '登录详情接口暂不可用'
        identity = (online, error)
        with self.lock:
            if identity == self.last_login_state:
                return
            self.last_login_state = identity
            event = {'at': time.time(), 'online': online, 'error': error}
            self.login_events.append(event)
        logging.getLogger('qq-maintenance').info('login_state=%s error=%s',online,error or 'none_reported')
        emit('maintenance','login_state',online=online,outcome='error' if error else 'ok',stage='kicked' if 'KickedOffLine' in error else 'login_invalid' if '失效' in error else 'interface_error' if error else 'normal')

    def start_observer(self):
        def watch():
            while True:
                try:
                    self.observe_login()
                    self.complete_login_if_ready()
                    if time.monotonic()>=self.live_probe_after and self.last_login_state and self.last_login_state[0] is True:
                        self.check_live_qq()
                    self.maintain_connections()
                except Exception:
                    logging.getLogger('qq-maintenance').warning('observer_check_failed')
                time.sleep(5)
        threading.Thread(target=watch, name='qq-login-observer', daemon=True).start()

    def maintain_connections(self):
        """Manage WebUI separately; never auto-relogin a QQ-revoked session."""
        now=time.monotonic()
        if now >= self.management_check_after:
            self.management_check_after=now+60
            try:credential(proactive=True)
            except Exception:logging.getLogger('qq-maintenance').warning('management_session_unavailable')
        online=bool(self.last_login_state and self.last_login_state[0] is True)
        with self.lock:
            busy=bool(self.operation and self.operation['state'] in ('running','awaiting_scan')) or self.awaiting_login
        if self.recovery_policy.eligible(time.time(),online,busy):
            # action() serializes manual and automatic recovery. Never bypass its gate.
            self.action('recover','auto-qq-'+secrets.token_hex(8),automatic=True)
            return
        if busy:return
        if not online or worker_connection()['connected']:
            self.worker_disconnected_since=None
            return
        if self.worker_disconnected_since is None:
            self.worker_disconnected_since=now
            return
        if now-self.worker_disconnected_since<45 or now<self.worker_recovery_after:
            return
        with self.lock:
            if self.operation and self.operation['state']=='running':return
            self.worker_recovery_after=now+300
            self.operation={'id':'auto-worker-'+secrets.token_hex(8),'action':'restart_bot',
                            'state':'running','message':'QQ 在线，正在自动恢复发卡连接','at':time.time()}
        try:
            self._ensure_worker()
            self._finish('done','发卡连接已自动恢复，QQ 登录未重启')
        except Exception:
            self._finish('failed','自动恢复发卡连接未确认，五分钟内不重复操作；可手动快速恢复')
        logging.getLogger('qq-maintenance').info('automatic_worker_recovery state=%s',self.operation['state'])

    def complete_login_if_ready(self):
        with self.lock:
            now = time.monotonic()
            if self.awaiting_login and self.login_ready_deadline and now >= self.login_ready_deadline:
                self.awaiting_login = False
                self.operation.update(state='failed',message='登录后接口持续未就绪，请使用快速恢复',finished_at=time.time())
                logging.getLogger('qq-maintenance').warning('post_login_readiness_timeout')
                return
            finish = (self.awaiting_login and self.last_login_state
                      and self.last_login_state[0] is True
                      and self.operation and self.operation['state'] in ('awaiting_scan','running')
                      and now >= self.login_ready_retry_at)
            if finish:
                if not self.login_ready_deadline:self.login_ready_deadline=now+120
                self.operation.update(state='running',message='QQ 已上线，正在恢复发卡连接')
        if finish:
            try:
                self._ensure_worker()
                self.awaiting_login=False
                self._finish('done','QQ 与发卡服务已连接，可以在群内领卡')
            except Exception:
                self.login_ready_retry_at=time.monotonic()+15
                self._step('QQ 已登录，接口仍在准备，正在自动重试，请勿重复扫码')
                logging.getLogger('qq-maintenance').warning('post_login_readiness_retry')

    def status(self):
        with self.status_lock:
            if self.cache is None or time.monotonic() - self.cache_time > 15:
                def attempt(function, failure):
                    try:
                        return function()
                    except Exception:
                        return failure
                def api_stats():
                    client = CardApiClient(self.settings.api_url, self.settings.api_token)
                    data = client.post(self.settings.api_url.rsplit('/',1)[0]+'/stats',
                                       {'admin_qq': sorted(self.settings.admins)[0]})
                    return {key: data.get(key) for key in ('claimed', 'today_claimed', 'mode')}
                with ThreadPoolExecutor(max_workers=3) as pool:
                    qq = pool.submit(attempt, lambda: asyncio.run(qq_status(self.settings)), {'connected':False,'online':None})
                    service = pool.submit(attempt, services, {'error': '服务状态暂不可用'})
                    cards = pool.submit(attempt, api_stats, {'error': '发卡 API 暂不可用'})
                    self.cache = {'qq': qq.result(), 'services': service.result(), 'cards': cards.result()}
                self.cache_time = time.monotonic()
        with self.lock:
            operation = dict(self.operation) if self.operation else None
            login_events = list(self.login_events)
        worker=worker_connection()
        health=health_state(self.cache['qq'].get('online'),worker['connected'],self.live_probe,operation,time.time())
        return {**self.cache, 'bot_qq': self.settings.bot_qq, 'groups': sorted(self.settings.groups),
                'health':{'state':health,'consecutive_failures':self.recovery_policy.failures,
                          'automatic_recovery_available':not self.recovery_policy.storage_error,
                          'next_auto_recovery_at':self.recovery_policy.last_attempt+self.recovery_policy.COOLDOWN},
                'qq_interface_health':dict(self.live_probe),
                'card_api_remote': os.environ.get('CARD_API_MANAGED_LOCALLY','1')=='0',
                'worker':worker, 'logs': recent_logs(), 'login_events': login_events,
                'operation': operation, 'csrf': self.csrf, 'server_time': time.time()}

    def login(self):
        state = login_state()
        result = {'online': bool(state.get('isLogin')), 'error': safe_text(state.get('loginError')), 'qr': None}
        url = state.get('qrcodeurl')
        with self.lock:
            verified = self.verified_qr
        fresh = verified and url==verified[0] and time.monotonic()-verified[1]<90
        if not result['online'] and fresh and not result['error'] and isinstance(url, str) and 0 < len(url) <= 4096:
            import qrcode
            image = qrcode.make(url)
            buffer = io.BytesIO()
            image.save(buffer, format='PNG')
            result['qr'] = 'data:image/png;base64,' + base64.b64encode(buffer.getvalue()).decode('ascii')
            result['refresh_in_seconds']=max(0,int(90-(time.monotonic()-verified[1])))
        elif not result['online']:
            result['needs_refresh']=True
            result['error']=result['error'] or '二维码未验证或显示时间已到，请刷新二维码；刷新失败时使用快速恢复'
        return result

    def _step(self,message):
        with self.lock:
            if self.operation:self.operation.update(message=message)

    def _finish(self,state,message):
        with self.lock:
            if self.operation:self.operation.update(state=state,message=message,finished_at=time.time())
        self.cache_time=0
        emit('maintenance','recovery_state',outcome=state)

    def _wait(self,predicate,seconds):
        deadline=time.monotonic()+seconds
        while time.monotonic()<deadline:
            try:
                value=predicate()
                if value:return value
            except Exception:
                pass
            time.sleep(1)
        raise WebuiError('等待恢复超时，请查看状态并重新点击快速恢复')

    def _ensure_worker(self):
        self._wait(lambda:asyncio.run(qq_status(self.settings)).get('online') is True,15)
        if not self.check_live_qq():
            raise WebuiError('QQ显示在线但实时接口无响应，不能确认恢复成功，请使用快速恢复')
        if not worker_connection()['connected']:
            self._step('QQ 已在线，正在重启发卡进程并确认连接')
            subprocess.run(['systemctl','restart','daodao-card-bot.service'],check=True,timeout=30,capture_output=True)
            self._wait(lambda:worker_connection()['connected'],45)
        client=CardApiClient(self.settings.api_url,self.settings.api_token)
        try:
            client.post(self.settings.api_url.rsplit('/',1)[0]+'/stats',{'admin_qq':sorted(self.settings.admins)[0]})
        except Exception:
            if os.environ.get('CARD_API_MANAGED_LOCALLY','1')=='0':
                # In split deployment the authoritative API lives on ECS.
                # Never restart an obsolete local card API/database as fallback.
                raise
            api_service=services().get('daodao-card-api.service',{})
            if api_service.get('ActiveState')!='active':
                self._step('正在恢复发卡 API 服务')
                subprocess.run(['systemctl','restart','daodao-card-api.service'],check=True,timeout=30,capture_output=True)
                self._wait(lambda:client.post(self.settings.api_url.rsplit('/',1)[0]+'/stats',{'admin_qq':sorted(self.settings.admins)[0]}),15)
            else:
                raise

    def _fresh_qr(self, previous=None):
        state=login_state()
        if state.get('isLogin'):return 'online'
        old=state.get('qrcodeurl') if previous is None else previous
        with self.lock:self.verified_qr=None
        self._step('正在请求并等待新的二维码，不显示旧二维码')
        webui('QQLogin/RefreshQRcode',{},credential())
        def changed():
            current=login_state()
            if current.get('isLogin'):return 'online'
            url=current.get('qrcodeurl')
            if isinstance(url,str) and 0<len(url)<=4096 and url!=old and not current.get('loginError'):
                return url
            return None
        value=self._wait(changed,25)
        if value=='online':return value
        with self.lock:
            self.verified_qr=(value,time.monotonic())
            self.awaiting_login=True
            self.login_ready_deadline=0
            self.login_ready_retry_at=0
        return 'scan'

    def _recover(self, force_restart=False):
        self._step('正在检测 QQ 与发卡连接')
        try:online=asyncio.run(qq_status(self.settings)).get('online') is True
        except Exception:online=False
        if online and not force_restart:
            if self.check_live_qq():
                self._ensure_worker()
                return 'online'
            self._step('检测到假在线：心跳存在但两次实时查询失败，重建登录进程')
        self._step('正在恢复 QQ 登录进程，保留登录数据和领取记录')
        self.diagnostic_snapshot()
        with self.lock:self.verified_qr=None;self.awaiting_login=False
        subprocess.run(['docker','restart','--timeout','15','daodao-card-bot-napcat-1'],check=True,timeout=35,capture_output=True)
        reset_credential()
        self._step('等待 QQ 登录服务启动')
        self._wait(login_state,90)
        result=self._fresh_qr()
        if result=='online':self._ensure_worker()
        return result

    def action(self, name, request_id, automatic=False):
        if name not in ACTIONS or not re.fullmatch(r'[a-zA-Z0-9-]{8,80}', request_id):
            raise ValueError('Invalid action')
        with self.lock:
            for prior in self.request_ids:
                if prior['id']==request_id:
                    return dict(prior)
            if (self.operation and self.operation['state'] == 'running') or time.monotonic()-self.last_action < 15:
                raise BlockingIOError('请等待当前操作完成，稍后再试')
            if automatic:
                busy=bool(self.operation and self.operation['state']=='awaiting_scan') or self.awaiting_login
                online=bool(self.last_login_state and self.last_login_state[0] is True)
                if not self.recovery_policy.eligible(time.time(),online,busy):
                    raise BlockingIOError('自动恢复条件已改变')
                if not self.recovery_policy.reserve(time.time()):
                    raise BlockingIOError('自动恢复预算无法保存，停止自动重启')
                emit('maintenance','automatic_recovery',outcome='reserved')
            self.last_action = time.monotonic()
            self.awaiting_login=False
            self.operation = {'id':request_id, 'action':name, 'state':'running', 'message':'正在处理', 'at':time.time()}
            self.request_ids.append(self.operation)
            threading.Thread(target=self._perform, args=(name,), daemon=True).start()
            return dict(self.operation)

    def _perform(self, name):
        logging.getLogger('qq-maintenance').info('action=%s state=started',name)
        emit('maintenance','recovery_action',action=name,outcome='started')
        self.observe_login()
        try:
            if name in ('recover','restart_napcat','quick_login'):
                result=self._recover(force_restart=name=='restart_napcat')
                if result=='scan':
                    self._finish('awaiting_scan','新二维码已生成，请在此页面扫码并在手机确认；上线后自动连接发卡')
                    return
                message='QQ 与发卡服务已连接，可以在群内领卡'
            elif name == 'restart_bot':
                subprocess.run(['systemctl','restart','daodao-card-bot.service'],check=True,timeout=30,capture_output=True)
                self._wait(lambda:worker_connection()['connected'],45)
                self._ensure_worker()
                message = '发卡进程已重新连接'
            else:
                result=self._fresh_qr()
                if result=='scan':
                    self._finish('awaiting_scan','新二维码已生成，请扫码；登录后自动确认发卡连接')
                    return
                self._ensure_worker()
                message='QQ 与发卡服务已连接，可以在群内领卡'
            state = 'done'
        except WebuiError as error:
            state, message = 'failed', str(error)
        except Exception:
            state, message = 'failed', '操作未确认完成，请刷新状态查看；登录失败时使用扫码登录'
        with self.lock:
            self.operation.update(state=state, message=message, finished_at=time.time())
        self.cache_time = 0
        logging.getLogger('qq-maintenance').info('action=%s state=%s',name,state)
        emit('maintenance','recovery_action',action=name,outcome=state)

def make_handler(maintenance, admin_user, admin_password):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass
        def setup(self):
            super().setup()
            self.connection.settimeout(15)
        def reply(self, code, data, mime='application/json; charset=utf-8'):
            raw = data if isinstance(data,bytes) else json.dumps(data,ensure_ascii=False).encode()
            self.send_response(code)
            if code == 401:
                self.send_header('WWW-Authenticate','Basic realm="DPS Monitor"')
            self.send_header('Content-Type',mime)
            self.send_header('Content-Length',str(len(raw)))
            self.send_header('Cache-Control','no-store')
            self.send_header('X-Content-Type-Options','nosniff')
            self.send_header('X-Frame-Options','DENY')
            self.end_headers()
            try:self.wfile.write(raw)
            except OSError:pass
        def authenticated(self):
            try:
                header=self.headers.get('Authorization','')
                if not admin_password or not header.startswith('Basic '):raise ValueError()
                username,password=base64.b64decode(header[6:],validate=True).decode().split(':',1)
                if hmac.compare_digest(username,admin_user) and hmac.compare_digest(password,admin_password):return True
            except (ValueError,UnicodeError,binascii.Error):pass
            self.reply(401,{'error':'admin_required'});return False
        def do_GET(self):
            if not self.authenticated():return
            path=urlsplit(self.path).path
            try:
                if path == PREFIX+'status':self.reply(200,maintenance.status())
                elif path == PREFIX+'diagnostics':self.reply(200,{'events':recent(3000),'generated_at':time.time(),'note':'API确认不等同于用户实际收到消息；仅包含允许的诊断字段'})
                elif path == PREFIX+'login':self.reply(200,maintenance.login())
                elif path == '/dps-monitor/qq-maintenance.js':
                    self.reply(200,Path('app/qq-maintenance.js').read_bytes(),'application/javascript; charset=utf-8')
                else:self.reply(404,{'error':'not_found'})
            except Exception:self.reply(503,{'error':'机器人维护连接暂不可用，请稍后刷新'})
        def do_POST(self):
            if not self.authenticated():return
            if self.headers.get('Origin') not in ORIGINS or not hmac.compare_digest(self.headers.get('X-Daodao-CSRF',''),maintenance.csrf):
                self.reply(403,{'error':'request_verification_failed'});return
            if urlsplit(self.path).path != PREFIX+'action':
                self.reply(404,{'error':'not_found'});return
            try:
                if self.headers.get('Content-Type','').split(';')[0] != 'application/json':raise ValueError()
                length=int(self.headers.get('Content-Length','0'))
                if not 0<length<=2048:raise ValueError()
                value=json.loads(self.rfile.read(length))
                if not isinstance(value,dict):raise ValueError()
                action=maintenance.action(str(value.get('action','')),str(value.get('request_id','')))
                self.reply(202,{'operation':action})
            except BlockingIOError:self.reply(429,{'error':'操作处理中或过于频繁，请稍后重试'})
            except (ValueError,TypeError):self.reply(400,{'error':'invalid_request'})
            except Exception:self.reply(503,{'error':'maintenance_unavailable'})
    return Handler

def main():
    os.umask(0o077)
    logger=logging.getLogger('qq-maintenance')
    logger.setLevel(logging.INFO)
    handler=TimedRotatingFileHandler('logs/qq-maintenance.log',when='midnight',backupCount=14,encoding='utf-8')
    handler.setFormatter(logging.Formatter('%(asctime)s %(message)s'))
    logger.addHandler(handler)
    settings=Settings.read(bot=True)
    password=os.environ.get('GMZZ_MONITOR_ADMIN_PASSWORD','')
    if not password:raise RuntimeError('Admin authentication not configured')
    service=Maintenance(settings)
    service.start_observer()
    server=CardApiServer(('127.0.0.1',8771),make_handler(service,os.environ.get('GMZZ_MONITOR_ADMIN_USER','admin'),password))
    server.serve_forever()

if __name__=='__main__':main()
