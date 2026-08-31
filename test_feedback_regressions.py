#!/usr/bin/env python3

"""Coverage manifest for every feedback at/after FBB286B655A129BAC3."""

from __future__ import annotations

import unittest

import test_combat_model
import test_network_state


def combat(name: str) -> tuple[str, str]:
    return "combat", name


def network(name: str) -> tuple[str, str]:
    return "network", name


OBSERVER_LEDGER = combat(
    "test_identical_common_snapshots_match_on_all_twelve_clients"
)
FINAL_SETTLEMENT = combat(
    "test_final_settlement_is_validation_only_and_never_rewrites_damage"
)
COMPLETED_STAGE_SETTLEMENT = combat(
    "test_delayed_final_common_matches_baldwin_completion_table"
)
LIVE_TEAM_DAMAGE = network(
    "test_hit_callback_and_hp_drop_do_not_create_teammate_damage"
)
BOSS_HP_BINDING = network(
    "test_boss_hp_stream_replaces_stale_player_pointer_binding"
)
ASTROLOGER_PHASES = network(
    "test_multiphase_boss_hp_changes_never_create_team_damage"
)
GUARD_MECHANIC = network(
    "test_star_guard_death_and_parent_hp_drop_emit_no_guessed_damage"
)
BALDWIN_PHASES = combat(
    "test_generic_ancestor_to_baldwin_long_transition_stays_one_encounter"
)


