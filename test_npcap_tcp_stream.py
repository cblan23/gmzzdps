import unittest

from npcap_key_state import Rc4Anchor
from npcap_rc4_decode import Rc4State
from npcap_tcp_stream import PassiveTcpRc4Reassembler
from passive_transport import TransportPacket


def frame(marker, size=31):
    return (size*8-20).to_bytes(3, 'little')+bytes([marker])*(size-3)


def packet(data, sequence, *, flags=16, stamp=21_000_000_000):
    return TransportPacket('203.0.113.9', '10.0.0.1', 30000, 123,
                           data, 'tcp', stamp, stamp, sequence=sequence, flags=flags)


class PassiveTcpStreamTests(unittest.TestCase):
    def stream(self, state):
        stream = PassiveTcpRc4Reassembler()
        stream.install_anchor(Rc4Anchor(1, 20, 20, state.clone(), state.clone()), now=0)
        return stream

    def test_coalesced_frames_and_snapshot_inside_tcp_packet(self):
        cipher = Rc4State(0, 0, list(range(256)))
        before = cipher.forward(frame(99, 23))
        stream = self.stream(cipher)
        plain = frame(1)+frame(2, 19)+frame(3, 43)+frame(4)
        output = stream.add(packet(before+cipher.forward(plain), 1000), now=1)
        self.assertTrue(stream.aligned)
        self.assertEqual(b''.join(v.plaintext for v in output), plain)
        self.assertEqual(output[0].sequence, 23)

    def test_frames_split_across_tcp_segments_and_retransmissions(self):
        cipher = Rc4State(0, 0, list(range(256)))
        stream = self.stream(cipher)
        plain = frame(1)+frame(2)+frame(3)+frame(4)
        encrypted = cipher.forward(plain)
        output = []
        for start, end in ((0, 2), (2, 24), (24, 68), (68, 124)):
            output.extend(stream.add(packet(encrypted[start:end], 1000+start), now=1))
        self.assertTrue(stream.aligned)
        self.assertEqual(b''.join(v.plaintext for v in output), plain)
        self.assertEqual(stream.add(packet(encrypted, 1000), now=2), [])
        extra = frame(5)
        self.assertEqual(stream.add(packet(cipher.forward(extra), 1124), now=3)[0].plaintext, extra)

    def test_out_of_order_after_syn_recovers_without_guessing(self):
        cipher = Rc4State(0, 0, list(range(256)))
        stream = self.stream(cipher)
        plain = frame(1)+frame(2)+frame(3)
        encrypted = cipher.forward(plain)
        self.assertEqual(stream.add(packet(b'', 999, flags=18), now=0), [])
        self.assertEqual(stream.add(packet(encrypted[17:], 1017), now=0.1), [])
        output = stream.add(packet(encrypted[:17], 1000), now=0.2)
        self.assertEqual(b''.join(v.plaintext for v in output), plain)

    def test_conflicting_retransmission_stops_active_stream(self):
        cipher = Rc4State(0, 0, list(range(256)))
        stream = self.stream(cipher)
        encrypted = cipher.forward(frame(1)+frame(2)+frame(3))
        stream.add(packet(encrypted, 1000), now=1)
        self.assertEqual(stream.add(packet(b'bad', 1000), now=2), [])
        self.assertTrue(stream.needs_resync(now=2))

    def test_missing_tcp_bytes_are_not_skipped_or_decrypted(self):
        cipher = Rc4State(0, 0, list(range(256)))
        stream = self.stream(cipher)
        stream.add(packet(cipher.forward(frame(1)+frame(2)+frame(3)), 1000), now=1)
        self.assertEqual(stream.add(packet(b'missing-prefix', 1200), now=2), [])
        self.assertFalse(stream.needs_resync(now=3))
        self.assertTrue(stream.needs_resync(now=4.1))

    def test_different_candidate_can_use_same_captured_bytes(self):
        cipher = Rc4State(0, 0, list(range(256)))
        wrong = cipher.clone()
        wrong.forward(b'wrong')
        stream = self.stream(wrong)
        encrypted = cipher.forward(frame(1)+frame(2)+frame(3))
        self.assertEqual(stream.add(packet(encrypted[:80], 1000), now=1), [])
        self.assertFalse(stream.aligned)
        correct = Rc4State(0, 0, list(range(256)))
        stream.install_anchor(Rc4Anchor(1, 20, 20, correct, correct), now=2)
        output = stream.add(packet(encrypted[80:], 1080), now=2.1)
        self.assertTrue(stream.aligned)
        self.assertEqual(b''.join(v.plaintext for v in output), frame(1)+frame(2)+frame(3))

    def test_application_rejection_can_rotate_to_detached_candidate_without_losing_bytes(self):
        cipher = Rc4State(0, 0, list(range(256)))
        stream = self.stream(cipher)
        plain = frame(1)+frame(2)+frame(3)
        encrypted = cipher.forward(plain)
        stream.add(packet(encrypted, 1000), now=1)
        initial = Rc4State(0, 0, list(range(256)))
        stream.install_anchor(Rc4Anchor(2, 20, 20, initial, initial), now=1.1)
        extra = frame(4)
        output = stream.add(packet(cipher.forward(extra), 1093), now=1.2)
        self.assertEqual(b''.join(v.plaintext for v in output), plain+extra)

    def test_only_application_confirmation_releases_cipher_history(self):
        cipher = Rc4State(0, 0, list(range(256)))
        stream = self.stream(cipher)
        stream.add(packet(cipher.forward(frame(1)+frame(2)+frame(3)), 1000), now=1)
        self.assertGreater(len(stream.flows[stream.active_flow].data), 0)
        stream.confirm_alignment()
        self.assertFalse(stream.flows[stream.active_flow].data)
        extra = frame(4)
        self.assertEqual(stream.add(packet(cipher.forward(extra), 1093), now=1.1)[0].plaintext, extra)

    def test_new_syn_generation_is_detected_with_one_coalesced_packet(self):
        cipher = Rc4State(0, 0, list(range(256)))
        stream = self.stream(cipher)
        stream.add(packet(cipher.forward(frame(1)+frame(2)+frame(3)), 1000), now=1)
        stream.confirm_alignment()
        old = stream.active_flow
        stream.add(packet(b'', 8000, flags=18), now=1.1)
        new_cipher = Rc4State(0, 0, list(range(256)))
        stream.add(packet(new_cipher.forward(frame(1)+frame(2)+frame(3)), 8001), now=1.2)
        self.assertNotEqual(stream.fresh_alternate_flow(now=1.2), old)
        self.assertTrue(stream.needs_resync(now=1.2))


if __name__ == '__main__':
    unittest.main()
