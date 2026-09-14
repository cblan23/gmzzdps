"""Compare two archived encounters without changing either history file.

Pass records for the SAME fight. Missing fields never become zero. Amounts,
counts and rates compare exactly; explicit timestamp fields permit a caller
chosen tolerance. Lists retain event order; participant lists use actor IDs.
"""
import argparse
import json
import math
from pathlib import Path


FIELDS = (
    'dungeon_id', 'dungeon_stage_id', 'dungeon_stage_phase', 'team_size',
    'started_at_epoch', 'ended_at_epoch', 'duration_seconds', 'dps_duration_seconds',
    'hps_duration_seconds', 'total_damage', 'team_dps', 'team_hps', 'team_total_healing',
    'team_effective_healing', 'team_overhealing', 'team_taken', 'monster', 'targets',
    'participants', 'healers', 'damage_taken', 'boss_damage', 'event_log',
    'skill_cast_log', 'dps_timeline', 'team_dps_timeline', 'participant_damage_samples',
)
MISSING = object()


def compare_encounters(legacy, passive, *, timestamp_tolerance_seconds=0.05):
    differences = []
    compared = 0

    def visit(left, right, path):
        nonlocal compared
        if left is MISSING or right is MISSING:
            if left is not right:
                differences.append({'path': path, 'reason': 'missing_field',
                                    'legacy_present': left is not MISSING, 'npcap_present': right is not MISSING})
            return
        if isinstance(left, dict) and isinstance(right, dict):
            for key in sorted(set(left) | set(right)):
                visit(left.get(key, MISSING), right.get(key, MISSING), f'{path}.{key}')
            return
        if isinstance(left, list) and isinstance(right, list):
            # Never align events by amounts: doing that can hide real losses.
            if left and right and all(isinstance(row, dict) and 'actor_id' in row for row in left + right):
                first = {str(row['actor_id']): row for row in left}
                second = {str(row['actor_id']): row for row in right}
                if len(first) == len(left) and len(second) == len(right):
                    visit(first, second, path)
                    return
            if len(left) != len(right):
                differences.append({'path': path, 'reason': 'length', 'legacy': len(left), 'npcap': len(right)})
            for index, (first, second) in enumerate(zip(left, right)):
                visit(first, second, f'{path}[{index}]')
            return
        compared += 1
        equal = left == right
        if isinstance(left, (int, float)) and isinstance(right, (int, float)):
            if not math.isfinite(left) or not math.isfinite(right):
                equal = False
            elif path.endswith(('_at_epoch', 'duration_seconds')):
                equal = abs(left-right) <= timestamp_tolerance_seconds
        if not equal:
            # Do not print names, tokens or event payloads. Local actor IDs
            # remain in diagnostic paths so differences can be investigated.
            item = {'path': path, 'reason': 'value'}
            if isinstance(left, (int, float)) and isinstance(right, (int, float)):
                item['delta'] = right-left
            differences.append(item)

    for field in FIELDS:
        visit(legacy.get(field, MISSING), passive.get(field, MISSING), field)
    required = ('started_at_epoch', 'ended_at_epoch', 'total_damage', 'participants')
    usable = all(field in legacy and field in passive for field in required)
    usable = usable and bool(legacy.get('participants')) and bool(passive.get('participants'))
    return {'comparable': bool(usable), 'matches': bool(usable and not differences),
            'unavailable_in_both': [field for field in FIELDS if field not in legacy and field not in passive],
            'compared_values': compared, 'difference_count': len(differences),
            'differences': differences[:500], 'timestamp_tolerance_seconds': timestamp_tolerance_seconds,
            'independent_passive_validation': 'not_established_by_file_comparison'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('legacy', type=Path)
    parser.add_argument('npcap', type=Path)
    parser.add_argument('--timestamp-tolerance', type=float, default=0.05)
    parser.add_argument('--legacy-index', type=int, default=0)
    parser.add_argument('--npcap-index', type=int, default=0)
    args = parser.parse_args()
    if not math.isfinite(args.timestamp_tolerance) or args.timestamp_tolerance < 0:
        parser.error('timestamp tolerance must be finite and nonnegative')
    first = json.loads(args.legacy.read_text(encoding='utf-8'))
    second = json.loads(args.npcap.read_text(encoding='utf-8'))
    try:
        if isinstance(first, list):
            first = first[args.legacy_index] if args.legacy_index >= 0 else None
        if isinstance(second, list):
            second = second[args.npcap_index] if args.npcap_index >= 0 else None
    except IndexError:
        parser.error('requested encounter index does not exist')
    if not isinstance(first, dict) or not isinstance(second, dict):
        parser.error('each file must contain one encounter object')
    result = compare_encounters(first, second, timestamp_tolerance_seconds=args.timestamp_tolerance)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['matches'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
