"""Receive-only libpcap setup; no dependency on game decoding or statistics."""
from __future__ import annotations

import ctypes
import ipaddress
import time
import threading
from collections import Counter, deque
from dataclasses import dataclass

from passive_transport import CaptureFrame


@dataclass(frozen=True)
class CaptureHandle:
    handle: object
    timestamp_resolution_ns: int
    activation_warning: int = 0


def bind_receive_configuration(dll):
    dll.pcap_create.argtypes = [ctypes.c_char_p, ctypes.c_char_p]
    dll.pcap_create.restype = ctypes.c_void_p
    for name in ('pcap_set_snaplen', 'pcap_set_promisc', 'pcap_set_timeout',
                 'pcap_set_buffer_size', 'pcap_set_immediate_mode', 'pcap_set_tstamp_precision'):
        function = getattr(dll, name)
        function.argtypes = [ctypes.c_void_p, ctypes.c_int]
        function.restype = ctypes.c_int
    dll.pcap_setnonblock.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_char_p]
    dll.pcap_setnonblock.restype = ctypes.c_int
    for name in ('pcap_activate', 'pcap_get_tstamp_precision'):
        function = getattr(dll, name)
        function.argtypes = [ctypes.c_void_p]
        function.restype = ctypes.c_int


def open_receive_handle(dll, adapter_name, *, timeout_ms=50, buffer_bytes=32*1024*1024):
    """Configure BEFORE activation, and report actual source precision."""
    error = ctypes.create_string_buffer(256)
    handle = dll.pcap_create(adapter_name.encode(), error)
    if not handle:
        raise RuntimeError('Npcap create failed: ' + error.value.decode('utf-8', errors='replace'))
    try:
        for name, value in (('pcap_set_snaplen', 262144), ('pcap_set_promisc', 0),
                            ('pcap_set_timeout', timeout_ms), ('pcap_set_buffer_size', buffer_bytes),
                            ('pcap_set_immediate_mode', 1)):
            result = getattr(dll, name)(handle, value)
            if result != 0:
                raise RuntimeError(f'{name} failed ({result})')
        if dll.pcap_set_tstamp_precision(handle, 1) != 0:
            result = dll.pcap_set_tstamp_precision(handle, 0)
            if result != 0:
                raise RuntimeError(f'Npcap timestamp precision unavailable ({result})')
        result = dll.pcap_activate(handle)
        if result < 0:
            detail = dll.pcap_geterr(handle) or b''
            raise RuntimeError(f'Npcap activate failed ({result}): ' + detail.decode('utf-8', errors='replace'))
        precision = dll.pcap_get_tstamp_precision(handle)
        if precision not in (0, 1):
            raise RuntimeError(f'Unknown Npcap timestamp precision: {precision}')
        return CaptureHandle(handle, 1 if precision == 1 else 1000, result)
    except BaseException:
        dll.pcap_close(handle)
        raise


def game_udp_filter(local_ip, ports):
    """Include non-first fragments; ownership is checked again after reassembly.

    IPv6 extension headers make fixed-offset port tests unsafe, so use host
    scope there. Other traffic is discarded in memory, never decoded/logged.
    """
    address = ipaddress.ip_address(local_ip)
    ports = sorted({int(port) for port in ports if 0 < int(port) <= 65535})
    if not ports:
        raise ValueError('No game UDP ports')
    port_filter = ' or '.join(f'port {port}' for port in ports)
    if address.version == 6:
        return (f'ip6 and host {address} and '
                f'((ip6[6] = 17 and udp and ({port_filter})) '
                'or (ip6[6] != 17 and ip6 protochain 17))')
    return f'ip and host {address} and ((udp and ({port_filter})) or (ip[9] = 17 and (ip[6:2] & 0x1fff != 0)))'


def endpoint_direction(packet, endpoints, local_addresses):
    """Match address AND owned port, including localhost and wildcard binds."""
    def owned(address, port):
        for endpoint in endpoints:
            if endpoint.local_port != port:
                continue
            bound = ipaddress.ip_address(endpoint.local_address)
            if str(bound) == address or (bound.is_unspecified and address in local_addresses
                                        and bound.version == ipaddress.ip_address(address).version):
                return True
        return False
    source = owned(packet.src_ip, packet.src_port)
    destination = owned(packet.dst_ip, packet.dst_port)
    if destination and not source:
        return packet.dst_ip, packet.dst_port, packet.src_ip, packet.src_port, 'inbound'
    if source and not destination:
        return packet.src_ip, packet.src_port, packet.dst_ip, packet.dst_port, 'outbound'
    return '', 0, '', 0, 'ambiguous' if source else 'unrelated'


