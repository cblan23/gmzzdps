#!/usr/bin/env python3

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from research_v0015_boss_skills import scan_logs


class BossSkillEvidenceTests(unittest.TestCase):
    def test_cast_packets_are_excluded_and_exact_boss_damage_is_kept(self):
        boss_entity = 57_236_882_400_409
        template_id = 7_107_030
        records = [
            {
                "function": "CommonComponent_TemplateBossType",
                "entity_id": boss_entity,
                "boss_type": 3,
                "template_id": template_id,
            },
            {
                "function": "doraemon::script::ScriptEntity::do_message",
                "method": "OnMsgCastSkillNew",
                "decoded_arguments": [86_021_070, boss_entity, None, None],
            },
            {
                "function": "KAPI_HandleDamageSyncV2",
                "attacker_id": boss_entity,
                "target_id": boss_entity + 1,
                "arg4_u64": 8_900_580_200_396,
                "damage": 1_234,
            },
        ]
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "network.jsonl"
            log.write_text(
                "".join(json.dumps(row) + "\n" for row in records),
                encoding="utf-8",
            )
            observations, counters = scan_logs(
                [log], {str(template_id): {"name": "卡尔·埃德加"}}
            )

        self.assertEqual(counters["lines"], 3)
        self.assertEqual(counters.get("parser_errors", 0), 0)
        self.assertNotIn(86_021_070, observations[template_id])
        damage = observations[template_id][89_005_802]
        self.assertEqual(damage["damage_event_count"], 1)
        self.assertEqual(damage["observed_damage"], 1_234)
