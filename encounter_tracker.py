"""Independent encounters and conservative deferred-settlement matching.

Only server cumulative STAGE snapshots settle damage. Receipt time is never
silently substituted for combat time. Unknown and ambiguous tables stay orphaned.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass, field, replace
import math
import uuid

from combat_statistics import NormalizedCombatStatistics
from network_state import boss_template_continues_encounter, is_ai_team_token


NEXT_CHALLENGE_BOUNDARY_WINDOW_NS = 250_000_000


@dataclass(frozen=True)
class EncounterDuration:
    seconds: float | None
    source: str


class EncounterDurationResolver:
    """Use explicit encounter-level clock evidence, never member battleLength."""
    @staticmethod
    def resolve(record: 'EncounterRecord') -> EncounterDuration:
        clock = record.duration_evidence
        if not clock or clock.get('verified') is not True:
            return EncounterDuration(None, 'unavailable')
        if clock.get('source') not in (
            'server_encounter_clock',
            'verified_shared_encounter_clock',
            'local_encounter_endpoints',
        ):
            return EncounterDuration(None, 'unavailable')
        if clock.get('local_encounter_id') != record.local_encounter_id:
            return EncounterDuration(None, 'unavailable')
        seconds = clock.get('seconds')
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not math.isfinite(seconds) or not 0 < seconds <= 86400:
            return EncounterDuration(None, 'unavailable')
        return EncounterDuration(float(seconds), clock['source'])


@dataclass
class EncounterRecord:
    local_encounter_id: str
    instance_id: str
    dungeon_id: int | None
    map_id: int | None
    stage_id: int | None
    stage_index: int | None
    boss_template_id: int | None
    boss_token: str | None
    started_at_ns: int
    participants_snapshot: list[dict]
    self_token: str | None = None
    party_session_id: int = 0
    server_battle_id: str | None = None
    ended_at_ns: int | None = None
    result: str = 'IN_PROGRESS'
    settlement_status: str = 'LIVE'
    stage_statistics: dict | None = None
    all_statistics: dict | None = None
    settlement_received_at_ns: int | None = None
    encounter_duration_seconds: float | None = None
    duration_source: str = 'unavailable'
    duration_evidence: dict | None = None
    match_confidence: str = 'unmatched'
    data_source: str = 'npcap_server_statistics'
    participants: list[dict] = field(default_factory=list)
    life_events: dict[str, list[tuple[int, bool]]] = field(default_factory=dict)
    snapshot_fingerprints: dict[str, str] = field(default_factory=dict)
    capture_complete: bool = True
    automatic_match_blocked: bool = False
    revision: int = 0
    boss_name: str | None = None
    boss_current_hp: float | None = None
    boss_max_hp: float | None = None
    boss_health_observed_at_ns: int | None = None
    boss_health_source: str | None = None
    history_deleted: bool = False
    boss_phase_tokens: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict) -> 'EncounterRecord':
        return cls(**deepcopy(value))

    @property
    def roster(self) -> dict[str, dict]:
        return {row['id']: row for row in self.participants_snapshot}

    def observed_alive_seconds(self, token: str) -> float | None:
        if self.ended_at_ns is None or not self.capture_complete:
            return None
        cursor, dead, total = self.started_at_ns, False, 0
        for timestamp, now_dead in sorted(self.life_events.get(token, [])):
            if not self.started_at_ns <= timestamp <= self.ended_at_ns:
                continue
            if not dead:
                total += timestamp - cursor
            cursor, dead = timestamp, now_dead
        if not dead:
            total += self.ended_at_ns - cursor
        return total / 1e9

    def death_count(self, token: str) -> int:
        if self.settlement_status == 'SETTLED' and isinstance(
            self.stage_statistics, dict
        ):
            members = self.stage_statistics.get('members')
            if isinstance(members, (list, tuple)):
                server_member = next((
                    member for member in members
                    if isinstance(member, dict) and member.get('id') == token
                ), None)
                killer_map = (
                    server_member.get('killer_map')
                    if isinstance(server_member, dict)
                    else None
                )
                if isinstance(killer_map, dict):
                    amounts = list(killer_map.values())
                    if all(
                        isinstance(amount, int)
                        and not isinstance(amount, bool)
                        and amount >= 0
                        for amount in amounts
                    ):
                        return sum(amounts)
        dead, count = False, 0
        for timestamp, state in sorted(self.life_events.get(token, [])):
            if timestamp < self.started_at_ns or (self.ended_at_ns is not None and timestamp > self.ended_at_ns):
                continue
            if state and not dead:
                count += 1
            dead = state
        return count


@dataclass(frozen=True)
class SettlementMatch:
    encounter_id: str | None
    confidence: str
    reason: str


class SettlementMatcher:
    @staticmethod
    def match(stat: NormalizedCombatStatistics, encounters: list[EncounterRecord]) -> SettlementMatch:
        if stat.scope != 'STAGE':
            return SettlementMatch(None, 'unmatched', 'missing_scoped_battle_identity')
        historical_group = bool(stat.associated_battle_id and not stat.battle_id)
        if historical_group:
            # The first STAGE in OnMsgSettlementCombatStatistics is the
            # authoritative result for the battle ID carried by that packet.
            # Additional STAGE groups are older dungeon stages.  They may fill
            # an older PENDING encounter, but must never settle the current
            # encounter merely because its start inherited the previous stage
            # context.  Anchor those groups to the already-bound current stage
            # and require a strictly older, independently ended pending pull.
            associated = [
                encounter
                for encounter in encounters
                if encounter.instance_id == stat.instance_id
                and encounter.server_battle_id == stat.associated_battle_id
            ]
            if len(associated) != 1:
                return SettlementMatch(
                    None,
                    'unmatched',
                    'unbound_associated_current_stage',
                )
            current_stage = associated[0]
            encounters = [
                encounter
                for encounter in encounters
                if encounter.local_encounter_id
                != current_stage.local_encounter_id
                and encounter.settlement_status == 'PENDING'
                and encounter.ended_at_ns is not None
                and encounter.ended_at_ns <= current_stage.started_at_ns
            ]
            if not encounters:
                return SettlementMatch(
                    None,
                    'unmatched',
                    'no_pending_historical_stage',
                )
        stat_roster = {m.id for m in stat.members}

        def shared_member_iids_match(record: EncounterRecord) -> bool:
            roster = record.roster
            return not any(
                member.id in roster
                and member.iid is not None
                and roster[member.id].get('iid') is not None
                and member.iid != roster[member.id]['iid']
                for member in stat.members
            )

        def roster_matches_stat(record: EncounterRecord) -> bool:
            if set(record.roster) != stat_roster:
                return False
            return shared_member_iids_match(record)

        def boss_tokens(record: EncounterRecord) -> set[str]:
            return {token for token in (
                record.boss_token, *record.boss_phase_tokens
            ) if token}

        def unbound_projection_roster_matches_stat(record: EncounterRecord) -> bool:
            """Ignore only synthetic slots never bound to a player entity."""

            confirmed = set()
            unbound = 0
            for member in record.participants_snapshot:
                if (
                    member.get('is_ai') is True
                    and not member.get('iid')
                    and not str(member.get('name') or '').strip()
                ):
                    unbound += 1
                else:
                    confirmed.add(member.get('id'))
            return bool(unbound and confirmed == stat_roster and shared_member_iids_match(record))

        def projection_roster_extends_record(record: EncounterRecord) -> bool:
            """Accept only server-proven AI slots missing from a local start.

            A capture started mid-instance can know the two real characters
            before DungeonBotDisplay has exposed the four projections.  This
            is deliberately not a generic incomplete-roster fallback: every
            local token must still occur in the server table and every added
            token must carry the game's synthetic-member token form.
            """

            local_roster = set(record.roster)
            if (
                not local_roster
                or not local_roster < stat_roster
                or not shared_member_iids_match(record)
            ):
                return False
            missing = [member for member in stat.members if member.id not in local_roster]
            return bool(missing) and all(
                is_ai_team_token(member.id)
                for member in missing
            )

        def exact_roster_matches_stat(record: EncounterRecord) -> bool:
            if not roster_matches_stat(record):
                return False
            return all(
                member.iid is not None
                and record.roster[member.id].get('iid') is not None
                and member.iid == record.roster[member.id]['iid']
                for member in stat.members
            )

        def boss_clock_partial_roster_matches(record: EncounterRecord) -> bool:
            # Players may leave after participating in a pull. The server
            # keeps their rows, while the local start roster can be smaller.
            # Require the exact Boss token and a matching encounter clock so
            # the next Boss's delayed table cannot fill the previous result.
            if (
                not stat.battle_id
                or not record.capture_complete
                or record.automatic_match_blocked
                or not set(record.roster) < stat_roster
                or not shared_member_iids_match(record)
                or not record.boss_token
                or not boss_tokens(record) & references
                or record.ended_at_ns is None
            ):
                return False
            member_seconds = [
                member.member_battle_length
                for member in stat.members
                if member.member_battle_length is not None
                and member.member_battle_length > 0
            ]
            local_seconds = (
                record.ended_at_ns - record.started_at_ns
            ) / 1_000_000_000
            return bool(
                member_seconds
                and local_seconds > 0
                and abs(max(member_seconds) - local_seconds)
                <= max(5.0, local_seconds * 0.20)
            )

        def same_boss(left: EncounterRecord, right: EncounterRecord) -> bool:
            template_match = bool(
                left.boss_template_id is not None
                and right.boss_template_id is not None
                and (
                    boss_template_continues_encounter(
                        left.boss_template_id, right.boss_template_id
                    )
                    or boss_template_continues_encounter(
                        right.boss_template_id, left.boss_template_id
                    )
                )
            )
            token_match = bool(boss_tokens(left) & boss_tokens(right))
            return template_match or token_match

        def exact_next_pull_boundary(record: EncounterRecord) -> bool:
            """Bind the returned STAGE table to the immediately preceding pull.

            The server sends that table immediately before the next Challenge.
            At this boundary the dungeon instance and ordering identify the old
            pull; the next Boss, roster and stage may all have changed.
            """

            if (
                record.result not in ('WIPE', 'VICTORY')
                or record.automatic_match_blocked
                or record.ended_at_ns is None
            ):
                return False
            successors = [
                candidate
                for candidate in encounters
                if candidate.local_encounter_id != record.local_encounter_id
                and candidate.instance_id == record.instance_id
                and 0 <= candidate.started_at_ns - stat.received_at_ns
                <= NEXT_CHALLENGE_BOUNDARY_WINDOW_NS
                and candidate.started_at_ns >= record.ended_at_ns
            ]
            if len(successors) != 1:
                return False
            successor = successors[0]
            if record.ended_at_ns > successor.started_at_ns:
                return False
            preceding = [
                candidate
                for candidate in encounters
                if candidate.local_encounter_id != successor.local_encounter_id
                and candidate.instance_id == successor.instance_id
                and candidate.ended_at_ns is not None
                and candidate.ended_at_ns <= successor.started_at_ns
                and candidate.settlement_status != 'ABANDONED'
                and candidate.result in ('WIPE', 'VICTORY')
            ]
            if not preceding:
                return False
            nearest = max(
                preceding,
                key=lambda candidate: (
                    int(candidate.ended_at_ns or 0),
                    int(candidate.started_at_ns),
                ),
            )
            return nearest.local_encounter_id == record.local_encounter_id

        def unbound_projection_next_pull_boundary(record: EncounterRecord) -> bool:
            if (
                record.result != 'WIPE'
                or record.automatic_match_blocked
                or record.ended_at_ns is None
                or stat.stage_id is None
                or not unbound_projection_roster_matches_stat(record)
                or not 0 <= stat.received_at_ns - record.ended_at_ns <= 120_000_000_000
                or (record.stage_id is not None and record.stage_id != stat.stage_id)
                or (
                    stat.stage_index is not None
                    and record.stage_index is not None
                    and record.stage_index != stat.stage_index
                )
            ):
                return False
            successors = [
                candidate for candidate in encounters
                if candidate.local_encounter_id != record.local_encounter_id
                and candidate.instance_id == record.instance_id
                and 0 <= candidate.started_at_ns - stat.received_at_ns
                <= NEXT_CHALLENGE_BOUNDARY_WINDOW_NS
                and candidate.started_at_ns >= record.ended_at_ns
                and same_boss(record, candidate)
            ]
            if len(successors) != 1:
                return False
            preceding = [
                candidate for candidate in encounters
                if candidate.local_encounter_id != successors[0].local_encounter_id
                and candidate.instance_id == record.instance_id
                and candidate.result in ('WIPE', 'VICTORY')
                and candidate.ended_at_ns is not None
                and candidate.ended_at_ns <= successors[0].started_at_ns
            ]
            return bool(preceding) and max(
                preceding, key=lambda candidate: candidate.ended_at_ns
            ).local_encounter_id == record.local_encounter_id

        def unique_unscoped_next_pull() -> EncounterRecord | None:
            """Recover a delayed wipe table whose packet lacks scene identity.

            Some clients receive the previous pull's STAGE table immediately
            before the next Boss fight-mode edge in the same inbound batch.
            In that ordering the parser has not attached ``instance_id`` yet.
            The exact successor start timestamp, successor instance, complete
            stable-token roster, stage and same Boss independently scope the
            packet; no receipt-order-only fallback is permitted.
            """

            if stat.instance_id or stat.is_stage_success is True:
                return None
            successors = [
                candidate
                for candidate in encounters
                if 0 <= candidate.started_at_ns - stat.received_at_ns
                <= NEXT_CHALLENGE_BOUNDARY_WINDOW_NS
                and bool(candidate.instance_id)
                and stat.stage_id is not None
                and (
                    candidate.stage_id is None
                    or candidate.stage_id == stat.stage_id
                )
                and (
                    stat.stage_index is None
                    or candidate.stage_index is None
                    or candidate.stage_index == stat.stage_index
                )
            ]
            if len(successors) != 1:
                return None
            successor = successors[0]
            preceding = [
                candidate
                for candidate in encounters
                if candidate.local_encounter_id != successor.local_encounter_id
                and candidate.instance_id == successor.instance_id
                and candidate.result == 'WIPE'
                and candidate.settlement_status != 'ABANDONED'
                and candidate.ended_at_ns is not None
                and candidate.ended_at_ns <= successor.started_at_ns
                and not candidate.automatic_match_blocked
                and exact_roster_matches_stat(candidate)
                and stat.stage_id is not None
                and (
                    candidate.stage_id is None
                    or candidate.stage_id == stat.stage_id
                )
                and (
                    stat.stage_index is None
                    or candidate.stage_index is None
                    or candidate.stage_index == stat.stage_index
                )
                and same_boss(candidate, successor)
            ]
            if not preceding:
                return None
            nearest_end = max(int(candidate.ended_at_ns or 0) for candidate in preceding)
            nearest = [
                candidate
                for candidate in preceding
                if int(candidate.ended_at_ns or 0) == nearest_end
            ]
            return nearest[0] if len(nearest) == 1 else None

        if not stat.instance_id:
            recovered = unique_unscoped_next_pull()
            if recovered is None:
                return SettlementMatch(
                    None, 'unmatched', 'missing_scoped_battle_identity'
                )
            return SettlementMatch(
                recovered.local_encounter_id,
                'high',
                'unique_unscoped_wipe_next_pull_stage_roster',
            )

        references = {
            key
            for member in stat.members
            for mapping in (member.bear_map, member.killer_map)
            if mapping
            for key in mapping
        }
        single_reference = next(iter(references)) if len(references) == 1 else None

        def projection_victory_next_boss_clock(record: EncounterRecord) -> bool:
            """Bind a delayed victory to the preceding Boss boundary.

            The party may change between Bosses.  The dungeon instance and the
            ordered end/start boundary are the identity evidence; roster
            overlap is deliberately not required.
            """

            if (
                not stat.battle_id
                or stat.is_stage_success is not True
                or not record.capture_complete
                or record.automatic_match_blocked
                or record.ended_at_ns is None
                or not 0 <= stat.received_at_ns - record.ended_at_ns <= 90_000_000_000
            ):
                return False
            local_seconds = (record.ended_at_ns - record.started_at_ns) / 1_000_000_000
            member_seconds = [
                member.member_battle_length
                for member in stat.members
                if member.member_battle_length and member.member_battle_length > 0
            ]
            if (
                local_seconds <= 0
                or not member_seconds
                or abs(max(member_seconds) - local_seconds)
                > max(5.0, local_seconds * 0.05)
            ):
                return False
            successors = [
                candidate for candidate in encounters
                if candidate.local_encounter_id != record.local_encounter_id
                and candidate.instance_id == record.instance_id
                and 0 <= candidate.started_at_ns - stat.received_at_ns
                <= NEXT_CHALLENGE_BOUNDARY_WINDOW_NS
                and candidate.started_at_ns >= record.ended_at_ns
            ]
            if len(successors) != 1:
                return False
            preceding = [
                candidate for candidate in encounters
                if candidate.local_encounter_id != successors[0].local_encounter_id
                and candidate.instance_id == record.instance_id
                and candidate.result in ('VICTORY', 'WIPE')
                and candidate.ended_at_ns is not None
                and candidate.ended_at_ns <= successors[0].started_at_ns
            ]
            return bool(preceding) and max(
                preceding, key=lambda candidate: candidate.ended_at_ns
            ).local_encounter_id == record.local_encounter_id

        bound = [
            e for e in encounters
            if stat.battle_id
            and e.instance_id == stat.instance_id
            and e.server_battle_id == stat.battle_id
        ]
        if len(bound) > 1:
            return SettlementMatch(None, 'unmatched', 'conflicting_battle_binding')
        candidates = []
        for e in bound or encounters:
            if e.instance_id != stat.instance_id or e.settlement_status == 'ABANDONED' or e.ended_at_ns is None:
                continue
            if (
                e.server_battle_id
                and e.server_battle_id != stat.battle_id
                and not historical_group
            ):
                continue
            if stat.received_at_ns < e.started_at_ns or stat.received_at_ns + 1_000_000_000 < e.ended_at_ns:
                continue
            exact_roster = roster_matches_stat(e)
            projection_roster = bool(
                stat.is_stage_success is True
                and e.result == 'VICTORY'
                and projection_roster_extends_record(e)
            )
            next_pull_boundary = exact_next_pull_boundary(e)
            unbound_projection_boundary = unbound_projection_next_pull_boundary(e)
            partial_roster = boss_clock_partial_roster_matches(e)
            if not exact_roster and not projection_roster and not next_pull_boundary and not unbound_projection_boundary and not partial_roster:
                continue
            if stat.is_stage_success is True and e.result != 'VICTORY':
                continue
            if stat.is_stage_success is False and e.result != 'WIPE':
                continue
            if stat.battle_id and e.server_battle_id == stat.battle_id:
                candidates.append((
                    e,
                    (
                        'bound_battle_id_projection_roster'
                        if projection_roster
                        else 'bound_battle_id'
                    ),
                    3,
                ))
                continue
            # First binding requires an independently observed ended encounter.
            if e.result not in ('VICTORY', 'WIPE'):
                continue
            if stat.server_started_at is not None and stat.server_started_at > 0:
                # Local capture time and server epoch are different clocks unless
                # an explicit conversion was established; do not compare blindly.
                pass
            stage_matches = bool(
                stat.stage_id is not None
                and (e.stage_id is None or e.stage_id == stat.stage_id)
                and (
                    stat.stage_index is None
                    or e.stage_index is None
                    or e.stage_index == stat.stage_index
                )
            )
            # In multi-Boss dungeons the statistics for stage N are delivered
            # at the exact start of stage N+1. A meter started mid-instance can
            # therefore freeze the preceding stage identity on the first pull
            # it observes. Consecutive IDs plus an exact instance/roster and an
            # independently observed end form a safe, uniqueness-gated bridge.
            adjacent_delayed_stage = bool(
                stat.stage_id is not None
                and stat.stage_index is not None
                and e.stage_id is not None
                and e.stage_index is not None
                and stat.stage_id == e.stage_id + 1
                and stat.stage_index == e.stage_index + 1
            )
            if (
                e.result == 'VICTORY'
                and stat.is_stage_success is True
                and projection_victory_next_boss_clock(e)
            ):
                candidates.append((e, 'unique_victory_next_boss_boundary', 2))
                continue
            if e.result == 'VICTORY':
                if stat.is_stage_success is False or not e.boss_token:
                    continue
                if next_pull_boundary:
                    candidates.append((
                        e,
                        'unique_previous_stage_next_challenge_boundary',
                        3,
                    ))
                    continue
                if partial_roster:
                    candidates.append((e, 'unique_boss_token_partial_roster_clock', 2))
                    continue
                if (
                    stat.source == 'OnMsgSettlementCombatStatistics'
                    and stat.is_stage_success is True
                    and stat.battle_id
                    and exact_roster
                    and str(e.boss_token).startswith('entity:')
                    and e.stage_id is not None
                    and e.stage_id == stat.stage_id
                    and e.stage_index is not None
                    and e.stage_index == stat.stage_index
                    and e.capture_complete
                    and not e.automatic_match_blocked
                    and 0 <= stat.received_at_ns - e.ended_at_ns <= 2_000_000_000
                ):
                    candidates.append((e, 'unique_final_victory_stage_roster_boundary', 2))
                    continue
                if projection_roster:
                    if boss_tokens(e) & references and not e.automatic_match_blocked:
                        candidates.append((
                            e,
                            'unique_victory_projection_roster_boss_token',
                            2,
                        ))
                    elif (
                        single_reference
                        and str(e.boss_token).startswith('entity:')
                        and stat.stage_id is not None
                        and not e.automatic_match_blocked
                        and 0 <= stat.received_at_ns - int(e.ended_at_ns or 0)
                        <= 2_000_000_000
                    ):
                        # Migration for records already persisted by builds
                        # that saw only a transient entity ID at startup.  The
                        # explicit success edge, one Boss reference, same
                        # instance and sub-two-second boundary must all agree;
                        # multiple candidates remain ambiguous below.
                        candidates.append((
                            e,
                            'unique_victory_projection_roster_entity_boss_boundary',
                            2,
                        ))
                    continue
                if boss_tokens(e) & references and not e.automatic_match_blocked:
                    # The stage/map context frozen at Boss spawn can still
                    # describe the preceding room: the authoritative stage ID
                    # often advances only in a later settlement RPC.  The
                    # exact Boss token embedded in the server table is stronger
                    # evidence and safely bridges that one-stage delay.
                    candidates.append((e, 'unique_victory_roster_boss_token', 2))
                elif (
                    stage_matches
                    and not references
                    and e.capture_complete
                    and not e.automatic_match_blocked
                ):
                    # Some bosses never damage a party member, so their token
                    # is absent from bear/killer maps. Only a table with no
                    # other target references can use the stage fallback;
                    # a reference to the next Boss proves this table is not
                    # the preceding Boss even if the stage ID is stale.
                    candidates.append((e, 'unique_victory_stage_roster', 1))
                elif (
                    adjacent_delayed_stage
                    and stat.battle_id
                    and stat.is_stage_success is True
                    and e.capture_complete
                    and not e.automatic_match_blocked
                ):
                    # The final settlement's second argument is the current
                    # Boss STAGE and carries its battleID.  At the exact prior
                    # Statistics -> next Challenge boundary the new encounter
                    # can temporarily inherit stage N while this authoritative
                    # result is stage N+1.  An exact roster, explicit victory,
                    # adjacent stage/index and unique ended candidate safely
                    # repair that one-stage lag even when Boss promotion only
                    # exposed a transient ``entity:N`` token.
                    candidates.append((
                        e,
                        'unique_victory_adjacent_stage_roster',
                        1,
                    ))
            elif e.result == 'WIPE':
                if stat.is_stage_success is True or not e.boss_token:
                    continue
                if unbound_projection_boundary:
                    candidates.append((e, 'unique_wipe_unbound_projection_next_pull', 2))
                    continue
                if partial_roster:
                    candidates.append((e, 'unique_boss_token_partial_roster_clock', 2))
                    continue
                candidate_boss_tokens = list(boss_tokens(e))
                if str(e.boss_token).startswith('entity:'):
                    # A mid-instance Npcap start knows the exact Boss entity
                    # but may miss its creation token.  The delayed server
                    # table can safely recover that token only when one same
                    # killer accounts for every independently observed death.
                    killer_sets = [
                        set(m.killer_map or {}) for m in stat.members
                    ]
                    candidate_boss_tokens = list(set.intersection(*killer_sets)) if killer_sets else []
                valid = False
                if not e.automatic_match_blocked:
                    for boss_token in candidate_boss_tokens:
                        token_valid = True
                        for m in stat.members:
                            deaths = e.death_count(m.id)
                            alive = e.observed_alive_seconds(m.id)
                            if (not deaths or m.killer_map is None or m.killer_map.get(boss_token) != deaths
                                    or m.member_battle_length is None or alive is None
                                    or abs(m.member_battle_length - alive) > 1.25):
                                token_valid = False
                                break
                        if token_valid:
                            valid = True
                            break
                if valid:
                    candidates.append((e, 'unique_wipe_roster_boss_life_intervals', 2))
                elif next_pull_boundary:
                    candidates.append((
                        e,
                        'unique_previous_stage_next_challenge_boundary',
                        3,
                    ))
                elif (
                    (stage_matches or adjacent_delayed_stage)
                    and (not references or bool(boss_tokens(e) & references))
                    and e.capture_complete
                    and not e.automatic_match_blocked
                ):
                    # A capture can miss one member's death edge while still
                    # having an exact complete start roster and Boss boundary.
                    # Accept only when this fallback is unique.
                    reason = (
                        'unique_wipe_adjacent_stage_roster'
                        if adjacent_delayed_stage and not stage_matches
                        else 'unique_wipe_stage_roster'
                    )
                    candidates.append((e, reason, 1))
        if not candidates:
            return SettlementMatch(None, 'unmatched', 'no_proven_candidate')
        strongest = max(candidate[2] for candidate in candidates)
        candidates = [
            candidate for candidate in candidates if candidate[2] == strongest
        ]
        if len(candidates) != 1:
            return SettlementMatch(None, 'unmatched', 'ambiguous_candidates')
        return SettlementMatch(candidates[0][0].local_encounter_id, 'high', candidates[0][1])


class EncounterTracker:
    def __init__(self):
        self.encounters: dict[str, EncounterRecord] = {}
        self.current_id: str | None = None
        self.orphans: dict[str, dict] = {}
        # Transient diagnostics are deliberately excluded from ``to_dict`` so
        # the journal schema and matcher behaviour remain unchanged.
        self._accept_diagnostics: list[dict] = []

    def begin(self, *, instance_id: str, started_at_ns: int, participants_snapshot: list[dict],
              dungeon_id: int | None = None, map_id: int | None = None, stage_id: int | None = None,
              stage_index: int | None = None, boss_template_id: int | None = None,
              boss_token: str | None = None, self_token: str | None = None,
              party_session_id: int = 0) -> EncounterRecord:
        if not instance_id or not isinstance(started_at_ns, int) or started_at_ns <= 0:
            raise ValueError('Missing encounter start identity')
        if self.current_id and self.encounters[self.current_id].result == 'IN_PROGRESS':
            raise ValueError('A live encounter must end independently before a new one begins')
        roster = deepcopy(participants_snapshot)
        tokens = [r.get('id') for r in roster]
        if not tokens or len(tokens) > 64 or any(not isinstance(t, str) or not t for t in tokens) or len(set(tokens)) != len(tokens):
            raise ValueError('Invalid start roster')
        e = EncounterRecord(uuid.uuid4().hex, instance_id, dungeon_id, map_id, stage_id, stage_index,
                            boss_template_id, boss_token, started_at_ns, roster,
                            self_token=self_token,
                            party_session_id=max(0, int(party_session_id or 0)))
        e.participants = [dict(r, damage=None, dps=None, settlement_hint='战后结算') for r in roster]
        # A failed-pull table may already be waiting as an orphan because the
        # server places it immediately before this pull's fight-mode edge in
        # the same batch.  Use the validated prospective start as independent
        # boundary evidence, but settle the previous pull before publishing the
        # new Encounter.  That preserves the server's Statistics -> [2] order
        # and makes it impossible for the previous table to touch the new pull.
        self.retry_orphans(prospective_encounter=e)
        self.encounters[e.local_encounter_id] = e
        self.current_id = e.local_encounter_id
        return e

    def life(self, token: str, timestamp_ns: int, dead: bool) -> None:
        if not self.current_id:
            return
        e = self.encounters[self.current_id]
        if token not in e.roster or timestamp_ns < e.started_at_ns or e.result != 'IN_PROGRESS':
            return
        events = e.life_events.setdefault(token, [])
        item = (timestamp_ns, bool(dead))
        if item not in events:
            events.append(item)
            events.sort()

    def end(self, result: str, ended_at_ns: int) -> EncounterRecord:
        if result not in ('VICTORY', 'WIPE', 'RESET') or not self.current_id:
            raise ValueError('Invalid encounter end')
        e = self.encounters[self.current_id]
        if e.result != 'IN_PROGRESS' or ended_at_ns < e.started_at_ns:
            raise ValueError('Invalid encounter interval')
        e.ended_at_ns, e.result = ended_at_ns, result
        e.settlement_status = 'ABANDONED' if result == 'RESET' else 'PENDING'
        for member in e.participants:
            member['settlement_hint'] = '未取得结算' if result == 'RESET' else '等待结算'
        e.revision += 1
        self.current_id = None
        self.retry_orphans()
        return e

    def accept(
        self,
        stat: NormalizedCombatStatistics,
        *,
        prospective_encounter: EncounterRecord | None = None,
        preferred_encounter_id: str | None = None,
        preferred_authoritative_final: bool = False,
    ) -> SettlementMatch:
        """Apply one server snapshot and retain a read-only matcher trace."""

        statuses_before = {
            encounter_id: encounter.settlement_status
            for encounter_id, encounter in self.encounters.items()
        }
        try:
            match = self._accept_impl(
                stat,
                prospective_encounter=prospective_encounter,
                preferred_encounter_id=preferred_encounter_id,
                preferred_authoritative_final=preferred_authoritative_final,
            )
        except BaseException as error:
            self._accept_diagnostics.append(
                self._accept_diagnostic(
                    stat,
                    SettlementMatch(None, 'unmatched', 'matcher_exception'),
                    statuses_before,
                    error=error,
                )
            )
            raise
        self._accept_diagnostics.append(
            self._accept_diagnostic(stat, match, statuses_before)
        )
        return match

    def _accept_diagnostic(
        self,
        stat: NormalizedCombatStatistics,
        match: SettlementMatch,
        statuses_before: dict[str, str],
        *,
        error: BaseException | None = None,
    ) -> dict:
        encounter = (
            self.encounters.get(match.encounter_id)
            if match.encounter_id
            else None
        )
        members = [
            {
                'id': member.id,
                'iid': member.iid,
                'name': member.name,
                'damage': member.damage,
            }
            for member in stat.members
        ]
        return {
            'received_at_ns': stat.received_at_ns,
            'source': stat.source,
            'scope': stat.scope,
            'battle_id': stat.battle_id,
            'associated_battle_id': stat.associated_battle_id,
            'stage_id': stat.stage_id,
            'stage_index': stat.stage_index,
            'is_stage_success': stat.is_stage_success,
            'member_count': len(members),
            'members': members,
            'complete': bool(members) and all(
                member['damage'] is not None for member in members
            ),
            'encounter_id': match.encounter_id,
            'confidence': match.confidence,
            'reason': match.reason,
            'settlement_status_before': (
                statuses_before.get(match.encounter_id)
                if match.encounter_id
                else None
            ),
            'settlement_status_after': (
                encounter.settlement_status if encounter is not None else None
            ),
            'error_type': type(error).__name__ if error is not None else '',
        }

    def take_accept_diagnostics(self) -> list[dict]:
        rows = self._accept_diagnostics
        self._accept_diagnostics = []
        return rows

    def _accept_impl(
        self,
        stat: NormalizedCombatStatistics,
        *,
        prospective_encounter: EncounterRecord | None = None,
        preferred_encounter_id: str | None = None,
        preferred_authoritative_final: bool = False,
    ) -> SettlementMatch:
        incoming_fingerprint = stat.fingerprint
        if stat.scope == 'ALL':
            candidates = [e for e in self.encounters.values() if e.instance_id == stat.instance_id
                          and e.server_battle_id and e.server_battle_id == stat.associated_battle_id]
            if len(candidates) == 1:
                e = candidates[0]
                if e.snapshot_fingerprints.get('ALL') != stat.fingerprint:
                    e.all_statistics = stat.to_dict()
                    e.snapshot_fingerprints['ALL'] = stat.fingerprint
                    e.revision += 1
                self.orphans.pop(stat.fingerprint, None)
                return SettlementMatch(e.local_encounter_id, 'associated', 'all_preserved_not_applied')
            match = SettlementMatch(None, 'unmatched', 'unbound_all_scope')
        else:
            candidates = list(self.encounters.values())
            if prospective_encounter is not None:
                candidates.append(prospective_encounter)
            preferred = (
                self.encounters.get(preferred_encounter_id)
                if preferred_encounter_id
                else None
            )
            if (
                preferred is not None
                and preferred.ended_at_ns is not None
                and preferred.result in ('VICTORY', 'WIPE')
                and preferred.settlement_status != 'ABANDONED'
                and preferred.self_token
                and preferred.self_token in {member.id for member in stat.members}
            ):
                # The UI may provide a provisional target when the server
                # table arrives before the next Challenge edge. That hint is
                # never sufficient by itself: validate the same identity
                # constraints used by the normal matcher so two consecutive
                # wipes cannot be assigned by receipt order.
                stat_ids = {member.id for member in stat.members}
                references = {
                    token
                    for member in stat.members
                    for mapping in (member.bear_map, member.killer_map)
                    if mapping
                    for token in mapping
                }
                battle_matches = bool(
                    stat.battle_id
                    and preferred.server_battle_id == stat.battle_id
                )
                battle_conflict = bool(
                    stat.battle_id
                    and preferred.server_battle_id
                    and preferred.server_battle_id != stat.battle_id
                )
                roster_compatible = set(preferred.roster) <= stat_ids
                # A team can remain in the same party session after every
                # teammate leaves. The next Boss's official STAGE table may
                # then contain only the local player. This is valid only for
                # the same session and only when the table is exactly local.
                if (
                    not roster_compatible
                    and preferred.party_session_id > 0
                    and stat_ids == {preferred.self_token}
                ):
                    roster_compatible = True
                stage_compatible = bool(
                    stat.stage_id is None
                    or preferred.stage_id is None
                    or preferred.stage_id == stat.stage_id
                    or preferred.boss_token in references
                ) and bool(
                    stat.stage_index is None
                    or preferred.stage_index is None
                    or preferred.stage_index == stat.stage_index
                    or preferred.boss_token in references
                )
                result_compatible = bool(
                    stat.is_stage_success is None
                    or (preferred.result == 'VICTORY') == stat.is_stage_success
                )
                authoritative_final_matches = bool(
                    preferred_authoritative_final
                    and stat.source == 'OnMsgSettlementCombatStatistics'
                    and stat.is_stage_success is True
                    and preferred.result == 'VICTORY'
                    and preferred.ended_at_ns is not None
                    and 0
                    <= stat.received_at_ns - preferred.ended_at_ns
                    <= 5_000_000_000
                )
                if (
                    result_compatible
                    and not battle_conflict
                    and (
                        authoritative_final_matches
                        or (
                            roster_compatible
                            and stage_compatible
                            and (
                                battle_matches
                                or preferred.boss_token in references
                                or (not stat.battle_id and (
                                    not references
                                    or preferred.boss_token in references
                                ))
                            )
                        )
                    )
                ):
                    match = SettlementMatch(
                        preferred.local_encounter_id,
                        'high',
                        (
                            'current_final_server_statistics'
                            if authoritative_final_matches
                            else 'current_party_server_statistics'
                        ),
                    )
                else:
                    match = SettlementMatcher.match(stat, candidates)
            else:
                match = SettlementMatcher.match(stat, candidates)
        if not match.encounter_id:
            self.orphans[stat.fingerprint] = {'statistics':stat.to_dict(), 'reason':match.reason}
            return match
        e = self.encounters[match.encounter_id]
        if match.reason == 'unique_unscoped_wipe_next_pull_stage_roster':
            stat = replace(
                stat,
                instance_id=e.instance_id,
                dungeon_id=(
                    stat.dungeon_id
                    if stat.dungeon_id is not None
                    else e.dungeon_id
                ),
                map_id=stat.map_id if stat.map_id is not None else e.map_id,
            )
        projection_match = match.reason in {
            'bound_battle_id_projection_roster',
            'unique_victory_projection_roster_boss_token',
            'unique_victory_projection_roster_entity_boss_boundary',
            'unique_victory_projection_roster_next_boss_clock',
        }
        if projection_match:
            known_tokens = set(e.roster)
            missing_members = [
                member for member in stat.members if member.id not in known_tokens
            ]
            if missing_members and all(
                is_ai_team_token(member.id)
                for member in missing_members
            ):
                e.participants_snapshot.extend({
                    'id': member.id,
                    'iid': member.iid,
                    'name': member.name,
                    'profession_id': member.profession_id,
                    'is_ai': True,
                } for member in missing_members)
        if match.reason == 'unique_victory_next_boss_boundary':
            known_tokens = set(e.roster)
            e.participants_snapshot.extend({
                'id': member.id,
                'iid': member.iid,
                'name': member.name,
                'profession_id': member.profession_id,
                'is_ai': is_ai_team_token(member.id),
            } for member in stat.members if member.id not in known_tokens)
        direct_server_match = match.reason in {
            'current_party_server_statistics',
            'current_final_server_statistics',
            'unique_boss_token_partial_roster_clock',
            'unique_previous_stage_next_challenge_boundary',
        }
        server_members = stat.members
        if direct_server_match:
            known_tokens = set(e.roster)
            # A delayed STAGE table can include someone who joined after the
            # Boss died. Keep the raw server table, but add a new participant
            # only when that row contains evidence from this battle.
            server_members = tuple(
                member for member in stat.members
                if member.id in known_tokens
                or (member.member_battle_length or 0) > 0
                or (member.damage or 0) > 0
                or (member.bear or 0) > 0
                or (member.heal or 0) > 0
            )
            e.participants_snapshot.extend({
                'id': member.id,
                'iid': member.iid,
                'name': member.name,
                'profession_id': member.profession_id,
                'is_ai': is_ai_team_token(member.id),
            } for member in server_members if member.id not in known_tokens)
        if match.reason in {
            'unique_victory_projection_roster_entity_boss_boundary',
            'unique_victory_projection_roster_next_boss_clock',
            'unique_victory_next_boss_boundary',
            'unique_final_victory_stage_roster_boundary',
        }:
            references = {
                key
                for member in stat.members
                for mapping in (member.bear_map, member.killer_map)
                if mapping
                for key in mapping
            }
            if len(references) == 1:
                e.boss_token = next(iter(references))
        if e.snapshot_fingerprints.get('STAGE') == stat.fingerprint:
            self.orphans.pop(incoming_fingerprint, None)
            self.orphans.pop(stat.fingerprint, None)
            return SettlementMatch(e.local_encounter_id, 'high', 'duplicate_ignored')
        if e.stage_statistics:
            # Without a server snapshot revision there is no safe latest-wins
            # ordering for changed totals, even if packet arrival is later.
            previous = NormalizedCombatStatistics.from_dict(e.stage_statistics)
            replaces_misbound_historical = bool(
                previous.battle_id is None
                and previous.associated_battle_id
                and stat.battle_id == previous.associated_battle_id
                and match.reason == 'unique_victory_adjacent_stage_roster'
                and previous.stage_id != stat.stage_id
            )
            if not replaces_misbound_historical:
                merged_members = []
                conflicting = False
                old_members = {m.id:m for m in previous.members}
                for member in stat.members:
                    if member.id not in old_members:
                        merged_members.append(member)
                        continue
                    old = asdict(old_members[member.id])
                    new = asdict(member)
                    for key, value in new.items():
                        if value is None:
                            continue
                        if old[key] is None:
                            old[key] = value
                        elif old[key] != value:
                            conflicting = True
                    merged_members.append(type(member)(**old))
                if conflicting:
                    conflict = SettlementMatch(None, 'unmatched', 'conflicting_snapshot_without_revision')
                    self.orphans[stat.fingerprint] = {'statistics':stat.to_dict(), 'reason':conflict.reason}
                    return conflict
                # Fill previously absent fields, never add cumulative amounts.
                # A changed known amount still requires an explicit revision.
                original_fingerprint = stat.fingerprint
                stat = replace(stat, members=tuple(merged_members))
                self.orphans.pop(original_fingerprint, None)
        if stat.battle_id:
            e.server_battle_id = stat.battle_id
        stage_identity_corrected = match.reason in {
            'unique_victory_roster_boss_token',
            'unique_victory_projection_roster_boss_token',
            'unique_victory_projection_roster_entity_boss_boundary',
            'unique_victory_projection_roster_next_boss_clock',
            'unique_victory_next_boss_boundary',
            'unique_final_victory_stage_roster_boundary',
            'bound_battle_id_projection_roster',
            'unique_victory_adjacent_stage_roster',
            'unique_wipe_adjacent_stage_roster',
            'unique_wipe_next_pull_stage_roster',
            'unique_wipe_unbound_projection_next_pull',
            'unique_unscoped_wipe_next_pull_stage_roster',
            'unique_previous_stage_next_challenge_boundary',
            'current_party_server_statistics',
            'current_final_server_statistics',
            'unique_boss_token_partial_roster_clock',
        }
        if stage_identity_corrected or e.stage_id is None:
            e.stage_id = stat.stage_id
        if stage_identity_corrected or e.stage_index is None:
            e.stage_index = stat.stage_index
        e.stage_statistics = stat.to_dict()
        e.settlement_received_at_ns = stat.received_at_ns
        e.match_confidence = match.confidence
        e.snapshot_fingerprints['STAGE'] = stat.fingerprint
        complete = all(m.damage is not None for m in server_members)
        e.settlement_status = 'SETTLED' if complete else 'PENDING'
        if (
            complete
            and match.confidence == 'high'
            and stat.scope == 'STAGE'
        ):
            # Once a complete server STAGE table is matched with high
            # confidence, its shared member clock supersedes the local live
            # estimate for every result type.  This applies equally to a
            # delayed wipe, an intermediate Boss, and a directly returned
            # final settlement.  Damage and duration are installed together
            # so the displayed DPS can never mix official totals with a local
            # denominator.
            from combat_history import authoritative_team_combat_seconds

            server_seconds, clock_policy, member_seconds = (
                authoritative_team_combat_seconds([
                    {
                        'combat_seconds_total': member.member_battle_length,
                    }
                    for member in server_members
                ])
            )
            local_seconds = 0.0
            if e.ended_at_ns is not None and e.started_at_ns > 0:
                local_seconds = max(
                    0.0,
                    (int(e.ended_at_ns) - int(e.started_at_ns)) / 1_000_000_000,
                )
            # Field 19 is not always a shared encounter clock. Some captures
            # contain short per-member combat segments such as [37, 5, 8, 2]
            # even though the local Boss interval lasted several minutes.
            # Reject that shorter table clock instead of publishing false DPS.
            clock_is_shorter_than_local = bool(
                server_seconds > 0
                and local_seconds > 0
                and server_seconds + max(5.0, local_seconds * 0.20)
                < local_seconds
            )
            if server_seconds > 0 and not clock_is_shorter_than_local:
                e.duration_evidence = {
                    'source': 'server_encounter_clock',
                    'seconds': server_seconds,
                    'verified': True,
                    'local_encounter_id': e.local_encounter_id,
                    'battle_id': stat.battle_id or stat.associated_battle_id,
                    'statistics_source': stat.source,
                    'clock_policy': clock_policy,
                    'member_seconds': member_seconds,
                }
            elif clock_is_shorter_than_local:
                e.duration_evidence = None
        if (
            complete
            and e.duration_evidence is None
            and e.ended_at_ns is not None
            and e.started_at_ns > 0
        ):
            local_seconds = max(
                0.0,
                (int(e.ended_at_ns) - int(e.started_at_ns)) / 1_000_000_000,
            )
            if local_seconds > 0:
                e.duration_evidence = {
                    'source': 'local_encounter_endpoints',
                    'seconds': local_seconds,
                    'verified': True,
                    'local_encounter_id': e.local_encounter_id,
                    'reason': 'server_member_clock_shorter_than_local_interval',
                }
        duration = EncounterDurationResolver.resolve(e)
        e.encounter_duration_seconds, e.duration_source = duration.seconds, duration.source
        roster = e.roster
        for member in server_members:
            identity = roster[member.id]
            if (
                member.iid is not None
                and (direct_server_match or not identity.get('iid'))
            ):
                identity['iid'] = member.iid
            if not str(identity.get('name') or '').strip() and member.name:
                identity['name'] = member.name
            if not identity.get('profession_id') and member.profession_id:
                identity['profession_id'] = member.profession_id
        e.participants = [dict(e.roster[m.id], damage=m.damage,
            dps=m.damage/duration.seconds if m.damage is not None and duration.seconds else None,
            bear=m.bear, heal=m.heal, member_battle_length=m.member_battle_length,
            damage_count=m.damage_count,
            damage_critical_count=m.damage_critical_count,
            penetration_excluded_count=m.penetration_excluded_count,
            skill_damage=deepcopy(m.skill_damage), skill_count=deepcopy(m.skill_count), skill_heal=deepcopy(m.skill_heal),
            bear_map=deepcopy(m.bear_map), killer_map=deepcopy(m.killer_map),
            unclassified_damage=m.unclassified_damage, skills_exceed_total=m.skills_exceed_total,
            settlement_hint=('延迟补齐' if e.result == 'WIPE' else '已结算') if complete else '等待结算')
            for m in server_members]
        e.revision += 1
        self.orphans.pop(incoming_fingerprint, None)
        self.orphans.pop(stat.fingerprint, None)
        return match

    def retry_orphans(
        self, prospective_encounter: EncounterRecord | None = None
    ) -> None:
        for stored_fingerprint, item in list(self.orphans.items()):
            stat = NormalizedCombatStatistics.from_dict(item['statistics'])
            self.accept(stat, prospective_encounter=prospective_encounter)
            if stored_fingerprint != stat.fingerprint:
                # Normalization rules can become more precise across versions
                # (for example an explicit empty healer damage table proving
                # zero). Keep only the canonical fingerprint after migration.
                self.orphans.pop(stored_fingerprint, None)

    def leave_instance(self, instance_id: str, at_ns: int) -> None:
        if self.current_id and self.encounters[self.current_id].instance_id == instance_id:
            self.end('RESET', at_ns)

    def to_dict(self) -> dict:
        return {'schema_version':1,'encounters':[e.to_dict() for e in self.encounters.values()],
                'current_id':self.current_id,'orphans':deepcopy(self.orphans)}

    @classmethod
    def from_dict(cls, data: dict) -> 'EncounterTracker':
        if data.get('schema_version') != 1:
            raise ValueError('Unknown encounter journal schema')
        tracker = cls()
        for row in data['encounters']:
            e = EncounterRecord.from_dict(row)
            # Older builds abandoned every pending record on scene exit. A
            # completed win/wipe can still receive its server table later (or
            # already have that table persisted as an orphan), so migrate only
            # those independently ended encounters back to retryable state.
            if (
                e.settlement_status == 'ABANDONED'
                and e.result in ('WIPE', 'VICTORY')
                and e.ended_at_ns is not None
            ):
                e.settlement_status = 'PENDING'
                for member in e.participants:
                    member['settlement_hint'] = '等待结算'
                e.revision += 1
            if e.local_encounter_id in tracker.encounters:
                raise ValueError('Duplicate encounter in journal')
            tracker.encounters[e.local_encounter_id] = e
        tracker.current_id = data.get('current_id')
        if tracker.current_id and tracker.current_id not in tracker.encounters:
            raise ValueError('Missing live encounter in journal')
        tracker.orphans = deepcopy(data.get('orphans', {}))
        return tracker
