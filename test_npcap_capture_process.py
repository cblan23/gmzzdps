from __future__ import annotations

import base64
import json
import struct
import unittest
from pathlib import Path
from unittest.mock import patch

import msgpack

from network_state import NetworkPacketParser
from npcap_capture_process import (
    ALIGNMENT_RETRY_SECONDS,
    PROTOCOL_STALL_MIN_PUSHES,
    PROTOCOL_STALL_SECONDS,
    PassiveRc4Reassembler,
    PushSegment,
    _protocol_stream_stalled,
    _rc4_anchor_changed,
)
from npcap_key_state import Rc4Anchor, _region_priority as _rc4_region_priority
from npcap_protocol import (
    MAX_ZSTD_RECOVERY_FRAMES,
    METHOD_ID_NAMES,
    NpcapProtocolDecoder,
)
from npcap_rc4_decode import Rc4State
from npcap_zstd_state import _region_priority


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


class ZstdRegionOrderingTests(unittest.TestCase):
    def test_small_connection_heap_precedes_nearby_asset_heap(self):
        far_small = _region_priority(0x5961B0000, 64 * 1024)
        near_asset = _region_priority(0x10D010000, 8 * 1024 * 1024)
        self.assertLess(far_small, near_asset)

    def test_large_heaps_keep_address_band_tiebreaker(self):
        near = _region_priority(0x10D100000, 64 * 1024 * 1024)
        far = _region_priority(0x500000000, 64 * 1024 * 1024)
        self.assertLess(near, far)


class Rc4RegionOrderingTests(unittest.TestCase):
    def test_small_connection_heap_precedes_nearby_asset_heap(self):
        far_small = _rc4_region_priority(0x5961B0000, 64 * 1024)
        near_asset = _rc4_region_priority(0x10D010000, 8 * 1024 * 1024)
        self.assertLess(far_small, near_asset)

    def test_activity_comparison_detects_live_decrypt_state(self):
        before = anchor(initial_state(), 10.0)
        changed_state = initial_state()
        changed_state.forward(b"live packet")
        after = anchor(changed_state, 11.0)
        self.assertTrue(_rc4_anchor_changed(before, after))
        self.assertFalse(_rc4_anchor_changed(before, before))


