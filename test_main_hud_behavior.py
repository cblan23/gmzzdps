"""Display-only regressions for result retention, resolved roster and AI labels."""
import copy
import unittest
from types import SimpleNamespace

from test_combat_model import DpsWindow, ActorStats, normalize_profession_display_metrics


def make_window():
    window = object.__new__(DpsWindow)
    members = set(range(1, 7)) | {-10, -11, -12, -13}
    actors = []
    for actor_id in range(1, 7):
        actor = ActorStats(actor_id=actor_id)
        actor.damage = 700_000 - actor_id * 70_000
        actors.append(actor)
    model = SimpleNamespace(
        self_id=1, started=True, combat_end_time=0, party_active=True, party_member_count=6,
        party_known=True, encounter_member_ids=set(range(1, 7)),
        non_player_actor_ids=set(), entity_names={i: f'队员{i}' for i in range(1, 7)},
        entity_professions={i: 1200001 for i in range(1, 7)}, entity_ai_states={},
        member_death_counts={}, friend_order=list(members),
    )
    model._encounter_started = lambda: model.started
    model.combat_in_progress = lambda: model.started and not model.combat_end_time
    model._current_member_ids = lambda: set(members)
    model.current_stats = lambda: actors if model.started else []
    model.current_taken_rows = lambda: [dict(actor_id=i, taken=230 if i > 0 else None, source='server_team_counter' if i > 0 else 'unavailable') for i in members]
    model.duration = lambda _now=None: 30
    model.actor_profession_id = lambda actor: model.entity_professions.get(actor, 0)
    model.display_name = lambda actor: model.entity_names.get(actor, '')
    model.current_monster = lambda: None
    window.model = model
    window._shown_actor_name = model.display_name
    window.latest_healing_summary = {'healers': []}
    window.profession_display_metrics = normalize_profession_display_metrics({})
    window.team_rating_preview_enabled = True
    window.hide_names = False
    window._preferred_main_topmost = lambda: True
    window.config = {}
    return window, members


def completed_record():
    return dict(encounter_id='finished-six-person-battle', ended_at_epoch=1000,
                duration_seconds=30, dps_duration_seconds=30, team_dps=105_000,
                monster=dict(entity_id=90, name='战斗首领', current_hp=0, max_hp=3_000_000),
                participants=[dict(actor_id=i, name=f'队员{i}', is_self=i == 1,
                                   profession_id=1200001, is_ai=i in (3, 4), damage=700_000-i*70_000,
                                   dps=(700_000-i*70_000)/30, taken=230, deaths=0)
                              for i in range(1, 7)], healers=[])


