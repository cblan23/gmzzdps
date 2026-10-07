from __future__ import annotations

import base64
import json
import struct
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import msgpack

from network_state import NetworkPacketParser
from npcap_capture_process import (
    ALIGNMENT_FAST_REJECT_PUSHES,
    ALIGNMENT_FAST_REJECT_SECONDS,
    ALIGNMENT_RETRY_SECONDS,
    ENTITY_METADATA_SIGNAL_METHODS,
    PROTOCOL_STALL_MIN_PUSHES,
    PROTOCOL_STALL_SECONDS,
    SCENE_TRANSITION_METHODS,
    PassiveRc4Reassembler,
    PassiveGameRc4Reassembler,
    PassiveStateError,
    PushSegment,
    _capture_forever,
    _bootstrap_initial_state,
    _locate_hinted_state_readers,
    _request_entity_metadata,
    _wait_for_new_connection,
    live_team_profile_tokens,
    _protocol_stream_stalled,
    _rc4_anchor_changed,
    _can_recover_stream_gap,
    STABLE_GAP_RECOVERY_SECONDS,
    _alternate_udp_state_candidate,
    GAMEPLAY_STREAM_METHODS,
)


class StreamGapRecoveryPolicyTests(unittest.TestCase):
    def test_stable_recovered_connection_can_recover_another_gap(self):
        ready_at = 100.0
        self.assertTrue(_can_recover_stream_gap(ready_at, 101.0, False))
        self.assertFalse(_can_recover_stream_gap(
            ready_at, ready_at + STABLE_GAP_RECOVERY_SECONDS - 1, True
        ))
        self.assertTrue(_can_recover_stream_gap(
            ready_at, ready_at + STABLE_GAP_RECOVERY_SECONDS, True
        ))
        self.assertFalse(_can_recover_stream_gap(0, 1000, False))

    def test_background_stream_keeps_alternate_gameplay_candidate(self):
        from npcap_bootstrap import FrozenSessionState

        ciphertext, states, _plaintext = encrypted_run(10, 8)
        stream = PassiveGameRc4Reassembler()
        old_flow = (41000, '203.0.113.1', 51000, 7, '10.0.0.1')
        live_flow = (42000, '203.0.113.2', 52000, 8, '10.0.0.1')
        stream.active = stream.udp
        stream.udp.active_flow = old_flow
        for sequence in range(11, 16):
            stream.udp.add(PushSegment(
                live_flow, sequence, 20.01 + (sequence - 11) * 0.01,
                ciphertext[sequence],
            ), now=5.0)
        readers = [
            FrozenSessionState(anchor(initial_state(), 20), None, 1),
            FrozenSessionState(anchor(states[10], 20), None, 2),
        ]

        self.assertEqual(
            _alternate_udp_state_candidate(stream, readers, 0, 5.0), 1
        )
        self.assertIsNone(
            _alternate_udp_state_candidate(stream, readers[:1], 0, 5.0)
        )
        self.assertNotIn('OnMsgUpdateCampWorldBidItemInfo', GAMEPLAY_STREAM_METHODS)
        self.assertIn('OnMsgDamageSyncV2', GAMEPLAY_STREAM_METHODS)
from npcap_key_state import Rc4Anchor, _region_priority as _rc4_region_priority
from npcap_protocol import (
    MAX_ZSTD_RECOVERY_FRAMES,
    METHOD_ID_NAMES,
    NpcapProtocolDecoder,
)
from npcap_rc4_decode import Rc4State
from npcap_method_tables import LOCAL_ROLE_RPC_METHODS
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


class LiveTeamProfileTokenTests(unittest.TestCase):
    def test_team_heartbeat_exposes_exact_recipient_and_member_tokens(self):
        self_token = "AQAAAOwNKLYHAAAA"
        teammate_token = "AQAAAOwNuopBAAAA"
        self.assertEqual(
            live_team_profile_tokens(
                {
                    "method": "OnUpdateTeamGroupMemberProps",
                    "npcap_recipient": self_token,
                    "decoded_arguments": [
                        teammate_token,
                        {"4": False, "5": 16_720.0, "6": 16_720.0},
                    ],
                }
            ),
            {self_token, teammate_token},
        )

    def test_unrelated_packet_cannot_seed_live_profile_polling(self):
        token = "AQAAAOwNuopBAAAA"
        self.assertEqual(
            live_team_profile_tokens(
                {
                    "method": "OnMsgChannelChat",
                    "npcap_recipient": token,
                    "decoded_arguments": [{"player_id": token}],
                }
            ),
            set(),
        )

    def test_current_hp_can_recover_entity_metadata_after_transport_resync(self):
        self.assertIn("OnMsgSyncCurrentHp", ENTITY_METADATA_SIGNAL_METHODS)

    def test_player_cast_target_is_preferred_metadata_candidate(self):
        reader = Mock()

        _request_entity_metadata(reader, {
            "method": "OnMsgCastSkillNew",
            "decoded_arguments": [86_061_010, 57_443_577_120_075],
            "filetime_100ns": 123,
        })

        reader.request.assert_called_once_with(
            57_443_577_120_075, 123, discovery="preferred"
        )

    def test_boss_cast_and_beaten_target_do_not_seed_metadata_scan(self):
        reader = Mock()

        _request_entity_metadata(reader, {
            "method": "OnMsgCastSkillNew",
            "decoded_arguments": [88_015_101, 57_443_577_120_079],
            "filetime_100ns": 123,
        })
        _request_entity_metadata(reader, {
            "method": "OnMsgBeatenSyncV2",
            "decoded_arguments": [57_284_129_032_273, 57_443_577_120_079],
            "filetime_100ns": 124,
        })

        reader.request.assert_not_called()

    def test_hp_and_heal_targets_are_single_scan_fallbacks(self):
        reader = Mock()

        _request_entity_metadata(reader, {
            "method": "OnMsgSyncCurrentHp",
            "network_entity_id": 57_443_577_120_075,
            "decoded_arguments": [149_720_213.0],
            "filetime_100ns": 123,
        })
        _request_entity_metadata(reader, {
            "method": "OnMsgHealSyncV2",
            "decoded_arguments": [
                57_284_129_032_273, 57_443_577_120_079, 800_200_042,
            ],
            "filetime_100ns": 124,
        })

        self.assertEqual(reader.request.call_count, 2)
        reader.request.assert_any_call(
            57_443_577_120_075, 123, discovery="fallback"
        )
        reader.request.assert_any_call(
            57_443_577_120_079, 124, discovery="fallback"
        )


