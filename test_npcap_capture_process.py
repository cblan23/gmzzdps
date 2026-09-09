from __future__ import annotations

import base64
import json
import unittest
from pathlib import Path

from network_state import NetworkPacketParser
from npcap_capture_process import (
    PassiveRc4Reassembler,
    PushSegment,
)
from npcap_key_state import Rc4Anchor
from npcap_protocol import METHOD_ID_NAMES, NpcapProtocolDecoder
from npcap_rc4_decode import Rc4State


def initial_state() -> Rc4State:
    # Any valid permutation is sufficient for exercising the reversible PRGA.
    return Rc4State(0, 0, list(range(256)))


def valid_push(marker: int, length: int = 13) -> bytes:
    value = bytearray([0] * length)
    bit_count = length * 8 - 20
    value[:3] = bit_count.to_bytes(3, "little")
    for index in range(3, length):
        value[index] = (marker + index * 17) & 0xFF
    return bytes(value)


def encrypted_run(
    first: int, count: int
) -> tuple[dict[int, bytes], dict[int, Rc4State], dict[int, bytes]]:
    state = initial_state()
    ciphertext: dict[int, bytes] = {}
    after: dict[int, Rc4State] = {}
    plaintext: dict[int, bytes] = {}
    for sequence in range(first, first + count):
        value = valid_push(sequence)
        plaintext[sequence] = value
        ciphertext[sequence] = state.forward(value)
        after[sequence] = state.clone()
    return ciphertext, after, plaintext


def anchor(state: Rc4State, epoch: float) -> Rc4Anchor:
    return Rc4Anchor(
        cryptor_address=0x1234,
        read_before_epoch=epoch - 0.0001,
        read_after_epoch=epoch + 0.0001,
        encrypt=state.clone(),
        decrypt=state.clone(),
    )


class PassiveRc4ReassemblerTests(unittest.TestCase):
    def test_aligns_both_sides_and_ignores_retransmission(self):
        ciphertext, states, plaintext = encrypted_run(100, 8)
        stream = PassiveRc4Reassembler(required_validation=3)
        stream.install_anchor(anchor(states[102], 10.2), now=1.0)
        output = []
        for sequence in range(100, 108):
            output.extend(
                stream.add(
                    PushSegment(
                        (40000, "203.0.113.9", 50000, 77),
                        sequence,
                        10.0 + (sequence - 100) / 10,
                        ciphertext[sequence],
                    ),
                    now=1.0 + (sequence - 100) / 10,
                )
            )
        self.assertEqual(
            {item.sequence: item.plaintext for item in output}, plaintext
        )
        duplicate = stream.add(
            PushSegment(
                (40000, "203.0.113.9", 50000, 77),
                107,
                10.7,
                ciphertext[107],
            ),
            now=2.0,
        )
        self.assertEqual(duplicate, [])
        self.assertEqual(stream.counters["kcp_retransmissions"], 1)

    def test_gap_pauses_and_requests_resynchronization(self):
        ciphertext, states, _plaintext = encrypted_run(10, 8)
        stream = PassiveRc4Reassembler(
            required_validation=3, gap_wait_seconds=0.5
        )
        stream.install_anchor(anchor(states[10], 20.0), now=2.0)
        for sequence in (11, 12, 13, 15):
            stream.add(
                PushSegment(
                    (41000, "203.0.113.10", 51000, 88),
                    sequence,
                    20.0 + sequence / 100,
                    ciphertext[sequence],
                ),
                now=2.0,
            )
        gap = stream.pending_gap(now=2.1)
        self.assertIsNotNone(gap)
        self.assertEqual(gap["expected"], 14)
        self.assertFalse(stream.needs_resync(now=2.4))
        self.assertTrue(stream.needs_resync(now=2.7))


class NpcapProtocolTests(unittest.TestCase):
    def test_verified_lifecycle_method_ids_are_available(self):
        self.assertEqual(METHOD_ID_NAMES[86], "OnMsgEntityRelive")
        self.assertEqual(METHOD_ID_NAMES[87], "OnMsgEntityDead")
        self.assertEqual(METHOD_ID_NAMES[217], "OnMsgBeforeEnterNewSpace")
        self.assertEqual(METHOD_ID_NAMES[1067], "OnMsgAddBuffNew")

    def test_existing_decrypted_capture_replays(self):
        path = Path(
            ".codex-tmp/npcap-shadow-runs/"
            "shadow_20260909_124718_pid3608/decrypted_125238.jsonl"
        )
        if not path.is_file():
            self.skipTest("local Npcap evidence capture is unavailable")
        decoder = NpcapProtocolDecoder()
        methods = []
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                item = json.loads(line)
                methods.extend(
                    record["method"]
                    for record in decoder.feed_push(
                        base64.b64decode(item["plaintext_base64"]),
                        int(item["sequence"]),
                        float(item["timestamp_epoch"]),
                    )
                )
        self.assertIn("OnMsgDamageSyncV2", methods)
        self.assertIn("OnMsgHealSyncV2", methods)
        self.assertIn("RetCommonCombatStatisticsByTeam", methods)
        self.assertGreater(decoder.diagnostics.retained_records, 700)

        settlement_path = path.with_name("decrypted_post_gap.jsonl")
        settlement_methods = []
        settlement_decoder = NpcapProtocolDecoder()
        with settlement_path.open(encoding="utf-8") as stream:
            for line in stream:
                item = json.loads(line)
                settlement_methods.extend(
                    record["method"]
                    for record in settlement_decoder.feed_push(
                        base64.b64decode(item["plaintext_base64"]),
                        int(item["sequence"]),
                        float(item["timestamp_epoch"]),
                    )
                )
        self.assertIn(
            "OnMsgSettlementCombatStatistics", settlement_methods
        )


class NpcapNetworkBindingTests(unittest.TestCase):
    def test_wire_entity_binding_is_limited_to_npcap_records(self):
        parser = NetworkPacketParser()
        parser.process(
            {
                "method": "OnMsgEntityDead",
                "decoded_arguments": [],
                "script_entity": 55500000000001,
                "network_entity_id": 55500000000001,
                "capture_source": "npcap",
                "filetime_100ns": 1,
            }
        )
        self.assertEqual(
            parser.pointer_entities[55500000000001], 55500000000001
        )

        legacy = NetworkPacketParser()
        legacy.process(
            {
                "method": "OnMsgEntityDead",
                "decoded_arguments": [],
                "script_entity": 0x12345678,
                "filetime_100ns": 1,
            }
        )
        self.assertNotIn(0x12345678, legacy.pointer_entities)


if __name__ == "__main__":
    unittest.main()