class MainHudBehaviorTests(unittest.TestCase):
    def test_rating_and_deaths_default_on_without_overwriting_explicit_choices(self):
        from test_combat_model import migrate_main_display_config
        fresh, _ = migrate_main_display_config({})
        self.assertTrue(fresh['team_rating_preview'])
        self.assertTrue(fresh['show_deaths'])
        configured, _ = migrate_main_display_config(dict(team_rating_preview=False, show_deaths=False))
        self.assertFalse(configured['team_rating_preview'])
        self.assertFalse(configured['show_deaths'])

    def test_boss_level_uses_only_the_current_monster_metadata(self):
        window, _ = make_window()
        monster = SimpleNamespace(name='子爵夫人', level=72, current_hp=50000000, max_hp=66735474)
        self.assertEqual(window._main_boss_display_values(monster)['boss_level'], 72)
        for level in (None, 0, 'unknown', -1, 1000):
            monster.level = level
            self.assertIsNone(window._main_boss_display_values(monster)['boss_level'])

    def test_six_member_combat_does_not_import_four_empty_counter_slots(self):
        window, members = make_window()
        before = set(members)
        rows = window._main_combat_display_rows()
        self.assertEqual(len(rows), 6)
        self.assertEqual({row['actor_id'] for row in rows}, set(range(1, 7)))
        self.assertEqual(members, before)
        self.assertEqual([row['actor_id'] for row in rows], list(range(1, 7)))

    def test_zero_damage_real_member_and_named_temporary_member_are_retained(self):
        window, members = make_window()
        members.add(7)
        window.model.entity_names[-10] = '已识别队员'
        rows = window._main_combat_display_rows()
        ids = {row['actor_id'] for row in rows}
        self.assertIn(7, ids)
        self.assertIn(-10, ids)
        self.assertNotIn(-11, ids)

    def test_ended_battle_has_priority_over_rating_preview(self):
        window, _ = make_window()
        self.assertFalse(window._team_rating_preview_active())
        window.model.combat_end_time = 1000
        self.assertFalse(window._team_rating_preview_active())
        rows, rating = window._main_display_rows()
        self.assertFalse(rating)
        self.assertEqual(len(rows), 6)
        self.assertEqual(rows[0]['total_value'], 630_000)

    def test_fight_mode_switches_before_first_damage_without_arming_statistics(self):
        window, _ = make_window()
        window.model.started = False
        window.model.entity_combat_states = {1: True}
        self.assertFalse(window._team_rating_preview_active())
        self.assertFalse(window._main_has_battle_values())
        _, rating = window._main_display_rows()
        self.assertFalse(rating)
        self.assertFalse(window.model.started)
        window._remember_main_battle_result(completed_record())
        self.assertFalse(window._main_retained_battle_active())

    def test_old_opacity_preferences_are_migrated_and_other_values_are_preserved(self):
        from test_combat_model import migrate_main_display_config
        for value in (0, 0.1, 0.45, 1.0, None, 'bad'):
            config = dict(alpha=value, geometry='123x456+78+90', font_size=16)
            updated, _ = migrate_main_display_config(config)
            self.assertEqual(updated['alpha'], 1.0)
            self.assertEqual(updated['geometry'], config['geometry'])
            self.assertEqual(updated['font_size'], 16)

    def test_boss_hp_updates_schedule_paint_without_waiting_for_summary_tick(self):
        window, _ = make_window()
        updates=[]
        window.model.ingest_monster = lambda value: updates.append(value) or True
        paints=[]
        window._schedule_layered_main_render = lambda: paints.append(True)
        packet=dict(entity_id=99, current_hp=238900, max_hp=1000000)
        window._dispatch_message('monster', packet)
        self.assertEqual(updates, [packet])
        self.assertEqual(paints, [True])

    def test_same_map_model_reset_retains_finished_battle_until_new_pull(self):
        window, _ = make_window()
        record = completed_record()
        original = copy.deepcopy(record)
        window._remember_main_battle_result(record)
        window.model.started = False
        self.assertFalse(window._team_rating_preview_active())
        self.assertTrue(window._main_retained_battle_active())
        snapshot = window._layered_main_snapshot()
        self.assertEqual(len(snapshot['rows']), 6)
        self.assertEqual(snapshot['boss_name'], '战斗首领')
        self.assertEqual(snapshot['boss_percent'], '0%')
        self.assertEqual(snapshot['time'], '00:30')
        self.assertEqual(snapshot['team_dps'], '105,000')
        self.assertEqual(record, original)
        window.model.started = True
        self.assertFalse(window._main_retained_battle_active())
        self.assertFalse(window._team_rating_preview_active())

    def test_map_transition_clears_hud_and_does_not_restore_late_old_record(self):
        window, _ = make_window()
        record = completed_record()
        window._remember_main_battle_result(record)
        window.model.encounter_id = record['encounter_id']
        archived = []
        def scene_update(_payload):
            archived.append(record)
            window.model.started = False
            window.model.encounter_id = 'new-map'
            return True
        window.model.ingest_scene = scene_update
        window._invalidate_team_rating_preview_rows = lambda: None
        scheduled = []
        window._schedule_layered_main_render = lambda: scheduled.append(True)
        window._dispatch_message('scene', {'transition': True})
        self.assertIsNone(window.main_last_battle_result)
        self.assertEqual(window.team_dps_text, '0')
        self.assertEqual(window.main_time_text, '00:00')
        self.assertEqual(archived, [record])
        window._remember_main_battle_result(record)
        self.assertIsNone(window.main_last_battle_result)
        self.assertTrue(window._team_rating_preview_active())
        self.assertEqual(scheduled, [True])

    def test_special_boss_scripted_phase_scene_does_not_clear_model_statistics(self):
        window, _ = make_window()
        record = completed_record()
        window._remember_main_battle_result(record)
        window.model.long_gap_phase_suspended = True
        window.model.ingest_scene = lambda _payload: True
        window._invalidate_team_rating_preview_rows = lambda: None
        window._dispatch_message('scene', {'scene_id': 200})
        self.assertIsNotNone(window.main_last_battle_result)
        self.assertTrue(window.model.started)

    def test_retained_hps_and_dt_are_read_from_existing_record_fields(self):
        window, _ = make_window()
        record = completed_record()
        record['participants'][0]['profession_id'] = 1200002
        record['participants'][1]['profession_id'] = 1200006
        record['healers'] = [dict(actor_id=1, hps=31_500, effective_healing=945_000)]
        window.profession_display_metrics['1200006'] = 'dt'
        window._remember_main_battle_result(record)
        window.model.started = False
        rows, rating = window._main_display_rows()
        self.assertFalse(rating)
        self.assertEqual(rows[0]['metric'], 'hps')
        self.assertEqual(rows[0]['stat_value'], 31_500)
        self.assertEqual(rows[0]['total_value'], 945_000)
        self.assertEqual(rows[1]['metric'], 'dt')
        self.assertEqual(rows[1]['stat_value'], 230 / 30)
        self.assertEqual([row['actor_id'] for row in rows], list(range(1, 7)))

    def test_dual_boss_snapshot_keeps_independent_health_without_resetting_clock(self):
        window, _ = make_window()
        bosses = [SimpleNamespace(entity_id=101, name='甲', current_hp=100, max_hp=400),
                  SimpleNamespace(entity_id=102, name='乙', current_hp=200, max_hp=300)]
        window.model.current_monster = lambda: bosses[0]
        window.model.current_bosses = lambda: bosses
        window.main_time_text = '01:25'
        window.team_dps_text = '7,500'
        snapshot = window._layered_main_snapshot()
        self.assertEqual(len(snapshot['bosses']), 2)
        self.assertEqual([row['boss_name'] for row in snapshot['bosses']], ['甲', '乙'])
        self.assertEqual([row['boss_percent'] for row in snapshot['bosses']], ['25.0%', '66.7%'])
        self.assertEqual(snapshot['time'], '01:25')
        bosses.pop(0)
        next_stage = window._layered_main_snapshot()
        self.assertEqual(len(next_stage['bosses']), 1)
        self.assertEqual(next_stage['time'], '01:25')

    def test_first_believer_forecast_owner_barney_is_the_first_bar(self):
        window, _ = make_window()
        anxia = SimpleNamespace(entity_id=101, template_id=7100208, name='安西娅', current_hp=300, max_hp=400)
        barney = SimpleNamespace(entity_id=102, template_id=7100209, name='巴尼先生', current_hp=200, max_hp=300)
        bosses = [anxia, barney]
        window.model.current_monster = lambda: anxia
        window.model.current_bosses = lambda: bosses
        first = window._layered_main_snapshot()
        self.assertEqual([boss['boss_name'] for boss in first['bosses']], ['巴尼先生', '安西娅'])
        self.assertEqual(bosses, [anxia, barney])
        barney.current_hp = 0
        barney.death_confirmed = True
        second = window._layered_main_snapshot()
        self.assertEqual(second['bosses'][0]['boss_name'], '安西娅')

    def test_confirmed_self_keeps_own_row_during_roster_handoff(self):
        window, members = make_window()
        window.model.self_id = 21
        window.model.entity_names[21] = '本人'
        window.model.current_stats = lambda: []
        window.model.current_taken_rows = lambda: []
        window.model.encounter_member_ids = set()
        rows = window._main_combat_display_rows()
        self.assertIn(21, {row['actor_id'] for row in rows})
        self.assertNotIn(21, members)

    def test_exact_dps_appears_before_token_actor_gets_positive_entity_id(self):
        window, members = make_window()
        temporary = -9912233
        members.add(temporary)
        actor = ActorStats(actor_id=temporary)
        actor.damage = 300_000
        window.model.current_stats = lambda: [actor]
        window.model.encounter_member_ids = {temporary}
        window.model.entity_names[temporary] = '已确认队友'
        window.model.entity_professions[temporary] = 1200001
        rows = window._main_combat_display_rows()
        teammate = next(row for row in rows if row['actor_id'] == temporary)
        self.assertEqual(teammate['stat_value'], 10000)
        self.assertEqual(teammate['total_value'], 300000)
        self.assertEqual(rows[0]['actor_id'], temporary)
        self.assertEqual(actor.damage, 300000)

    def test_party_fight_signal_immediately_leaves_pre_pull_preview(self):
        window, _ = make_window()
        window.model.started = False
        window.model.entity_combat_states = {2: True}
        self.assertFalse(window._team_rating_preview_active())
        window.model.entity_combat_states = {999: True}
        self.assertTrue(window._team_rating_preview_active())

    def test_healing_only_participant_is_retained_after_scene_reset(self):
        window, _ = make_window()
        record = completed_record()
        record['healers'] = [dict(actor_id=12, name='观众', is_self=False, profession_id=1200002,
                                  hps=1000, effective_healing=30000)]
        window._remember_main_battle_result(record)
        window.model.started = False
        rows, _ = window._main_display_rows()
        healer = next(row for row in rows if row['actor_id'] == 12)
        self.assertEqual(healer['metric'], 'hps')
        self.assertEqual(healer['stat_value'], 1000)

    def test_ai_rating_text_overrides_numeric_rating_without_mutating_profile(self):
        window, _ = make_window()
        window.model.started = False
        profiles = [dict(actor_id=1, profession_id=1200001, display_name='玩家', extraordinary_rating=123456, is_ai=False),
                    dict(actor_id=2, profession_id=1200002, display_name='队员·投影', extraordinary_rating=75900, is_ai=True)]
        window._team_rating_preview_rows = lambda: profiles
        snapshot = window._layered_main_snapshot()
        self.assertTrue(snapshot['rating_preview'])
        self.assertEqual(snapshot['rows'][0]['rating_text'], '123456')
        self.assertEqual(snapshot['rows'][1]['rating_text'], '人机')
        self.assertEqual(profiles[1]['extraordinary_rating'], 75900)

    def test_pre_pull_preview_omits_unbound_roster_slots(self):
        window, _ = make_window()
        window.model.started = False
        rows = window._team_rating_preview_rows()
        self.assertEqual(len(rows), 6)

    def test_authorization_reset_does_not_preserve_previous_identity_result(self):
        window, _ = make_window()
        window.authorization_resetting = True
        window._remember_main_battle_result(completed_record())
        self.assertIsNone(getattr(window, 'main_last_battle_result', None))

    def test_manual_clear_discards_result_and_restores_pre_pull_preview(self):
        window, _ = make_window()
        window._remember_main_battle_result(completed_record())
        window.model.started = False
        window.model.reset = lambda **_kwargs: None
        window._flush_combat_history = lambda: None
        window._draw_main_rows = lambda: None
        window._render_skill_details = lambda: None
        window.reset()
        self.assertIsNone(window.main_last_battle_result)
        self.assertTrue(window._team_rating_preview_active())


if __name__ == '__main__':
    unittest.main()
