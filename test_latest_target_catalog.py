import unittest
from pathlib import Path

from monster_metadata import load_monster_metadata
from network_state import DAMAGE_TARGET_TEMPLATE_IDS
from npcap_parser_adapter import NpcapParserAdapter


ROOT = Path(__file__).resolve().parent
DUMMY_TEMPLATE_ID = 7_100_640
BOSS_TEMPLATE_ID = 7_110_300


def entity_created(entity_id: int, template_id: int, sequence: int) -> dict:
    return {
        "method": "NpcapEntityCreated",
        "decoded_arguments": [
            {
                "entity_id": entity_id,
                "entity_token": f"target-token-{sequence}",
                "entity_class": "NpcActor",
                "properties": {
                    "TemplateID": template_id,
                    "Level": 98,
                    "BossType": 3,
                    "FinalOwnerUID": entity_id,
                    "LogicServerID": 1206,
                },
                "schema": "c7-2026-09-02-properties",
                "known_property_schema": True,
            }
        ],
        "script_entity": entity_id,
        "network_entity_id": entity_id,
        "sequence": sequence,
        "capture_source": "npcap",
        "arguments_synchronized": True,
        "capture_timestamp_ns": 1_795_765_335_616_975_300 + sequence,
        "filetime_100ns": 134_402_389_356_169_753 + sequence,
        "capture_event_id": f"latest-target:{sequence}",
        "npcap_message_type": 11,
        "npcap_method_scope": "entity_creation",
        "npcap_transport": "udp",
    }


class LatestTargetCatalogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog = load_monster_metadata(ROOT / "monster_metadata.json")

    def test_current_client_metadata_is_packaged(self):
        dummy = self.catalog[str(DUMMY_TEMPLATE_ID)]
        self.assertEqual(
            dummy,
            {
                "boss_type": 3,
                "level": 98,
                "localization_id": 826_834_354_803_456,
                "name": "伤害木桩",
            },
        )
        self.assertIn(DUMMY_TEMPLATE_ID, DAMAGE_TARGET_TEMPLATE_IDS)

        boss = self.catalog[str(BOSS_TEMPLATE_ID)]
        self.assertEqual(boss["boss_type"], 3)
        self.assertEqual(boss["level"], 98)
        self.assertEqual(boss["localization_id"], 826_834_354_737_408)
        self.assertEqual(boss["name"], "罗塞尔的残留意志")
        self.assertEqual(
            boss["fc_paths"],
            ["AI.RossellBossRaid.RossellBoss_Hero.Boss_Rossell_Hero"],
        )

        allowlist = (ROOT / "boss_allowlist.txt").read_text(encoding="utf-8")
        self.assertIn(dummy["name"], allowlist)
        self.assertIn(boss["name"], allowlist)

    def test_real_wire_creation_shapes_promote_dummy_and_boss(self):
        cases = (
            (DUMMY_TEMPLATE_ID, 57_255_135_376_640, "伤害木桩", True),
            (BOSS_TEMPLATE_ID, 57_255_135_377_085, "罗塞尔的残留意志", False),
        )
        for sequence, (template_id, entity_id, name, is_dummy) in enumerate(
            cases, start=1
        ):
            with self.subTest(template_id=template_id):
                target_catalog = {
                    str(template_id): dict(self.catalog[str(template_id)])
                }
                parser = NpcapParserAdapter(
                    {},
                    target_catalog,
                    target_identity_catalog=target_catalog,
                )

                updates = parser.process(
                    entity_created(entity_id, template_id, sequence)
                )

                profile = next(value for kind, value in updates if kind == "profile")
                self.assertEqual(profile["entity_id"], entity_id)
                self.assertEqual(profile["template_id"], template_id)
                self.assertEqual(profile["name"], name)
                self.assertEqual(profile["level"], 98)
                self.assertEqual(profile["boss_source"], "monster_template_catalog")
                self.assertIn(entity_id, parser.confirmed_boss_entities)
                self.assertEqual(entity_id in parser.training_dummy_entities, is_dummy)


if __name__ == "__main__":
    unittest.main()
