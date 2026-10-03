"""Windows built-in receive-only IPv4 packet capture.

The receiver uses ``SOCK_RAW`` + ``SIO_RCVALL`` and never exposes a send
operation.  It deliberately implements the small receiver contract consumed by
``npcap_capture_process`` so the protocol/reassembly layers stay independent of
the packet source.

Windows raw sockets require an elevated process.  They return complete IPv4
packets (DLT_RAW), so no synthetic Ethernet header is added.
"""
from __future__ import annotations

import ctypes
import ipaddress
import os
import select
import socket
import struct
import time
from collections import Counter
from dataclasses import dataclass
from typing import Callable, Iterable

from passive_transport import CaptureFrame


ERROR_INSUFFICIENT_BUFFER = 122
DLT_RAW = 101
MAX_IPV4_PACKET = 65535
DEFAULT_RECEIVE_BUFFER = 16 * 1024 * 1024
MAX_RAW_INTERFACES = 60  # Stay below Winsock select()'s default fd_set capacity.
RECEIVE_CLOCK_RESOLUTION_NS = max(1, round(time.get_clock_info("time").resolution * 1_000_000_000))

# CPython exposes these constants on Windows.  Numeric fallbacks keep frozen
# builds and unit-test hosts deterministic without importing platform-only code.
SIO_RCVALL = getattr(socket, "SIO_RCVALL", 0x98000001)
RCVALL_OFF = getattr(socket, "RCVALL_OFF", 0)
RCVALL_ON = getattr(socket, "RCVALL_ON", 1)
RCVALL_IPLEVEL = getattr(socket, "RCVALL_IPLEVEL", 3)


class RawSocketUnavailable(RuntimeError):
    """The built-in capture source cannot be opened on this machine."""


class _MibIpAddrRow(ctypes.Structure):
    _fields_ = [
        ("address", ctypes.c_uint32),
        ("index", ctypes.c_uint32),
        ("mask", ctypes.c_uint32),
        ("broadcast", ctypes.c_uint32),
        ("reassembly_size", ctypes.c_uint32),
        ("unused", ctypes.c_uint16),
        ("type", ctypes.c_uint16),
    ]


def _fallback_local_ipv4() -> tuple[str, ...]:
    values: set[str] = set()
    try:
        rows = socket.getaddrinfo(
            socket.gethostname(), None, socket.AF_INET, socket.SOCK_DGRAM
        )
    except OSError:
        rows = []
    for row in rows:
        try:
            address = ipaddress.ip_address(row[4][0])
        except (IndexError, TypeError, ValueError):
            continue
        if address.version == 4 and not address.is_unspecified:
            values.add(str(address))
    values.add("127.0.0.1")
    return tuple(sorted(values, key=ipaddress.ip_address))


def list_local_ipv4() -> tuple[str, ...]:
    """Return Windows IPv4 unicast addresses, including VPN interfaces."""
    if os.name != "nt":
        return _fallback_local_ipv4()
    try:
        iphlpapi = ctypes.WinDLL("iphlpapi.dll", use_last_error=True)
        function = iphlpapi.GetIpAddrTable
        function.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32), ctypes.c_bool]
        function.restype = ctypes.c_uint32
        size = ctypes.c_uint32(0)
        result = int(function(None, ctypes.byref(size), False))
        if result not in (0, ERROR_INSUFFICIENT_BUFFER) or size.value < 4:
            raise OSError(result, "GetIpAddrTable size query failed")
        buffer = ctypes.create_string_buffer(size.value)
        result = int(function(buffer, ctypes.byref(size), False))
        if result:
            raise OSError(result, "GetIpAddrTable failed")
        count = struct.unpack_from("<I", buffer.raw)[0]
        row_size = ctypes.sizeof(_MibIpAddrRow)
        if 4 + count * row_size > len(buffer):
            raise RuntimeError("GetIpAddrTable returned a truncated table")
        values: set[str] = set()
        for index in range(count):
            row = _MibIpAddrRow.from_buffer_copy(buffer.raw, 4 + index * row_size)
            address = ipaddress.ip_address(
                socket.inet_ntoa(struct.pack("<I", int(row.address)))
            )
            if address.version == 4 and not address.is_unspecified and not address.is_multicast:
                values.add(str(address))
        if values:
            return tuple(sorted(values, key=ipaddress.ip_address))
    except (AttributeError, OSError, RuntimeError, ValueError):
        pass
    return _fallback_local_ipv4()


def _endpoint_protocol(endpoint: object) -> str:
    return str(getattr(endpoint, "protocol", "udp") or "udp").casefold()


