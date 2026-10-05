"""Adapt passive wire observations to the existing parser's input contract.

The shared parser and combat model keep their original rules. This adapter
supplies authoritative local identity and translates capture latency without
pretending it is the age of a deferred game-memory argument read.
"""
import math
from collections import deque
from npcap_wire_entities import SPACE_CLASSES
from network_state import (
    NetworkPacketParser, TEAM_SELF_PROPS_METHOD, is_player_skill,
    normalize_network_skill_id, parse_exact_combat_entity_id, is_ai_team_token,
    SCENE_TRANSITION_METHODS, MAX_PARTY_MEMBERS, plausible_name,
)


# These callbacks are routed through the local-role capture channel, but the
# wire ``network_entity_id`` is not guaranteed to be the local avatar.  In
# particular, live 1v1 captures report the opponent entity for
# ``IndividualPVPState/Result``.  They are control messages, not combat
# events, so the local-role scope is the authoritative ownership signal.
PVP_LOCAL_ROLE_CONTROL_METHODS = frozenset({
    "OnMsgSyncBattleType",
    "OnMsgIndividualPVP",
    "RetIndividualPVP",
    "OnMsgIndividualPVPResponse",
    "OnMsgIndividualPVPState",
    "OnMsgIndividualPVPResult",
    "OnMsgIndividualPVPLeave",
    "OnMsgAddFightRelationship",
    "OnMsgDelFightRelationship",
    "OnMsgSyncTeamPVPInfo",
    "OnMsgTeamPVPSettlement",
    "RetGetTeamArenaBattleInfo",
})


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
        self.completed_settlement_observations = deque()
        self.completed_pvp_observations = deque(maxlen=2048)
        self.pvp_observation_errors = 0
        self.pending_scoped_bytes = 0
        self.scoped_records_dropped = 0
        self.wire_instance_id = None
        self.wire_map_id = None
        self.wire_entity_tokens = {}
        self.wire_classes = {}
        # The server can create the next scene's local AvatarActor a fraction
        # of a second before OnMsgBeforeEnterNewSpace. Scene reset must clear
        # general entity ownership, but retain this bounded token candidate
        # long enough for the following local-scope RetNTP to prove it.
        self.recent_avatar_tokens = {}
        self.settlement_boss_observation_keys = set()
        # Capture can recover in the middle of a pull.  In that case the
        # wire carries changing CurrentHP before the read-only template reader
        # has identified the entity as a Boss.  Keep that evidence detached
        # from the combat model until identity is proven; a mere HP sample is
        # never enough to promote an entity or start an encounter.
        self.unconfirmed_hp_activity = {}
        self.boss_hp_activity_observation_keys = set()

    def apply_authoritative_local_profile(self, record):
        """Bind a log-proven current local role to this parser epoch.

        ``PassiveTeamProfilePoller`` can observe an account switch before a
        routed network message exposes the new recipient token.  This record
        contains the exact current MainPlayer token/actor pair plus a live,
        token-bound profile.  It is read-only evidence, not a synthetic game
        packet, and must replace rather than merge with the previous role.
        """

        if not isinstance(record, dict) or record.get(
            "local_role_confirmed"
        ) is not True:
            return []
        token = self._team_token(record.get("user_token"))
        actor_id = parse_exact_combat_entity_id(record.get("entity_id"))
        if not token or is_ai_team_token(token) or actor_id is None:
            return []

        updates = []
        current_token = str(self.self_token or "")
        current_actor = int(self.native_self_id or self.self_id or 0)
        # A runtime actor ID can legitimately change when the same character
        # enters another space.  Only a different token (or an anonymous
        # parser already attached to another positive actor) is a character
        # boundary; the same token must use the normal actor-rebind path.
        role_changed = bool(
            (current_token and current_token != token)
            or (
                not current_token
                and current_actor > 0
                and current_actor != actor_id
            )
        )
        if role_changed:
            reset_update, _identity_hint = self._reset_local_role_session(
                actor_id,
                record,
                authoritative_self_token=token,
                reason="log_proven_local_role_changed",
            )
            updates.append(reset_update)

        # Confirm the actor before the token so _confirm_self_token binds the
        # exact positive runtime entity rather than a provisional party row.
        updates.extend(
            super()._confirm_local_actor(actor_id, record, native=True)
        )
        self.live_team_profile_tokens.add(token)
        updates.extend(self._confirm_self_token(token, record))
        updates.extend(self.apply_read_only_team_profile(record))
        return updates

    def apply_read_only_group_roster(self, record):
        """Restore the exact current party missed by a late startup."""

        if not isinstance(record, dict) or record.get("method") != "ReadOnlyCurrentDungeonRoster":
            return []
        local_token = self._team_token(record.get("local_user_token"))
        if not local_token or local_token != self.self_token or not self.self_confirmed:
            return []
        try:
            dungeon_id = int(record.get("dungeon_id", 0) or 0)
            group_id = int(record.get("group_id", 0) or 0)
        except (TypeError, ValueError, OverflowError):
            return []
        members = record.get("members")
        if (
            dungeon_id < 0 or group_id <= 0
            or not isinstance(members, list)
            or not 1 <= len(members) <= MAX_PARTY_MEMBERS
        ):
            return []
        profiles = []
        seen = set()
        for member in members:
            if not isinstance(member, dict):
                return []
            token = self._team_token(member.get("user_token"))
            name = plausible_name(member.get("name"))
            if not token or not name or token in seen:
                return []
            seen.add(token)
            fields = [[2, token], [5, name]]
            for source, field in (
                ("role_number", 6),
                ("profession_id", 8),
                ("level", 9),
                ("extraordinary_rating", 27),
            ):
                try:
                    value = int(member.get(source, 0) or 0)
                except (TypeError, ValueError, OverflowError):
                    value = 0
                if value > 0:
                    fields.append([field, value])
            profiles.append({"$map": fields})
        if local_token not in seen:
            return []
        source = dict(record, method="OnJoinGroupSuccess", npcap_synthetic=True)
        return self._team_join_success_updates(source, [profiles])

    def _reset_local_role_session(
        self,
        actor_id,
        record,
        *,
        authoritative_self_token="",
        reason="authoritative_local_actor_changed",
    ):
        """Start a clean parser epoch when the authoritative local actor changes.

        A character switch does not necessarily replace the game process or the
        Npcap capture session. Rebinding the previous self token to the new
        positive actor makes the old character both ``self`` and a party ghost.
        Recreate all role/scene/combat state in place, retaining only immutable
        catalogs and the profile store used to recognise live tokens.
        """
        previous_actor_id = int(self.native_self_id or self.self_id or 0)
        previous_self_token = str(self.self_token or "")
        authoritative_self_token = self._team_token(authoritative_self_token)
        hinted_token = str(
            authoritative_self_token
            or self.actor_tokens.get(int(actor_id), "")
            or ""
        )
        if hinted_token == previous_self_token or is_ai_team_token(hinted_token):
            hinted_token = ""

        profile_cache = dict(self.team_profile_cache)
        profile_cache_dirty = bool(self.team_profile_cache_dirty)
        target_catalog = {
            str(template_id): dict(metadata)
            for template_id, metadata in self.target_identity_catalog.items()
            if isinstance(metadata, dict)
        }
        boss_catalog = target_catalog if self.boss_template_catalog_enabled else None
        boss_name_allowlist = tuple(self.boss_name_allowlist)
        completed_observations = tuple(self.completed_settlement_observations)
        hinted_token_was_live = bool(
            hinted_token and hinted_token in self.live_team_profile_tokens
        )
        hinted_live_rating = self.live_team_property_ratings.get(hinted_token)

        # The adapter constructor also clears queued scoped packets, wire
        # identities and Boss/dummy state from the previous character.
        self.__init__(
            profile_cache,
            boss_catalog,
            target_identity_catalog=target_catalog,
            remembered_self_token="",
            allow_cached_projection_roster=False,
            boss_name_allowlist=boss_name_allowlist,
        )
        self.team_profile_cache_dirty = profile_cache_dirty
        self.completed_settlement_observations.extend(completed_observations)
        if hinted_token_was_live:
            self.live_team_profile_tokens.add(hinted_token)
        if hinted_live_rating is not None:
            self.live_team_property_ratings[hinted_token] = hinted_live_rating

        return (
            "identity_session_reset",
            {
                **self._base_update(record),
                "capture_timestamp_ns": int(
                    record.get("capture_timestamp_ns", 0) or 0
                ),
                "previous_entity_id": previous_actor_id,
                "entity_id": int(actor_id),
                "previous_user_token": previous_self_token,
                "user_token": hinted_token,
                "reason": str(reason or "local_role_session_changed"),
                "identity_hint_available": bool(hinted_token),
            },
        ), hinted_token

    def _ensure_bootstrap_instance(self, entity_id, entity_token=None):
        """Create a stable passive scene key when capture starts mid-instance.

        DungeonSpace creation is only sent while entering a scene.  Starting
        the meter after the party is already inside therefore has no space
        record to consume, even though the verified Boss entity and all fight
        mode/life records are available.  Use that first verified Boss as the
        local scene anchor until a real DungeonSpace token arrives.
        """
        try:
            entity_id = int(entity_id or 0)
        except (TypeError, ValueError, OverflowError):
            return
        if not entity_id:
            return
        token = str(entity_token or self.wire_entity_tokens.get(entity_id) or '').strip()
        if not token:
            token = f'entity:{entity_id}'
        self.wire_entity_tokens.setdefault(entity_id, token)
        if self.wire_instance_id is None:
            self.wire_instance_id = f'npcap-bootstrap:{entity_id}'

    def _freeze_settlement_observation(self, record, updates):
        # Scoped records can be parsed well after their capture batch. Freeze
        # the detached settlement view here, while parser state still belongs
        # to the record that was actually processed.
        from settlement_ui_controller import parser_observation

        observation = parser_observation(self, record, updates)
        if observation is not None:
            self.completed_settlement_observations.append(observation)

    def _freeze_pvp_observation(self, record, updates):
        from pvp_tracker import parser_observation

        try:
            observation = parser_observation(self, record, updates)
        except (TypeError, ValueError, OverflowError, KeyError):
            # Optional PVP enrichment must never break the formal PVE parser.
            self.pvp_observation_errors += 1
            return
        if observation is not None:
            if len(self.completed_pvp_observations) == self.completed_pvp_observations.maxlen:
                observation['capture_gap'] = True
            self.completed_pvp_observations.append(observation)

    def take_pvp_observations(self):
        observations = list(self.completed_pvp_observations)
        self.completed_pvp_observations.clear()
        return observations

    @staticmethod
    def _record_timestamp_ns(record):
        try:
            stamp = int(record.get("capture_timestamp_ns", 0) or 0)
        except (TypeError, ValueError, OverflowError):
            stamp = 0
        if stamp > 0:
            return stamp
        try:
            filetime = int(record.get("filetime_100ns", 0) or 0)
        except (TypeError, ValueError, OverflowError):
            return 0
        return max(0, (filetime - 116_444_736_000_000_000) * 100)

    @staticmethod
    def _record_flow_id(record):
        event_id = str(record.get("capture_event_id", "") or "")
        return event_id.split(":", 1)[0] if event_id else ""

    def _remember_avatar_token(self, entity_id, token, record):
        timestamp_ns = self._record_timestamp_ns(record)
        self.recent_avatar_tokens[int(entity_id)] = (
            str(token or ""),
            timestamp_ns,
            self._record_flow_id(record),
        )
        while len(self.recent_avatar_tokens) > 256:
            self.recent_avatar_tokens.pop(
                next(iter(self.recent_avatar_tokens)), None
            )

    def _recent_avatar_token(self, entity_id, record):
        candidate = self.recent_avatar_tokens.get(int(entity_id))
        if not candidate:
            return ""
        token, observed_ns, observed_flow = candidate
        current_ns = self._record_timestamp_ns(record)
        current_flow = self._record_flow_id(record)
        if (
            observed_ns <= 0
            or current_ns <= 0
            or current_ns < observed_ns
            or current_ns - observed_ns > 15_000_000_000
            or (observed_flow and current_flow and observed_flow != current_flow)
        ):
            self.recent_avatar_tokens.pop(int(entity_id), None)
            return ""
        return str(token or "")

    def _remember_unconfirmed_hp_activity(self, record):
        """Remember only a positive, explicitly decreasing HP sequence.

        Player and party HP packets are common, so observations remain inert
        and bounded.  They are consumed only if this exact entity is later
        promoted by verified Boss template metadata in the same scene epoch.
        """

        if record.get("method") != "OnMsgSyncCurrentHp":
            return
        args = record.get("decoded_arguments")
        if not isinstance(args, list) or len(args) != 1:
            return
        try:
            entity_id = int(record.get("network_entity_id", 0) or 0)
            hp = float(args[0])
        except (TypeError, ValueError, OverflowError):
            return
        if (
            entity_id <= 0
            or not math.isfinite(hp)
            or hp <= 0.0
            or entity_id == self.self_id
            or entity_id == self.native_self_id
            or entity_id in self.party_ids
            or entity_id in self.actor_tokens
            or entity_id in self.training_dummy_entities
            or entity_id in self.confirmed_boss_entities
        ):
            return
        stamp = self._record_timestamp_ns(record)
        if stamp <= 0:
            return

        # Bound unknown-entity state even in a long crowded capture.  Scene
        # transitions clear the whole mapping; this cap handles a scene that
        # itself creates many short-lived NPCs.
        if entity_id not in self.unconfirmed_hp_activity:
            while len(self.unconfirmed_hp_activity) >= 256:
                self.unconfirmed_hp_activity.pop(
                    next(iter(self.unconfirmed_hp_activity)), None
                )
            self.unconfirmed_hp_activity[entity_id] = {
                "first_timestamp_ns": stamp,
                "first_filetime_100ns": int(
                    record.get("filetime_100ns", 0) or 0
                ),
                "first_hp": hp,
                "maximum_hp": hp,
                "latest_hp": hp,
                "sample_count": 1,
                "declined": False,
            }
            return

        activity = self.unconfirmed_hp_activity[entity_id]
        maximum = max(float(activity.get("maximum_hp", hp) or hp), hp)
        decline_floor = max(1.0, maximum * 0.000001)
        if hp < maximum - decline_floor:
            activity["declined"] = True
        activity["maximum_hp"] = maximum
        activity["latest_hp"] = hp
        activity["sample_count"] = int(activity.get("sample_count", 0)) + 1

    def _confirmed_boss_hp_activity_updates(self, entity_id, record, token):
        activity = self.unconfirmed_hp_activity.pop(int(entity_id), None)
        if not isinstance(activity, dict):
            return []
        if (
            activity.get("declined") is not True
            or int(activity.get("sample_count", 0) or 0) < 2
        ):
            return []
        start_stamp = int(activity.get("first_timestamp_ns", 0) or 0)
        confirmation_stamp = self._record_timestamp_ns(record)
        # The metadata reader records the time of its read, which can precede
        # delivery to the parser by a fraction of a second.  Absolute distance
        # is therefore intentional; process order plus the scene epoch still
        # proves that the HP sequence was seen before promotion.
        if (
            start_stamp <= 0
            or confirmation_stamp <= 0
            or abs(confirmation_stamp - start_stamp) > 120_000_000_000
        ):
            return []
        key = (
            str(self.wire_instance_id or ""),
            int(entity_id),
            str(token or ""),
        )
        if key in self.boss_hp_activity_observation_keys:
            return []
        self.boss_hp_activity_observation_keys.add(key)

        start_filetime = int(activity.get("first_filetime_100ns", 0) or 0)
        if start_filetime <= 0:
            start_filetime = start_stamp // 100 + 116_444_736_000_000_000
        synthetic = {
            **record,
            "method": "NpcapBossHpActivity",
            "decoded_arguments": [],
            "capture_timestamp_ns": start_stamp,
            "filetime_100ns": start_filetime,
            "network_entity_id": int(entity_id),
            "script_entity": int(entity_id),
            "npcap_synthetic": True,
            "npcap_confirmation_source_method": str(
                record.get("method", "") or record.get("function", "") or ""
            ),
            "first_observed_hp": float(activity.get("first_hp", 0.0) or 0.0),
            "latest_observed_hp": float(activity.get("latest_hp", 0.0) or 0.0),
            "hp_sample_count": int(activity.get("sample_count", 0) or 0),
        }
        update = (
            "combat_state",
            {
                **self._base_update(synthetic),
                "entity_id": int(entity_id),
                "in_combat": True,
                "passive_hp_activity": True,
            },
        )
        self._freeze_settlement_observation(synthetic, [update])
        return [update]

    def _freeze_new_boss_confirmations(self, record, previously_confirmed):
        """Emit one boundary when an ordinary packet first proves a Boss.

        Fight-mode can arrive before the damage/template evidence which
        promotes its entity to ``confirmed_boss_entities``.  Damage packets
        are intentionally not settlement observations themselves, so expose
        only the promotion edge and let the UI controller replay the already
        captured start signal at its original timestamp.
        """
        newly_confirmed = set(self.confirmed_boss_entities) - set(
            previously_confirmed
        )
        updates = []
        for entity_id in sorted(newly_confirmed):
            token = self.wire_entity_tokens.get(entity_id)
            self._ensure_bootstrap_instance(entity_id, token)
            token = self.wire_entity_tokens.get(entity_id)
            updates.extend(
                self._confirmed_boss_hp_activity_updates(
                    entity_id, record, token
                )
            )
            key = (
                str(self.wire_instance_id or ""),
                int(entity_id),
                str(token or ""),
            )
            if key in self.settlement_boss_observation_keys:
                continue
            self.settlement_boss_observation_keys.add(key)
            synthetic = {
                **record,
                "method": "NpcapBossConfirmed",
                "decoded_arguments": [],
                "network_entity_id": int(entity_id),
                "script_entity": int(entity_id),
                "npcap_synthetic": True,
                "npcap_confirmation_source_method": str(
                    record.get("method", "") or ""
                ),
            }
            self._freeze_settlement_observation(synthetic, [])
        return updates

    def take_settlement_observations(self):
        observations = list(self.completed_settlement_observations)
        self.completed_settlement_observations.clear()
        return observations

    def _wire_creation(self, record):
        value = record['decoded_arguments'][0]
        entity, cls, token = value['entity_id'], value['entity_class'], value['entity_token']
        self.wire_entity_tokens[entity] = token
        self.wire_classes[entity] = cls
        props = value['properties']
        updates = []
        if cls in SPACE_CLASSES or (
            record.get('capture_source') == 'npcap_read_only_pvp_lifecycle'
            and cls.endswith('Space')
        ):
            instance_id = props.get('WorldEntityID') or token
            map_id = props.get('TemplateID')
            changed_space = (
                self.wire_instance_id != instance_id
                or self.wire_map_id != map_id
            )
            if changed_space:
                # A full space creation is also a scene boundary when the
                # optional leave/before-enter RPC was not captured. Duplicate
                # creations in the same space must retain its current STAGE.
                self._clear_dungeon_stage_context()
                replacement = self._quarantine_old_party_on_projection_scene(record)
                if replacement is not None:
                    updates.append(replacement)
            self.wire_instance_id = instance_id
            self.wire_map_id = map_id
            try:
                self.map_id = max(0, int(map_id or 0))
            except (TypeError, ValueError, OverflowError):
                self.map_id = 0
            if 'ApplyTemplateID' in props:
                self.dungeon_id = props['ApplyTemplateID']
            self.dungeon_context_source = "space_creation_properties"
            self.dungeon_context_time_100ns = max(
                int(self.dungeon_context_time_100ns or 0),
                int(record.get("filetime_100ns", 0) or 0),
            )
            updates.append(('instance_context', {**self._base_update(record),
                'instance_id':self.wire_instance_id,'map_id':self.wire_map_id,
                'dungeon_id':props.get('ApplyTemplateID'),'space_token':token}))
        elif cls == 'AvatarActor':
            self._remember_avatar_token(entity, token, record)
            # BotDisplay publishes a temporary named projection before this
            # exact AvatarActor exists. Its join RPC may still be deferred by
            # local-role scope, so claim the temporary row before rebinding
            # the token; otherwise both rows briefly appear in the HUD.
            display_merge = self._claim_dungeon_bot_display_actor(
                token, entity, props.get('Name'), props.get('Profession'), record
            )
            if display_merge is not None:
                updates.append(('actor_merge', display_merge))
            updates.extend(
                self._rebind_team_actor(
                    token, entity, record, allow_positive_rebind=True
                )
            )
            profile = self._profile_update(entity, record, entity_type='Player',user_token=token,
                name=props.get('Name'),level=props.get('Level'),profession_id=props.get('Profession'))
            if profile:updates.append(profile)
        elif cls == 'NpcActor':
            # This is the existing pure parser's template promotion contract;
            # the method does not invoke a native function or install a hook.
            previously_confirmed = set(self.confirmed_boss_entities)
            updates.extend(super().process_native_boss_type({**record,'entity_id':entity,
                'template_id':props.get('TemplateID',0),'boss_type':props.get('BossType',-1),
                'boss_source':'npcap_entity_creation','level':props.get('Level'),
                'damage_target_validated':value.get('damage_target_validated') is True,
                'localization_id':value.get('localization_id'),
                'name':value.get('name'),
                'entity_token':token}))
            if entity in self.confirmed_boss_entities:
                self._ensure_bootstrap_instance(entity, token)
            updates.extend(
                self._freeze_new_boss_confirmations(
                    record, previously_confirmed
                )
            )
        self._freeze_settlement_observation(record, updates)
        self._freeze_pvp_observation(record, updates)
        updates.extend(self._drain_scoped())
        return updates

    def _scope_ready(self, record):
        # Scene boundaries are lifecycle events, not role-owned data.  A
        # transition clears ``native_self_id`` immediately; deferring another
        # transition from the same inbound batch until the next local clock
        # reply would replay an old-space reset after new-space entities have
        # already been created.  Consume every boundary at its wire timestamp.
        if record.get('method') in SCENE_TRANSITION_METHODS:
            return True
        scope = record.get('npcap_method_scope')
        entity = parse_exact_combat_entity_id(record.get('network_entity_id'))
        if scope == 'local_role':
            if self.native_self_id is None and self._is_local_clock_reply(record):
                return True
            if record.get('method') in PVP_LOCAL_ROLE_CONTROL_METHODS:
                # The capture backend has already classified this callback as
                # local-role traffic.  Some PVP server callbacks carry the
                # peer's entity as their wire owner, so requiring an exact
                # actor match here silently drops the duel result.
                return self.native_self_id is not None
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

    def _discard_scoped_before_scene_boundary(self):
        # Deferred local-role callbacks were captured in the space being left.
        # Replaying them after the next RetNTP identifies the new-space role
        # can restore the old STAGE/roster and poison a fresh encounter.
        self.scoped_records_dropped += len(self.pending_scoped_records)
        self.pending_scoped_records.clear()
        self.pending_scoped_bytes = 0

    def _drain_scoped(self):
        updates = []
        waiting = deque()
        while self.pending_scoped_records:
            record, size = self.pending_scoped_records.popleft()
            if self._scope_ready(record):
                self.pending_scoped_bytes -= size
                self._prepare_wire_owner(record)
                previously_confirmed = set(self.confirmed_boss_entities)
                record_updates = super().process(record)
                self._freeze_settlement_observation(record, record_updates)
                if record.get('method') not in ('OnMsgEntityDead', 'OnMsgEntityRelive'):
                    self._freeze_pvp_observation(record, record_updates)
                record_updates.extend(
                    self._freeze_new_boss_confirmations(
                        record, previously_confirmed
                    )
                )
                updates.extend(record_updates)
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
        args = self._args(record)
        try:
            fight_mode = int(args[0]) if args else -1
        except (TypeError, ValueError, OverflowError):
            fight_mode = -1
        if (
            entity
            and pointer == entity
            and record.get('method') == 'OnMsgSyncFightMode'
            and fight_mode == 2
            and self.wire_classes.get(entity) == 'NpcActor'
            and entity in self.confirmed_boss_entities
        ):
            # The wire owner is the exact catalog-confirmed Boss entity.  Its
            # formal fight start may be the first safe boundary after the
            # previous Boss reported FightMode[0] while retaining stale HP.
            # ``_activate_boss`` still rejects live simultaneous Bosses and
            # untrusted catalog entries; this only supplies the corroborating
            # start signal that metadata creation alone deliberately lacks.
            self._activate_boss(
                entity,
                record,
                corroborated_signal=True,
            )
        if entity and pointer == entity and entity == self.active_boss_entity_id:
            # The original parser expects a verified ScriptEntity owner for
            # HP/death. On the wire that owner is the exact entity ID, not the
            # CommonComponent address used only to initialize its template.
            self.pointer_entities[pointer] = entity
            self.active_boss_pointer = pointer
            self.active_boss_pointer_time_100ns = int(record.get('filetime_100ns', 0))

    def process_native_boss_type(self, record):
        previously_confirmed = set(self.confirmed_boss_entities)
        updates = super().process_native_boss_type(record)
        try:
            entity_id = int(record.get('entity_id', 0) or 0)
        except (TypeError, ValueError, OverflowError):
            entity_id = 0
        if entity_id in self.confirmed_boss_entities:
            self._ensure_bootstrap_instance(
                entity_id, record.get('entity_token')
            )
        self._freeze_settlement_observation(record, updates)
        updates.extend(
            self._freeze_new_boss_confirmations(record, previously_confirmed)
        )
        updates.extend(self._drain_scoped())
        return updates

    def process_native_damage(self, record, *, include_damage=True):
        updates = super().process_native_damage(
            record, include_damage=include_damage
        )
        observation_record = {
            **record,
            'method': 'KAPI_HandleDamageSyncV2',
            'capture_source': str(
                record.get('capture_source') or 'native_damage'
            ),
        }
        self._freeze_pvp_observation(observation_record, updates)
        return updates

    def _confirm_local_actor(self, actor_id, record, *, native=False):
        # Existing parser calls with native=False use skill/time correlation.
        # The wire source instead supplies the explicit local-only recipient.
        if (str(record.get('capture_source', '')).casefold() == 'npcap'
                and not native and actor_id != self.native_self_id):
            return []
        updates = []
        identity_hint = ""
        npcap_native = bool(
            native
            and str(record.get('capture_source', '')).casefold() == 'npcap'
        )
        wire_token_hint = ""
        if npcap_native:
            wire_token_hint = self._team_token(
                self.wire_entity_tokens.get(int(actor_id), "")
                or self._recent_avatar_token(actor_id, record)
                or self.actor_tokens.get(int(actor_id), "")
            )
            if is_ai_team_token(wire_token_hint):
                wire_token_hint = ""
        current_self_token = str(self.self_token or "")
        native_actor_changed = bool(
            npcap_native
            and self.native_self_id is not None
            and int(self.native_self_id) > 0
            and int(actor_id) != int(self.native_self_id)
        )
        token_proves_same_role = bool(
            current_self_token and wire_token_hint == current_self_token
        )
        token_proves_new_role = bool(
            current_self_token
            and wire_token_hint
            and wire_token_hint != current_self_token
        )
        if (
            token_proves_new_role
            or (native_actor_changed and not token_proves_same_role)
        ):
            reset_update, identity_hint = self._reset_local_role_session(
                actor_id,
                record,
                authoritative_self_token=(
                    wire_token_hint if token_proves_new_role else ""
                ),
                reason=(
                    "wire_local_token_changed"
                    if token_proves_new_role
                    else "authoritative_local_actor_changed"
                ),
            )
            updates.append(reset_update)
        updates.extend(
            super()._confirm_local_actor(actor_id, record, native=native)
        )
        if identity_hint and self.self_token is None:
            # This exact token -> actor mapping was already proven while the
            # new local role was a teammate. It can identify the new role
            # immediately without retaining the old self or roster state. Any
            # genuinely live rating was preserved by the session reset; never
            # promote the disk-cached score merely because identity matched.
            self.live_team_profile_tokens.add(identity_hint)
            updates.extend(self._confirm_self_token(identity_hint, record))
        if wire_token_hint and self.self_token is None:
            # AvatarActor creation binds an exact token to an entity, and a
            # local-scope RetNTP proves that same entity is this client.  This
            # pair is sufficient to identify the role immediately; waiting for
            # the slower read-only score poll made startup/account switching
            # stall for tens of seconds.  It does not prove a current score, so
            # keep rating discovery on its independent live-only path.
            self.other_party_tokens.discard(wire_token_hint)
            self.live_team_profile_tokens.add(wire_token_hint)
            updates.extend(self._confirm_self_token(wire_token_hint, record))
        # DpsWorker only supplies remembered_self_token when it belongs to the
        # exact game PID that this parser is currently capturing.  A local-only
        # Npcap reply proves the actor ID, so bind that same-process token now;
        # late-start captures do not necessarily receive another roster/name
        # packet from which the base parser could rediscover it.  The profile
        # restore deliberately excludes the disk-cached equipment rating.
        if (
            native
            and str(record.get('capture_source', '')).casefold() == 'npcap'
            and self.self_token is None
            and self.remembered_self_token
        ):
            updates.extend(
                self._confirm_self_token(self.remembered_self_token, record)
            )
        return updates

    def process(self, record, *, include_damage=True):
        record = normalize_npcap_record(record)
        # The affected player can be an enemy, not the local role/party. Keep
        # this passive observation even when the PVE scoped gate rejects it.
        if record.get('method') in ('OnMsgEntityDead', 'OnMsgEntityRelive'):
            self._freeze_pvp_observation(record, [])
        if record.get('method') in SCENE_TRANSITION_METHODS:
            # Wire creation IDs are scoped to the space being left.  A missing
            # BeforeEnterNewSpace packet must not let the next instance inherit
            # the old bootstrap/space key or stale Boss entity tokens.
            self.wire_instance_id = None
            self.wire_map_id = None
            self.wire_entity_tokens.clear()
            self.wire_classes.clear()
            self.settlement_boss_observation_keys.clear()
            self.unconfirmed_hp_activity.clear()
            self.boss_hp_activity_observation_keys.clear()
            self._discard_scoped_before_scene_boundary()
        if record.get('method') == 'NpcapEntityCreated' and record.get('capture_source') in (
            'npcap', 'npcap_read_only_pvp_lifecycle',
        ):
            return self._wire_creation(record)
        if not self._scope_ready(record):
            self._queue_scoped(record)
            return []
        updates = []
        if str(record.get('capture_source', '')).casefold() == 'npcap':
            self._remember_unconfirmed_hp_activity(record)
            args = self._args(record)
            method = record.get('method')
            if method == 'OnSyncTeamGroupPropsForceRefresh':
                # The named RPC is routed to the local role token; args[0]
                # identifies the remote member whose health is being updated.
                # They must not be confused, especially with 11 projections.
                recipient = self._team_token(record.get('npcap_recipient'))
                if recipient and not is_ai_team_token(recipient):
                    current_self_token = str(self.self_token or "")
                    if current_self_token and recipient != current_self_token:
                        # Character changes can reuse the same positive actor
                        # ID inside one game process.  The routed recipient is
                        # the authoritative local-role identity, so an exact
                        # token change must start a new parser epoch even when
                        # the actor number did not change.  Otherwise the old
                        # character and roster survive as ghosts and the next
                        # server STAGE table cannot match its Encounter.
                        local_actor = int(
                            self.native_self_id
                            or self.self_id
                            or self.token_actors.get(recipient, 0)
                            or 0
                        )
                        if local_actor > 0:
                            reset_update, identity_hint = (
                                self._reset_local_role_session(
                                    local_actor,
                                    record,
                                    authoritative_self_token=recipient,
                                    reason="authoritative_local_token_changed",
                                )
                            )
                            updates.append(reset_update)
                            updates.extend(
                                super()._confirm_local_actor(
                                    local_actor, record, native=True
                                )
                            )
                            if identity_hint:
                                self.live_team_profile_tokens.add(identity_hint)
                                updates.extend(
                                    self._confirm_self_token(
                                        identity_hint, record
                                    )
                                )
                    elif self.self_token is None:
                        self.live_team_profile_tokens.add(recipient)
                        updates.extend(
                            self._confirm_self_token(recipient, record)
                        )
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
                updates.extend(self._confirm_local_actor(entity, record, native=True))
                profile = self._profile_update(entity, record, entity_type='Player', is_ai=False)
                if profile:
                    updates.append(profile)
        self._prepare_wire_owner(record)
        previously_confirmed = set(self.confirmed_boss_entities)
        updates.extend(super().process(record, include_damage=include_damage))
        self._freeze_settlement_observation(record, updates)
        if record.get('method') not in ('OnMsgEntityDead', 'OnMsgEntityRelive'):
            self._freeze_pvp_observation(record, updates)
        updates.extend(
            self._freeze_new_boss_confirmations(record, previously_confirmed)
        )
        updates.extend(self._drain_scoped())
        return updates
