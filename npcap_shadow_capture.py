#!/usr/bin/env python3
"""Passively capture this game's encrypted UDP/KCP traffic with Npcap.

This standalone research tool never imports the DPS application and never
loads or calls an Npcap transmit function. It limits capture to UDP traffic on
ports owned by the selected game process and writes packet/KCP metadata to
JSONL for later protocol correlation.
"""

from __future__ import annotations

import argparse
import base64
import ctypes
import hashlib
import ipaddress
import json
import os
import socket
import struct
import sys
import time
from collections import Counter, OrderedDict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Sequence


ERRBUF_SIZE = 256
AF_INET = 2
ERROR_INSUFFICIENT_BUFFER = 122
UDP_TABLE_OWNER_PID = 1
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

DLT_NULL = 0
DLT_EN10MB = 1
DLT_RAW = 12
DLT_IPV4 = 228
DLT_IPV6 = 229

KCP_HEADER = struct.Struct("<IBBHIIII")
KCP_COMMANDS = {
    0x51: "PUSH",
    0x52: "ACK",
    0x53: "WASK",
    0x54: "WINS",
    # This game also uses a fixed-size ACK/range frame whose four trailing
    # words are acknowledgement metadata rather than the standard KCP length
    # field. It is frequently concatenated with a normal PUSH in one UDP
    # datagram, so treating its final word as a payload length hides the PUSH.
    0x55: "ACK_RANGE",
}


class SockAddr(ctypes.Structure):
    _fields_ = [("sa_family", ctypes.c_ushort), ("sa_data", ctypes.c_ubyte * 14)]


class PcapAddr(ctypes.Structure):
    pass


PcapAddrPointer = ctypes.POINTER(PcapAddr)
PcapAddr._fields_ = [
    ("next", PcapAddrPointer),
    ("addr", ctypes.POINTER(SockAddr)),
    ("netmask", ctypes.POINTER(SockAddr)),
    ("broadaddr", ctypes.POINTER(SockAddr)),
    ("dstaddr", ctypes.POINTER(SockAddr)),
]


class PcapIf(ctypes.Structure):
    pass


PcapIfPointer = ctypes.POINTER(PcapIf)
PcapIf._fields_ = [
    ("next", PcapIfPointer),
    ("name", ctypes.c_char_p),
    ("description", ctypes.c_char_p),
    ("addresses", PcapAddrPointer),
    ("flags", ctypes.c_uint),
]


class BpfInsn(ctypes.Structure):
    _fields_ = [
        ("code", ctypes.c_ushort),
        ("jt", ctypes.c_ubyte),
        ("jf", ctypes.c_ubyte),
        ("k", ctypes.c_uint32),
    ]


class BpfProgram(ctypes.Structure):
    _fields_ = [("bf_len", ctypes.c_uint), ("bf_insns", ctypes.POINTER(BpfInsn))]


class TimeVal(ctypes.Structure):
    _fields_ = [("tv_sec", ctypes.c_long), ("tv_usec", ctypes.c_long)]


class PcapPacketHeader(ctypes.Structure):
    _fields_ = [
        ("ts", TimeVal),
        ("caplen", ctypes.c_uint32),
        ("length", ctypes.c_uint32),
    ]


class MibUdpRowOwnerPid(ctypes.Structure):
    _fields_ = [
        ("local_addr", ctypes.c_uint32),
        ("local_port", ctypes.c_uint32),
        ("owning_pid", ctypes.c_uint32),
    ]


@dataclass(frozen=True)
class Adapter:
    name: str
    description: str
    addresses: tuple[str, ...]


@dataclass(frozen=True)
class UdpEndpoint:
    pid: int
    local_address: str
    local_port: int


@dataclass(frozen=True)
class ParsedUdp:
    src_ip: str
    dst_ip: str
    src_port: int
    dst_port: int
    payload: bytes


def _decode_native(value: Optional[bytes]) -> str:
    if not value:
        return ""
    for encoding in ("utf-8", "mbcs", "latin-1"):
        try:
            return value.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            pass
    return value.decode("latin-1", errors="replace")


