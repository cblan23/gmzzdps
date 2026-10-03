"""Portable raw-capture contract tests: no real sockets, game, or Npcap needed."""
import collections
import socket
import struct
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from passive_transport import PacketReassembler
from windows_raw_receiver import (
    DLT_RAW, RCVALL_IPLEVEL, RCVALL_OFF, RCVALL_ON, SIO_RCVALL,
    RawSocketUnavailable, WindowsRawSocketReceiver, _transport_header,
    probe_windows_raw_socket, relevant_local_ipv4,
)


def endpoint(address="192.0.2.10", port=4321, protocol="udp", **kwargs):
    return SimpleNamespace(local_address=address, local_port=port, protocol=protocol, **kwargs)


def ipv4(payload, protocol=17, source="198.51.100.20", destination="192.0.2.10", fragment=0):
    return struct.pack("!BBHHHBBH4s4s", 0x45, 0, 20 + len(payload), 19, fragment,
                       64, protocol, 0, socket.inet_aton(source), socket.inet_aton(destination)) + payload


def udp(payload=b"game", source_port=1234, destination_port=4321, **kwargs):
    return ipv4(struct.pack("!HHHH", source_port, destination_port, 8 + len(payload), 0) + payload, **kwargs)


def tcp(payload=b"game", source_port=1234, destination_port=4321, **kwargs):
    return ipv4(struct.pack("!HHIIBBHHH", source_port, destination_port, 42, 9,
                            0x50, 0x18, 65535, 0, 0) + payload, protocol=6, **kwargs)


class FakeSocket:
    def __init__(self, frames=()):
        self.frames = collections.deque(frames)
        self.bind = Mock()
        self.ioctl = Mock()
        self.setblocking = Mock()
        self.setsockopt = Mock()
        self.close = Mock()

    def recvfrom(self, _size):
        if not self.frames:
            raise BlockingIOError()
        return self.frames.popleft(), ("198.51.100.20", 0)


def ready(sockets, _write, _error, _timeout):
    return [item for item in sockets if item.frames], [], []


class AddressSelectionTests(unittest.TestCase):
    def test_concrete_interface_does_not_probe_or_open_unrelated_adapters(self):
        provider = Mock(side_effect=AssertionError("should not enumerate"))
        result = relevant_local_ipv4([endpoint()], address_provider=provider)
        self.assertEqual(result, ("192.0.2.10",))

    def test_wildcard_includes_all_valid_ipv4_interfaces(self):
        result = relevant_local_ipv4([endpoint("0.0.0.0")], address_provider=lambda:
                                    ["0.0.0.0", "192.0.2.10", "10.8.0.2", "127.0.0.1", "224.0.0.1", "::1", "bad"])
        self.assertEqual(set(result), {"192.0.2.10", "10.8.0.2", "127.0.0.1"})

    def test_ipv6_is_not_silently_treated_as_ipv4(self):
        self.assertEqual(relevant_local_ipv4([endpoint("2001:db8::10")]), ())

    def test_dual_stack_wildcard_uses_available_ipv4_interfaces(self):
        result = relevant_local_ipv4(
            [endpoint("::")],
            address_provider=lambda: ["192.0.2.10", "10.8.0.2"],
        )
        self.assertEqual(set(result), {"192.0.2.10", "10.8.0.2"})

    def test_ipv4_mapped_ipv6_endpoint_uses_its_ipv4_interface(self):
        result = relevant_local_ipv4([endpoint("::ffff:192.0.2.10")])
        self.assertEqual(result, ("192.0.2.10",))


