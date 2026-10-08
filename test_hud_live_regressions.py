"""Regression cases for live roster, tab scrolling, and post-combat clocks."""
import unittest
from types import SimpleNamespace
from unittest import mock

from encounter_tracker import EncounterTracker
from settlement_presenter import encounter_view
from test_main_hud_behavior import make_window, completed_record
from test_combat_model import CombatModel
from test_network_state import NetworkPacketParser, packet, SELF_TOKEN, TEAMMATE_TOKEN, PLAYER_ID


class HudLiveRegressions(unittest.TestCase):
    def window(self):
        window, members = make_window()
        window.main_last_battle_result = None
        window.main_time_text = '00:01'
        window.team_dps_text = '99,999'
        window.main_visible_rows = 4
        window.main_scroll_offset = 0
        window.pve_hud_view = 'team'
        window.window_locked = False
        window.model.entity_extraordinary_ratings = {1: 90_000, 2: 80_000}
        return window

    def ended(self, window, duration=37):
        tracker = EncounterTracker()
        record = tracker.begin(
            instance_id='instance', started_at_ns=100_000_000_000,
            participants_snapshot=[{'id': 'self', 'iid': 1, 'name': 'Self'}],
            self_token='self', boss_template_id=7109821, boss_token='boss',
        )
        tracker.end('WIPE', 142_000_000_000)
        record.settlement_status = 'SETTLED'
        record.encounter_duration_seconds = duration
        record.boss_name = 'Previous Boss'
        record.participants = [{'id': 'self', 'damage': 37000, 'dps': 1000}]
        window.settlement_ui = SimpleNamespace(tracker=tracker, visible=lambda: encounter_view(record))
        window.history_store = SimpleNamespace(load=lambda _key: None)
        window.model.started = False
        return record

    def test_footer_is_fixed_by_tab_even_without_battle_data(self):
        window = self.window()
        window.model.started = False
        team = window._layered_main_snapshot()
        self.assertEqual((team['dps_summary_caption'], team['team_dps']), ('团队超凡评分', '85,000'))
        window.pve_hud_view = 'recent_battle'
        recent = window._layered_main_snapshot()
        self.assertEqual((recent['dps_summary_caption'], recent['team_dps']), ('团队总秒伤', '--'))
        section = next(
            row for row in recent['rows'] if row.get('row_kind') == 'section'
        )
        self.assertEqual(section['live_team_dps'], '--')

    def test_team_footer_never_uses_live_damage_number(self):
        window = self.window()
        window._main_has_live_boss_team_dps = lambda: True
        team = window._layered_main_snapshot()
        self.assertEqual(team['team_dps'], '85,000')
        window.pve_hud_view = 'recent_battle'
        self.assertEqual(window._layered_main_snapshot()['team_dps'], '--')

    def test_live_team_dps_is_visible_from_the_first_active_pull(self):
        window = self.window()
        window.pve_hud_view = 'recent_battle'
        window.model.started = True
        window._main_display_rows = lambda: ([
            {'actor_id': 1, 'metric': 'dps', 'dps_value': 12_345.0},
            {'actor_id': 2, 'metric': 'dps', 'dps_value': 23_456.0},
        ], False)

        snapshot = window._layered_main_snapshot()
        section = next(
            row for row in snapshot['rows'] if row.get('row_kind') == 'section'
        )

        self.assertEqual(section['live_team_dps'], '35,801/s')
        self.assertEqual(snapshot['team_dps'], '--')

    def test_live_team_dps_falls_back_to_boss_hp_before_teammates_arrive(self):
        window = self.window()
        window.model.combat_in_progress = lambda _now=None: True
        window.model.current_bosses = lambda: [SimpleNamespace(current_hp=80)]
        window.model.current_monster = lambda: window.model.current_bosses()[0]
        window.model.observed_boss_damage_taken = lambda: 1_200_000
        window.model.display_duration = lambda _now=None: 12.9

        value = window._main_live_team_dps_text([
            {'actor_id': 1, 'metric': 'dps', 'dps_value': 12_345.0},
        ], now=1_000.0)

        self.assertEqual(value, '100,000/s')

    def dummy_window(self):
        window = self.window()
        window.pve_hud_view = 'recent_battle'
        window.model.combat_in_progress = lambda _now=None: True
        window.model._is_dummy_encounter = lambda: True
        window.model.duration = lambda _now=None: 31
        window.model.display_duration = lambda _now=None: 31
        window.model.current_stats()[0].damage = 562_179
        window.model.current_bosses = lambda: [SimpleNamespace(
            entity_id=90, name='伤害木桩', current_hp=8_749_410_821,
            max_hp=10_000_000_000, template_id=7_100_634,
            observed_damage_taken=1_250_589_179,
        )]
        window.model.current_monster = lambda: window.model.current_bosses()[0]
        return window

    def test_dummy_live_dps_uses_self_damage_instead_of_hp_or_teammates(self):
        window = self.dummy_window()

        value = window._main_live_team_dps_text([
            {'actor_id': 1, 'metric': 'dps', 'dps_value': 12_345.0},
            {'actor_id': 2, 'metric': 'dps', 'dps_value': 23_456.0},
        ], now=1_000.0)

        self.assertEqual(value, '18,135/s')
        with mock.patch.object(
            window, '_main_live_boss_hp_team_dps',
            side_effect=AssertionError('dummy DPS must not use HP loss'),
        ):
            self.assertEqual(
                window._main_live_team_dps_text([], now=1_000.0), '18,135/s'
            )

    def test_dummy_without_self_identity_does_not_use_hp_damage(self):
        window = self.dummy_window()
        window.model.self_id = None

        self.assertIsNone(window._main_live_team_dps_text([], now=1_000.0))

    def test_dummy_local_hits_update_live_dps_before_the_next_snapshot(self):
        window = self.window()
        model = window.model = CombatModel(run_id='dummy-live-hit-regression')
        model.ingest_identity({'entity_id': 1})
        model.ingest_profile({
            'entity_id': 90, 'template_id': 7_100_634, 'name': '伤害木桩',
            'entity_type': 'Boss', 'boss_type': 3, 'boss_rank': 3,
        })
        model.ingest_server_clock(1_024, 1_000)
        window._shown_actor_name = model.display_name
        window._bind_active_settlement_history = lambda: None
        scheduled = []
        window._schedule_layered_main_render = lambda: scheduled.append(True)
        timestamp = 116_444_736_000_000_000 + 1_000 * 10_000_000
        event = {
            'filetime_100ns': timestamp, 'attacker_id': 1, 'target_id': 90,
            'skill_id': 86_021_070, 'damage': 812, 'player_attacker': True,
        }
        window._ingest_combat_event(event)
        model.ingest_team_stat({
            'filetime_100ns': timestamp + 2_000_000, 'actor_id': 1,
            'absolute_damage': 812, 'server_time': 1_024,
        })
        self.assertEqual(
            window._main_live_team_dps_text([], now=1_000.2), '812/s'
        )

        window._ingest_combat_event(dict(
            event, filetime_100ns=timestamp + 10_000_000, damage=1_183,
        ))
        model.ingest_team_stat({
            'filetime_100ns': timestamp + 11_000_000, 'actor_id': 1,
            'absolute_damage': 812, 'server_time': 1_024,
        })

        self.assertEqual(scheduled, [True, True])
        self.assertEqual(model.dummy_round_official_total, 812)
        self.assertEqual(
            window._main_live_team_dps_text([], now=1_001.1), '1,995/s'
        )

    def test_live_dps_caption_changes_only_for_the_dummy(self):
        window = self.dummy_window()
        snapshot = window._layered_main_snapshot()
        section = next(
            row for row in snapshot['rows'] if row.get('row_kind') == 'section'
        )
        self.assertEqual(section['live_dps_caption'], '实时玩家秒伤')
        self.assertEqual(section['live_team_dps'], '18,135/s')

        window.model._is_dummy_encounter = lambda: False
        snapshot = window._layered_main_snapshot()
        section = next(
            row for row in snapshot['rows'] if row.get('row_kind') == 'section'
        )
        self.assertEqual(section['live_dps_caption'], '实时团队秒伤')
        self.assertNotEqual(section['live_team_dps'], '18,135/s')

    def test_live_teammate_counters_replace_boss_hp_fallback(self):
        window = self.window()
        window.model.combat_in_progress = lambda _now=None: True
        window.model.current_bosses = lambda: [SimpleNamespace(current_hp=80)]
        window.model.current_monster = lambda: window.model.current_bosses()[0]
        window.model.observed_boss_damage_taken = lambda: 1_200_000
        window.model.display_duration = lambda _now=None: 12.0

        value = window._main_live_team_dps_text([
            {'actor_id': 1, 'metric': 'dps', 'dps_value': 12_345.0},
            {'actor_id': 2, 'metric': 'dps', 'dps_value': 23_456.0},
        ], now=1_000.0)

        self.assertEqual(value, '35,801/s')

    def test_boss_hp_fallback_uses_only_the_pending_next_phase(self):
        window = self.window()
        window.model.combat_in_progress = lambda _now=None: True
        pending = SimpleNamespace(
            current_hp=40_000_000,
            observed_damage_taken=600_000,
        )
        window.model.current_bosses = lambda: [pending]
        window.model.current_monster = lambda: pending
        window.model.observed_boss_damage_taken = lambda: 10_000_000
        window.model.display_duration = lambda _now=None: 6.0

        value = window._main_live_team_dps_text([], now=1_000.0)

        self.assertEqual(value, '100,000/s')

    def test_packet_only_boss_hp_can_supply_live_team_dps(self):
        window = self.window()
        window.model.combat_in_progress = lambda _now=None: False
        window.live_hud_boss_state = {
            'active': True,
            'entity_id': 99,
            'template_id': 7_115_718,
            'started_at_epoch': 1_000.0,
            'current_hp': 9_500_000.0,
            'current_hp_known': True,
            'observed_damage_taken': 500_000.0,
        }
        window.live_hud_dps_segment = {
            'kind': 'boss',
            'active': True,
            'started_at_epoch': 1_000.0,
            'ended_at_epoch': 0.0,
        }

        value = window._main_live_team_dps_text([
            {'actor_id': 1, 'metric': 'dps', 'dps_value': 12_345.0},
        ], now=1_010.0)

        self.assertEqual(value, '50,000/s')

    def test_ended_clock_uses_official_duration_even_with_remaining_boss_hp(self):
        window = self.window()
        self.ended(window)
        boss = SimpleNamespace(entity_id=90, name='Previous Boss', current_hp=50, max_hp=100)
        window.model.current_monster = lambda: boss
        self.assertEqual(window._layered_main_snapshot()['time'], '00:37')

    def test_ended_clock_falls_back_to_same_encounter_boundaries(self):
        window = self.window()
        self.ended(window, duration=None)
        self.assertEqual(window._layered_main_snapshot()['time'], '00:42')

    def test_pending_new_boss_beats_retained_boss_without_first_hp(self):
        window = self.window()
        self.ended(window)
        window.main_last_battle_result = completed_record()
        boss = SimpleNamespace(entity_id=91, name='New Boss', current_hp=None, max_hp=100)
        window.model.current_monster = lambda: boss
        window.model.current_bosses = lambda: [boss]
        window.model.pending_active_boss_id = 91
        window.model.display_duration = lambda: 5
        snapshot = window._layered_main_snapshot()
        self.assertEqual(snapshot['boss_name'], 'New Boss')
        self.assertEqual(snapshot['time'], '00:05')

    def test_newer_boss_health_beats_retained_boss_before_combat_signal(self):
        window = self.window()
        window.model.started = False
        window.main_last_battle_result = completed_record()
        boss = SimpleNamespace(
            entity_id=91,
            name='New Boss',
            current_hp=3_805_441,
            max_hp=5_114_224,
            last_hp_update_100ns=(
                116_444_736_000_000_000 + 1_001 * 10_000_000
            ),
        )
        window.model.current_monster = lambda: boss
        window.model.current_bosses = lambda: [boss]

        snapshot = window._layered_main_snapshot()

        self.assertEqual(snapshot['boss_name'], 'New Boss')
        self.assertEqual(snapshot['boss_hp'], '380.54万 / 511.42万')
        self.assertAlmostEqual(snapshot['boss_ratio'], 3_805_441 / 5_114_224)

    def test_health_older_than_result_keeps_retained_boss(self):
        window = self.window()
        window.model.started = False
        window.main_last_battle_result = completed_record()
        boss = SimpleNamespace(
            entity_id=91,
            name='Stale Boss',
            current_hp=3_805_441,
            max_hp=5_114_224,
            last_hp_update_100ns=(
                116_444_736_000_000_000 + 999 * 10_000_000
            ),
        )
        window.model.current_monster = lambda: boss
        window.model.current_bosses = lambda: [boss]

        snapshot = window._layered_main_snapshot()

        self.assertEqual(snapshot['boss_name'], 'Boss')
        self.assertEqual(snapshot['boss_percent'], '0%')

    def test_new_live_pull_does_not_use_previous_settlement_clock(self):
        window = self.window()
        self.ended(window)
        window.model.started = True
        window.main_time_text = '00:07'
        self.assertEqual(window._layered_main_snapshot()['time'], '00:07')

    def test_team_wheel_and_thumb_use_rendered_rows_not_recent_battle_rows(self):
        window = self.window()
        window.model.started = False
        window.settlement_ui = SimpleNamespace(tracker=EncounterTracker(), visible=lambda: None)
        window._layered_main_active = lambda: True
        window._main_row_height = lambda: 10
        window._schedule_layered_main_render = lambda: None
        window.layered_main_hit_regions = {
            'scrollbar:rows': (280, 100, 306, 400),
            'scroll:rows': (0, 100, 306, 400),
        }
        self.assertEqual(len(window._main_display_rows()[0]), 3)
        self.assertEqual(len(window._layered_main_snapshot()['rows']), 7)
        window._scroll_main(SimpleNamespace(x=120, y=160, delta=-120, num='??'))
        self.assertEqual(window.main_scroll_offset, 10)
        window._layered_main_press(SimpleNamespace(x=290, y=399, x_root=290, y_root=399))
        self.assertEqual(window.main_scroll_offset, 30)
        self.assertIsNotNone(window.layered_main_scroll_drag_origin)

    def test_trend_refreshes_same_record_when_settlement_arrives(self):
        window = self.window()
        record = {'encounter_id': 'battle', 'duration_seconds': None, 'total_damage': None}
        window.history_selected_id = 'battle'
        window.history_trend_record_id = ''
        window._selected_history_record = lambda: record
        self.assertEqual(window._history_snapshot_team_dps_points(), [])
        record.update(duration_seconds=20, dps_duration_seconds=None, total_damage=3000, settlement_revision=2)
        self.assertEqual(window._history_snapshot_team_dps_points(), [(0, 150), (20, 150)])

    def test_exact_roster_replacement_drops_provisional_ghost(self):
        model = CombatModel()
        model.ingest_identity({'entity_id': 1})
        model.provisional_party_ids = {2}
        model.friend_order = [1, 2]
        model.ingest_party({'entity_ids': [-3], 'member_count': 2, 'roster_replace': True, 'in_team': True})
        self.assertEqual(model._current_member_ids(), {1, -3})

    def test_token_confirmed_member_is_visible_before_name_or_profession(self):
        window, members = make_window()
        members.add(-500)
        window.model.actor_character_ids = {-500: 'confirmed-member'}
        window.model.party_user_tokens = {'confirmed-member'}
        self.assertIn(-500, window._main_visible_roster_members())
        self.assertNotIn(-10, window._main_visible_roster_members())

    def test_official_party_member_is_visible_while_profile_is_pending(self):
        window, members = make_window()
        members.add(-500)
        window.model.party_ids = {-500}
        window.model.actor_character_ids = {}
        window.model.party_user_tokens = {'pending-member'}

        self.assertIn(-500, window._main_visible_roster_members())

    def test_pve_team_section_keeps_eight_member_roster_while_profiles_pending(self):
        window, members = make_window()
        pending_ids = {-500 - index for index in range(7)}
        members.clear()
        members.update({window.model.self_id, *pending_ids})
        window.model.party_ids = set(pending_ids)
        window.model.party_member_count = 8
        window.model.friend_order = [window.model.self_id, *sorted(pending_ids)]
        window.model.actor_character_ids = {}
        window.model.self_character_id = 'self-token'
        window.model.party_user_tokens = {'self-token', *{
            f'pending-member-{index}' for index in range(7)
        }}
        window.team_rating_preview_enabled = False

        rows = window._main_team_rating_section_rows()

        self.assertEqual(len(rows), 8)
        self.assertEqual(
            {row['actor_id'] for row in rows},
            {window.model.self_id, *pending_ids},
        )
        self.assertEqual(len(window._current_team_equipment_tokens()), 8)

    def test_named_projection_preview_is_visible_without_duplicate_empty_slot(self):
        window, members = make_window()
        members.update({-500, -501, -502, -503, 20})
        window.model.actor_character_ids = {
            -500: 'synthetic-position-only',
            -501: 'confirmed-human',
            -502: 'named-projection',
            20: 'bound-projection',
        }
        window.model.party_user_tokens = set(window.model.actor_character_ids.values())
        window.model.entity_ai_states.update({-500: True, -502: True, -503: True, 20: True})
        window.model.entity_names.update({
            -500: '玩家3', -502: '投影队友', -503: '未入队投影', 20: '实体投影',
        })
        window.model.entity_professions.update({
            -502: 1_200_005, -503: 1_200_007, 20: 1_200_005,
        })

        visible = window._main_visible_roster_members()
        self.assertNotIn(-500, visible)
        self.assertTrue({-501, -502, -503, 20}.issubset(visible))
        rows = window._main_team_rating_section_rows()
        self.assertNotIn(-500, {row['actor_id'] for row in rows})
        self.assertIn(-503, {row['actor_id'] for row in rows})


