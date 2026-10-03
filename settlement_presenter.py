"""UI-ready detached values. Never infer teammate metrics or a missing clock."""
from copy import deepcopy
from combat_history import authoritative_team_combat_seconds
from encounter_tracker import EncounterRecord


def metric_text(value):
    return '--' if value is None else f'{value:,.0f}'


def completed_statistics_scope(record: EncounterRecord) -> str:
    statistics = record.all_statistics
    members = statistics.get('members') if isinstance(statistics, dict) else None
    return (
        'ALL'
        if record.result == 'VICTORY'
        and record.settlement_status == 'SETTLED'
        and isinstance(members, (list, tuple))
        and bool(members)
        else 'STAGE'
    )


def _all_statistics_view(record: EncounterRecord, status: str) -> dict | None:
    statistics = record.all_statistics
    raw_members = statistics.get('members') if isinstance(statistics, dict) else None
    if (
        record.result != 'VICTORY'
        or record.settlement_status != 'SETTLED'
        or not isinstance(raw_members, (list, tuple))
        or not raw_members
        or any(not isinstance(member, dict) or not member.get('id') for member in raw_members)
    ):
        return None

    duration, clock_policy, _member_seconds = authoritative_team_combat_seconds([
        {'combat_seconds_total': member.get('member_battle_length')}
        for member in raw_members
    ])
    duration = duration if duration > 0 else None
    identities = {
        str(identity['id']): deepcopy(identity)
        for identity in record.participants_snapshot
        if isinstance(identity, dict) and identity.get('id')
    }
    for participant in record.participants:
        if not isinstance(participant, dict) or not participant.get('id'):
            continue
        token = str(participant['id'])
        identity = identities.setdefault(token, {'id': token})
        for key in ('iid', 'name', 'profession_id', 'extraordinary_rating', 'is_ai'):
            if identity.get(key) in (None, '') and participant.get(key) is not None:
                identity[key] = participant[key]
    rows = []
    for values in raw_members:
        token = str(values['id'])
        identity = identities.get(token, {'id': token})
        for key in ('iid', 'name', 'profession_id'):
            if values.get(key) is not None:
                identity[key] = values[key]
        damage = values.get('damage')
        dps = damage / duration if damage is not None and duration else None
        bear = values.get('bear')
        heal = values.get('heal')
        rows.append({
            **identity,
            'damage': damage,
            'dps': dps,
            'bear': bear,
            'heal': heal,
            'damage_text': metric_text(damage),
            'dps_text': metric_text(dps),
            'bear_text': metric_text(bear),
            'heal_text': metric_text(heal),
            'status_text': status,
            'data_source': 'server_all',
        })
    return {
        'members': rows,
        'duration_seconds': duration,
        'duration_source': (
            f'server_all_{clock_policy}' if duration is not None else 'unavailable'
        ),
    }


def encounter_view(
    record: EncounterRecord,
    *,
    live_self: dict | None = None,
    statistics_scope: str = 'STAGE',
) -> dict:
    labels={'LIVE':'战后结算','PENDING':'等待结算','ABANDONED':'未取得结算',
            'SETTLED':'延迟补齐' if record.result=='WIPE' else '已结算'}
    status=labels[record.settlement_status]
    requested_scope = str(statistics_scope or 'STAGE').upper()
    if requested_scope not in {'STAGE', 'ALL'}:
        raise ValueError('Unsupported statistics scope')
    all_view = _all_statistics_view(record, status) if requested_scope == 'ALL' else None
    if all_view is not None:
        rows = all_view['members']
        duration_seconds = all_view['duration_seconds']
        duration_source = all_view['duration_source']
        resolved_scope = 'ALL'
    else:
        rows=[]
        duration_seconds = record.encounter_duration_seconds
        duration_source = record.duration_source
        resolved_scope = 'STAGE'
    current={row['id']:row for row in record.participants}
    if all_view is None:
        for identity in record.participants_snapshot:
            values=current.get(identity['id'],{})
            damage=dps=bear=heal=None
            source='unavailable'
            if record.settlement_status=='SETTLED':
                damage,dps,bear,heal=(values.get(k) for k in ('damage','dps','bear','heal'))
                source='server_stage'
            elif record.result=='IN_PROGRESS' and identity['id']==record.self_token and live_self:
                if live_self.get('local_encounter_id')==record.local_encounter_id and live_self.get('verified') is True:
                    damage,dps,bear,heal=(live_self.get(k) for k in ('damage','dps','bear','heal'))
                    source='verified_local_observation'
            rows.append({**deepcopy(identity),'damage':damage,'dps':dps,'bear':bear,'heal':heal,
                'damage_text':metric_text(damage),'dps_text':metric_text(dps),
                'bear_text':metric_text(bear),'heal_text':metric_text(heal),
                'status_text':status,'data_source':source})
    return {'local_encounter_id':record.local_encounter_id,'server_battle_id':record.server_battle_id,
            'result':record.result,'settlement_status':record.settlement_status,'status_text':status,
            'duration_seconds':duration_seconds,'duration_source':duration_source,
            'members':rows,'statistics_scope':resolved_scope,'revision':record.revision}


def skill_distribution(record: EncounterRecord, member_id: str) -> dict:
    member=next((m for m in record.participants if m['id']==member_id),None)
    if member is None or record.settlement_status!='SETTLED':
        return {'rows':[],'status':'等待结算','timeline_available':False}
    damage_map,count_map,heal_map=(member.get(k) for k in ('skill_damage','skill_count','skill_heal'))
    damage_map,count_map,heal_map=damage_map or {},count_map or {},heal_map or {}
    total=member.get('damage')
    rows=[{'skill_id':skill,'damage':damage_map.get(skill),'server_skill_count':count_map.get(skill),
           'heal':heal_map.get(skill),'share':damage_map[skill]/total if skill in damage_map and total else None,
           'crit_rate':None,'penetration_rate':None,'hit_timestamps':None}
          for skill in sorted(set(damage_map)|set(count_map)|set(heal_map))]
    return {'rows':rows,'total_damage':total,'unclassified_damage':member.get('unclassified_damage'),
            'skills_exceed_total':member.get('skills_exceed_total',False),
            'timeline_available':False,'count_semantics':'server_skill_count_not_verified_cast_or_hit_count'}