def _capture_ipv4_address(raw: object) -> ipaddress.IPv4Address | None:
    """Map endpoint-table addresses that can represent an IPv4 flow.

    Windows may expose a dual-stack socket as ``::`` and may expose an IPv4
    peer as an IPv4-mapped IPv6 address.  Neither case proves that game traffic
    is native IPv6.  Treating an ephemeral ``::`` UDP listener as native IPv6
    used to tear down an otherwise healthy IPv4 capture session.
    """
    try:
        address = ipaddress.ip_address(str(raw or ""))
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv4Address):
        return address
    if address.ipv4_mapped is not None:
        return address.ipv4_mapped
    if address.is_unspecified:
        return ipaddress.IPv4Address("0.0.0.0")
    return None


def _requires_native_ipv6(raw: object) -> bool:
    """Return true only for a concrete endpoint that really needs IPv6."""
    try:
        address = ipaddress.ip_address(str(raw or ""))
    except ValueError:
        return False
    return bool(
        isinstance(address, ipaddress.IPv6Address)
        and address.ipv4_mapped is None
        and not address.is_unspecified
        and not address.is_loopback
    )


def relevant_local_ipv4(
    endpoints: Iterable[object],
    *,
    address_provider: Callable[[], Iterable[str]] = list_local_ipv4,
) -> tuple[str, ...]:
    """Resolve the minimum interface set needed for the owned endpoints."""
    concrete: set[str] = set()
    wildcard = False
    has_ipv4 = False
    for endpoint in endpoints:
        address = _capture_ipv4_address(getattr(endpoint, "local_address", ""))
        if address is None:
            continue
        has_ipv4 = True
        if address.is_unspecified:
            wildcard = True
        elif not address.is_multicast:
            concrete.add(str(address))
    if wildcard:
        for raw in address_provider():
            try:
                address = ipaddress.ip_address(str(raw))
            except ValueError:
                continue
            if address.version == 4 and not address.is_unspecified and not address.is_multicast:
                concrete.add(str(address))
    if not concrete and has_ipv4:
        for raw in address_provider():
            try:
                address = ipaddress.ip_address(str(raw))
            except ValueError:
                continue
            if address.version == 4 and not address.is_unspecified and not address.is_multicast:
                concrete.add(str(address))
    return tuple(sorted(concrete, key=ipaddress.ip_address))


@dataclass(frozen=True)
class RawSocketHandle:
    handle: object
    local_address: str
    timestamp_resolution_ns: int = RECEIVE_CLOCK_RESOLUTION_NS
    capture_mode: int = RCVALL_IPLEVEL


def _transport_header(packet: bytes):
    """Return IPv4 routing/transport fields needed for an early safe filter."""
    if len(packet) < 20 or packet[0] >> 4 != 4:
        return None
    header = (packet[0] & 0x0F) * 4
    total = struct.unpack_from("!H", packet, 2)[0]
    if header < 20 or total < header or total > len(packet):
        return None
    protocol_number = packet[9]
    if protocol_number not in (6, 17):
        return None
    protocol = "tcp" if protocol_number == 6 else "udp"
    source = socket.inet_ntoa(packet[12:16])
    destination = socket.inet_ntoa(packet[16:20])
    fragment = struct.unpack_from("!H", packet, 6)[0]
    fragment_offset = (fragment & 0x1FFF) * 8
    if fragment_offset:
        return source, destination, protocol, None, None, packet[:total]
    if total < header + 4:
        return None
    source_port, destination_port = struct.unpack_from("!HH", packet, header)
    return (
        source,
        destination,
        protocol,
        source_port,
        destination_port,
        packet[:total],
    )


def _matches_endpoint(
    source: str,
    destination: str,
    protocol: str,
    source_port: int | None,
    destination_port: int | None,
    endpoint: object,
    local_addresses: set[str],
) -> bool:
    if _endpoint_protocol(endpoint) != protocol:
        return False
    try:
        local_port = int(getattr(endpoint, "local_port", 0) or 0)
        bound = _capture_ipv4_address(getattr(endpoint, "local_address", ""))
    except (TypeError, ValueError):
        return False
    if bound is None or not local_port:
        return False
    bound_text = str(bound)

    def local_match(address: str, port: int | None) -> bool:
        return bool(
            port == local_port
            and (
                bound_text == address
                or (bound.is_unspecified and address in local_addresses)
            )
        )

    outbound = local_match(source, source_port)
    inbound = local_match(destination, destination_port)
    if not (outbound or inbound):
        return False
    if protocol != "tcp":
        return True
    remote_raw = str(getattr(endpoint, "remote_address", "") or "")
    remote = _capture_ipv4_address(remote_raw) if remote_raw else None
    remote_address = str(remote) if remote is not None else remote_raw
    try:
        remote_port = int(getattr(endpoint, "remote_port", 0) or 0)
    except (TypeError, ValueError):
        remote_port = 0
    if not remote_address or not remote_port:
        return True
    if outbound:
        return destination == remote_address and destination_port == remote_port
    return source == remote_address and source_port == remote_port


