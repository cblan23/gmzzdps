import socket
import struct
import unittest

from passive_transport import CaptureFrame, PacketReassembler, SequenceUnwrapper, TcpStreamReassembler, TransportPacket


def udp(data=b'abcdefghijklmno'):
    return struct.pack('!HHHH', 1234, 4321, len(data) + 8, 0) + data


def ipv4(payload, offset=0, more=False, identity=7):
    return struct.pack('!BBHHHBBH4s4s', 0x45, 0, 20+len(payload), identity,
                       offset//8 | (0x2000 if more else 0), 64, 17, 0,
                       socket.inet_aton('10.0.0.1'), socket.inet_aton('10.0.0.2')) + payload


def frame(data, stamp=100, interface='a', datalink=101):
    return CaptureFrame(data, stamp, interface, datalink)


def tcp(seq, data=b'', flags=16, stamp=100):
    return TransportPacket('10.0.0.1', '10.0.0.2', 123, 456, data, 'tcp', stamp, stamp,
                           sequence=seq, flags=flags)


class PacketTests(unittest.TestCase):
    def test_reordered_fragments_and_timestamp(self):
        parser = PacketReassembler()
        payload = udp()
        self.assertIsNone(parser.feed(frame(ipv4(payload[16:], 16), 200)))
        result = parser.feed(frame(ipv4(payload[:16], more=True), 100))
        self.assertEqual(result.payload, payload[8:])
        self.assertEqual((result.timestamp_ns, result.completed_ns), (100, 200))
        self.assertEqual(parser.buffered_bytes, 0)

    def test_duplicate_fragment(self):
        parser = PacketReassembler()
        first = frame(ipv4(udp()[:16], more=True))
        parser.feed(first)
        parser.feed(first)
        self.assertEqual(parser.diagnostics['duplicate_ip_fragments'], 1)
        self.assertEqual(parser.feed(frame(ipv4(udp()[16:], 16))).payload, udp()[8:])

    def test_conflicting_overlap_rejected(self):
        parser = PacketReassembler()
        parser.feed(frame(ipv4(udp()[:16], more=True)))
        self.assertIsNone(parser.feed(frame(ipv4(b'X'*15, 8))))
        self.assertEqual(parser.diagnostics['ip_fragment_conflicts'], 1)
        self.assertEqual(parser.buffered_bytes, 0)

    def test_fragment_timeout(self):
        parser = PacketReassembler(timeout_seconds=1)
        parser.feed(frame(ipv4(udp()[:16], more=True)), now_ns=0)
        parser.expire(1_000_000_000)
        self.assertEqual(parser.buffered_bytes, 0)
        self.assertEqual(parser.diagnostics['ip_fragment_timeouts'], 1)

    def test_conflict_cannot_be_revived_by_later_fragments(self):
        parser = PacketReassembler(timeout_seconds=1)
        first = frame(ipv4(udp()[:16], more=True))
        last = frame(ipv4(udp()[16:], 16))
        parser.feed(first, now_ns=0)
        parser.feed(frame(ipv4(b'X'*15, 8)), now_ns=1)
        self.assertIsNone(parser.feed(first, now_ns=2))
        self.assertIsNone(parser.feed(last, now_ns=3))
        parser.feed(first, now_ns=1_000_000_001)
        self.assertIsNotNone(parser.feed(last, now_ns=1_000_000_002))

    def test_interfaces_do_not_mix_fragments(self):
        parser = PacketReassembler()
        parser.feed(frame(ipv4(udp()[:16], more=True), interface='a'))
        self.assertIsNone(parser.feed(frame(ipv4(udp()[16:], 16), interface='b')))

    def test_truncated_ip_and_capture_rejected(self):
        parser = PacketReassembler()
        raw = ipv4(udp())
        self.assertIsNone(parser.feed(frame(raw[:-1])))
        self.assertIsNone(parser.feed(CaptureFrame(raw, 100, datalink=101, original_length=len(raw)+1)))

    def test_vlan_stack(self):
        raw = b'\x00'*12 + struct.pack('!HHHHH', 0x88a8, 1, 0x8100, 2, 0x0800) + ipv4(udp())
        self.assertEqual(PacketReassembler().feed(frame(raw, datalink=1)).payload, udp()[8:])

    def test_ipv6_fragments_after_extension(self):
        def packet(part, offset, more):
            fragment = struct.pack('!BBHI', 17, 0, offset | int(more), 99) + part
            extension = bytes([44, 0]) + b'\0'*6
            return struct.pack('!IHBB16s16s', 6 << 28, len(extension+fragment), 0, 64,
                               socket.inet_pton(socket.AF_INET6, '::1'),
                               socket.inet_pton(socket.AF_INET6, '::2')) + extension + fragment
        parser = PacketReassembler()
        parser.feed(frame(packet(udp()[16:], 16, False)))
        self.assertEqual(parser.feed(frame(packet(udp()[:16], 0, True))).payload, udp()[8:])

    def test_fragment_capacity_bounded(self):
        parser = PacketReassembler(max_datagrams=2)
        for identity in range(10):
            parser.feed(frame(ipv4(udp()[:16], more=True, identity=identity)))
        self.assertEqual(len(parser.fragments), 2)
        self.assertEqual(parser.buffered_bytes, 32)