def load_wpcap() -> ctypes.CDLL:
    if os.name != "nt":
        raise RuntimeError("Npcap shadow capture currently supports Windows only")
    npcap_dir = Path(os.environ.get("WINDIR", r"C:\Windows")) / "System32" / "Npcap"
    dll_path = npcap_dir / "wpcap.dll"
    if not dll_path.is_file():
        raise RuntimeError(f"Npcap wpcap.dll was not found: {dll_path}")
    if hasattr(os, "add_dll_directory"):
        load_wpcap._dll_cookie = os.add_dll_directory(str(npcap_dir))  # type: ignore[attr-defined]
    dll = ctypes.CDLL(str(dll_path))
    dll.pcap_findalldevs.argtypes = [ctypes.POINTER(PcapIfPointer), ctypes.c_char_p]
    dll.pcap_findalldevs.restype = ctypes.c_int
    dll.pcap_freealldevs.argtypes = [PcapIfPointer]
    dll.pcap_open_live.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_char_p]
    dll.pcap_open_live.restype = ctypes.c_void_p
    dll.pcap_close.argtypes = [ctypes.c_void_p]
    dll.pcap_datalink.argtypes = [ctypes.c_void_p]
    dll.pcap_datalink.restype = ctypes.c_int
    dll.pcap_compile.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(BpfProgram),
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_uint32,
    ]
    dll.pcap_compile.restype = ctypes.c_int
    dll.pcap_setfilter.argtypes = [ctypes.c_void_p, ctypes.POINTER(BpfProgram)]
    dll.pcap_setfilter.restype = ctypes.c_int
    dll.pcap_freecode.argtypes = [ctypes.POINTER(BpfProgram)]
    dll.pcap_next_ex.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.POINTER(PcapPacketHeader)),
        ctypes.POINTER(ctypes.POINTER(ctypes.c_ubyte)),
    ]
    dll.pcap_next_ex.restype = ctypes.c_int
    dll.pcap_geterr.argtypes = [ctypes.c_void_p]
    dll.pcap_geterr.restype = ctypes.c_char_p
    return dll


def _sockaddr_ip(pointer: ctypes.POINTER(SockAddr)) -> Optional[str]:
    if not pointer:
        return None
    raw = ctypes.string_at(pointer, 28)
    family = struct.unpack_from("<H", raw, 0)[0]
    if family == socket.AF_INET:
        return socket.inet_ntop(socket.AF_INET, raw[4:8])
    if family == socket.AF_INET6:
        return socket.inet_ntop(socket.AF_INET6, raw[8:24])
    return None


def list_adapters(wpcap: ctypes.CDLL) -> list[Adapter]:
    first = PcapIfPointer()
    errbuf = ctypes.create_string_buffer(ERRBUF_SIZE)
    if wpcap.pcap_findalldevs(ctypes.byref(first), errbuf) != 0:
        raise RuntimeError(f"pcap_findalldevs failed: {_decode_native(errbuf.value)}")
    result: list[Adapter] = []
    try:
        current = first
        while current:
            item = current.contents
            addresses: list[str] = []
            address = item.addresses
            while address:
                value = _sockaddr_ip(address.contents.addr)
                if value and value not in addresses:
                    addresses.append(value)
                address = address.contents.next
            result.append(
                Adapter(
                    name=_decode_native(item.name),
                    description=_decode_native(item.description),
                    addresses=tuple(addresses),
                )
            )
            current = item.next
    finally:
        if first:
            wpcap.pcap_freealldevs(first)
    return result


def list_udp_endpoints() -> list[UdpEndpoint]:
    iphlpapi = ctypes.WinDLL("iphlpapi.dll", use_last_error=True)
    function = iphlpapi.GetExtendedUdpTable
    function.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.c_bool,
        ctypes.c_uint32,
        ctypes.c_int,
        ctypes.c_uint32,
    ]
    function.restype = ctypes.c_uint32
    size = ctypes.c_uint32(0)
    result = function(None, ctypes.byref(size), False, AF_INET, UDP_TABLE_OWNER_PID, 0)
    if result not in (0, ERROR_INSUFFICIENT_BUFFER):
        raise OSError(result, "GetExtendedUdpTable size query failed")
    buffer = ctypes.create_string_buffer(size.value)
    result = function(buffer, ctypes.byref(size), False, AF_INET, UDP_TABLE_OWNER_PID, 0)
    if result != 0:
        raise OSError(result, "GetExtendedUdpTable failed")
    count = struct.unpack_from("<I", buffer.raw, 0)[0]
    endpoints: list[UdpEndpoint] = []
    row_size = ctypes.sizeof(MibUdpRowOwnerPid)
    for index in range(count):
        row = MibUdpRowOwnerPid.from_buffer_copy(buffer.raw, 4 + index * row_size)
        address = socket.inet_ntoa(struct.pack("<I", row.local_addr))
        port = socket.ntohs(row.local_port & 0xFFFF)
        endpoints.append(UdpEndpoint(int(row.owning_pid), address, port))
    return endpoints


