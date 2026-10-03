"""Replay passive PVP records without capture, login or production writes.

Example (startup token proven by the accompanying live-profile/native logs):
  python tools/replay_pvp_capture.py logs/network_20260917_215040.jsonl \
    --self-token AQAAAOwNkGB8AAAA --until 2026-09-17T22:04:42.333860 \
    --preview .codex-tmp/pvp-recorded-live.png
"""
from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import re
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from npcap_parser_adapter import NpcapParserAdapter
from pvp_tracker import PvpTracker, timestamp_ns
from pvp_records import PvpHistoryRepository, PvpRecordingController


def recorded_space_maps(path):
    """Recover omitted historical map fields from exact client-log tokens.

    Old captures dropped unknown Space schemas. This explicit offline option
    is audit evidence, not a production map detector or a guessed wire index.
    """
    pattern = re.compile(r'\[Entity\((\w+Space), ([A-Za-z0-9_-]+)\)\].*'
                         r'Space ctor entityId\s*:\s*([A-Za-z0-9_-]+), TemplateID:(\d+)')
    evidence = {}
    with path.open(encoding='utf-8-sig', errors='replace') as source:
        for line in source:
            match = pattern.search(line)
            if match and match[2] == match[3]:
                evidence[(match[1], match[2])] = int(match[4])
    return evidence


def replay(path, self_token='', until='', *, window_totals=False, space_log=None,
           since='', pvp_client_log=None):
    space_maps = recorded_space_maps(space_log) if space_log else {}
    recovered_spaces = 0
    runtime_profiles = {}
    if self_token:
        with path.open(encoding='utf-8-sig') as stream:
            for line in stream:
                record = json.loads(line)
                if record.get('method') == 'NpcapLiveTeamProfile' and record.get('user_token') == self_token:
                    # Bootstrap identity only, not a future rating/equipment.
                    runtime_profiles[self_token] = {key: record[key] for key in ('name', 'role_number') if key in record}
                    break
    parser = NpcapParserAdapter(runtime_profiles, remembered_self_token=self_token)
    tracker = PvpTracker()
    last_stamp = 0
    records = 0
    until_time = datetime.fromisoformat(until) if until else None
    since_time = datetime.fromisoformat(since) if since else None
    since_ns = int(since_time.astimezone().timestamp() * 1e9) if since_time else 0
    if pvp_client_log and not since_time:
        raise ValueError('--pvp-client-log requires an explicit --since snapshot time')
    temporary = tempfile.TemporaryDirectory() if window_totals else None
    controller = (PvpRecordingController(PvpHistoryRepository(Path(temporary.name) / 'offline-pvp.sqlite3'))
                  if temporary is not None else None)
    if controller is not None:
        controller.bind_account('offline-replay')
        tracker = controller.current
    bootstrap_identities = 0
    if pvp_client_log:
        from pvp_client_bootstrap import pvp_lifecycle_records
        initial_records = pvp_lifecycle_records(pvp_client_log, until_ns=since_ns)
        for initial_record in initial_records:
            last_stamp = max(last_stamp, timestamp_ns(initial_record))
            for kind, value in parser.process(initial_record):
                if controller is None:
                    tracker.ingest_update(kind, value)
                else:
                    controller.ingest_update(kind, value)
            for observation in parser.take_pvp_observations():
                if controller is None:
                    tracker.consume(observation)
                else:
                    controller.observe(observation)
        bootstrap_identities = len(initial_records)
    with path.open(encoding='utf-8-sig') as stream:
        for line in stream:
            record = json.loads(line)
            event_time = str(record.get('event_time') or '')
            if until_time and event_time:
                observed_time = datetime.fromisoformat(event_time)
                bound = until_time
                if bound.tzinfo is None:
                    bound = bound.replace(tzinfo=observed_time.tzinfo)
                if observed_time > bound:
                    break
            if record.get('diagnostic_type'):
                continue
            if since_time and timestamp_ns(record) < since_ns:
                continue
            if record.get('method') == 'NpcapEntityCreated' and space_maps:
                args = record.get('decoded_arguments', [])
                creation = args[0] if args and isinstance(args[0], dict) else {}
                key = (creation.get('entity_class'), creation.get('entity_token'))
                if key in space_maps and not creation.get('properties', {}).get('TemplateID'):
                    creation['properties'] = {**creation.get('properties', {}),
                                              'TemplateID': space_maps[key]}
                    record['offline_space_evidence'] = str(space_log)
                    recovered_spaces += 1
            records += 1
            last_stamp = max(last_stamp, timestamp_ns(record))
            if record.get('method') == 'NpcapLiveTeamProfile':
                updates = parser.apply_read_only_team_profile(record)
            else:
                updates = parser.process(record)
            for kind, value in updates:
                if controller is None:
                    tracker.ingest_update(kind, value)
                elif kind == 'scene':
                    controller.ingest_scene(value)
                else:
                    controller.ingest_update(kind, value)
            for observation in parser.take_pvp_observations():
                if controller is not None:
                    controller.observe(observation)
                else:
                    tracker.consume(observation)
    if controller is not None:
        controller.poll(last_stamp)
    view = controller.snapshot(last_stamp) if controller is not None else tracker.snapshot(last_stamp)
    single_matches = controller.repository.list('offline-replay') if controller is not None else []
    if temporary is not None:
        temporary.cleanup()
    self_profile = tracker.profiles.get(tracker.self_id, {})
    return {'capture_file': str(path), 'records': records,
            'space_log_evidence': str(space_log) if space_log else None,
            'pvp_bootstrap_identities': bootstrap_identities,
            'historical_spaces_recovered': recovered_spaces,
            'parser_errors': parser.pvp_observation_errors,
            'self_id': tracker.self_id, 'self_token': tracker.self_token,
            'self_profile': self_profile, 'pvp': view, 'single_matches': single_matches}