class InitialBootstrapRetryTests(unittest.TestCase):
    def test_valid_address_hint_skips_full_state_scan(self):
        endpoint = SimpleNamespace(local_port=4321)
        receiver = Mock(endpoints=[endpoint])
        snapshots = [object()]

        def bootstrap(pid, locate, _coherent):
            readers, diagnostic = locate(pid)
            self.assertEqual(readers, ["hinted"])
            return snapshots, diagnostic

        with (
            patch(
                "npcap_capture_process._locate_hinted_state_readers",
                return_value=(["hinted"], {"address_hint_hits": 1}),
            ) as hinted,
            patch("npcap_capture_process._locate_state_readers") as full_scan,
            patch(
                "npcap_capture_process.bootstrap_copies", side_effect=bootstrap
            ),
        ):
            result, diagnostic, endpoints = _bootstrap_initial_state(
                123,
                receiver,
                threading.Event(),
                Mock(is_alive=Mock(return_value=True)),
                SimpleNamespace(value=time.time() + 60),
                object(),
                [(123, 0x10, 0x20, 0x30)],
            )

        self.assertEqual(result, snapshots)
        self.assertEqual(endpoints, [endpoint])
        self.assertTrue(diagnostic["address_hint_fast_path"])
        hinted.assert_called_once()
        full_scan.assert_not_called()

    def test_stale_address_hint_falls_back_to_full_state_scan(self):
        endpoint = SimpleNamespace(local_port=4321)
        receiver = Mock(endpoints=[endpoint])
        snapshots = [object()]

        def bootstrap(pid, locate, _coherent):
            readers, diagnostic = locate(pid)
            if not readers:
                raise PassiveStateError("stale address hint")
            return snapshots, diagnostic

        with (
            patch(
                "npcap_capture_process._locate_hinted_state_readers",
                return_value=([], {"address_hint_hits": 0}),
            ) as hinted,
            patch(
                "npcap_capture_process._locate_state_readers",
                return_value=(["full"], {"paired_candidates": 1}),
            ) as full_scan,
            patch(
                "npcap_capture_process.bootstrap_copies", side_effect=bootstrap
            ) as copies,
        ):
            result, diagnostic, endpoints = _bootstrap_initial_state(
                123,
                receiver,
                threading.Event(),
                Mock(is_alive=Mock(return_value=True)),
                SimpleNamespace(value=time.time() + 60),
                object(),
                [(123, 0x10, 0x20, 0x30)],
            )

        self.assertEqual(result, snapshots)
        self.assertEqual(endpoints, [endpoint])
        self.assertNotIn("address_hint_fast_path", diagnostic)
        self.assertEqual(copies.call_count, 2)
        hinted.assert_called_once()
        full_scan.assert_called_once_with(123)

    def test_address_hints_are_scoped_to_the_game_pid(self):
        with patch("npcap_capture_process.proc_inspect.find_module") as find_module:
            readers, diagnostic = _locate_hinted_state_readers(
                456, [(123, 0x10, 0x20, 0x30)]
            )

        self.assertEqual(readers, [])
        self.assertEqual(diagnostic["address_hint_candidates"], 0)
        find_module.assert_not_called()

    def test_program_first_game_second_retries_until_state_exists(self):
        endpoint = SimpleNamespace(local_port=4321)
        receiver = Mock(endpoints=[endpoint])
        snapshots = [object()]
        stop = threading.Event()
        messages = []
        with (
            patch(
                "npcap_capture_process.bootstrap_copies",
                side_effect=[PassiveStateError("not ready"), (snapshots, {"paired_candidates": 1})],
            ) as bootstrap,
            patch(
                "npcap_capture_process.shadow_capture.list_game_endpoints",
                return_value=[endpoint],
            ),
            patch("npcap_capture_process._interruptible_wait", return_value=False),
            patch(
                "npcap_capture_process._put",
                side_effect=lambda _queue, kind, payload=None: messages.append((kind, payload)),
            ),
        ):
            result, diagnostic, endpoints = _bootstrap_initial_state(
                123,
                receiver,
                stop,
                Mock(is_alive=Mock(return_value=True)),
                SimpleNamespace(value=time.time() + 60),
                object(),
            )
        self.assertEqual(result, snapshots)
        self.assertEqual(endpoints, [endpoint])
        self.assertEqual(bootstrap.call_count, 2)
        receiver.refresh.assert_called_once_with([endpoint])
        self.assertEqual(diagnostic["bootstrap_attempts_for_connection"], 2)
        self.assertEqual(diagnostic["same_connection_memory_rescans"], 1)
        self.assertTrue(any(
            kind == "state" and payload["stage"] == "npcap_waiting_connection_state"
            for kind, payload in messages
        ))

    def test_handle_cleanup_failure_is_not_retried(self):
        receiver = Mock(endpoints=[])
        with (
            patch(
                "npcap_capture_process.bootstrap_copies",
                side_effect=PassiveStateError("Read-only initialization handle cleanup failed"),
            ) as bootstrap,
            patch("npcap_capture_process._interruptible_wait") as wait,
        ):
            with self.assertRaisesRegex(PassiveStateError, "cleanup failed"):
                _bootstrap_initial_state(
                    123,
                    receiver,
                    threading.Event(),
                    Mock(is_alive=Mock(return_value=True)),
                    SimpleNamespace(value=time.time() + 60),
                    object(),
                )
        bootstrap.assert_called_once()
        wait.assert_not_called()

    def test_periodic_startup_recovery_uses_only_one_bounded_scan(self):
        receiver = Mock(endpoints=[])
        with patch(
            "npcap_capture_process.bootstrap_copies",
            side_effect=PassiveStateError("No coherent connection state"),
        ) as bootstrap:
            with self.assertRaisesRegex(PassiveStateError, "No coherent"):
                _bootstrap_initial_state(
                    123, receiver, threading.Event(),
                    Mock(is_alive=Mock(return_value=True)),
                    SimpleNamespace(value=time.time() + 60),
                    object(), max_attempts=1,
                )
        bootstrap.assert_called_once()


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
    def test_buffered_pre_snapshot_pushes_do_not_reject_live_anchor(self):
        ciphertext, states, plaintext = encrypted_run(100, 30)
        stream = PassiveRc4Reassembler(required_validation=3)
        flow = (41000, "203.0.113.10", 51000, 88)
        stream.install_anchor(anchor(states[119], 20.0), now=1.0)

        for sequence in range(100, 112):
            stream.add(
                PushSegment(
                    flow,
                    sequence,
                    19.0 + (sequence - 100) / 100,
                    ciphertext[sequence],
                ),
                now=1.3,
            )

        self.assertEqual(stream.alignment_new_unique_pushes, 0)
        self.assertFalse(stream.alignment_stale(now=1.3))

        output = []
        for sequence in range(112, 123):
            output.extend(
                stream.add(
                    PushSegment(
                        flow,
                        sequence,
                        19.2 if sequence <= 119 else 20.01 + sequence / 1000,
                        ciphertext[sequence],
                    ),
                    now=1.4,
                )
            )

        self.assertTrue(stream.aligned)
        self.assertEqual(stream.alignment_boundary, 119)
        self.assertEqual(
            {item.sequence: item.plaintext for item in output},
            {sequence: plaintext[sequence] for sequence in range(100, 123)},
        )

    def test_sparse_alignment_keeps_anchor_long_enough_for_validation(self):
        _ciphertext, states, _plaintext = encrypted_run(100, 4)
        stream = PassiveRc4Reassembler(required_validation=3)
        stream.install_anchor(anchor(states[100], 10.0), now=1.0)
        self.assertFalse(stream.alignment_stale(now=1.0 + 3.0))
        self.assertFalse(
            stream.alignment_stale(now=1.0 + ALIGNMENT_RETRY_SECONDS)
        )
        self.assertFalse(stream.alignment_stale(now=1.0 + 3600))

    def test_sparse_wrong_candidate_expires_only_after_actual_push_evidence(self):
        ciphertext, _states, _plain = encrypted_run(10, 4)
        stream = PassiveRc4Reassembler()
        stream.install_anchor(anchor(initial_state(), 20), now=0.0)
        for sequence in range(11, 14):
            stream.add(PushSegment((41000, '203.0.113.10', 51000, 88),
                sequence, 20.01, ciphertext[sequence]), now=0.1)
        self.assertTrue(stream.alignment_stale(now=ALIGNMENT_RETRY_SECONDS))

    def test_busy_wrong_candidate_is_rejected_without_fifteen_second_freeze(self):
        ciphertext, _states, _plaintext = encrypted_run(
            10, ALIGNMENT_FAST_REJECT_PUSHES + 4
        )
        wrong_state = initial_state()
        wrong_state.s[0], wrong_state.s[1] = (
            wrong_state.s[1],
            wrong_state.s[0],
        )
        wrong_state.x = 97
        wrong_state.y = 211
        stream = PassiveRc4Reassembler(required_validation=3)
        flow = (41000, "203.0.113.10", 51000, 88)
        stream.install_anchor(anchor(wrong_state, 20.0), now=1.0)

        for offset, sequence in enumerate(
            range(11, 11 + ALIGNMENT_FAST_REJECT_PUSHES)
        ):
            stream.add(
                PushSegment(
                    flow,
                    sequence,
                    20.01 + offset / 100,
                    ciphertext[sequence],
                ),
                now=1.01 + offset / 100,
            )

        self.assertFalse(stream.aligned)
        # Diagnostic counters are drained once per second in production; the
        # rejection evidence must remain session-local and survive that drain.
        stream.counters.clear()
        self.assertFalse(
            stream.alignment_stale(
                now=1.0 + ALIGNMENT_FAST_REJECT_SECONDS - 0.001
            )
        )
        self.assertTrue(
            stream.alignment_stale(
                now=1.0 + ALIGNMENT_FAST_REJECT_SECONDS
            )
        )
        self.assertLess(ALIGNMENT_FAST_REJECT_SECONDS, ALIGNMENT_RETRY_SECONDS)

    def test_busy_valid_candidate_aligns_before_fast_rejection(self):
        ciphertext, states, _plaintext = encrypted_run(
            10, ALIGNMENT_FAST_REJECT_PUSHES + 4
        )
        stream = PassiveRc4Reassembler(required_validation=3)
        flow = (41000, "203.0.113.10", 51000, 88)
        stream.install_anchor(anchor(states[10], 20.0), now=1.0)

        for offset, sequence in enumerate(range(11, 15)):
            stream.add(
                PushSegment(
                    flow,
                    sequence,
                    20.01 + offset / 100,
                    ciphertext[sequence],
                ),
                now=1.01 + offset / 100,
            )

        self.assertTrue(stream.aligned)
        self.assertFalse(stream.alignment_stale(now=2.0))

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
        self.assertIsNone(stream.fresh_alternate_flow(now=2.7))

    def test_stale_active_flow_identifies_a_fresh_replacement_connection(self):
        ciphertext, states, _plaintext = encrypted_run(10, 8)
        stream = PassiveRc4Reassembler(
            required_validation=3,
            gap_wait_seconds=0.1,
            flow_switch_wait_seconds=0.5,
        )
        old_flow = (41000, "203.0.113.10", 51000, 88)
        new_flow = (42000, "203.0.113.10", 51000, 99)
        stream.install_anchor(anchor(states[10], 20.0), now=1.0)
        for sequence in range(11, 15):
            stream.add(
                PushSegment(
                    old_flow,
                    sequence,
                    20.0 + sequence / 100,
                    ciphertext[sequence],
                ),
                now=1.0,
            )
        self.assertEqual(stream.active_flow, old_flow)

        for sequence in range(1, 4):
            stream.add(
                PushSegment(
                    new_flow,
                    sequence,
                    30.0 + sequence / 100,
                    b"new-connection-" + bytes([sequence]),
                ),
                now=2.0,
            )

        self.assertTrue(stream.needs_resync(now=2.1))
        self.assertEqual(stream.fresh_alternate_flow(now=2.1), new_flow)
        self.assertIsNone(stream.pending_gap(now=2.1))

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