def process_path(pid: int) -> str:
    kernel32 = ctypes.WinDLL("kernel32.dll", use_last_error=True)
    kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_bool, ctypes.c_uint32]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.QueryFullProcessImageNameW.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_wchar_p,
        ctypes.POINTER(ctypes.c_uint32),
    ]
    kernel32.QueryFullProcessImageNameW.restype = ctypes.c_bool
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        size = ctypes.c_uint32(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return ""
        return buffer.value
    finally:
        kernel32.CloseHandle(handle)


def discover_game(args: argparse.Namespace) -> tuple[int, str, list[UdpEndpoint]]:
    endpoints = list_udp_endpoints()
    if args.pid:
        selected = [row for row in endpoints if row.pid == args.pid]
        if not selected:
            raise RuntimeError(f"PID {args.pid} owns no IPv4 UDP ports")
        return args.pid, process_path(args.pid), selected
    matches: list[tuple[int, str, list[UdpEndpoint]]] = []
    for pid in {row.pid for row in endpoints}:
        path = process_path(pid)
        if Path(path).name.casefold() == args.process_name.casefold():
            matches.append((pid, path, [row for row in endpoints if row.pid == pid]))
    if not matches:
        raise RuntimeError(f"No running {args.process_name!r} process with UDP ports was found")
    return max(matches, key=lambda item: item[0])


def preferred_ipv4() -> Optional[str]:
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("1.1.1.1", 9))
        return str(probe.getsockname()[0])
    except OSError:
        return None
    finally:
        probe.close()


def select_adapter(adapters: Sequence[Adapter], selector: str, local_ip: str) -> tuple[Adapter, str]:
    candidates = list(adapters)
    if selector:
        needle = selector.casefold()
        candidates = [a for a in candidates if needle in a.name.casefold() or needle in a.description.casefold()]
    wanted_ip = local_ip or preferred_ipv4() or ""
    matching = [a for a in candidates if wanted_ip and wanted_ip in a.addresses]
    if len(matching) == 1:
        return matching[0], wanted_ip
    viable: list[tuple[Adapter, str]] = []
    for adapter in candidates:
        for address in adapter.addresses:
            try:
                parsed = ipaddress.ip_address(address)
            except ValueError:
                continue
            if parsed.version == 4 and not parsed.is_loopback and not parsed.is_link_local:
                viable.append((adapter, address))
    if len(viable) == 1:
        return viable[0]
    raise RuntimeError("Could not select one Npcap adapter; specify --adapter or --local-ip")


def make_bpf(local_ip: str, ports: Sequence[int]) -> str:
    ipaddress.ip_address(local_ip)
    clean = sorted({int(port) for port in ports if 0 < int(port) <= 65535})
    if not clean:
        raise ValueError("No valid UDP ports")
    return f"udp and host {local_ip} and ({' or '.join(f'port {port}' for port in clean)})"


def parse_udp_packet(packet: bytes, datalink: int) -> Optional[ParsedUdp]:
    offset = 0
    protocol = 0
    if datalink == DLT_EN10MB:
        if len(packet) < 14:
            return None
        protocol = struct.unpack_from("!H", packet, 12)[0]
        offset = 14
        while protocol in (0x8100, 0x88A8, 0x9100):
            if len(packet) < offset + 4:
                return None
            protocol = struct.unpack_from("!H", packet, offset + 2)[0]
            offset += 4
    elif datalink == DLT_NULL:
        if len(packet) < 4:
            return None
        family = struct.unpack_from("<I", packet, 0)[0]
        protocol = 0x0800 if family == socket.AF_INET else 0x86DD
        offset = 4
    elif datalink in (DLT_RAW, DLT_IPV4, DLT_IPV6):
        version = packet[0] >> 4 if packet else 0
        protocol = 0x0800 if version == 4 else 0x86DD if version == 6 else 0
    else:
        return None
    if protocol == 0x0800:
        if len(packet) < offset + 28 or packet[offset] >> 4 != 4 or packet[offset + 9] != 17:
            return None
        ihl = (packet[offset] & 15) * 4
        if ihl < 20 or len(packet) < offset + ihl + 8:
            return None
        if struct.unpack_from("!H", packet, offset + 6)[0] & 0x1FFF:
            return None
        src_ip = socket.inet_ntop(socket.AF_INET, packet[offset + 12:offset + 16])
        dst_ip = socket.inet_ntop(socket.AF_INET, packet[offset + 16:offset + 20])
        udp_offset = offset + ihl
    elif protocol == 0x86DD:
        if len(packet) < offset + 48 or packet[offset] >> 4 != 6 or packet[offset + 6] != 17:
            return None
        src_ip = socket.inet_ntop(socket.AF_INET6, packet[offset + 8:offset + 24])
        dst_ip = socket.inet_ntop(socket.AF_INET6, packet[offset + 24:offset + 40])
        udp_offset = offset + 40
    else:
        return None
    src_port, dst_port, udp_length = struct.unpack_from("!HHH", packet, udp_offset)
    if udp_length < 8:
        return None
    end = min(len(packet), udp_offset + udp_length)
    return ParsedUdp(src_ip, dst_ip, src_port, dst_port, packet[udp_offset + 8:end])