class TcpTests(unittest.TestCase):
    def test_reorder_retransmit_and_real_repetition(self):
        parser = TcpStreamReassembler()
        parser.feed(tcp(100, flags=2))
        self.assertEqual(parser.feed(tcp(104, b'def', stamp=20)), [])
        output = parser.feed(tcp(101, b'abc', stamp=10))
        self.assertEqual(b''.join(chunk.data for chunk in output), b'abcdef')
        self.assertEqual([chunk.timestamp_ns for chunk in output], [10, 20])
        self.assertEqual(parser.feed(tcp(101, b'abcdef')), [])
        self.assertEqual(parser.feed(tcp(107, b'abcdef'))[0].data, b'abcdef')

    def test_wrap(self):
        parser = TcpStreamReassembler()
        parser.feed(tcp(0xfffffffd, flags=2))
        self.assertEqual(parser.feed(tcp(0xfffffffe, b'ab'))[0].offset, 0)
        self.assertEqual(parser.feed(tcp(0, b'cd'))[0].offset, 2)

    def test_conflicting_delivered_overlap(self):
        parser = TcpStreamReassembler()
        parser.feed(tcp(100, b'abc'))
        self.assertEqual(parser.feed(tcp(101, b'ZZd')), [])
        self.assertEqual(parser.feed(tcp(103, b'def')), [])
        self.assertEqual(parser.diagnostics['tcp_overlap_conflicts'], 1)

    def test_reconnect(self):
        parser = TcpStreamReassembler()
        parser.feed(tcp(100, flags=2))
        self.assertEqual(parser.feed(tcp(101, b'abc', flags=17))[0].generation, 0)
        parser.feed(tcp(500, flags=2))
        self.assertEqual(parser.feed(tcp(501, b'xyz'))[0].generation, 1)

    def test_gap_never_fabricates_bytes(self):
        parser = TcpStreamReassembler(gap_seconds=1)
        parser.feed(tcp(100, flags=2), now_ns=0)
        parser.feed(tcp(104, b'def'), now_ns=0)
        self.assertEqual(parser.feed(tcp(107, b'ghi'), now_ns=1_000_000_000), [])
        self.assertEqual(parser.diagnostics['tcp_gap_timeouts'], 1)
        self.assertEqual(parser.feed(tcp(101, b'abc')), [])

    def test_sequence_unwrap_accepts_old_packet(self):
        serial = SequenceUnwrapper()
        self.assertEqual(serial.unwrap(0xfffffffe), 0xfffffffe)
        self.assertEqual(serial.unwrap(1), 0x100000001)
        self.assertEqual(serial.unwrap(0xffffffff), 0xffffffff)
        self.assertEqual(serial.unwrap(2), 0x100000002)

    def test_gap_expires_without_another_packet(self):
        parser = TcpStreamReassembler(gap_seconds=1)
        parser.feed(tcp(100, flags=2), now_ns=0)
        parser.feed(tcp(104, b'def'), now_ns=0)
        parser.expire(1_000_000_000)
        self.assertEqual(parser.diagnostics['tcp_gap_timeouts'], 1)
        self.assertEqual(parser.feed(tcp(101, b'abc'), now_ns=1_000_000_001), [])


if __name__ == '__main__':
    unittest.main()