class WindowsRawSocketReceiver:
    """Multi-interface, bounded-polling receiver for game-owned IPv4 flows."""

    def __init__(
        self,
        endpoints: Iterable[object],
        *,
        socket_factory=socket.socket,
        address_provider: Callable[[], Iterable[str]] = list_local_ipv4,
        select_function=select.select,
        receive_buffer: int = DEFAULT_RECEIVE_BUFFER,
        poll_seconds: float = 0.01,
        clock_ns: Callable[[], int] = time.time_ns,
    ):
        if os.name != "nt" and socket_factory is socket.socket:
            raise RawSocketUnavailable("Windows Raw Socket capture is available only on Windows")
        self.socket_factory = socket_factory
        self.address_provider = address_provider
        self.select_function = select_function
        self.receive_buffer = max(256 * 1024, int(receive_buffer))
        self.poll_seconds = max(0.0, min(0.1, float(poll_seconds)))
        self.clock_ns = clock_ns
        self.handles: dict[str, tuple[RawSocketHandle, int, str]] = {}
        self.endpoints = list(endpoints)
        self.local_addresses: set[str] = set()
        self.counters: Counter[str] = Counter()
        self.errors: dict[str, str] = {}
        self.closed = False
        self.cursor = 0
        try:
            self.refresh(self.endpoints)
        except BaseException:
            self.close()
            raise

    def _open(self, address: str) -> RawSocketHandle:
        receiver = self.socket_factory(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_IP)
        enabled = False
        try:
            receiver.bind((address, 0))
            try:
                receiver.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, self.receive_buffer)
            except OSError:
                self.counters["raw_socket_buffer_warnings"] += 1
            # Never enable NIC promiscuous mode as an automatic workaround.
            receiver.ioctl(SIO_RCVALL, RCVALL_IPLEVEL)
            enabled = True
            receiver.setblocking(False)
            return RawSocketHandle(receiver, address)
        except BaseException:
            if enabled:
                try:
                    receiver.ioctl(SIO_RCVALL, RCVALL_OFF)
                except OSError:
                    pass
            receiver.close()
            raise

    @staticmethod
    def _disable(handle: RawSocketHandle) -> None:
        try:
            handle.handle.ioctl(SIO_RCVALL, RCVALL_OFF)
        except OSError:
            pass
        try:
            handle.handle.close()
        except OSError:
            pass

    def refresh(self, endpoints: Iterable[object]) -> None:
        endpoints = list(endpoints)
        desired = set(
            relevant_local_ipv4(endpoints, address_provider=self.address_provider)
        )
        if not desired:
            ipv6_only = any(
                _requires_native_ipv6(getattr(row, "local_address", ""))
                for row in endpoints
            )
            if ipv6_only:
                raise RawSocketUnavailable(
                    "The game currently has IPv6-only endpoints; the IPv6 receive source is required"
                )
            raise RawSocketUnavailable("No usable IPv4 interface was found for the game endpoints")
        if len(desired) > MAX_RAW_INTERFACES:
            raise RawSocketUnavailable(
                "Too many IPv4 interfaces for the Windows Raw Socket compatibility backend"
            )
        # Concrete native IPv6 game flows must not silently disappear on
        # mixed-stack hosts. IPv6 loopback IPC, ``::`` dual-stack listeners and
        # IPv4-mapped endpoints are not proof of native IPv6 game traffic.
        for row in endpoints:
            if _requires_native_ipv6(getattr(row, "local_address", "")):
                raise RawSocketUnavailable(
                    "IPv6 game endpoints require the bundled IPv6 receive source"
                )

        for address in sorted(desired - set(self.handles), key=ipaddress.ip_address):
            try:
                opened = self._open(address)
                self.handles[address] = (
                    opened,
                    DLT_RAW,
                    "owned game TCP/UDP endpoints",
                )
                self.errors.pop(address, None)
                self.counters["capture_adapter_opens"] += 1
            except OSError as exc:
                self.errors[address] = f"{type(exc).__name__}: {exc}"
                self.counters["raw_socket_adapter_open_failures"] += 1

        # Open replacements before closing old interfaces so a route change does
        # not create an avoidable capture gap.
        if not any(address in desired for address in self.handles):
            self.counters["capture_adapter_errors"] += 1
            details = "; ".join(f"{key}: {value}" for key, value in self.errors.items())
            raise RawSocketUnavailable(
                "No usable Windows Raw Socket interface"
                + (f": {details}" if details else "")
            )
        for row in endpoints:
            required = _capture_ipv4_address(getattr(row, "local_address", ""))
            if required is None:
                continue
            if (not required.is_unspecified and not required.is_loopback
                    and str(required) not in self.handles):
                raise RawSocketUnavailable(
                    f"Required game interface {required} cannot be captured: "
                    + self.errors.get(str(required), "interface unavailable")
                )
        for address in list(self.handles):
            if address not in desired:
                self._disable(self.handles.pop(address)[0])
                self.counters["capture_adapter_removals"] += 1
        self.endpoints = endpoints
        self.local_addresses = set(self.handles)

    def _matches(self, parsed) -> bool:
        source, destination, protocol, source_port, destination_port, _packet = parsed
        if not ({source, destination} & self.local_addresses):
            return False
        if source_port is None:
            # Non-first fragments do not contain ports.  PacketReassembler will
            # validate and associate them with a first fragment before decoding.
            return any(_endpoint_protocol(row) == protocol for row in self.endpoints)
        return any(
            _matches_endpoint(
                source,
                destination,
                protocol,
                source_port,
                destination_port,
                endpoint,
                self.local_addresses,
            )
            for endpoint in self.endpoints
        )

    def next_frame(self):
        sockets = [value[0].handle for value in self.handles.values()]
        if not sockets:
            raise RawSocketUnavailable("All Windows Raw Socket interfaces are closed")
        try:
            readable, _writable, _errors = self.select_function(
                sockets, [], [], self.poll_seconds
            )
        except OSError as exc:
            self.counters["capture_adapter_errors"] += 1
            raise RawSocketUnavailable(f"Windows Raw Socket polling failed: {exc}") from exc
        if not readable:
            return None
        reverse = {
            value[0].handle: (name, value[0]) for name, value in self.handles.items()
        }
        ordered_sockets = sockets[self.cursor:] + sockets[:self.cursor]
        ready_set = set(readable)
        for receiver in ordered_sockets:
            if receiver not in ready_set:
                continue
            item = reverse.get(receiver)
            if item is None:
                continue
            name, handle = item
            try:
                data, _peer = receiver.recvfrom(MAX_IPV4_PACKET)
            except BlockingIOError:
                continue
            except OSError as exc:
                self.errors[name] = f"{type(exc).__name__}: {exc}"
                self.counters["capture_adapter_errors"] += 1
                self._disable(handle)
                self.handles.pop(name, None)
                self.local_addresses.discard(name)
                continue
            if (len(data) >= 20 and data[0] >> 4 == 4
                    and struct.unpack_from("!H", data, 2)[0] > len(data)):
                self.counters["raw_socket_truncated_packets"] += 1
                continue
            parsed = _transport_header(data)
            if parsed is None:
                self.counters["raw_socket_non_game_packets"] += 1
                continue
            if not self._matches(parsed):
                self.counters["raw_socket_filtered_packets"] += 1
                continue
            packet = parsed[-1]
            self.cursor = (sockets.index(receiver) + 1) % len(sockets)
            self.counters["raw_socket_frames_received"] += 1
            self.counters["raw_socket_bytes_received"] += len(packet)
            return CaptureFrame(
                packet,
                self.clock_ns(),
                name,
                DLT_RAW,
                len(packet),
                RECEIVE_CLOCK_RESOLUTION_NS,
            )
        if not self.handles:
            details = "; ".join(self.errors.values())
            raise RawSocketUnavailable(
                "All Windows Raw Socket interfaces failed"
                + (f": {details}" if details else "")
            )
        return None

    def statistics(self, _read_stats=None):
        interfaces = {
            name: {
                "available": True,
                "capture_mode": int(value[0].capture_mode),
                "timestamp_resolution_ns": RECEIVE_CLOCK_RESOLUTION_NS,
                "timestamp_source": "userspace_receive_clock",
                "kernel_drop_visibility": False,
            }
            for name, value in self.handles.items()
        }
        return {
            "available": bool(interfaces),
            "interfaces": interfaces,
            "dropped": 0,
            "kernel_drop_visibility": False,
            "timestamp_source": "userspace_receive_clock",
            "capture_source": "windows_raw_socket",
            "interface_open_errors": dict(self.errors),
        }

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        for handle, _datalink, _description in list(self.handles.values()):
            self._disable(handle)
        self.handles.clear()
        self.local_addresses.clear()


def probe_windows_raw_socket(
    local_address: str,
    *,
    socket_factory=socket.socket,
) -> dict[str, object]:
    """Open and immediately close one receive-only socket for preflight checks."""
    endpoint = type(
        "ProbeEndpoint",
        (),
        {"local_address": local_address, "local_port": 9, "protocol": "udp"},
    )()
    receiver = WindowsRawSocketReceiver(
        [endpoint], socket_factory=socket_factory, address_provider=lambda: (local_address,)
    )
    try:
        handle = receiver.handles[local_address][0]
        return {
            "available": True,
            "local_address": local_address,
            "capture_mode": int(handle.capture_mode),
            "timestamp_resolution_ns": int(handle.timestamp_resolution_ns),
        }
    finally:
        receiver.close()
