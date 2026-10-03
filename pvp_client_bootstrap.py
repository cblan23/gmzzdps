"""Recover missed PvP scene/player identities from the client's own lifecycle log.

This is a bounded startup snapshot, not a combat data source. No requests,
hooks, equipment cache, damage amounts or death events are read from the log.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
import re
import time

from pvp_records import RECORDABLE_MAPS


SOURCE = 'npcap_read_only_pvp_lifecycle'
TAIL_BYTES = 8 * 1024 * 1024
_STAMP = re.compile(r'^\[(\d{4}\.\d{2}\.\d{2}-\d{2}\.\d{2}\.\d{2}:\d{3})\]')
_SPACE = re.compile(r'\[Entity\((\w+Space), ([A-Za-z0-9_-]{8,32})\)\].*'
                    r'Space ctor entityId\s*:\s*([A-Za-z0-9_-]{8,32}), TemplateID:(\d+)\b')
_SPACE_END = re.compile(r'\[Entity\((\w+Space), ([A-Za-z0-9_-]{8,32})\)\].*'
                        r'\[Space\]\[dtor\] entityId:([A-Za-z0-9_-]{8,32})(?=\s|$)')
_CREATED = re.compile(r'create_entity Entity:([A-Za-z0-9_-]{12,32}), Uid:(\d+) '
                      r'cname:(AvatarActor|\w+Space)\b.*is_brief:(true|false)\b')
_DESTROYED = re.compile(r'destroy_entity Entity:([A-Za-z0-9_-]{12,32}), Uid:(\d+)\b')


def _time_ns(line):
    match = _STAMP.match(line)
    if not match:
        return None
    try:
        stamp = datetime.strptime(match[1], '%Y.%m.%d-%H.%M.%S:%f').astimezone()
        return int(stamp.timestamp() * 1_000_000_000)
    except (ValueError, OverflowError, OSError):
        return None


def pvp_lifecycle_records(log_path, *, until_ns=None, include_non_pvp=False):
    """Return only identities in the latest still-open, explicitly named Space.

    ``until_ns`` is an offline audit bound. Production reads only the current
    append-only log tail; old arenas closed or replaced by a city are rejected.
    A complete scene constructor AND its matching entity creation are required.
    """
    try:
        path = Path(log_path)
        with path.open('rb') as stream:
            stream.seek(0, 2)
            size = stream.tell()
            start = max(0, size - TAIL_BYTES)
            stream.seek(start)
            text = stream.read(TAIL_BYTES).decode('utf-8', errors='replace')
        if start:
            text = text.partition('\n')[2]
    except (OSError, TypeError, ValueError):
        return []
    scene = None
    scene_entity = 0
    avatars = {}
    latest_stamp = 0
    for line in text.splitlines():
        stamp = _time_ns(line)
        if stamp is None or (until_ns is not None and stamp > until_ns):
            continue
        latest_stamp = max(latest_stamp, stamp)
        match = _SPACE.search(line)
        if match:
            scene_entity = 0
            avatars.clear()
            scene = ({'class': match[1], 'token': match[2],
                      'map_id': int(match[4]), 'stamp': stamp}
                     if match[2] == match[3] else None)
            continue
        match = _SPACE_END.search(line)
        if match and scene and match[2] == match[3] == scene['token']:
            scene = None
            scene_entity = 0
            avatars.clear()
            continue
        if scene is None:
            continue
        match = _CREATED.search(line)
        if match:
            token, entity, cls, brief = match[1], int(match[2]), match[3], match[4]
            if not 0 < entity < 2**64:
                continue
            if token == scene['token'] and cls == scene['class']:
                scene_entity = entity
            elif cls == 'AvatarActor' and brief == 'false':
                avatars[token] = (entity, stamp)
            continue
        match = _DESTROYED.search(line)
        if match:
            token, entity = match[1], int(match[2])
            if token == scene['token'] and entity == scene_entity:
                scene = None
                scene_entity = 0
                avatars.clear()
            elif token in avatars and avatars[token][0] == entity:
                avatars.pop(token, None)
    if not scene or not scene_entity:
        return []
    if scene['map_id'] not in RECORDABLE_MAPS and not include_non_pvp:
        return []
    # An old/stopped client's log must never initialize a live encounter.
    if until_ns is None and abs(time.time_ns() - latest_stamp) > 60_000_000_000:
        return []

    def creation(entity, token, cls, properties, stamp):
        return {
            'method': 'NpcapEntityCreated', 'capture_source': SOURCE,
            'capture_event_id': f'pvp-log:{entity}:{stamp}', 'sequence': -1,
            'capture_timestamp_ns': stamp,
            'filetime_100ns': stamp // 100 + 116_444_736_000_000_000,
            'event_time': datetime.fromtimestamp(stamp / 1e9).astimezone().isoformat(),
            'arguments_synchronized': True,
            'network_entity_id': entity, 'script_entity': entity,
            'decoded_arguments': [{
                'entity_id': entity, 'entity_token': token, 'entity_class': cls,
                'properties': properties, 'schema': 'c7-client-lifecycle-field-names',
                'known_property_schema': False,
            }],
        }

    records = [creation(scene_entity, scene['token'], scene['class'],
                        {'TemplateID': scene['map_id'], 'WorldEntityID': scene['token']},
                        scene['stamp'])]
    if scene['map_id'] in RECORDABLE_MAPS:
        records.extend(creation(entity, token, 'AvatarActor', {}, stamp)
                       for token, (entity, stamp) in avatars.items())
    return records
