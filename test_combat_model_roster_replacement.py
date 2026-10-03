#!/usr/bin/env python3

from __future__ import annotations

import runpy
import unittest
from pathlib import Path


MODULE = runpy.run_path(str(Path(__file__).with_name("dps_meter.pyw")))
CombatModel = MODULE["CombatModel"]


class CombatModelRosterReplacementTests(unittest.TestCase):
    def test_replacement_removes_departed_live_profile_cache(self):
        model = CombatModel()
        model.ingest_identity({"entity_id": 100})
        model.ingest_party(
            {
                "entity_ids": [200, 300],
                "member_count": 3,
                "user_tokens": ["old-200", "old-300"],
                "authoritative": True,
            }
        )
        model.entity_names.update({200: "Old A", 300: "Old B"})
        model.entity_professions.update({200: 1200001, 300: 1200002})
        model.entity_extraordinary_ratings.update({200: 80000, 300: 81000})
        model.entity_ai_states.update({200: False, 300: True})
        model.entity_fight_attributes.update({200: {"power": 1.0}})
        model.actor_character_ids.update({200: "old-200", 300: "old-300"})
        model.events.append(
            {
                "attacker_id": 200,
                "target_id": 999,
                "damage": 1234,
                "filetime_100ns": 1,
            }
        )

        changed = model.ingest_party(
            {
                "entity_ids": [400],
                "member_count": 2,
                "user_tokens": ["new-400"],
                "authoritative": True,
                "roster_replace": True,
            }
        )

        self.assertTrue(changed)
        self.assertEqual(model.party_ids, {400})
        self.assertEqual(model.party_user_tokens, {"new-400"})
        self.assertNotIn(200, model.friend_order)
        self.assertNotIn(300, model.friend_order)
        for mapping in (
            model.entity_names,
            model.entity_professions,
            model.entity_extraordinary_ratings,
            model.entity_ai_states,
            model.entity_fight_attributes,
            model.actor_character_ids,
        ):
            self.assertNotIn(200, mapping)
            self.assertNotIn(300, mapping)
        # Roster replacement must not erase the current encounter's damage
        # history; only live profile/identity caches are replaced.
        self.assertEqual(model.events[0]["attacker_id"], 200)
        self.assertEqual(model.events[0]["damage"], 1234)

    def test_replacement_keeps_current_actor_when_token_snapshot_is_complete(self):
        model = CombatModel()
        model.ingest_identity({"entity_id": 100})
        model.ingest_party(
            {
                "entity_ids": [200],
                "member_count": 2,
                "user_tokens": ["old-200"],
                "authoritative": True,
            }
        )
        model.entity_names[200] = "Current"
        model.actor_character_ids[200] = "old-200"

        model.ingest_party(
            {
                "entity_ids": [200],
                "member_count": 2,
                "user_tokens": ["old-200"],
                "authoritative": True,
                "roster_replace": True,
            }
        )

        self.assertIn(200, model.party_ids)
        self.assertEqual(model.entity_names.get(200), "Current")
        self.assertEqual(model.actor_character_ids.get(200), "old-200")


if __name__ == "__main__":
    unittest.main()
