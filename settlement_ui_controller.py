"""UI-thread coordinator for passive Boss records and delayed server snapshots.

Consumes detached parser observations. It never accesses the game or sends RPCs.
Old archives remain read-only; new journal state is separate.
"""
from copy import deepcopy
import math
import time
from combat_statistics import (
    fields,
    NormalizedCombatStatistics,
    normalize_statistics,
    pairs,
    SETTLEMENT_MESSAGE,
    STATISTICS_MESSAGES,
)
from encounter_tracker import (
    EncounterTracker,
    NEXT_CHALLENGE_BOUNDARY_WINDOW_NS,
)
from network_state import (
    BOSS_PHASE_TEMPLATE_TRANSITIONS,
    SCENE_TRANSITION_METHODS,
    VERIFIED_SIMULTANEOUS_BOSS_SUCCESSORS,
    VERIFIED_SIMULTANEOUS_BOSS_TEMPLATE_GROUPS,
    boss_template_continues_encounter,
)
from settlement_presenter import completed_statistics_scope, encounter_view


WINDOWS_EPOCH_FILETIME_100NS = 116_444_736_000_000_000
BOSS_HEALTH_SAMPLE_LIMIT = 2048
BOSS_DEATH_CONFIRM_TAIL_NS = 2_000_000_000


def parser_observation(parser, record, updates):
    """Freeze known identities before future roster/scene mutations."""
    method=record.get('method','')
    roster_replace=bool(
        method=='LivePartyRosterSnapshot'
        and any(
            kind=='party' and isinstance(value,dict)
            and value.get('roster_replace') is True
            for kind,value in updates
        )
    )
    if method in STATISTICS_MESSAGES and record.get('network_entity_id') != getattr(parser,'self_id',None):
        return None
    if method not in STATISTICS_MESSAGES | {'NpcapEntityCreated','NpcapBossConfirmed','NpcapBossHpActivity','LivePartyRosterSnapshot','OnMsgSyncFightMode',
            'OnMsgDungeonStageSettlement',
            'OnMsgEntityDead','OnMsgEntityRelive'} | SCENE_TRANSITION_METHODS:
        return None
    if method=='LivePartyRosterSnapshot' and not roster_replace:
        return None
    stamp=record.get('capture_timestamp_ns')
    if stamp is None:
        stamp=(int(record.get('filetime_100ns',0))-116444736000000000)*100
    if stamp<=0:return None
    tokens=set(getattr(parser,'party_tokens',set()))
    self_token=getattr(parser,'self_token',None)
    if self_token:tokens.add(self_token)
    roster=[]
    for token in sorted(tokens):
        entity=getattr(parser,'token_actors',{}).get(token)
        profile=getattr(parser,'entity_profiles',{}).get(entity,{})
        roster.append({'id':token,'iid':entity if entity and entity>0 else None,
                       'name':profile.get('name'), 'profession_id':profile.get('profession_id'),
                       'extraordinary_rating':profile.get('extraordinary_rating'),
                       'is_ai':profile.get('is_ai',False)})
    bosses={}
    training_dummies=set(getattr(parser,'training_dummy_entities',set()))
    for entity in (
        set(getattr(parser,'confirmed_boss_entities',set()))
        - training_dummies
    ):
        profile=getattr(parser,'entity_profiles',{}).get(entity,{})
        bosses[str(entity)]={'token':getattr(parser,'wire_entity_tokens',{}).get(entity),
            'template_id':getattr(parser,'entity_template_ids',{}).get(entity),'name':profile.get('name')}
    authoritative_tokens=set(getattr(parser,'authoritative_party_tokens',set()))
    exact_snapshot_roster=bool(
        authoritative_tokens
        and authoritative_tokens == tokens
        and self_token in authoritative_tokens
        and roster
        and all(row.get('iid') for row in roster)
    )
    party_session_id=int(getattr(parser,'party_session_id',0) or 0)
    return {'record':deepcopy(record),'timestamp_ns':stamp,'context':{
        'instance_id':getattr(parser,'wire_instance_id',None),
        'map_id':getattr(parser,'wire_map_id',None),'dungeon_id':getattr(parser,'dungeon_id',None),
        'stage_id':getattr(parser,'dungeon_stage_id',None) or None,
        'stage_index':getattr(parser,'dungeon_stage_phase',None) or None,
        'self_token':self_token,'roster':roster,
        'party_session_id':party_session_id,
        'in_team':bool(getattr(parser,'team_group_active',False)),
        'roster_replace':roster_replace,
        'roster_complete':bool(
            roster_replace or
            getattr(parser,'explicit_party_roster_seen',False)
            or exact_snapshot_roster
        ),
        'bosses':bosses},
        'updates':deepcopy([(k,v) for k,v in updates if k in ('life','monster','combat_state')])}