def render_preview(report, path):
    from PIL import Image, ImageDraw, ImageFont
    from main_hud import MainHudRenderer
    from tools.preview_main_hud import COLORS

    view = report['pvp']
    profile = report['self_profile']
    snapshot = {'combat_mode': 'pvp', 'time': view['time'],
                'pvp_player_name': profile.get('name') or '未识别',
                'pvp_profession_id': profile.get('profession_id', 0),
                'pvp_rating': profile.get('extraordinary_rating'),
                'pvp_map_name': view.get('map_name') or view['module_name'], 'pvp_result': view['result'],
                'pvp_in_map': bool(view.get('map_name')),
                'pvp_kills': view['kills'], 'pvp_deaths': view['deaths'],
                'pvp_assists': view['assists'], 'pvp_total_damage': view['total_damage'],
                'pvp_total_taken': view['total_taken'],
                'pvp_outgoing': view['outgoing'], 'pvp_incoming': view['incoming']}
    frame = MainHudRenderer(ROOT / 'assets', COLORS).render(snapshot, pixel_scale=1.5).image
    board = Image.new('RGBA', (frame.width + 48, frame.height + 80), '#d8dee5')
    font = ImageFont.truetype(r'C:\Windows\Fonts\msyh.ttc', 13)
    ImageDraw.Draw(board).text((24, 10), '实测被动数据回放（非演示数据）', font=font, fill='#24313d')
    board.alpha_composite(frame, (24, 38))
    path.parent.mkdir(parents=True, exist_ok=True)
    board.convert('RGB').save(path)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('capture_file', type=Path)
    ap.add_argument('--self-token', default='', help='Only a log-proven startup local-role token')
    ap.add_argument('--until', default='', help='ISO event_time upper bound')
    ap.add_argument('--preview', type=Path)
    ap.add_argument('--window-totals', action='store_true', help='Replay HUD totals plus independent local history (temporary database only)')
    ap.add_argument('--space-log', type=Path, help='Offline C7.log evidence for Space map fields omitted by old capture schemas')
    ap.add_argument('--since', default='', help='ISO lower bound; also the explicit PvP lifecycle bootstrap time')
    ap.add_argument('--pvp-client-log', type=Path, help='Recover missed startup PvP identities from C7.log at --since (offline only)')
    args = ap.parse_args()
    report = replay(args.capture_file, args.self_token, args.until, window_totals=args.window_totals,
                    space_log=args.space_log, since=args.since, pvp_client_log=args.pvp_client_log)
    print(json.dumps(report, ensure_ascii=True, indent=2))
    if args.preview:
        render_preview(report, args.preview)


if __name__ == '__main__':
    main()
