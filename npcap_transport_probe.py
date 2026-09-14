"""Bounded read-only transport check; emits counts, never payloads or keys."""
from __future__ import annotations

import argparse
import json
import time
from collections import Counter

import npcap_shadow_capture as capture
from npcap_receiver import MultiAdapterReceiver, endpoint_direction
from passive_transport import PacketReassembler, SequenceUnwrapper


def install_filter(dll, handle, expression):
    import ctypes
    program = capture.BpfProgram()
    if dll.pcap_compile(handle, ctypes.byref(program), expression.encode(), 1, 0xffffffff) != 0:
        raise RuntimeError(capture._decode_native(dll.pcap_geterr(handle)))
    try:
        if dll.pcap_setfilter(handle, ctypes.byref(program)) != 0:
            raise RuntimeError(capture._decode_native(dll.pcap_geterr(handle)))
    finally:
        dll.pcap_freecode(ctypes.byref(program))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seconds', type=float, default=15)
    parser.add_argument('--pid', type=int)
    parser.add_argument('--process-name', default='C7-Win64-Shipping.exe')
    args = parser.parse_args()
    if not 0 < args.seconds <= 120:
        parser.error('--seconds must be between 0 and 120')
    pid, _path, endpoints = capture.discover_game(args)
    dll = capture.load_wpcap()
    receiver = MultiAdapterReceiver(dll, capture, install_filter, lambda *_: None, endpoints)
    packets = PacketReassembler()
    counters = Counter()
    seen = {}
    sequences = {}
    started = time.monotonic()
    refresh_at = started + 1
    try:
        while time.monotonic() - started < args.seconds:
            if time.monotonic() >= refresh_at:
                receiver.refresh([row for row in capture.list_udp_endpoints() if row.pid == pid])
                refresh_at = time.monotonic() + 1
            frame = receiver.next_frame()
            if frame is None:
                continue
            counters['captured_frames'] += 1
            packet = packets.feed(frame)
            if packet is None or packet.protocol != 'udp':
                continue
            local_ip, local_port, remote_ip, remote_port, direction = endpoint_direction(
                packet, receiver.endpoints, receiver.local_addresses)
            if direction != 'inbound':
                continue
            counters['game_inbound_datagrams'] += 1
            for segment in capture.parse_kcp_segments(packet.payload):
                if segment['command'] != 'PUSH':
                    continue
                flow = local_ip, local_port, remote_ip, remote_port, segment['conv']
                if flow not in seen and len(seen) >= 64:
                    counters['probe_flow_limit'] += 1
                    continue
                serial = sequences.setdefault(flow, SequenceUnwrapper()).unwrap(segment['sequence'])
                values = seen.setdefault(flow, set())
                if serial in values:
                    counters['duplicate_push_observations'] += 1
                elif len(values) < 100000:
                    values.add(serial)
                    counters['unique_push_sequences'] += 1
                else:
                    counters['probe_sequence_limit'] += 1
        counters['observed_flows'] = len(seen)
        counters['missing_sequences_inside_observed_spans'] = sum(max(value)-min(value)+1-len(value) for value in seen.values() if value)
        print(json.dumps({'seconds': round(time.monotonic()-started, 3),
                          'open_interfaces': len(receiver.handles),
                          'interface_errors': len(receiver.errors), 'counts': dict(counters),
                          'transport': dict(packets.diagnostics),
                          'note': 'Transport observations only; not evidence of combat metric parity.'}, ensure_ascii=False))
    finally:
        receiver.close()


if __name__ == '__main__':
    main()
