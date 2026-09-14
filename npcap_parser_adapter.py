"""Adapt passive wire observations to the existing parser's input contract.

The shared parser and combat model keep their original rules. This adapter
supplies authoritative local identity and translates capture latency without
pretending it is the age of a deferred game-memory argument read.
"""
from collections import deque
from network_state import (
    NetworkPacketParser, TEAM_SELF_PROPS_METHOD, is_player_skill,
    normalize_network_skill_id, parse_exact_combat_entity_id, is_ai_team_token,
)


def normalize_npcap_record(record):
    if (str(record.get('capture_source', '')).casefold() != 'npcap'
            or record.get('arguments_synchronized') is not True):
        return record
    adapted = dict(record)
    adapted.setdefault('capture_decode_latency_ms', record.get('decode_delay_ms', 0.0))
    adapted['decode_delay_ms'] = 0.0
    return adapted


class NpcapParserAdapter(NetworkPacketParser):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.pending_scoped_records = deque()
        self.pending_scoped_bytes = 0
        self.scoped_records_dropped = 0

    def _scope_ready(self, record):
        scope = record.get('npcap_method_scope')
        entity = parse_exact_combat_entity_id(record.get('network_entity_id'))
        if scope == 'local_role':
            if self.native_self_id is None and self._is_local_clock_reply(record):
                return True
            return self.native_self_id is not None and entity == self.native_self_id
        if scope == 'player_entity':
            return bool(entity and (entity == self.native_self_id or self._is_known_player_actor(entity, int(record.get('filetime_100ns', 0)))))
        if scope == 'npc':
            return bool(entity and self._is_confirmed_non_player_actor(entity))
        return True

    @staticmethod
    def _is_local_clock_reply(record):
        args = record.get('decoded_arguments')
        return bool(record.get('method') == 'RetNTP' and isinstance(args, list)
                    and len(args) == 2 and all(isinstance(value, int) and not isinstance(value, bool)
                    and 1_000_000_000_000 < value < 10_000_000_000_000 for value in args)
                    and abs(args[0]-args[1]) < 120_000)

    def _queue_scoped(self, record):
        size = max(1, int(record.get('npcap_message_bytes', 1024)))
        if size > 1024*1024:
            self.scoped_records_dropped += 1
            return
        self.pending_scoped_records.append((record, size))
        self.pending_scoped_bytes += size
        while len(self.pending_scoped_records) > 128 or self.pending_scoped_bytes > 1024*1024:
            _record, size = self.pending_scoped_records.popleft()
            self.pending_scoped_bytes -= size
            self.scoped_records_dropped += 1

    def _drain_scoped(self):
        updates = []
        waiting = deque()
        while self.pending_scoped_records:
            record, size = self.pending_scoped_records.popleft()
            if self._scope_ready(record):
                self.pending_scoped_bytes -= size
                self._prepare_wire_owner(record)
                updates.extend(super().process(record))
            elif record.get('npcap_method_scope') == 'local_role' and self.native_self_id is not None:
                self.pending_scoped_bytes -= size
            else:
                waiting.append((record, size))
        self.pending_scoped_records = waiting
        return updates

    def _prepare_wire_owner(self, record):
        if str(record.get('capture_source', '')).casefold() != 'npcap':
            return
        entity = parse_exact_combat_entity_id(record.get('network_entity_id'))
        pointer = int(record.get('script_entity', 0) or 0)
        if entity and pointer == entity and entity == self.active_boss_entity_id:
            # The original parser expects a verified ScriptEntity owner for
            # HP/death. On the wire that owner is the exact entity ID, not the
            # CommonComponent address used only to initialize its template.
            self.active_boss_pointer = pointer
            self.active_boss_pointer_time_100ns = int(record.get('filetime_100ns', 0))

    def process_native_boss_type(self, record):
        updates = super().process_native_boss_type(record)
        updates.extend(self._drain_scoped())
        return updates

    def _confirm_local_actor(self, actor_id, record, *, native=False):
        # Existing parser calls with native=False use skill/time correlation.
        # The wire source instead supplies the explicit local-only recipient.
        if (str(record.get('capture_source', '')).casefold() == 'npcap'
                and not native and actor_id != self.native_self_id):
            return []
        return super()._confirm_local_actor(actor_id, record, native=native)

    def process(self, record, *, include_damage=True):
        record = normalize_npcap_record(record)
        if not self._scope_ready(record):
            self._queue_scoped(record)
            return []
        updates = []
        if str(record.get('capture_source', '')).casefold() == 'npcap':
            args = self._args(record)
            method = record.get('method')
            if method == 'OnSyncTeamGroupPropsForceRefresh':
                # The named RPC is routed to the local role token; args[0]
                # identifies the remote member whose health is being updated.
                # They must not be confused, especially with 11 projections.
                recipient = self._team_token(record.get('npcap_recipient'))
                if recipient and not is_ai_team_token(recipient) and self.self_token is None:
                    updates.extend(self._confirm_self_token(recipient, record))
            local_reply = (
                method == TEAM_SELF_PROPS_METHOD and bool(args) and isinstance(args[0], dict)
            ) or (
                method == 'RetCastSkillSuccessNew' and bool(args)
                and is_player_skill(normalize_network_skill_id(args[0]))
            ) or self._is_local_clock_reply(record)
            entity = parse_exact_combat_entity_id(record.get('network_entity_id'))
            if local_reply and entity:
                # 'native' is the existing parser's authoritative-ID contract;
                # this calls no hook, native capture backend or game function.
                updates.extend(super()._confirm_local_actor(entity, record, native=True))
                profile = self._profile_update(entity, record, entity_type='Player', is_ai=False)
                if profile:
                    updates.append(profile)
        self._prepare_wire_owner(record)
        updates.extend(super().process(record, include_damage=include_damage))
        updates.extend(self._drain_scoped())
        return updates
