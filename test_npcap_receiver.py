import unittest
from unittest.mock import Mock
from types import SimpleNamespace

from npcap_receiver import MultiAdapterReceiver, endpoint_direction, game_udp_filter, open_receive_handle
from passive_transport import TransportPacket


class ReceiverTests(unittest.TestCase):
    def dll(self):
        dll = Mock()
        dll.pcap_create.return_value = 42
        for name in ('pcap_set_snaplen', 'pcap_set_promisc', 'pcap_set_timeout',
                     'pcap_set_buffer_size', 'pcap_set_immediate_mode',
                     'pcap_set_tstamp_precision', 'pcap_activate', 'pcap_get_tstamp_precision'):
            getattr(dll, name).return_value = 0
        return dll

    def test_settings_before_activation(self):
        dll = self.dll()
        dll.pcap_get_tstamp_precision.return_value = 1
        result = open_receive_handle(dll, 'test')
        self.assertEqual(result.timestamp_resolution_ns, 1)
        names = [call[0] for call in dll.mock_calls]
        self.assertLess(names.index('pcap_set_buffer_size'), names.index('pcap_activate'))
        self.assertLess(names.index('pcap_set_immediate_mode'), names.index('pcap_activate'))
        self.assertLess(names.index('pcap_set_tstamp_precision'), names.index('pcap_activate'))
        dll.pcap_set_promisc.assert_called_once_with(42, 0)
        dll.pcap_close.assert_not_called()

    def test_microsecond_fallback_is_not_claimed_as_nanosecond(self):
        dll = self.dll()
        dll.pcap_set_tstamp_precision.side_effect = [-12, 0]
        self.assertEqual(open_receive_handle(dll, 'test').timestamp_resolution_ns, 1000)

    def test_failure_closes_handle(self):
        dll = self.dll()
        dll.pcap_set_buffer_size.return_value = -1
        with self.assertRaises(RuntimeError):
            open_receive_handle(dll, 'test')
        dll.pcap_close.assert_called_once_with(42)
        dll.pcap_activate.assert_not_called()

    def test_filter_retains_fragments_and_scopes_host(self):
        expression = game_udp_filter('10.0.0.1', [123, 123, 456])
        self.assertIn('host 10.0.0.1', expression)
        self.assertIn('ip[6:2] & 0x1fff', expression)
        self.assertEqual(expression.count('port 123'), 1)
        expression6 = game_udp_filter('::1', [123])
        self.assertIn('ip6 and host ::1', expression6)
        self.assertIn('port 123', expression6)
        self.assertIn('ip6 protochain 17', expression6)

    def test_loopback_direction_uses_owned_port(self):
        packet = TransportPacket('127.0.0.1', '127.0.0.1', 456, 123, b'', 'udp', 0, 0)
        endpoints = [SimpleNamespace(local_address='0.0.0.0', local_port=123)]
        self.assertEqual(endpoint_direction(packet, endpoints, {'127.0.0.1'})[-1], 'inbound')
        endpoints.append(SimpleNamespace(local_address='127.0.0.1', local_port=456))
        self.assertEqual(endpoint_direction(packet, endpoints, {'127.0.0.1'})[-1], 'ambiguous')

    def test_wildcard_does_not_claim_remote_endpoint(self):
        packet = TransportPacket('203.0.113.9', '10.0.0.1', 123, 123, b'', 'udp', 0, 0)
        endpoints = [SimpleNamespace(local_address='0.0.0.0', local_port=123)]
        self.assertEqual(endpoint_direction(packet, endpoints, {'10.0.0.1'})[-1], 'inbound')

    def test_ipv6_owned_endpoint(self):
        packet = TransportPacket('2001:db8::2', '2001:db8::1', 456, 123, b'', 'udp', 0, 0)
        endpoints = [SimpleNamespace(local_address='::', local_port=123)]
        self.assertEqual(endpoint_direction(packet, endpoints, {'2001:db8::1'})[-1], 'inbound')

    def test_adapter_refresh_closes_removed_and_updates_filter(self):
        dll = self.dll()
        dll.pcap_setnonblock.return_value = 0
        dll.pcap_datalink.return_value = 1
        dll.pcap_create.side_effect = [42, 43]
        provider = Mock()
        provider.list_adapters.return_value = [SimpleNamespace(name='a', addresses=('10.0.0.1',))]
        install = Mock()
        endpoint = SimpleNamespace(local_address='0.0.0.0', local_port=123)
        receiver = MultiAdapterReceiver(dll, provider, install, Mock(), [endpoint])
        receiver.refresh([SimpleNamespace(local_address='0.0.0.0', local_port=456)])
        self.assertEqual(dll.pcap_create.call_count, 1)
        self.assertIn('port 456', install.call_args.args[2])
        provider.list_adapters.return_value = [SimpleNamespace(name='vpn', addresses=('10.1.0.1',))]
        receiver.refresh([endpoint])
        self.assertEqual(set(receiver.handles), {'vpn'})
        dll.pcap_close.assert_called_once_with(42)
        receiver.close()
        self.assertEqual(dll.pcap_close.call_count, 2)

    def test_failed_adapter_does_not_leak_handle(self):
        dll = self.dll()
        dll.pcap_setnonblock.return_value = -1
        provider = Mock()
        provider.list_adapters.return_value = [SimpleNamespace(name='a', addresses=('10.0.0.1',))]
        with self.assertRaises(RuntimeError):
            MultiAdapterReceiver(dll, provider, Mock(), Mock(),
                                 [SimpleNamespace(local_address='0.0.0.0', local_port=123)])
        dll.pcap_close.assert_called_once_with(42)


if __name__ == '__main__':
    unittest.main()