class RawReceiverTests(unittest.TestCase):
    def create(self, frames=(), endpoints=None, sockets=None):
        values = sockets or [FakeSocket(frames)]
        factory = Mock(side_effect=values)
        receiver = WindowsRawSocketReceiver(endpoints or [endpoint()], socket_factory=factory,
            address_provider=lambda: ["192.0.2.10"], select_function=ready, clock_ns=lambda: 123456789)
        self.addCleanup(receiver.close)
        return receiver, values, factory

    def test_socket_is_explicitly_bound_receive_only_at_ip_level(self):
        receiver, values, factory = self.create()
        factory.assert_called_once_with(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_IP)
        values[0].bind.assert_called_once_with(("192.0.2.10", 0))
        values[0].ioctl.assert_called_once_with(SIO_RCVALL, RCVALL_IPLEVEL)
        values[0].setblocking.assert_called_once_with(False)
        self.assertFalse(hasattr(receiver, "send"))
        self.assertFalse(hasattr(receiver, "sendto"))

    def test_ipv4_frame_has_no_fabricated_ethernet_header(self):
        raw = udp()
        receiver, _values, _factory = self.create([raw])
        frame = receiver.next_frame()
        self.assertEqual(frame.data, raw)
        self.assertEqual(frame.datalink, DLT_RAW)
        self.assertEqual(frame.original_length, len(raw))
        self.assertEqual(frame.timestamp_ns, 123456789)
        packet = PacketReassembler().feed(frame)
        self.assertEqual(packet.payload, b"game")
        self.assertEqual(packet.protocol, "udp")

    def test_owned_tcp_matches_remote_address_and_port(self):
        rows = [endpoint(protocol="tcp", remote_address="198.51.100.20", remote_port=1234)]
        receiver, _values, _factory = self.create([tcp()], rows)
        self.assertEqual(PacketReassembler().feed(receiver.next_frame()).sequence, 42)

    def test_tcp_wrong_remote_is_discarded(self):
        rows = [endpoint(protocol="tcp", remote_address="198.51.100.30", remote_port=1234)]
        receiver, _values, _factory = self.create([tcp()], rows)
        self.assertIsNone(receiver.next_frame())
        self.assertEqual(receiver.counters["raw_socket_filtered_packets"], 1)

    def test_upstream_is_captured(self):
        raw = udp(source="192.0.2.10", destination="198.51.100.20", source_port=4321, destination_port=1234)
        receiver, _values, _factory = self.create([raw])
        packet = PacketReassembler().feed(receiver.next_frame())
        self.assertEqual(packet.src_ip, "192.0.2.10")
        self.assertEqual(packet.src_port, 4321)

    def test_other_process_port_is_discarded(self):
        receiver, _values, _factory = self.create([udp(destination_port=4322)])
        self.assertIsNone(receiver.next_frame())

    def test_non_first_fragment_reaches_existing_reassembler(self):
        payload = struct.pack("!HHHH", 1234, 4321, 32, 0) + b"a" * 24
        receiver, _values, _factory = self.create([
            ipv4(payload[:16], fragment=0x2000), ipv4(payload[16:], fragment=2)])
        parser = PacketReassembler()
        self.assertIsNone(parser.feed(receiver.next_frame()))
        self.assertEqual(parser.feed(receiver.next_frame()).payload, b"a" * 24)

    def test_same_payload_at_different_tcp_sequence_is_not_deduplicated(self):
        first = tcp()
        second = bytearray(first)
        struct.pack_into("!I", second, 24, 46)
        receiver, _values, _factory = self.create([first, bytes(second)], [endpoint(protocol="tcp")])
        parser = PacketReassembler()
        self.assertEqual(parser.feed(receiver.next_frame()).sequence, 42)
        self.assertEqual(parser.feed(receiver.next_frame()).sequence, 46)

    def test_interface_change_opens_new_before_closing_old(self):
        receiver, values, _factory = self.create(sockets=[FakeSocket(), FakeSocket()])
        receiver.refresh([endpoint("10.8.0.2")])
        self.assertEqual(set(receiver.handles), {"10.8.0.2"})
        values[0].ioctl.assert_any_call(SIO_RCVALL, RCVALL_OFF)
        values[0].close.assert_called_once()
        self.assertEqual(receiver.counters["capture_adapter_removals"], 1)

    def test_endpoint_refresh_does_not_reopen_same_interface(self):
        receiver, _values, factory = self.create()
        receiver.refresh([endpoint(port=9876)])
        factory.assert_called_once()
        self.assertEqual(receiver.endpoints[0].local_port, 9876)

    def test_close_is_idempotent_and_turns_capture_off(self):
        receiver, values, _factory = self.create()
        receiver.close()
        receiver.close()
        values[0].ioctl.assert_any_call(SIO_RCVALL, RCVALL_OFF)
        values[0].close.assert_called_once()
        self.assertEqual(receiver.handles, {})

    def test_access_denied_is_actionable_and_does_not_leak_handles(self):
        fake = FakeSocket()
        fake.bind.side_effect = PermissionError(10013, "WSAEACCES")
        with self.assertRaisesRegex(RawSocketUnavailable, "No usable Windows Raw Socket"):
            WindowsRawSocketReceiver([endpoint()], socket_factory=Mock(return_value=fake))
        fake.close.assert_called_once()

    def test_ip_level_unavailable_does_not_enable_promiscuous_mode(self):
        fake = FakeSocket()
        fake.ioctl.side_effect = OSError(10045, "not supported")
        with self.assertRaises(RawSocketUnavailable):
            self.create(sockets=[fake])
        fake.ioctl.assert_called_once_with(SIO_RCVALL, RCVALL_IPLEVEL)

    def test_ipv6_only_has_explicit_failure_instead_of_empty_dps(self):
        with self.assertRaisesRegex(RawSocketUnavailable, "IPv6-only"):
            WindowsRawSocketReceiver([endpoint("2001:db8::10")], socket_factory=Mock())

    def test_mixed_ipv6_does_not_silently_lose_game_flows(self):
        with self.assertRaisesRegex(RawSocketUnavailable, "IPv6 game endpoints"):
            self.create(endpoints=[endpoint(), endpoint("2001:db8::10")])

    def test_mixed_dual_stack_wildcard_does_not_abort_ipv4_capture(self):
        receiver, _values, factory = self.create(
            endpoints=[endpoint(), endpoint("::", port=9876)]
        )
        factory.assert_called_once_with(
            socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_IP
        )
        self.assertEqual(set(receiver.handles), {"192.0.2.10"})

    def test_dual_stack_wildcard_matches_captured_ipv4_port(self):
        receiver, _values, _factory = self.create(
            [udp(destination_port=9876)],
            endpoints=[endpoint("::", port=9876)],
        )
        self.assertEqual(PacketReassembler().feed(receiver.next_frame()).payload, b"game")

    def test_busy_interface_does_not_starve_another_interface(self):
        a, b = FakeSocket([udp(), udp()]), FakeSocket([
            udp(destination="10.8.0.2")])
        receiver, _values, _factory = self.create(
            endpoints=[endpoint(), endpoint("10.8.0.2")], sockets=[b, a])
        self.assertNotEqual(receiver.next_frame().interface, receiver.next_frame().interface)

    def test_kernel_drop_counter_is_not_claimed_available(self):
        receiver, _values, _factory = self.create()
        self.assertFalse(receiver.statistics()["kernel_drop_visibility"])

    def test_runtime_receive_error_closes_bad_interface(self):
        fake = FakeSocket([udp()])
        fake.recvfrom = Mock(side_effect=OSError(10050, "network down"))
        receiver, _values, _factory = self.create(sockets=[fake])
        with self.assertRaisesRegex(RawSocketUnavailable, "All Windows Raw Socket interfaces failed"):
            receiver.next_frame()
        fake.close.assert_called_once()

    def test_required_server_interface_must_not_silently_fail(self):
        good, bad = FakeSocket(), FakeSocket()
        bad.bind.side_effect = OSError(10049, "not available")
        with self.assertRaisesRegex(RawSocketUnavailable, "Required game interface"):
            self.create(endpoints=[endpoint("10.8.0.2"), endpoint()], sockets=[good, bad])
        good.close.assert_called_once()

    def test_startup_failure_after_ioctl_restores_capture_off(self):
        fake = FakeSocket()
        fake.setblocking.side_effect = OSError(10050, "network down")
        with self.assertRaises(RawSocketUnavailable):
            self.create(sockets=[fake])
        fake.ioctl.assert_any_call(SIO_RCVALL, RCVALL_OFF)
        fake.close.assert_called_once()

    def test_interface_capacity_is_checked_before_opening_sockets(self):
        factory = Mock()
        with self.assertRaisesRegex(RawSocketUnavailable, "Too many IPv4 interfaces"):
            WindowsRawSocketReceiver([endpoint("0.0.0.0")], socket_factory=factory,
                address_provider=lambda: [f"10.0.0.{index}" for index in range(1, 65)])
        factory.assert_not_called()

    def test_truncation_is_reported_before_parser(self):
        receiver, _values, _factory = self.create([udp()[:-1]])
        self.assertIsNone(receiver.next_frame())
        self.assertEqual(receiver.counters["raw_socket_truncated_packets"], 1)

    def test_preflight_socket_is_immediately_closed(self):
        fake = FakeSocket()
        result = probe_windows_raw_socket("192.0.2.10", socket_factory=Mock(return_value=fake))
        self.assertTrue(result["available"])
        fake.close.assert_called_once()
        fake.ioctl.assert_any_call(SIO_RCVALL, RCVALL_OFF)


class LengthValidationTests(unittest.TestCase):
    def test_invalid_headers_are_rejected(self):
        for packet in (b"", b"abc", b"\x65" + b"\0" * 40, b"\x41" + b"\0" * 25):
            self.assertIsNone(_transport_header(packet))

    def test_declared_truncated_packet_is_rejected(self):
        raw = udp()
        self.assertIsNone(_transport_header(raw[:-1]))

    def test_padding_is_removed_not_reported_as_payload(self):
        raw = udp()
        self.assertEqual(_transport_header(raw + b"padding")[-1], raw)


if __name__ == "__main__":
    unittest.main()
