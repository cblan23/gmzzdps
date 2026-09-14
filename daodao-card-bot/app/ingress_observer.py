"""Independent passive OneBot witness; never sends API calls or chat messages."""
import asyncio
import json
import logging
import os
import random
import time
from collections import Counter
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

from app.config import Settings
from app.utils.diagnostics import emit


def metadata(row, settings):
    """Keep only allowlisted metadata; never retain a message body."""
    if not isinstance(row, dict):
        return {'kind': 'invalid'}
    if row.get('post_type') == 'meta_event':
        if row.get('meta_event_type') == 'heartbeat':
            status = row.get('status')
            online = status.get('online') if isinstance(status, dict) else None
            return {'kind': 'heartbeat', 'online': online if isinstance(online, bool) else None}
        return {'kind': 'lifecycle'}
    if row.get('post_type') != 'message' or row.get('message_type') != 'group':
        return {'kind': 'other'}
    group = str(row.get('group_id', ''))
    if group not in settings.groups:
        return {'kind': 'other_group'}
    message = row.get('message')
    mentioned = isinstance(message, list) and any(
        isinstance(s, dict) and s.get('type') == 'at'
        and isinstance(s.get('data'), dict)
        and str(s['data'].get('qq')) == settings.bot_qq for s in message)
    result = {'kind': 'group', 'group': group, 'mentioned': mentioned}
    if mentioned:
        for key in ('user_id', 'message_id', 'time'):
            value = row.get(key)
            if type(value) is int:
                result[key] = value
    return result


async def observe(settings, log):
    import websockets
    delay = 2
    while True:
        connected_at = time.monotonic()
        try:
            async with websockets.connect(settings.ws_url,
                    additional_headers={'Authorization': 'Bearer ' + settings.ws_token},
                    open_timeout=5, ping_interval=20, ping_timeout=20) as ws:
                log.info('witness_connected')
                emit('ingress','connected')
                counts = Counter()
                report_at = time.monotonic() + 60
                last_online = 'unknown'
                while True:
                    try:
                        payload = await asyncio.wait_for(ws.recv(), max(.01, report_at-time.monotonic()))
                    except asyncio.TimeoutError:
                        payload = None
                    if payload is not None:
                        try:
                            item = metadata(json.loads(payload), settings)
                        except (ValueError, TypeError):
                            item = {'kind': 'invalid'}
                        counts[item['kind']] += 1
                        if item.get('mentioned'):
                            log.info('witness_mention %s', json.dumps(item, separators=(',', ':')))
                            emit('ingress','mention_received',group=item.get('group'),qq=item.get('user_id'),message_id=item.get('message_id'),event_time=item.get('time'),lag_ms=int((time.time()-item['time'])*1000) if type(item.get('time')) is int else None)
                        if item['kind'] == 'heartbeat' and item.get('online') != last_online:
                            last_online = item.get('online')
                            log.info('witness_online=%s', last_online)
                            emit('ingress','online_changed',online=last_online)
                    if time.monotonic() >= report_at:
                        log.info('witness_minute online=%s counts=%s', last_online, json.dumps(dict(counts)))
                        emit('ingress','minute',online=last_online,count=counts['group'])
                        counts.clear()
                        report_at = time.monotonic()+60
        except Exception as error:
            # Exception strings can include connection credentials: type only.
            log.warning('witness_disconnected type=%s', type(error).__name__)
            emit('ingress','disconnected',error_type=type(error).__name__)
        delay = 2 if time.monotonic()-connected_at >= 60 else min(60, delay*2)
        await asyncio.sleep(random.uniform(delay/2, delay))


def main():
    os.umask(0o077)
    settings = Settings.read(bot=True)
    Path('logs').mkdir(exist_ok=True)
    log = logging.getLogger('daodao.witness')
    log.setLevel(logging.INFO)
    log.propagate = False
    output = TimedRotatingFileHandler('logs/qq-ingress.log', when='midnight', backupCount=7, encoding='utf-8')
    output.setFormatter(logging.Formatter('%(asctime)s %(message)s'))
    log.addHandler(output)
    asyncio.run(observe(settings, log))


if __name__ == '__main__':
    main()