class MultiAdapterReceiver:
    """Bounded nonblocking polling; handles refreshed as interfaces/ports change.

    No worker threads, no socket probing, and no capture-wide payload dedup:
    the transport sequence (not content equality) identifies retransmissions.
    """
    def __init__(self, dll, provider, install_filter, configure, endpoints):
        self.dll, self.provider = dll, provider
        self.install_filter, self.configure = install_filter, configure
        self.handles = {}
        self.endpoints = list(endpoints)
        self.local_addresses = set()
        self.counters = Counter()
        self.errors = {}
        self._drop_samples = {}
        self._dropped_total = 0
        self.cursor = 0
        try:
            self.refresh(endpoints)
        except BaseException:
            self.close()
            raise

    def refresh(self, endpoints):
        self.endpoints = list(endpoints)
        ports = sorted({row.local_port for row in endpoints if row.local_port})
        adapters = self.provider.list_adapters(self.dll)
        selected = {}
        for adapter in adapters:
            addresses = set()
            for raw in adapter.addresses:
                address = ipaddress.ip_address(raw)
                if not address.is_unspecified and not address.is_multicast:
                    addresses.add(str(address))
            # Npcap loopback can have an empty address list.
            if 'loopback' in adapter.name.lower():
                addresses.update(('127.0.0.1', '::1'))
            if addresses and ports:
                selected[adapter.name] = adapter, addresses
        self.local_addresses = set().union(*(value[1] for value in selected.values())) if selected else set()
        for name in list(self.handles):
            if name not in selected:
                self.dll.pcap_close(self.handles.pop(name)[0].handle)
                self.counters['capture_adapter_removals'] += 1
        for name, (adapter, addresses) in selected.items():
            expression = ' or '.join(f'({game_udp_filter(address, ports)})' for address in sorted(addresses))
            existing = self.handles.get(name)
            opened = None
            try:
                if existing is not None:
                    if existing[2] != expression:
                        self.install_filter(self.dll, existing[0].handle, expression)
                        self.handles[name] = existing[0], existing[1], expression
                        self.counters['capture_filter_updates'] += 1
                    continue
                opened = open_receive_handle(self.dll, name)
                self.install_filter(self.dll, opened.handle, expression)
                error = ctypes.create_string_buffer(256)
                if self.dll.pcap_setnonblock(opened.handle, 1, error) != 0:
                    raise RuntimeError('Npcap nonblocking capture unavailable')
                self.configure(self.dll, opened.handle)
                datalink = int(self.dll.pcap_datalink(opened.handle))
                self.handles[name] = opened, datalink, expression
                self.errors.pop(name, None)
                self.counters['capture_adapter_opens'] += 1
            except (OSError, RuntimeError) as exc:
                if opened is not None:
                    self.dll.pcap_close(opened.handle)
                if existing is not None:
                    self.dll.pcap_close(self.handles.pop(name)[0].handle)
                self.errors[name] = str(exc)
                self.counters['capture_adapter_errors'] += 1
        if not self.handles:
            raise RuntimeError('No usable Npcap receive interface: ' + '; '.join(self.errors.values()))

    def next_frame(self):
        handles = list(self.handles.items())
        for offset in range(len(handles)):
            index = (self.cursor + offset) % len(handles)
            name, (receiver, datalink, _expression) = handles[index]
            header = ctypes.POINTER(self.provider.PcapPacketHeader)()
            data = ctypes.POINTER(ctypes.c_ubyte)()
            status = self.dll.pcap_next_ex(receiver.handle, ctypes.byref(header), ctypes.byref(data))
            if status < 0:
                self.dll.pcap_close(self.handles.pop(name)[0].handle)
                self.counters['capture_adapter_errors'] += 1
                continue
            if status == 1:
                self.cursor = (index + 1) % len(handles)
                value = header.contents
                if value.caplen > 262144:
                    self.counters['invalid_capture_lengths'] += 1
                    continue
                timestamp = int(value.ts.tv_sec)*1_000_000_000 + int(value.ts.tv_usec)*receiver.timestamp_resolution_ns
                return CaptureFrame(ctypes.string_at(data, value.caplen), timestamp, name,
                                    datalink, int(value.length), receiver.timestamp_resolution_ns)
        time.sleep(0.002)
        return None

    def statistics(self, read_stats):
        interfaces = {}
        current_keys = set()
        for name, (receiver, _datalink, _expression) in self.handles.items():
            item = read_stats(self.dll, receiver.handle)
            interfaces[name] = item
            key = name, id(receiver)
            current_keys.add(key)
            if item.get('available'):
                sample = int(item.get('dropped', 0))
                previous = self._drop_samples.get(key, 0)
                # Driver counters are unsigned 32-bit and reset on reopen.
                self._dropped_total += (sample - previous) & 0xffffffff
                self._drop_samples[key] = sample
        self._drop_samples = {key: value for key, value in self._drop_samples.items() if key in current_keys}
        return {'available': bool(interfaces) and all(item.get('available') for item in interfaces.values()),
                'interfaces': interfaces, 'dropped': self._dropped_total}

    def close(self):
        for receiver, _datalink, _expression in self.handles.values():
            self.dll.pcap_close(receiver.handle)
        self.handles.clear()