class SettlementUIController:
    def _tracker_state_signature(self):
        """Detect durable changes without copying every archived encounter."""

        return (
            self.tracker.current_id,
            tuple(
                (
                    key, encounter.revision, encounter.result,
                    encounter.settlement_status, encounter.ended_at_ns,
                    encounter.capture_complete,
                    encounter.automatic_match_blocked,
                    encounter.boss_template_id, encounter.boss_token,
                    encounter.boss_name, encounter.boss_current_hp,
                    encounter.boss_max_hp, encounter.party_session_id,
                    len(encounter.participants_snapshot),
                    len(encounter.participants),
                    tuple(encounter.snapshot_fingerprints.items()),
                    tuple(
                        (token, len(events), events[-1] if events else None)
                        for token, events in encounter.life_events.items()
                    ),
                )
                for key, encounter in self.tracker.encounters.items()
            ),
            tuple(
                (key, value.get('reason'))
                for key, value in self.tracker.orphans.items()
            ),
        )

    def __init__(self, repository):
        self.repository=repository
        self.tracker=repository.load()
        # The journal is durable history, but the compact main window is a
        # view of this application/identity epoch only.  Old encounters stay
        # available in Settings and are never selected automatically after a
        # restart.
        self.session_encounter_ids=set()
        # A live span interrupted by app shutdown has no observed end boundary.
        # Keep that fact explicit and release current_id so a new pull can start.
        if self.tracker.current_id:
            interrupted=self.tracker.encounters[self.tracker.current_id]
            interrupted.capture_complete=False
            if interrupted.result=='IN_PROGRESS':
                interrupted.result='RESET'
                interrupted.settlement_status='ABANDONED'
                for member in interrupted.participants:
                    member['settlement_hint']='未取得结算'
                interrupted.revision+=1
            self.tracker.current_id=None
            self.repository.save(self.tracker)
        # Statistics can arrive before the local Boss end edge and are kept as
        # orphans. Re-evaluate them on every launch so a matcher improvement or
        # a cleanly persisted end boundary restores the last result immediately
        # instead of requiring the user to fight once more.
        before_retry=self.tracker.to_dict()
        for encounter in list(self.tracker.encounters.values()):
            if encounter.settlement_status == 'PENDING' and encounter.stage_statistics:
                self.tracker.accept(
                    NormalizedCombatStatistics.from_dict(encounter.stage_statistics)
                )
        self.tracker.retry_orphans()
        if before_retry!=self.tracker.to_dict():
            self.repository.save(self.tracker)
        self.instance_id=None
        self.last_visible_id=None
        self.boss_entities={}
        self.boss_in_combat={}
        self.boss_out_at={}
        self.boss_health_samples={}
        self.boss_max_health_samples={}
        self.pending_boss_starts={}
        self.last_statistics_boundary=None
        self.last_error=None
        self.generation=0

    def _latest_visible_history_id(self):
        candidates=[
            encounter for encounter in self.tracker.encounters.values()
            if encounter.local_encounter_id in self.session_encounter_ids
            and encounter.ended_at_ns is not None
            and encounter.settlement_status!='ABANDONED'
            and not encounter.history_deleted
        ]
        if not candidates:return None
        return max(candidates,key=lambda encounter:(
            int(encounter.ended_at_ns or 0),int(encounter.started_at_ns)
        )).local_encounter_id

    def mark_gap(self):
        if self.tracker.current_id:
            current=self.tracker.encounters[self.tracker.current_id]
            current.capture_complete=False
            current.automatic_match_blocked=True
            self.repository.save(self.tracker)

    @staticmethod
    def _health_update_timestamp_ns(update):
        try:
            stamp=int(update.get('capture_timestamp_ns',0) or 0)
        except (TypeError,ValueError,OverflowError):
            stamp=0
        if stamp>0:
            return stamp
        try:
            filetime=int(update.get('filetime_100ns',0) or 0)
        except (TypeError,ValueError,OverflowError):
            return 0
        if filetime<=WINDOWS_EPOCH_FILETIME_100NS:
            return 0
        return (filetime-WINDOWS_EPOCH_FILETIME_100NS)*100

    @staticmethod
    def _health_value(value, *, maximum=False):
        try:
            parsed=float(value)
        except (TypeError,ValueError,OverflowError):
            return None
        if not math.isfinite(parsed) or parsed<0 or (maximum and parsed<=0):
            return None
        return parsed

    @staticmethod
    def _health_sample_matches(encounter, entity, sample):
        encounter_token=str(encounter.boss_token or '')
        sample_token=str(sample.get('boss_token') or '')
        if encounter_token and sample_token and encounter_token==sample_token:
            return True
        if encounter_token==f'entity:{entity}':
            return True
        try:
            encounter_template=int(encounter.boss_template_id or 0)
            sample_template=int(sample.get('template_id',0) or 0)
        except (TypeError,ValueError,OverflowError):
            return False
        return bool(encounter_template and encounter_template==sample_template)

    def _freeze_encounter_boss_health(self, encounter, ended_at_ns):
        """Freeze the last validated Boss HP sample at the encounter boundary."""
        if encounter is None or encounter.boss_health_source=='server_statistics':
            return False
        try:
            boundary=int(ended_at_ns or 0)
        except (TypeError,ValueError,OverflowError):
            return False
        if boundary<encounter.started_at_ns:
            return False

        candidates=[]
        for entity,samples in self.boss_health_samples.items():
            for sample in samples:
                stamp=int(sample.get('timestamp_ns',0) or 0)
                confirmed_death_tail=bool(
                    sample.get('death_confirmed') is True
                    and sample.get('current_hp')==0
                    and boundary<=stamp
                    and stamp-boundary<=BOSS_DEATH_CONFIRM_TAIL_NS
                )
                if (
                    encounter.started_at_ns<=stamp
                    and (stamp<=boundary or confirmed_death_tail)
                    and self._health_sample_matches(encounter,entity,sample)
                ):
                    candidates.append((stamp,entity,sample))
        if not candidates:
            return False
        observed_at,entity,current_sample=max(candidates,key=lambda item:item[0])
        current_hp=current_sample.get('current_hp')

        maximum_candidates=[]
        for sample in self.boss_max_health_samples.get(entity,()):
            stamp=int(sample.get('timestamp_ns',0) or 0)
            if stamp<=boundary and self._health_sample_matches(
                encounter,entity,sample
            ):
                maximum_candidates.append((stamp,sample.get('max_hp')))
        maximum_hp=(
            max(maximum_candidates,key=lambda item:item[0])[1]
            if maximum_candidates
            else current_sample.get('max_hp')
        )
        if maximum_hp is not None and current_hp is not None:
            try:
                if float(current_hp)>float(maximum_hp)*1.02:
                    maximum_hp=None
            except (TypeError,ValueError,OverflowError):
                maximum_hp=None

        next_values=(current_hp,maximum_hp,observed_at,'passive_encounter_end_snapshot')
        previous_values=(
            encounter.boss_current_hp,
            encounter.boss_max_hp,
            encounter.boss_health_observed_at_ns,
            encounter.boss_health_source,
        )
        if next_values==previous_values:
            return False
        already_ended=encounter.ended_at_ns is not None
        (
            encounter.boss_current_hp,
            encounter.boss_max_hp,
            encounter.boss_health_observed_at_ns,
            encounter.boss_health_source,
        )=next_values
        if already_ended:
            encounter.revision+=1
        return True

    def observe_boss_health(self, update):
        """Receive validated passive HP without persisting every live sample."""
        if not isinstance(update,dict):
            return False
        try:
            entity=str(int(update.get('entity_id',0) or 0))
        except (TypeError,ValueError,OverflowError):
            return False
        if entity=='0':
            return False
        stamp=self._health_update_timestamp_ns(update)
        if stamp<=0:
            return False
        current_hp=(
            self._health_value(update.get('current_hp'))
            if 'current_hp' in update else None
        )
        maximum_hp=(
            self._health_value(update.get('max_hp'),maximum=True)
            if 'max_hp' in update else None
        )
        if current_hp is None and maximum_hp is None:
            return False
        boss=self.boss_entities.get(entity,{})
        identity={
            'timestamp_ns':stamp,
            'boss_token':boss.get('token'),
            'template_id':boss.get('template_id'),
        }
        if current_hp is not None:
            samples=self.boss_health_samples.setdefault(entity,[])
            samples.append(dict(
                identity,current_hp=current_hp,max_hp=maximum_hp,
                death_confirmed=update.get('death_confirmed') is True,
            ))
            if len(samples)>BOSS_HEALTH_SAMPLE_LIMIT:
                del samples[:-BOSS_HEALTH_SAMPLE_LIMIT]
        if maximum_hp is not None:
            maxima=self.boss_max_health_samples.setdefault(entity,[])
            maxima.append(dict(identity,max_hp=maximum_hp))
            if len(maxima)>64:
                del maxima[:-64]

        changed=False
        for encounter in self.tracker.encounters.values():
            death_tail=bool(
                update.get('death_confirmed') is True
                and current_hp==0
                and encounter.ended_at_ns is not None
                and encounter.ended_at_ns<=stamp
                and stamp-encounter.ended_at_ns<=BOSS_DEATH_CONFIRM_TAIL_NS
                and self._health_sample_matches(encounter,entity,identity)
            )
            if (
                encounter.ended_at_ns is not None
                and encounter.started_at_ns<=stamp
                and (stamp<=encounter.ended_at_ns or death_tail)
            ):
                changed |= self._freeze_encounter_boss_health(
                    encounter,encounter.ended_at_ns
                )
        if changed:
            self.repository.save(self.tracker)
            self.generation+=1
        return changed

    def reset_capture_session(self, timestamp_ns=None):
        """Abandon only the interrupted live pull and clear session identity.

        A replacement capture stream may be another character in the same
        game process.  Completed encounters remain in the journal, while all
        unproven scene/Boss/start state must be learned again from live data.
        """
        before=self.tracker.to_dict()
        current=self.tracker.encounters.get(self.tracker.current_id)
        if current is not None:
            current.capture_complete=False
            stamp=int(timestamp_ns or time.time_ns())
            stamp=max(current.started_at_ns,stamp)
            self.tracker.end('RESET',stamp)
        self.instance_id=None
        self.session_encounter_ids.clear()
        self.last_visible_id=None
        self.boss_entities.clear()
        self.boss_in_combat.clear()
        self.boss_out_at.clear()
        self.boss_health_samples.clear()
        self.boss_max_health_samples.clear()
        self.pending_boss_starts.clear()
        self.last_statistics_boundary=None
        changed=before!=self.tracker.to_dict()
        if changed:
            self.repository.save(self.tracker)
            self.generation+=1
        return changed

    def _refresh_incomplete_encounter_roster(self, context, stamp):
        """Upgrade one same-session Encounter from later authoritative roster data.

        Boss/fight-mode can be observed before the complete party table.  The
        stable token snapshot seen later in the same instance is stronger than
        those early placeholders and is required to match the server's final
        STAGE roster.  Never apply a roster from another character epoch and
        never remove a member that was already attached to the pull.
        """
        # A periodic live-party snapshot is an explicit replacement boundary.
        # It is allowed to remove members that were present at pull start; the
        # old implementation treated every snapshot as an additive upgrade
        # and therefore retained departed players forever once the encounter
        # had been marked complete.
        roster_replace = bool(context.get('roster_replace'))
        if context.get('roster_complete') is not True and not roster_replace:
            return False
        self_token=str(context.get('self_token') or '')
        try:
            party_session_id=max(0,int(context.get('party_session_id',0) or 0))
        except (TypeError,ValueError,OverflowError):
            party_session_id=0
        raw_roster=context.get('roster')
        if not self_token or not isinstance(raw_roster,list):
            return False
        roster=[]
        seen=set()
        for raw in raw_roster:
            if not isinstance(raw,dict):
                return False
            token=str(raw.get('id') or '')
            if not token or token in seen:
                return False
            seen.add(token)
            roster.append(deepcopy(raw))
        if self_token not in seen:
            return False

        candidate=self.tracker.encounters.get(self.tracker.current_id)
        if roster_replace and candidate is not None:
            # Replacement heartbeats must stay inside the same character,
            # party and instance epoch.  Never let a delayed old heartbeat
            # rewrite a newly started pull.
            context_instance = context.get('instance_id') or self.instance_id
            candidate_party_session_id = int(
                getattr(candidate, 'party_session_id', 0) or 0
            )
            if (
                candidate.result != 'IN_PROGRESS'
                or candidate.self_token != self_token
                or stamp < candidate.started_at_ns
                or (
                    context_instance
                    and candidate.instance_id
                    and context_instance != candidate.instance_id
                )
                or (
                    party_session_id
                    and candidate_party_session_id
                    and party_session_id != candidate_party_session_id
                )
            ):
                candidate = None
        if candidate is None:
            # A replacement snapshot describes the roster for the scene now
            # being entered. Once the previous pull has ended, its participant
            # snapshot is immutable even if the next pull has fewer, more, or
            # entirely different teammates.
            if roster_replace:
                return False
            instance=context.get('instance_id') or self.instance_id
            pending=[
                encounter for encounter in self.tracker.encounters.values()
                if encounter.local_encounter_id in self.session_encounter_ids
                and encounter.capture_complete is False
                and encounter.result in ('VICTORY','WIPE')
                and encounter.ended_at_ns is not None
                and encounter.ended_at_ns<=stamp
                and encounter.self_token==self_token
                and (
                    not party_session_id
                    or not int(getattr(encounter,'party_session_id',0) or 0)
                    or int(getattr(encounter,'party_session_id',0) or 0)
                    ==party_session_id
                )
                and (
                    not instance
                    or not encounter.instance_id
                    or encounter.instance_id==instance
                )
            ]
            candidate=max(
                pending,
                key=lambda encounter:(
                    int(encounter.ended_at_ns or 0),
                    int(encounter.started_at_ns),
                ),
                default=None,
            )
        if (
            candidate is None
            or candidate.local_encounter_id not in self.session_encounter_ids
            or candidate.settlement_status == 'SETTLED'
            or (candidate.capture_complete is True and not roster_replace)
            or candidate.self_token!=self_token
            or stamp<candidate.started_at_ns
        ):
            return False
        candidate_party_session_id=int(
            getattr(candidate,'party_session_id',0) or 0
        )
        if (
            party_session_id
            and candidate_party_session_id
            and candidate_party_session_id!=party_session_id
        ):
            return False
        context_instance=context.get('instance_id') or self.instance_id
        if (
            context_instance
            and candidate.instance_id
            and context_instance!=candidate.instance_id
        ):
            return False
        old_tokens={
            str(row.get('id') or '')
            for row in candidate.participants_snapshot
            if isinstance(row,dict) and row.get('id')
        }
        if not old_tokens:
            return False
        # Normal late identity completion may only add server-proven members.
        # An explicit replacement heartbeat is the sole path that may shrink
        # the roster, and it must be marked by the parser/UI boundary above.
        if not roster_replace and not old_tokens.issubset(seen):
            return False

        old_participants={
            str(row.get('id') or ''):row
            for row in candidate.participants
            if isinstance(row,dict) and row.get('id')
        }
        next_participants=[]
        for identity in roster:
            token=str(identity['id'])
            existing=old_participants.get(token)
            if existing is None:
                existing={
                    'damage':None,
                    'dps':None,
                    'settlement_hint':'等待结算',
                }
            merged=dict(existing)
            merged.update(identity)
            next_participants.append(merged)
        changed = (
            candidate.participants_snapshot != roster
            or candidate.participants != next_participants
        )
        candidate.participants_snapshot=roster
        candidate.participants=next_participants
        if roster_replace:
            # Life events are keyed by character token.  Drop events for a
            # departed member together with the visible row so death counts
            # and later projections cannot resurrect that member.
            candidate.life_events = {
                token: events
                for token, events in candidate.life_events.items()
                if token in seen
            }
        if party_session_id and not candidate_party_session_id:
            candidate.party_session_id=party_session_id
            changed = True
        if not candidate.capture_complete:
            changed = True
        candidate.capture_complete=True
        if not changed:
            return False
        candidate.revision+=1
        self.tracker.retry_orphans()
        return True

    @staticmethod
    def _continuation_replaces_primary_boss(current, boss):
        try:
            previous = int(current.boss_template_id or 0)
            incoming = int(boss.get('template_id') or 0)
        except (TypeError, ValueError, OverflowError):
            return False
        if (previous, incoming) in BOSS_PHASE_TEMPLATE_TRANSITIONS:
            return True
        return any(
            previous in group
            and incoming in VERIFIED_SIMULTANEOUS_BOSS_SUCCESSORS.get(group, ())
            for group in VERIFIED_SIMULTANEOUS_BOSS_TEMPLATE_GROUPS
        )

    def _different_boss_end_stamp(self, current, stamp):
        ended=[]
        for entity, identity in self.boss_entities.items():
            if identity.get('token') != current.boss_token:
                continue
            observed=self.boss_out_at.get(str(entity))
            if observed is None:
                continue
            observed=int(observed)
            if current.started_at_ns<=observed<=stamp:
                ended.append(observed)
        return max(ended) if ended else stamp

    @staticmethod
    def _boss_death_completes_encounter(current, boss):
        try:
            current_template=int(current.boss_template_id or 0)
            dead_template=int(boss.get('template_id') or 0)
        except (TypeError,ValueError,OverflowError):
            return True
        return not any(
            current_template in group and dead_template in group
            for group in VERIFIED_SIMULTANEOUS_BOSS_TEMPLATE_GROUPS
        )

    def _begin_observed_encounter(
        self, entity, boss, stamp, context, *, challenge_start=False
    ):
        context=self._encounter_start_context(context,stamp)
        current=self.tracker.encounters.get(self.tracker.current_id)
        instance=context.get('instance_id')
        roster=context.get('roster')
        self_token=context.get('self_token')
        if current and current.boss_token!=boss.get('token'):
            previous_template=current.boss_template_id
            incoming_template=boss.get('template_id')
            continuation = bool(
                previous_template
                and incoming_template
                and previous_template!=incoming_template
                and boss_template_continues_encounter(
                    previous_template,incoming_template
                )
            )
            if continuation:
                if self._continuation_replaces_primary_boss(current,boss):
                    changed=bool(
                        current.boss_template_id!=incoming_template
                        or current.boss_token!=boss.get('token')
                        or current.boss_name!=boss.get('name')
                    )
                    if (
                        current.boss_token
                        and current.boss_token != boss.get('token')
                        and current.boss_token not in current.boss_phase_tokens
                    ):
                        current.boss_phase_tokens.append(current.boss_token)
                    current.boss_template_id=incoming_template
                    current.boss_token=boss.get('token')
                    current.boss_name=boss.get('name') or current.boss_name
                    if changed:
                        current.revision+=1
            elif challenge_start:
                # Statistics can precede this [2] in the same inbound batch.
                # End/retry the old Boss before begin() exposes the new pull.
                # Do not assume that every challenge boundary is a victory:
                # delayed wipe tables use the same boundary and the matcher
                # needs the old encounter to remain ``WIPE``.
                end_stamp=self._different_boss_end_stamp(current,stamp)
                self._end_observed_encounter(
                    self._infer_boundary_result(current, stamp),
                    end_stamp,
                )
                current=None
            else:
                # HP activity alone cannot prove a new Encounter rather than
                # an unverified multi-target promotion.
                current.capture_complete=False
                current.automatic_match_blocked=True
        if current is None and instance and roster and self_token:
            try:
                party_session_id=max(
                    0,int(context.get('party_session_id',0) or 0)
                )
            except (TypeError,ValueError,OverflowError):
                party_session_id=0
            current=self.tracker.begin(instance_id=instance,started_at_ns=stamp,
                participants_snapshot=roster,dungeon_id=context.get('dungeon_id'),
                map_id=context.get('map_id'),stage_id=context.get('stage_id'),
                stage_index=context.get('stage_index'),boss_template_id=boss.get('template_id'),
                boss_token=boss.get('token'),self_token=self_token,
                party_session_id=party_session_id)
            current.boss_name=boss.get('name')
            current.capture_complete=context.get('roster_complete') is True
            self.session_encounter_ids.add(current.local_encounter_id)
            self.last_visible_id=current.local_encounter_id
        elif current is None and challenge_start:
            self._remember_unknown_boss_start(
                str(entity),stamp,context,challenge_start=True
            )
        if current is not None:
            # A late Boss confirmation can replay a start only after the local
            # character/roster becomes known.  Do not overwrite a newer
            # out-of-combat edge that was already observed while identity was
            # unavailable; the next [2] edge needs it to close the old pull.
            observed_out=self.boss_out_at.get(str(entity))
            if observed_out is None or int(observed_out)<int(stamp):
                self.boss_in_combat[str(entity)]=True
        return current

    def _infer_boundary_result(self, current, boundary_stamp):
        """Classify the pull that ends immediately before a new challenge.

        The server can deliver the previous STAGE table before the next
        ``OnMsgSyncFightMode [2]`` edge.  Its success flag is the strongest
        evidence.  When no table is available, use independently observed
        Boss-death or full-party-death evidence; otherwise keep the
        conservative wipe classification so a delayed wipe table can still be
        matched instead of being discarded as a victory.
        """
        boundary = self.last_statistics_boundary
        try:
            stamp = int(boundary_stamp or 0)
        except (TypeError, ValueError, OverflowError):
            stamp = 0
        if isinstance(boundary, dict):
            try:
                boundary_stamp_ns = int(boundary.get("timestamp_ns", 0) or 0)
            except (TypeError, ValueError, OverflowError):
                boundary_stamp_ns = 0
            same_instance = not boundary.get("instance_id") or not current.instance_id or boundary.get("instance_id") == current.instance_id
            if (
                same_instance
                and boundary_stamp_ns
                and 0 <= stamp - boundary_stamp_ns <= NEXT_CHALLENGE_BOUNDARY_WINDOW_NS
            ):
                success = boundary.get("is_stage_success")
                if success is True:
                    return "VICTORY"
                if success is False:
                    return "WIPE"
                # Captured stage tables omit the success field for failed
                # pulls.  The table itself proves this was a real encounter,
                # including a multi-phase Boss restarting from its first form.
                if boundary.get("summary_id"):
                    return "WIPE"

        # A complete set of death edges is sufficient to classify a wipe even
        # when the statistics packet did not carry an explicit success flag.
        roster = list(current.participants_snapshot or ())
        if roster and all(
            current.life_events.get(str(row.get("id")))
            and current.life_events[str(row.get("id"))][-1][1]
            for row in roster
            if isinstance(row, dict) and row.get("id")
        ):
            return "WIPE"

        # Conversely, a validated zero-HP/death-tail sample proves a victory.
        token = str(current.boss_token or "")
        for entity, samples in self.boss_health_samples.items():
            for sample in reversed(samples):
                if int(sample.get("timestamp_ns", 0) or 0) > stamp:
                    continue
                if not self._health_sample_matches(current, entity, sample):
                    continue
                try:
                    hp = float(sample.get("current_hp"))
                except (TypeError, ValueError, OverflowError):
                    hp = None
                if sample.get("death_confirmed") is True and hp == 0:
                    return "VICTORY"
                if token and sample.get("boss_token") == token and hp == 0:
                    return "VICTORY"

        # A different Boss starting does not prove that a *fought* pull was
        # defeated. Keep a real, observed pull pending as a wipe when the end
        # edge was missed; an explicit success table or a confirmed Boss death
        # above can still establish a victory. A pull with no damage/death
        # evidence is only a capture/start split, so retain the historical
        # victory classification to avoid creating a phantom result.
        has_observed_activity = bool(
            current.life_events
            or any(
                isinstance(row, dict)
                and row.get("damage") is not None
                for row in current.participants
            )
        )
        return "WIPE" if has_observed_activity else "VICTORY"

    def _encounter_start_context(self, context, stamp):
        """Do not stamp the previous STAGE onto the next Challenge.

        Observed server ordering is Statistics(N) followed immediately by
        fight-mode [2] for Challenge(N+1).  The parser quite correctly knows
        only stage N at that instant, but that value is settlement context, not
        the identity of the encounter being created.  Leave the new identity
        unknown until its own authoritative STAGE arrives.
        """

        boundary=self.last_statistics_boundary
        if not isinstance(boundary,dict):
            return context
        try:
            delta=int(stamp)-int(boundary.get('timestamp_ns',0) or 0)
            stage_id=int(context.get('stage_id',0) or 0)
            boundary_stage=int(boundary.get('stage_id',0) or 0)
        except (TypeError,ValueError,OverflowError):
            return context
        if (
            not 0<=delta<=NEXT_CHALLENGE_BOUNDARY_WINDOW_NS
            or not stage_id
            or stage_id!=boundary_stage
            or context.get('instance_id')!=boundary.get('instance_id')
        ):
            return context
        adjusted=deepcopy(context)
        adjusted['stage_id']=None
        adjusted['stage_index']=None
        return adjusted

    def _remember_unknown_boss_start(
        self, entity, stamp, context, *, challenge_start=False
    ):
        if not entity or entity == '0':
            return
        previous=self.pending_boss_starts.get(entity)
        if previous is None or stamp < previous['timestamp_ns']:
            self.pending_boss_starts[entity]={
                'timestamp_ns':stamp,
                'context':deepcopy(context),
                'challenge_start':bool(challenge_start),
            }

    def _resolve_pending_boss_start(self, entity, boss, context, now_stamp):
        pending=self.pending_boss_starts.get(entity)
        if pending is None:
            return None
        start_stamp=int(pending['timestamp_ns'])
        # Entity IDs can be reused after a scene change.  A promotion arriving
        # minutes later is not proof that an old unknown fight signal belongs
        # to this Boss.
        if now_stamp < start_stamp or now_stamp-start_stamp > 120_000_000_000:
            self.pending_boss_starts.pop(entity,None)
            return None
        original=pending.get('context',{})
        start_context=dict(context)
        for key in ('instance_id','map_id','dungeon_id','stage_id','stage_index'):
            if not start_context.get(key) and original.get(key):
                start_context[key]=original[key]
        if original.get('roster') and original.get('self_token'):
            start_context['roster']=deepcopy(original['roster'])
            start_context['self_token']=original['self_token']
            original_ids={
                (row.get('id'),row.get('iid'))
                for row in original['roster'] if isinstance(row,dict)
            }
            confirmed_ids={
                (row.get('id'),row.get('iid'))
                for row in context.get('roster',()) if isinstance(row,dict)
            }
            start_context['roster_complete']=bool(
                original.get('roster_complete') is True
                or (
                    context.get('roster_complete') is True
                    and original_ids
                    and original_ids==confirmed_ids
                )
            )
        resolved=self._begin_observed_encounter(
            entity,boss,start_stamp,start_context,
            challenge_start=bool(pending.get('challenge_start')),
        )
        # Boss metadata can arrive before the local character or the complete
        # current roster.  Keep the original combat edge until a later passive
        # observation supplies enough identity to create the encounter.
        if resolved is not None:
            self.pending_boss_starts.pop(entity,None)
        return resolved

    def _resolve_ready_pending_boss_start(self, context, stamp):
        """Replay one known pending Boss as soon as roster identity is ready."""
        if self.tracker.current_id or not context.get('instance_id'):
            return None
        if not context.get('roster') or not context.get('self_token'):
            return None
        for pending_entity in list(self.pending_boss_starts):
            pending_boss=self.boss_entities.get(pending_entity)
            if pending_boss is None:
                continue
            resolved=self._resolve_pending_boss_start(
                pending_entity,pending_boss,context,stamp
            )
            if resolved is not None:
                return resolved
        return None

    def _end_observed_encounter(self, result, stamp):
        current=self.tracker.encounters.get(self.tracker.current_id)
        if current is None:return None
        seconds=(stamp-current.started_at_ns)/1_000_000_000
        if 0 < seconds <= 86400:
            current.duration_evidence={
                'source':'verified_shared_encounter_clock',
                'seconds':seconds,
                'verified':True,
                'local_encounter_id':current.local_encounter_id,
            }
            current.encounter_duration_seconds=seconds
            current.duration_source='verified_shared_encounter_clock'
        self._freeze_encounter_boss_health(current,stamp)
        if result=='VICTORY':
            self._confirm_successful_boss_health(current,stamp)
        return self.tracker.end(result,stamp)

    def _restart_of_current_boss(self, current, entity, boss, stamp):
        """Return the old Boss's observed end edge for a respawned pull."""
        del entity
        if current is None or current.boss_token==boss.get('token'):
            return None
        try:
            current_template=int(current.boss_template_id or 0)
            next_template=int(boss.get('template_id') or 0)
        except (TypeError,ValueError,OverflowError):
            return None
        if not current_template or current_template!=next_template:
            return None
        old_entities=[
            key for key,value in self.boss_entities.items()
            if value.get('token')==current.boss_token
        ]
        ended=[
            int(self.boss_out_at[key]) for key in old_entities
            if self.boss_in_combat.get(key) is False
            and key in self.boss_out_at
            and current.started_at_ns<=int(self.boss_out_at[key])<=stamp
        ]
        return max(ended) if ended else None

    @staticmethod
    def _stage_settlement_identity(args):
        """Return the authoritative stage and Boss references from a win RPC."""
        if not isinstance(args,list) or len(args)<2 or args[1] is not True:
            return None,set()
        try:
            stage_id=int(args[0] or 0)
        except (TypeError,ValueError,OverflowError):
            return None,set()
        if stage_id<=0:
            return None,set()
        references=set()
        if len(args)>=5:
            try:
                member=fields(args[4])
                for key in (36,37):
                    if member.get(key) is not None:
                        references.update(str(token) for token,_amount in pairs(member[key]))
            except (ValueError,KeyError,TypeError):
                references.clear()
        return stage_id,references

    def _apply_stage_victory_boundary(self, args, stamp, instance):
        """End a win immediately and repair the one-stage-late spawn context."""
        stage_id,references=self._stage_settlement_identity(args)
        if stage_id is None:
            return None
        authoritative_token=(next(iter(references)) if len(references)==1 else None)
        current=self.tracker.encounters.get(self.tracker.current_id)
        target=None
        if (
            current is not None
            and current.instance_id==instance
            and (
                current.stage_id==stage_id
                or (
                    current.boss_token
                    and current.boss_token in references
                )
                or (
                    authoritative_token
                    and str(current.boss_token or '').startswith('entity:')
                )
            )
        ):
            target=current
        elif references:
            # Boss death normally closes the encounter a few seconds before
            # this reward/settlement RPC. Use its exact Boss token to correct
            # the stage ID before the full team table follows.
            candidates=[
                encounter for encounter in self.tracker.encounters.values()
                if encounter.instance_id==instance
                and encounter.result=='VICTORY'
                and encounter.ended_at_ns is not None
                and encounter.ended_at_ns<=stamp
                and encounter.boss_token in references
                and encounter.settlement_status!='ABANDONED'
            ]
            if candidates:
                target=max(candidates,key=lambda encounter:int(encounter.ended_at_ns or 0))
        if target is None and authoritative_token and instance:
            # Boss death and the explicit victory RPC are adjacent on the
            # server stream.  A mid-instance capture may know only ``entity:N``
            # until this RPC exposes the stable Boss token.  Require one unique
            # pending victory inside a tight boundary; never choose by general
            # arrival order or cross an instance/capture-gap boundary.
            candidates=[
                encounter for encounter in self.tracker.encounters.values()
                if encounter.instance_id==instance
                and encounter.result=='VICTORY'
                and encounter.ended_at_ns is not None
                and 0<=stamp-int(encounter.ended_at_ns)<=2_000_000_000
                and str(encounter.boss_token or '').startswith('entity:')
                and encounter.settlement_status!='ABANDONED'
                and not encounter.automatic_match_blocked
            ]
            if len(candidates)==1:
                target=candidates[0]
        if target is None:
            return None
        if (
            authoritative_token
            and str(target.boss_token or '').startswith('entity:')
            and target.boss_token!=authoritative_token
        ):
            target.boss_token=authoritative_token
            target.revision+=1
        if target.stage_id!=stage_id:
            target.stage_id=stage_id
            target.revision+=1
        if target.local_encounter_id==self.tracker.current_id:
            self._end_observed_encounter('VICTORY',stamp)
        self.tracker.retry_orphans()
        self.last_visible_id=target.local_encounter_id
        return target

    @staticmethod
    def _statistics_is_for_current_party(snapshot, context):
        """Keep the direct server-result path scoped to the local character.

        Runtime entity IDs, stage bindings and battle IDs can legitimately move
        at revive/finalize boundaries. The local character token is stable and
        is also present for a valid solo-dungeon result.
        """

        self_token=str(context.get('self_token') or '').strip()
        roster_tokens={
            str(row.get('id') or '').strip()
            for row in context.get('roster',())
            if isinstance(row,dict) and row.get('id')
        }
        stat_tokens={member.id for member in snapshot.members}
        return bool(
            self_token
            and self_token in roster_tokens
            and self_token in stat_tokens
            and snapshot.members
        )

    def _direct_server_statistics_target(
        self, snapshot, context, *, allow_without_live=False
    ):
        if (
            snapshot.scope!='STAGE'
            or not self._statistics_is_for_current_party(snapshot,context)
        ):
            return None
        if not allow_without_live and self.tracker.current_id is None:
            # UpdateStage statistics can precede the next [2] edge in the same
            # server batch.  Keep it as an orphan for begin() to bind against
            # that exact boundary instead of consuming it too early.
            return None
        self_token=str(context.get('self_token') or '').strip()
        roster_tokens={
            str(row.get('id') or '').strip()
            for row in context.get('roster',())
            if isinstance(row,dict) and row.get('id')
        }
        party_active=bool(context.get('in_team')) or len(roster_tokens)>1
        stat_tokens={member.id for member in snapshot.members}
        references={
            token for member in snapshot.members
            for mapping in (member.bear_map,member.killer_map)
            if mapping for token in mapping
        }
        try:
            party_session_id=int(context.get('party_session_id',0) or 0)
        except (TypeError,ValueError,OverflowError):
            party_session_id=0
        roster_shrunk_after_team_leave = bool(
            party_active
            and party_session_id
            and self_token in roster_tokens
            and roster_tokens == {self_token}
        )
        known_boss_references={
            encounter.boss_token for encounter in self.tracker.encounters.values()
            if encounter.instance_id==snapshot.instance_id
            and encounter.boss_token in references
        }
        candidates=[
            encounter for encounter in self.tracker.encounters.values()
            if encounter.local_encounter_id in self.session_encounter_ids
            and encounter.local_encounter_id!=self.tracker.current_id
            and encounter.instance_id==snapshot.instance_id
            and encounter.self_token==self_token
            and encounter.result in ('WIPE','VICTORY')
            and (snapshot.is_stage_success is None or
                 (encounter.result=='VICTORY')==snapshot.is_stage_success)
            and (not party_session_id or not encounter.party_session_id or
                 encounter.party_session_id==party_session_id)
            and (
                set(encounter.roster) <= stat_tokens
                or (
                    roster_shrunk_after_team_leave
                    and encounter.party_session_id == party_session_id
                    and stat_tokens == {self_token}
                    and self_token in encounter.roster
                )
            )
            and not (known_boss_references and
                     encounter.boss_token not in known_boss_references)
            and encounter.ended_at_ns is not None
            and encounter.ended_at_ns<=snapshot.received_at_ns
            and encounter.settlement_status=='PENDING'
            and not encounter.history_deleted
        ]
        if not candidates:
            return None
        bound=[encounter for encounter in candidates
               if snapshot.battle_id and encounter.server_battle_id==snapshot.battle_id]
        if bound:
            return bound[0] if len(bound)==1 else None
        if snapshot.battle_id and any(
            encounter.server_battle_id==snapshot.battle_id
            for encounter in self.tracker.encounters.values()
        ):
            return None
        candidates=[encounter for encounter in candidates
                    if not encounter.server_battle_id]
        if len(candidates)!=1:
            # Two wipes of the same Boss may have the same stage and roster.
            # Leave this packet orphaned until the following Challenge gives
            # the matcher an independent, exact next-pull boundary.
            return None
        candidate=candidates[0]
        if ((candidate.stage_id is not None and snapshot.stage_id is not None
             and candidate.stage_id!=snapshot.stage_id)
            or (candidate.stage_index is not None and snapshot.stage_index is not None
                and candidate.stage_index!=snapshot.stage_index)) and candidate.boss_token not in references:
            return None
        return candidate

    def _apply_final_statistics_victory_boundary(self, snapshot, stamp, context):
        """Close the live Boss when the authoritative final table is the win edge.

        Some instances omit ``OnMsgDungeonStageSettlement`` entirely.  Their
        first explicit victory boundary is the current STAGE carried by
        ``OnMsgSettlementCombatStatistics``.  Validate the proposed ended
        encounter from this authoritative server result, then let the normal
        accept path bind the battle ID and data.
        """
        if (
            snapshot.source != SETTLEMENT_MESSAGE
            or snapshot.scope != 'STAGE'
            or snapshot.is_stage_success is not True
            or not self._statistics_is_for_current_party(snapshot,context)
        ):
            return None
        target=self.tracker.encounters.get(self.tracker.current_id)
        if target is not None:
            if (
                target.result!='IN_PROGRESS'
                or snapshot.received_at_ns<target.started_at_ns
            ):
                return None
            end_stamp=self._different_boss_end_stamp(target,stamp)
        else:
            candidates=[
                encounter for encounter in self.tracker.encounters.values()
                if encounter.local_encounter_id in self.session_encounter_ids
                and encounter.instance_id==snapshot.instance_id
                and encounter.result=='VICTORY'
                and encounter.settlement_status=='PENDING'
                and encounter.ended_at_ns is not None
                and 0<=snapshot.received_at_ns-int(encounter.ended_at_ns)
                <=5_000_000_000
                and encounter.self_token in {member.id for member in snapshot.members}
            ]
            if not candidates:
                return None
            latest_end=max(int(encounter.ended_at_ns or 0) for encounter in candidates)
            candidates=[
                encounter for encounter in candidates
                if int(encounter.ended_at_ns or 0)==latest_end
            ]
            if len(candidates)!=1:
                return None
            target=candidates[0]
            end_stamp=int(target.ended_at_ns)
        if (
            target.stage_id!=snapshot.stage_id
            or target.stage_index!=snapshot.stage_index
        ):
            target.stage_id=snapshot.stage_id
            target.stage_index=snapshot.stage_index
            target.revision+=1
        if target.local_encounter_id==self.tracker.current_id:
            self._end_observed_encounter('VICTORY',end_stamp)
        self.last_visible_id=target.local_encounter_id
        return target

    @staticmethod
    def _confirm_successful_boss_health(encounter, stamp):
        """Apply the authoritative zero implied by a successful STAGE table."""
        if encounter is None:
            return False
        current_hp=int(encounter.boss_current_hp or 0)
        source=encounter.boss_health_source
        ended_at=int(encounter.ended_at_ns or 0)
        observed_before=int(encounter.boss_health_observed_at_ns or 0)
        if current_hp==0:
            if source=='passive_encounter_end_snapshot':
                return False
            if source=='server_statistics' and observed_before>ended_at:
                return False
        observed_at=max(
            observed_before,int(stamp or 0)
        )
        changed=bool(
            encounter.boss_current_hp!=0
            or int(encounter.boss_health_observed_at_ns or 0)!=observed_at
            or encounter.boss_health_source!='server_statistics'
        )
        if changed:
            encounter.boss_current_hp=0
            encounter.boss_health_observed_at_ns=observed_at
            encounter.boss_health_source='server_statistics'
            encounter.revision+=1
        return changed

    def process(self, observation):
        before=self._tracker_state_signature()
        record=observation['record'];context=observation['context'];stamp=observation['timestamp_ns']
        method=record.get('method');args=record.get('decoded_arguments',[])
        instance=context.get('instance_id')
        scene_transition=method in SCENE_TRANSITION_METHODS
        current_before_transition=self.tracker.encounters.get(
            self.tracker.current_id
        )
        if (
            scene_transition
            and current_before_transition is not None
            and stamp <= current_before_transition.started_at_ns
        ):
            # A local-role-scoped leave packet can be held until identity is
            # known, then drain after the next Boss pull has already started.
            # Its frozen capture timestamp proves that it belongs before the
            # live encounter.  Applying it now would either raise on a
            # negative interval or reset the new pull at zero seconds.
            return False
        if scene_transition:
            if self.instance_id:self.tracker.leave_instance(self.instance_id,stamp)
            self.instance_id=None;self.last_visible_id=None;self.boss_entities.clear();self.boss_in_combat.clear();self.boss_out_at.clear();self.boss_health_samples.clear();self.boss_max_health_samples.clear();self.pending_boss_starts.clear();self.last_statistics_boundary=None
        elif instance:
            if self.instance_id and instance!=self.instance_id:
                self.tracker.leave_instance(self.instance_id,stamp)
                self.last_visible_id=None;self.boss_entities.clear();self.boss_in_combat.clear();self.boss_out_at.clear();self.boss_health_samples.clear();self.boss_max_health_samples.clear();self.pending_boss_starts.clear()
            self.instance_id=instance
        if not scene_transition:
            observed_bosses=context.get('bosses',{})
            self.boss_entities.update(observed_bosses)
            for boss_entity,boss_identity in observed_bosses.items():
                for collection in (
                    self.boss_health_samples,
                    self.boss_max_health_samples,
                ):
                    for sample in collection.get(str(boss_entity),()):
                        if not sample.get('boss_token'):
                            sample['boss_token']=boss_identity.get('token')
                        if not sample.get('template_id'):
                            sample['template_id']=boss_identity.get('template_id')
            self._refresh_incomplete_encounter_roster(context,stamp)
        entity=str(record.get('network_entity_id',''))
        boss=self.boss_entities.get(entity)
        current=self.tracker.encounters.get(self.tracker.current_id)
        if boss:
            resolved=self._resolve_pending_boss_start(entity,boss,context,stamp)
            if resolved is not None:
                current=resolved
        if current is None:
            resolved=self._resolve_ready_pending_boss_start(context,stamp)
            if resolved is not None:
                current=resolved
        if method=='NpcapBossHpActivity':
            # Capture may begin after the server's fight-mode [2] edge.  The
            # adapter emits this only after an exact Boss promotion and only
            # when two or more pre-promotion HP samples prove a decrease.  Its
            # timestamp is the first observed positive HP, not the later
            # metadata/death packet that finally identified the target.
            if boss:
                self._begin_observed_encounter(entity,boss,stamp,context)
                self.boss_in_combat[entity]=True
                self.boss_out_at.pop(entity,None)
            else:
                self._remember_unknown_boss_start(entity,stamp,context)
        if method=='OnMsgSyncFightMode' and args in ([0],[2]):
            if not boss:
                if args==[2]:
                    self._remember_unknown_boss_start(
                        entity,stamp,context,challenge_start=True
                    )
                else:
                    self.pending_boss_starts.pop(entity,None)
            else:
                was_in_combat=self.boss_in_combat.get(entity)
                if args==[2]:
                    if current and current.boss_token==boss.get('token') and was_in_combat is False:
                        end_stamp=max(
                            current.started_at_ns,
                            int(self.boss_out_at.get(entity,stamp)),
                        )
                        self._end_observed_encounter('WIPE',end_stamp)
                        current=None
                    elif current:
                        end_stamp=self._restart_of_current_boss(
                            current,entity,boss,stamp
                        )
                        if end_stamp is not None:
                            self._end_observed_encounter('WIPE',end_stamp)
                            current=None
                    self._begin_observed_encounter(
                        entity,boss,stamp,context,challenge_start=True
                    )
                    self.boss_out_at.pop(entity,None)
                else:
                    self.boss_in_combat[entity]=False
                    self.boss_out_at[entity]=stamp
        if method=='OnMsgDungeonStageSettlement':
            self._apply_stage_victory_boundary(args,stamp,instance)
        current=self.tracker.encounters.get(self.tracker.current_id)
        if current:
            actor=record.get('network_entity_id')
            member=next((r for r in current.participants_snapshot if r.get('iid')==actor),None)
            if member and method in ('OnMsgEntityDead','OnMsgEntityRelive'):
                self.tracker.life(member['id'],stamp,method=='OnMsgEntityDead')
            if (
                method=='OnMsgEntityDead'
                and boss
                and boss.get('token')==current.boss_token
                and self._boss_death_completes_encounter(current,boss)
            ):
                self._end_observed_encounter('VICTORY',stamp)
            else:
                all_dead=all(current.life_events.get(r['id']) and current.life_events[r['id']][-1][1]
                             for r in current.participants_snapshot)
                boss_off=any(b.get('token')==current.boss_token and self.boss_in_combat.get(k) is False
                             for k,b in self.boss_entities.items())
                if all_dead and boss_off:self._end_observed_encounter('WIPE',stamp)
        if method in STATISTICS_MESSAGES:
            try:
                snapshots=normalize_statistics(record,instance_id=instance,
                    dungeon_id=context.get('dungeon_id'),map_id=context.get('map_id'))
                if snapshots:
                    current_stage=snapshots[0]
                    self.last_statistics_boundary={
                        'timestamp_ns':current_stage.received_at_ns,
                        'instance_id':current_stage.instance_id,
                        'stage_id':current_stage.stage_id,
                        'stage_index':current_stage.stage_index,
                        'is_stage_success':current_stage.is_stage_success,
                        'battle_id':current_stage.battle_id,
                        'summary_id':current_stage.fingerprint,
                    }
                    final_target=None
                    if method==SETTLEMENT_MESSAGE:
                        final_target=self._apply_final_statistics_victory_boundary(
                            current_stage,stamp,context
                        )
                else:
                    final_target=None
                for snapshot in snapshots:
                    target=(
                        final_target
                        if snapshot.scope=='STAGE' and final_target is not None
                        else self._direct_server_statistics_target(
                            snapshot,context,
                            allow_without_live=(method==SETTLEMENT_MESSAGE),
                        )
                    )
                    if target is not None:
                        match=self.tracker.accept(
                            snapshot,
                            preferred_encounter_id=target.local_encounter_id,
                            preferred_authoritative_final=bool(
                                snapshot.scope == 'STAGE'
                                and final_target is not None
                                and target.local_encounter_id
                                == final_target.local_encounter_id
                            ),
                        )
                        if match.encounter_id:
                            self.last_visible_id=target.local_encounter_id
                    else:
                        match=self.tracker.accept(snapshot)
                    if (
                        snapshot.scope=='STAGE'
                        and snapshot.is_stage_success is True
                        and match.encounter_id
                    ):
                        self._confirm_successful_boss_health(
                            self.tracker.encounters.get(match.encounter_id),stamp
                        )
                self.tracker.retry_orphans()
                self.last_error=None
            except (ValueError,KeyError,TypeError) as error:
                self.last_error=f'统计结构未通过校验：{type(error).__name__}'
        changed=before!=self._tracker_state_signature()
        if changed:
            self.repository.save(self.tracker)
            self.generation+=1
        return changed

    def visible(self, live_self=None):
        key=self.tracker.current_id or self.last_visible_id
        if key not in self.session_encounter_ids:
            return None
        record=self.tracker.encounters.get(key)
        return (
            encounter_view(
                record,
                live_self=live_self,
                statistics_scope=completed_statistics_scope(record),
            )
            if record
            else None
        )

    def hide_history_records(self, encounter_ids) -> set[str]:
        hidden=set()
        for encounter_id in encounter_ids:
            key=str(encounter_id or '')
            record=self.tracker.encounters.get(key)
            if record is None or record.history_deleted:
                continue
            record.history_deleted=True
            record.revision+=1
            hidden.add(key)
        if hidden:
            self.repository.save(self.tracker)
            self.generation+=1
        return hidden