def parse_kcp_segments(payload: bytes) -> list[dict]:
    segments: list[dict] = []
    offset = 0
    while offset < len(payload):
        if len(payload) - offset < KCP_HEADER.size:
            return []
        conv, cmd, frg, wnd, ts, sn, una, length = KCP_HEADER.unpack_from(payload, offset)
        if cmd not in KCP_COMMANDS:
            return []
        data_offset = offset + KCP_HEADER.size
        if cmd == 0x55:
            # ACK_RANGE has two wire layouts. Variant 1 is a fixed 24-byte
            # inclusive range. Variant 0 has a 20-byte header: the word at +16
            # is the byte length of an array of uint32 sequence numbers that
            # starts at +20. KCP_HEADER therefore already consumed the first
            # array item for variant 0 and must not be used to find its end.
            data_length = 0
            if frg == 1:
                next_offset = offset + KCP_HEADER.size
                ack_words = [length]
            elif frg == 0 and una >= 4 and una % 4 == 0:
                next_offset = offset + 20 + una
                if next_offset > len(payload):
                    return []
                ack_words = list(
                    struct.unpack_from(f"<{una // 4}I", payload, offset + 20)
                )
            else:
                return []
        else:
            data_length = length
            if data_length > len(payload) - data_offset:
                return []
            next_offset = data_offset + data_length
        data = payload[data_offset:data_offset + data_length]
        segments.append({
            "offset": offset, "conv": conv, "command": KCP_COMMANDS[cmd], "command_code": cmd,
            "fragment": frg, "window": wnd, "timestamp": ts, "sequence": sn,
            "unacknowledged": una, "payload_offset": data_offset, "payload_length": length,
            "payload_sha256": hashlib.sha256(data).hexdigest(),
        })
        if cmd == 0x55:
            segments[-1]["ack_sequences"] = ack_words
            segments[-1]["payload_length"] = 0
        offset = next_offset
    return segments


class DuplicateTracker:
    def __init__(self, limit: int = 500_000):
        self.limit = limit
        self.seen: OrderedDict[tuple, None] = OrderedDict()

    def mark(self, direction: str, segment: dict) -> bool:
        key = (direction, segment["conv"], segment["command_code"], segment["fragment"],
               segment["sequence"], segment["payload_length"], segment["payload_sha256"])
        duplicate = key in self.seen
        if duplicate:
            self.seen.move_to_end(key)
        else:
            self.seen[key] = None
            if len(self.seen) > self.limit:
                self.seen.popitem(last=False)
        return duplicate


def utc_iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="microseconds")


def write_jsonl(stream, record: dict) -> None:
    stream.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")


