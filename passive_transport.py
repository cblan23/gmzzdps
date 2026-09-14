"""Pure packet/stream reconstruction. No sockets, process access or game calls."""
from __future__ import annotations

import socket
import struct
import time
from collections import Counter, OrderedDict
from dataclasses import dataclass, field


@dataclass(frozen=True)
class CaptureFrame:
    data: bytes
    timestamp_ns: int
    interface: str = ''
    datalink: int = 1
    original_length: int = 0
    timestamp_resolution_ns: int = 1000


@dataclass(frozen=True)
class TransportPacket:
    src_ip: str
    dst_ip: str
    src_port: int
    dst_port: int
    payload: bytes
    protocol: str
    timestamp_ns: int
    completed_ns: int
    interface: str = ''
    sequence: int = 0
    acknowledgement: int = 0
    flags: int = 0

    @property
    def direction_key(self) -> tuple:
        return self.protocol, self.src_ip, self.src_port, self.dst_ip, self.dst_port


class SequenceUnwrapper:
    """Map a 32-bit serial number to its nearest extended sequence."""
    def __init__(self):
        self.highest: int | None = None

    def unwrap(self, value: int) -> int:
        value &= 0xffffffff
        if self.highest is None:
            self.highest = value
            return value
        delta = ((value - (self.highest & 0xffffffff) + (1 << 31)) & 0xffffffff) - (1 << 31)
        extended = self.highest + delta
        self.highest = max(self.highest, extended)
        return extended


@dataclass
class _Fragments:
    created_ns: int
    first_ns: int
    last_ns: int
    parts: dict[int, bytes] = field(default_factory=dict)
    end: int | None = None