class LiveRosterRegressions(unittest.TestCase):
    def parser(self):
        parser = NetworkPacketParser()
        parser.self_id, parser.self_token, parser.self_confirmed = PLAYER_ID, SELF_TOKEN, True
        parser._bind_team_token(SELF_TOKEN, PLAYER_ID)
        return parser

    def roster(self, parser, tokens):
        members = [{'$map': [[2, token], [5, f'Player {index}'], [8, 1200001]]}
                   for index, token in enumerate(tokens)]
        return parser.process(packet('OnJoinGroupSuccess', [members]))

    def test_one_property_delta_keeps_eight_member_team(self):
        parser = self.parser()
        tokens = [SELF_TOKEN, TEAMMATE_TOKEN, *[f'AQAAAOwN0ga{i}AAAA' for i in range(6)]]
        self.roster(parser, tokens)
        parser.process(packet('OnUpdateTeamGroupMemberProps', [TEAMMATE_TOKEN, {'$map': [[4, False], [5, 100], [6, 100]]}]))
        updates = parser.flush_live_party_roster(now=parser.live_party_roster_last_monotonic + 2)
        roster = next(value for kind, value in updates if kind == 'party')
        self.assertEqual(set(roster['user_tokens']), set(tokens))
        self.assertEqual(roster['member_count'], 8)

    def test_six_player_roster_survives_long_quiet_property_window(self):
        with mock.patch('network_state.time.monotonic', return_value=100.0):
            parser = self.parser()
            tokens = [SELF_TOKEN, TEAMMATE_TOKEN,
                      *[f'AQAAAOwN0gc{i}AAAA' for i in range(4)]]
            self.roster(parser, tokens)
            parser.process(packet('OnUpdateTeamGroupMemberProps', [
                TEAMMATE_TOKEN, {'$map': [[5, 100], [11, 90_000]]},
            ]))

        updates = parser.flush_live_party_roster(now=160.0)
        roster = next(value for kind, value in updates if kind == 'party')
        self.assertEqual(set(roster['user_tokens']), set(tokens))
        self.assertEqual(roster['member_count'], 6)
        self.assertEqual(parser.live_team_property_ratings[TEAMMATE_TOKEN], 90_000)
        self.assertEqual(parser.flush_live_party_roster(now=700.0), [])
        self.assertEqual(parser.current_team_profile_tokens(), set(tokens))

    def test_explicit_leave_is_not_undone_by_queued_heartbeat(self):
        parser = self.parser()
        self.roster(parser, [SELF_TOKEN, TEAMMATE_TOKEN])
        parser.process(packet('OnUpdateTeamGroupMemberProps', [TEAMMATE_TOKEN, {'$map': [[5, 100]]}]))
        updates = parser.process(packet('OnMsgOtherLeaveTeamGroup', [TEAMMATE_TOKEN]))
        self.assertTrue(next(value for kind, value in updates if kind == 'party')['roster_replace'])
        parser.flush_live_party_roster(now=parser.live_party_roster_last_monotonic + 2)
        self.assertNotIn(TEAMMATE_TOKEN, parser.party_tokens)

    def test_full_roster_drops_member_and_old_buffered_property(self):
        parser = self.parser()
        self.roster(parser, [SELF_TOKEN, TEAMMATE_TOKEN])
        parser.process(packet('OnUpdateTeamGroupMemberProps', [TEAMMATE_TOKEN, {'$map': [[5, 100]]}]))
        updates = self.roster(parser, [SELF_TOKEN])
        self.assertTrue(next(value for kind, value in updates if kind == 'party')['roster_replace'])
        parser.flush_live_party_roster(now=parser.live_party_roster_last_monotonic + 2)
        self.assertNotIn(TEAMMATE_TOKEN, parser.party_tokens)

    def test_scene_exit_partial_heartbeat_keeps_quiet_human_teammates(self):
        parser = self.parser()
        tokens = [
            SELF_TOKEN,
            TEAMMATE_TOKEN,
            *[f'AQAAAOwN0gb{i}AAAA' for i in range(4)],
        ]
        self.roster(parser, tokens)

        parser.process(packet('OnMsgBeforeEnterNewSpace', [], sequence=20))
        parser.process(packet(
            'OnUpdateTeamGroupMemberProps',
            [TEAMMATE_TOKEN, {'$map': [[4, False], [5, 100], [6, 100]]}],
            sequence=21,
        ))
        updates = parser.flush_live_party_roster(
            now=parser.live_party_roster_last_monotonic + 2
        )

        roster = next(value for kind, value in updates if kind == 'party')
        self.assertTrue(roster['roster_replace'])
        self.assertEqual(set(roster['user_tokens']), set(tokens))
        self.assertEqual(roster['member_count'], 6)


if __name__ == '__main__':
    unittest.main()