class BufferedReceiver:
    """Keep receiving during slow decoder-state/metadata reads.

    One thread exclusively owns all pcap operations. No handle is closed or
    reconfigured concurrently with pcap_next_ex. Queued frames own their bytes.
    """
    def __init__(self, receiver, read_stats, *, max_frames=32768, max_bytes=64*1024*1024):
        self.receiver = receiver
        self.read_stats = read_stats
        self.max_frames = max(1, int(max_frames))
        self.max_bytes = max(1, int(max_bytes))
        self.condition = threading.Condition()
        self.frames = deque()
        self.byte_count = 0
        self.pending_refresh = None
        self.error = None
        self.stopped = False
        self.done = False
        self.closed = False
        self.counters = Counter()
        self.status = {'available': False}
        self._handles = dict(receiver.handles)
        self._addresses = set(receiver.local_addresses)
        self._endpoints = list(receiver.endpoints)
        self.thread = threading.Thread(target=self._run, name='NpcapReceive', daemon=True)
        self.thread.start()

    @property
    def handles(self):
        with self.condition:
            return dict(self._handles)

    @property
    def local_addresses(self):
        with self.condition:
            return set(self._addresses)

    @property
    def endpoints(self):
        with self.condition:
            return list(self._endpoints)

    def refresh(self, endpoints):
        with self.condition:
            self.pending_refresh = list(endpoints)

    def take_counters(self):
        with self.condition:
            result = self.counters.copy()
            self.counters.clear()
            return result

    def statistics(self, _read_stats=None):
        with self.condition:
            return {**self.status, 'queued_frames': len(self.frames), 'queued_bytes': self.byte_count}

    def _run(self):
        next_stats = 0.0
        try:
            while True:
                with self.condition:
                    if self.stopped:
                        break
                    refresh, self.pending_refresh = self.pending_refresh, None
                if refresh is not None:
                    self.receiver.refresh(refresh)
                frame = self.receiver.next_frame()
                now = time.monotonic()
                if now >= next_stats:
                    status = self.receiver.statistics(self.read_stats)
                    with self.condition:
                        self.status = status
                        self._handles = dict(self.receiver.handles)
                        self._addresses = set(self.receiver.local_addresses)
                        self._endpoints = list(self.receiver.endpoints)
                    next_stats = now + 1
                with self.condition:
                    self.counters.update(self.receiver.counters)
                    self.receiver.counters.clear()
                    if frame is not None:
                        if len(self.frames) >= self.max_frames or self.byte_count+len(frame.data) > self.max_bytes:
                            self.counters['capture_queue_dropped'] += 1
                        else:
                            self.frames.append(frame)
                            self.byte_count += len(frame.data)
                            self.condition.notify()
        except Exception as exc:
            with self.condition:
                self.error = f'{type(exc).__name__}: {exc}'
        finally:
            try:
                self.receiver.close()
            finally:
                with self.condition:
                    self.done = True
                    self.condition.notify_all()

    def next_frame(self):
        with self.condition:
            if not self.frames and not self.done:
                self.condition.wait(0.01)
            if self.frames:
                frame = self.frames.popleft()
                self.byte_count -= len(frame.data)
                return frame
            if self.error:
                raise RuntimeError('Npcap receiver failed: ' + self.error)
            return None

    def close(self):
        with self.condition:
            self.stopped = True
            self.condition.notify_all()
        self.thread.join(5)
        if self.thread.is_alive():
            raise RuntimeError('Npcap receive thread did not stop; refusing concurrent handle close')
        with self.condition:
            self.frames.clear()
            self.byte_count = 0
            self.closed = True