class PacketReassembler:
    """Validate Ethernet/IP lengths and reconstruct IPv4/IPv6 fragments."""
    def __init__(self, *, max_datagrams=256, max_bytes=8*1024*1024, timeout_seconds=3.0):
        self.max_datagrams = max(1, int(max_datagrams))
        self.max_bytes = max(65536, int(max_bytes))
        self.timeout_ns = int(timeout_seconds * 1_000_000_000)
        self.fragments: OrderedDict[tuple, _Fragments] = OrderedDict()
        self.rejected: OrderedDict[tuple, int] = OrderedDict()
        self._now_ns = time.monotonic_ns()
        self.buffered_bytes = 0
        self.diagnostics: Counter[str] = Counter()

    def _discard(self, key, reason):
        item = self.fragments.pop(key, None)
        if item is not None:
            self.buffered_bytes -= sum(map(len, item.parts.values()))
            self.diagnostics[reason] += 1
        if reason == 'ip_fragment_conflicts':
            self.rejected[key] = self._now_ns
            while len(self.rejected) > self.max_datagrams:
                self.rejected.popitem(last=False)

    def expire(self, now_ns=None):
        now_ns = time.monotonic_ns() if now_ns is None else int(now_ns)
        self._now_ns = now_ns
        for key, item in list(self.fragments.items()):
            if now_ns - item.created_ns >= self.timeout_ns:
                self._discard(key, 'ip_fragment_timeouts')
        for key, rejected_at in list(self.rejected.items()):
            if now_ns - rejected_at >= self.timeout_ns:
                self.rejected.pop(key, None)

    def _fragment(self, key, offset, more, payload, timestamp_ns, now_ns):
        if key in self.rejected:
            self.diagnostics['rejected_fragment_followups'] += 1
            return None
        if not payload or offset + len(payload) > 65535 or (more and len(payload) % 8):
            self._discard(key, 'ip_fragment_conflicts')
            self.diagnostics['invalid_fragments'] += 1
            return None
        item = self.fragments.get(key)
        if item is None:
            while len(self.fragments) >= self.max_datagrams:
                self._discard(next(iter(self.fragments)), 'ip_fragment_evictions')
            item = _Fragments(now_ns, timestamp_ns, timestamp_ns)
            self.fragments[key] = item
        end = offset + len(payload)
        if item.end is not None and (end > item.end or (not more and end != item.end)):
            self._discard(key, 'ip_fragment_conflicts')
            return None
        if not more:
            if any(start + len(data) > end for start, data in item.parts.items()):
                self._discard(key, 'ip_fragment_conflicts')
                return None
            item.end = end
        for start, data in item.parts.items():
            left, right = max(start, offset), min(start + len(data), end)
            if left < right and data[left-start:right-start] != payload[left-offset:right-offset]:
                self._discard(key, 'ip_fragment_conflicts')
                return None
        if item.parts.get(offset) == payload:
            self.diagnostics['duplicate_ip_fragments'] += 1
            return None
        previous = item.parts.get(offset, b'')
        if len(previous) > len(payload):
            return None
        self.buffered_bytes += len(payload) - len(previous)
        item.parts[offset] = payload
        item.first_ns = min(item.first_ns, timestamp_ns)
        item.last_ns = max(item.last_ns, timestamp_ns)
        while self.buffered_bytes > self.max_bytes and self.fragments:
            self._discard(next(iter(self.fragments)), 'ip_fragment_evictions')
        if key not in self.fragments or item.end is None:
            return None
        cursor = 0
        chunks = []
        for start, data in sorted(item.parts.items()):
            if start > cursor:
                return None
            if start + len(data) > cursor:
                chunks.append(data[cursor-start:])
                cursor = start + len(data)
        if cursor != item.end:
            return None
        result = b''.join(chunks), item.first_ns, item.last_ns
        self._discard(key, 'ip_datagrams_reassembled')
        return result

    def feed(self, frame: CaptureFrame, *, now_ns=None) -> TransportPacket | None:
        now_ns = time.monotonic_ns() if now_ns is None else int(now_ns)
        self.expire(now_ns)
        data = frame.data
        if frame.original_length and len(data) < frame.original_length:
            self.diagnostics['truncated_packets'] += 1
            return None
        offset = 0
        if frame.datalink == 1:
            if len(data) < 14:
                return None
            kind = struct.unpack_from('!H', data, 12)[0]
            offset = 14
            while kind in (0x8100, 0x88a8, 0x9100):
                if len(data) < offset + 4:
                    return None
                kind = struct.unpack_from('!H', data, offset + 2)[0]
                offset += 4
            if kind not in (0x0800, 0x86dd):
                return None
        elif frame.datalink in (0, 108):
            if len(data) < 5:
                return None
            family = int.from_bytes(data[:4], 'little' if frame.datalink == 0 else 'big')
            if family not in (2, 10, 23, 24, 28, 30):
                return None
            offset = 4
        elif frame.datalink not in (12, 101, 228, 229):
            self.diagnostics['unsupported_datalinks'] += 1
            return None
        if len(data) <= offset:
            return None
        version = data[offset] >> 4
        first_ns = last_ns = frame.timestamp_ns
        if version == 4:
            if len(data) < offset + 20:
                return None
            header = (data[offset] & 15) * 4
            total = struct.unpack_from('!H', data, offset+2)[0]
            if header < 20 or total < header or offset + total > len(data):
                self.diagnostics['invalid_ip_lengths'] += 1
                return None
            src = socket.inet_ntop(socket.AF_INET, data[offset+12:offset+16])
            dst = socket.inet_ntop(socket.AF_INET, data[offset+16:offset+20])
            protocol = data[offset+9]
            fragment = struct.unpack_from('!H', data, offset+6)[0]
            payload = data[offset+header:offset+total]
            if fragment & 0x3fff:
                identity = struct.unpack_from('!H', data, offset+4)[0]
                assembled = self._fragment((frame.interface, 4, src, dst, protocol, identity),
                    (fragment & 0x1fff)*8, bool(fragment & 0x2000), payload, frame.timestamp_ns, now_ns)
                if assembled is None:
                    return None
                payload, first_ns, last_ns = assembled
        elif version == 6:
            if len(data) < offset + 40:
                return None
            length = struct.unpack_from('!H', data, offset+4)[0]
            if not length or offset + 40 + length > len(data):
                self.diagnostics['invalid_ip_lengths'] += 1
                return None
            src = socket.inet_ntop(socket.AF_INET6, data[offset+8:offset+24])
            dst = socket.inet_ntop(socket.AF_INET6, data[offset+24:offset+40])
            protocol = data[offset+6]
            payload = data[offset+40:offset+40+length]
            for _ in range(12):
                if protocol not in (0, 43, 44, 51, 60):
                    break
                if len(payload) < 8:
                    return None
                next_protocol = payload[0]
                if protocol == 44:
                    bits, identity = struct.unpack_from('!HI', payload, 2)
                    assembled = self._fragment((frame.interface, 6, src, dst, next_protocol, identity),
                        bits & 0xfff8, bool(bits & 1), payload[8:], frame.timestamp_ns, now_ns)
                    if assembled is None:
                        return None
                    payload, first_ns, last_ns = assembled
                else:
                    size = (payload[1]+2)*4 if protocol == 51 else (payload[1]+1)*8
                    if size > len(payload):
                        return None
                    payload = payload[size:]
                protocol = next_protocol
            else:
                self.diagnostics['excessive_ipv6_extensions'] += 1
                return None
        else:
            return None
        if protocol == 17:
            if len(payload) < 8:
                return None
            source, destination, size = struct.unpack_from('!HHH', payload)
            if size < 8 or size > len(payload):
                self.diagnostics['invalid_udp_lengths'] += 1
                return None
            return TransportPacket(src, dst, source, destination, payload[8:size], 'udp', first_ns, last_ns, frame.interface)
        if protocol == 6:
            if len(payload) < 20:
                return None
            source, destination, sequence, acknowledgement = struct.unpack_from('!HHII', payload)
            header = (payload[12] >> 4) * 4
            if header < 20 or header > len(payload):
                self.diagnostics['invalid_tcp_lengths'] += 1
                return None
            return TransportPacket(src, dst, source, destination, payload[header:], 'tcp', first_ns, last_ns,
                                   frame.interface, sequence, acknowledgement, payload[13])
        return None