class KcpWrapTests(unittest.TestCase):
    def test_sequence_wrap_and_late_retransmission(self):
        first = 0xfffffffc
        ciphertext, states, plaintext = encrypted_run(first, 9)
        stream = PassiveRc4Reassembler()
        stream.install_anchor(anchor(states[first], 10.0))
        flow = (41000, '203.0.113.10', 51000, 88)
        output = []
        for sequence in range(first + 1, first + 9):
            output.extend(stream.add(PushSegment(flow, sequence & 0xffffffff,
                                                10.0 + (sequence-first)/100, ciphertext[sequence])))
        self.assertEqual([item.sequence for item in output], list(range(first+1, first+9)))
        self.assertEqual([item.plaintext for item in output], [plaintext[key] for key in range(first+1, first+9)])
        stream.flows[flow].pop(first+1)
        self.assertEqual(stream.add(PushSegment(flow, first+1, 11.0, ciphertext[first+1])), [])
        self.assertEqual(stream.counters['kcp_old_retransmissions'], 1)


class NpcapProtocolTests(unittest.TestCase):
    @staticmethod
    def _append_numeric_rpc(decoder, entity_id, method_id, arguments):
        payload = msgpack.packb(
            [{}, [entity_id, method_id, arguments]], use_bin_type=True
        )
        decoder._append_application(
            struct.pack('<IH', len(payload) + 2, 22) + payload,
            method_id,
            10.0,
        )

    def test_current_method_93_damage_shape_is_accepted(self):
        attacker = 57_223_461_897_621
        arguments = [
            attacker,
            57_463_441_195_725,
            8_602_107_000_361,
            1,
            2,
            1_041,
            0,
            1_041,
            False,
        ]
        decoder = NpcapProtocolDecoder(capture_unknown=True)

        self._append_numeric_rpc(decoder, attacker, 93, arguments)
        records = decoder._rpc_records()

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]['method'], 'OnMsgDamageSyncV2')
        self.assertEqual(records[0]['decoded_arguments'], arguments)
        self.assertEqual(records[0]['npcap_method_id'], 93)
        self.assertEqual(records[0]['npcap_method_scope'], 'damage_shape_compat')
        self.assertEqual(list(decoder.unknown_records), [])

    def test_current_method_93_dispatched_on_target_is_accepted(self):
        attacker = 57_223_461_897_621
        target = 57_463_441_195_725
        arguments = [
            attacker,
            target,
            8_602_107_000_361,
            1,
            2,
            1_041,
            0,
            1_041,
            False,
        ]
        decoder = NpcapProtocolDecoder(capture_unknown=True)

        self._append_numeric_rpc(decoder, target, 93, arguments)
        records = decoder._rpc_records()

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]['method'], 'OnMsgDamageSyncV2')
        self.assertEqual(records[0]['decoded_arguments'], arguments)
        self.assertEqual(records[0]['npcap_method_scope'], 'damage_shape_compat')
        self.assertEqual(list(decoder.unknown_records), [])

    def test_method_93_with_unrelated_shape_remains_unknown(self):
        attacker = 57_223_461_897_621
        decoder = NpcapProtocolDecoder(capture_unknown=True)

        self._append_numeric_rpc(
            decoder,
            attacker,
            93,
            [attacker + 1, 57_463_441_195_725, 8_602_107_000_361,
             1, 2, 1_041, 0, 1_041, False],
        )

        self.assertEqual(decoder._rpc_records(), [])
        self.assertEqual([row['method_id'] for row in decoder.unknown_records], [93])

    def test_unverified_numeric_lifecycle_never_emits_death_or_revive(self):
        decoder = NpcapProtocolDecoder(capture_unknown=True)
        for method_id, arguments in ((86, [57226681286372, 3600.0]), (87, [])):
            payload = msgpack.packb([{}, [57226681284911, method_id, arguments]], use_bin_type=True)
            decoder._append_application(struct.pack('<IH', len(payload)+2, 22)+payload, method_id, 10.0)
        self.assertEqual(decoder._rpc_records(), [])
        self.assertEqual({record['method_id'] for record in decoder.unknown_records}, {86, 87})
        self.assertTrue(
            all(record['event_time'] for record in decoder.unknown_records)
        )
        self.assertTrue(
            all('npcap_kcp_sequence' in record for record in decoder.unknown_records)
        )

    def test_unknown_timeline_keeps_repeated_occurrences_with_timestamps(self):
        decoder = NpcapProtocolDecoder(
            stream_id='unknown-timeline',
            capture_unknown=True,
            capture_unknown_timeline=True,
        )
        for sequence, timestamp in ((7, 10.0), (8, 10.25)):
            payload = msgpack.packb(
                [{}, [57226681284911, 86, [sequence]]], use_bin_type=True
            )
            decoder._append_application(
                struct.pack('<IH', len(payload) + 2, 22) + payload,
                sequence,
                timestamp,
            )
        self.assertEqual(decoder._rpc_records(), [])
        records = list(decoder.unknown_records)
        self.assertEqual([record['method_id'] for record in records], [86, 86])
        self.assertEqual(
            [record['npcap_kcp_sequence'] for record in records], [7, 8]
        )
        self.assertEqual(
            [record['capture_timestamp_ns'] for record in records],
            [10_000_000_000, 10_250_000_000],
        )

    def test_targeted_unknown_method_keeps_timeline_without_global_capture(self):
        decoder = NpcapProtocolDecoder(
            stream_id='targeted-unknown',
            capture_unknown=True,
            capture_unknown_method_ids=(803,),
        )
        for sequence in (7, 8):
            payload = msgpack.packb(
                [{}, [57226681284911, 803, [sequence]]], use_bin_type=True
            )
            decoder._append_application(
                struct.pack('<IH', len(payload) + 2, 22) + payload,
                sequence,
                10.0 + sequence,
            )
        for sequence in (9, 10):
            payload = msgpack.packb(
                [{}, [57226681284911, 804, [sequence]]], use_bin_type=True
            )
            decoder._append_application(
                struct.pack('<IH', len(payload) + 2, 22) + payload,
                sequence,
                10.0 + sequence,
            )

        self.assertEqual(decoder._rpc_records(), [])
        self.assertEqual(
            [record['method_id'] for record in decoder.unknown_records],
            [803, 803, 804],
        )

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

    def test_identical_rpc_events_are_distinct_and_keep_integer_timestamp(self):
        rpc = self._rpc_message()

        class FakeNativeDecoder:
            def __init__(self, _snapshot):
                pass

            def decompress(self, _block):
                return rpc + rpc

            def close(self):
                pass

        timestamp_ns = 1_789_100_000_123_456_789
        with patch('npcap_protocol.NativeZstdDecoder', FakeNativeDecoder):
            decoder = NpcapProtocolDecoder(stream_id='replay-session')
            decoder.install_zstd_snapshot(object())
            records = decoder.feed_push(self._doraemon_frame(), 100, timestamp_ns/1e9,
                                        timestamp_ns=timestamp_ns)
        self.assertEqual(len(records), 2)
        self.assertNotEqual(records[0]['capture_event_id'], records[1]['capture_event_id'])
        self.assertEqual(records[0]['capture_timestamp_ns'], timestamp_ns)
        self.assertEqual(records[0]['filetime_100ns'], 116444736000000000 + timestamp_ns//100)

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
        self.assertNotIn(86, METHOD_ID_NAMES)
        self.assertNotIn(87, METHOD_ID_NAMES)
        self.assertEqual(LOCAL_ROLE_RPC_METHODS[209], "OnCreateTeamSuccess")
        self.assertEqual(LOCAL_ROLE_RPC_METHODS[213], "OnMsgNewTeamGroupApply")
        self.assertEqual(LOCAL_ROLE_RPC_METHODS[219], "OnUpdateTeamGroupMemberProps")
        self.assertEqual(LOCAL_ROLE_RPC_METHODS[220], "OnUpdateTeamGroupSelfProps")
        self.assertEqual(LOCAL_ROLE_RPC_METHODS[221], "OnMsgOtherJoinTeamGroup")
        self.assertEqual(LOCAL_ROLE_RPC_METHODS[224], "OnMsgSyncWorldReturnInfo")
        self.assertEqual(LOCAL_ROLE_RPC_METHODS[345], "RetCommonCombatStatisticsByTeam")
        self.assertEqual(LOCAL_ROLE_RPC_METHODS[346], "RetDirtyCommonCombatStatisticsByTeam")
        self.assertEqual(LOCAL_ROLE_RPC_METHODS[348], "OnMsgUpdateStageCombatStatistics")
        self.assertEqual(LOCAL_ROLE_RPC_METHODS[349], "OnMsgSettlementCombatStatistics")
        self.assertEqual(LOCAL_ROLE_RPC_METHODS[389], "OnMsgBeforeEnterNewSpace")
        self.assertEqual(LOCAL_ROLE_RPC_METHODS[1441], "OnMsgLeaveQuestControl")
        self.assertIn("OnMsgLeaveQuestControl", SCENE_TRANSITION_METHODS)
        self.assertEqual(METHOD_ID_NAMES[1067], "OnMsgAddBuffNew")

    def test_current_team_and_settlement_ids_decode_to_existing_handlers(self):
        decoder = NpcapProtocolDecoder()
        expected = {
            209: "OnCreateTeamSuccess",
            219: "OnUpdateTeamGroupMemberProps",
            220: "OnUpdateTeamGroupSelfProps",
            221: "OnMsgOtherJoinTeamGroup",
            345: "RetCommonCombatStatisticsByTeam",
            346: "RetDirtyCommonCombatStatisticsByTeam",
            348: "OnMsgUpdateStageCombatStatistics",
            349: "OnMsgSettlementCombatStatistics",
        }
        for method_id in expected:
            self._append_numeric_rpc(
                decoder, 57_223_461_897_621, method_id, []
            )

        records = decoder._rpc_records()

        self.assertEqual(
            [(record["npcap_method_id"], record["method"]) for record in records],
            list(expected.items()),
        )

    def test_existing_decrypted_capture_transport_replays_with_current_schema(self):
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
        # The fixture predates the 2026-09-17 descriptor shift. Its numeric
        # callbacks are intentionally interpreted with the current production
        # table; this test covers stream recovery rather than legacy RPC names.
        self.assertIn("OnMsgUpdateDungeonTeamPlayerBattleStatistics", methods)
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
        self.assertIn("RetNpcCombatStatisticsByTeam", settlement_methods)


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


class NpcapCaptureRecoveryTests(unittest.TestCase):
    def test_confirmed_stream_gap_retries_once_with_a_new_session(self):
        stop = threading.Event()
        attempts = []
        messages = []

        def session(*args, **kwargs):
            attempts.append((args[6], kwargs['same_connection_recovery_used']))
            if len(attempts) == 1:
                return 'recoverable_gap'
            self.assertTrue(kwargs['same_connection_recovery_used'])
            stop.set()
            return 'stopped'

        with (
            patch('npcap_capture_process.os.name', 'nt'),
            patch('npcap_capture_process.shadow_capture.load_wpcap', return_value=object()),
            patch('npcap_capture_process._session', side_effect=session),
            patch('npcap_capture_process._interruptible_wait'),
            patch('npcap_capture_process._put', side_effect=lambda _queue, kind, value=None: messages.append((kind, value))),
        ):
            _capture_forever(
                stop, object(), Mock(is_alive=lambda: True),
                SimpleNamespace(value=time.time() + 60), 0,
            )

        self.assertEqual(attempts, [(1, False), (2, True)])
        self.assertTrue(any(kind == 'session_closed' for kind, _ in messages))

    def test_watcher_accepts_new_udp_flow_seen_just_before_gap(self):
        from passive_transport import TransportPacket

        stop = threading.Event()
        stream = PassiveGameRc4Reassembler()
        old = (41000, '203.0.113.10', 30000, 8, '10.0.0.1')
        new = (41000, '203.0.113.10', 30000, 9, '10.0.0.1')
        stream.active = stream.udp
        stream.udp.active_flow = old
        stream.udp.flows[old] = {}
        stream.udp.flows[new] = {}
        stream.udp.last_seen_monotonic[old] = 1.0
        stream.udp.last_seen_monotonic[new] = 2.0
        receiver = Mock(endpoints=[], local_addresses={'10.0.0.1'})
        receiver.next_frame.return_value = object()
        packet = TransportPacket('203.0.113.10', '10.0.0.1', 30000,
                                 41000, b'cipher', 'udp', 0, 0)
        packets = Mock(feed=Mock(return_value=packet))
        pushes = iter({'command': 'PUSH', 'conv': 9, 'sequence': sequence}
                      for sequence in (20, 21, 22))
        with (
            patch('npcap_capture_process.shadow_capture.list_game_endpoints', return_value=[]),
            patch('npcap_capture_process.shadow_capture.process_path', return_value='game.exe'),
            patch('npcap_capture_process.endpoint_direction', return_value=('10.0.0.1', 41000, '203.0.113.10', 30000, 'inbound')),
            patch('npcap_capture_process.shadow_capture.parse_kcp_segments', side_effect=lambda _payload: [next(pushes)]),
            patch('npcap_capture_process._put'),
        ):
            reason = _wait_for_new_connection(
                stop, object(), Mock(is_alive=lambda: True),
                SimpleNamespace(value=time.time() + 60), receiver, packets,
                stream, 1, 1,
            )
        self.assertEqual(reason, 'connection_changed')
        self.assertEqual(packets.feed.call_count, 3)

    def test_initial_state_retries_same_connection_without_new_traffic(self):
        receiver = Mock(endpoints=[], local_addresses=set())
        receiver.next_frame.return_value = None
        stop = threading.Event()
        with (
            patch('npcap_capture_process.INITIAL_STATE_RECOVERY_SECONDS', 0),
            patch('npcap_capture_process.shadow_capture.list_game_endpoints', return_value=[]),
            patch('npcap_capture_process.shadow_capture.process_path', return_value='game.exe'),
            patch('npcap_capture_process._put') as send,
            patch('npcap_capture_process.bootstrap_copies') as bootstrap,
        ):
            reason = _wait_for_new_connection(
                stop, object(), Mock(is_alive=lambda: True),
                SimpleNamespace(value=time.time() + 60),
                receiver, Mock(), PassiveGameRc4Reassembler(), 123, 1,
                retry_initial_state=True,
            )
        self.assertEqual(reason, 'retry')
        receiver.next_frame.assert_not_called()
        bootstrap.assert_not_called()
        self.assertEqual(send.call_args_list[0].args[2]['stage'],
                         'npcap_waiting_connection_state')

    def test_startup_retry_keeps_session_id_until_a_real_connection(self):
        stop = threading.Event()
        session_ids, retry_flags = [], []

        def session(*args, **kwargs):
            session_ids.append(args[6])
            retry_flags.append(kwargs['startup_retry'])
            if len(session_ids) == 1:
                return 'retry'
            stop.set()
            return 'stopped'

        with (
            patch('npcap_capture_process.os.name', 'nt'),
            patch('npcap_capture_process._session', side_effect=session),
            patch('npcap_capture_process._put'),
            patch('npcap_capture_process._interruptible_wait'),
        ):
            _capture_forever(
                stop, object(), Mock(is_alive=lambda: True),
                SimpleNamespace(value=time.time() + 60), 0,
            )
        self.assertEqual(session_ids, [1, 1])
        self.assertEqual(retry_flags, [False, True])

    def test_unwatched_passive_state_exhaustion_never_rebootstraps_same_connection(self):
        stop = threading.Event()
        messages = []
        session_ids = []

        def session(*args, **_kwargs):
            session_id = int(args[6])
            session_ids.append(session_id)
            if len(session_ids) == 1:
                raise PassiveStateError('Copied candidates exhausted')
            stop.set()
            return 'stopped'

        with (
            patch('npcap_capture_process.os.name', 'nt'),
            patch('npcap_capture_process.shadow_capture.load_wpcap', return_value=object()),
            patch('npcap_capture_process._session', side_effect=session),
            patch('npcap_capture_process._interruptible_wait', return_value=False),
            patch(
                'npcap_capture_process._put',
                side_effect=lambda _queue, kind, payload=None: messages.append(
                    (kind, payload)
                ),
            ),
        ):
            _capture_forever(
                stop,
                object(),
                Mock(is_alive=Mock(return_value=True)),
                SimpleNamespace(value=time.time() + 60),
                0,
            )

        self.assertEqual(session_ids, [1])
        self.assertTrue(any(kind == 'capture_error' for kind, _ in messages))
        self.assertTrue(any(kind == 'fatal' for kind, _ in messages))

    def test_real_connection_change_starts_exactly_one_new_session(self):
        stop = threading.Event()
        ids = []
        hint_lists = []
        def session(*args, **kwargs):
            ids.append(args[6])
            hints = kwargs["state_address_hints"]
            hint_lists.append(hints)
            if len(ids) == 1:
                hints.append((123, 0x10, 0x20, 0x30))
                return 'connection_changed'
            self.assertEqual(hints, [(123, 0x10, 0x20, 0x30)])
            stop.set()
            return 'stopped'
        with (patch('npcap_capture_process.os.name', 'nt'),
              patch('npcap_capture_process.shadow_capture.load_wpcap', return_value=object()),
              patch('npcap_capture_process._session', side_effect=session),
              patch('npcap_capture_process._put'),
              patch('npcap_capture_process._interruptible_wait') as wait):
            _capture_forever(stop, object(), Mock(is_alive=lambda: True),
                             SimpleNamespace(value=time.time()+60), 0)
        self.assertEqual(ids, [1, 2])
        self.assertIs(hint_lists[0], hint_lists[1])
        wait.assert_not_called()

    def test_game_reassembler_no_traffic_never_exhausts_snapshot(self):
        stream = PassiveGameRc4Reassembler()
        stream.install_anchor(anchor(initial_state(), 20), now=0)
        self.assertFalse(stream.alignment_stale(now=3600))

    def test_failed_connection_watches_without_any_memory_bootstrap(self):
        from npcap_shadow_capture import TcpEndpoint
        from passive_transport import TransportPacket
        stop = threading.Event()
        stream = PassiveGameRc4Reassembler()
        stream.active = stream.tcp
        stream.tcp.active_flow = (123, '203.0.113.10', 30000, ('tcp', 0), '10.0.0.1')
        endpoint = TcpEndpoint(1, '10.0.0.1', 123, '203.0.113.10', 30000)
        receiver = Mock(endpoints=[endpoint], local_addresses={'10.0.0.1'})
        count = 0
        def next_frame():
            nonlocal count
            count += 1
            if count == 101:
                stop.set()
                return None
            return object()
        receiver.next_frame.side_effect = next_frame
        packet = TransportPacket('203.0.113.10', '10.0.0.1', 30000, 123,
                                 b'old-ciphertext', 'tcp', 0, 0, sequence=100)
        with (patch('npcap_capture_process.shadow_capture.list_game_endpoints', return_value=[endpoint]),
              patch('npcap_capture_process.shadow_capture.process_path', return_value='game.exe'),
              patch('npcap_capture_process._put'),
              patch('npcap_capture_process.bootstrap_copies') as bootstrap):
            reason = _wait_for_new_connection(stop, object(), Mock(is_alive=lambda: True),
                SimpleNamespace(value=time.time()+60), receiver, Mock(feed=lambda _frame: packet),
                stream, 1, 1)
        self.assertEqual(reason, 'stopped')
        self.assertEqual(count, 101)
        bootstrap.assert_not_called()

    def test_passive_watcher_detects_new_tcp_endpoint_not_https_noise(self):
        from npcap_shadow_capture import TcpEndpoint
        from passive_transport import TransportPacket
        stop = threading.Event()
        stream = PassiveGameRc4Reassembler()
        stream.active = stream.tcp
        stream.tcp.active_flow = (123, '203.0.113.10', 30000, ('tcp', 0), '10.0.0.1')
        old = TcpEndpoint(1, '10.0.0.1', 123, '203.0.113.10', 30000)
        new = TcpEndpoint(1, '10.0.0.1', 124, '203.0.113.10', 30000)
        https = TcpEndpoint(1, '10.0.0.1', 125, '203.0.113.20', 443)
        receiver = Mock(endpoints=[old], local_addresses={'10.0.0.1'})
        receiver.refresh.side_effect = lambda rows: setattr(receiver, 'endpoints', rows)
        receiver.next_frame.return_value = object()
        def incoming(remote, remote_port, port, seq):
            return TransportPacket(remote, '10.0.0.1', remote_port, port, b'cipher', 'tcp', 0, 0, sequence=seq)
        packets = Mock(feed=Mock(side_effect=[
            incoming('203.0.113.20', 443, 125, 1),
            incoming('203.0.113.10', 30000, 124, 1),
            incoming('203.0.113.10', 30000, 124, 2),
        ]))
        with (patch('npcap_capture_process.shadow_capture.list_game_endpoints', return_value=[new, https]),
              patch('npcap_capture_process.shadow_capture.process_path', return_value='game.exe'),
              patch('npcap_capture_process._put'),
              patch('npcap_capture_process.bootstrap_copies') as bootstrap):
            reason = _wait_for_new_connection(stop, object(), Mock(is_alive=lambda: True),
                SimpleNamespace(value=time.time()+60), receiver, packets, stream, 1, 1)
        self.assertEqual(reason, 'connection_changed')
        self.assertEqual(packets.feed.call_count, 3)
        bootstrap.assert_not_called()


if __name__ == "__main__":
    unittest.main()
