"""Receive-only TCP/RC4 alignment using detached state and contiguous bytes.

TCP packet boundaries are NOT application frame boundaries. Alignment is
proved by three complete Doraemon frames; the protocol decoder must still
validate decompression and an application message before declaring readiness.
"""
from __future__ import annotations

import time
from collections import Counter, OrderedDict
from dataclasses import dataclass, field

from npcap_protocol import MAX_DORAEMON_FRAME_BYTES, application_frame_valid
from passive_transport import TcpStreamReassembler


@dataclass(frozen=True)
class DecryptedTcpBytes:
    flow: tuple
    sequence: int
    timestamp_epoch: float
    plaintext: bytes
    timestamp_ns: int
    after_anchor: bool = True


@dataclass
class _CipherBytes:
    start: int
    data: bytearray = field(default_factory=bytearray)
    origins: list = field(default_factory=list)
    rejected: set = field(default_factory=set)
    incomplete: dict = field(default_factory=dict)


class PassiveTcpRc4Reassembler:
    def __init__(self, *, required_validation=3, max_buffer_bytes=4*1024*1024):
        self.transport = TcpStreamReassembler(max_flows=16, max_buffer_bytes=max_buffer_bytes)
        self.required_validation = max(3, int(required_validation))
        self.max_buffer_bytes = int(max_buffer_bytes)
        self.flows = OrderedDict()
        self.wire_keys = {}
        self.last_seen_monotonic = {}
        self.anchor = None
        self.state = None
        self.active_flow = None
        self.next_sequence = None
        self.alignment_new_unique_pushes = 0
        self.counters = Counter()
        self._prefix_key = b''
        self.confirmed = False

    @property
    def aligned(self):
        return self.state is not None and self.active_flow is not None

    def install_anchor(self, anchor, now=None):
        self.anchor = anchor
        self.state = self.active_flow = self.next_sequence = None
        self.alignment_new_unique_pushes = 0
        self.confirmed = False
        self._prefix_key = anchor.decrypt.clone().forward(b'\0\0\0')
        for value in self.flows.values():
            value.rejected.clear()
            value.incomplete.clear()

    def invalidate(self, *, clear_history=False):
        self.anchor = self.state = self.active_flow = self.next_sequence = None
        if clear_history:
            self.flows.clear()
            self.wire_keys.clear()
            self.last_seen_monotonic.clear()

    def add(self, packet, now=None):
        monotonic_now = time.monotonic() if now is None else float(now)
        output = []
        for chunk in self.transport.feed(packet, now_ns=int(monotonic_now*1e9)):
            flow = (packet.dst_port, packet.src_ip, packet.src_port,
                    ('tcp', chunk.generation), packet.dst_ip)
            self.wire_keys[flow] = packet.direction_key
            self.last_seen_monotonic[flow] = monotonic_now
            self.counters['tcp_stream_chunks'] += 1
            if self.anchor is not None and chunk.timestamp_ns/1e9 >= self.anchor.read_after_epoch:
                self.alignment_new_unique_pushes += 1
            if self.aligned and flow == self.active_flow:
                if chunk.offset != self.next_sequence:
                    raise ValueError('TCP contiguous byte offset changed')
                if not self.confirmed:
                    value = self.flows[flow]
                    value.data.extend(chunk.data)
                    value.origins.append((chunk.offset, chunk.offset+len(chunk.data), chunk.timestamp_ns))
                    if len(value.data) > self.max_buffer_bytes:
                        raise ValueError('Unvalidated TCP cipher history limit exceeded')
                output.append(self._decrypt(flow, chunk.offset, chunk.data, chunk.timestamp_ns))
                continue
            value = self.flows.get(flow)
            if value is None:
                value = _CipherBytes(chunk.offset)
                self.flows[flow] = value
                while len(self.flows) > 16:
                    old, _ = self.flows.popitem(last=False)
                    self.wire_keys.pop(old, None)
                    self.last_seen_monotonic.pop(old, None)
            if chunk.offset != value.start + len(value.data):
                raise ValueError('Non-contiguous TCP alignment buffer')
            value.data.extend(chunk.data)
            value.origins.append((chunk.offset, chunk.offset+len(chunk.data), chunk.timestamp_ns))
            if len(value.data) > self.max_buffer_bytes:
                raise ValueError('TCP alignment buffer limit exceeded')
            if not self.aligned and self.anchor is not None:
                output.extend(self._try_align(flow, value))
        self.counters.update(self.transport.diagnostics)
        self.transport.diagnostics.clear()
        return output

    def _frame_size(self, prefix):
        bits = int.from_bytes(prefix, 'little') & 0xfffff
        if bits % 8 != 4:
            return None
        size = (20+bits)//8
        return size if 3 <= size <= MAX_DORAEMON_FRAME_BYTES else None

    def _validate(self, value, position):
        trial = self.anchor.decrypt.clone()
        cursor = position-value.start
        for _ in range(self.required_validation):
            if len(value.data) < cursor+3:
                return cursor+3
            prefix = trial.forward(bytes(value.data[cursor:cursor+3]))
            size = self._frame_size(prefix)
            if size is None:
                return -1
            end = cursor+size
            if end > len(value.data):
                return end
            plain = prefix+trial.forward(bytes(value.data[cursor+3:end]))
            if not application_frame_valid(plain):
                return -1
            cursor = end
        return 0

    def _try_align(self, flow, value):
        nearby = sorted(value.origins,
                        key=lambda item: abs(item[2]/1e9-self.anchor.midpoint_epoch))[:32]
        # Try observed chunk boundaries first, then bounded byte positions
        # inside the same chunks (coalesced/split TCP frames).
        boundaries = sorted({point for start, end, _ in nearby for point in (start, end)})
        positions = list(boundaries)
        for start, end, _ in nearby:
            positions.extend(range(start+1, min(end, start+65536)))
        for position in positions[:65536]:
            cursor = position-value.start
            if cursor < 0 or cursor+3 > len(value.data) or position in value.rejected:
                continue
            required = value.incomplete.get(position, 0)
            if len(value.data) < required:
                continue
            prefix = bytes(value.data[cursor+i] ^ self._prefix_key[i] for i in range(3))
            if self._frame_size(prefix) is None:
                value.rejected.add(position)
                continue
            result = self._validate(value, position)
            if result < 0:
                value.rejected.add(position)
                value.incomplete.pop(position, None)
            elif result:
                value.incomplete[position] = result
            else:
                self.active_flow = flow
                self.state = self.anchor.decrypt.clone()
                self.next_sequence = position
                self.counters['tcp_rc4_alignments'] += 1
                output = []
                for start, end, stamp in value.origins:
                    start = max(start, position)
                    if end > start:
                        output.append(self._decrypt(flow, start,
                            bytes(value.data[start-value.start:end-value.start]), stamp))
                value.rejected.clear()
                value.incomplete.clear()
                return output
        return []

    def confirm_alignment(self):
        """Release cipher history only AFTER the application decoder proves it."""
        self.confirmed = True
        value = self.flows.get(self.active_flow)
        if value is not None:
            value.start = self.next_sequence
            value.data.clear()
            value.origins.clear()
            value.rejected.clear()
            value.incomplete.clear()

    def _decrypt(self, flow, offset, data, stamp):
        plain = self.state.forward(data)
        self.next_sequence = offset+len(data)
        return DecryptedTcpBytes(flow, offset, stamp/1e9, plain, stamp)

    def pending_gap(self, now=None):
        monotonic_now = time.monotonic() if now is None else float(now)
        self.transport.expire(int(monotonic_now*1e9))
        key = self.wire_keys.get(self.active_flow)
        stream = self.transport.flows.get(key)
        if stream is None:
            return None
        if stream.conflicted or stream.gap_since is not None:
            return {'expected': stream.next_sequence,
                    'actual': min(stream.segments, default=stream.next_sequence),
                    'age_seconds': max(0, monotonic_now-(stream.gap_since or int(monotonic_now*1e9))/1e9),
                    'conflicted': stream.conflicted, 'flow': self.active_flow}
        return None

    def fresh_alternate_flow(self, now=None):
        monotonic_now = time.monotonic() if now is None else float(now)
        if not self.aligned:
            return None
        key = self.wire_keys.get(self.active_flow)
        stream = self.transport.flows.get(key)
        if stream is not None and stream.generation != self.active_flow[3][1]:
            # A real SYN-created generation is a connection boundary even
            # when every application frame is coalesced into one packet.
            return next((flow for flow in reversed(self.flows)
                         if self.wire_keys.get(flow) == key
                         and flow[3][1] == stream.generation), None)
        if monotonic_now-self.last_seen_monotonic.get(self.active_flow, 0) < 2:
            return None
        return next((flow for flow in reversed(self.flows)
                     if flow != self.active_flow
                     and monotonic_now-self.last_seen_monotonic.get(flow, 0) < 2
                     and len(self.flows[flow].origins) >= self.required_validation), None)

    def needs_resync(self, now=None):
        gap = self.pending_gap(now)
        return bool((gap and (gap['conflicted'] or gap['age_seconds'] >= 2))
                    or self.fresh_alternate_flow(now) is not None)