@dataclass(frozen=True)
class StreamChunk:
    flow: tuple
    generation: int
    offset: int
    data: bytes
    timestamp_ns: int


@dataclass
class _TcpDirection:
    serial: SequenceUnwrapper = field(default_factory=SequenceUnwrapper)
    next_sequence: int | None = None
    origin: int = 0
    generation: int = 0
    syn_sequence: int | None = None
    segments: dict[int, tuple[bytes, int]] = field(default_factory=dict)
    history: bytearray = field(default_factory=bytearray)
    history_start: int = 0
    fin_sequence: int | None = None
    closed: bool = False
    conflicted: bool = False
    gap_since: int | None = None


class TcpStreamReassembler:
    """Sequence-based reconstruction; missing or conflicting bytes never guessed."""
    def __init__(self, *, max_flows=64, max_buffer_bytes=4*1024*1024, gap_seconds=2.0):
        self.flows: OrderedDict[tuple, _TcpDirection] = OrderedDict()
        self.max_flows = max(1, int(max_flows))
        self.max_buffer_bytes = max(1, int(max_buffer_bytes))
        self.gap_ns = int(gap_seconds * 1_000_000_000)
        self.diagnostics: Counter[str] = Counter()
        self.next_generation = 0

    def expire(self, now_ns=None):
        now_ns = time.monotonic_ns() if now_ns is None else int(now_ns)
        for stream in self.flows.values():
            if not stream.conflicted and stream.gap_since is not None and now_ns-stream.gap_since >= self.gap_ns:
                stream.conflicted = True
                stream.segments.clear()
                self.diagnostics['tcp_gap_timeouts'] += 1

    def feed(self, packet: TransportPacket, *, now_ns=None) -> list[StreamChunk]:
        if packet.protocol != 'tcp':
            return []
        now_ns = time.monotonic_ns() if now_ns is None else int(now_ns)
        self.expire(now_ns)
        key = packet.direction_key
        stream = self.flows.get(key)
        syn = bool(packet.flags & 2)
        if stream is None or (syn and (stream.closed or stream.syn_sequence != packet.sequence)):
            generation = self.next_generation
            self.next_generation += 1
            stream = _TcpDirection(generation=generation, syn_sequence=packet.sequence if syn else None)
            self.flows[key] = stream
            if not syn:
                self.diagnostics['tcp_midstream_starts'] += 1
            while len(self.flows) > self.max_flows:
                self.flows.popitem(last=False)
                self.diagnostics['tcp_flow_evictions'] += 1
        self.flows.move_to_end(key)
        if packet.flags & 4:
            stream.closed = True
            stream.segments.clear()
            self.diagnostics['tcp_resets'] += 1
            return []
        if stream.closed or stream.conflicted:
            return []
        sequence = stream.serial.unwrap(packet.sequence) + int(syn)
        if stream.next_sequence is None:
            stream.next_sequence = stream.origin = stream.history_start = sequence
        data = packet.payload
        end = sequence + len(data)
        if packet.flags & 1:
            stream.fin_sequence = end
        if sequence < stream.next_sequence:
            overlap_end = min(end, stream.next_sequence)
            overlap_start = max(sequence, stream.history_start)
            if overlap_start < overlap_end and bytes(stream.history[overlap_start-stream.history_start:overlap_end-stream.history_start]) != data[overlap_start-sequence:overlap_end-sequence]:
                stream.conflicted = True
                self.diagnostics['tcp_overlap_conflicts'] += 1
                return []
            cut = min(len(data), stream.next_sequence - sequence)
            data = data[cut:]
            sequence += cut
            self.diagnostics['tcp_retransmitted_bytes'] += cut
        for start, (existing, _stamp) in stream.segments.items():
            left, right = max(start, sequence), min(start+len(existing), sequence+len(data))
            if left < right and existing[left-start:right-start] != data[left-sequence:right-sequence]:
                stream.conflicted = True
                stream.segments.clear()
                self.diagnostics['tcp_overlap_conflicts'] += 1
                return []
        if data:
            old = stream.segments.get(sequence)
            if old is None or len(data) > len(old[0]):
                stream.segments[sequence] = data, packet.timestamp_ns
            else:
                self.diagnostics['tcp_duplicate_segments'] += 1
        if sum(len(data) for data, _stamp in stream.segments.values()) > self.max_buffer_bytes:
            stream.conflicted = True
            stream.segments.clear()
            self.diagnostics['tcp_buffer_overflows'] += 1
            return []
        output = []
        for start in sorted(list(stream.segments)):
            if start > stream.next_sequence:
                break
            chunk, stamp = stream.segments.pop(start)
            chunk = chunk[max(0, stream.next_sequence-start):]
            if not chunk:
                continue
            output.append(StreamChunk(key, stream.generation, stream.next_sequence-stream.origin, chunk, stamp))
            stream.history.extend(chunk)
            stream.next_sequence += len(chunk)
            if len(stream.history) > 65536:
                stream.history = stream.history[-65536:]
            stream.history_start = stream.next_sequence - len(stream.history)
        if stream.fin_sequence is not None and stream.next_sequence >= stream.fin_sequence:
            stream.closed = True
            self.diagnostics['tcp_finished_streams'] += 1
        if stream.segments:
            if stream.gap_since is None:
                stream.gap_since = now_ns
            elif now_ns - stream.gap_since >= self.gap_ns:
                self.diagnostics['tcp_gap_timeouts'] += 1
                stream.conflicted = True
                stream.segments.clear()
        else:
            stream.gap_since = None
        return output
