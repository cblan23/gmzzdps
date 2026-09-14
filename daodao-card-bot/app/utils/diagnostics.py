"""Bounded structured diagnostics; only explicitly typed metadata is accepted."""
import contextvars
import json
import logging
import os
import re
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

trace = contextvars.ContextVar('diagnostic_trace', default='')
_writers = {}
_lock = threading.Lock()
CHANNELS = ('worker', 'maintenance', 'ingress')
NUMBERS = {'elapsed_ms', 'http_status', 'retcode', 'attempt', 'pending', 'pid',
           'event_time', 'lag_ms', 'count', 'exit_code', 'native_result'}
IDS = {'qq', 'group', 'message_id'}
LABELS = {'stage', 'outcome', 'error_type', 'command', 'action'}


def clean_fields(fields):
    clean = {}
    for key, value in fields.items():
        if key in NUMBERS and type(value) is int:
            clean[key] = value
        elif key in IDS and re.fullmatch(r'-?\d{1,20}', str(value)):
            clean[key] = str(value)
        elif key in LABELS and isinstance(value, str) and re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]{0,63}', value):
            clean[key] = value
        elif key in ('online', 'running', 'oom') and type(value) is bool:
            clean[key] = value
    return clean


def emit(channel, event, **fields):
    try:
        if channel not in CHANNELS or not re.fullmatch(r'[a-z_]{1,64}', event):return
        row = {'at': round(time.time(), 3), 'channel': channel, 'event': event,
               'pid': os.getpid(), **clean_fields(fields)}
        current = trace.get()
        if re.fullmatch(r'[a-f0-9]{16}', current):row['trace'] = current
        with _lock:
            if channel not in _writers:
                Path('logs').mkdir(exist_ok=True)
                handler = RotatingFileHandler('logs/diagnostic-'+channel+'.jsonl',
                                              maxBytes=2*1024*1024, backupCount=4, encoding='utf-8')
                handler.setFormatter(logging.Formatter('%(message)s'))
                _writers[channel] = handler
            # Dedicated handler avoids third-party logger filters and payloads.
            _writers[channel].handle(logging.LogRecord('daodao.diagnostic', logging.INFO,
                 '', 0, json.dumps(row, separators=(',', ':')), (), None))
    except Exception:
        # Diagnostic failures must never interrupt card delivery.
        pass


def recent(limit=500, root=Path('logs')):
    rows = []
    for channel in CHANNELS:
        for suffix in ('.1', ''):
            path = root/('diagnostic-'+channel+'.jsonl'+suffix)
            try:
                with path.open('rb') as stream:
                    stream.seek(max(0, path.stat().st_size-256*1024))
                    if stream.tell():stream.readline()
                    lines=stream.read(256*1024).splitlines()
            except OSError:continue
            for line in lines:
                try:
                    row=json.loads(line)
                    if not isinstance(row,dict) or not isinstance(row.get('at'),(int,float)):continue
                    event=row.get('event','')
                    if not re.fullmatch(r'[a-z_]{1,64}',event):continue
                    safe={'at':row['at'],'channel':channel,'event':event,**clean_fields(row)}
                    if re.fullmatch(r'[a-f0-9]{16}',str(row.get('trace',''))):safe['trace']=row['trace']
                    rows.append(safe)
                except (ValueError,TypeError):continue
    return sorted(rows,key=lambda row:row['at'])[-min(3000,max(1,limit)):]