FEEDBACK_REGRESSIONS = {
    "FBB286B655A129BAC3": (ASTROLOGER_PHASES, GUARD_MECHANIC),
    "FBCA9EE74B02230FD8": (
        OBSERVER_LEDGER,
        network("test_stale_same_template_drill_respawn_replaces_wiped_entity"),
    ),
    "FBE64D87D66CCBE07C": (FINAL_SETTLEMENT, COMPLETED_STAGE_SETTLEMENT),
    "FB603EAB7F0DF79D15": (BOSS_HP_BINDING,),
    "FB985AA2E794890AB5": (
        combat("test_dps_is_continuous_across_minute_boundaries"),
        combat("test_legacy_hp_correlated_events_are_rejected"),
    ),
    "FBE2944D642C3A241E": (
        LIVE_TEAM_DAMAGE,
        FINAL_SETTLEMENT,
        COMPLETED_STAGE_SETTLEMENT,
    ),
    "FB540BE52083C4BACB": (
        combat("test_full_positive_roster_admits_live_damage_actor_namespace"),
    ),
    "FBAF3C425C269BAFE1": (
        combat("test_full_positive_roster_admits_live_damage_actor_namespace"),
        network("test_native_damage_classifies_unbound_actor_by_complete_party_profession"),
    ),
    "FB1FB02AA330C2BBB4": (GUARD_MECHANIC, ASTROLOGER_PHASES),
    "FB01EF06DCAF1BA6D3": (
        network("test_boss_max_hp_epoch_does_not_credit_phase_drop_to_old_hits"),
    ),
    "FB2A3B3171F6F8ADB6": (
        network("test_transient_tiny_boss_hp_sample_does_not_create_fake_team_damage"),
    ),
    "FBB3FC05BD474B4915": (
        combat("test_first_observed_monster_hp_is_progress_baseline"),
        network("test_first_hp_does_not_backfill_recent_or_midfight_max_hp"),
    ),
    "FB05779B4338E2B393": (
        combat("test_legacy_hp_correlated_events_are_rejected"),
        network("test_malformed_hp_shape_and_transient_rise_do_not_change_boss_baseline"),
    ),
    "FB0FF4CCB0AF8424F7": (
        network("test_star_guard_real_damage_is_not_removed_with_parent_mechanic"),
    ),
    "FBD2EA0E4E3620B20E": (
        combat("test_zero_hp_daily_boss_ignores_late_linked_monster_damage"),
        combat("test_delayed_damage_before_death_does_not_create_a_second_death"),
    ),
    "FB8E738F35DCC29DFE": (
        GUARD_MECHANIC,
        combat("test_nonzero_unconfirmed_dead_state_does_not_increment_deaths"),
    ),
    "FBD54AD8CFD46B5926": (
        network("test_knowledge_prohibition_proxy_death_never_counts_as_player_death"),
        GUARD_MECHANIC,
    ),
    "FB3958DB48DB1D4CE8": (
        network("test_revived_star_guard_deaths_never_create_player_damage"),
        combat("test_astrologer_phase_refill_and_long_gap_stay_in_one_encounter"),
    ),
    "FBE658BA8EED086927": (
        FINAL_SETTLEMENT,
        COMPLETED_STAGE_SETTLEMENT,
        OBSERVER_LEDGER,
    ),
    "FB7408AA158422D3F4": (
        combat("test_astrologer_final_death_freezes_dps_before_delayed_archive"),
        network("test_missing_astrologer_imprisonments_are_inferred_from_damage_targets"),
    ),
    "FB0A492F3833B47F1B": (
        OBSERVER_LEDGER,
        FINAL_SETTLEMENT,
        COMPLETED_STAGE_SETTLEMENT,
    ),
    "FB887BDEF04E76F978": (
        LIVE_TEAM_DAMAGE,
        combat("test_mid_scene_projection_roster_shows_every_damage_actor_immediately"),
        BALDWIN_PHASES,
        COMPLETED_STAGE_SETTLEMENT,
    ),
    "FB053CBEF725205826": (
        OBSERVER_LEDGER,
        FINAL_SETTLEMENT,
        BALDWIN_PHASES,
        COMPLETED_STAGE_SETTLEMENT,
    ),
    "FB2126C7A95990D4E6": (OBSERVER_LEDGER,),
    "FBA195A96FE643842A": (OBSERVER_LEDGER,),
    "FBC72B709FBA84FC42": (OBSERVER_LEDGER,),
    "FB445488FAF1DB3163": (OBSERVER_LEDGER, ASTROLOGER_PHASES),
    "FB7F3D350B3F113642": (OBSERVER_LEDGER,),
    "FBFDDB9EEE0DF9294F": (
        OBSERVER_LEDGER,
        BALDWIN_PHASES,
        COMPLETED_STAGE_SETTLEMENT,
    ),
    "FB3B530638A93D7659": (
        OBSERVER_LEDGER,
        BALDWIN_PHASES,
        COMPLETED_STAGE_SETTLEMENT,
    ),
    "FBFD9FCBB563E85BDD": (
        OBSERVER_LEDGER,
        BALDWIN_PHASES,
        COMPLETED_STAGE_SETTLEMENT,
    ),
    "FBA4568B0FE889AF64": (OBSERVER_LEDGER,),
    "FBD7312FD201DA3C87": (
        OBSERVER_LEDGER,
        FINAL_SETTLEMENT,
        BALDWIN_PHASES,
        COMPLETED_STAGE_SETTLEMENT,
    ),
    "FB0E2CF2A7E0845B53": (
        OBSERVER_LEDGER,
        FINAL_SETTLEMENT,
        BALDWIN_PHASES,
        COMPLETED_STAGE_SETTLEMENT,
    ),
    "FBE3F6873ED8E68D8B": (
        OBSERVER_LEDGER,
        FINAL_SETTLEMENT,
        BALDWIN_PHASES,
        COMPLETED_STAGE_SETTLEMENT,
    ),
    "FBA04673C16EDED0DA": (
        BOSS_HP_BINDING,
        network("test_stage_bound_actor_cast_does_not_guess_realtime_damage"),
        FINAL_SETTLEMENT,
        COMPLETED_STAGE_SETTLEMENT,
    ),
    "FBD5F0B5D34007F9E0": (
        BOSS_HP_BINDING,
        network("test_stage_bound_actor_cast_does_not_guess_realtime_damage"),
        COMPLETED_STAGE_SETTLEMENT,
    ),
    "FBF0D88FDA5EFE479A": (OBSERVER_LEDGER, ASTROLOGER_PHASES),
    "FB76FFBB4D8494831A": (OBSERVER_LEDGER, ASTROLOGER_PHASES),
    "FB7117B57DAA25F147": (OBSERVER_LEDGER, ASTROLOGER_PHASES),
    "FB9466C5D00ECF5699": (
        combat(
            "test_feedback_fb9466_common_matches_all_twelve_completion_rows"
        ),
        network("test_completed_stage_update_emits_authoritative_exact_result"),
        OBSERVER_LEDGER,
    ),
    "FB9561E555D7298DFF": (
        combat(
            "test_feedback_fb9561_common_replaces_inflated_observer_damage"
        ),
        OBSERVER_LEDGER,
        FINAL_SETTLEMENT,
    ),
}


class FeedbackRegressionManifestTests(unittest.TestCase):
    def test_every_feedback_from_anchor_through_latest_has_live_regressions(self):
        suites = {
            "combat": test_combat_model.CombatModelTests,
            "network": test_network_state.NetworkPacketParserTests,
        }
        self.assertEqual(len(FEEDBACK_REGRESSIONS), 42)
        for feedback_id, regressions in FEEDBACK_REGRESSIONS.items():
            with self.subTest(feedback_id=feedback_id):
                self.assertRegex(feedback_id, r"^FB[A-F0-9]{16}$")
                self.assertTrue(regressions)
                for suite_name, method_name in regressions:
                    self.assertTrue(
                        callable(getattr(suites[suite_name], method_name, None))
                    )


if __name__ == "__main__":
    unittest.main()