class PassiveRc4ReassemblerTests(unittest.TestCase):
    def test_sparse_alignment_keeps_anchor_long_enough_for_validation(self):
        _ciphertext, states, _plaintext = encrypted_run(100, 4)
        stream = PassiveRc4Reassembler(required_validation=3)
        stream.install_anchor(anchor(states[100], 10.0), now=1.0)
        self.assertFalse(stream.alignment_stale(now=1.0 + 3.0))
        self.assertTrue(
            stream.alignment_stale(now=1.0 + ALIGNMENT_RETRY_SECONDS)
        )

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
        self.assertEqual(stream.alignment_boundary, 102)
        self.assertEqual(
            [item.sequence for item in output if item.after_anchor],
            [103, 104, 105, 106, 107],
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

    def test_aligned_stream_allows_frames_split_across_pushes(self):
        state = initial_state()
        plaintext = {
            sequence: valid_push(sequence) for sequence in range(100, 108)
        }
        plaintext[106] = b"split-frame-part-a"
        plaintext[107] = b"split-frame-part-b"
        ciphertext = {}
        states = {}
        for sequence in range(100, 108):
            ciphertext[sequence] = state.forward(plaintext[sequence])
            states[sequence] = state.clone()

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
        self.assertTrue(stream.aligned)

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

    def test_connection_history_can_be_cleared_after_scene_transition(self):
        ciphertext, states, _plaintext = encrypted_run(10, 5)
        stream = PassiveRc4Reassembler(required_validation=3)
        stream.install_anchor(anchor(states[10], 20.0), now=2.0)
        flow = (41000, "203.0.113.10", 51000, 88)
        for sequence in range(11, 15):
            stream.add(
                PushSegment(
                    flow,
                    sequence,
                    20.0 + sequence / 100,
                    ciphertext[sequence],
                ),
                now=2.0,
            )
        self.assertTrue(stream.flows)
        stream.invalidate(clear_history=True)
        self.assertFalse(stream.flows)
        self.assertFalse(stream.last_delivered)
        self.assertEqual(stream.counters["connection_history_resets"], 1)

    def test_protocol_stall_requires_time_and_push_thresholds(self):
        values = {
            "aligned": True,
            "pushes_without_frame": PROTOCOL_STALL_MIN_PUSHES,
            "last_frame_monotonic": 10.0,
            "now": 10.0 + PROTOCOL_STALL_SECONDS,
        }
        self.assertTrue(_protocol_stream_stalled(**values))
        self.assertFalse(
            _protocol_stream_stalled(
                **{
                    **values,
                    "pushes_without_frame": PROTOCOL_STALL_MIN_PUSHES - 1,
                }
            )
        )
        self.assertFalse(
            _protocol_stream_stalled(
                **{
                    **values,
                    "now": 10.0 + PROTOCOL_STALL_SECONDS - 0.01,
                }
            )
        )
        self.assertFalse(
            _protocol_stream_stalled(**{**values, "aligned": False})
        )


class NpcapProtocolTests(unittest.TestCase):
    @staticmethod
    def _rpc_message(method_id: int = 90) -> bytes:
        payload = msgpack.packb([{}, [123456, method_id, []]], use_bin_type=True)
        return struct.pack("<IH", len(payload) + 2, 18) + payload

    @staticmethod
    def _doraemon_frame(marker: int = 1, length: int = 13) -> bytes:
        return valid_push(marker, length)

    def test_native_snapshot_is_not_trusted_until_rpc_validation(self):
        rpc = self._rpc_message()

        class FakeNativeDecoder:
            def __init__(self, _snapshot):
                self.closed = False

            def decompress(self, _block):
                return rpc

            def close(self):
                self.closed = True

        with patch("npcap_protocol.NativeZstdDecoder", FakeNativeDecoder):
            decoder = NpcapProtocolDecoder()
            decoder.install_zstd_snapshot(object())
            records = decoder.feed_push(
                self._doraemon_frame(), 100, 10.0
            )

        self.assertEqual([record["method"] for record in records], ["OnMsgDamageSyncV2"])
        self.assertEqual(decoder.diagnostics.native_zstd_validations, 1)
        self.assertFalse(decoder.consume_state_resync_request())

    def test_native_snapshot_rejects_pseudo_plaintext(self):
        class FakeNativeDecoder:
            def __init__(self, _snapshot):
                self.closed = False

            def decompress(self, _block):
                return b"not-an-rpc"

            def close(self):
                self.closed = True

        records = []
        with patch("npcap_protocol.NativeZstdDecoder", FakeNativeDecoder):
            decoder = NpcapProtocolDecoder()
            decoder.install_zstd_snapshot(object())
            for sequence in range(MAX_ZSTD_RECOVERY_FRAMES):
                records.extend(
                    decoder.feed_push(
                        self._doraemon_frame(sequence),
                        sequence,
                        10.0 + sequence / 100.0,
                    )
                )

        self.assertEqual(records, [])
        self.assertEqual(decoder.diagnostics.native_zstd_rejections, 1)
        self.assertTrue(decoder.consume_state_resync_request())

    def test_named_rpc_envelope_is_retained_without_fake_entity_binding(self):
        payload = msgpack.packb(
            [
                {},
                [
                    "AQAAAOwNKLYHAAAA",
                    "OnSyncTeamGroupPropsForceRefresh",
                    ["member-token", 100, 100],
                ],
            ],
            use_bin_type=True,
        )
        rpc = struct.pack("<IH", len(payload) + 2, 12) + payload

        class FakeNativeDecoder:
            def __init__(self, _snapshot):
                pass

            def decompress(self, _block):
                return rpc

            def close(self):
                pass

        with patch("npcap_protocol.NativeZstdDecoder", FakeNativeDecoder):
            decoder = NpcapProtocolDecoder()
            decoder.install_zstd_snapshot(object())
            records = decoder.feed_push(
                self._doraemon_frame(), 100, 10.0
            )

        self.assertEqual(len(records), 1)
        self.assertEqual(
            records[0]["method"], "OnSyncTeamGroupPropsForceRefresh"
        )
        self.assertEqual(records[0]["script_entity"], 0)
        self.assertNotIn("npcap_method_id", records[0])

    def test_false_incomplete_named_header_does_not_block_later_rpc(self):
        false_header = struct.pack("<IH", 12_000_000, 12) + b"\x02junk"
        rpc = self._rpc_message()

        class FakeNativeDecoder:
            def __init__(self, _snapshot):
                pass

            def decompress(self, _block):
                return false_header + rpc

            def close(self):
                pass

        with patch("npcap_protocol.NativeZstdDecoder", FakeNativeDecoder):
            decoder = NpcapProtocolDecoder()
            decoder.install_zstd_snapshot(object())
            records = decoder.feed_push(
                self._doraemon_frame(), 100, 10.0
            )

        self.assertEqual([item["method"] for item in records], [
            "OnMsgDamageSyncV2"
        ])

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