def capture(args: argparse.Namespace) -> int:
    wpcap = load_wpcap()
    adapters = list_adapters(wpcap)
    if args.list:
        for adapter in adapters:
            print(f"{adapter.name} | {adapter.description} | {', '.join(adapter.addresses)}")
        return 0
    pid, executable, endpoints = discover_game(args)
    adapter, local_ip = select_adapter(adapters, args.adapter, args.local_ip)
    ports = sorted({row.local_port for row in endpoints})
    bpf = make_bpf(local_ip, ports)
    errbuf = ctypes.create_string_buffer(ERRBUF_SIZE)
    handle = wpcap.pcap_open_live(adapter.name.encode(), 65535, 0, 200, errbuf)
    if not handle:
        raise RuntimeError(f"pcap_open_live failed: {_decode_native(errbuf.value)}")
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output = Path(args.output) if args.output else Path(".codex-tmp/npcap-shadow") / f"npcap_shadow_{stamp}.jsonl"
    output.parent.mkdir(parents=True, exist_ok=True)
    started = time.time()
    counters: Counter[str] = Counter()
    tracker = DuplicateTracker()
    try:
        program = BpfProgram()
        if wpcap.pcap_compile(handle, ctypes.byref(program), bpf.encode("ascii"), 1, 0xFFFFFFFF) != 0:
            raise RuntimeError(_decode_native(wpcap.pcap_geterr(handle)))
        try:
            if wpcap.pcap_setfilter(handle, ctypes.byref(program)) != 0:
                raise RuntimeError(_decode_native(wpcap.pcap_geterr(handle)))
        finally:
            wpcap.pcap_freecode(ctypes.byref(program))
        datalink = int(wpcap.pcap_datalink(handle))
        with output.open("w", encoding="utf-8", newline="\n") as stream:
            write_jsonl(stream, {"record_type": "capture_start", "schema": 1, "started_epoch": started,
                "started_utc": utc_iso(started), "pid": pid, "process": executable,
                "adapter": asdict(adapter), "local_ip": local_ip, "game_udp_ports": ports,
                "bpf": bpf, "datalink": datalink, "mode": "passive_read_only"})
            stream.flush()
            print("NPCAP_SHADOW_READY", flush=True)
            print(f"output={output.resolve()}", flush=True)
            print(f"pid={pid} local_ip={local_ip} ports={','.join(map(str, ports))}", flush=True)
            deadline = started + args.duration if args.duration > 0 else None
            packet_index = 0
            while deadline is None or time.time() < deadline:
                header_ptr = ctypes.POINTER(PcapPacketHeader)()
                data_ptr = ctypes.POINTER(ctypes.c_ubyte)()
                status = wpcap.pcap_next_ex(handle, ctypes.byref(header_ptr), ctypes.byref(data_ptr))
                if status == 0:
                    continue
                if status == -2:
                    break
                if status < 0:
                    raise RuntimeError(_decode_native(wpcap.pcap_geterr(handle)))
                header = header_ptr.contents
                raw = ctypes.string_at(data_ptr, header.caplen)
                udp = parse_udp_packet(raw, datalink)
                if not udp:
                    counters["unparsed"] += 1
                    continue
                packet_index += 1
                timestamp = float(header.ts.tv_sec) + float(header.ts.tv_usec) / 1_000_000
                direction = "outbound" if udp.src_ip == local_ip else "inbound" if udp.dst_ip == local_ip else "unknown"
                segments = parse_kcp_segments(udp.payload)
                for segment in segments:
                    segment["duplicate"] = tracker.mark(direction, segment)
                    counters[f"kcp_{segment['command'].lower()}"] += 1
                    counters["kcp_duplicate"] += int(segment["duplicate"])
                counters["udp_packets"] += 1
                counters[f"direction_{direction}"] += 1
                counters["udp_payload_bytes"] += len(udp.payload)
                counters["kcp_datagrams" if segments else "non_kcp_datagrams"] += 1
                write_jsonl(stream, {"record_type": "udp_packet", "packet_index": packet_index,
                    "timestamp_epoch": timestamp, "timestamp_utc": utc_iso(timestamp), "direction": direction,
                    "src_ip": udp.src_ip, "src_port": udp.src_port, "dst_ip": udp.dst_ip, "dst_port": udp.dst_port,
                    "captured_length": int(header.caplen), "wire_length": int(header.length),
                    "udp_payload_length": len(udp.payload), "udp_payload_sha256": hashlib.sha256(udp.payload).hexdigest(),
                    "udp_payload_base64": base64.b64encode(udp.payload).decode("ascii"), "kcp_segments": segments})
                if packet_index % 25 == 0:
                    stream.flush()
            finished = time.time()
            summary = {"record_type": "capture_end", "finished_epoch": finished,
                "finished_utc": utc_iso(finished), "elapsed_seconds": round(finished - started, 3),
                "counts": dict(sorted(counters.items()))}
            write_jsonl(stream, summary)
        print("NPCAP_SHADOW_FINISHED", flush=True)
        print(json.dumps(summary, ensure_ascii=False), flush=True)
        return 0
    finally:
        wpcap.pcap_close(handle)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pid", type=int)
    parser.add_argument("--process-name", default="C7-Win64-Shipping.exe")
    parser.add_argument("--adapter", default="")
    parser.add_argument("--local-ip", default="")
    parser.add_argument("--duration", type=float, default=600.0)
    parser.add_argument("--output", default="")
    parser.add_argument("--list", action="store_true")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    try:
        return capture(build_parser().parse_args(argv))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"Npcap shadow capture failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
